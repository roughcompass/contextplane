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
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.retention import derivatives, policies
from contextplane.types import Clock, TenantContext, TraversalResult

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


#: How many distinct subject entities a preview will seed the traversal from.
#: Each seed is one closure lookup, so an unbounded match set would turn a
#: preview into a scan. The result reports both figures, because a truncated
#: answer that does not say it was truncated reads as a complete one.
_MAX_PREVIEW_SEEDS: Final = 50

#: Hops the preview follows. The traversal caps at 5 itself; asking for the cap
#: rather than a smaller number is deliberate -- a claim four hops downstream
#: rests on the withheld content exactly as much as one hop does.
_PREVIEW_DEPTH: Final = 5


class BlastRadius(Protocol):
    """The traversal a preview borrows, narrowed to what it needs.

    A `Protocol` rather than an import of the closure service, so this module
    does not acquire a dependency on the retrieval stack to ask one question of
    it -- and so a test can answer that question without a graph.
    """

    async def get_blast_radius(
        self, ctx: TenantContext, entity_id: uuid.UUID, direction: str = ..., depth: int = ...
    ) -> TraversalResult: ...


@dataclasses.dataclass(frozen=True)
class QuarantinePreview:
    """What a predicate reaches, and what depends on what it reaches.

    Two different questions, kept apart. `matched` is what the quarantine would
    withhold -- it is decided by the predicate and is exact. `downstream` is
    what those claims' subjects are depended on by, which is a statement about
    the catalog graph and is advisory: nothing there is withheld by applying
    this quarantine.
    """

    matched: tuple[uuid.UUID, ...]
    subjects: tuple[uuid.UUID, ...]
    downstream: tuple[uuid.UUID, ...]
    #: Seeds actually traversed, and seeds there were. Unequal means the
    #: downstream set is a floor rather than the answer.
    seeds_traversed: int
    seeds_total: int

    @property
    def truncated(self) -> bool:
        """Whether `downstream` is incomplete because the seed cap was hit."""
        return self.seeds_traversed < self.seeds_total


class ReceiptWithholding(Protocol):
    """Withholding receipts that quoted a quarantined claim, and releasing them.

    A `Protocol` because `contextplane.service` may not import
    `contextplane.context`, and both the receipt tables and the
    `observed_claims` block name live there. Inlining the block name here would
    be a second source of truth for a vocabulary value -- the failure this
    codebase refuses in `PROHIBITED_CLASSES` and in the governed-magnitude
    registry.

    Both methods take the caller's `session`: the writes belong in the
    quarantine's own transaction, so claims and the receipts that quoted them
    become withheld at one instant and no reader sees one without the other.
    """

    async def withhold_for_claims(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        claims: tuple[uuid.UUID, ...],
        quarantine_id: uuid.UUID,
        now: datetime.datetime,
    ) -> None: ...

    async def release(self, session: AsyncSession, *, quarantine_id: uuid.UUID) -> None: ...


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

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        blast_radius: BlastRadius | None = None,
        receipts: ReceiptWithholding | None = None,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._blast_radius = blast_radius
        self._receipts = receipts

    # -- preview -----------------------------------------------------------

    async def preview(self, ctx: TenantContext, *, selector: str, value: str) -> QuarantinePreview:
        """What applying this predicate would reach, right now.

        **A point-in-time answer, and callers must present it as one.** The
        graph moves between a preview and an apply, so a preview acted on ten
        minutes later reached a different set. This returns ids rather than
        counts so a caller that wants to show the difference can.

        Two sets, and they mean different things. `matched` is what would be
        withheld: the predicate decides it and it is exact. `downstream` is what
        depends on those claims' subjects -- advisory, because applying this
        quarantine withholds none of it.
        """
        self._require_operator(ctx)
        async with self._factory() as session:
            matched = await self._match(session, ctx, selector=selector, value=value)
            subjects = await self._subjects_of(session, ctx, matched)
        downstream, traversed = await self._downstream_of(ctx, subjects)
        return QuarantinePreview(
            matched=matched,
            subjects=subjects,
            downstream=downstream,
            seeds_traversed=traversed,
            seeds_total=len(subjects),
        )

    @staticmethod
    async def _subjects_of(
        session: AsyncSession, ctx: TenantContext, claims: tuple[uuid.UUID, ...]
    ) -> tuple[uuid.UUID, ...]:
        """The distinct entities the matched claims are about.

        Tenant-scoped in the query for the reason `_match` is: a subject read
        that could reach another tenant's rows leaks the shape of their catalog
        through a preview nobody thought of as a read.
        """
        if not claims:
            return ()
        rows = await session.execute(
            text(
                "SELECT DISTINCT subject_entity_id FROM memory_claims "
                " WHERE owning_tenant_id = :tid AND claim_id = ANY(:ids) "
                "   AND subject_entity_id IS NOT NULL"
            ),
            {"ids": list(claims), "tid": ctx.tenant_id},
        )
        return tuple(sorted(rows.scalars().all()))

    async def _downstream_of(
        self, ctx: TenantContext, subjects: tuple[uuid.UUID, ...]
    ) -> tuple[tuple[uuid.UUID, ...], int]:
        """Entities depending on any subject, through the traversal that exists.

        **The existing traversal, called once per seed -- not a second one, and
        not a widened one.** `get_blast_radius` is single-root by construction:
        `closure_cache` is keyed by root, and the CTE recurses from one. Teaching
        it about sets would rewrite a cached path that REST and MCP both serve,
        to answer a question an operator asks occasionally. Calling it per seed
        costs one cache lookup each and agrees with the promotion path about what
        "downstream" means by being the same code.

        **`promotion_eligibility.blast_radius_for` is deliberately not this.** It
        counts direct dependants over three edge types in its own statement, and
        says why: the count is a review threshold an owner can verify by looking,
        not a correctness property. A quarantine asks the other question -- what
        content rests on this -- and a claim two hops away is as affected as one
        hop away. The two disagree on purpose; collapsing them would break
        whichever one lost.

        Returns the reached ids and how many seeds were traversed, so the caller
        can tell a complete answer from a capped one.
        """
        if not subjects or self._blast_radius is None:
            return (), 0
        seeds = subjects[:_MAX_PREVIEW_SEEDS]
        reached: set[uuid.UUID] = set()
        for seed in seeds:
            result = await self._blast_radius.get_blast_radius(ctx, seed, "reverse", _PREVIEW_DEPTH)
            reached.update(node.entity_id for node in result.nodes)
        # A subject is not downstream of itself, and reporting it as such would
        # double-count the match set inside the advisory set beside it.
        reached.difference_update(subjects)
        return tuple(sorted(reached)), len(seeds)

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
            await self._withhold_receipts(session, ctx, matched, quarantine_id=quarantine_id, now=now)
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
            await self._release_receipts(session, quarantine_id=quarantine_id)
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

    async def _withhold_receipts(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        claims: tuple[uuid.UUID, ...],
        *,
        quarantine_id: uuid.UUID,
        now: datetime.datetime,
    ) -> None:
        """Withhold every receipt that served one of these claims. E4-T4.

        **In the same transaction as the claim write, which is stronger than
        what the task asked for.** The entry prescribes marking the downstream
        set first and reconciling afterwards, to close the window in which a
        row-at-a-time sweep still serves the receipts it has not reached yet.
        There is no such window here: `apply` is one transaction, so the claims
        and the receipts that quoted them become withheld at the same instant
        and no reader observes one without the other. "Mark first" is a remedy
        for an incremental sweep, and ordering statements inside a transaction
        nothing outside can see part-way would buy nothing while adding a
        marked-but-unreconciled state to reason about. The session is handed
        across so the collaborator writes inside that transaction rather than
        opening one of its own.

        **The residual race, stated rather than papered over.** A resolution
        already in flight can commit a receipt citing a claim this transaction
        withholds, a moment after it commits. Serving refuses the claim itself
        from that instant, so no *new* resolution can cite it -- but a receipt
        recording a true past serving is not reached by this write. That is a
        reconciliation sweep's job, and this task does not build one.

        A deployment with no withholding collaborator wired quarantines claims
        and leaves receipts alone, which is the behaviour before this task
        rather than a silent half-application.
        """
        if not claims or self._receipts is None:
            return
        await self._receipts.withhold_for_claims(
            session, tenant_id=ctx.tenant_id, claims=claims, quarantine_id=quarantine_id, now=now
        )

    async def _release_receipts(self, session: AsyncSession, *, quarantine_id: uuid.UUID) -> None:
        """Serve again every receipt this quarantine withheld, and only those."""
        if self._receipts is None:
            return
        await self._receipts.release(session, quarantine_id=quarantine_id)

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
