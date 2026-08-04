"""What usage recording costs, and what reading it back costs.

Two budgets, both asserted against traffic the service actually serves:

  * recording adds ≤ 0.5 ms at p99 to a request;
  * an aggregate read over 90 days answers at p95 ≤ 300 ms.

The first is measured as a **delta** against the same app without recording. An
absolute latency assertion would measure the runner rather than the change: a slow
machine fails a correct implementation and a fast one passes a regression. The delta
is what the budget is about.

The second is measured against a rollup table holding the window under test for
*forty other tenants as well*, because the index the read depends on is
`(tenant_id, day DESC)` and a table with one occupant answers just as fast with no
index at all. Raw events are seeded too: the summary's second query counts distinct
actors from `usage_events`, and against an empty table that half of the read costs
nothing and the budget would be met by measuring the cheap half.

Every measurement here asserts that it measured something — an enqueue count, a
non-null headcount, a non-empty ranking. Two of those assertions have already earned
their place: the enqueue count caught a harness where Starlette silently replaced
`scope["app"]`, so 2200 requests recorded nothing and the delta read as ~0 ms.
"""

from __future__ import annotations

import datetime
import statistics
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from registry.api.middleware.metrics import MetricsMiddleware
from registry.usage import reads
from registry.usage.identity import _REQUEST_ATTR, UsageIdentity
from registry.usage.writer import DEFAULT_BATCH_SIZE, UsageEvent, UsageWriter

pytestmark = [pytest.mark.perf, pytest.mark.slow]

_ITERATIONS = 2000
_WARMUP = 200

#: The recording budget, restated here so a failure names the number it broke.
_MAX_ADDED_P99_MS = 0.5

#: The aggregate-read budget, over the window the requirement names.
_MAX_READ_P95_MS = 300.0
_WINDOW_DAYS = 90

#: Other tenants' rows in the rollup table. Without them the read would be
#: measured against a table it is the sole occupant of, where a sequential scan is
#: as fast as an index lookup and the test proves nothing about either.
_OTHER_TENANTS = 40


def _pct(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    return ordered[min(int(len(ordered) * pct), len(ordered) - 1)]


# ---------------------------------------------------------------------------
# Recording overhead
# ---------------------------------------------------------------------------


class _NullWriter(UsageWriter):
    """A real writer whose queue is never drained.

    Real, because `record_rest_usage` isinstance-checks what it finds on app state
    and a stand-in would be skipped — measuring an app that records nothing and
    reporting it as the cost of recording. Never drained, because the drain runs off
    the request path and including it would measure the wrong thing; what a request
    pays is the enqueue.

    A queue large enough for the whole run, so no iteration hits the full-buffer
    branch. That branch is *cheaper* than the enqueue, so letting the queue fill
    partway through would flatter the result.
    """

    def __init__(self) -> None:
        super().__init__(session_factory=None, max_queue=_ITERATIONS + _WARMUP + 1000)  # type: ignore[arg-type]


def _asgi(writer: object | None) -> object:
    """The metrics middleware over a trivial handler, with or without a writer.

    Recording is reached only when a writer is present *and* identity was stashed,
    so the two variants differ by exactly the work under measurement. The middleware
    itself is in both, which keeps the operational instrumentation out of the delta —
    that cost was measured when it was added.

    The writer goes on the Starlette app's own `state`, which is where production
    puts it. Attaching it to a stand-in object and setting `scope["app"]` does not
    work: Starlette overwrites `scope["app"]` with itself on the way in, so the
    recorder looks for the writer on the real app and finds nothing. That failure is
    silent — recording is wrapped in a bare `except` so it can never break a request
    — and it is exactly what the enqueue-count assertion below exists to catch.
    """
    identity = UsageIdentity(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4())

    async def handler(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/v1/thing", handler)])
    if writer is not None:
        inner.state.usage_writer = writer

    async def wrapped(scope: dict, receive: object, send: object) -> None:
        if scope.get("type") == "http":
            scope.setdefault("state", {})
            if writer is not None:
                scope["state"][_REQUEST_ATTR] = identity
        await MetricsMiddleware(inner)(scope, receive, send)  # type: ignore[arg-type]

    return wrapped


async def _timings(app: object) -> list[float]:
    samples: list[float] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:  # type: ignore[arg-type]
        for _ in range(_WARMUP):
            await client.get("/v1/thing")
        for _ in range(_ITERATIONS):
            started = time.perf_counter()
            await client.get("/v1/thing")
            samples.append((time.perf_counter() - started) * 1000.0)
    return samples


@pytest.mark.asyncio
async def test_recording_adds_under_half_a_millisecond_at_p99() -> None:
    """The number the whole buffered-writer design exists to hit.

    If recording cost a request more than this there would be no honest way to keep
    it on the request path at all, and the alternative — sampling — would make every
    count an estimate.
    """
    writer = _NullWriter()

    without = await _timings(_asgi(None))
    with_recording = await _timings(_asgi(writer))

    added_p99 = _pct(with_recording, 0.99) - _pct(without, 0.99)
    added_p50 = statistics.median(with_recording) - statistics.median(without)

    # Proof the measurement measured something: an app that recorded nothing would
    # produce a flattering delta and an empty queue.
    assert writer._queue.qsize() == _ITERATIONS + _WARMUP, (
        f"only {writer._queue.qsize()} events were enqueued out of "
        f"{_ITERATIONS + _WARMUP} requests — the delta above is not the cost of recording"
    )

    print(
        f"\nwithout recording p50={statistics.median(without):.4f}ms p99={_pct(without, 0.99):.4f}ms"
        f"\nwith recording    p50={statistics.median(with_recording):.4f}ms "
        f"p99={_pct(with_recording, 0.99):.4f}ms"
        f"\nadded             p50={added_p50:.4f}ms p99={added_p99:.4f}ms"
    )

    assert (
        added_p99 <= _MAX_ADDED_P99_MS
    ), f"usage recording adds {added_p99:.4f}ms at p99, over the {_MAX_ADDED_P99_MS}ms budget"


@pytest.mark.asyncio
async def test_a_full_buffer_does_not_cost_the_request_more() -> None:
    """The overload case, which is when the budget matters most.

    A full queue is the state under exactly the load where an extra millisecond per
    request compounds. `put_nowait` raising and being caught must not be slower than
    the enqueue it replaces, or shedding usage data would make the incident worse.
    """
    tiny = UsageWriter(session_factory=None, max_queue=1)  # type: ignore[arg-type]
    tiny.record(
        UsageEvent(
            occurred_at=datetime.datetime.now(tz=datetime.UTC),
            tenant_id=uuid.uuid4(),
            actor_id=None,
            surface="rest",
            operation="/v1/thing",
            outcome="ok",
            status_class="2xx",
            latency_ms=1,
        )
    )
    assert tiny._queue.full(), "the fixture did not fill the queue"

    without = await _timings(_asgi(None))
    saturated = await _timings(_asgi(tiny))

    added_p99 = _pct(saturated, 0.99) - _pct(without, 0.99)
    print(f"\nsaturated-buffer added p99={added_p99:.4f}ms")

    assert (
        added_p99 <= _MAX_ADDED_P99_MS
    ), f"a saturated buffer adds {added_p99:.4f}ms at p99, over the {_MAX_ADDED_P99_MS}ms budget"


# ---------------------------------------------------------------------------
# Aggregate read latency
# ---------------------------------------------------------------------------


async def _seed_rollups(
    factory: async_sessionmaker,
    tenant: uuid.UUID,
    end: datetime.date,
    days: int = _WINDOW_DAYS,
) -> None:
    """`days` × 2 surfaces for this tenant, and the same for 40 others.

    The other tenants are the point. `(tenant_id, day DESC)` is the index the read
    depends on, and a table holding one tenant's rows would answer just as fast
    without it — so the test would pass with the index dropped.
    """
    tenants = [tenant, *(uuid.uuid4() for _ in range(_OTHER_TENANTS))]
    rows = []
    for tid in tenants:
        for offset in range(days):
            day = end - datetime.timedelta(days=offset)
            for surface in ("rest", "mcp"):
                rows.append(
                    {
                        "t": tid,
                        "d": day,
                        "s": surface,
                        "c": 1000 + offset,
                        "ok": 990 + offset,
                        "err": 10,
                        "da": 25,
                        "p50": 12,
                        "p95": 40,
                        "p99": 90,
                        "pb": 4096,
                    }
                )

    async with factory() as session:
        for chunk_start in range(0, len(rows), 500):
            await session.execute(
                text(
                    "INSERT INTO usage_rollup_tenant_day (tenant_id, day, surface, calls, ok_calls,"
                    " error_calls, distinct_actors, p50_ms, p95_ms, p99_ms, payload_bytes)"
                    " VALUES (:t,:d,:s,:c,:ok,:err,:da,:p50,:p95,:p99,:pb)"
                    " ON CONFLICT (tenant_id, day, surface) DO NOTHING"
                ),
                rows[chunk_start : chunk_start + 500],
            )
        await session.commit()
        await session.execute(text("ANALYZE usage_rollup_tenant_day"))


async def _seed_raw(factory: async_sessionmaker, tenant: uuid.UUID, end: datetime.date, days: int) -> None:
    """Raw events across the window, for this tenant and a few others.

    Needed because the summary's second query — the true distinct-actor count — runs
    against `usage_events`, not the rollups. Seeding only rollups leaves that query
    scanning an empty table, so the measurement would omit the more expensive half of
    the read and report the cheap half as the whole cost.
    """
    tenants = [tenant, *(uuid.uuid4() for _ in range(5))]
    actors = [uuid.uuid4() for _ in range(60)]
    for tid in tenants:
        for offset in range(days):
            day = end - datetime.timedelta(days=offset)
            occurred = datetime.datetime.combine(day, datetime.time(9, 0), tzinfo=datetime.UTC)
            async with factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO usage_events (event_id, occurred_at, tenant_id, actor_id,"
                        " surface, operation, outcome, status_class, latency_ms)"
                        " VALUES (:e,:o,:t,:a,'rest','/v1/capabilities','ok','2xx',:l)"
                    ),
                    [
                        {
                            "e": uuid.uuid4(),
                            "o": occurred,
                            "t": tid,
                            "a": actors[i % len(actors)],
                            "l": 10 + (i % 40),
                        }
                        for i in range(50)
                    ],
                )
                await session.commit()
    async with factory() as session:
        await session.execute(text("ANALYZE usage_events"))
        await session.commit()


@pytest.mark.asyncio
async def test_a_ninety_day_aggregate_read_stays_under_the_latency_budget(pg_container: str) -> None:
    """The read the console makes on every page load, at the window the budget names.

    Measured against the summary rather than the series because the summary is the
    expensive one: it reads the rollups *and* counts distinct actors from raw rows.
    Both halves are seeded, and the assertion that `distinct_actors` came back
    non-null is what proves the second half actually ran — with an empty
    `usage_events` it would return zero in microseconds and the budget would be met
    by measuring nothing.
    """
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = uuid.uuid4()
    end = datetime.date(2026, 5, 20)
    start = end - datetime.timedelta(days=_WINDOW_DAYS - 1)

    try:
        await _seed_rollups(factory, tenant, end)
        await _seed_raw(factory, tenant, end, _WINDOW_DAYS)

        # Warm the connection pool and the plan cache; the first call pays for both
        # and neither is what a steady-state read costs.
        for _ in range(3):
            await reads.read_summary(factory, tenant_id=tenant, start=start, end=end, retention_days=90, today=end)

        samples: list[float] = []
        for _ in range(30):
            began = time.perf_counter()
            summary = await reads.read_summary(
                factory, tenant_id=tenant, start=start, end=end, retention_days=90, today=end
            )
            samples.append((time.perf_counter() - began) * 1000.0)

        # The read must have read something, or this measures an empty table.
        assert summary.days == _WINDOW_DAYS
        assert {s.surface for s in summary.surfaces} == {"rest", "mcp"}
        assert all(s.calls > 0 for s in summary.surfaces)
        # And the expensive half must have run. Null here would mean the window was
        # judged past retention and the raw count skipped, which is a different and
        # much cheaper query than the one this budget is about.
        rest = next(s for s in summary.surfaces if s.surface == "rest")
        assert (
            rest.distinct_actors is not None and rest.distinct_actors > 0
        ), "the distinct-actor count was skipped or empty — this measured only the rollup read"

        p95 = _pct(samples, 0.95)
        print(
            f"\n{_WINDOW_DAYS}-day summary over {(_OTHER_TENANTS + 1) * _WINDOW_DAYS * 2} rollup rows "
            f"and {6 * _WINDOW_DAYS * 50} raw rows ({rest.distinct_actors} distinct actors): "
            f"p50={statistics.median(samples):.2f}ms p95={p95:.2f}ms"
        )

        assert p95 <= _MAX_READ_P95_MS, (
            f"a {_WINDOW_DAYS}-day aggregate read takes {p95:.2f}ms at p95, " f"over the {_MAX_READ_P95_MS}ms budget"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_widest_permitted_window_also_answers_in_budget(pg_container: str) -> None:
    """The cap exists because cost is linear in days, so the cap is what to measure.

    A budget verified only at 90 days says nothing about the 400 the API will accept,
    and the day someone asks for 400 is the day they find out.

    Seeded with 400 days of rollups, so the scan is genuinely 400 days wide. The raw
    distinct-actor query is *not* exercised here and cannot be: retention tops out at
    180 days, so a window this wide always reaches past the boundary and always
    returns a null headcount. The 90-day test above is the one that measures both
    halves.
    """
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = uuid.uuid4()
    end = datetime.date(2026, 5, 20)

    try:
        await _seed_rollups(factory, tenant, end, days=reads.MAX_RANGE_DAYS)

        widest_start = end - datetime.timedelta(days=reads.MAX_RANGE_DAYS - 1)
        for _ in range(3):
            await reads.read_summary(
                factory, tenant_id=tenant, start=widest_start, end=end, retention_days=90, today=end
            )

        samples: list[float] = []
        summary = None
        for _ in range(20):
            began = time.perf_counter()
            summary = await reads.read_summary(
                factory, tenant_id=tenant, start=widest_start, end=end, retention_days=90, today=end
            )
            samples.append((time.perf_counter() - began) * 1000.0)

        assert summary is not None
        assert summary.days == reads.MAX_RANGE_DAYS
        # Every seeded day is in range, so the scan really was this wide.
        assert all(s.calls > 0 for s in summary.surfaces), "the widest window read nothing"
        assert (
            summary.surfaces[0].distinct_actors is None
        ), "a window wider than any permitted retention must report no headcount"

        p95 = _pct(samples, 0.95)
        print(
            f"\n{reads.MAX_RANGE_DAYS}-day summary over "
            f"{(_OTHER_TENANTS + 1) * reads.MAX_RANGE_DAYS * 2} rollup rows: p95={p95:.2f}ms"
        )

        assert (
            p95 <= _MAX_READ_P95_MS
        ), f"the widest permitted window takes {p95:.2f}ms at p95, over the {_MAX_READ_P95_MS}ms budget"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_rankings_stay_in_budget_too(pg_container: str) -> None:
    """Both rankings sort a grouped aggregate, which the summary does not.

    A budget met by the summary does not carry over to a query with an ORDER BY over
    a GROUP BY, and the tool ranking is on the same console page.
    """
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = uuid.uuid4()
    end = datetime.date(2026, 5, 20)
    start = end - datetime.timedelta(days=_WINDOW_DAYS - 1)

    try:
        # Tool and capability rollups, seeded directly: many distinct keys, because a
        # ranking over three rows sorts nothing.
        tools = [f"tool_{i}" for i in range(40)]
        caps = [uuid.uuid4() for _ in range(200)]
        async with factory() as session:
            for offset in range(_WINDOW_DAYS):
                day = end - datetime.timedelta(days=offset)
                await session.execute(
                    text(
                        "INSERT INTO usage_rollup_tool_day (tenant_id, day, tool, calls, ok_calls,"
                        " error_calls, distinct_actors, p50_ms, p95_ms, p99_ms)"
                        " VALUES (:t,:d,:tool,:c,:c,0,5,10,20,30)"
                        " ON CONFLICT (tenant_id, day, tool) DO NOTHING"
                    ),
                    [{"t": tenant, "d": day, "tool": name, "c": 10 + i} for i, name in enumerate(tools)],
                )
                await session.execute(
                    text(
                        "INSERT INTO usage_rollup_capability_day (tenant_id, day, capability_id,"
                        " calls, distinct_actors) VALUES (:t,:d,:cap,:c,3)"
                        " ON CONFLICT (tenant_id, day, capability_id) DO NOTHING"
                    ),
                    [{"t": tenant, "d": day, "cap": cap, "c": 5 + i} for i, cap in enumerate(caps)],
                )
            await session.commit()
            await session.execute(text("ANALYZE usage_rollup_tool_day"))
            await session.execute(text("ANALYZE usage_rollup_capability_day"))

        async def measure(fn) -> tuple[float, int]:
            for _ in range(3):
                await fn(factory, tenant_id=tenant, start=start, end=end)
            samples: list[float] = []
            for _ in range(20):
                began = time.perf_counter()
                rows = await fn(factory, tenant_id=tenant, start=start, end=end)
                samples.append((time.perf_counter() - began) * 1000.0)
            return _pct(samples, 0.95), len(rows)

        tool_p95, tool_rows = await measure(reads.read_tool_rankings)
        cap_p95, cap_rows = await measure(reads.read_capability_rankings)

        print(f"\ntool ranking p95={tool_p95:.2f}ms  capability ranking p95={cap_p95:.2f}ms")

        assert tool_rows > 0 and cap_rows > 0, "the rankings returned nothing — this measured an empty table"
        assert tool_p95 <= _MAX_READ_P95_MS, f"tool ranking takes {tool_p95:.2f}ms at p95"
        assert cap_p95 <= _MAX_READ_P95_MS, f"capability ranking takes {cap_p95:.2f}ms at p95"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_flush_moves_a_full_batch_without_stalling_the_loop(pg_container: str) -> None:
    """The drain is off the request path, but it shares the event loop.

    A flush that blocked for a long time would delay every coroutine on that loop,
    including request handling — so the thing that matters is not the flush's own
    speed but that one flush is bounded. Measured at the configured batch size.
    """
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = uuid.uuid4()

    try:
        writer = UsageWriter(factory)
        now = datetime.datetime.now(tz=datetime.UTC)
        for i in range(DEFAULT_BATCH_SIZE):
            writer.record(
                UsageEvent(
                    occurred_at=now,
                    tenant_id=tenant,
                    actor_id=uuid.uuid4(),
                    surface="rest",
                    operation="/v1/thing",
                    outcome="ok",
                    status_class="2xx",
                    latency_ms=i % 50,
                )
            )

        began = time.perf_counter()
        await writer._flush_once()
        elapsed_ms = (time.perf_counter() - began) * 1000.0

        async with factory() as session:
            written = int(
                (
                    await session.execute(text("SELECT count(*) FROM usage_events WHERE tenant_id = :t"), {"t": tenant})
                ).scalar_one()
            )

        print(f"\nflush of {DEFAULT_BATCH_SIZE} events: {elapsed_ms:.2f}ms ({written} rows written)")

        assert written == DEFAULT_BATCH_SIZE, f"only {written} of {DEFAULT_BATCH_SIZE} events were written"
        # Generous, and deliberately so: the number that matters is that a single
        # flush is bounded and small relative to the one-second interval, not that it
        # hits a tight figure on a shared runner.
        assert elapsed_ms <= 1000.0, (
            f"one flush of {DEFAULT_BATCH_SIZE} events took {elapsed_ms:.0f}ms, which is longer "
            "than the interval between flushes — the queue would grow without bound"
        )
    finally:
        await engine.dispose()
