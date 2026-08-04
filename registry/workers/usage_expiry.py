"""Deletes usage events past their retention boundary.

A hard delete, unlike every other expiry in this system. The session-event and
workspace sweeps soft-invalidate because their rows are addressable for audit
afterwards; these rows are not evidence and are declared non-authoritative, so
there is nothing to preserve and a `t_invalidated_at` column would only keep
personal data in the table while pretending it was gone.

**Retention is the reason this table is allowed to hold identity at all.** The
aggregate answers survive expiry because the rollups are actor-free and kept
indefinitely, so deleting raw rows costs nothing analytically and removes the
liability. If this worker stops running, the trade collapses and the table becomes
an unbounded personal-data store — which is why the requirement says it is never
deferred.

**Batched, bounded, and loud when it cannot keep up.** Same shape as
`memory_expiry`: independently committed batches so a large backlog is cleared over
several passes rather than in one long transaction, a ceiling on batches per run,
and a warning when the ceiling is what stopped it. Hitting the ceiling means ingest
is outpacing the sweep, which an operator should hear rather than discover.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.metrics import observe_worker_run
from registry.types import Clock, SystemClock

__all__ = [
    "MAX_RETENTION_DAYS",
    "MIN_RETENTION_DAYS",
    "UsageExpiryResult",
    "UsageExpiryWorker",
    "validate_retention_days",
]

_log = logging.getLogger(__name__)

# Rows per batch. Larger than the session-event sweep because this is a delete on a
# partitioned table with no index maintenance to speak of, and the rows are narrow.
_BATCH_SIZE = 5000

_MAX_BATCHES = 50

#: The band the requirement fixes. Below thirty days the adoption questions this
#: table exists to answer — repeat-actor rate over weeks, month-on-month growth —
#: stop being computable from raw rows at all. Above a hundred and eighty the
#: retention liability grows without the analysis improving, because anything older
#: is answered from the rollups anyway.
MIN_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 180


def validate_retention_days(days: int) -> int:
    """Return `days`, or raise if it is outside the permitted band.

    Raises rather than clamping. A deployment that asked for a year and silently
    got a hundred and eighty days would believe it had a year of raw history, and
    would find out when a query returned less than it should — at which point the
    data is already gone.
    """
    if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        msg = (
            f"usage retention of {days} days is outside the permitted "
            f"{MIN_RETENTION_DAYS}-{MAX_RETENTION_DAYS} day band"
        )
        raise ValueError(msg)
    return days


@dataclasses.dataclass(frozen=True)
class UsageExpiryResult:
    deleted_count: int
    batches: int
    #: True when the run stopped on the batch ceiling rather than because it ran
    #: out of work. The first means a backlog is outpacing the schedule; the second
    #: means everything is current.
    truncated: bool
    cutoff: datetime.datetime
    ran_at: datetime.datetime


class UsageExpiryWorker:
    """Sweeps usage events older than the retention boundary out of existence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        retention_days: int,
        clock: Clock | None = None,
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._retention_days = validate_retention_days(retention_days)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._batch_size = batch_size

    async def run(self) -> UsageExpiryResult:
        """Timed wrapper. The work is in ``_run_inner``.

        A background worker's failure is otherwise invisible: nothing is on a
        request path, so nobody receives an error and the only symptom is work
        quietly not happening — here, a table that grows past its retention
        boundary while every dashboard still looks right.
        """
        with observe_worker_run("usage_expiry"):
            return await self._run_inner()

    async def _run_inner(self) -> UsageExpiryResult:
        now = self._clock.now()
        cutoff = now - datetime.timedelta(days=self._retention_days)
        total = 0
        batches = 0

        while batches < _MAX_BATCHES:
            deleted = await self._delete_batch(cutoff)
            if deleted == 0:
                break
            total += deleted
            batches += 1

        truncated = batches >= _MAX_BATCHES
        if truncated:
            _log.warning(
                "usage_expiry.truncated: stopped at %d batches with rows still past the "
                "%d-day boundary; the retention sweep is not keeping up with ingest",
                batches,
                self._retention_days,
            )
        return UsageExpiryResult(deleted_count=total, batches=batches, truncated=truncated, cutoff=cutoff, ran_at=now)

    async def _delete_batch(self, cutoff: datetime.datetime) -> int:
        """One bounded, independently committed delete.

        Scoped by primary key from a subquery because Postgres does not accept a
        `LIMIT` on `DELETE` directly, and `FOR UPDATE SKIP LOCKED` so two
        overlapping runs cannot block on each other — the same reasoning the
        session-event sweep records.
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                text(
                    "DELETE FROM usage_events "
                    "WHERE (event_id, occurred_at) IN ("
                    "  SELECT event_id, occurred_at FROM usage_events "
                    "   WHERE occurred_at < :cutoff "
                    "   ORDER BY occurred_at "
                    "   LIMIT :limit FOR UPDATE SKIP LOCKED"
                    ")"
                ),
                {"cutoff": cutoff, "limit": self._batch_size},
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]
