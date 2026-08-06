"""CheckpointExportService: gets a pending `arc_operational_chain_
checkpoints` row acknowledged by an external append-only sink, and records
that acknowledgment durably.

The reason this exists at all: a compromised database alone must not be
able to delete a valid suffix of a revision's operational chain and reset
its head to an older, still-valid value. A local hash chain cannot detect
that on its own -- the shortened chain is still internally consistent,
just shorter. An external, independently-held acknowledgment of "this
deployment had reached at least this sequence" is what closes that gap,
and this service is what gets one.

**The sink is out of scope for this task.** `CheckpointSink` below is the
append-only sink adapter abstraction; a real implementation backed by a
genuine append-only store (object storage with retention lock, a
write-once ledger service, ...) is not. Every wiring call site constructs
this service with `sink=None`, which is not a stub that pretends to
succeed -- it is the honest "no sink is configured," and
`export_checkpoint` treats an unavailable sink exactly the way it should
be treated: the checkpoint stays pending, visible to an operator, never
silently marked durable. Tests inject a sink that actually implements the
protocol to exercise the rest of this service.

**Two transactions, not one**, matching `receipt.py`'s `mark_integrity_
failed`'s own reasoning for why some writes cannot share a session with
the read that decided to make them: the sink call is an external,
possibly slow round trip, and holding a database transaction open across
it would serialize unrelated work behind however long the sink takes to
answer. `export_checkpoint` reads the checkpoint on its own short-lived
session, calls the sink with no transaction open at all, and then opens a
second, separate transaction to record the result -- so a crash between
the sink's acknowledgment and that second transaction's commit is a real,
named scenario this design accounts for: local state stays pending until
that second transaction commits, and retrying `export_checkpoint` for the
same checkpoint is how it reconciles -- the sink's own identity-keyed
idempotency means asking it again returns the same receipt rather than
creating a second one.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import uuid
from typing import Any, Protocol

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service.queries import operational_chain as queries
from registry.exceptions import NotFoundError, RegistryError
from registry.types import Clock

#: `arc_operational_chain_checkpoints.last_export_error_code`'s bound,
#: matching `arc_audit_outbox`'s own 64-character ceiling -- these are the
#: only two codes this service ever writes there, never a raw exception
#: string.
ERROR_SINK_UNAVAILABLE = "sink_unavailable"
ERROR_SINK_FAILED = "sink_failed"


class CheckpointIntegrityError(RegistryError):
    """A checkpoint's export revealed the local chain and the sink
    disagree (`arc_operational_integrity_failed`, 409).

    Carries `reason_code`: `"sink_mismatch"` (the sink already holds a
    different digest for this exact `{deployment_id, revision_id,
    sequence}` identity), `"suffix_rollback"` (the sink has acknowledged a
    sequence this deployment's local chain no longer reaches -- exactly
    what a compromised database resetting its own head to an older value
    would produce), or `"missing_receipt"` (a checkpoint this deployment
    recorded as durable has no matching receipt at the sink).
    Never a repair opportunity: recovery is refusing to trust the local
    chain for this revision, not silently accepting whichever side looks
    newer.
    """

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class CheckpointSinkIdentityConflict(RegistryError):
    """Raised *by a sink implementation* (never by this service) when
    `append` is called with an identity the sink already holds a different
    digest for. `export_checkpoint` catches this and re-raises it as a
    `CheckpointIntegrityError` with `reason_code="sink_mismatch"` -- this
    class exists so a sink implementation has something concrete to raise
    without importing this module's own service-level exception."""


@dataclasses.dataclass(frozen=True)
class SinkReceipt:
    """What a sink hands back once it has durably accepted one checkpoint identity."""

    receipt_digest: str
    receipt_signature: str
    accepted_at: datetime.datetime


class CheckpointSink(Protocol):
    """Where a checkpoint's identity actually gets acknowledged outside
    this database. See the module docstring for why no production
    implementation ships with this task."""

    async def append(
        self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int, head_digest: str
    ) -> SinkReceipt:
        """Idempotent by `{deployment_id, revision_id, sequence}`: an exact
        duplicate (same identity, same `head_digest`) returns the original
        receipt; the same identity with a different `head_digest` raises
        `CheckpointSinkIdentityConflict`."""

    async def receipt_for(self, *, deployment_id: str, revision_id: uuid.UUID, sequence: int) -> SinkReceipt | None:
        """The receipt the sink already holds for this identity, or `None`
        if it has never seen it -- used by `verify_against_sink`, never by
        `export_checkpoint` itself."""

    async def latest_sequence(self, *, deployment_id: str, revision_id: uuid.UUID) -> int | None:
        """The highest sequence the sink has ever acknowledged for this
        `{deployment_id, revision_id}`, or `None` if it has acknowledged
        none -- used by `verify_against_sink` to detect a local chain that
        no longer reaches a sequence the sink already durably holds."""


class CheckpointExportOutcome(enum.StrEnum):
    """`export_checkpoint`'s bounded result -- a caller (the worker, a
    test) branches on this rather than inferring the outcome from what did
    or did not raise."""

    EXPORTED = "exported"
    ALREADY_EXPORTED = "already_exported"
    SINK_UNAVAILABLE = "sink_unavailable"


class CheckpointExportService:
    """Gets one pending checkpoint acknowledged, and can re-verify a
    revision's exported checkpoints against the sink's own record.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        sink: CheckpointSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._sink = sink

    async def export_checkpoint(self, checkpoint_id: uuid.UUID) -> CheckpointExportOutcome:
        """Get *checkpoint_id* acknowledged by the sink, and record that
        acknowledgment durably. See the module docstring for the two-
        transaction shape and why.
        """
        async with self._session_factory() as session:
            checkpoint = await queries.load_checkpoint(session, checkpoint_id)
        if checkpoint is None:
            raise NotFoundError(f"operational chain checkpoint {checkpoint_id} not found")
        if checkpoint.exported_at is not None:
            return CheckpointExportOutcome.ALREADY_EXPORTED
        if self._sink is None:
            # The honest "not configured" outcome -- see the module
            # docstring. The checkpoint stays pending; nothing here
            # pretends an acknowledgment happened.
            return CheckpointExportOutcome.SINK_UNAVAILABLE

        now = self._clock.now()
        try:
            receipt = await self._sink.append(
                deployment_id=checkpoint.deployment_id,
                revision_id=checkpoint.revision_id,
                sequence=checkpoint.sequence,
                head_digest=checkpoint.head_digest,
            )
        except CheckpointSinkIdentityConflict as exc:
            raise CheckpointIntegrityError(
                f"checkpoint {checkpoint_id} (revision {checkpoint.revision_id}, sequence "
                f"{checkpoint.sequence}) disagrees with what the sink already holds for this identity",
                reason_code="sink_mismatch",
            ) from exc
        except Exception as exc:  # a sink implementation's own failure -- recorded below, then re-raised typed
            async with self._session_factory() as session, session.begin():
                await queries.record_export_failure(
                    session, checkpoint_id=checkpoint_id, error_code=ERROR_SINK_FAILED, attempted_at=now
                )
            raise CheckpointIntegrityError(
                f"checkpoint {checkpoint_id} export failed: {exc}", reason_code="sink_failed"
            ) from exc

        async with self._session_factory() as session, session.begin():
            applied = await queries.mark_exported(
                session,
                checkpoint_id=checkpoint_id,
                exported_at=now,
                sink_receipt_digest=receipt.receipt_digest,
                sink_receipt_signature=receipt.receipt_signature,
            )
        # `applied is False` means a concurrent pass already recorded this
        # checkpoint's receipt between our read and this update -- the
        # sink's own idempotency means the receipt we just got back is the
        # same one already stored, so this is a duplicate no-op, not an
        # error.
        return CheckpointExportOutcome.EXPORTED if applied else CheckpointExportOutcome.ALREADY_EXPORTED

    async def verify_against_sink(self, session: AsyncSession, revision_id: uuid.UUID) -> None:
        """Compare every durable local checkpoint for *revision_id*
        against what the sink independently holds. Raises
        `CheckpointIntegrityError` on the first disagreement.

        Not on the export path -- this is the auditor's operation, the
        checkpoint half of what `OperationalChainService.verify_chain` is
        for the event chain itself. A later task's `RevisionIntegrityService.
        assess` is expected to call something that ultimately calls this,
        the same non-goal `verify_chain` states.
        """
        if self._sink is None:
            raise CheckpointIntegrityError(
                f"cannot verify revision {revision_id} against a sink: none is configured on this deployment",
                reason_code="missing_receipt",
            )

        rows = await _load_exported(session, revision_id)
        deployment_ids = {row.deployment_id for row in rows}
        for deployment_id in deployment_ids:
            local_max = max(row.sequence for row in rows if row.deployment_id == deployment_id)
            sink_latest = await self._sink.latest_sequence(deployment_id=deployment_id, revision_id=revision_id)
            if sink_latest is not None and sink_latest > local_max:
                raise CheckpointIntegrityError(
                    f"revision {revision_id} local chain (deployment {deployment_id!r}) reaches sequence "
                    f"{local_max}, but the sink has already acknowledged sequence {sink_latest} -- the local "
                    "chain has rolled back a durably-checkpointed suffix",
                    reason_code="suffix_rollback",
                )

        for row in rows:
            receipt = await self._sink.receipt_for(
                deployment_id=row.deployment_id, revision_id=revision_id, sequence=row.sequence
            )
            if receipt is None:
                raise CheckpointIntegrityError(
                    f"revision {revision_id} sequence {row.sequence} is recorded locally as durable but the "
                    "sink holds no receipt for it",
                    reason_code="missing_receipt",
                )
            if receipt.receipt_digest != row.sink_receipt_digest:
                raise CheckpointIntegrityError(
                    f"revision {revision_id} sequence {row.sequence} local receipt digest disagrees with the "
                    "sink's own record",
                    reason_code="sink_mismatch",
                )


async def _load_exported(session: AsyncSession, revision_id: uuid.UUID) -> list[Row[Any]]:
    result = await session.execute(
        text(
            "SELECT deployment_id, sequence, sink_receipt_digest FROM arc_operational_chain_checkpoints "
            "WHERE revision_id = :rid AND exported_at IS NOT NULL"
        ),
        {"rid": revision_id},
    )
    return list(result)


__all__ = [
    "ERROR_SINK_FAILED",
    "ERROR_SINK_UNAVAILABLE",
    "CheckpointExportOutcome",
    "CheckpointExportService",
    "CheckpointIntegrityError",
    "CheckpointSink",
    "CheckpointSinkIdentityConflict",
    "SinkReceipt",
]
