"""Reconciling claims within staging: duplicates collapse, conflicts resolve.

Without this, a staging store only grows. The same assertion arrives from ten
sessions and becomes ten rows; two sources disagree and both sit there forever. What
consolidation does is decide, for each newly staged claim, whether it adds something,
replaces something, or says nothing new.

**Authority first, recency second.** A higher-authority claim supersedes a lower one
regardless of which arrived later. Recency decides only among sources of comparable
authority. The alternative -- newest wins -- is the behaviour this whole design exists
to reject: it means a model's guess overwrites an owner's published contract because
it happened to be observed this morning.

**A lower-authority claim never supersedes a higher one.** It is recorded contested,
and if it concerns another tenant's capability it becomes something the owner is asked
about rather than something that quietly replaces their assertion.

**Cross-tenant is gated on the tenant columns, never on rank.** "Different tenant"
routes a proposal; "lower rank" contests and can supersede. One ordinal cannot express
both, which is why both tenant columns are persisted alongside the tier.

**Nothing is overwritten.** A superseded claim is closed bi-temporally and the
survivor is a new row, so the chain stays queryable. That is what makes "what did we
believe last month, and why did it change" answerable, and what makes a mistaken
supersession recoverable.

**Every decision is audited, including the decision to do nothing.** A sweep that
recorded only its changes would be indistinguishable from a sweep that never ran.

**Idempotent by construction.** Re-running over an unchanged neighbourhood produces no
new rows, no duplicate audit entries, and no confidence drift -- because a closed claim
is excluded from the neighbourhood and a claim already superseded cannot be closed
again.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.audit import actions
from registry.service.authority import SOURCE_AUTHORITY_RANK
from registry.service.claim_compare import (
    COMPATIBLE,
    INCOMPATIBLE,
    intervals_overlap,
    is_near_duplicate,
    values_compatible,
)
from registry.service.claims import ClaimService
from registry.service.contest import resolve_contests_for
from registry.service.global_vocabulary import CARDINALITY_SINGLE
from registry.types import Clock

# What consolidation decided about one claim.
DECISION_ADD = "add"
DECISION_UPDATE = "update"
DECISION_NOOP = "noop"
DECISION_CONTESTED = "contested"
DECISION_PROPOSAL = "proposal"

DECISIONS = frozenset({DECISION_ADD, DECISION_UPDATE, DECISION_NOOP, DECISION_CONTESTED, DECISION_PROPOSAL})

# Why a claim stopped being current. Distinct from the decision that closed it: a
# reviewer asking "what did we stop believing" wants the reason, and "lost a
# conflict" and "was a duplicate" are different histories.
REASON_LOST_CONFLICT = "lost_conflict"
REASON_CLUSTER_COLLAPSED = "cluster_collapsed"
REASON_HUMAN_CONFIRMED = "human_confirmed"
REASON_CURATOR_REPLACED = "curator_replaced"

MATCHED_EXACT = "exact_value"
MATCHED_SEMANTIC = "semantic"

_DECIDED = Counter(
    "registry_claim_consolidation_decisions_total",
    "Consolidation decisions, by decision and predicate.",
    ["decision", "predicate"],
)

_AUDIT_ACTION_BY_DECISION = {
    DECISION_ADD: actions.CLAIM_CONSOLIDATED_ADD,
    DECISION_UPDATE: actions.CLAIM_CONSOLIDATED_UPDATE,
    DECISION_NOOP: actions.CLAIM_CONSOLIDATED_NOOP,
    DECISION_CONTESTED: actions.CLAIM_CONTESTED,
    DECISION_PROPOSAL: actions.CLAIM_PROPOSAL_ROUTED,
}

# A neighbourhood larger than this is not compared exhaustively. Comparison is
# pairwise, so cost grows with the square; a subject carrying hundreds of live claims
# under one predicate is already pathological and the right response is to say so
# rather than to spend minutes on it.
MAX_NEIGHBOURHOOD = 50


@dataclasses.dataclass(frozen=True)
class Neighbour:
    """A live claim sharing a candidate's subject and predicate."""

    claim_id: uuid.UUID
    value: object
    value_type: str
    value_entity_id: uuid.UUID | None
    value_cardinality: str
    authority: str
    owning_tenant_id: uuid.UUID | None
    author_tenant_id: uuid.UUID
    created_at: datetime.datetime
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    is_confirmed: bool

    @property
    def rank(self) -> int:
        return SOURCE_AUTHORITY_RANK.get(self.authority, len(SOURCE_AUTHORITY_RANK))


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What consolidation did, and enough to explain it."""

    claim_id: uuid.UUID
    decision: str
    reason: str
    superseded: tuple[uuid.UUID, ...] = ()
    collapsed: tuple[uuid.UUID, ...] = ()
    contested_with: tuple[uuid.UUID, ...] = ()
    # True when the sweep found nothing to decide because it had already decided.
    # Distinguished from an ordinary no-op so a caller can tell "there was nothing
    # to do" from "this was done before".
    already_settled: bool = False
    # How the survivor was matched, and how close it was. Carried onto the cluster row
    # so a threshold change can be evaluated against past decisions rather than
    # guessed at.
    collapse_similarity: float = 1.0
    collapse_matched_by: str = MATCHED_EXACT


class ConsolidationService:
    """Decides ADD, UPDATE, or NO-OP for a staged claim, and applies it."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        claims: ClaimService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        # This module decides; the claim service writes. Constructed here when not
        # supplied so a caller cannot forget, but never bypassed: retiring a claim and
        # attributing evidence to one both change what the store believes, and both
        # belong behind the one module that owns those tables.
        self._claims = claims or ClaimService(session_factory, clock=clock)

    async def consolidate(self, claim_id: uuid.UUID) -> Outcome:
        """Reconcile one claim against its neighbourhood."""
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            return await self.consolidate_in(session, claim_id=claim_id, now=now)

    async def consolidate_in(self, session: AsyncSession, *, claim_id: uuid.UUID, now: datetime.datetime) -> Outcome:
        """The same decision, inside a caller's transaction.

        Offered because a caller staging a claim wants the claim and its
        consolidation to commit together: a claim briefly live and unconsolidated is
        a claim a concurrent reader can see twice.
        """
        candidate = await self._load(session, claim_id)
        if candidate is None:
            return Outcome(
                claim_id=claim_id,
                decision=DECISION_NOOP,
                reason="claim is not live, so there is nothing to reconcile",
                already_settled=True,
            )

        neighbours = await self._neighbourhood(session, candidate)

        if self._already_settled(candidate, neighbours):
            # Nothing has arrived since this claim was last reconciled, so the
            # decision would be the one already recorded. Returned without writing
            # anything -- an audit row here would say the sweep ran, not that
            # anything was decided, and the log has to be a record of decisions.
            return Outcome(
                claim_id=claim_id,
                decision=DECISION_NOOP,
                reason="already reconciled, and nothing has arrived in the neighbourhood since",
                already_settled=True,
            )

        outcome = self._decide(candidate, neighbours)
        await self._apply(session, candidate, outcome, now=now)
        await self._mark_consolidated(session, claim_id=claim_id, now=now)

        _DECIDED.labels(decision=outcome.decision, predicate=candidate.predicate).inc()
        return outcome

    def _already_settled(self, candidate: _Candidate, neighbours: list[Neighbour]) -> bool:
        """Has this claim been reconciled against exactly this neighbourhood?

        A timestamp comparison rather than a flag, because a claim arriving later
        genuinely changes the answer. Treating consolidation as one-shot would leave
        a conflict undetected whenever the conflicting claim showed up second.
        """
        if candidate.consolidated_at is None:
            return False
        newest = max((n.created_at for n in neighbours), default=None)
        return newest is None or newest <= candidate.consolidated_at

    async def _mark_consolidated(self, session: AsyncSession, *, claim_id: uuid.UUID, now: datetime.datetime) -> None:
        await self._claims.mark_consolidated(session, claim_id=claim_id, now=now)

    # -- the decision ----------------------------------------------------------

    def _decide(self, candidate: _Candidate, neighbours: list[Neighbour]) -> Outcome:
        """ADD, UPDATE, NO-OP, contested, or routed -- from two rows and no model.

        Typed values make equivalence decidable for every predicate the ontology
        ships, so no provider is consulted. That is not an optimization: a decision
        that needed a model could not be re-derived, and a supersession nobody can
        re-derive is one nobody can review.
        """
        if not neighbours:
            return Outcome(
                claim_id=candidate.claim_id,
                decision=DECISION_ADD,
                reason="no comparable claim exists for this subject and predicate",
            )

        equivalent: list[tuple[Neighbour, float, str]] = []
        conflicting: list[Neighbour] = []

        for other in neighbours:
            verdict = values_compatible(
                candidate.value_type,
                candidate.value,
                other.value,
                left_entity_id=str(candidate.value_entity_id) if candidate.value_entity_id else None,
                right_entity_id=str(other.value_entity_id) if other.value_entity_id else None,
            )
            if verdict == COMPATIBLE:
                equivalent.append((other, 1.0, MATCHED_EXACT))
                continue

            # Near-duplicate before conflict. The same assertion phrased differently
            # across many sessions would otherwise become that many *contested*
            # claims -- not merely noise, but claims no reviewer can resolve, because
            # they all mean the same thing and none of them is wrong.
            near, similarity = is_near_duplicate(candidate.value_type, candidate.value, other.value)
            if near:
                equivalent.append((other, similarity, MATCHED_SEMANTIC))
                continue

            if verdict == INCOMPATIBLE and self._competes(candidate, other):
                conflicting.append(other)
            # An undecidable comparison is neither. A value neither side can read is
            # a validation gap, and treating it as a conflict would manufacture a
            # contested claim out of a bug.

        if equivalent:
            # The same assertion already present. Collapsed rather than added, so a
            # subject does not accumulate a row per session that mentioned it.
            # The exact match is preferred as the survivor when there is one: it is
            # the claim the newcomer literally agrees with, so keeping it loses
            # nothing, while keeping a merely-near match would leave the store's
            # canonical phrasing decided by arrival order.
            equivalent.sort(key=lambda item: (item[2] != MATCHED_EXACT, -item[1]))
            best, similarity, matched_by = equivalent[0]
            return Outcome(
                claim_id=candidate.claim_id,
                decision=DECISION_NOOP,
                reason=(
                    f"an equivalent claim is already present ({len(equivalent)} match(es), "
                    f"closest by {matched_by} at {similarity:.2f}); collapsed into it "
                    "rather than stored again"
                ),
                collapsed=(best.claim_id,) + tuple(n.claim_id for n, _, _ in equivalent[1:]),
                collapse_similarity=similarity,
                collapse_matched_by=matched_by,
            )

        if not conflicting:
            return Outcome(
                claim_id=candidate.claim_id,
                decision=DECISION_ADD,
                reason="nothing in the neighbourhood is equivalent or in conflict",
            )

        # A conflict about somebody else's capability is never resolved here. The
        # owner decides, and until they do both claims stand.
        if candidate.owning_tenant_id is not None and (candidate.author_tenant_id != candidate.owning_tenant_id):
            return Outcome(
                claim_id=candidate.claim_id,
                decision=DECISION_PROPOSAL,
                reason=(
                    "this claim is about another tenant's capability and conflicts with "
                    "theirs, so it is routed to the owner rather than superseding anything"
                ),
                contested_with=tuple(n.claim_id for n in conflicting),
            )

        # Authority first. Recency only among equals -- and a human-confirmed claim
        # needs equal-or-higher authority to displace, which the rank comparison
        # already gives, since no machine tier equals a human one.
        supersedable = [
            other
            for other in conflicting
            if candidate.rank < other.rank or (candidate.rank == other.rank and candidate.created_at > other.created_at)
        ]

        if not supersedable:
            strongest = min(conflicting, key=lambda n: n.rank)
            return Outcome(
                claim_id=candidate.claim_id,
                decision=DECISION_CONTESTED,
                reason=(
                    f"a claim of {strongest.authority} authority holds the field and this one "
                    f"is {candidate.authority}; recorded as contested rather than superseding, "
                    "because a weaker source never displaces a stronger one however recent it is"
                ),
                contested_with=tuple(n.claim_id for n in conflicting),
            )

        outranked = tuple(n.claim_id for n in supersedable)
        still_standing = tuple(n.claim_id for n in conflicting if n.claim_id not in set(outranked))
        return Outcome(
            claim_id=candidate.claim_id,
            decision=DECISION_UPDATE,
            reason=(
                f"supersedes {len(outranked)} claim(s) of lower or equal authority; "
                f"{len(still_standing)} stronger claim(s) remain and are contested"
            ),
            superseded=outranked,
            contested_with=still_standing,
        )

    def _competes(self, candidate: _Candidate, other: Neighbour) -> bool:
        """Do these two claims answer the same question at the same time?

        Only single-valued predicates compete. A capability depends on many things,
        so two dependency claims are two facts -- treating them as rivals would make
        every second dependency lose a conflict it was never in.
        """
        if candidate.value_cardinality != CARDINALITY_SINGLE:
            return False
        if other.value_cardinality != CARDINALITY_SINGLE:
            return False
        return intervals_overlap(candidate.valid_from, candidate.valid_to, other.valid_from, other.valid_to)

    # -- applying it -----------------------------------------------------------

    async def _apply(
        self,
        session: AsyncSession,
        candidate: _Candidate,
        outcome: Outcome,
        *,
        now: datetime.datetime,
    ) -> None:
        """Write what the decision implies, and audit it either way."""
        if outcome.decision == DECISION_UPDATE:
            for loser in outcome.superseded:
                await self._close(
                    session,
                    claim_id=loser,
                    survivor=candidate.claim_id,
                    reason=REASON_LOST_CONFLICT,
                    now=now,
                )

        if outcome.decision == DECISION_NOOP and outcome.collapsed:
            # The newcomer is the one closed. The claim already present is the one
            # that has been scored, corroborated, and possibly reviewed; keeping the
            # newer row instead would discard that history to gain nothing.
            survivor = outcome.collapsed[0]
            await self._close(
                session,
                claim_id=candidate.claim_id,
                survivor=survivor,
                reason=REASON_CLUSTER_COLLAPSED,
                now=now,
            )
            await self._merge_provenance(session, survivor=survivor, collapsed=candidate.claim_id)
            await self._record_cluster(
                session,
                survivor=survivor,
                collapsed=candidate.claim_id,
                similarity=outcome.collapse_similarity,
                matched_by=outcome.collapse_matched_by,
                now=now,
            )

        await self._audit(session, candidate, outcome, now=now)

    async def _close(
        self,
        session: AsyncSession,
        *,
        claim_id: uuid.UUID,
        survivor: uuid.UUID,
        reason: str,
        now: datetime.datetime,
    ) -> None:
        await self._claims.close_superseded(session, claim_id=claim_id, survivor=survivor, reason=reason, now=now)

        # Every open disagreement the closed claim was part of is settled by its
        # closure. Leaving them open would keep the survivor permanently flagged as
        # conflicting with something that no longer stands -- and a contested claim
        # cannot be promoted, so the conflict would outlive its own resolution and
        # block the claim that won it.
        await resolve_contests_for(session, claim_id=claim_id, now=now)

    async def _merge_provenance(self, session: AsyncSession, *, survivor: uuid.UUID, collapsed: uuid.UUID) -> None:
        await self._claims.merge_provenance(session, survivor=survivor, collapsed=collapsed)

    async def _record_cluster(
        self,
        session: AsyncSession,
        *,
        survivor: uuid.UUID,
        collapsed: uuid.UUID,
        similarity: float,
        matched_by: str,
        now: datetime.datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO lmm_claim_cluster "
                "  (survivor_claim_id, collapsed_claim_id, similarity, matched_by, collapsed_at) "
                "VALUES (:survivor, :collapsed, CAST(:sim AS NUMERIC), :matched, "
                "        CAST(:now AS TIMESTAMPTZ)) "
                "ON CONFLICT (survivor_claim_id, collapsed_claim_id) DO NOTHING"
            ),
            {
                "survivor": survivor,
                "collapsed": collapsed,
                "sim": similarity,
                "matched": matched_by,
                "now": now,
            },
        )

    async def _audit(
        self,
        session: AsyncSession,
        candidate: _Candidate,
        outcome: Outcome,
        *,
        now: datetime.datetime,
    ) -> None:
        """One row per decision, including the decision to do nothing.

        Idempotence comes from the decision, not from the insert: a repeated sweep
        finds a closed claim excluded from the neighbourhood, so it reaches the same
        no-op conclusion and the audit row it writes says exactly that. The audit
        table is append-only by design -- deduplicating rows in it would make the
        log a summary rather than a record.
        """
        action = _AUDIT_ACTION_BY_DECISION[outcome.decision]
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES (:audit_id, :tid, :aid, :action, 'lmm_claim', :cid, NULL, "
                "        CAST(:after AS JSONB), CAST(:now AS TIMESTAMPTZ), NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tid": candidate.author_tenant_id,
                "aid": candidate.author_actor_id,
                "action": action,
                "cid": candidate.claim_id,
                "after": _audit_metadata(candidate, outcome),
                "now": now,
            },
        )

    # -- reading ---------------------------------------------------------------

    async def _load(self, session: AsyncSession, claim_id: uuid.UUID) -> _Candidate | None:
        row = (
            await session.execute(
                text(
                    "SELECT claim_id, subject_entity_id, predicate, value_jsonb, value_type, "
                    "       value_cardinality, value_entity_id, source_authority, "
                    "       owning_tenant_id, author_tenant_id, author_actor_id, created_at, "
                    "       asserted_valid_from, asserted_valid_to, consolidated_at "
                    "FROM lmm_claims "
                    "WHERE claim_id = :cid AND status = 'staged' "
                    "  AND subject_entity_id IS NOT NULL "
                    "FOR UPDATE"
                ),
                {"cid": claim_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return _Candidate(
            claim_id=row.claim_id,
            subject_entity_id=row.subject_entity_id,
            predicate=row.predicate,
            value=row.value_jsonb,
            value_type=row.value_type,
            value_cardinality=row.value_cardinality,
            value_entity_id=row.value_entity_id,
            authority=row.source_authority,
            owning_tenant_id=row.owning_tenant_id,
            author_tenant_id=row.author_tenant_id,
            author_actor_id=row.author_actor_id,
            created_at=row.created_at,
            valid_from=row.asserted_valid_from,
            valid_to=row.asserted_valid_to,
            consolidated_at=row.consolidated_at,
        )

    async def _neighbourhood(self, session: AsyncSession, candidate: _Candidate) -> list[Neighbour]:
        """Live claims sharing this subject and predicate.

        The exact arm is an indexed lookup on subject and predicate, and it is the
        only arm correctness depends on -- because values are typed, equivalence is
        decidable without embeddings. Semantic proximity can widen this later; it can
        never be what decides whether two claims are the same.

        Superseded claims are excluded, which is what makes a repeated sweep find
        nothing to do rather than reconsidering closed history. A claim naming a
        successor is excluded too, even if its status has not caught up -- a claim
        that has been replaced must never compete with its own replacement.
        """
        rows = (
            await session.execute(
                text(
                    "SELECT claim_id, value_jsonb, value_type, value_entity_id, "
                    "       value_cardinality, source_authority, owning_tenant_id, "
                    "       author_tenant_id, created_at, asserted_valid_from, "
                    "       asserted_valid_to, confidence_hold_until "
                    "FROM lmm_claims "
                    "WHERE subject_entity_id = :eid AND predicate = :pred "
                    "  AND status = 'staged' AND superseded_by IS NULL "
                    "  AND claim_id <> :cid "
                    "ORDER BY created_at DESC "
                    "LIMIT :lim"
                ),
                {
                    "eid": candidate.subject_entity_id,
                    "pred": candidate.predicate,
                    "cid": candidate.claim_id,
                    "lim": MAX_NEIGHBOURHOOD,
                },
            )
        ).all()

        return [
            Neighbour(
                claim_id=r.claim_id,
                value=r.value_jsonb,
                value_type=r.value_type,
                value_entity_id=r.value_entity_id,
                value_cardinality=r.value_cardinality,
                authority=r.source_authority,
                owning_tenant_id=r.owning_tenant_id,
                author_tenant_id=r.author_tenant_id,
                created_at=r.created_at,
                valid_from=r.asserted_valid_from,
                valid_to=r.asserted_valid_to,
                is_confirmed=r.confidence_hold_until is not None,
            )
            for r in rows
        ]


def _audit_metadata(candidate: _Candidate, outcome: Outcome) -> str:
    return json.dumps(
        {
            "decision": outcome.decision,
            "reason": outcome.reason,
            "predicate": candidate.predicate,
            "authority": candidate.authority,
            "superseded": [str(c) for c in outcome.superseded],
            "collapsed": [str(c) for c in outcome.collapsed],
            "contested_with": [str(c) for c in outcome.contested_with],
        },
        sort_keys=True,
    )


@dataclasses.dataclass(frozen=True)
class _Candidate:
    claim_id: uuid.UUID
    subject_entity_id: uuid.UUID
    predicate: str
    value: object
    value_type: str
    value_cardinality: str
    value_entity_id: uuid.UUID | None
    authority: str
    owning_tenant_id: uuid.UUID | None
    author_tenant_id: uuid.UUID
    author_actor_id: uuid.UUID | None
    created_at: datetime.datetime
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    consolidated_at: datetime.datetime | None = None

    @property
    def rank(self) -> int:
        return SOURCE_AUTHORITY_RANK.get(self.authority, len(SOURCE_AUTHORITY_RANK))


__all__ = [
    "DECISIONS",
    "DECISION_ADD",
    "DECISION_CONTESTED",
    "DECISION_NOOP",
    "DECISION_PROPOSAL",
    "DECISION_UPDATE",
    "MATCHED_EXACT",
    "MATCHED_SEMANTIC",
    "MAX_NEIGHBOURHOOD",
    "REASON_CLUSTER_COLLAPSED",
    "REASON_CURATOR_REPLACED",
    "REASON_HUMAN_CONFIRMED",
    "REASON_LOST_CONFLICT",
    "ConsolidationService",
    "Neighbour",
    "Outcome",
]
