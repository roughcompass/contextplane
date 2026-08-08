"""The two claims that decide whether this subsystem is real rather than present.

Everything else about usage recording can be true while these are false, and if they
are false the subsystem is a liability instead of a feature:

1. **Recording never costs a caller anything, even when it is broken.** Kill the
   writer under load and every request still succeeds. What is lost is counted, not
   silently dropped, because a usage number nobody knows is incomplete is worse than
   no number.

2. **A closed month's aggregate outlives its raw rows.** Drop the partition and the
   answer does not change. That is the whole justification for a retention boundary:
   the liability goes and the analysis stays.

Both are asserted against a real database, and the second against a real
`DETACH PARTITION` — the operator procedure, not a simulation of it.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.usage import reads
from contextplane.usage.rollups import roll_up_day
from contextplane.usage.writer import UsageEvent, UsageWriter
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

type _Harness = tuple[EntitlementAuthHarness, AsyncClient]

#: A month whose partition exists and which no other test writes to, so detaching it
#: cannot take another test's rows with it.
_CLOSED_MONTH = datetime.date(2025, 3, 1)
_PARTITION = "usage_events_2025_03"


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[_Harness]:
    async with EntitlementAuthHarness(pg_container) as app_harness:
        transport = ASGITransport(app=app_harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield app_harness, client


async def _materialise(harness: EntitlementAuthHarness, persona: TenantPersona) -> uuid.UUID:
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
            assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["tenant_id"])


def _drops() -> float:
    value = REGISTRY.get_sample_value("registry_worker_dead_lettered_total", {"queue": "usage_events"})
    return value or 0.0


# ---------------------------------------------------------------------------
# 1. A dead writer must not cost a caller anything
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_killing_the_writer_under_load_fails_no_request_and_counts_the_loss(
    harness: _Harness,
) -> None:
    """The invariant the whole buffered design exists to protect.

    Recording is instrumentation. If it can fail a request then adding it made the
    service less reliable than before, and the correct response would be to remove it.

    Driven through the real app with real authentication, because the failure this
    guards against is a `record` call raising inside the middleware's `finally` — and
    that only happens on the real path.

    The writer is replaced with one whose queue holds five events, so the buffer
    saturates within a handful of requests instead of ten thousand. Then it is stopped
    outright: no drain, nothing to relieve the queue, exactly the state a crashed
    flush task leaves behind.
    """
    app_harness, client = harness
    persona = app_harness.add_persona(f"kill-writer-{uuid.uuid4().hex[:6]}", roles=["admin"])
    await _materialise(app_harness, persona)

    factory = app_harness.app.state.session_factory
    crippled = UsageWriter(factory, max_queue=5)
    await crippled.start()
    app_harness.app.state.usage_writer = crippled

    async def hit() -> int:
        app_harness.configure_fetcher_for(persona)
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
        return resp.status_code

    # Healthy: a few requests while the drain is alive.
    assert [await hit() for _ in range(3)] == [200, 200, 200]

    # Now kill it mid-load. `stop()` cancels the drain; the queue is never emptied
    # again, so every subsequent event has nowhere to go.
    await crippled.stop()
    drops_before = _drops()

    statuses = [await hit() for _ in range(25)]

    assert set(statuses) == {200}, f"a request failed after the writer died: {sorted(set(statuses))}"
    assert _drops() > drops_before, (
        "events were lost without being counted — an incomplete usage number that "
        "nobody knows is incomplete is worse than no number at all"
    )


@pytest.mark.asyncio
async def test_an_unreachable_database_fails_no_request_either(harness: _Harness) -> None:
    """The other way the writer dies, and the one that arrives without warning.

    A flush against a database that is away must drop its batch and count it, not
    retry forever behind a growing queue. Distinct from the case above: there the
    drain was gone, here it is running and failing.
    """
    app_harness, client = harness
    persona = app_harness.add_persona(f"dead-db-{uuid.uuid4().hex[:6]}", roles=["admin"])
    await _materialise(app_harness, persona)

    unreachable = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    try:
        writer = UsageWriter(
            async_sessionmaker(unreachable, expire_on_commit=False),
            flush_interval_s=0.05,
        )
        await writer.start()
        app_harness.app.state.usage_writer = writer

        drops_before = _drops()
        statuses = []
        for _ in range(10):
            app_harness.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
            statuses.append(resp.status_code)

        # `stop()` must survive a final flush against a database that is away. It
        # runs first in the app's shutdown `finally`, so raising here would skip the
        # scheduler shutdown and both HTTP clients that follow it — leaking three
        # resources because some non-authoritative rows could not be written.
        await writer.stop()

        assert set(statuses) == {200}, f"a request failed while the usage database was away: {statuses}"
        assert _drops() > drops_before, "a failed flush lost its batch without counting it"
    finally:
        await unreachable.dispose()


# ---------------------------------------------------------------------------
# 2. A closed month outlives its raw rows
# ---------------------------------------------------------------------------


async def _seed_closed_month(factory: async_sessionmaker, tenant: uuid.UUID) -> list[datetime.date]:
    """Three days of traffic inside the month whose partition will be dropped."""
    days = [_CLOSED_MONTH, _CLOSED_MONTH + datetime.timedelta(days=1), _CLOSED_MONTH + datetime.timedelta(days=2)]
    actors = [uuid.uuid4(), uuid.uuid4()]
    async with factory() as session:
        for day in days:
            for i in range(6):
                await session.execute(
                    text(
                        "INSERT INTO usage_events (event_id, occurred_at, tenant_id, actor_id, surface,"
                        " operation, outcome, status_class, latency_ms, payload_bytes)"
                        " VALUES (:e,:o,:t,:a,'rest','/v1/capabilities',:oc,:sc,:l,:pb)"
                    ),
                    {
                        "e": uuid.uuid4(),
                        "o": datetime.datetime.combine(day, datetime.time(9, i), tzinfo=datetime.UTC),
                        "t": tenant,
                        "a": actors[i % 2],
                        "oc": "error" if i == 5 else "ok",
                        "sc": "5xx" if i == 5 else "2xx",
                        "l": 10 + i * 5,
                        "pb": 200,
                    },
                )
        await session.commit()
    for day in days:
        await roll_up_day(factory, day)
    return days


@pytest.mark.asyncio
async def test_a_closed_months_aggregate_is_identical_after_its_partition_is_dropped(
    pg_container: str,
) -> None:
    """The claim that makes the retention boundary affordable.

    If dropping a month's raw rows changed the month's reported numbers, then either
    the rows could never be dropped — an unbounded personal-data store — or every
    historical figure would be provisional. Neither is acceptable, so the aggregate
    has to be genuinely independent of the rows it came from.

    Read past the retention boundary, which is the only time an operator would detach
    a partition. Inside the boundary the summary also counts distinct actors from raw
    rows, and that number legitimately cannot survive their deletion — the next test
    pins that difference down rather than leaving it to be discovered.
    """
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = uuid.uuid4()

    try:
        days = await _seed_closed_month(factory, tenant)
        start, end = days[0], days[-1]
        # A `today` far enough ahead that the month is outside any retention window.
        long_after = end + datetime.timedelta(days=400)

        before = await reads.read_summary(
            factory, tenant_id=tenant, start=start, end=end, retention_days=90, today=long_after
        )
        series_before = await reads.read_daily_series(factory, tenant_id=tenant, start=start, end=end)

        (surface,) = before.surfaces
        assert surface.calls == 18, "the fixture did not land — nothing is being compared"
        assert surface.error_calls == 3
        assert surface.payload_bytes == 3600
        assert len(series_before) == 3

        # The operator procedure, run for real rather than simulated.
        async with engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE usage_events DETACH PARTITION {_PARTITION}"))
            await conn.execute(text(f"DROP TABLE {_PARTITION}"))

        # The raw rows really are gone.
        async with factory() as session:
            remaining = int(
                (
                    await session.execute(text("SELECT count(*) FROM usage_events WHERE tenant_id = :t"), {"t": tenant})
                ).scalar_one()
            )
        assert remaining == 0, f"{remaining} raw rows survived the drop — the test proved nothing"

        after = await reads.read_summary(
            factory, tenant_id=tenant, start=start, end=end, retention_days=90, today=long_after
        )
        series_after = await reads.read_daily_series(factory, tenant_id=tenant, start=start, end=end)

        assert after == before
        assert series_after == series_before
    finally:
        # Put the partition back. It is shared with every other test in the session,
        # and leaving a hole in the range would make later inserts for this month fail
        # in a way that looks unrelated to this file.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {_PARTITION} PARTITION OF usage_events "
                    f"FOR VALUES FROM ('2025-03-01') TO ('2025-04-01')"
                )
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_only_the_raw_derived_headcount_changes_when_the_rows_go(pg_container: str) -> None:
    """The honest limit of the claim above, asserted rather than footnoted.

    `distinct_actors` is counted from raw rows on every read, because a per-day
    distinct count cannot be summed into a per-range one. So inside the retention
    window it answers, and once the rows are gone it cannot — which is exactly why the
    read refuses at the boundary and returns null with a reason instead of a zero.

    Demonstrated by deleting the rows and reading the same window twice.
    """
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = uuid.uuid4()

    try:
        days = await _seed_closed_month(factory, tenant)
        start, end = days[0], days[-1]
        # A `today` that keeps the window inside a 90-day retention, so the raw count
        # is attempted.
        soon_after = end + datetime.timedelta(days=10)

        before = await reads.read_summary(
            factory, tenant_id=tenant, start=start, end=end, retention_days=90, today=soon_after
        )
        assert before.surfaces[0].distinct_actors == 2
        assert before.surfaces[0].distinct_actors_unavailable_reason is None

        async with factory() as session:
            await session.execute(text("DELETE FROM usage_events WHERE tenant_id = :t"), {"t": tenant})
            await session.commit()

        after = await reads.read_summary(
            factory, tenant_id=tenant, start=start, end=end, retention_days=90, today=soon_after
        )

        # Everything served from the rollup is untouched...
        assert after.surfaces[0].calls == before.surfaces[0].calls
        assert after.surfaces[0].actor_days == before.surfaces[0].actor_days
        assert after.surfaces[0].worst_daily_p95_ms == before.surfaces[0].worst_daily_p95_ms
        # ...and the one field that reads raw rows now reports zero, because inside the
        # retention window the read trusts the rows to be there. Past the boundary it
        # would decline to answer at all, which is the case a real operator hits.
        assert after.surfaces[0].distinct_actors == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_rollup_row_carries_no_actor_after_all_of_this(pg_container: str) -> None:
    """The property everything above depends on, checked against the live schema.

    If a rollup table ever gained an actor column, the partition drop would stop being
    a no-op for the aggregate, the retention boundary would start applying to the
    rollups, and an erasure request would begin rewriting history. One column would
    undo all three claims.
    """
    engine = create_async_engine(pg_container)
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT table_name, column_name FROM information_schema.columns"
                        " WHERE table_name LIKE 'usage_rollup%'"
                    )
                )
            ).all()

        assert rows, "no rollup tables found — the migration did not apply"
        offenders = [(t, c) for t, c in rows if "actor" in c and c != "distinct_actors"]
        assert not offenders, f"rollup tables must not identify actors: {offenders}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_buffered_event_never_blocks_on_the_writer_being_started(pg_container: str) -> None:
    """Recording before the drain exists must still not raise.

    Startup order is a real hazard: the app wires the writer, then the scheduler, then
    begins serving. A request arriving in that gap — or during shutdown, after `stop()`
    — must be served normally, and the event either buffered or counted as lost.
    """
    engine = create_async_engine(pg_container)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        never_started = UsageWriter(factory)
        event = UsageEvent(
            occurred_at=datetime.datetime.now(tz=datetime.UTC),
            tenant_id=uuid.uuid4(),
            actor_id=None,
            surface="rest",
            operation="/v1/capabilities",
            outcome="ok",
            status_class="2xx",
            latency_ms=3,
        )

        never_started.record(event)  # must not raise
        assert never_started._queue.qsize() == 1

        # And after stopping, which flushes what was accepted.
        await never_started.stop()
        never_started.record(event)  # must not raise either
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shutdown_completes_even_when_the_final_flush_cannot_write() -> None:
    """Shutdown teardown must not be hostage to a flush of non-authoritative rows.

    `stop()` is the first statement in the app lifespan's `finally`. Everything after
    it — the scheduler, the webhook client, the entitlement client — is skipped if it
    raises, so a database that is away at shutdown would leak all three. Asserted on
    the ordering directly, rather than trusting that `stop()` happens to be last.
    """
    unreachable = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    try:
        writer = UsageWriter(async_sessionmaker(unreachable, expire_on_commit=False))
        await writer.start()
        writer.record(
            UsageEvent(
                occurred_at=datetime.datetime.now(tz=datetime.UTC),
                tenant_id=uuid.uuid4(),
                actor_id=None,
                surface="rest",
                operation="/v1/capabilities",
                outcome="ok",
                status_class="2xx",
                latency_ms=1,
            )
        )

        teardown_ran = False

        # The shape of the real lifespan, so the consequence is visible rather than
        # asserted about in a comment.
        try:
            await writer.stop()
        finally:
            teardown_ran = True

        assert teardown_ran

    finally:
        await unreachable.dispose()


def test_the_lifespan_stops_the_writer_before_the_rest_of_teardown() -> None:
    """Why the test above matters, pinned to the actual ordering.

    If `stop()` ever moves to the end of the shutdown block this test becomes
    redundant — but until it does, an exception from it costs three resources.
    """
    import inspect

    from contextplane import main

    source = inspect.getsource(main.create_app)
    stop_at = source.index("usage_writer.stop()")
    scheduler_at = source.index("scheduler.shutdown(")

    assert stop_at < scheduler_at, (
        "the writer is no longer stopped before the scheduler; if it now runs last, "
        "this test and the swallowed exception in UsageWriter.stop can be revisited"
    )
