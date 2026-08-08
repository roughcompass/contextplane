"""Rolls completed UTC days into the actor-free aggregates.

Runs hourly and rolls up **yesterday and today**. Two days rather than one, for two
different reasons that both matter:

*Yesterday* because a day is only complete once it is over, and the run that happens
at 00:30 UTC is the first one that can finish it. Re-rolling it on later passes is
free and idempotent, and it repairs a day that was rolled up while events were still
arriving late.

*Today* because a dashboard that shows nothing until tomorrow is a dashboard nobody
opens. The partial day is explicitly partial — it is recomputed on the next pass —
and the alternative, waiting for completeness, makes the whole surface useless on the
day anyone actually looks at it.

A completed day has to be queryable within six hours; hourly gives that with a wide
margin, and the cost of the margin is a few extra aggregate queries over one day of
rows.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.metrics import observe_worker_run
from contextplane.types import Clock, SystemClock
from contextplane.usage.rollups import RollupResult, roll_up_day

__all__ = ["UsageRollupResult", "UsageRollupWorker"]

_log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class UsageRollupResult:
    days: tuple[RollupResult, ...]
    ran_at: datetime.datetime


class UsageRollupWorker:
    """Recomputes the recent days' aggregates."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock: Clock = clock if clock is not None else SystemClock()

    async def run(self) -> UsageRollupResult:
        """Timed wrapper; the work is in ``_run_inner``.

        A rollup that stops running is the quietest failure in this subsystem: raw
        events keep arriving, retention keeps deleting them, and the aggregates
        silently stop advancing — so the dashboards go flat while the service is
        perfectly healthy, and the raw rows that would have explained it expire.
        """
        with observe_worker_run("usage_rollup"):
            return await self._run_inner()

    async def _run_inner(self) -> UsageRollupResult:
        now = self._clock.now()
        today = now.astimezone(datetime.UTC).date()
        yesterday = today - datetime.timedelta(days=1)

        results = []
        for day in (yesterday, today):
            results.append(await roll_up_day(self._session_factory, day))

        rows = sum(r.tenant_day_rows for r in results)
        if rows:
            _log.info("usage_rollup.run: days=%s tenant_day_rows=%d", [str(r.day) for r in results], rows)
        return UsageRollupResult(days=tuple(results), ran_at=now)
