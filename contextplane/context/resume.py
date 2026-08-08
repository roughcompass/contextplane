"""Pick up a task where it was left, from the work it was about.

Resume is the capability the phase exists to make deterministic. An agent
returning to a task after an hour, a restart or a handoff has to get the same
answer its predecessor would have got, and has to get it from what it can name:
a run, a stage, a pull request -- never a session id it does not have.

**Bounded, and the bounds are not advisory.** Every arm of a resume has a cap
applied here rather than trusted to its query. Unbounded resume is how a task
that ran for three weeks returns three weeks of material and the caller's
context window decides what got dropped -- silently, and differently every time.

**Never a transcript.** Not "a truncated transcript", not "the last few
messages": nothing. The whole design of the checkpoint chain is that an agent
records what it concluded rather than everything it said, and handing back the
raw exchange would make the summary decorative and the privacy story
meaningless. There is no parameter here that can ask for one.

**Determinism means the same head gives the same answer.** Two resumes against
an unchanged head return identical material in identical order -- not merely
equivalent sets, because a caller diffing two resumes to see what changed would
otherwise see churn that no work caused. Later checkpoints change the head and
therefore change the answer, which is the only thing that should.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from contextplane.context.models import ContextExternalReference, ContextReferenceBinding
from contextplane.context.models_receipt import ContextReceipt
from contextplane.workspaces.models import TaskCheckpoint
from contextplane.workspaces.queries_audience import lookup_authorized_head

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock, TenantContext

#: What a reference binds to when a checkpoint cites it. The junction's subject
#: set is closed, and receipts use their own value -- so naming the constant here
#: keeps the two reads from drifting onto each other's rows.
SUBJECT_TASK_CHECKPOINT = "task_checkpoint"

#: What a receipt binds under, for the prior-resolution arm.
SUBJECT_CONTEXT_ITEM = "context_item"

#: Most checkpoints a resume will look back over. A task that ran for weeks has
#: hundreds; returning them would let the task's age decide how much of the
#: caller's context window resume consumes.
DEFAULT_CHECKPOINT_BOUND = 5

#: Most prior receipts a resume will name. One is the common case -- what the
#: last resolution knew -- and the rest exist so a caller can see whether the
#: answer has been drifting.
DEFAULT_RECEIPT_BOUND = 3

#: Most external references a resume will echo back.
DEFAULT_REFERENCE_BOUND = 20


@dataclasses.dataclass(frozen=True)
class ResumeRequest:
    """The work a caller is picking up, named the way they can name it.

    External references rather than a task id, because the caller resuming is
    usually not the process that created the task -- it is a new run of a
    pipeline that knows its own run id and the pull request it is working on.

    There is deliberately no `include_transcript` flag. A parameter that could
    be set to true is a parameter somebody sets to true.
    """

    references: tuple[tuple[str, str, str, str], ...]
    checkpoint_bound: int = DEFAULT_CHECKPOINT_BOUND
    receipt_bound: int = DEFAULT_RECEIPT_BOUND
    reference_bound: int = DEFAULT_REFERENCE_BOUND

    def __post_init__(self) -> None:
        if not self.references:
            raise ValueError(
                "a resume needs at least one external reference to resume from; resuming everything "
                "a tenant has ever done is not a resume"
            )
        for name, bound in (
            ("checkpoint_bound", self.checkpoint_bound),
            ("receipt_bound", self.receipt_bound),
            ("reference_bound", self.reference_bound),
        ):
            if bound < 1:
                raise ValueError(f"{name} must be at least 1, got {bound}; a bound of zero returns nothing")


@dataclasses.dataclass(frozen=True)
class ResumeState:
    """What a caller needs to carry on, and nothing it does not.

    `truncated` names the arms that hit their bound. A resume that quietly
    returned the first five of forty checkpoints would read as the whole story,
    and the caller would carry on from a middle it believed was the start.
    """

    task_id: uuid.UUID | None
    head_checkpoint_id: uuid.UUID | None
    head_sequence: int | None
    head_summary: str | None
    checkpoints: tuple[TaskCheckpoint, ...]
    receipts: tuple[ContextReceipt, ...]
    references: tuple[ContextExternalReference, ...]
    open_questions: tuple[str, ...]
    next_action: str | None
    truncated: tuple[str, ...]

    def is_empty(self) -> bool:
        """True when nothing authorized was found.

        Distinct from a resume that found a task with no checkpoints: that one
        has a task id. Empty means the references named nothing this actor may
        see, which a caller should treat as "start fresh" rather than as an
        error.
        """
        return self.task_id is None and not self.receipts and not self.references


class ContextResumeService:
    """Bounded resume over checkpoints, receipts and the work they cite."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def resume(self, ctx: TenantContext, request: ResumeRequest) -> ResumeState:
        """Everything needed to carry on with the named work, within bounds.

        Ordering is fixed at every level: checkpoints by sequence, receipts by
        resolution time, references by their identity tuple. Two resumes against
        an unchanged head therefore return byte-identical material -- a caller
        diffing them to see what moved sees only what actually moved.
        """
        moment = self._clock.now()
        truncated: list[str] = []

        async with self._session_factory() as session:
            references = await self._resolve_references(session, ctx=ctx, request=request, truncated=truncated)
            reference_ids = tuple(reference.reference_id for reference in references)

            task_id = await self._task_for_references(session, ctx=ctx, reference_ids=reference_ids)

            head = None
            if task_id is not None:
                head = await lookup_authorized_head(
                    session,
                    tenant_id=ctx.tenant_id,
                    actor_id=str(ctx.actor_id),
                    task_id=task_id,
                    moment=moment,
                )

            checkpoints: tuple[TaskCheckpoint, ...] = ()
            if task_id is not None and head is not None:
                checkpoints = await self._recent_checkpoints(
                    session, ctx=ctx, task_id=task_id, bound=request.checkpoint_bound, truncated=truncated
                )

            receipts = await self._recent_receipts(
                session, ctx=ctx, reference_ids=reference_ids, bound=request.receipt_bound, truncated=truncated
            )

        latest = checkpoints[-1] if checkpoints else None
        return ResumeState(
            task_id=task_id,
            head_checkpoint_id=head.head_checkpoint_id if head else None,
            head_sequence=head.head_sequence if head else None,
            head_summary=head.summary if head else None,
            checkpoints=checkpoints,
            receipts=receipts,
            references=references,
            # Carried from the newest checkpoint rather than merged across the
            # window: a question closed three checkpoints ago is not open, and
            # a union would resurrect it.
            open_questions=tuple(latest.open_questions) if latest else (),
            next_action=latest.next_action if latest else None,
            truncated=tuple(truncated),
        )

    # -- arms -------------------------------------------------------------

    async def _resolve_references(
        self,
        session: AsyncSession,
        *,
        ctx: TenantContext,
        request: ResumeRequest,
        truncated: list[str],
    ) -> tuple[ContextExternalReference, ...]:
        """The named work, as stored references this tenant owns.

        The tenant predicate is in the SELECT. A reference belonging to another
        tenant contributes nothing rather than being filtered afterwards, so a
        caller cannot learn that a run id exists elsewhere by watching a count.
        """
        clauses = [
            (
                (ContextExternalReference.source_system == system)
                & (ContextExternalReference.source_namespace == namespace)
                & (ContextExternalReference.kind == kind)
                & (ContextExternalReference.external_id == external_id)
            )
            for system, namespace, kind, external_id in request.references
        ]
        matched = clauses[0]
        for clause in clauses[1:]:
            matched = matched | clause

        stmt = (
            select(ContextExternalReference)
            .where(ContextExternalReference.tenant_id == ctx.tenant_id, matched)
            .order_by(
                ContextExternalReference.source_system,
                ContextExternalReference.source_namespace,
                ContextExternalReference.kind,
                ContextExternalReference.external_id,
            )
            .limit(request.reference_bound + 1)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if len(rows) > request.reference_bound:
            truncated.append("references")
            rows = rows[: request.reference_bound]
        return tuple(rows)

    async def _task_for_references(
        self,
        session: AsyncSession,
        *,
        ctx: TenantContext,
        reference_ids: Sequence[uuid.UUID],
    ) -> uuid.UUID | None:
        """The task the named work belongs to, if exactly one does.

        Reached through the checkpoints that cite the reference rather than
        through a binding on the task itself: a reference is evidence a
        checkpoint recorded, and the junction's closed subject set says so --
        there is no `task` subject type, and inventing one would put the same
        fact in two places.

        Ambiguity resolves to nothing rather than to a guess. Two tasks citing
        the same pull request is a real situation, and picking one would give
        two callers different answers from the same request with no way to tell
        which they got.
        """
        if not reference_ids:
            return None

        stmt = (
            select(TaskCheckpoint.task_id)
            .join(ContextReferenceBinding, ContextReferenceBinding.subject_id == TaskCheckpoint.checkpoint_id)
            .where(
                TaskCheckpoint.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.subject_type == SUBJECT_TASK_CHECKPOINT,
                ContextReferenceBinding.reference_id.in_(tuple(reference_ids)),
            )
            .distinct()
            .limit(2)
        )
        found = list((await session.execute(stmt)).scalars().all())
        return found[0] if len(found) == 1 else None

    async def _recent_checkpoints(
        self,
        session: AsyncSession,
        *,
        ctx: TenantContext,
        task_id: uuid.UUID,
        bound: int,
        truncated: list[str],
    ) -> tuple[TaskCheckpoint, ...]:
        """The last few steps, oldest first.

        Read newest-first so the bound keeps the *recent* end, then reversed so
        the caller reads them in the order they happened. Bounding from the old
        end would return the beginning of a long task and call it resume.
        """
        stmt = (
            select(TaskCheckpoint)
            .where(TaskCheckpoint.tenant_id == ctx.tenant_id, TaskCheckpoint.task_id == task_id)
            .order_by(TaskCheckpoint.sequence.desc())
            .limit(bound + 1)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if len(rows) > bound:
            truncated.append("checkpoints")
            rows = rows[:bound]
        return tuple(reversed(rows))

    async def _recent_receipts(
        self,
        session: AsyncSession,
        *,
        ctx: TenantContext,
        reference_ids: Sequence[uuid.UUID],
        bound: int,
        truncated: list[str],
    ) -> tuple[ContextReceipt, ...]:
        """What previous resolutions of this work concluded, newest first."""
        if not reference_ids:
            return ()

        stmt = (
            select(ContextReceipt)
            .join(ContextReferenceBinding, ContextReferenceBinding.subject_id == ContextReceipt.receipt_id)
            .where(
                ContextReceipt.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.subject_type == SUBJECT_CONTEXT_ITEM,
                ContextReferenceBinding.reference_id.in_(tuple(reference_ids)),
            )
            .order_by(ContextReceipt.resolved_at.desc(), ContextReceipt.receipt_id)
            .limit(bound + 1)
        )
        rows = list((await session.execute(stmt)).scalars().unique().all())
        if len(rows) > bound:
            truncated.append("receipts")
            rows = rows[:bound]
        return tuple(rows)


__all__ = [
    "DEFAULT_CHECKPOINT_BOUND",
    "DEFAULT_RECEIPT_BOUND",
    "DEFAULT_REFERENCE_BOUND",
    "ContextResumeService",
    "ResumeRequest",
    "ResumeState",
    "SUBJECT_CONTEXT_ITEM",
    "SUBJECT_TASK_CHECKPOINT",
]
