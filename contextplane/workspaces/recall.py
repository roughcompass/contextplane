"""Workspace recall for the context envelope: lexical and by external reference.

Two ways to find workspace material, both bounded and both inside the task
audience. Lexical takes a term; reference takes an external work item and finds
the checkpoints that cited it.

**Authorization is the candidate set, not a filter over it.** Both reads compose
the task-audience predicate in SQL, so a task the caller does not participate in
never enters the query. That is a stronger statement than "its rows are removed
before returning", and the difference is observable: a count taken before
filtering, a page that comes back short, a search whose latency tracks how many
matches the caller cannot see.

**Which is why non-participation produces no exclusion.** The assembler records
an `Exclusion` so a reader learns "there was something you may not see", which
is the right answer for content withheld on classification. It is the wrong
answer for participation: reporting that a task exists but is not yours is
exactly the discovery the audience boundary is for. So exclusions here are
reserved for material inside the caller's own tasks that recall declined to
return; a task outside the audience is not withheld, it is not a candidate.

**No vector arm.** Lexical and reference only. The repaired embedding
dead-letter and chunking defects make the canonical and claim semantic arms
usable again; they license nothing for workspace content, which has no vector
table and gets none here.

**Reads fail closed on overdue blocking derivative propagation.** An erasure
schedules propagation into every artefact built from what the erased person
wrote. Until a *blocking* item has run, this arm can still be holding their
words, so it refuses to serve rather than serving them and reporting nothing.
Refusal is a raise: the assembler turns an arm that could not answer into a
failed block carrying no items, which is the only shape that cannot leak part of
an answer.

**The guard runs once per read, at the arm.** Not per item: the answer is a
tenant-scoped count that cannot differ between two items read at the same
moment, so asking per item multiplies a bounded read by its page size and buys
nothing. Not at envelope assembly either: that layer decides what a block
*means* and holds no session, it has no way to know which blocks can expose
erased content, and putting the check there would run it for resolutions with no
workspace block at all. The arm that can serve the content is the thing that has
to refuse it.

**Scoped to this tenant, and to blocking items only.** Refusing because another
tenant has overdue propagation would be availability lost for no privacy gained,
and a non-blocking derivative is by definition one whose staleness exposes
nothing -- that is what the flag means. Both narrowings make the guard weaker on
purpose; a guard that fires on facts about somebody else's data gets switched
off, and a guard that is switched off protects nobody.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.context.assembler import ArmOutcome, Exclusion, contextual_item, ordered_items
from contextplane.context.models import ContextExternalReference, ContextReferenceBinding
from contextplane.context.schemas.envelope import BLOCK_WORKSPACE, ContextItemV1
from contextplane.context.schemas.trust import Classification, TrustMetadataV1
from contextplane.sensitivity import MOST_RESTRICTIVE, TIERS, is_tier, rank
from contextplane.workers.derivative_propagation import pending_overdue
from contextplane.workspaces.models import IntentCheckpoint

# The audience sub-select every read here composes. Imported rather than
# restated: a second copy of "this actor participates right now" is how one read
# path keeps honouring a revoked grant after the others have stopped.
from contextplane.workspaces.queries_audience import _authorized_task_ids

if TYPE_CHECKING:
    from collections.abc import Sequence

    from contextplane.context.assembler import ContextArm

#: The most workspace items one arm will return. A ceiling rather than a
#: default: the assembler applies its own cap on top, and an arm that could be
#: asked for an unbounded page would let one caller's request decide how much
#: work every other request waits behind.
MAX_RESULTS = 50

#: What an arm returns when the caller names no bound of its own.
DEFAULT_LIMIT = 20

#: Least to most restrictive. Used to pick a checkpoint's classification from
#: the references it cites, so a checkpoint quoting restricted evidence is
#: itself treated as restricted.

#: Where a checkpoint's classification starts when it cites no references.
#:
#: A floor, not a measurement: `task_checkpoint_v1` carries no classification of
#: its own, so there is nothing to read. `internal` is the conservative choice
#: for agent-authored task content -- it is not `public`, and claiming
#: `confidential` for material nobody classified would be its own fiction.
CLASSIFICATION_FLOOR = "internal"

#: Bindings that point at a checkpoint. The other member of that closed set
#: (`context_item`) is not workspace material and is not recalled here.
_CHECKPOINT_SUBJECT = "intent_checkpoint"


class OverdueDerivativeRefusal(Exception):
    """This tenant has blocking derivative propagation past due, so nothing is served.

    Raised rather than returned as an empty or degraded outcome. Empty would say
    the caller's tasks hold nothing, which is false and is the answer a reader
    acts on by writing the checkpoint again; degraded would still carry items,
    and the items are exactly what may not be served. The assembler maps a raised
    arm to a failed block with no items, which is the honest shape: the block
    could not be answered safely, and the receipt says so.
    """


_SOURCE = "intent_checkpoint"


def classification_for(evidence: Sequence[Any]) -> Classification:
    """The most restrictive classification among the references a checkpoint cites.

    Most-restrictive rather than first or last: a checkpoint that cites one
    public and one confidential reference has quoted confidential material, and
    labelling the whole item by whichever reference happened to be first would
    make the label depend on write order.
    """
    worst = rank(CLASSIFICATION_FLOOR)
    for reference in evidence:
        if not isinstance(reference, dict):
            continue
        label = reference.get("classification")
        if not is_tier(label):
            # An unreadable or unknown label is treated as the most restrictive
            # thing it could be. Guessing downward would publish it.
            worst = rank(MOST_RESTRICTIVE)
            continue
        worst = max(worst, rank(str(label)))
    return TIERS[worst]


def _trust_for(row: IntentCheckpoint) -> TrustMetadataV1:
    """Trust metadata for one checkpoint.

    `asserted`, not `observed`: an agent wrote this record about its own work, so
    the system has the agent's word for it and no independent observation.
    `immutable` because a checkpoint cannot be rewritten -- the head projection
    beside it is mutable, which is exactly why the head is not recalled here.
    """
    return TrustMetadataV1(
        trust="asserted",
        source=_SOURCE,
        assertion_kind="annotation",
        # The author stands behind the content; the task is the boundary it was
        # written inside. Attribution names the author separately so a reader can
        # tell who wrote it from who vouches for it.
        authority=f"task:{row.intent_id}",
        freshness=row.recorded_at,
        mutability="immutable",
        attribution=row.author,
        classification=classification_for(row.evidence),
    )


def _payload(row: IntentCheckpoint) -> dict[str, object]:
    """The item body. Structured fields stay structured.

    `goal`, `next_action` and the four lists are carried separately rather than
    flattened into prose: resume treats an open question and a completed check
    differently, and a reader that has to parse them back out of a paragraph
    will parse them differently than the writer meant.
    """
    return {
        "checkpoint_id": str(row.checkpoint_id),
        "intent_id": str(row.intent_id),
        "sequence": row.sequence,
        "goal": row.goal,
        "decisions": list(row.decisions),
        "assumptions": list(row.assumptions),
        "completed_checks": list(row.completed_checks),
        "open_questions": list(row.open_questions),
        "next_action": row.next_action,
        "author": row.author,
        "recorded_at": row.recorded_at.isoformat(),
        "digest": row.digest,
    }


def _item(row: IntentCheckpoint) -> ContextItemV1:
    return contextual_item(
        block=BLOCK_WORKSPACE,
        source=_SOURCE,
        item_key=str(row.checkpoint_id),
        payload=_payload(row),
        trust=_trust_for(row),
    )


def _bounded(limit: int | None) -> int:
    """The effective page size: the caller's, clamped, never unbounded."""
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        raise ValueError("a workspace recall limit must be at least 1")
    return min(limit, MAX_RESULTS)


@dataclasses.dataclass(frozen=True)
class _Read:
    """One arm's rows plus whether the arm's own bound cut them short."""

    rows: tuple[IntentCheckpoint, ...]
    truncated: bool


class WorkspaceRecall:
    """Bounded, authorized reads of workspace material for one deployment."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # -- arms ------------------------------------------------------------

    def lexical_arm(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_id: str,
        term: str,
        moment: datetime.datetime,
        limit: int | None = None,
    ) -> ContextArm:
        """An arm the assembler can call, closing over this request's parameters.

        Returned as a zero-argument callable because the assembler runs arms
        concurrently under its own timeout and must not need to know what any of
        them takes.
        """
        size = _bounded(limit)

        async def arm() -> ArmOutcome:
            return await self._as_outcome(
                await self._lexical(tenant_id=tenant_id, actor_id=actor_id, term=term, moment=moment, size=size),
                moment=moment,
            )

        return arm

    def reference_arm(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_id: str,
        source_system: str,
        source_namespace: str,
        kind: str,
        external_id: str,
        moment: datetime.datetime,
        limit: int | None = None,
    ) -> ContextArm:
        """Recall by the external work item a checkpoint cited.

        The reference is named by its identity fields rather than by a Registry
        id: a caller resuming work has a commit or a ticket, not a row id, and
        requiring the id would mean a lookup this arm exists to perform.
        """
        size = _bounded(limit)

        async def arm() -> ArmOutcome:
            return await self._as_outcome(
                await self._by_reference(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    source_system=source_system,
                    source_namespace=source_namespace,
                    kind=kind,
                    external_id=external_id,
                    moment=moment,
                    size=size,
                ),
                moment=moment,
            )

        return arm

    # -- reads -----------------------------------------------------------

    async def _lexical(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_id: str,
        term: str,
        moment: datetime.datetime,
        size: int,
    ) -> _Read:
        needle = term.strip()
        if not needle:
            # An empty term is not a wildcard. Returning the caller's whole
            # corpus for a blank input is a different feature, and not one a
            # missing query parameter should invoke.
            return _Read(rows=(), truncated=False)
        async with self._session_factory() as session:
            await self._refuse_if_overdue(session, tenant_id=tenant_id, moment=moment)
            rows = (
                await session.execute(
                    select(IntentCheckpoint)
                    .where(
                        IntentCheckpoint.tenant_id == tenant_id,
                        IntentCheckpoint.intent_id.in_(
                            _authorized_task_ids(tenant_id=tenant_id, actor_id=actor_id, moment=moment)
                        ),
                        IntentCheckpoint.goal.ilike(f"%{needle}%"),
                    )
                    # Tiebroken to a total order. `recorded_at` alone leaves two
                    # checkpoints recorded in the same instant free to arrive in
                    # either order, and the block now presents items in the order
                    # its read produced (ADR 0028) — so an untiebroken read makes
                    # two identical requests differ, and a `LIMIT` on one keeps a
                    # different row each time. `ordered_items`'s digest sort used to
                    # hide this by discarding the order entirely.
                    .order_by(IntentCheckpoint.recorded_at.desc(), IntentCheckpoint.checkpoint_id)
                    # One more than asked for, so hitting the bound is
                    # distinguishable from happening to land on it exactly.
                    .limit(size + 1)
                )
            ).scalars()
        return self._cut(tuple(rows), size)

    async def _by_reference(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_id: str,
        source_system: str,
        source_namespace: str,
        kind: str,
        external_id: str,
        moment: datetime.datetime,
        size: int,
    ) -> _Read:
        bound_checkpoints = (
            select(ContextReferenceBinding.subject_id)
            .join(
                ContextExternalReference,
                ContextExternalReference.reference_id == ContextReferenceBinding.reference_id,
            )
            .where(
                ContextReferenceBinding.tenant_id == tenant_id,
                ContextReferenceBinding.subject_type == _CHECKPOINT_SUBJECT,
                ContextExternalReference.tenant_id == tenant_id,
                ContextExternalReference.source_system == source_system,
                ContextExternalReference.source_namespace == source_namespace,
                ContextExternalReference.kind == kind,
                ContextExternalReference.external_id == external_id,
            )
        )
        async with self._session_factory() as session:
            await self._refuse_if_overdue(session, tenant_id=tenant_id, moment=moment)
            rows = (
                await session.execute(
                    select(IntentCheckpoint)
                    .where(
                        IntentCheckpoint.tenant_id == tenant_id,
                        # Both predicates, and the audience one is not optional
                        # because the binding was found: a reference cited by a
                        # task the caller is not in is still not theirs to read.
                        IntentCheckpoint.intent_id.in_(
                            _authorized_task_ids(tenant_id=tenant_id, actor_id=actor_id, moment=moment)
                        ),
                        IntentCheckpoint.checkpoint_id.in_(bound_checkpoints),
                    )
                    # Tiebroken to a total order. `recorded_at` alone leaves two
                    # checkpoints recorded in the same instant free to arrive in
                    # either order, and the block now presents items in the order
                    # its read produced (ADR 0028) — so an untiebroken read makes
                    # two identical requests differ, and a `LIMIT` on one keeps a
                    # different row each time. `ordered_items`'s digest sort used to
                    # hide this by discarding the order entirely.
                    .order_by(IntentCheckpoint.recorded_at.desc(), IntentCheckpoint.checkpoint_id)
                    .limit(size + 1)
                )
            ).scalars()
        return self._cut(tuple(rows), size)

    @staticmethod
    async def _refuse_if_overdue(session: AsyncSession, *, tenant_id: uuid.UUID, moment: datetime.datetime) -> None:
        """Ask, before reading, whether this tenant's erasures have actually landed.

        In the same session as the read that follows, so the count and the rows
        come from one transaction: a check on its own connection could pass while
        the read that trusted it sees a state the check never saw.
        """
        overdue = await pending_overdue(session, now=moment, blocking_only=True, tenant_id=tenant_id)
        if overdue:
            raise OverdueDerivativeRefusal(
                f"{overdue} blocking derivative propagation item(s) are past due for this tenant; "
                "workspace recall does not serve content whose erasure has not reached it"
            )

    @staticmethod
    def _cut(rows: tuple[IntentCheckpoint, ...], size: int) -> _Read:
        if len(rows) > size:
            return _Read(rows=rows[:size], truncated=True)
        return _Read(rows=rows, truncated=False)

    async def _as_outcome(self, read: _Read, *, moment: datetime.datetime) -> ArmOutcome:
        """Turn rows into the facts the assembler maps to a block state.

        No state is decided here. The arm reports what it found, what it
        withheld and whether it stopped early; success, empty, degraded and
        failed are one decision made in one place, and that place is not here.
        """
        kept: list[ContextItemV1] = []
        withheld: list[Exclusion] = []
        for row in read.rows:
            trust = _trust_for(row)
            if trust.classification == "restricted":
                # Inside the caller's own task, so its existence is not a
                # disclosure -- which is what makes an exclusion the right
                # answer here and the wrong one for non-participation.
                withheld.append(Exclusion(item_key=str(row.checkpoint_id), reason="classification restricted"))
                continue
            kept.append(_item(row))
        return ArmOutcome(
            items=ordered_items(kept),
            exclusions=tuple(withheld),
            truncated=read.truncated,
            # The rows were read live at `moment`, so the arm does know how
            # fresh they are. `None` here would claim it does not track
            # staleness, which is a different and untrue statement.
            fresh_as_of=moment,
        )


__all__ = [
    "CLASSIFICATION_FLOOR",
    "DEFAULT_LIMIT",
    "MAX_RESULTS",
    "WorkspaceRecall",
    "classification_for",
]
