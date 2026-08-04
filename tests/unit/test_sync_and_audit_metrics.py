"""Sync-run duration and audit-write success — the two non-request metric
families the shipped dashboard already queries.

Both panels existed before any code emitted these, so both have been drawing
against nothing. The audit panel is the worse of the two: it could only ever
draw the failure counter, so it showed failures with no denominator, and a
failure count alone cannot answer "is that rate bad".
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from registry import metrics
from registry.api import audit
from registry.types import TenantContext


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ---------------------------------------------------------------------------
# Sync runs
# ---------------------------------------------------------------------------


def _session_factory(run: object | None) -> MagicMock:
    session = AsyncMock()
    session.get = AsyncMock(return_value=run)
    session.commit = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.mark.asyncio
async def test_a_finished_sync_run_is_observed_once() -> None:
    from sync.runner import _finish_run

    run = MagicMock()
    before = _sample("sync_run_duration_seconds_count")

    await _finish_run(
        _session_factory(run),
        uuid.uuid4(),
        status="succeeded",
        artifact_count=3,
        duration_s=42,
        error_summary=None,
    )

    assert _sample("sync_run_duration_seconds_count") == before + 1
    assert _sample("sync_run_duration_seconds_sum") >= 42


@pytest.mark.asyncio
async def test_a_failed_sync_run_is_observed_too() -> None:
    """Duration is recorded for every terminal status, not only success.

    A run that fails after twenty minutes is the one an operator most wants on
    the latency panel; recording only successes would make the graph look
    healthiest exactly when it is not.
    """
    from sync.runner import _finish_run

    before = _sample("sync_run_duration_seconds_count")
    await _finish_run(
        _session_factory(MagicMock()),
        uuid.uuid4(),
        status="failed",
        artifact_count=0,
        duration_s=7,
        error_summary="connector exploded",
    )
    assert _sample("sync_run_duration_seconds_count") == before + 1


@pytest.mark.asyncio
async def test_a_missing_run_row_records_nothing() -> None:
    # The early return means no run was finished, so there is no duration to
    # report. Observing here would inject a fabricated measurement.
    from sync.runner import _finish_run

    before = _sample("sync_run_duration_seconds_count")
    await _finish_run(
        _session_factory(None),
        uuid.uuid4(),
        status="succeeded",
        artifact_count=0,
        duration_s=5,
        error_summary=None,
    )
    assert _sample("sync_run_duration_seconds_count") == before


def test_the_metric_and_the_stored_column_cannot_disagree() -> None:
    # Both come from the caller's single elapsed count. A second clock started
    # inside the observer would drift from the value written to sync_runs, and
    # the dashboard and the table would then tell different stories.
    import inspect

    from sync import runner

    source = inspect.getsource(runner._finish_run)  # noqa: SLF001
    assert "observe_sync_run(seconds=float(duration_s))" in source
    assert "monotonic" not in source


# ---------------------------------------------------------------------------
# Audit writes
# ---------------------------------------------------------------------------


def _audit_session_factory(*, fail: bool = False) -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=None)
    begin.__aexit__ = AsyncMock(
        side_effect=RuntimeError("commit failed") if fail else AsyncMock(return_value=False)
    )
    session.begin = MagicMock(return_value=begin)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=frozenset({"admin"}))


@pytest.mark.asyncio
async def test_a_successful_audit_write_is_counted() -> None:
    before = _sample("catalog_audit_writes_total")

    await audit.emit(
        _audit_session_factory(),
        clock=MagicMock(now=MagicMock(return_value=None)),
        ctx=_ctx(),
        action="capability.create",
        target_type="capability",
        target_id=uuid.uuid4(),
    )

    assert _sample("catalog_audit_writes_total") == before + 1


@pytest.mark.asyncio
async def test_a_failed_audit_write_counts_only_the_failure() -> None:
    """The two counters must never both move for one write.

    They are read as a ratio. A write counted as both a success and a failure
    would put the success rate above what actually happened, in the one metric
    whose whole job is to say how much of the compliance record is missing.
    """
    before_ok = _sample("catalog_audit_writes_total")
    before_fail = _sample("catalog_audit_write_failures_total")

    await audit.emit(
        _audit_session_factory(fail=True),
        clock=MagicMock(now=MagicMock(return_value=None)),
        ctx=_ctx(),
        action="capability.create",
        target_type="capability",
        target_id=uuid.uuid4(),
    )

    assert _sample("catalog_audit_writes_total") == before_ok
    assert _sample("catalog_audit_write_failures_total") == before_fail + 1


def test_neither_family_carries_an_identity_label() -> None:
    # A sync source or a tenant here would grow the series count with adoption
    # rather than with code changes.
    for metric in (metrics.SYNC_RUN_DURATION_SECONDS, metrics.AUDIT_WRITES_TOTAL):
        assert not getattr(metric, "_labelnames", ())
