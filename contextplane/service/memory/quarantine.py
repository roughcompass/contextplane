"""Quarantine claims by provenance, and put them back.

E4-T2. The shape is settled by
`.develop/adr/0016-quarantine-is-a-materialised-state-on-its-own-column.md`, and
the two decisions that matter most here are both about where things are *not*
evaluated.

**The predicate is evaluated at write time. No read ever sees it.** Applying a
quarantine matches a set of claims once, stamps `quarantined_at` on each, and
records which rows were matched. Every read then filters on the column, which it
was already doing for `status` -- so no serving path has to learn anything, and
no future serving path can forget. A read-time predicate would have been the
alternative, and its cost is that every scan added from now on has to remember
to join it.

**Revert restores the set that was withheld, not the set the predicate matches
now.** The graph moves: claims arrive, get consolidated, get superseded. A revert
that re-ran the predicate would restore a different population than it took
away, and the difference would be silent. `claim_quarantine_members` is what
makes revert exact.

**What quarantine is not.** It does not delete, and it does not decide anything
about retention. A quarantined claim is withheld and recoverable; that is the
whole promise. Disposal is `retention/`'s job and answers to a legal basis this
mechanism does not have.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.retention import derivatives, policies
from contextplane.types import Clock, TenantContext

#: Who may quarantine. The same bar the curation actions set, and for the same
#: reason: withholding content other people are relying on is a decision, not
#: something a machine's own evidence implies.
_OPERATOR_ROLES: Final[frozenset[str]] = frozenset({"producer", "admin"})

#: The provenance dimensions a quarantine may select on, and the only ones.
#:
#: Closed deliberately. An open predicate language over claim columns would let
#: an operator withhold by confidence, or by subject, or by author -- none of
#: which is a *provenance* statement, and each of which is a way to make content
#: disappear for a reason nobody wrote down. E4 asks for provenance-scoped
#: quarantine, and this is the scope.
#:
#: Each maps to an indexed column: `ix_memory_prov_evidence` for the connector
#: run, `ix_memory_claims_strategy` for the extractor version, and
#: `ix_memory_claims_namespace` for the namespace.
SELECTOR_CONNECTOR_RUN: Final = "connector_run"
SELECTOR_STRATEGY: Final = "strategy_id"
SELECTOR_NAMESPACE_PREFIX: Final = "namespace_prefix"
SELECTORS: Final[tuple[str, ...]] = (SELECTOR_CONNECTOR_RUN, SELECTOR_STRATEGY, SELECTOR_NAMESPACE_PREFIX)

_MATCH_SQL: Final[dict[str, str]] = {
    SELECTOR_CONNECTOR_RUN: (
        "SELECT c.claim_id FROM memory_claims c "
        "  JOIN memory_claim_provenance p ON p.claim_id = c.claim_id "
        " WHERE c.owning_tenant_id = :tid "
        "   AND p.evidence_kind = 'connector_run' AND p.evidence_ref = :value"
    ),
    SELECTOR_STRATEGY: (
        "SELECT c.claim_id FROM memory_claims c WHERE c.owning_tenant_id = :tid AND c.strategy_id = :value"
    ),
    SELECTOR_NAMESPACE_PREFIX: (
        "SELECT c.claim_id FROM memory_claims c " "WHERE c.owning_tenant_id = :tid AND c.namespace LIKE :value || '%'"
    ),
}


@dataclasses.dataclass(frozen=True)
class Quarantine:
    """One applied quarantine, and what it reached."""

    quarantine_id: uuid.UUID
    selector: str
    value: str
    matched: tuple[uuid.UUID, ...]
    applied_at: datetime.datetime

    @property
    def matched_count(self) -> int:
        """How many claims this quarantine withheld when it was applied."""
        return len(self.matched)


class QuarantineService:
    """Apply and revert provenance-scoped quarantines."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._factory = session_factory
        self._clock = clock

    # -- preview -----------------------------------------------------------

    async def preview(self, ctx: TenantContext, *, selector: str, value: str) -> tuple[uuid.UUID, ...]:
        """What applying this predicate would reach, right now.

        **A point-in-time answer, and callers must present it as one.** The
        graph moves between a preview and an apply, so a preview acted on ten
        minutes later reached a different set. This returns the ids rather than
        a count so a caller that wants to show the difference can.
        """
        self._require_operator(ctx)
        async with self._factory() as session:
            return await self._match(session, ctx, selector=selector, value=value)

    # -- apply -------------------------------------------------------------

    async def apply(self, ctx: TenantContext, *, selector: str, value: str, reason: str) -> Quarantine:
        """Withhold every claim this predicate matches, and record which ones.

        One transaction. The column write, the ledger, the membership rows and
        the propagation enqueue commit together, because a quarantine that
        recorded its membership but did not withhold -- or withheld without
        recording -- is worse than one that failed outright.
        """
        self._require_operator(ctx)
        if not reason.strip():
            raise ValidationError("a quarantine needs a reason; withheld content with no stated cause is unreviewable")
        now = self._clock.now()

        async with self._factory() as session, session.begin():
            matched = await self._match(session, ctx, selector=selector, value=value)
            if not matched:
                raise ConflictError(
                    f"{selector}={value!r} matches no claim in this tenant; "
                    "applying it would record a quarantine that withheld nothing"
                )
            quarantine_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO claim_quarantines "
                    "  (quarantine_id, tenant_id, predicate, matched_count, reason, applied_by, applied_at) "
                    "VALUES (:qid, :tid, CAST(:pred AS JSONB), :n, :reason, :actor, :now)"
                ),
                {
                    "qid": quarantine_id,
                    "tid": ctx.tenant_id,
                    "pred": json.dumps({"selector": selector, "value": value}, sort_keys=True),
                    "n": len(matched),
                    "reason": reason,
                    "actor": ctx.actor_id,
                    "now": now,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO claim_quarantine_members (quarantine_id, claim_id) "
                    "SELECT :qid, unnest(CAST(:claims AS UUID[]))"
                ),
                {"qid": quarantine_id, "claims": list(matched)},
            )
            # Only claims not already withheld by an earlier quarantine move.
            # Overwriting `quarantined_at` would relabel content as withheld
            # later than it was, and the earlier quarantine's revert would then
            # restore something the later one still means to withhold.
            await session.execute(
                text(
                    "UPDATE memory_claims SET quarantined_at = CAST(:now AS TIMESTAMPTZ) "
                    " WHERE claim_id = ANY(CAST(:claims AS UUID[])) AND quarantined_at IS NULL"
                ),
                {"now": now, "claims": list(matched)},
            )
            await self._propagate(session, ctx, matched, now=now)

        return Quarantine(quarantine_id=quarantine_id, selector=selector, value=value, matched=matched, applied_at=now)

    # -- revert ------------------------------------------------------------

    async def revert(self, ctx: TenantContext, *, quarantine_id: uuid.UUID) -> int:
        """Put back exactly what this quarantine withheld. Returns how many.

        Restores from the recorded membership rather than by re-running the
        predicate, so the set restored is the set taken. A claim still held by
        another, unreverted quarantine stays withheld -- otherwise reverting the
        older of two overlapping quarantines would silently undo the newer one.
        """
        self._require_operator(ctx)
        now = self._clock.now()

        async with self._factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT reverted_at FROM claim_quarantines "
                        " WHERE quarantine_id = :qid AND tenant_id = :tid FOR UPDATE"
                    ),
                    {"qid": quarantine_id, "tid": ctx.tenant_id},
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError(f"quarantine {quarantine_id} not found")
            if row.reverted_at is not None:
                raise ConflictError(f"quarantine {quarantine_id} was already reverted at {row.reverted_at.isoformat()}")

            await session.execute(
                text(
                    "UPDATE claim_quarantines SET reverted_by = :actor, reverted_at = CAST(:now AS TIMESTAMPTZ) "
                    " WHERE quarantine_id = :qid"
                ),
                {"qid": quarantine_id, "actor": ctx.actor_id, "now": now},
            )
            restored = (
                (
                    await session.execute(
                        text(
                            "UPDATE memory_claims SET quarantined_at = NULL "
                            " WHERE claim_id IN ("
                            "         SELECT claim_id FROM claim_quarantine_members WHERE quarantine_id = :qid) "
                            "   AND quarantined_at IS NOT NULL "
                            "   AND NOT EXISTS ("
                            "         SELECT 1 FROM claim_quarantine_members m "
                            "           JOIN claim_quarantines q ON q.quarantine_id = m.quarantine_id "
                            "          WHERE m.claim_id = memory_claims.claim_id "
                            "            AND m.quarantine_id <> :qid "
                            "            AND q.reverted_at IS NULL) "
                            " RETURNING claim_id"
                        ),
                        {"qid": quarantine_id},
                    )
                )
                .scalars()
                .all()
            )
            await self._propagate(session, ctx, tuple(restored), now=now)
        return len(restored)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _require_operator(ctx: TenantContext) -> None:
        if not (set(ctx.roles) & _OPERATOR_ROLES):
            raise PermissionError("quarantining claims requires the producer or admin role")

    @staticmethod
    async def _match(session: AsyncSession, ctx: TenantContext, *, selector: str, value: str) -> tuple[uuid.UUID, ...]:
        """Every claim this tenant owns that the predicate selects.

        Tenant-scoped in the query rather than filtered afterwards, for the
        reason `claim_serving` now asserts rather than re-derives: a predicate
        that could reach another tenant's rows is a cross-tenant write however
        carefully the result is trimmed.
        """
        if selector not in _MATCH_SQL:
            raise ValidationError(f"unknown quarantine selector {selector!r}; legal values are {list(SELECTORS)}")
        if not value.strip():
            raise ValidationError(f"{selector} needs a value; an empty one matches everything or nothing by accident")
        rows = await session.execute(text(_MATCH_SQL[selector]), {"tid": ctx.tenant_id, "value": value})
        # Sorted and de-duplicated: the connector-run join can return a claim
        # once per piece of evidence, and a membership insert would then refuse
        # the duplicate. Sorting makes the recorded set reproducible.
        return tuple(sorted(set(rows.scalars().all())))

    @staticmethod
    async def _propagate(
        session: AsyncSession, ctx: TenantContext, claims: tuple[uuid.UUID, ...], *, now: datetime.datetime
    ) -> None:
        """Rebuild every derivative of these claims, so the index follows.

        The correctness of a quarantine does not depend on this landing: the
        serving predicate refuses a withheld claim in the same transaction that
        withheld it. What this buys is recall -- a withheld claim's vectors
        would otherwise sit in the ANN index occupying candidate slots that live
        claims could have used.

        `TRIGGER_POLICY_CHANGE` rather than erasure or revocation, which is also
        why no tombstone is passed: nothing is being destroyed, and a tombstone
        would claim otherwise on a record an auditor reads.
        """
        if not claims:
            return
        await derivatives.enqueue_for_sources(
            session,
            tenant_id=ctx.tenant_id,
            record_class=policies.RECORD_MEMORY_CLAIM,
            source_ids=list(claims),
            operation=derivatives.OPERATION_REBUILD,
            trigger=derivatives.TRIGGER_POLICY_CHANGE,
            now=now,
        )


__all__ = [
    "SELECTORS",
    "SELECTOR_CONNECTOR_RUN",
    "SELECTOR_NAMESPACE_PREFIX",
    "SELECTOR_STRATEGY",
    "Quarantine",
    "QuarantineService",
]
