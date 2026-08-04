"""Reading usage back, against a real database.

Most of this file is about two numbers that do not add up across days. Every other
field in a rollup is a sum, so a range is a `SUM()` and there is nothing to get
wrong. Distinct actors and latency percentiles are different, and the wrong answer
for both looks entirely plausible on a chart:

- Summing thirty days of `distinct_actors` counts a daily visitor thirty times.
- Averaging thirty daily p95s produces a number with no definition at all.

So the tests here mostly assert what the reads *refuse* to claim.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.usage import reads
from registry.usage.rollups import roll_up_day

_DAY = datetime.date(2026, 5, 14)
_TODAY = datetime.date(2026, 5, 20)


@pytest.fixture
async def factory(pg_container: str):
    engine = create_async_engine(pg_container)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _insert(factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID, rows: list[dict]) -> None:
    async with factory() as session:
        for row in rows:
            day = row.get("day", _DAY)
            await session.execute(
                text(
                    "INSERT INTO usage_events (event_id, occurred_at, tenant_id, actor_id, surface,"
                    " operation, outcome, status_class, latency_ms, subject_entity_ids, payload_bytes)"
                    " VALUES (:e,:o,:t,:a,:s,:op,:oc,:sc,:l,:se,:pb)"
                ),
                {
                    "e": uuid.uuid4(),
                    "o": datetime.datetime.combine(day, datetime.time(9, 0), tzinfo=datetime.UTC),
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


async def _roll(factory: async_sessionmaker[AsyncSession], days: list[datetime.date]) -> None:
    for day in days:
        await roll_up_day(factory, day)


# ---------------------------------------------------------------------------
# The two numbers that do not add up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_daily_visitor_is_one_person_and_several_actor_days(factory) -> None:
    """The wrong answer here is the inviting one.

    One actor calling on three consecutive days produces three rollup rows each
    saying `distinct_actors = 1`. Summing gives three, which is the count of
    actor-days and not of people. Both numbers are useful and they are not the same
    number, so both are returned under names that say which is which.
    """
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    days = [_DAY, _DAY + datetime.timedelta(days=1), _DAY + datetime.timedelta(days=2)]
    await _insert(factory, tenant, [{"actor": actor, "day": d} for d in days])
    await _roll(factory, days)

    summary = await reads.read_summary(
        factory,
        tenant_id=tenant,
        start=days[0],
        end=days[-1],
        retention_days=90,
        today=_TODAY,
    )

    (surface,) = summary.surfaces
    assert surface.calls == 3
    assert surface.actor_days == 3
    assert surface.distinct_actors == 1


@pytest.mark.asyncio
async def test_distinct_actors_is_null_with_a_reason_past_the_retention_boundary(factory) -> None:
    """Null rather than the sum, and null rather than zero.

    Past the boundary the raw rows are gone, so a true distinct count cannot be
    computed. Returning `actor_days` in its place would be up to thirty times too
    large for a month; returning zero would read as "nobody used it". A gap gets
    asked about, which is the only one of the three outcomes that leads somewhere.
    """
    tenant = uuid.uuid4()
    await _insert(factory, tenant, [{"actor": uuid.uuid4()}])
    await _roll(factory, [_DAY])

    # A 90-day retention with `today` far enough ahead that _DAY is outside it.
    summary = await reads.read_summary(
        factory,
        tenant_id=tenant,
        start=_DAY,
        end=_DAY,
        retention_days=90,
        today=_DAY + datetime.timedelta(days=200),
    )

    (surface,) = summary.surfaces
    assert surface.calls == 1, "the rollup itself must still answer"
    assert surface.distinct_actors is None
    assert surface.distinct_actors_unavailable_reason is not None
    assert "retention boundary" in surface.distinct_actors_unavailable_reason


@pytest.mark.asyncio
async def test_the_boundary_day_itself_counts_as_outside(factory) -> None:
    """Refused at the boundary, not one day inside it.

    The retention sweep runs hourly, so on the boundary day itself some of the
    window's rows have already gone and some have not. Answering would make the
    number depend on when in the hour the question was asked.
    """
    tenant = uuid.uuid4()
    await _insert(factory, tenant, [{"actor": uuid.uuid4()}])
    await _roll(factory, [_DAY])

    at_boundary = await reads.read_summary(
        factory, tenant_id=tenant, start=_DAY, end=_DAY, retention_days=90, today=_DAY + datetime.timedelta(days=90)
    )
    inside = await reads.read_summary(
        factory, tenant_id=tenant, start=_DAY, end=_DAY, retention_days=90, today=_DAY + datetime.timedelta(days=89)
    )

    assert at_boundary.surfaces[0].distinct_actors is None
    assert inside.surfaces[0].distinct_actors == 1


@pytest.mark.asyncio
async def test_the_summary_reports_the_worst_daily_p95_not_an_average(factory) -> None:
    """An average of percentiles is not a percentile of anything.

    With daily p95s of 10 and 100, the average is 55 — a figure describing no day
    that happened and no request that was served. The largest is 100, which is a
    latency something actually experienced.
    """
    tenant = uuid.uuid4()
    day_two = _DAY + datetime.timedelta(days=1)
    await _insert(factory, tenant, [{"actor": uuid.uuid4(), "day": _DAY, "latency": 10}])
    await _insert(factory, tenant, [{"actor": uuid.uuid4(), "day": day_two, "latency": 100}])
    await _roll(factory, [_DAY, day_two])

    summary = await reads.read_summary(
        factory, tenant_id=tenant, start=_DAY, end=day_two, retention_days=90, today=_TODAY
    )

    assert summary.surfaces[0].worst_daily_p95_ms == 100


# ---------------------------------------------------------------------------
# Ordinary aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_and_mcp_are_summarised_separately(factory) -> None:
    # The split that makes agent adoption visible at all.
    tenant = uuid.uuid4()
    await _insert(
        factory,
        tenant,
        [
            {"actor": uuid.uuid4(), "surface": "rest"},
            {"actor": uuid.uuid4(), "surface": "mcp", "operation": "search_capabilities"},
            {"actor": uuid.uuid4(), "surface": "mcp", "operation": "get_capability"},
        ],
    )
    await _roll(factory, [_DAY])

    summary = await reads.read_summary(
        factory, tenant_id=tenant, start=_DAY, end=_DAY, retention_days=90, today=_TODAY
    )

    by_surface = {s.surface: s for s in summary.surfaces}
    assert by_surface["rest"].calls == 1
    assert by_surface["mcp"].calls == 2


@pytest.mark.asyncio
async def test_another_tenants_usage_is_not_in_the_answer(factory) -> None:
    # Tenant scoping is structural here: the id comes from the request context and
    # no route accepts one, so there is no cross-tenant read to refuse.
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _insert(factory, mine, [{"actor": uuid.uuid4()}])
    await _insert(factory, theirs, [{"actor": uuid.uuid4()} for _ in range(5)])
    await _roll(factory, [_DAY])

    summary = await reads.read_summary(
        factory, tenant_id=mine, start=_DAY, end=_DAY, retention_days=90, today=_TODAY
    )

    assert summary.surfaces[0].calls == 1


@pytest.mark.asyncio
async def test_a_quiet_day_is_absent_from_the_series_rather_than_zero(factory) -> None:
    """A caller plotting a line has to be able to tell an outage from a Sunday.

    Zero-filling would erase that distinction, and the two call for opposite
    responses.
    """
    tenant = uuid.uuid4()
    third = _DAY + datetime.timedelta(days=2)
    await _insert(factory, tenant, [{"actor": uuid.uuid4(), "day": _DAY}])
    await _insert(factory, tenant, [{"actor": uuid.uuid4(), "day": third}])
    await _roll(factory, [_DAY, _DAY + datetime.timedelta(days=1), third])

    points = await reads.read_daily_series(factory, tenant_id=tenant, start=_DAY, end=third)

    assert [p.day for p in points] == [_DAY, third]


@pytest.mark.asyncio
async def test_the_series_can_be_narrowed_to_one_surface(factory) -> None:
    tenant = uuid.uuid4()
    await _insert(
        factory,
        tenant,
        [
            {"actor": uuid.uuid4(), "surface": "rest"},
            {"actor": uuid.uuid4(), "surface": "mcp", "operation": "get_capability"},
        ],
    )
    await _roll(factory, [_DAY])

    points = await reads.read_daily_series(factory, tenant_id=tenant, start=_DAY, end=_DAY, surface="mcp")

    assert [p.surface for p in points] == ["mcp"]


@pytest.mark.asyncio
async def test_an_unknown_surface_is_refused_rather_than_returning_nothing(factory) -> None:
    # An empty result for a typo'd filter reads as "no traffic on that surface",
    # which is a wrong answer rather than an error.
    with pytest.raises(ValueError, match="unknown surface"):
        await reads.read_daily_series(factory, tenant_id=uuid.uuid4(), start=_DAY, end=_DAY, surface="graphql")


@pytest.mark.asyncio
async def test_tools_are_ranked_by_calls(factory) -> None:
    tenant = uuid.uuid4()
    await _insert(
        factory,
        tenant,
        [{"actor": uuid.uuid4(), "surface": "mcp", "operation": "search_capabilities"} for _ in range(3)]
        + [{"actor": uuid.uuid4(), "surface": "mcp", "operation": "get_capability"}]
        + [{"actor": uuid.uuid4(), "surface": "rest", "operation": "/v1/capabilities"}],
    )
    await _roll(factory, [_DAY])

    tools = await reads.read_tool_rankings(factory, tenant_id=tenant, start=_DAY, end=_DAY)

    assert [(t.tool, t.calls) for t in tools] == [("search_capabilities", 3), ("get_capability", 1)]


@pytest.mark.asyncio
async def test_a_ranking_limit_takes_the_top_rows_not_arbitrary_ones(factory) -> None:
    tenant = uuid.uuid4()
    rows = []
    for i, count in enumerate([1, 5, 3]):
        rows += [{"actor": uuid.uuid4(), "surface": "mcp", "operation": f"tool_{i}"} for _ in range(count)]
    await _insert(factory, tenant, rows)
    await _roll(factory, [_DAY])

    tools = await reads.read_tool_rankings(factory, tenant_id=tenant, start=_DAY, end=_DAY, limit=2)

    assert [t.tool for t in tools] == ["tool_1", "tool_2"]


@pytest.mark.asyncio
async def test_capabilities_are_ranked_and_a_multi_entity_call_counts_for_each(factory) -> None:
    tenant = uuid.uuid4()
    hot, cold = uuid.uuid4(), uuid.uuid4()
    await _insert(
        factory,
        tenant,
        [
            {"actor": uuid.uuid4(), "entities": [hot, cold]},
            {"actor": uuid.uuid4(), "entities": [hot]},
        ],
    )
    await _roll(factory, [_DAY])

    caps = await reads.read_capability_rankings(factory, tenant_id=tenant, start=_DAY, end=_DAY)

    assert [(c.capability_id, c.calls) for c in caps] == [(hot, 2), (cold, 1)]


# ---------------------------------------------------------------------------
# Window validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_backwards_window_is_refused(factory) -> None:
    with pytest.raises(reads.InvalidRangeError):
        await reads.read_summary(
            factory,
            tenant_id=uuid.uuid4(),
            start=_DAY,
            end=_DAY - datetime.timedelta(days=1),
            retention_days=90,
            today=_TODAY,
        )


@pytest.mark.asyncio
async def test_a_window_wider_than_the_cap_is_refused(factory) -> None:
    """Refused rather than served slowly.

    Cost is linear in days, so one request asking for a deployment's whole history
    would discover the limit as a timeout — and a timeout does not say what to ask
    for instead.
    """
    with pytest.raises(reads.RangeTooWideError, match="exceeds"):
        await reads.read_summary(
            factory,
            tenant_id=uuid.uuid4(),
            start=_DAY,
            end=_DAY + datetime.timedelta(days=reads.MAX_RANGE_DAYS),
            retention_days=90,
            today=_TODAY,
        )


@pytest.mark.asyncio
async def test_the_widest_permitted_window_is_accepted(factory) -> None:
    # Boundary in the other direction: an off-by-one here would make the
    # documented maximum unusable.
    summary = await reads.read_summary(
        factory,
        tenant_id=uuid.uuid4(),
        start=_DAY,
        end=_DAY + datetime.timedelta(days=reads.MAX_RANGE_DAYS - 1),
        retention_days=90,
        today=_TODAY,
    )
    assert summary.days == reads.MAX_RANGE_DAYS
