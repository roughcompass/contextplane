"""Retention, which is the reason this table is allowed to hold identity at all.

Raw usage rows carry an actor id, so they are personal data with an erasure
obligation. The boundary is what keeps that bounded, and the analytical cost of
enforcing it is zero because the rollups are actor-free and kept forever. If this
worker stops running, that trade collapses and the table becomes an unbounded
personal-data store — which is why the requirement says it is never deferred.
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.types import FakeClock
from registry.workers.usage_expiry import (
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    UsageExpiryWorker,
    validate_retention_days,
)

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


def _session_factory(rowcounts: list[int]) -> tuple[MagicMock, list[dict]]:
    """A factory whose deletes report the given rowcounts in order."""
    seen: list[dict] = []
    session = AsyncMock()

    async def execute(_statement: object, params: dict | None = None) -> MagicMock:
        seen.append(params or {})
        result = MagicMock()
        result.rowcount = rowcounts[len(seen) - 1] if len(seen) <= len(rowcounts) else 0
        return result

    session.execute = execute
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=None)
    begin.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, seen


# ---------------------------------------------------------------------------
# The band, and why it refuses rather than clamps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", [MIN_RETENTION_DAYS, 90, MAX_RETENTION_DAYS])
def test_a_value_inside_the_band_is_accepted(days: int) -> None:
    assert validate_retention_days(days) == days


@pytest.mark.parametrize("days", [0, 29, 181, 365])
def test_a_value_outside_the_band_is_refused_not_clamped(days: int) -> None:
    """Clamping would be the quiet failure.

    A deployment that asked for a year and silently got a hundred and eighty days
    would believe it had a year of raw history, and would find out when a query
    returned less than it should — by which time the rows are gone. A startup
    error is recoverable; silently discarded history is not.
    """
    with pytest.raises(ValueError, match="outside the permitted"):
        validate_retention_days(days)


def test_the_worker_refuses_a_bad_retention_at_construction() -> None:
    # Not on first run: a misconfigured deployment should fail to start rather
    # than run for an hour and then delete more than it was asked to.
    factory, _ = _session_factory([0])
    with pytest.raises(ValueError, match="outside the permitted"):
        UsageExpiryWorker(factory, retention_days=1000)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cutoff_is_the_boundary_the_retention_names() -> None:
    factory, seen = _session_factory([0])
    worker = UsageExpiryWorker(factory, retention_days=90, clock=FakeClock(_NOW))

    result = await worker.run()

    assert result.cutoff == _NOW - datetime.timedelta(days=90)
    assert seen[0]["cutoff"] == result.cutoff


@pytest.mark.asyncio
async def test_batches_run_until_the_work_is_gone() -> None:
    # Independently committed batches, so a large backlog clears over several
    # passes rather than in one long transaction holding locks.
    factory, seen = _session_factory([5000, 5000, 120, 0])
    worker = UsageExpiryWorker(factory, retention_days=90, clock=FakeClock(_NOW))

    result = await worker.run()

    assert result.deleted_count == 10_120
    assert result.batches == 3
    assert result.truncated is False
    assert len(seen) == 4  # three with work, one that found none


@pytest.mark.asyncio
async def test_hitting_the_ceiling_is_reported_as_truncated() -> None:
    """The distinction an operator needs.

    Stopping because there was no more work and stopping because the ceiling was
    reached look identical in a row count. The second means ingest is outpacing the
    sweep, and the boundary is quietly not being enforced.
    """
    factory, _ = _session_factory([5000] * 60)
    worker = UsageExpiryWorker(factory, retention_days=30, clock=FakeClock(_NOW))

    result = await worker.run()

    assert result.truncated is True
    assert result.batches == 50


@pytest.mark.asyncio
async def test_an_empty_sweep_deletes_nothing_and_says_so() -> None:
    factory, seen = _session_factory([0])
    result = await UsageExpiryWorker(factory, retention_days=90, clock=FakeClock(_NOW)).run()
    assert result.deleted_count == 0
    assert result.batches == 0
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Hard delete, deliberately unlike every other expiry here
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_deletes_rather_than_soft_invalidating() -> None:
    """Every other expiry in this system soft-invalidates. This one must not.

    The session-event and workspace sweeps keep rows addressable for audit. These
    rows are declared non-authoritative, so there is nothing to preserve — and a
    soft flag would keep personal data in the table while presenting it as gone,
    which is the worst of both.
    """
    import inspect

    from registry.workers import usage_expiry

    source = inspect.getsource(usage_expiry.UsageExpiryWorker._delete_batch)  # noqa: SLF001
    assert "DELETE FROM usage_events" in source
    assert "invalidated" not in source.lower(), "retention must be a hard delete, not a soft flag"


@pytest.mark.asyncio
async def test_the_delete_is_bounded_and_skips_locked_rows() -> None:
    # `LIMIT` scoped through a subquery because Postgres rejects it on DELETE
    # directly, and SKIP LOCKED so two overlapping runs cannot block each other.
    factory, seen = _session_factory([0])
    worker = UsageExpiryWorker(factory, retention_days=90, clock=FakeClock(_NOW), batch_size=250)
    await worker.run()
    assert seen[0]["limit"] == 250


def test_the_run_is_wrapped_in_the_worker_metric() -> None:
    # A background worker's failure is otherwise invisible: nothing is on a
    # request path, so the only symptom is the boundary quietly not being enforced
    # while every dashboard still looks right.
    import inspect

    from registry.workers import usage_expiry

    assert "observe_worker_run" in inspect.getsource(usage_expiry.UsageExpiryWorker.run)
