"""Unit tests for session_events.py: sequence allocation and cursor/ordering
semantics, without Postgres.

Session interaction is mocked via an SQL-string-keyed AsyncMock router (see
test_promotion_sweep_worker.py for the established pattern). Rows returned
by `list_events`/`list_sessions`/`get_event` are read by attribute (a real
`Result.all()`/`.one()` yields `Row` objects), so fakes here are
`types.SimpleNamespace`, not dicts.

Coverage:
- `list_events`: ascending is the default and descending reverses it; a
  cursor reads strictly after the cursor in ascending order and strictly
  before it in descending order; no cursor means no cursor predicate at all;
  pagination is by `seq`, never `OFFSET`; `since_seq`/`until_seq` reach the
  query unchanged; rows come back in the order the query returned them.
- `record_event`'s sequence-collision retry: a unique-violation is retried
  (the loser just takes the next position), a different IntegrityError is
  not retried, and giving up after the attempt ceiling raises a typed error
  rather than the raw driver exception.
- `_validate`: the closed kind vocabulary, the byte-accurate size cap, and
  the tool_name/kind pairing rule, in both directions.
- `_require_actor`: a context with no actor is refused before any query, the
  same way a missing/foreign event is reported identically to protect
  against an existence oracle.
- `_page`: clamps below 1 and above `MAX_PAGE`, passes an in-range value
  through unchanged.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

import registry.service.memory.session_events as session_events_module
from registry.exceptions import NotFoundError, ValidationError
from registry.service.memory.session_events import MAX_BODY_BYTES, MAX_PAGE, MemoryService, _page
from registry.types import TenantContext
from tests.helpers.clock import FakeClock
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncCM:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _event_row(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = dict(
        event_id=uuid.uuid4(),
        session_id="s1",
        seq=1,
        kind="user_message",
        body="hi",
        tool_name=None,
        metadata={},
        created_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _refusing_factory() -> MagicMock:
    def _refuse() -> Any:
        raise AssertionError("the database was touched despite the validation failure")

    factory = MagicMock()
    factory.side_effect = _refuse
    return factory


def _read_capturing_factory(rows: list[Any] | None = None) -> tuple[MagicMock, list[tuple[str, dict]]]:
    """A `_read`-shaped session (`list_events`/`list_sessions`/`get_event`):
    `session.execute(...)` returns a result whose `.all()` is the fixed row
    list, and every call is recorded for SQL/param assertions."""
    captured: list[tuple[str, dict]] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        captured.append((sql, params or {}))
        result = MagicMock()
        result.all = MagicMock(return_value=rows or [])
        return result

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory, captured


def _sequence_attempt_factory(
    effects: list[Exception | dict[str, object]],
) -> tuple[MagicMock, dict[str, int]]:
    """One session per `record_event` attempt. `effects[i]` is either an
    exception the INSERT raises on attempt `i`, or the `RETURNING` values it
    succeeds with."""
    calls = {"n": 0}

    def _make_session() -> AsyncMock:
        idx = calls["n"]
        calls["n"] += 1
        effect = effects[idx]

        async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
            sql = " ".join(str(stmt).split())
            if "SELECT memory_retention_days" in sql:
                result = MagicMock()
                result.scalar_one = MagicMock(return_value=30)
                return result
            if "INSERT INTO memory_session_events (" in sql:
                if isinstance(effect, Exception):
                    raise effect
                result = MagicMock()
                result.one = MagicMock(return_value=SimpleNamespace(**effect))
                return result
            raise AssertionError(f"unexpected SQL: {sql}")

        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_make_session())
    return factory, calls


def _collision(sqlstate: str = "23505") -> IntegrityError:
    return IntegrityError("insert ...", {}, SimpleNamespace(sqlstate=sqlstate))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# list_events: cursor and ordering semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_defaults_to_ascending_order_with_no_offset() -> None:
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_events(tenant_context(), session_id="s1")
    sql, _ = captured[-1]
    assert "ORDER BY seq ASC" in sql
    assert "OFFSET" not in sql


@pytest.mark.asyncio
async def test_list_events_descending_order_reverses_the_sort() -> None:
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_events(tenant_context(), session_id="s1", order="desc")
    sql, _ = captured[-1]
    assert "ORDER BY seq DESC" in sql


@pytest.mark.asyncio
async def test_list_events_without_a_cursor_has_no_cursor_predicate_at_all() -> None:
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_events(tenant_context(), session_id="s1")
    sql, params = captured[-1]
    assert "cursor" not in params
    assert ":cursor" not in sql


@pytest.mark.asyncio
async def test_list_events_cursor_reads_strictly_after_in_ascending_order() -> None:
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_events(
        tenant_context(), session_id="s1", cursor=5, order="asc"
    )
    sql, params = captured[-1]
    assert "seq > :cursor" in sql
    assert "seq < :cursor" not in sql
    assert params["cursor"] == 5


@pytest.mark.asyncio
async def test_list_events_cursor_reads_strictly_before_in_descending_order() -> None:
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_events(
        tenant_context(), session_id="s1", cursor=5, order="desc"
    )
    sql, params = captured[-1]
    assert "seq < :cursor" in sql
    assert "seq > :cursor" not in sql
    assert params["cursor"] == 5


@pytest.mark.asyncio
async def test_list_events_passes_since_and_until_seq_through_unchanged() -> None:
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_events(
        tenant_context(), session_id="s1", since_seq=10, until_seq=20
    )
    _, params = captured[-1]
    assert params["since"] == 10
    assert params["until"] == 20


@pytest.mark.asyncio
async def test_list_events_returns_rows_in_the_order_the_query_returned_them() -> None:
    rows = [_event_row(seq=1), _event_row(seq=2), _event_row(seq=3)]
    factory, _ = _read_capturing_factory(rows)
    events = await MemoryService(factory, clock=FakeClock(_NOW)).list_events(tenant_context(), session_id="s1")
    assert [e.seq for e in events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_list_events_excludes_invalidated_events_by_predicate() -> None:
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_events(tenant_context(), session_id="s1")
    sql, _ = captured[-1]
    assert "invalidated_at IS NULL" in sql


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


def _summary_row(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = dict(session_id="s1", event_count=3, first_at=_NOW, last_at=_NOW)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_list_sessions_maps_aggregated_rows_into_summaries() -> None:
    row = _summary_row(session_id="s1", event_count=5)
    factory, _ = _read_capturing_factory([row])

    summaries = await MemoryService(factory, clock=FakeClock(_NOW)).list_sessions(tenant_context())

    assert len(summaries) == 1
    assert summaries[0].session_id == "s1"
    assert summaries[0].event_count == 5
    assert summaries[0].first_activity_at == _NOW
    assert summaries[0].last_activity_at == _NOW


@pytest.mark.asyncio
async def test_list_sessions_orders_most_recently_active_first() -> None:
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_sessions(tenant_context())
    sql, _ = captured[-1]
    assert "ORDER BY max(created_at) DESC" in sql


@pytest.mark.asyncio
async def test_list_sessions_scopes_to_the_caller_and_passes_since_through() -> None:
    ctx = tenant_context()
    since = _NOW - datetime.timedelta(days=7)
    factory, captured = _read_capturing_factory([])
    await MemoryService(factory, clock=FakeClock(_NOW)).list_sessions(ctx, since=since)
    _, params = captured[-1]
    assert params["tid"] == ctx.tenant_id
    assert params["aid"] == ctx.actor_id
    assert params["since"] == since


# ---------------------------------------------------------------------------
# record_event: sequence-collision retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_event_retries_a_sequence_collision_and_succeeds_on_the_next_attempt() -> None:
    row = {"event_id": uuid.uuid4(), "seq": 3, "created_at": _NOW}
    factory, calls = _sequence_attempt_factory([_collision(), row])

    event = await MemoryService(factory, clock=FakeClock(_NOW)).record_event(
        tenant_context(), session_id="s1", kind="user_message", body="hi"
    )

    assert event.seq == 3
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_record_event_does_not_retry_a_non_unique_violation() -> None:
    other = _collision(sqlstate="23502")  # not-null violation, not the retried case
    factory, calls = _sequence_attempt_factory([other])

    with pytest.raises(IntegrityError):
        await MemoryService(factory, clock=FakeClock(_NOW)).record_event(
            tenant_context(), session_id="s1", kind="user_message", body="hi"
        )
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_record_event_gives_up_after_the_maximum_number_of_attempts() -> None:
    max_attempts = session_events_module._MAX_SEQ_ATTEMPTS
    factory, calls = _sequence_attempt_factory([_collision() for _ in range(max_attempts)])

    with pytest.raises(ValidationError, match="after"):
        await MemoryService(factory, clock=FakeClock(_NOW)).record_event(
            tenant_context(), session_id="s1", kind="user_message", body="hi"
        )
    assert calls["n"] == max_attempts


@pytest.mark.asyncio
async def test_record_event_validates_the_kind_before_touching_the_database() -> None:
    service = MemoryService(_refusing_factory(), clock=FakeClock(_NOW))
    with pytest.raises(ValidationError, match="unknown event kind"):
        await service.record_event(tenant_context(), session_id="s1", kind="not_a_real_kind", body="hi")


@pytest.mark.asyncio
async def test_record_event_requires_an_actor_before_touching_the_database() -> None:
    service = MemoryService(_refusing_factory(), clock=FakeClock(_NOW))
    ctx = TenantContext(tenant_id=uuid.uuid4(), actor_id=None, roles=["producer"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="requires an actor identity"):
        await service.record_event(ctx, session_id="s1", kind="user_message", body="hi")


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


def _service() -> MemoryService:
    return MemoryService(_refusing_factory(), clock=FakeClock(_NOW))


def test_validate_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValidationError, match="unknown event kind"):
        _service()._validate(kind="not_a_real_kind", body="hi", tool_name=None)


def test_validate_rejects_a_body_over_the_byte_cap() -> None:
    oversized = "x" * (MAX_BODY_BYTES + 1)
    with pytest.raises(ValidationError, match="exceeds"):
        _service()._validate(kind="user_message", body=oversized, tool_name=None)


def test_validate_requires_tool_name_for_a_tool_invocation() -> None:
    with pytest.raises(ValidationError, match="tool_name is required"):
        _service()._validate(kind="tool_invocation", body="ran it", tool_name=None)


def test_validate_forbids_tool_name_on_a_non_tool_invocation() -> None:
    with pytest.raises(ValidationError, match="not permitted"):
        _service()._validate(kind="user_message", body="hi", tool_name="grep")


def test_validate_returns_the_utf8_byte_size_not_the_character_count() -> None:
    # "é" is two bytes in UTF-8 -- a character count would under-report the cap.
    body = "é" * 3
    size = _service()._validate(kind="user_message", body=body, tool_name=None)
    assert size == len(body.encode("utf-8"))
    assert size == 6


def test_validate_accepts_a_well_formed_tool_invocation() -> None:
    """Positive control for the two tool_name pairing tests above."""
    size = _service()._validate(kind="tool_invocation", body="ran it", tool_name="grep")
    assert size == len(b"ran it")


# ---------------------------------------------------------------------------
# get_event / delete_event: existence-oracle avoidance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_event_raises_not_found_when_the_row_is_absent() -> None:
    factory, _ = _read_capturing_factory([])
    with pytest.raises(NotFoundError):
        await MemoryService(factory, clock=FakeClock(_NOW)).get_event(
            tenant_context(), session_id="s1", event_id=uuid.uuid4()
        )


@pytest.mark.asyncio
async def test_get_event_returns_the_event_when_present() -> None:
    row = _event_row()
    factory, _ = _read_capturing_factory([row])
    event = await MemoryService(factory, clock=FakeClock(_NOW)).get_event(
        tenant_context(), session_id="s1", event_id=row.event_id
    )
    assert event.event_id == row.event_id


@pytest.mark.asyncio
async def test_delete_event_raises_not_found_when_absent_already_deleted_or_someone_elses() -> None:
    """The three cases are reported identically by design -- distinguishing
    them would tell the caller something they are not entitled to know."""

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        result = MagicMock()
        result.one_or_none = MagicMock(return_value=None)
        return result

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())

    with pytest.raises(NotFoundError):
        await MemoryService(factory, clock=FakeClock(_NOW)).delete_event(
            tenant_context(), session_id="s1", event_id=uuid.uuid4()
        )


# ---------------------------------------------------------------------------
# erase_actor_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erase_actor_events_deletes_from_all_three_tables_scoped_to_tenant_and_actor() -> None:
    tenant_id, target_actor = uuid.uuid4(), uuid.uuid4()
    executed: list[tuple[str, dict]] = []

    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        executed.append((sql, params or {}))
        result = MagicMock()
        result.rowcount = 1
        return result

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())

    counts = await MemoryService(factory, clock=FakeClock(_NOW)).erase_actor_events(
        tenant_context(tenant_id=tenant_id), target_actor_id=target_actor
    )

    tables = {sql.split("FROM", 1)[1].split()[0] for sql, _ in executed}
    assert tables == {"memory_session_events", "memory_extraction_outbox", "memory_extraction_outbox_failed"}
    assert all(p["tid"] == tenant_id and p["aid"] == target_actor for _, p in executed)
    assert counts == {"session_events": 1, "extraction_queued": 1, "extraction_dead_lettered": 1}


@pytest.mark.asyncio
async def test_erase_actor_events_reports_zero_when_nothing_matches() -> None:
    async def _execute(stmt: Any, params: dict | None = None) -> MagicMock:
        result = MagicMock()
        result.rowcount = 0
        return result

    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = _execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())

    counts = await MemoryService(factory, clock=FakeClock(_NOW)).erase_actor_events(
        tenant_context(), target_actor_id=uuid.uuid4()
    )

    assert counts == {"session_events": 0, "extraction_queued": 0, "extraction_dead_lettered": 0}


# ---------------------------------------------------------------------------
# _page
# ---------------------------------------------------------------------------


def test_page_clamps_a_limit_at_or_below_zero_to_one() -> None:
    assert _page(0) == 1
    assert _page(-5) == 1


def test_page_clamps_a_limit_above_the_maximum() -> None:
    assert _page(MAX_PAGE + 500) == MAX_PAGE


def test_page_passes_an_in_range_limit_through_unchanged() -> None:
    assert _page(42) == 42
