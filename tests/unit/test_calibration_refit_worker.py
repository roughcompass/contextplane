"""Unit tests for CalibrationRefitWorker and the shared `refit_one` sequence.

All DB interaction for the worker's own discovery query is mocked at
`session.execute` via an SQL-string-keyed router -- no Postgres required,
mirroring `test_promotion_sweep_worker.py`'s mock-factory pattern.
`CalibrationService` is replaced with a lightweight mock so these tests
exercise only the worker's own wiring: strategy discovery, per-strategy
outcome bucketing (activated / stored-failed / below-minimum), and per-triple
failure isolation.

Coverage:
- `_candidate_strategies`: the exact predicate the worker scans on (a real
  self-report, a verdict that decided something, a non-null strategy_id).
- `run_once`: empty batch, an activated fit, a fit stored but not activated
  (missed the accuracy bound), a fit below the evaluation-set floor, and
  per-strategy failure isolation.
- `refit_one`: calls `load_observations -> fit -> publish` in that order with
  the triple it was given, and returns what `publish` decided.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.memory.calibration import UNCALIBRATED, Adjudication, Fit
from registry.workers.calibration_refit import CalibrationRefitReport, CalibrationRefitWorker, refit_one
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)
_PROVIDER = "anthropic"
_MODEL = "claude-haiku-4-5-20251001"


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _make_session_factory(*, strategy_ids: list[str] | None = None) -> tuple[MagicMock, list[str]]:
    """SQL-string-keyed AsyncMock session factory.

    Routes ``SELECT DISTINCT c.strategy_id FROM memory_claim_adjudication ...``
    to ``strategy_ids``. Returns the factory and a list every executed SQL
    statement (whitespace-collapsed) is appended to, so a test can assert on
    the exact predicate.
    """
    ids = strategy_ids or []
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()
        if "SELECT DISTINCT c.strategy_id FROM memory_claim_adjudication" in sql:
            rows = []
            for sid in ids:
                row = MagicMock()
                row.strategy_id = sid
                rows.append(row)
            result.all = MagicMock(return_value=rows)
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, executed


def _worker(
    *,
    calibration: Any = None,
    strategy_ids: list[str] | None = None,
    batch_size: int = 100,
) -> tuple[CalibrationRefitWorker, list[str]]:
    factory, executed = _make_session_factory(strategy_ids=strategy_ids)
    worker = CalibrationRefitWorker(
        factory,
        calibration or MagicMock(),
        provider_id=_PROVIDER,
        model_id=_MODEL,
        clock=FakeClock(_NOW),
        batch_size=batch_size,
    )
    return worker, executed


def _fit(*, n: int = 200, error: float = 0.0) -> Fit:
    return Fit(bins=tuple(0.9 for _ in range(10)), pooled_rate=0.9, n_adjudicated=n, measured_error=error)


# ---------------------------------------------------------------------------
# _candidate_strategies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_strategies_query_scans_usable_adjudications_only() -> None:
    """The exact predicate the worker scans on: a real self-report, a verdict
    that decided something, joined through to a non-null strategy_id."""
    worker, executed = _worker(strategy_ids=[])
    await worker._candidate_strategies()

    sql = next(s for s in executed if "SELECT DISTINCT c.strategy_id" in s)
    assert "FROM memory_claim_adjudication a" in sql
    assert "JOIN memory_claims c ON c.claim_id = a.claim_id" in sql
    assert "a.provider_confidence IS NOT NULL" in sql
    assert "a.verdict IN ('correct', 'incorrect')" in sql
    assert "c.strategy_id IS NOT NULL" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_candidate_strategies_returns_strategy_ids_in_query_order() -> None:
    ids = ["observation", "preference"]
    worker, _ = _worker(strategy_ids=ids)
    result = await worker._candidate_strategies()
    assert result == ids


# ---------------------------------------------------------------------------
# run_once: empty batch, outcome bucketing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_empty_candidates_is_not_an_error() -> None:
    calibration = MagicMock()
    calibration.load_observations = AsyncMock()
    worker, _ = _worker(calibration=calibration, strategy_ids=[])

    report = await worker.run_once()

    assert report == CalibrationRefitReport(considered=0, activated=0, stored_failed=0, below_minimum=0, failed=0)
    assert not report.had_work
    calibration.load_observations.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_activated_fit_is_counted_as_activated() -> None:
    version = "anthropic:claude-haiku-4-5-20251001:observation:2026-08-05:200"
    calibration = MagicMock()
    calibration.load_observations = AsyncMock(return_value=[Adjudication(provider_confidence=0.9, was_correct=True)])
    calibration.publish = AsyncMock(return_value=(version, True))
    worker, _ = _worker(calibration=calibration, strategy_ids=["observation"])

    report = await worker.run_once()

    assert report.considered == 1
    assert report.activated == 1
    assert report.stored_failed == 0
    assert report.below_minimum == 0
    assert report.failed == 0


@pytest.mark.asyncio
async def test_a_fit_missing_the_bound_is_counted_as_stored_failed() -> None:
    """`publish` stores a failing fit without activating it -- the worker
    reports that as `stored_failed`, distinct from `below_minimum`."""
    version = "anthropic:claude-haiku-4-5-20251001:observation:2026-08-05:200"
    calibration = MagicMock()
    calibration.load_observations = AsyncMock(return_value=[Adjudication(provider_confidence=0.9, was_correct=False)])
    calibration.publish = AsyncMock(return_value=(version, False))
    worker, _ = _worker(calibration=calibration, strategy_ids=["observation"])

    report = await worker.run_once()

    assert report.considered == 1
    assert report.activated == 0
    assert report.stored_failed == 1
    assert report.below_minimum == 0


@pytest.mark.asyncio
async def test_a_fit_below_the_evaluation_floor_is_counted_as_below_minimum() -> None:
    """`publish` refuses to store anything below `MIN_ADJUDICATED_FOR_MAPPING`
    and returns the `uncalibrated` sentinel -- the worker's own gate is just
    reporting that, never bypassing it."""
    calibration = MagicMock()
    calibration.load_observations = AsyncMock(return_value=[Adjudication(provider_confidence=0.9, was_correct=True)])
    calibration.publish = AsyncMock(return_value=(UNCALIBRATED, False))
    worker, _ = _worker(calibration=calibration, strategy_ids=["observation"])

    report = await worker.run_once()

    assert report.considered == 1
    assert report.activated == 0
    assert report.stored_failed == 0
    assert report.below_minimum == 1


@pytest.mark.asyncio
async def test_run_once_calls_publish_with_the_configured_provider_and_model() -> None:
    calibration = MagicMock()
    calibration.load_observations = AsyncMock(return_value=[])
    calibration.publish = AsyncMock(return_value=(UNCALIBRATED, False))
    worker, _ = _worker(calibration=calibration, strategy_ids=["observation"])

    await worker.run_once()

    calibration.load_observations.assert_awaited_once_with(
        provider_id=_PROVIDER, model_id=_MODEL, strategy_id="observation"
    )
    calibration.publish.assert_awaited_once()
    assert calibration.publish.await_args.kwargs["provider_id"] == _PROVIDER
    assert calibration.publish.await_args.kwargs["model_id"] == _MODEL
    assert calibration.publish.await_args.kwargs["strategy_id"] == "observation"


# ---------------------------------------------------------------------------
# Per-triple failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_strategy_failing_load_observations_does_not_stop_the_rest() -> None:
    strategies = ["observation", "preference", "session_summary"]
    failing = "preference"

    async def _load_observations(*, provider_id: str, model_id: str, strategy_id: str) -> list[Adjudication]:
        if strategy_id == failing:
            msg = "this neighbourhood is pathological"
            raise RuntimeError(msg)
        return [Adjudication(provider_confidence=0.9, was_correct=True)]

    calibration = MagicMock()
    calibration.load_observations = AsyncMock(side_effect=_load_observations)
    calibration.publish = AsyncMock(return_value=(UNCALIBRATED, False))
    worker, _ = _worker(calibration=calibration, strategy_ids=strategies)

    report = await worker.run_once()

    assert report.considered == 3
    assert report.failed == 1
    assert report.below_minimum == 2


@pytest.mark.asyncio
async def test_one_strategy_failing_publish_does_not_stop_the_rest() -> None:
    strategies = ["observation", "preference"]
    failing = "preference"

    async def _publish(*, provider_id: str, model_id: str, strategy_id: str, **kwargs: Any) -> tuple[str, bool]:
        if strategy_id == failing:
            msg = "database unavailable"
            raise RuntimeError(msg)
        return "v", True

    calibration = MagicMock()
    calibration.load_observations = AsyncMock(return_value=[Adjudication(provider_confidence=0.9, was_correct=True)])
    calibration.publish = AsyncMock(side_effect=_publish)
    worker, _ = _worker(calibration=calibration, strategy_ids=strategies)

    report = await worker.run_once()

    assert report.considered == 2
    assert report.activated == 1
    assert report.failed == 1


# ---------------------------------------------------------------------------
# refit_one: the shared sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refit_one_calls_load_observations_then_fit_then_publish() -> None:
    calibration = MagicMock()
    calibration.load_observations = AsyncMock(return_value=[Adjudication(provider_confidence=0.9, was_correct=True)])
    calibration.publish = AsyncMock(return_value=("v1", True))

    outcome = await refit_one(
        calibration,
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id="observation",
        clock=FakeClock(_NOW),
        fitted_by=uuid.uuid4(),
    )

    calibration.load_observations.assert_awaited_once_with(
        provider_id=_PROVIDER, model_id=_MODEL, strategy_id="observation"
    )
    calibration.publish.assert_awaited_once()
    publish_kwargs = calibration.publish.await_args.kwargs
    assert publish_kwargs["provider_id"] == _PROVIDER
    assert publish_kwargs["model_id"] == _MODEL
    assert publish_kwargs["strategy_id"] == "observation"
    assert publish_kwargs["now"] == _NOW
    assert outcome.version == "v1"
    assert outcome.activated is True
    assert outcome.n_adjudicated == 1


@pytest.mark.asyncio
async def test_refit_one_passes_fitted_by_through_to_publish() -> None:
    actor_id = uuid.uuid4()
    calibration = MagicMock()
    calibration.load_observations = AsyncMock(return_value=[])
    calibration.publish = AsyncMock(return_value=(UNCALIBRATED, False))

    await refit_one(
        calibration,
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id="observation",
        clock=FakeClock(_NOW),
        fitted_by=actor_id,
    )

    assert calibration.publish.await_args.kwargs["fitted_by"] == actor_id


@pytest.mark.asyncio
async def test_refit_one_defaults_fitted_by_to_none_for_the_periodic_worker() -> None:
    calibration = MagicMock()
    calibration.load_observations = AsyncMock(return_value=[])
    calibration.publish = AsyncMock(return_value=(UNCALIBRATED, False))

    await refit_one(
        calibration,
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id="observation",
        clock=FakeClock(_NOW),
    )

    assert calibration.publish.await_args.kwargs["fitted_by"] is None


def test_report_had_work_reflects_whether_any_strategy_was_considered() -> None:
    empty = CalibrationRefitReport(considered=0, activated=0, stored_failed=0, below_minimum=0, failed=0)
    assert not empty.had_work
    non_empty = dataclasses.replace(empty, considered=1)
    assert non_empty.had_work
