"""Unit tests for UsageRollupWorker.

`roll_up_day` (imported from `contextplane.usage.rollups`) is the worker's only
collaborator, and it is a module-level function rather than a constructor-
injected dependency, so these tests monkeypatch it on the worker module
instead of using an SQL-string-keyed session router -- `UsageRollupWorker`
itself never touches `session.execute` directly; it only decides which two
days to ask for and hands the session factory through unchanged.

The real entry point is `run()`, not `run_once`: there is no per-item
(here, per-day) isolation. `_run_inner` awaits `roll_up_day` for yesterday
and then today in a plain loop with no try/except, so a failure on the first
day stops the second from ever running and propagates rather than being
swallowed -- which matters here specifically, per the module's own
docstring, because a rollup that silently stops running looks identical to a
healthy service with flat dashboards.

Coverage:
- The day window is the clock's *UTC* date, not the clock instant's own
  possibly-offset date -- proven with a clock parked after UTC midnight but
  still the previous evening under a positive offset.
- `run` passes the same session-factory object through to both days' calls.
- `run` returns `(yesterday, today)` in that order with each day's own
  result.
- A first-day failure prevents the second day from running and is not
  swallowed.
- The default-clock branch (`clock=None` falls back to `SystemClock`).
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.types import SystemClock
from contextplane.usage.rollups import RollupResult
from contextplane.workers import usage_rollup as rollup_module
from contextplane.workers.usage_rollup import UsageRollupResult, UsageRollupWorker
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


def _result(day: datetime.date, rows: int = 0) -> RollupResult:
    return RollupResult(day=day, tenant_day_rows=rows, capability_day_rows=rows, tool_day_rows=rows)


class _FixedOffsetClock:
    """A clock that returns exactly the instant given, with no UTC
    normalisation -- unlike `FakeClock`, which converts to UTC at
    construction and would silently hide the bug this test exists to catch.
    """

    def __init__(self, instant: datetime.datetime) -> None:
        self._instant = instant

    def now(self) -> datetime.datetime:
        return self._instant


# ---------------------------------------------------------------------------
# Day-window selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_rolls_up_yesterday_then_today_using_the_clocks_utc_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """01:00 at UTC+5 is 20:00 the previous day in UTC. The naive calendar
    date embedded in the offset-aware instant names the wrong day; only the
    UTC-converted date names the day this worker owes."""
    instant = datetime.datetime(2026, 8, 6, 1, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5)))
    utc_today = datetime.date(2026, 8, 5)
    utc_yesterday = datetime.date(2026, 8, 4)

    seen_days: list[datetime.date] = []

    async def _roll_up_day(session_factory: Any, day: datetime.date) -> RollupResult:
        seen_days.append(day)
        return _result(day)

    monkeypatch.setattr(rollup_module, "roll_up_day", AsyncMock(side_effect=_roll_up_day))

    worker = UsageRollupWorker(MagicMock(), clock=_FixedOffsetClock(instant))
    await worker.run()

    assert seen_days == [utc_yesterday, utc_today]


@pytest.mark.asyncio
async def test_run_passes_the_same_session_factory_object_to_both_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = MagicMock(name="session_factory")
    seen_factories: list[Any] = []

    async def _roll_up_day(factory: Any, day: datetime.date) -> RollupResult:
        seen_factories.append(factory)
        return _result(day)

    monkeypatch.setattr(rollup_module, "roll_up_day", AsyncMock(side_effect=_roll_up_day))

    worker = UsageRollupWorker(session_factory, clock=FakeClock(_NOW))
    await worker.run()

    assert seen_factories == [session_factory, session_factory]


@pytest.mark.asyncio
async def test_run_returns_results_in_yesterday_then_today_order(monkeypatch: pytest.MonkeyPatch) -> None:
    yesterday = datetime.date(2026, 8, 4)
    today = datetime.date(2026, 8, 5)
    result_yesterday = _result(yesterday, rows=3)
    result_today = _result(today, rows=9)
    results_by_day = {yesterday: result_yesterday, today: result_today}

    async def _roll_up_day(factory: Any, day: datetime.date) -> RollupResult:
        return results_by_day[day]

    monkeypatch.setattr(rollup_module, "roll_up_day", AsyncMock(side_effect=_roll_up_day))

    worker = UsageRollupWorker(MagicMock(), clock=FakeClock(_NOW))
    result = await worker.run()

    assert result == UsageRollupResult(days=(result_yesterday, result_today), ran_at=_NOW)


# ---------------------------------------------------------------------------
# No per-day isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_first_day_propagates_and_never_attempts_the_second_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no per-day try/except: `_run_inner` awaits yesterday, then
    today, in a plain loop. A failure on the first must stop the second from
    ever running, and must surface rather than being swallowed."""
    roll_up_day = AsyncMock(side_effect=RuntimeError("usage_events unavailable"))
    monkeypatch.setattr(rollup_module, "roll_up_day", roll_up_day)

    worker = UsageRollupWorker(MagicMock(), clock=FakeClock(_NOW))

    with pytest.raises(RuntimeError, match="usage_events unavailable"):
        await worker.run()

    assert roll_up_day.await_count == 1


# ---------------------------------------------------------------------------
# Default clock
# ---------------------------------------------------------------------------


def test_default_clock_is_system_clock_when_none_given() -> None:
    worker = UsageRollupWorker(MagicMock())
    assert isinstance(worker._clock, SystemClock)
