"""Unit tests for MemoryExpiryWorker.

All DB interaction is mocked at `session.execute` via an SQL-string-keyed
router -- no Postgres required, mirroring `test_promotion_sweep_worker.py`'s
mock-factory pattern. There is only one query in this worker, so the router
has a single route; a test can still assert on the exact predicate and on
the params each batch is called with.

The real entry point is `run()`, not `run_once` -- there is no per-item
(here, per-batch) isolation: `_run_inner` loops calling `_expire_batch`
directly, with no try/except around the call, so a batch that raises aborts
the whole run rather than being counted and skipped.

Coverage:
- `_expire_batch`: the exact predicate the sweep scans on (unexpired, ordered
  by deadline, row-locked, bounded by `batch_size`).
- `run`: zero matching rows, stopping after the first empty batch, expired
  counts accumulating across several nonzero batches, hitting the batch
  ceiling when the backlog never runs dry, and a mid-run failure propagating
  rather than being isolated.
- The default-clock branch (`clock=None` falls back to `SystemClock`).
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.service.memory.session_events import REASON_RETENTION
from contextplane.types import SystemClock
from contextplane.workers.memory_expiry import MemoryExpiryResult, MemoryExpiryWorker
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _make_session_factory(outcomes: list[int | BaseException]) -> tuple[MagicMock, list[dict[str, Any]]]:
    """SQL-string-keyed AsyncMock session factory for the one query this
    worker runs.

    ``outcomes`` is popped one value per `_expire_batch` call, in order; a
    call past the end of the list returns rowcount 0 (no more work). An
    exception instance in the list is raised instead of returned, so a test
    can simulate a batch that fails partway through a sweep.
    """
    remaining = list(outcomes)
    calls: list[dict[str, Any]] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        if "UPDATE memory_session_events" not in sql:
            raise AssertionError(f"unexpected SQL in test session: {sql}")
        calls.append({"sql": sql, "params": params})
        outcome = remaining.pop(0) if remaining else 0
        if isinstance(outcome, BaseException):
            raise outcome
        result = MagicMock()
        result.rowcount = outcome
        return result

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, calls


# ---------------------------------------------------------------------------
# _expire_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_batch_query_scans_unexpired_ordered_locked_and_bounded() -> None:
    factory, calls = _make_session_factory([0])
    worker = MemoryExpiryWorker(factory, clock=FakeClock(_NOW), batch_size=250)

    await worker._expire_batch(_NOW)

    sql = calls[0]["sql"]
    assert "invalidated_at IS NULL" in sql
    assert "expires_at <= :now" in sql
    assert "ORDER BY expires_at LIMIT :limit" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    params = calls[0]["params"]
    assert params["now"] == _NOW
    assert params["reason"] == REASON_RETENTION
    assert params["limit"] == 250


# ---------------------------------------------------------------------------
# run: empty, single batch, accumulation, batch ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_zero_matching_rows_is_not_an_error() -> None:
    factory, calls = _make_session_factory([0])
    worker = MemoryExpiryWorker(factory, clock=FakeClock(_NOW))

    result = await worker.run()

    assert result == MemoryExpiryResult(expired_count=0, batches=0, truncated=False, ran_at=_NOW)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_stops_after_the_batch_that_returns_zero() -> None:
    factory, calls = _make_session_factory([5, 0])
    worker = MemoryExpiryWorker(factory, clock=FakeClock(_NOW))

    result = await worker.run()

    assert result.expired_count == 5
    assert result.batches == 1
    assert result.truncated is False
    assert len(calls) == 2  # the zero-row batch is what stopped it


@pytest.mark.asyncio
async def test_run_accumulates_expired_count_across_multiple_nonzero_batches() -> None:
    factory, calls = _make_session_factory([400, 300, 0])
    worker = MemoryExpiryWorker(factory, clock=FakeClock(_NOW))

    result = await worker.run()

    assert result.expired_count == 700
    assert result.batches == 2
    assert result.truncated is False
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_run_hits_the_batch_ceiling_when_every_batch_is_full() -> None:
    """Every batch returns work -- the sweep never sees a zero -- so the only
    thing that can stop it is the ceiling. This is the backlog-outpacing-the-
    schedule case the worker's own `truncated` flag exists to distinguish
    from 'everything is current'."""
    factory, calls = _make_session_factory([1] * 60)
    worker = MemoryExpiryWorker(factory, clock=FakeClock(_NOW))

    result = await worker.run()

    assert result.batches == 50
    assert result.expired_count == 50
    assert result.truncated is True
    assert len(calls) == 50  # the 51st through 60th never ran


@pytest.mark.asyncio
async def test_run_propagates_a_batch_failure_without_isolating_it() -> None:
    """There is no per-batch try/except in `_run_inner`: a failure partway
    through the sweep aborts the run and surfaces to the scheduler, rather
    than being counted and skipped the way a per-row worker would isolate a
    single bad row."""
    factory, calls = _make_session_factory([5, RuntimeError("boom")])
    worker = MemoryExpiryWorker(factory, clock=FakeClock(_NOW))

    with pytest.raises(RuntimeError, match="boom"):
        await worker.run()

    assert len(calls) == 2  # the first batch ran and committed; the second raised


# ---------------------------------------------------------------------------
# Default clock
# ---------------------------------------------------------------------------


def test_default_clock_is_system_clock_when_none_given() -> None:
    factory, _ = _make_session_factory([0])
    worker = MemoryExpiryWorker(factory)
    assert isinstance(worker._clock, SystemClock)
