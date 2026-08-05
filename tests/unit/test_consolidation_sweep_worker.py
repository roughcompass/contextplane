"""Unit tests for ConsolidationSweepWorker.

All DB interaction is mocked at `session.execute` via an SQL-string-keyed
router -- no Postgres required, mirroring `test_promotion_sweep_worker.py`'s
mock-factory pattern. `ConsolidationService` is replaced with a lightweight
mock so these tests exercise only the sweep's own wiring: the candidate
predicate, the pending gauge, and per-claim outcome bucketing and isolation.

Coverage:
- `_candidates` / `_refresh_pending_gauge`: the exact predicate the sweep
  scans on (staged, not superseded, subject resolved, and either never
  consolidated or with a newer neighbour since).
- `run_once`: empty batch, an already-settled outcome, a fresh decision, and
  the module docstring's own central claim -- "every row's failure is its
  own" -- proven with a batch where the middle claim raises and the other two
  still land in their correct, distinct buckets.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.service.memory.consolidation import DECISION_ADD, DECISION_NOOP, Outcome
from registry.workers import consolidation_sweep as sweep_module
from registry.workers.consolidation_sweep import ConsolidationSweepWorker, SweepReport
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


def _make_session_factory(
    *,
    candidate_ids: list[uuid.UUID] | None = None,
    pending_count: int = 0,
) -> tuple[MagicMock, list[str]]:
    """SQL-string-keyed AsyncMock session factory.

    Routes:
    - ``SELECT c.claim_id FROM memory_claims ...`` -> ``candidate_ids``
    - ``SELECT count(*) FROM memory_claims ...``   -> ``pending_count``

    Returns the factory and a list every executed SQL statement (whitespace-
    collapsed) is appended to, so a test can assert on the exact predicate.
    """
    candidates = candidate_ids or []
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append(sql)
        result = MagicMock()
        if "SELECT c.claim_id FROM memory_claims" in sql:
            rows = []
            for cid in candidates:
                row = MagicMock()
                row.claim_id = cid
                rows.append(row)
            result.all = MagicMock(return_value=rows)
            return result
        if "SELECT count(*) FROM memory_claims" in sql:
            result.scalar_one = MagicMock(return_value=pending_count)
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
    consolidation: Any = None,
    candidate_ids: list[uuid.UUID] | None = None,
    pending_count: int = 0,
    batch_size: int = 100,
) -> tuple[ConsolidationSweepWorker, list[str]]:
    factory, executed = _make_session_factory(candidate_ids=candidate_ids, pending_count=pending_count)
    worker = ConsolidationSweepWorker(
        factory,
        consolidation or MagicMock(),
        clock=FakeClock(_NOW),
        batch_size=batch_size,
    )
    return worker, executed


# ---------------------------------------------------------------------------
# _candidates / _refresh_pending_gauge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_query_scans_staged_unsuperseded_resolved_claims_awaiting_reconciliation() -> None:
    """The exact predicate the sweep scans on: staged, not superseded, subject
    resolved, and either never consolidated or with a newer neighbour since --
    the second half is what makes the sweep revisit a claim rather than decide
    it once and never look again."""
    worker, executed = _worker(candidate_ids=[])
    await worker._candidates()

    sql = next(s for s in executed if "SELECT c.claim_id FROM memory_claims" in s)
    assert "c.status = 'staged'" in sql
    assert "c.superseded_by IS NULL" in sql
    assert "c.subject_entity_id IS NOT NULL" in sql
    assert "c.consolidated_at IS NULL" in sql
    assert "n.created_at > c.consolidated_at" in sql
    assert "ORDER BY c.created_at" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_candidates_returns_claim_ids_in_query_order() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    worker, _ = _worker(candidate_ids=ids)
    result = await worker._candidates()
    assert result == ids


@pytest.mark.asyncio
async def test_refresh_pending_gauge_reads_the_same_predicate_without_a_limit() -> None:
    worker, executed = _worker(pending_count=5)
    await worker._refresh_pending_gauge()

    sql = next(s for s in executed if "SELECT count(*) FROM memory_claims" in s)
    assert "status = 'staged'" in sql
    assert "superseded_by IS NULL" in sql
    assert "subject_entity_id IS NOT NULL" in sql
    assert "consolidated_at IS NULL" in sql
    assert "LIMIT" not in sql
    assert sweep_module._PENDING._value.get() == 5


# ---------------------------------------------------------------------------
# run_once: empty batch, outcome bucketing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_empty_candidates_is_not_an_error() -> None:
    consolidation = MagicMock()
    consolidation.consolidate = AsyncMock()
    worker, _ = _worker(consolidation=consolidation, candidate_ids=[])

    report = await worker.run_once()

    assert report == SweepReport(considered=0, decided=0, already_settled=0, failed=0)
    assert not report.had_work
    consolidation.consolidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_settled_outcome_is_counted_separately_from_a_fresh_decision() -> None:
    """`already_settled=True` means the sweep found nothing new to decide --
    distinct from an ordinary no-op, and distinct from a fresh decision."""
    claim_id = uuid.uuid4()
    consolidation = MagicMock()
    consolidation.consolidate = AsyncMock(
        return_value=Outcome(claim_id=claim_id, decision=DECISION_NOOP, reason="settled", already_settled=True)
    )
    worker, _ = _worker(consolidation=consolidation, candidate_ids=[claim_id])

    report = await worker.run_once()

    assert report.considered == 1
    assert report.already_settled == 1
    assert report.decided == 0
    assert report.failed == 0


@pytest.mark.asyncio
async def test_a_fresh_decision_is_counted_as_decided_not_already_settled() -> None:
    claim_id = uuid.uuid4()
    consolidation = MagicMock()
    consolidation.consolidate = AsyncMock(
        return_value=Outcome(claim_id=claim_id, decision=DECISION_ADD, reason="first claim on subject/predicate")
    )
    worker, _ = _worker(consolidation=consolidation, candidate_ids=[claim_id])

    report = await worker.run_once()

    assert report.considered == 1
    assert report.decided == 1
    assert report.already_settled == 0
    assert report.failed == 0


# ---------------------------------------------------------------------------
# Per-claim failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_claims_failing_consolidate_does_not_stop_the_batch() -> None:
    """The module docstring's own claim: 'every row's failure is its own'. A
    batch of three where the middle claim's neighbourhood is pathological must
    still land the other two in their correct, distinct buckets -- not just
    "the batch didn't crash", but the specific outcomes for the surviving rows
    are exactly what they would have been without the failure."""
    ok_settled, failing, ok_decided = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _consolidate(claim_id: uuid.UUID) -> Outcome:
        if claim_id == failing:
            msg = "this neighbourhood is pathological"
            raise RuntimeError(msg)
        if claim_id == ok_settled:
            return Outcome(claim_id=claim_id, decision=DECISION_NOOP, reason="settled", already_settled=True)
        return Outcome(claim_id=claim_id, decision=DECISION_ADD, reason="new")

    consolidation = MagicMock()
    consolidation.consolidate = AsyncMock(side_effect=_consolidate)
    worker, _ = _worker(consolidation=consolidation, candidate_ids=[ok_settled, failing, ok_decided])

    report = await worker.run_once()

    assert report.considered == 3
    assert report.failed == 1
    assert report.already_settled == 1
    assert report.decided == 1


@pytest.mark.asyncio
async def test_a_failed_claim_still_refreshes_the_pending_gauge_after_the_batch() -> None:
    """The gauge is refreshed both before and after the batch; a failure
    partway through must not skip the second refresh, or an operator watching
    the gauge would see a stale number after every sweep that hit a bad row."""
    claim_id = uuid.uuid4()
    consolidation = MagicMock()
    consolidation.consolidate = AsyncMock(side_effect=RuntimeError("boom"))
    worker, executed = _worker(consolidation=consolidation, candidate_ids=[claim_id], pending_count=3)

    await worker.run_once()

    gauge_reads = [s for s in executed if "SELECT count(*) FROM memory_claims" in s]
    assert len(gauge_reads) == 2
    assert sweep_module._PENDING._value.get() == 3
