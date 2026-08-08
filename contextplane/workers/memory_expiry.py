"""Soft-invalidate session events whose retention window has passed.

Each event carries an `expires_at` set when it was written, from its tenant's
configured window. This worker sweeps past-deadline events out of the default
read path in batches.

**Soft, not physical.** An expired event leaves replay and stops being
extraction-eligible, but stays in the table and stays addressable by id. The
audit trail has to keep answering "what was here" for a moment somebody may
later ask about. Physical erasure is a separate, deliberate act with its own
justification -- a right-to-be-forgotten request -- and is not something a
scheduled sweep should do on a timer.

**The deadline is read, not computed.** `expires_at` was materialised at write
time, so shortening a tenant's window does not retroactively expire events
recorded under the old one, and this scan is a plain index range rather than
an expression over every row.

Idempotent by construction: the `invalidated_at IS NULL` filter excludes rows a
previous pass already handled, so a crash mid-run resumes cleanly and a double
run is harmless.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.metrics import observe_worker_run
from contextplane.service.memory.session_events import REASON_RETENTION
from contextplane.types import Clock, SystemClock

_log = logging.getLogger(__name__)

# Bounded so one pass cannot hold locks across an unbounded set. A backlog is
# cleared over several passes rather than one long transaction.
_BATCH_SIZE = 1000

# A backlog large enough to need this many batches means the schedule is not
# keeping up, which an operator should hear about rather than discover.
_MAX_BATCHES = 50


@dataclasses.dataclass(frozen=True)
class MemoryExpiryResult:
    expired_count: int
    batches: int
    # True when the worker stopped on the batch ceiling rather than because it
    # ran out of work. The distinction matters: the first means a backlog is
    # outpacing the schedule, the second means everything is current.
    truncated: bool
    ran_at: datetime.datetime


class MemoryExpiryWorker:
    """Sweeps expired session events out of the default read path."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._batch_size = batch_size

    async def run(self) -> MemoryExpiryResult:
        """Timed wrapper. The work itself is in ``_run_inner``.

        Background workers are the one place a failure is otherwise invisible:
        nothing is on a request path, so nobody receives an error and the only
        symptom is work quietly not happening.
        """
        with observe_worker_run("memory_expiry"):
            return await self._run_inner()

    async def _run_inner(self) -> MemoryExpiryResult:
        now = self._clock.now()
        total = 0
        batches = 0

        while batches < _MAX_BATCHES:
            invalidated = await self._expire_batch(now)
            if invalidated == 0:
                break
            total += invalidated
            batches += 1

        truncated = batches >= _MAX_BATCHES
        if truncated:
            _log.warning(
                "memory_expiry.truncated: stopped at %d batches with work remaining; "
                "the retention sweep is not keeping up with ingest",
                batches,
            )
        return MemoryExpiryResult(expired_count=total, batches=batches, truncated=truncated, ran_at=now)

    async def _expire_batch(self, now: datetime.datetime) -> int:
        """One bounded, independently committed batch.

        Scoped by primary key from a subquery rather than by `LIMIT` on the
        UPDATE directly, which Postgres does not accept -- and `FOR UPDATE
        SKIP LOCKED` so two overlapping runs cannot block on each other.
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE memory_session_events SET invalidated_at = :now, "
                    "  invalidated_reason = :reason "
                    "WHERE event_id IN ("
                    "  SELECT event_id FROM memory_session_events "
                    "   WHERE invalidated_at IS NULL AND expires_at <= :now "
                    "   ORDER BY expires_at LIMIT :limit FOR UPDATE SKIP LOCKED"
                    ")"
                ),
                {"now": now, "reason": REASON_RETENTION, "limit": self._batch_size},
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]


__all__ = ["MemoryExpiryResult", "MemoryExpiryWorker"]
