"""The sweep that turns consolidated claims into promotion proposals -- and, per
tenant opt-in, straight into the canonical graph.

Consolidation decides a claim is settled; nothing upstream of this worker ever
calls `PromotionService.propose`. Without this sweep a consolidated claim sits in
staging forever, eligible but never proposed, and the review queue a reviewer
looks at stays empty no matter how much staged truth has accumulated behind it.

**Every claim's outcome is its own**, matching `ConsolidationSweepWorker`: one
claim whose neighbourhood is pathological must not stop the ones behind it, so
each claim is proposed, guardrail-checked, and possibly accepted inside its own
try/except.

**Auto-acceptance is the sharp edge, so it goes through the same two gates a
human reviewer's request does, not around them.** `GuardrailService.may_auto_promote`
decides whether a claim may skip review at all (allowlist empty by default, so
nothing does on a fresh deployment); `PromotionService.accept` decides whether
*this* actor may act on *this* proposal, via `_assert_may_review`'s owner-tenant
and role check. The sweep does not bypass the second gate -- it satisfies it, by
constructing a system-curator identity whose tenant matches the proposal's owner
and whose roles include `admin`. A proposal this identity does not own still
fails `_assert_may_review` exactly as it would for a human, which is what keeps
"the sweep's actor is privileged" from becoming "the sweep's actor is exempt".

**The audit trail says "machine", not just "accepted".** `accept()` already
writes its own audit row (actor_id, target, promotion id) -- that row alone would
look identical whether a human or this sweep produced it, since roles are not
part of what gets logged. So the sweep writes a second, its own row naming the
guardrail decision that permitted the promotion and the system-curator actor
that made it, right after `accept()` returns. It is deliberately not folded into
`accept()`'s own audit payload: that would mean this worker editing the shared
promotion module's write path for every caller, human reviewers included, to
serve one caller's bookkeeping.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from typing import Final

from prometheus_client import Counter, Gauge
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.audit import actions
from contextplane.service.memory.promotion import PromotionService, Proposal
from contextplane.service.memory.promotion_guardrails import AutoPromoteDecision, GuardrailService
from contextplane.storage.models import Actor
from contextplane.types import Clock

_log = logging.getLogger(__name__)

# Proposals attempted per tick. Bounded for the same reason the consolidation sweep's
# batch is: a tick that ran for minutes holding rows would starve everything else
# reconciling in the meantime.
DEFAULT_BATCH_SIZE = 100

# The system-curator identity's own actor_kind and display name. Distinct from
# 'sync_worker' on purpose: reusing the sync-worker identity would make an
# auto-promotion indistinguishable, in the actor table, from a connector run --
# and the whole point of this identity is that it is neither a connector nor a
# human.
_SYSTEM_CURATOR_KIND: Final[str] = "system_curator"
_SYSTEM_CURATOR_DISPLAY_NAME: Final[str] = "system-curator"

# The roles the sweep presents to `_assert_may_review`. The sync-worker identity
# precedent's own roles (`["sync_worker"]`) fail that check's `REVIEW_ROLES` test,
# so this is a deliberate, different choice made for this caller alone -- see the
# module docstring.
_SYSTEM_CURATOR_ROLES: Final[frozenset[str]] = frozenset({"admin"})

_SWEPT = Counter(
    "contextplane_promotion_sweep_total",
    "Claims the promotion sweep considered, by outcome.",
    ["outcome"],
)

_PENDING = Gauge(
    "contextplane_promotion_sweep_pending",
    "Consolidated, subject-resolved staged claims never yet proposed for promotion.",
)

# One actor_id per tenant, cached for the life of the process. Mirrors
# `contextplane.ingest.runner.resolve_sync_actor`'s cache -- the identity is looked up
# or provisioned once per tenant, not once per tick, so a busy sweep is not also a
# busy actors-table writer.
_actor_cache: dict[uuid.UUID, uuid.UUID] = {}


@dataclasses.dataclass(frozen=True)
class SweepReport:
    """What one tick did. Returned rather than only logged so tests can assert."""

    considered: int
    auto_promoted: int
    awaiting_review: int
    not_eligible: int
    failed: int

    @property
    def had_work(self) -> bool:
        return self.considered > 0


class PromotionSweepWorker:
    """Proposes consolidated claims for promotion, and auto-accepts the ones the
    tenant's own guardrails permit."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        promotion: PromotionService,
        guardrails: GuardrailService,
        *,
        clock: Clock,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._promotion = promotion
        self._guardrails = guardrails
        self._clock = clock
        self._batch_size = batch_size

    async def run_once(self) -> SweepReport:
        candidates = await self._candidates()
        await self._refresh_pending_gauge()

        if not candidates:
            return SweepReport(considered=0, auto_promoted=0, awaiting_review=0, not_eligible=0, failed=0)

        auto_promoted = awaiting_review = not_eligible = failed = 0
        for claim_id in candidates:
            try:
                proposal = await self._promotion.propose(claim_id)
                if proposal is None:
                    # Not promotable right now -- below the floor, contested, no
                    # canonical target, or already refused at this authority. Not a
                    # failure: it stays a candidate and is re-assessed next tick, in
                    # case whatever blocked it stops blocking it.
                    not_eligible += 1
                    _SWEPT.labels(outcome="not_eligible").inc()
                    continue

                decision = await self._guardrails.may_auto_promote(
                    tenant_id=proposal.owner_tenant_id,
                    predicate=proposal.predicate,
                    high_impact=proposal.high_impact,
                    eligible=True,
                    author_is_owner=proposal.author_tenant_id == proposal.owner_tenant_id,
                )
                if decision.permitted:
                    await self._auto_promote(proposal, decision)
                    auto_promoted += 1
                    _SWEPT.labels(outcome="auto_promoted").inc()
                else:
                    # Left open. The queue is where a human reviewer finds it.
                    awaiting_review += 1
                    _SWEPT.labels(outcome="awaiting_review").inc()
            except Exception:  # noqa: BLE001 - see comment above
                # Logged with the claim, same reasoning as the consolidation sweep:
                # a report that only carries a count leaves nobody able to find the
                # row that is stuck.
                _log.exception("promotion_sweep.sweep_failed claim_id=%s", claim_id)
                _SWEPT.labels(outcome="failed").inc()
                failed += 1
                continue

        await self._refresh_pending_gauge()
        report = SweepReport(
            considered=len(candidates),
            auto_promoted=auto_promoted,
            awaiting_review=awaiting_review,
            not_eligible=not_eligible,
            failed=failed,
        )
        if failed:
            _log.warning(
                "promotion_sweep.run considered=%d auto_promoted=%d awaiting_review=%d failed=%d",
                report.considered,
                report.auto_promoted,
                report.awaiting_review,
                report.failed,
            )
        return report

    async def _candidates(self) -> list[uuid.UUID]:
        """Staged, consolidated, subject-resolved claims never yet proposed.

        `promotion_state IS NULL` is what makes a claim disappear from this query
        the moment `propose` succeeds for it -- `propose` sets it to 'proposed' in
        the same transaction that inserts the proposal row, so there is no window
        in which the same claim could be proposed twice by two ticks.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT claim_id FROM memory_claims "
                        "WHERE status = 'staged' AND t_invalidated_at IS NULL "
                        "  AND subject_entity_id IS NOT NULL "
                        "  AND consolidated_at IS NOT NULL "
                        "  AND promotion_state IS NULL "
                        "ORDER BY created_at "
                        "LIMIT :lim"
                    ),
                    {"lim": self._batch_size},
                )
            ).all()
        return [r.claim_id for r in rows]

    async def _refresh_pending_gauge(self) -> None:
        async with self._session_factory() as session:
            pending = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM memory_claims "
                        "WHERE status = 'staged' AND t_invalidated_at IS NULL "
                        "  AND subject_entity_id IS NOT NULL AND consolidated_at IS NOT NULL "
                        "  AND promotion_state IS NULL"
                    )
                )
            ).scalar_one()
        _PENDING.set(pending)

    async def _auto_promote(self, proposal: Proposal, decision: AutoPromoteDecision) -> None:
        """Resolve the system-curator actor, accept the proposal as that actor, and
        write the sweep's own wrapper audit row.

        Actor resolution runs in its own committed transaction before `accept` is
        called: `accept` writes `created_by`/`promoted_by` columns that reference
        `actors.actor_id`, so the row has to exist and be visible before `accept`
        opens its own transaction against it.
        """
        async with self._session_factory() as session, session.begin():
            actor_id = await resolve_system_curator_actor(session, proposal.owner_tenant_id, clock=self._clock)

        promotion_id = await self._promotion.accept(
            proposal.proposal_id,
            actor_tenant_id=proposal.owner_tenant_id,
            actor_id=actor_id,
            roles=_SYSTEM_CURATOR_ROLES,
            auto_promoted=True,
        )

        try:
            await self._write_auto_promotion_audit(proposal, promotion_id, actor_id, decision)
        except Exception:  # noqa: BLE001 - see comment above
            # The promotion itself already committed -- losing this second,
            # sweep-owned marker must not roll it back or count an already-real
            # promotion as a failed claim. It is still loud: without this row an
            # operator scanning the audit log has no way to tell this promotion
            # apart from a human reviewer's short of cross-referencing the actor
            # table by hand.
            _log.exception(
                "promotion_sweep.auto_promotion_audit_failed proposal_id=%s promotion_id=%s",
                proposal.proposal_id,
                promotion_id,
            )

    async def _write_auto_promotion_audit(
        self,
        proposal: Proposal,
        promotion_id: uuid.UUID,
        actor_id: uuid.UUID,
        decision: AutoPromoteDecision,
    ) -> None:
        now = self._clock.now()
        payload = {
            "promotion_id": str(promotion_id),
            "proposal_id": str(proposal.proposal_id),
            "predicate": proposal.predicate,
            "auto_promoted": True,
            "system_actor_id": str(actor_id),
            "guardrail_decision": {
                "permitted": decision.permitted,
                "blocked_by": list(decision.blocked_by),
            },
        }
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO audit_log "
                    "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                    "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                    "VALUES (:audit_id, :tid, :aid, :action, 'memory_claim', :target, NULL, "
                    "        CAST(:after AS JSONB), :now, NULL, NULL)"
                ),
                {
                    "audit_id": uuid.uuid4(),
                    "tid": proposal.owner_tenant_id,
                    "aid": actor_id,
                    "action": actions.CLAIM_AUTO_PROMOTED,
                    "target": proposal.claim_id,
                    "after": json.dumps(payload, sort_keys=True),
                    "now": now,
                },
            )


async def resolve_system_curator_actor(session: AsyncSession, tenant_id: uuid.UUID, *, clock: Clock) -> uuid.UUID:
    """Return the actor_id for this tenant's system-curator identity, provisioning it
    on first use.

    Mirrors `contextplane.ingest.runner.resolve_sync_actor`'s cache-then-provision
    shape and its deterministic, clearly-non-human `oidc_subject` sentinel, with its
    own `actor_kind` so this identity is never mistaken for that one: a promotion
    the sweep accepted and a connector sync run are different things, and sharing
    an actor would make the audit log unable to say which happened.
    """
    if tenant_id in _actor_cache:
        return _actor_cache[tenant_id]

    result = await session.execute(
        select(Actor).where(
            Actor.tenant_id == tenant_id,
            Actor.display_name == _SYSTEM_CURATOR_DISPLAY_NAME,
            Actor.actor_kind == _SYSTEM_CURATOR_KIND,
        )
    )
    actor = result.scalar_one_or_none()
    if actor is None:
        actor = Actor(
            actor_id=uuid.uuid4(),
            tenant_id=tenant_id,
            display_name=_SYSTEM_CURATOR_DISPLAY_NAME,
            actor_kind=_SYSTEM_CURATOR_KIND,
            oidc_subject=f"system-curator:{tenant_id.hex}",
            created_at=clock.now(),
        )
        session.add(actor)
        await session.flush()
        _log.info("promotion_sweep: provisioned system-curator actor %s for tenant=%s", actor.actor_id, tenant_id)

    _actor_cache[tenant_id] = actor.actor_id
    return actor.actor_id


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "PromotionSweepWorker",
    "SweepReport",
    "resolve_system_curator_actor",
]
