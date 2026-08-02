"""AuditDrainWorker -- moves `arc_audit_outbox` rows into `audit_log`.

`registry.arc.service.audit_outbox` explains why ARC writes an outbox row
instead of `audit_log` directly: an ARC write is one retryable transaction,
and an inline audit write inside it would either roll back with a retried
attempt (losing the record) or, on a separate connection, record an attempt
that never committed. The outbox makes the audit row atomic with the state
it describes. This worker is the other half of that design -- the *only*
process that ever writes `audit_log` on ARC's behalf. If anything else wrote
there too, ordering between the two paths would be undefined and the audit
log could show an effect before its cause.

Claiming with `FOR UPDATE SKIP LOCKED` and holding those locks for the whole
batch (rather than releasing them the moment rows are selected) is what
makes two drain workers running concurrently -- two processes, or one
process whose interval trigger overlaps a slow pass -- never work the same
row twice. A worker that instead released its claim early and relied on the
write being idempotent would still be correct, but it would also mean
"claim" doesn't actually mean anything; the SKIP LOCKED guarantee here is
real exclusivity, not just an eventually-consistent convergence.

Failure is per row, not per batch. A single sink write failing on one
outbox row must not cost the batch's other rows their progress, so each
write happens inside its own SAVEPOINT: a failure there rolls back only
that row's attempted insert, and the batch's outer transaction still
commits every other row's outcome. The failing row keeps `drained_at NULL`,
gets `attempts` incremented, and records a bounded code -- never the raw
exception text, which could carry a connection string, a stack frame, or
anything else a driver decided to put in a message. A row that keeps
failing past `max_attempts` stops being claimed at all: it is never
deleted and never silently reprocessed forever, it just drops out of the
active query, which is what turns "this one row is poisoned" into
something an operator can find (`attempts`, `last_error_code`, and
`last_attempt_at` are all still sitting on the row) instead of something
that quietly stalls every row behind it.

Redelivery is safe because the audit row's identity is not invented per
attempt -- it is the outbox row's own identity, reused: `audit_id =
outbox_id`, `ts = created_at`. A crash between the `audit_log` insert
landing and `drained_at` being set leaves the row eligible for another
pass, and that pass inserts the exact same primary key a second time.
`ON CONFLICT DO NOTHING` absorbs it, so at-least-once redelivery can never
produce two audit rows for one event.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import uuid
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc import metrics
from registry.types import Clock, SystemClock

_log = logging.getLogger(__name__)

#: Rows claimed per pass. Bounds how long one call holds its row locks --
#: not a correctness requirement, an operational one: a batch of a million
#: rows would hold FOR UPDATE locks against the outbox table for however
#: long draining a million rows takes.
DEFAULT_LIMIT = 500

#: Attempts before a row stops being claimed. Owned by the worker rather
#: than the schema, so it can be tuned per deployment without a migration --
#: see the index built for exactly this purpose in the migration that
#: creates `arc_audit_outbox`.
DEFAULT_MAX_ATTEMPTS = 8

#: `arc_audit_outbox.last_error_code` is bounded to 64 characters precisely
#: so it can never become a place to park whatever a driver happened to
#: raise. These are the only two values this worker ever writes there.
ERROR_SINK_WRITE_FAILED = "sink_write_failed"
ERROR_UNEXPECTED = "unexpected_error"

#: `audit_log.target_type` for every row this worker writes. A drained
#: row's payload shape is producer-defined -- new ARC event types add new
#: keys without this worker's knowledge -- so it deliberately never parses
#: the payload looking for "the" entity id. `outbox_id` doubles as
#: `target_id`, which keeps every drained row pointing at something
#: concrete and traceable instead of a placeholder.
AUDIT_LOG_TARGET_TYPE = "arc_audit_outbox"


class AuditSinkError(Exception):
    """A sink failed to write a row. Carries the bounded code the worker records verbatim.

    Raised by a sink, not by the worker -- the worker only ever repeats the
    code back onto the outbox row, it does not invent one. Any code passed
    here must already satisfy the same bound the schema enforces, so a
    mistake fails loudly in the sink rather than as an opaque CHECK
    violation later.
    """

    def __init__(self, code: str) -> None:
        if not 1 <= len(code) <= 64:
            msg = f"AuditSinkError code must be 1-64 characters, got {len(code)}"
            raise ValueError(msg)
        super().__init__(code)
        self.code = code


class AuditSink(Protocol):
    """Where a claimed row's audit fact actually lands.

    The production sink is the one INSERT into `audit_log` that makes this
    worker ARC's sole writer there -- see `_PostgresAuditSink`. The seam
    exists for tests, not for flexibility in production: there is no
    legitimate outbox row that makes a real `audit_log` insert fail (the
    payload was already size-checked at emit time, and every other column
    is worker-controlled), so exercising the retry path honestly requires a
    way to inject that failure rather than fabricate a constraint violation
    the schema never actually produces.
    """

    async def write(
        self,
        session: AsyncSession,
        *,
        outbox_id: uuid.UUID,
        tenant_id: uuid.UUID,
        event_type: str,
        event_payload: dict[str, Any],
        created_at: datetime.datetime,
    ) -> None: ...


class _PostgresAuditSink:
    """The real sink. One INSERT, keyed by the outbox row's own identity."""

    async def write(
        self,
        session: AsyncSession,
        *,
        outbox_id: uuid.UUID,
        tenant_id: uuid.UUID,
        event_type: str,
        event_payload: dict[str, Any],
        created_at: datetime.datetime,
    ) -> None:
        # Canonical JSON, matching the outbox emit side: two identical events
        # must serialize identically, or ON CONFLICT-based dedup and any
        # downstream comparison have nothing reliable to work against.
        encoded = json.dumps(event_payload, sort_keys=True, separators=(",", ":"), default=str)
        try:
            await session.execute(
                text(
                    "INSERT INTO audit_log ("
                    "  audit_id, tenant_id, actor_id, action, target_type, target_id,"
                    "  before_jsonb, after_jsonb, ts"
                    ") VALUES ("
                    "  :audit_id, :tenant_id, NULL, :action, :target_type, :target_id,"
                    "  NULL, CAST(:after_jsonb AS JSONB), :ts"
                    ") ON CONFLICT (audit_id, ts) DO NOTHING"
                ),
                {
                    "audit_id": outbox_id,
                    "tenant_id": tenant_id,
                    "action": event_type,
                    "target_type": AUDIT_LOG_TARGET_TYPE,
                    "target_id": outbox_id,
                    "after_jsonb": encoded,
                    "ts": created_at,
                },
            )
        except SQLAlchemyError as exc:
            raise AuditSinkError(ERROR_SINK_WRITE_FAILED) from exc


@dataclasses.dataclass(frozen=True)
class DrainResult:
    """Outcome of one bounded pass -- what a scheduler or metrics layer reports.

    `claimed` is `drained + failed`, kept as its own field so a caller does
    not need to add the other two back together to know whether this pass
    found anything to do at all.
    """

    claimed: int
    drained: int
    failed: int


class AuditDrainWorker:
    """Drains `arc_audit_outbox` into `audit_log`, one bounded pass per call.

    Parameters
    ----------
    session_factory:
        Async session factory wired to the Postgres database.
    clock:
        Injectable clock. Defaults to the real UTC wall-clock when `None`.
    limit:
        Maximum rows claimed per `run_once()` call.
    max_attempts:
        Attempts a row gets before it stops being claimed. Left undrained,
        not deleted, so an operator can still find and fix it.
    sink:
        Where a claimed row's audit fact is written. Defaults to the real
        `audit_log` writer; tests substitute one that fails on command.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sink: AuditSink | None = None,
    ) -> None:
        if limit < 1:
            msg = f"limit must be at least 1, got {limit}"
            raise ValueError(msg)
        if max_attempts < 1:
            msg = f"max_attempts must be at least 1, got {max_attempts}"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._limit = limit
        self._max_attempts = max_attempts
        self._sink: AuditSink = sink if sink is not None else _PostgresAuditSink()

    async def run_once(self) -> DrainResult:
        """Claim and drain up to `limit` undrained rows in one transaction.

        One outer transaction holds every claimed row's lock for the whole
        pass; each row's write happens in its own SAVEPOINT so one failure
        does not undo its siblings' progress. Nothing here is visible to
        another worker until this method returns -- a crash mid-pass leaves
        every row exactly as it was before this call started.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            rows = await self._claim(session)
            drained = 0
            failed = 0
            for row in rows:
                outbox_id: uuid.UUID = row["outbox_id"]
                try:
                    async with session.begin_nested():
                        await self._sink.write(
                            session,
                            outbox_id=outbox_id,
                            tenant_id=row["tenant_id"],
                            event_type=row["event_type"],
                            event_payload=row["event_payload"],
                            created_at=row["created_at"],
                        )
                    await session.execute(
                        text(
                            "UPDATE arc_audit_outbox SET drained_at = :now, last_error_code = NULL "
                            "WHERE outbox_id = :id"
                        ),
                        {"now": now, "id": outbox_id},
                    )
                    drained += 1
                except Exception as exc:
                    code = exc.code if isinstance(exc, AuditSinkError) else ERROR_UNEXPECTED
                    _log.warning(
                        "arc_audit_drain: failed outbox_id=%s code=%s",
                        outbox_id,
                        code,
                        exc_info=exc,
                    )
                    await session.execute(
                        text(
                            "UPDATE arc_audit_outbox SET attempts = attempts + 1, last_error_code = :code, "
                            "  last_attempt_at = :now WHERE outbox_id = :id"
                        ),
                        {"code": code, "now": now, "id": outbox_id},
                    )
                    failed += 1

        result = DrainResult(claimed=len(rows), drained=drained, failed=failed)
        if result.claimed:
            _log.info(
                "arc_audit_drain: claimed=%d drained=%d failed=%d",
                result.claimed,
                result.drained,
                result.failed,
            )
        await self._report_depth()
        return result

    async def _claim(self, session: AsyncSession) -> list[dict[str, Any]]:
        """Lock up to `limit` undrained, still-retryable rows, oldest first.

        `attempts < max_attempts` is the ceiling: once a row has failed that
        many times it simply stops matching this query, which is what
        drops a poison row out of the active queue without deleting or
        reprocessing it.
        """
        result = await session.execute(
            text(
                "SELECT outbox_id, tenant_id, event_type, event_payload, created_at "
                "FROM arc_audit_outbox "
                "WHERE drained_at IS NULL AND attempts < :max_attempts "
                "ORDER BY created_at "
                "LIMIT :limit "
                "FOR UPDATE SKIP LOCKED"
            ),
            {"max_attempts": self._max_attempts, "limit": self._limit},
        )
        return [dict(row) for row in result.mappings().all()]

    async def _report_depth(self) -> None:
        """Publish the current undrained-row count to the ARC metrics gauge.

        A fresh read after the pass commits, not a count carried over from
        the claim -- the gauge answers "how far behind is the drain worker
        right now", including rows this pass never touched (past the
        `limit`) and rows other producers wrote while this pass ran.
        """
        async with self._session_factory() as session:
            depth = (
                await session.execute(text("SELECT count(*) FROM arc_audit_outbox WHERE drained_at IS NULL"))
            ).scalar_one()
        metrics.set_audit_outbox_depth(depth)


__all__ = [
    "AUDIT_LOG_TARGET_TYPE",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_ATTEMPTS",
    "ERROR_SINK_WRITE_FAILED",
    "ERROR_UNEXPECTED",
    "AuditDrainWorker",
    "AuditSink",
    "AuditSinkError",
    "DrainResult",
]
