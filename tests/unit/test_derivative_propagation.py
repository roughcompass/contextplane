"""The propagation drain: what happens to one queued item, and to the queue.

This machinery is inert as shipped — nothing constructs it, because no handler exists
for any derivative kind yet — which makes these the only tests that exercise it at
all. That is the reason to be thorough rather than a reason to be brief: the first
time it runs for real it will be deleting content somebody asked to have erased, and
the failure modes that matter (a handler that throws, a retry that exhausts, a batch
where one item fails and the others must not) are all reachable here with a fake
session and nothing else.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from contextplane.retention import derivatives
from contextplane.workers import derivative_propagation as drain

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)


class _AsyncCM:
    """The `async with session_factory() as session` shape, and nothing more."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Records every statement and answers the claim query with declared rows.

    Keyed on the leading verb rather than the whole statement: the assertions below
    care which rows were claimed and what was written back, not the exact SQL, which
    the integration tier is the right place to pin.
    """

    def __init__(self, claim_rows: list[SimpleNamespace]) -> None:
        self._claim_rows = claim_rows
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params or {}))
        if sql.startswith("SELECT"):
            rows = self._claim_rows
            # Claimed once: a second tick must not re-serve the same batch.
            self._claim_rows = []
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [dict(vars(r)) for r in rows]))
        return SimpleNamespace()

    async def commit(self) -> None:
        self.commits += 1

    def outcomes(self) -> list[dict[str, Any]]:
        """The per-item write-backs, excluding the batch claim.

        Claiming issues its own `UPDATE ... SET claimed_at ... WHERE work_id = ANY(:ids)`
        before any handler runs, so a bare "first UPDATE" would read the claim and not
        the outcome. Distinguished by the bound `ids` list, which only the claim has.
        """
        return [params for sql, params in self.executed if sql.startswith("UPDATE") and "ids" not in params]

    def claims(self) -> list[dict[str, Any]]:
        return [params for sql, params in self.executed if sql.startswith("UPDATE") and "ids" in params]


def _factory(session: _FakeSession) -> MagicMock:
    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(session)
    return factory


def _row(*, kind: str = derivatives.KIND_VECTOR, attempts: int = 0, blocking: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        work_id=uuid.uuid4(),
        tenant_id=_TENANT,
        derivative_id=uuid.uuid4(),
        operation=derivatives.OPERATION_DELETE,
        trigger=derivatives.TRIGGER_ERASURE,
        attempts=attempts,
        derivative_kind=kind,
        storage_locator="vectors/shard-1",
        audience_partition="tenant",
        classification="internal",
        expires_at=_NOW + datetime.timedelta(days=1),
        blocking=blocking,
    )


class _Handler:
    """A handler that reports how many artefacts it touched, or raises."""

    def __init__(
        self,
        kind: str = derivatives.KIND_VECTOR,
        *,
        touched: int = 3,
        error: Exception | None = None,
    ) -> None:
        self.kind = kind
        self.version = "v1"
        self._touched = touched
        self._error = error
        self.calls: list[str] = []

    async def apply(self, session: Any, registration: derivatives.Registration, operation: str) -> int:
        self.calls.append(operation)
        if self._error is not None:
            raise self._error
        return self._touched


def _registry(*handlers: _Handler) -> derivatives.HandlerRegistry:
    registry = derivatives.HandlerRegistry()
    for handler in handlers:
        registry.register(handler)  # type: ignore[arg-type]
    return registry


# --- The ordinary path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_applied_item_is_marked_done_and_its_artefacts_counted() -> None:
    """The count is artefacts the handler actually touched, not items processed: one
    queue row can stand for many vectors, and reporting rows would understate what
    the erasure achieved."""
    session = _FakeSession([_row()])
    handler = _Handler(touched=7)
    worker = drain.DerivativePropagationWorker(_factory(session), _registry(handler))

    report = await worker.run_once(now=_NOW)

    assert (report.claimed, report.applied, report.artefacts) == (1, 1, 7)
    assert (report.retried, report.failed) == (0, 0)
    assert report.had_work is True
    assert handler.calls == [derivatives.OPERATION_DELETE]

    done = session.outcomes()[0]
    assert done["done"] == drain.STATE_DONE
    assert done["now"] == _NOW


@pytest.mark.asyncio
async def test_an_empty_queue_reports_no_work_rather_than_success() -> None:
    """`had_work` is what a scheduler reads to decide whether to log anything, and a
    quiet tick must be distinguishable from a productive one."""
    session = _FakeSession([])
    report = await drain.DerivativePropagationWorker(_factory(session), _registry(_Handler())).run_once(now=_NOW)

    assert (report.claimed, report.applied, report.artefacts) == (0, 0, 0)
    assert report.had_work is False
    assert session.outcomes() == [] and session.claims() == []


@pytest.mark.asyncio
async def test_each_item_commits_separately_so_one_failure_cannot_undo_the_rest() -> None:
    """The reason per-item transactions exist. The successful items are deletions, and
    re-doing them is not free: a rebuild re-reads sources that may themselves be
    mid-erasure."""
    session = _FakeSession([_row(), _row(), _row()])
    worker = drain.DerivativePropagationWorker(_factory(session), _registry(_Handler(touched=1)))

    report = await worker.run_once(now=_NOW)

    assert (report.claimed, report.applied, report.artefacts) == (3, 3, 3)
    # One commit per item plus the one that claimed the batch — not a single commit
    # wrapping all three, which is what would let the third item's failure undo the
    # first two deletions.
    assert session.commits == 4
    assert len(session.outcomes()) == 3
    assert len(session.claims()) == 1


# --- Failure, retry, and exhaustion --------------------------------------------


@pytest.mark.asyncio
async def test_a_kind_with_no_handler_is_a_recorded_failure_not_a_crash() -> None:
    """The inert-tree case, and the one an operator will actually hit first: the row
    records why, and the tick keeps going instead of taking the scheduler down."""
    session = _FakeSession([_row(kind=derivatives.KIND_EXPORT)])
    worker = drain.DerivativePropagationWorker(_factory(session), _registry(_Handler(derivatives.KIND_VECTOR)))

    report = await worker.run_once(now=_NOW)

    assert (report.claimed, report.applied, report.retried) == (1, 0, 1)
    recorded = session.outcomes()[0]
    assert recorded["attempts"] == 1
    assert recorded["state"] == drain.STATE_PENDING
    assert "UnhandledDerivativeKind" in recorded["error"]


@pytest.mark.asyncio
async def test_a_failing_handler_is_retried_with_backoff_written_to_the_row() -> None:
    """Backoff on the row rather than slept in the worker: a sleeping worker holds a
    connection and a scheduler slot doing nothing, and loses its place on restart."""
    session = _FakeSession([_row(attempts=1)])
    handler = _Handler(error=RuntimeError("vector store unreachable"))
    worker = drain.DerivativePropagationWorker(_factory(session), _registry(handler))

    report = await worker.run_once(now=_NOW)

    assert (report.retried, report.failed) == (1, 0)
    recorded = session.outcomes()[0]
    assert recorded["attempts"] == 2
    assert recorded["state"] == drain.STATE_PENDING
    assert recorded["available"] > _NOW
    assert recorded["available"] == _NOW + drain._backoff(2)
    assert "RuntimeError: vector store unreachable" in recorded["error"]


@pytest.mark.asyncio
async def test_the_last_attempt_ends_in_failed_rather_than_retrying_forever() -> None:
    """Retrying forever turns a broken handler into a queue that looks busy while the
    erased content stays exactly where it is. A `failed` item is a compliance incident
    and has to read as one."""
    session = _FakeSession([_row(attempts=drain.MAX_ATTEMPTS - 1)])
    worker = drain.DerivativePropagationWorker(_factory(session), _registry(_Handler(error=OSError("disk gone"))))

    report = await worker.run_once(now=_NOW)

    assert (report.failed, report.retried) == (1, 0)
    recorded = session.outcomes()[0]
    assert recorded["attempts"] == drain.MAX_ATTEMPTS
    assert recorded["state"] == drain.STATE_FAILED


@pytest.mark.asyncio
async def test_a_long_handler_error_is_truncated_before_it_reaches_the_row() -> None:
    """An unbounded error string is a handler's stack trace in a column somebody has
    to read; the row keeps enough to diagnose and not the whole of it."""
    session = _FakeSession([_row()])
    worker = drain.DerivativePropagationWorker(_factory(session), _registry(_Handler(error=RuntimeError("x" * 5000))))

    await worker.run_once(now=_NOW)

    assert len(session.outcomes()[0]["error"]) <= 2000


def test_backoff_grows_with_attempts_and_stays_bounded() -> None:
    """Growing, so a broken dependency is not hammered; bounded, so an item that will
    succeed does not wait a day to prove it."""
    delays = [drain._backoff(attempt) for attempt in range(1, drain.MAX_ATTEMPTS + 1)]

    assert all(earlier <= later for earlier, later in zip(delays, delays[1:], strict=False))
    assert delays[0] > datetime.timedelta(0)
    # The whole retry ladder outlasts a redeploy without stretching past an operator's
    # attention span, which is the property the module's own docstring claims.
    assert sum(delays, datetime.timedelta(0)) < datetime.timedelta(hours=1)


def test_a_report_with_only_failures_still_counts_as_work() -> None:
    """A tick that failed everything is not an idle tick, and logging it as one is how
    a stuck queue goes unnoticed."""
    assert drain.PropagationReport(claimed=1, failed=1).had_work is True
    assert drain.PropagationReport().had_work is False
