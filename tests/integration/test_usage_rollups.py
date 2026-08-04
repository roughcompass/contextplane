"""The rollups, against a real database — because every claim here is SQL.

Percentiles, `count(DISTINCT)`, `unnest` over a UUID array, and an upsert across
three grains in one transaction. None of that can be shown correct with a mock: a
mock proves the function was called, and what matters is what Postgres computed.

The property the whole retention design rests on is the last test in this file: an
aggregate with no actor identifier is not personal data, so it needs no boundary and
no erasure — and if an actor column ever appeared here, deleting a person would
start rewriting numbers that had already been quoted.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.usage.rollups import roll_up_day

_DAY = datetime.date(2026, 5, 14)
_START = datetime.datetime.combine(_DAY, datetime.time(9, 0), tzinfo=datetime.UTC)


@pytest.fixture
async def session_factory(pg_container: str):
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert(factory, tenant, rows) -> None:
    async with factory() as session:
        for row in rows:
            await session.execute(
                text(
                    "INSERT INTO usage_events (event_id, occurred_at, tenant_id, actor_id, surface,"
                    " operation, outcome, status_class, latency_ms, subject_entity_ids, payload_bytes)"
                    " VALUES (:e,:o,:t,:a,:s,:op,:oc,:sc,:l,:se,:pb)"
                ),
                {
                    "e": uuid.uuid4(),
                    "o": row.get("at", _START),
                    "t": tenant,
                    "a": row.get("actor"),
                    "s": row.get("surface", "rest"),
                    "op": row.get("operation", "/v1/capabilities"),
                    "oc": row.get("outcome", "ok"),
                    "sc": row.get("status", "2xx"),
                    "l": row.get("latency", 10),
                    "se": row.get("entities", []),
                    "pb": row.get("bytes"),
                },
            )
        await session.commit()


async def _fetch(factory, table: str, tenant) -> list[dict]:
    async with factory() as session:
        rows = (
            (await session.execute(text(f"SELECT * FROM {table} WHERE tenant_id = :t ORDER BY 1, 2, 3"), {"t": tenant}))
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


@pytest.mark.asyncio
async def test_a_day_rolls_up_counts_outcomes_and_percentiles(session_factory) -> None:
    tenant = uuid.uuid4()
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    await _insert(
        session_factory,
        tenant,
        [
            {"actor": a1, "latency": 10, "bytes": 100},
            {"actor": a1, "latency": 20, "bytes": 100},
            {"actor": a2, "latency": 30, "bytes": 100},
            {"actor": a2, "latency": 40, "outcome": "error", "status": "5xx"},
        ],
    )

    await roll_up_day(session_factory, _DAY)
    (row,) = await _fetch(session_factory, "usage_rollup_tenant_day", tenant)

    assert row["calls"] == 4
    assert row["ok_calls"] == 3
    assert row["error_calls"] == 1
    # Two actors, four calls. The count is the point: it answers "how many people"
    # without storing which people.
    assert row["distinct_actors"] == 2
    assert row["p50_ms"] == 20
    assert row["p99_ms"] == 40
    assert row["payload_bytes"] == 300


@pytest.mark.asyncio
async def test_rolling_the_same_day_twice_changes_nothing(session_factory) -> None:
    """Idempotence, which the schedule depends on.

    The worker rolls yesterday *and* today on every hourly pass, so a day is
    recomputed many times. An upsert rather than delete-then-insert also means a
    reader mid-rerun sees stale data rather than a hole — and the read API cannot
    tell a hole from a genuine zero.
    """
    tenant = uuid.uuid4()
    await _insert(session_factory, tenant, [{"actor": uuid.uuid4()} for _ in range(3)])

    await roll_up_day(session_factory, _DAY)
    first = await _fetch(session_factory, "usage_rollup_tenant_day", tenant)
    await roll_up_day(session_factory, _DAY)
    second = await _fetch(session_factory, "usage_rollup_tenant_day", tenant)

    assert len(first) == len(second) == 1
    assert first[0]["calls"] == second[0]["calls"] == 3


@pytest.mark.asyncio
async def test_rest_and_mcp_are_separate_rows(session_factory) -> None:
    # The split that makes agent adoption visible at all. Collapsing it would make
    # the single most interesting question about this product unanswerable.
    tenant = uuid.uuid4()
    await _insert(
        session_factory,
        tenant,
        [
            {"actor": uuid.uuid4(), "surface": "rest"},
            {"actor": uuid.uuid4(), "surface": "mcp", "operation": "search_capabilities"},
        ],
    )
    await roll_up_day(session_factory, _DAY)
    rows = await _fetch(session_factory, "usage_rollup_tenant_day", tenant)
    assert {r["surface"] for r in rows} == {"rest", "mcp"}


@pytest.mark.asyncio
async def test_only_mcp_calls_reach_the_tool_rollup(session_factory) -> None:
    """The tool grain must not absorb REST route templates.

    Both live in the `operation` column, so a missing surface filter would put
    `/v1/capabilities` in a table whose column is named `tool` — and the top-tools
    ranking would be topped by something that is not a tool.
    """
    tenant = uuid.uuid4()
    await _insert(
        session_factory,
        tenant,
        [
            {"actor": uuid.uuid4(), "surface": "rest", "operation": "/v1/capabilities"},
            {"actor": uuid.uuid4(), "surface": "mcp", "operation": "get_capability"},
        ],
    )
    await roll_up_day(session_factory, _DAY)
    rows = await _fetch(session_factory, "usage_rollup_tool_day", tenant)
    assert [r["tool"] for r in rows] == ["get_capability"]


@pytest.mark.asyncio
async def test_a_call_touching_two_entities_counts_for_both(session_factory) -> None:
    # One call can concern several capabilities — a blast-radius query touches
    # many — and each should show the call.
    tenant = uuid.uuid4()
    cap_a, cap_b = uuid.uuid4(), uuid.uuid4()
    await _insert(session_factory, tenant, [{"actor": uuid.uuid4(), "entities": [cap_a, cap_b]}])

    await roll_up_day(session_factory, _DAY)
    rows = await _fetch(session_factory, "usage_rollup_capability_day", tenant)

    assert {r["capability_id"] for r in rows} == {cap_a, cap_b}
    assert all(r["calls"] == 1 for r in rows)


@pytest.mark.asyncio
async def test_events_outside_the_day_are_not_included(session_factory) -> None:
    # A whole UTC day, half-open. Getting the boundary wrong would double-count
    # midnight in two adjacent days, which is invisible in a total and obvious in a
    # month-on-month comparison.
    tenant = uuid.uuid4()

    def at(day: datetime.date, time: datetime.time) -> datetime.datetime:
        return datetime.datetime.combine(day, time, tzinfo=datetime.UTC)

    await _insert(
        session_factory,
        tenant,
        [
            {"actor": uuid.uuid4(), "at": at(_DAY, datetime.time.min)},
            {"actor": uuid.uuid4(), "at": at(_DAY, datetime.time.max)},
            {"actor": uuid.uuid4(), "at": at(_DAY + datetime.timedelta(days=1), datetime.time.min)},
        ],
    )
    await roll_up_day(session_factory, _DAY)
    (row,) = await _fetch(session_factory, "usage_rollup_tenant_day", tenant)
    assert row["calls"] == 2


@pytest.mark.asyncio
async def test_an_unauthenticated_call_counts_but_adds_no_actor(session_factory) -> None:
    """A null actor is a call without an identity, not an identity of its own.

    `count(DISTINCT actor_id)` ignores nulls, which is the behaviour wanted: the
    call is real and counted, and it does not inflate the distinct-actor number
    that the cohort metrics are built on.
    """
    tenant = uuid.uuid4()
    await _insert(session_factory, tenant, [{"actor": None}, {"actor": None}])

    await roll_up_day(session_factory, _DAY)
    (row,) = await _fetch(session_factory, "usage_rollup_tenant_day", tenant)

    assert row["calls"] == 2
    assert row["distinct_actors"] == 0


@pytest.mark.asyncio
async def test_no_rollup_table_has_an_actor_column(session_factory) -> None:
    """The property the entire retention design rests on.

    An aggregate with no actor identifier is not personal data: no retention
    boundary, no erasure obligation, and a right-to-be-forgotten request cannot
    change a number that has already been quoted. Asserted against the live schema
    rather than the migration text, because what matters is the table that exists.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns"
                    " WHERE table_name LIKE 'usage_rollup%'"
                )
            )
        ).all()

    assert rows, "no rollup tables found — the migration did not apply"
    offenders = [(t, c) for t, c in rows if "actor" in c and c != "distinct_actors"]
    assert not offenders, f"rollup tables must not identify actors: {offenders}"
