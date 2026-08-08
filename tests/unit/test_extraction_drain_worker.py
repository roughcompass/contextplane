"""Unit tests for ExtractionDrainWorker.

All DB interaction is mocked at `session.execute` via an SQL-string-keyed
router -- no Postgres required, mirroring `test_promotion_sweep_worker.py`'s
mock-factory pattern. The provider, the extraction service, and the strategy
config resolver are each replaced with lightweight mocks so these tests
exercise only the drain's own wiring: claiming, the unknown/disabled/empty
short-circuits, successful staging, the retry-vs-dead-letter split, and
per-row isolation of provider failures within one batch.

Coverage:
- `_claim` / `_refresh_pending_gauge`: the exact predicate the drain scans
  on, and the unfiltered total the gauge reads.
- `run_once`: empty batch, an unknown strategy, a disabled strategy, a window
  whose events are already gone, a successful stage-and-complete, a retriable
  provider failure (both freshly backed off and exhausted into a dead
  letter), a terminal provider failure, and one row's provider failure not
  stopping the rest of the batch -- the module docstring's own claim that "a
  provider that times out on one session must not stall the twenty behind
  it."
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from registry.extraction.provider import ProviderError
from registry.extraction.strategies import STRATEGY_OBSERVATION
from registry.workers import extraction_drain as drain_module
from registry.workers.extraction_drain import (
    BACKOFF_SCHEDULE_S,
    MAX_ATTEMPTS,
    DrainReport,
    ExtractionDrainWorker,
)
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


def _claim_row(
    *,
    outbox_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    session_id: str = "sess-1",
    strategy_id: str = STRATEGY_OBSERVATION,
    from_seq: int = 1,
    through_seq: int = 5,
    attempts: int = 0,
    enqueued_at: datetime.datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        outbox_id=outbox_id or uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        actor_id=actor_id or uuid.uuid4(),
        session_id=session_id,
        strategy_id=strategy_id,
        from_seq=from_seq,
        through_seq=through_seq,
        attempts=attempts,
        enqueued_at=enqueued_at or (_NOW - datetime.timedelta(minutes=5)),
    )


def _event_row(
    *,
    event_id: uuid.UUID | None = None,
    session_id: str = "sess-1",
    seq: int = 1,
    kind: str = "user_message",
    body: str = "hello",
    tool_name: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: datetime.datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id or uuid.uuid4(),
        session_id=session_id,
        seq=seq,
        kind=kind,
        body=body,
        tool_name=tool_name,
        metadata=metadata,
        created_at=created_at or (_NOW - datetime.timedelta(minutes=10)),
    )


def _strategy(
    *,
    strategy_id: str = STRATEGY_OBSERVATION,
    system_prompt: str = "extract observations",
    output_schema: dict[str, Any] | None = None,
    default_model_id: str = "test-model",
    max_output_tokens: int = 256,
    permitted_predicates: tuple[str, ...] = ("uses_tool",),
) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_id=strategy_id,
        system_prompt=system_prompt,
        output_schema=output_schema or {},
        default_model_id=default_model_id,
        max_output_tokens=max_output_tokens,
        permitted_predicates=permitted_predicates,
    )


def _resolved(
    *,
    strategy: Any,
    is_enabled: bool = True,
    confidence_floor: float = 0.5,
    namespace: str = "ns",
) -> MagicMock:
    resolved = MagicMock()
    resolved.strategy = strategy
    resolved.is_enabled = is_enabled
    resolved.confidence_floor = confidence_floor
    resolved.namespace_for = MagicMock(return_value=namespace)
    return resolved


def _config_with(resolved: Any) -> MagicMock:
    config = MagicMock()
    config.resolve_one = AsyncMock(return_value=resolved)
    return config


def _make_session_factory(
    *,
    claim_rows: list[Any] | None = None,
    window_rows_by_session: dict[str, list[Any]] | None = None,
    pending_count: int = 0,
) -> tuple[MagicMock, list[dict[str, Any]]]:
    """SQL-string-keyed AsyncMock session factory.

    Routes:
    - claim SELECT               -> ``claim_rows``
    - window SELECT (keyed by ``:sid``) -> ``window_rows_by_session[sid]``
    - pending-count SELECT       -> ``pending_count``
    - the complete/retry/dead-letter writes -> no-op

    Returns the factory and a list every executed SQL statement (whitespace-
    collapsed) plus its params is appended to, so a test can assert on the
    exact predicate or the exact write.
    """
    claims = claim_rows or []
    windows = window_rows_by_session or {}
    executed: list[dict[str, Any]] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append({"sql": sql, "params": params})
        result = MagicMock()
        if "SELECT outbox_id, tenant_id, actor_id, session_id, strategy_id" in sql:
            result.all = MagicMock(return_value=list(claims))
            return result
        if "SELECT event_id, session_id, seq, kind, body, tool_name, metadata" in sql:
            sid = str((params or {}).get("sid"))
            result.all = MagicMock(return_value=list(windows.get(sid, [])))
            return result
        if "SELECT count(*) FROM memory_extraction_outbox" in sql:
            result.scalar_one = MagicMock(return_value=pending_count)
            return result
        if "DELETE FROM memory_extraction_outbox WHERE outbox_id = :oid AND through_seq" in sql:
            return result
        if "UPDATE memory_extraction_outbox SET from_seq" in sql:
            return result
        if "UPDATE memory_extraction_outbox SET attempts = CAST(:attempts AS INTEGER)" in sql:
            return result
        if "INSERT INTO memory_extraction_outbox_failed" in sql:
            return result
        if "DELETE FROM memory_extraction_outbox WHERE outbox_id = :oid" in sql:
            return result
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, executed


def _worker(
    *,
    provider: Any = None,
    extraction: Any = None,
    config: Any = None,
    claim_rows: list[Any] | None = None,
    window_rows_by_session: dict[str, list[Any]] | None = None,
    pending_count: int = 0,
    batch_size: int = 10,
) -> tuple[ExtractionDrainWorker, list[dict[str, Any]]]:
    factory, executed = _make_session_factory(
        claim_rows=claim_rows,
        window_rows_by_session=window_rows_by_session,
        pending_count=pending_count,
    )
    worker = ExtractionDrainWorker(
        factory,
        provider or MagicMock(),
        extraction or MagicMock(),
        clock=FakeClock(_NOW),
        batch_size=batch_size,
        config=config or MagicMock(),
    )
    return worker, executed


# ---------------------------------------------------------------------------
# _claim / _refresh_pending_gauge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_query_scans_eligible_rows_ordered_locked_and_bounded() -> None:
    worker, executed = _worker(batch_size=7)
    await worker._claim(_NOW)

    entry = next(e for e in executed if "SELECT outbox_id, tenant_id, actor_id, session_id, strategy_id" in e["sql"])
    sql = entry["sql"]
    assert "next_attempt_at IS NULL OR next_attempt_at <= CAST(:now AS TIMESTAMPTZ)" in sql
    assert "ORDER BY enqueued_at" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert entry["params"]["lim"] == 7
    assert entry["params"]["now"] == _NOW


@pytest.mark.asyncio
async def test_refresh_pending_gauge_reads_total_count_without_a_where_clause() -> None:
    worker, executed = _worker(pending_count=4)
    await worker._refresh_pending_gauge()

    sql = next(e["sql"] for e in executed if "SELECT count(*) FROM memory_extraction_outbox" in e["sql"])
    assert "WHERE" not in sql
    assert drain_module._PENDING._value.get() == 4


# ---------------------------------------------------------------------------
# run_once: empty batch, unknown/disabled/empty short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_empty_batch_is_not_an_error() -> None:
    provider = MagicMock()
    provider.extract = AsyncMock()
    worker, _ = _worker(provider=provider, claim_rows=[])

    report = await worker.run_once()

    assert report == DrainReport(claimed=0, staged_claims=0, retried=0, dead_lettered=0, refusals=0)
    assert not report.had_work
    provider.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_strategy_is_dead_lettered_without_calling_the_provider() -> None:
    row = _claim_row(strategy_id="not-a-real-strategy", attempts=0)
    provider = MagicMock()
    provider.extract = AsyncMock()
    worker, executed = _worker(provider=provider, claim_rows=[row])

    report = await worker.run_once()

    assert report.claimed == 1
    assert report.dead_lettered == 1
    assert report.staged_claims == 0
    assert report.retried == 0
    provider.extract.assert_not_awaited()

    insert = next(e["params"] for e in executed if "INSERT INTO memory_extraction_outbox_failed" in e["sql"])
    assert insert["strat"] == "not-a-real-strategy"
    assert "unknown strategy" in insert["err"]
    assert insert["attempts"] == 0


@pytest.mark.asyncio
async def test_disabled_strategy_completes_the_row_without_calling_the_provider() -> None:
    row = _claim_row(through_seq=9)
    resolved = _resolved(strategy=_strategy(), is_enabled=False)
    provider = MagicMock()
    provider.extract = AsyncMock()
    worker, executed = _worker(provider=provider, config=_config_with(resolved), claim_rows=[row])

    report = await worker.run_once()

    assert report.claimed == 1
    assert report.dead_lettered == 0
    assert report.retried == 0
    assert report.staged_claims == 0
    provider.extract.assert_not_awaited()

    advance = next(e["params"] for e in executed if "UPDATE memory_extraction_outbox SET from_seq" in e["sql"])
    assert advance["next"] == row.through_seq + 1


@pytest.mark.asyncio
async def test_empty_window_completes_the_row_without_calling_the_provider() -> None:
    row = _claim_row(through_seq=4)
    resolved = _resolved(strategy=_strategy())
    provider = MagicMock()
    provider.extract = AsyncMock()
    worker, executed = _worker(
        provider=provider,
        config=_config_with(resolved),
        claim_rows=[row],
        window_rows_by_session={},  # no events for row.session_id: the window is already gone
    )

    report = await worker.run_once()

    assert report.claimed == 1
    assert report.staged_claims == 0
    assert report.dead_lettered == 0
    provider.extract.assert_not_awaited()

    advance = next(e["params"] for e in executed if "UPDATE memory_extraction_outbox SET from_seq" in e["sql"])
    assert advance["next"] == row.through_seq + 1


# ---------------------------------------------------------------------------
# run_once: successful staging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_extraction_stages_and_completes_through_the_last_event_seq() -> None:
    row = _claim_row(session_id="sess-ok", from_seq=1, through_seq=9)
    events = [
        _event_row(session_id="sess-ok", seq=1, created_at=_NOW - datetime.timedelta(minutes=10)),
        _event_row(session_id="sess-ok", seq=2, created_at=_NOW - datetime.timedelta(minutes=9)),
    ]
    strategy = _strategy()
    resolved = _resolved(strategy=strategy, confidence_floor=0.42, namespace="tenant-ns")
    provider = MagicMock()
    provider.extract = AsyncMock(return_value=MagicMock())
    extraction = MagicMock()
    stage_result = AsyncMock(return_value=MagicMock(staged=["claim-1", "claim-2"], refusals=["refusal-1"]))
    extraction.stage_result = stage_result

    worker, executed = _worker(
        provider=provider,
        extraction=extraction,
        config=_config_with(resolved),
        claim_rows=[row],
        window_rows_by_session={"sess-ok": events},
    )

    report = await worker.run_once()

    assert report.claimed == 1
    assert report.staged_claims == 2
    assert report.refusals == 1
    assert report.retried == 0
    assert report.dead_lettered == 0

    provider.extract.assert_awaited_once()
    request = provider.extract.call_args.args[0]
    assert request.strategy_id == strategy.strategy_id
    assert request.model_id == strategy.default_model_id
    assert [e.seq for e in request.events] == [1, 2]

    stage_result.assert_awaited_once()
    ctx = stage_result.call_args.args[0]
    assert ctx.tenant_id == row.tenant_id
    assert ctx.oidc_subject == f"extraction-worker:{row.strategy_id}"
    assert list(ctx.roles) == []
    kwargs = stage_result.call_args.kwargs
    assert kwargs["confidence_floor"] == 0.42
    assert kwargs["namespace"] == "tenant-ns"
    assert kwargs["known_event_ids"] == frozenset(str(e.event_id) for e in events)
    # The same request object reaches the provider and the staging path. That
    # identity is what makes the containment boundary checkable: two copies of
    # it agree only by accident, and the check is worth nothing on the run where
    # they do not.
    assert kwargs["request"] is request

    advance = next(e["params"] for e in executed if "UPDATE memory_extraction_outbox SET from_seq" in e["sql"])
    assert advance["next"] == events[-1].seq + 1


# ---------------------------------------------------------------------------
# run_once: retriable vs terminal provider failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retriable_provider_error_schedules_backoff_with_the_first_delay() -> None:
    row = _claim_row(session_id="sess-retry", attempts=0)
    events = [_event_row(session_id="sess-retry", seq=1)]
    resolved = _resolved(strategy=_strategy())
    provider = MagicMock()
    provider.extract = AsyncMock(side_effect=ProviderError("rate limited", is_retriable=True))
    worker, executed = _worker(
        provider=provider,
        config=_config_with(resolved),
        claim_rows=[row],
        window_rows_by_session={"sess-retry": events},
    )

    report = await worker.run_once()

    assert report.retried == 1
    assert report.dead_lettered == 0
    assert report.staged_claims == 0

    retry_update = next(e["params"] for e in executed if "SET attempts = CAST(:attempts AS INTEGER)" in e["sql"])
    assert retry_update["attempts"] == 1
    assert retry_update["delay"] == BACKOFF_SCHEDULE_S[0]
    assert "rate limited" in retry_update["err"]
    assert not any("INSERT INTO memory_extraction_outbox_failed" in e["sql"] for e in executed)


@pytest.mark.asyncio
async def test_retriable_provider_error_dead_letters_once_retries_are_exhausted() -> None:
    row = _claim_row(session_id="sess-exhausted", attempts=MAX_ATTEMPTS - 1)
    events = [_event_row(session_id="sess-exhausted", seq=1)]
    resolved = _resolved(strategy=_strategy())
    provider = MagicMock()
    provider.extract = AsyncMock(side_effect=ProviderError("still rate limited", is_retriable=True))
    worker, executed = _worker(
        provider=provider,
        config=_config_with(resolved),
        claim_rows=[row],
        window_rows_by_session={"sess-exhausted": events},
    )

    report = await worker.run_once()

    assert report.dead_lettered == 1
    assert report.retried == 0

    dead = next(e["params"] for e in executed if "INSERT INTO memory_extraction_outbox_failed" in e["sql"])
    assert dead["attempts"] == MAX_ATTEMPTS
    assert not any("SET attempts = CAST(:attempts AS INTEGER)" in e["sql"] for e in executed)


@pytest.mark.asyncio
async def test_terminal_provider_error_dead_letters_immediately_without_consuming_a_retry() -> None:
    """A rejected credential means 'never, until somebody changes something' --
    dead-lettered straight away rather than backed off, and the attempts count
    in the dead-letter row proves it never went through the retry counter."""
    row = _claim_row(session_id="sess-terminal", attempts=0)
    events = [_event_row(session_id="sess-terminal", seq=1)]
    resolved = _resolved(strategy=_strategy())
    provider = MagicMock()
    provider.extract = AsyncMock(side_effect=ProviderError("invalid api key", is_retriable=False))
    worker, executed = _worker(
        provider=provider,
        config=_config_with(resolved),
        claim_rows=[row],
        window_rows_by_session={"sess-terminal": events},
    )

    report = await worker.run_once()

    assert report.dead_lettered == 1
    assert report.retried == 0

    dead = next(e["params"] for e in executed if "INSERT INTO memory_extraction_outbox_failed" in e["sql"])
    assert dead["attempts"] == 0
    assert "invalid api key" in dead["err"]


# ---------------------------------------------------------------------------
# Per-row isolation of provider failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_rows_provider_failure_does_not_stop_the_rest_of_the_batch() -> None:
    """The module docstring's own claim: 'a provider that times out on one
    session must not stall the twenty behind it'. A batch of three where the
    middle row's provider call fails must still land the other two in their
    correct, distinct buckets, and the provider must still be tried for all
    three rows -- not just 'the batch didn't crash'."""
    ok_row_1 = _claim_row(session_id="sess-a", attempts=0)
    failing_row = _claim_row(session_id="sess-b", attempts=0)
    ok_row_2 = _claim_row(session_id="sess-c", attempts=0)

    windows = {
        "sess-a": [_event_row(session_id="sess-a", seq=1)],
        "sess-b": [_event_row(session_id="sess-b", seq=1)],
        "sess-c": [_event_row(session_id="sess-c", seq=1)],
    }
    resolved = _resolved(strategy=_strategy())

    async def _extract(request: Any) -> MagicMock:
        if request.events[0].session_id == "sess-b":
            raise ProviderError("temporarily unavailable", is_retriable=True)
        return MagicMock()

    provider = MagicMock()
    provider.extract = AsyncMock(side_effect=_extract)
    extraction = MagicMock()
    extraction.stage_result = AsyncMock(return_value=MagicMock(staged=["c"], refusals=[]))

    worker, _ = _worker(
        provider=provider,
        extraction=extraction,
        config=_config_with(resolved),
        claim_rows=[ok_row_1, failing_row, ok_row_2],
        window_rows_by_session=windows,
    )

    report = await worker.run_once()

    assert report.claimed == 3
    assert provider.extract.await_count == 3
    assert report.retried == 1
    assert report.staged_claims == 2
    assert report.dead_lettered == 0
