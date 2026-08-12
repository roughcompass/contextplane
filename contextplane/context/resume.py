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

**Determinism means stable identities and ordering.** Two resumes against the
same durable inputs return the same rows in the same order -- not merely
equivalent sets, because a caller diffing two resumes to see what changed would
otherwise see churn that no work caused. Governed claim freshness is evaluated
at request time, so its ``as_of`` basis and decayed confidence may advance even
when those ordered claim identities do not.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from contextplane.context.models import ContextExternalReference, ContextReferenceBinding
from contextplane.context.models_receipt import ContextReceipt
from contextplane.service.memory.claim_serving import ClaimQuery, ClaimServingService, ServedClaim
from contextplane.workspaces.models import IntentCheckpoint
from contextplane.workspaces.queries_audience import fetch_actor_role, lookup_authorized_head

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.types import Clock, TenantContext

#: What a reference binds to when a checkpoint cites it. The junction's subject
#: set is closed, and receipts use their own value -- so naming the constant here
#: keeps the two reads from drifting onto each other's rows.
SUBJECT_TASK_CHECKPOINT = "intent_checkpoint"

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

#: Most feedback rows from the last receipt a resume may return. The query is
#: composed above this package because signals sit above context in the import
#: contract, but the bound belongs to the one resume request both transports use.
DEFAULT_FEEDBACK_BOUND = 20

#: Most claims that became reviewed after the last receipt. Smaller than the
#: reference bound because every claim carries its citations and trust labels.
DEFAULT_LEARNING_BOUND = 10


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
    feedback_bound: int = DEFAULT_FEEDBACK_BOUND
    learning_bound: int = DEFAULT_LEARNING_BOUND

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
            ("feedback_bound", self.feedback_bound),
            ("learning_bound", self.learning_bound),
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

    intent_id: uuid.UUID | None
    head_checkpoint_id: uuid.UUID | None
    head_sequence: int | None
    head_summary: str | None
    checkpoints: tuple[IntentCheckpoint, ...]
    receipts: tuple[ContextReceipt, ...]
    references: tuple[ContextExternalReference, ...]
    open_questions: tuple[str, ...]
    next_action: str | None
    truncated: tuple[str, ...]
    #: Task ids the named work resolved to, when it resolved to more than one.
    #: Empty in the ordinary case. Returning no task is still correct -- picking
    #: one would give two callers different answers from the same request -- but
    #: returning no explanation left them unable to tell "two tasks cite this
    #: reference, disambiguate" from "there is nothing to resume".
    ambiguous_intent_ids: tuple[uuid.UUID, ...] = ()
    #: Governed claims that became serveable after the newest receipt. These are
    #: the ordinary claim-serving objects, so resume cannot lose citations,
    #: confidence, authority, or the recalled/untrusted label while adapting them.
    learning: tuple[ServedClaim, ...] = ()

    def is_empty(self) -> bool:
        """True when nothing authorized was found.

        Distinct from a resume that found a task with no checkpoints: that one
        has a task id. Empty means the references named nothing this actor may
        see, which a caller should treat as "start fresh" rather than as an
        error.
        """
        return self.intent_id is None and not self.ambiguous_intent_ids and not self.receipts and not self.references

    def is_ambiguous(self) -> bool:
        """True when the named work belongs to more than one task.

        A distinct state from empty. "Nothing to resume" says start fresh; this
        says the request was under-specified, and names what to choose between.
        """
        return bool(self.ambiguous_intent_ids)


class ContextResumeService:
    """Bounded resume over checkpoints, receipts and the work they cite."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        claims: ClaimServingService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        # Reuse the governed claim read rather than constructing claim-shaped
        # response data from raw rows. The optional injection keeps unit tests
        # able to prove the candidate selection without rebuilding that service.
        self._claims = claims or ClaimServingService(session_factory, clock=clock)

    async def resume(self, ctx: TenantContext, request: ResumeRequest) -> ResumeState:
        """Everything needed to carry on with the named work, within bounds.

        Ordering is fixed at every level: checkpoints by sequence, receipts by
        resolution time, references by their identity tuple, and learning by
        review time plus claim id. The governed claim reader still stamps its
        request-time freshness basis; identity and ordering, not a stale
        confidence value, are the deterministic contract.
        """
        moment = self._clock.now()
        truncated: list[str] = []

        async with self._session_factory() as session:
            references = await self._resolve_references(session, ctx=ctx, request=request, truncated=truncated)
            reference_ids = tuple(reference.reference_id for reference in references)

            candidates = await self._task_for_references(session, ctx=ctx, reference_ids=reference_ids)
            await self._require_task_audience(session, ctx=ctx, intent_ids=candidates, moment=moment)
            # One task is an answer. More than one is a question for the caller,
            # named rather than swallowed.
            intent_id = candidates[0] if len(candidates) == 1 else None
            ambiguous = candidates if len(candidates) > 1 else ()

            head = None
            if intent_id is not None:
                head = await lookup_authorized_head(
                    session,
                    tenant_id=ctx.tenant_id,
                    actor_id=str(ctx.actor_id),
                    intent_id=intent_id,
                    moment=moment,
                )

            checkpoints: tuple[IntentCheckpoint, ...] = ()
            if intent_id is not None and head is not None:
                checkpoints = await self._recent_checkpoints(
                    session, ctx=ctx, intent_id=intent_id, bound=request.checkpoint_bound, truncated=truncated
                )

            receipts = await self._recent_receipts(
                session, ctx=ctx, reference_ids=reference_ids, bound=request.receipt_bound, truncated=truncated
            )

        learning: tuple[ServedClaim, ...] = ()
        if receipts:
            learning = await self._newer_learning(
                ctx,
                after=receipts[0].resolved_at,
                moment=moment,
                bound=request.learning_bound,
                truncated=truncated,
            )

        latest = checkpoints[-1] if checkpoints else None
        return ResumeState(
            intent_id=intent_id,
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
            ambiguous_intent_ids=ambiguous,
            learning=learning,
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
    ) -> tuple[uuid.UUID, ...]:
        """Every task the named work belongs to, capped at two.

        Two is enough: one is the answer, more than one is ambiguous however
        many more there are. Reading the rest would cost a scan to report a
        number no caller acts on differently.

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
            return ()

        stmt = (
            select(IntentCheckpoint.intent_id)
            .join(ContextReferenceBinding, ContextReferenceBinding.subject_id == IntentCheckpoint.checkpoint_id)
            .where(
                IntentCheckpoint.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.tenant_id == ctx.tenant_id,
                ContextReferenceBinding.subject_type == SUBJECT_TASK_CHECKPOINT,
                ContextReferenceBinding.reference_id.in_(tuple(reference_ids)),
            )
            .distinct()
            .limit(2)
        )
        return tuple((await session.execute(stmt)).scalars().all())

    async def _require_task_audience(
        self,
        session: AsyncSession,
        *,
        ctx: TenantContext,
        intent_ids: Sequence[uuid.UUID],
        moment: datetime.datetime,
    ) -> None:
        """Refuse named task work unless the caller participates now.

        Reference resolution is tenant-scoped but task memory is audience-
        scoped. Checking only the head would turn a missing grant into a partial
        successful resume whose receipt and learning arms still returned data.
        The refusal happens before either arm runs, and its message names no task
        id so the denial does not become an inventory of hidden work.
        """
        for intent_id in intent_ids:
            role = await fetch_actor_role(
                session,
                tenant_id=ctx.tenant_id,
                intent_id=intent_id,
                actor_id=str(ctx.actor_id),
                moment=moment,
            )
            if role is None:
                raise PermissionError("the caller is outside the task audience")

    async def _recent_checkpoints(
        self,
        session: AsyncSession,
        *,
        ctx: TenantContext,
        intent_id: uuid.UUID,
        bound: int,
        truncated: list[str],
    ) -> tuple[IntentCheckpoint, ...]:
        """The last few steps, oldest first.

        Read newest-first so the bound keeps the *recent* end, then reversed so
        the caller reads them in the order they happened. Bounding from the old
        end would return the beginning of a long task and call it resume.
        """
        stmt = (
            select(IntentCheckpoint)
            .where(IntentCheckpoint.tenant_id == ctx.tenant_id, IntentCheckpoint.intent_id == intent_id)
            .order_by(IntentCheckpoint.sequence.desc())
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

    async def _newer_learning(
        self,
        ctx: TenantContext,
        *,
        after: datetime.datetime,
        moment: datetime.datetime,
        bound: int,
        truncated: list[str],
    ) -> tuple[ServedClaim, ...]:
        """Reviewed claims that became serveable after the last receipt.

        "Reviewed" means the existing claim-serving contract: a staged or
        superseded claim with ``consolidated_at`` set. The comparison is against
        ``consolidated_at`` rather than ``created_at`` because a claim drafted
        before the receipt but reviewed afterwards is precisely new learning to
        a reconnecting caller. Promotion into the canonical graph is not
        required; resume serves recalled learning with the same citations,
        confidence and trust labels as every other claim read.

        The window is read through ``ClaimServingService.consolidated_since``
        rather than selected here. Resume is a read path, and a read path that
        queried the claim tables directly would be a second place deciding what a
        staged assertion is allowed to look like -- which is how an unverified
        claim acquires the authority of a reviewed one. Asking the owning service
        keeps visibility, decay, citations and recall labelling in the one place
        that decides them, and costs one query rather than one per claim.

        One more than the bound is requested, so "there is more" is answered by
        the read rather than inferred from a page that happens to be full.
        """
        served = await self._claims.consolidated_since(
            ctx, after=after, as_of=moment, limit=min(bound + 1, ClaimQuery.MAX_LIMIT)
        )
        if len(served) > bound:
            truncated.append("learning")
            served = served[:bound]
        return tuple(served)


__all__ = [
    "DEFAULT_CHECKPOINT_BOUND",
    "DEFAULT_FEEDBACK_BOUND",
    "DEFAULT_LEARNING_BOUND",
    "DEFAULT_RECEIPT_BOUND",
    "DEFAULT_REFERENCE_BOUND",
    "ContextResumeService",
    "ResumeRequest",
    "ResumeState",
    "SUBJECT_CONTEXT_ITEM",
    "SUBJECT_TASK_CHECKPOINT",
]
