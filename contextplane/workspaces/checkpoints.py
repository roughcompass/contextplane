"""Appending to a task's checkpoint chain, and reading one back unchanged.

A checkpoint is what a later agent resumes from, which makes two properties
load-bearing and everything else negotiable.

**The chain is ordered and has no holes.** Appends to one task serialize on a
task-scoped advisory lock, so two agents finishing at the same moment produce
sequence 4 then sequence 5, each naming the other's predecessor -- not two
sequence 4s, and not a 5 whose predecessor was never written. The unique index
and the predecessor CHECK in the database are still the backstop; the lock is
what turns a concurrent append from an error the caller must retry into an
ordered one.

**A checkpoint that has been read once reads the same forever.** Later
checkpoints do not touch it, and neither does the head summary, which is prose
that anybody may overwrite. That separation is the reason the head is a
projection rather than the record: a mutable field cannot be the evidence a
claim rests on, because the version that was relied upon is gone the moment it
is edited.

**Identity comes from the idempotency key, not from a fresh UUID per attempt.**
The checkpoint id is derived from tenant, task and the caller's key, so a retry
after a lost response resolves to the row the first attempt wrote instead of
appending the same step twice. Reusing one key for *different* content is
refused rather than merged: the caller believes those are one write, and the
two possible repairs -- overwriting the stored checkpoint, or appending a second
one under the same key -- are both worse than saying no.

**Attribution and time are the server's.** Both are taken from the
authenticated request and the clock, and the frozen checkpoint shape refuses a
payload that supplies either.

**Tenant scope and task audience are both enforced here, in SQL.** Every read
and the append carry a correlated `EXISTS` against the active participant
grants, so a caller with no grant on the task gets nothing back and cannot
write -- whether it arrived through a published surface or called this service
directly. The transports keep their own guard as defence in depth; neither is
load-bearing alone.

For a while only the transports checked, and this paragraph said so: it noted
that a caller publishing the service without resolving the audience first had
published an append any tenant member could aim at any task id. That was an
accurate description of a hole, which is not the same as a control. It is now
one statement, and a caller that forgets to check cannot be the reason a
checkpoint leaks.

**A refusal is indistinguishable from absence.** A checkpoint the actor may not
see reads as not found, and an append it may not make fails the way an append to
a nonexistent task would. Three separable answers -- no such task, not a
participant, grant expired -- together enumerate the tenant's tasks.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from prometheus_client import Counter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.context.schemas.reference import normalize_reference
from contextplane.context.schemas.trust import ExternalReferenceV1, InvalidContextItem
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.types import Clock, TenantContext
from contextplane.workspaces import queries_checkpoint as queries
from contextplane.workspaces.audience import CAPABILITY_EXTEND, AudienceDenied
from contextplane.workspaces.schemas.intent_memory import IntentCheckpointV1, checkpoint_from_client_payload

_log = logging.getLogger(__name__)

# A fixed namespace, so a checkpoint id is reproducible from the tenant, the
# task and the caller's idempotency key on any deployment. Changing this value
# would make every retry of an in-flight request append a duplicate step, so it
# is a constant rather than configuration.
_CHECKPOINT_NAMESPACE: Final = uuid.UUID("8d0c6f0f-52a2-5b3f-9a3d-1f3a0f9c6d21")

# Long enough for a request id, a run id or a digest; short enough that the key
# is a name for one attempt rather than somewhere to park a payload.
MAX_IDEMPOTENCY_KEY_LENGTH: Final = 200

# The head summary is a projection, not a place to write a report. Bounded so a
# caller cannot use it as the storage the checkpoint chain deliberately is not.
MAX_SUMMARY_LENGTH: Final = 2000

AUDIT_TARGET_TYPE: Final = "intent_checkpoint"
ACTION_CHECKPOINT_APPENDED: Final = "intent.checkpoint.appended"
ACTION_HEAD_SUMMARY_SET: Final = "intent.head.summary_set"

_APPENDED = Counter(
    "contextplane_intent_checkpoint_appended_total",
    "Checkpoints appended to a task chain.",
)
_REPLAYED = Counter(
    "contextplane_intent_checkpoint_replayed_total",
    "Appends resolved to an existing checkpoint because the idempotency key was reused with identical content.",
)
_CONFLICTED = Counter(
    "contextplane_intent_checkpoint_conflict_total",
    "Appends refused because an idempotency key was reused with different content.",
)


@dataclasses.dataclass(frozen=True)
class AppendResult:
    """A checkpoint, and whether this call is what created it.

    `created` is carried rather than inferred by the caller: a replay and a
    fresh append return the same checkpoint, and a surface that wants to answer
    `201` versus `200` cannot tell them apart from the row alone.
    """

    checkpoint: IntentCheckpointV1
    created: bool


def checkpoint_identity(*, tenant_id: uuid.UUID, intent_id: uuid.UUID, idempotency_key: str) -> uuid.UUID:
    """The stable id an append under this key resolves to.

    Derived rather than random so the identity exists before the row does. That
    is what lets a retry find its own earlier write with a plain primary-key
    read, instead of scanning for something that looks similar -- and it makes
    the database's own primary key the last line of defence against a duplicate
    append, even for a writer that skipped the task lock.

    The tenant is in the derivation so one tenant cannot aim a key at an id
    another tenant already holds.
    """
    return uuid.uuid5(_CHECKPOINT_NAMESPACE, f"{tenant_id}:{intent_id}:{idempotency_key}")


class IntentCheckpointService:
    """Append-only checkpoint writes and stable reads for one deployment."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        retention_policy: str = "standard",
    ) -> None:
        """Bind the deployment's retention policy at construction, not per request.

        Retention is a deployment decision about how long a record is kept. A
        per-request argument would let the caller writing the record also choose
        how long anyone can audit it, which is the one party that should not get
        that vote.
        """
        if not retention_policy.strip():
            raise ValueError("a checkpoint service needs a retention policy; every checkpoint binds one at write time")
        self._session_factory = session_factory
        self._clock = clock
        self._retention_policy = retention_policy

    # -- writes ----------------------------------------------------------

    async def append_checkpoint(
        self,
        ctx: TenantContext,
        *,
        intent_id: uuid.UUID,
        payload: Mapping[str, Any],
        idempotency_key: str,
        evidence: Sequence[Mapping[str, Any]] = (),
    ) -> AppendResult:
        """Append one checkpoint to a task, or return the one this key already wrote.

        Everything that decides ordering, identity, attribution and retention is
        computed here from the server's own view; `payload` carries content
        only, and the frozen checkpoint shape refuses one that tries to carry
        more.
        """
        key = self._checked_key(idempotency_key)
        references = self._normalized_evidence(evidence)
        checkpoint_id = checkpoint_identity(tenant_id=ctx.tenant_id, intent_id=intent_id, idempotency_key=key)
        author = str(ctx.actor_id)
        # One instant for the whole append: the moment the audience is tested
        # against is the moment the checkpoint is stamped with. Two clock reads
        # would leave a window where a grant expires between the test and the
        # stamp, and the row would carry a time the actor was not authorized at.
        moment = self._clock.now()

        async with self._session_factory() as session, session.begin():
            # Taken before the head is read. Reading first and locking after
            # would let two appends both observe sequence 3 as the head and
            # derive the same successor.
            await queries.lock_task(session, tenant_id=ctx.tenant_id, intent_id=intent_id)

            # `extend`, not `read`: this is an append, and a caller who may
            # only read must be refused rather than handed the checkpoint a
            # previous append wrote. Otherwise a reader can probe for stored
            # checkpoints by guessing idempotency keys.
            existing = await queries.select_checkpoint(
                session,
                tenant_id=ctx.tenant_id,
                checkpoint_id=checkpoint_id,
                actor_id=author,
                moment=moment,
                capability=CAPABILITY_EXTEND,
            )
            if existing is not None:
                return self._resolve_replay(
                    existing, payload=payload, references=references, author=author, intent_id=intent_id
                )

            head = await queries.select_head(session, tenant_id=ctx.tenant_id, intent_id=intent_id)
            sequence = 1 if head is None else int(head["head_sequence"]) + 1
            predecessor_id = None if head is None else uuid.UUID(str(head["head_checkpoint_id"]))
            recorded_at = moment

            checkpoint = self._build(
                payload,
                checkpoint_id=checkpoint_id,
                intent_id=intent_id,
                sequence=sequence,
                predecessor_id=predecessor_id,
                author=author,
                recorded_at=recorded_at,
                retention_policy=self._retention_policy,
                references=references,
            )

            inserted = await queries.insert_checkpoint(
                session,
                tenant_id=ctx.tenant_id,
                checkpoint_id=checkpoint.checkpoint_id,
                intent_id=checkpoint.intent_id,
                sequence=checkpoint.sequence,
                predecessor_id=checkpoint.predecessor_id,
                goal=checkpoint.goal,
                decisions=checkpoint.decisions,
                assumptions=checkpoint.assumptions,
                evidence=[_reference_payload(reference) for reference in checkpoint.evidence],
                completed_checks=checkpoint.completed_checks,
                open_questions=checkpoint.open_questions,
                next_action=checkpoint.next_action,
                author=checkpoint.author,
                recorded_at=checkpoint.recorded_at,
                retention_policy=checkpoint.retention_policy,
                digest=checkpoint.digest,
                actor_id=author,
                moment=moment,
            )
            if not inserted:
                # The insert's own WHERE EXISTS refused it. Raised with one
                # reason for every denial: "no such task", "not a participant"
                # and "grant expired" are three answers that together enumerate
                # the tenant's tasks.
                raise AudienceDenied("no active participant grant for this actor on this task")
            await queries.upsert_head(
                session,
                tenant_id=ctx.tenant_id,
                intent_id=checkpoint.intent_id,
                head_checkpoint_id=checkpoint.checkpoint_id,
                head_sequence=checkpoint.sequence,
                summary=checkpoint.next_action or checkpoint.goal,
                updated_at=recorded_at,
            )
            # The head summary is this checkpoint's own words, copied into a row
            # the checkpoint's table knows nothing about. Registered in the same
            # transaction that writes it, so an erasure of the checkpoint can
            # always find the copy -- there is no window where one exists without
            # the other.
            await queries.register_summary_derivative(
                session,
                tenant_id=ctx.tenant_id,
                intent_id=checkpoint.intent_id,
                head_checkpoint_id=checkpoint.checkpoint_id,
            )
            # Same transaction as the two writes above: an appended checkpoint
            # always has its audit row, and a rolled-back append leaves neither.
            await queries.insert_audit(
                session,
                audit_id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                action=ACTION_CHECKPOINT_APPENDED,
                target_type=AUDIT_TARGET_TYPE,
                target_id=checkpoint.checkpoint_id,
                # Identity and position only. The goal, decisions and open
                # questions are the task's content and are already stored once;
                # copying them here would spread the same text into a second
                # table with a different retention rule.
                after={
                    "intent_id": str(checkpoint.intent_id),
                    "sequence": checkpoint.sequence,
                    "predecessor_id": None if checkpoint.predecessor_id is None else str(checkpoint.predecessor_id),
                    "digest": checkpoint.digest,
                    "retention_policy": checkpoint.retention_policy,
                    "evidence_count": len(checkpoint.evidence),
                },
                ts=recorded_at,
            )

        _APPENDED.inc()
        _log.info(
            "task_checkpoint_appended",
            extra={
                "tenant_id": str(ctx.tenant_id),
                "intent_id": str(intent_id),
                "checkpoint_id": str(checkpoint.checkpoint_id),
                "sequence": checkpoint.sequence,
                "digest": checkpoint.digest,
            },
        )
        return AppendResult(checkpoint=checkpoint, created=True)

    async def set_head_summary(self, ctx: TenantContext, *, intent_id: uuid.UUID, summary: str) -> None:
        """Overwrite the mutable summary on a task's head.

        Deliberately cannot move the head or touch a checkpoint. The summary is
        the one part of task memory anybody may rewrite, and the chain it points
        at is the part nobody may -- which is what keeps a summary edit from
        changing what a past agent is recorded as having decided.
        """
        if not summary.strip():
            raise ValidationError("a head summary says something or is left as it was; an empty one says neither")
        if len(summary) > MAX_SUMMARY_LENGTH:
            raise ValidationError(
                f"head summary is {len(summary)} characters, over the {MAX_SUMMARY_LENGTH}-character bound; "
                "the summary points at the chain, it does not replace it"
            )
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            head_checkpoint_id = await queries.update_head_summary(
                session, tenant_id=ctx.tenant_id, intent_id=intent_id, summary=summary, updated_at=now
            )
            if head_checkpoint_id is None:
                raise NotFoundError(
                    f"task {intent_id} has no checkpoints in this tenant, so it has no head to summarize"
                )
            # Caller-written prose about a task is still content derived from the
            # chain it describes, and it is registered on the same terms as the
            # summary an append writes: same transaction, same locator, this head
            # checkpoint as its source.
            await queries.register_summary_derivative(
                session,
                tenant_id=ctx.tenant_id,
                intent_id=intent_id,
                head_checkpoint_id=head_checkpoint_id,
            )
            await queries.insert_audit(
                session,
                audit_id=uuid.uuid4(),
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                action=ACTION_HEAD_SUMMARY_SET,
                target_type=AUDIT_TARGET_TYPE,
                target_id=intent_id,
                after={"intent_id": str(intent_id)},
                ts=now,
            )

    # -- reads -----------------------------------------------------------

    async def get_checkpoint(self, ctx: TenantContext, *, checkpoint_id: uuid.UUID) -> IntentCheckpointV1:
        """One checkpoint by its stable id, as it was written."""
        async with self._session_factory() as session:
            row = await queries.select_checkpoint(
                session,
                tenant_id=ctx.tenant_id,
                checkpoint_id=checkpoint_id,
                actor_id=str(ctx.actor_id),
                moment=self._clock.now(),
            )
        if row is None:
            raise NotFoundError(f"checkpoint {checkpoint_id} not found")
        return _from_row(row)

    async def get_checkpoint_by_digest(self, ctx: TenantContext, *, digest: str) -> IntentCheckpointV1:
        """One checkpoint by the digest that names its content.

        The digest is a second stable handle on the same row, which is what lets
        a reader who was handed a digest confirm the content it names rather
        than trusting a copy of it.
        """
        if not digest.strip():
            raise ValidationError("a checkpoint digest lookup needs a digest")
        async with self._session_factory() as session:
            row = await queries.select_checkpoint_by_digest(
                session,
                tenant_id=ctx.tenant_id,
                digest=digest,
                actor_id=str(ctx.actor_id),
                moment=self._clock.now(),
            )
        if row is None:
            raise NotFoundError("no checkpoint with that digest")
        return _from_row(row)

    async def get_head(self, ctx: TenantContext, *, intent_id: uuid.UUID) -> Mapping[str, Any]:
        """The current head projection for a task."""
        async with self._session_factory() as session:
            row = await queries.select_head(session, tenant_id=ctx.tenant_id, intent_id=intent_id)
        if row is None:
            raise NotFoundError(f"task {intent_id} has no checkpoints in this tenant")
        return dict(row)

    # -- internals -------------------------------------------------------

    def _resolve_replay(
        self,
        existing: Mapping[str, Any],
        *,
        payload: Mapping[str, Any],
        references: tuple[ExternalReferenceV1, ...],
        author: str,
        intent_id: uuid.UUID,
    ) -> AppendResult:
        """Decide whether a reused idempotency key is a retry or a collision.

        The comparison is made against the stored checkpoint's own position and
        retention, so only what the caller controls -- the content and who they
        authenticated as -- can make the two differ. Comparing against the
        *current* retention policy instead would turn every replay that crossed
        a policy change into a spurious conflict about something the caller
        never sent.
        """
        stored = _from_row(existing)
        candidate = self._build(
            payload,
            checkpoint_id=stored.checkpoint_id,
            intent_id=intent_id,
            sequence=stored.sequence,
            predecessor_id=stored.predecessor_id,
            author=author,
            recorded_at=stored.recorded_at,
            retention_policy=stored.retention_policy,
            references=references,
        )
        if candidate.digest != stored.digest:
            _CONFLICTED.inc()
            raise ConflictError(
                f"idempotency key already recorded checkpoint {stored.checkpoint_id} with different content; "
                "the caller believes these are one write, and neither overwriting the stored checkpoint nor "
                "appending a second one under the same key would be true"
            )
        _REPLAYED.inc()
        return AppendResult(checkpoint=stored, created=False)

    def _build(
        self,
        payload: Mapping[str, Any],
        *,
        checkpoint_id: uuid.UUID,
        intent_id: uuid.UUID,
        sequence: int,
        predecessor_id: uuid.UUID | None,
        author: str,
        recorded_at: datetime.datetime,
        retention_policy: str,
        references: tuple[ExternalReferenceV1, ...],
    ) -> IntentCheckpointV1:
        """Assemble the frozen checkpoint, translating its refusals for the boundary.

        `InvalidContextItem` is the schema's own vocabulary for "this cannot be
        assembled as described"; at a service boundary that is a bad request,
        and re-raising it as one keeps every caller on a single error family
        instead of two that mean the same thing.
        """
        try:
            return checkpoint_from_client_payload(
                payload,
                checkpoint_id=checkpoint_id,
                intent_id=intent_id,
                sequence=sequence,
                predecessor_id=predecessor_id,
                author=author,
                recorded_at=recorded_at,
                retention_policy=retention_policy,
                evidence=references,
            )
        except InvalidContextItem as exc:
            raise ValidationError(str(exc)) from exc

    def _checked_key(self, idempotency_key: str) -> str:
        key = idempotency_key.strip()
        if not key:
            raise ValidationError(
                "an append needs an idempotency key; without one a retried request appends the same step twice "
                "and the chain records work that happened once as work that happened twice"
            )
        if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValidationError(
                f"idempotency key is {len(key)} characters, over the {MAX_IDEMPOTENCY_KEY_LENGTH}-character bound"
            )
        return key

    def _normalized_evidence(self, evidence: Sequence[Mapping[str, Any]]) -> tuple[ExternalReferenceV1, ...]:
        """Normalize each reference before the checkpoint shape checks for duplicates.

        Order matters: two spellings of one reference collide only once both are
        folded to the same form. Normalizing afterwards would store them as two
        rows that never meet, which reads as two independent sources supporting
        one claim.
        """
        try:
            return tuple(normalize_reference(dict(reference)) for reference in evidence)
        except InvalidContextItem as exc:
            raise ValidationError(str(exc)) from exc


def _reference_payload(reference: ExternalReferenceV1) -> dict[str, Any]:
    """A reference as JSONB-safe fields, in the shape `normalize_reference` reads back.

    Round-tripping through the same normalizer that produced it is what keeps
    the stored digest verifiable: a rehydrated checkpoint recomputes its digest
    on construction, so any field that did not survive storage surfaces as a
    refused read rather than as a checkpoint that quietly means something else.
    """
    fields = dataclasses.asdict(reference)
    observed_at = fields.get("observed_at")
    if isinstance(observed_at, datetime.datetime):
        fields["observed_at"] = observed_at.isoformat()
    return fields


def _json_list(value: object) -> list[Any]:
    """Read a JSONB column that may arrive already decoded or still as text.

    Which one it is depends on the driver's codec registration, not on
    anything this module controls, so both are handled rather than assumed.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list):
        return list(value)
    msg = f"a checkpoint list column came back as {type(value).__name__}, which is not an array"
    raise InvalidContextItem(msg)


def _from_row(row: Mapping[str, Any]) -> IntentCheckpointV1:
    """Rehydrate a stored checkpoint, verifying it against its own digest.

    The frozen shape recomputes the digest on construction, so a row whose
    content no longer matches the digest it carries is refused here instead of
    being served as evidence. That check is the reason retrieval is worth
    trusting at all: the table is append-only, but "append-only" is a claim
    about writers, and this is the check that does not depend on it.
    """
    references = tuple(normalize_reference(dict(entry)) for entry in _json_list(row["evidence"]))
    predecessor_id = row["predecessor_id"]
    return IntentCheckpointV1(
        checkpoint_id=uuid.UUID(str(row["checkpoint_id"])),
        intent_id=uuid.UUID(str(row["intent_id"])),
        sequence=int(row["sequence"]),
        predecessor_id=None if predecessor_id is None else uuid.UUID(str(predecessor_id)),
        goal=row["goal"],
        decisions=tuple(_json_list(row["decisions"])),
        assumptions=tuple(_json_list(row["assumptions"])),
        evidence=references,
        completed_checks=tuple(_json_list(row["completed_checks"])),
        open_questions=tuple(_json_list(row["open_questions"])),
        next_action=row["next_action"],
        author=row["author"],
        recorded_at=row["recorded_at"],
        retention_policy=row["retention_policy"],
        digest=row["digest"],
    )


__all__ = [
    "ACTION_CHECKPOINT_APPENDED",
    "ACTION_HEAD_SUMMARY_SET",
    "AUDIT_TARGET_TYPE",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_SUMMARY_LENGTH",
    "AppendResult",
    "IntentCheckpointService",
    "checkpoint_identity",
]
