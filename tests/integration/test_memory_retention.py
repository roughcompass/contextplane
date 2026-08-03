"""Retention: expired session events leave replay but stay for audit.

Exit scenario 7. The interesting cases are the boundaries -- an event one
second short of its deadline must survive, and an expired one must stop
appearing in every read path while remaining answerable to "what was here".
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.exceptions import NotFoundError
from registry.service.memory import MemoryService
from registry.types import FakeClock, TenantContext
from registry.workers.memory_expiry import MemoryExpiryWorker

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_actor(
    factory: async_sessionmaker[AsyncSession], *, retention_days: int = 30
) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active,"
                "                     memory_retention_days) "
                "VALUES (:tid, :slug, :slug, :now, TRUE, :days)"
            ),
            {"tid": tid, "slug": f"ret-{tid.hex[:8]}", "now": _NOW, "days": retention_days},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return tid, aid


def _ctx(tid: uuid.UUID, aid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["consumer"], oidc_subject="s")


@pytest.mark.asyncio
async def test_an_expired_event_leaves_replay_but_stays_in_the_table(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    written = MemoryService(factory, clock=FakeClock(_NOW))
    event = await written.record_event(ctx, session_id="R", kind="user_message", body="old")

    later = _NOW + datetime.timedelta(days=31)
    result = await MemoryExpiryWorker(factory, clock=FakeClock(later)).run()

    assert result.expired_count >= 1
    reader = MemoryService(factory, clock=FakeClock(later))
    assert await reader.list_events(ctx, session_id="R") == []
    with pytest.raises(NotFoundError):
        await reader.get_event(ctx, session_id="R", event_id=event.event_id)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT invalidated_reason FROM memory_session_events WHERE event_id = :eid"
                ),
                {"eid": event.event_id},
            )
        ).one()
    assert row.invalidated_reason == "retention_expired"


@pytest.mark.asyncio
async def test_an_event_inside_its_window_is_untouched(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    """The boundary that matters more. A sweep that expired live events would
    silently delete an agent's working context."""
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    await MemoryService(factory, clock=FakeClock(_NOW)).record_event(
        ctx, session_id="R", kind="user_message", body="fresh"
    )

    just_before = _NOW + datetime.timedelta(days=30) - datetime.timedelta(seconds=1)
    await MemoryExpiryWorker(factory, clock=FakeClock(just_before)).run()

    events = await MemoryService(factory, clock=FakeClock(just_before)).list_events(
        ctx, session_id="R"
    )
    assert [e.body for e in events] == ["fresh"]


@pytest.mark.asyncio
async def test_a_tenants_longer_window_is_honoured(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    """180 days is the configurable maximum. An event under that window must
    survive a sweep that would have expired a default-retention one."""
    tid, aid = await _seed_actor(factory, retention_days=180)
    ctx = _ctx(tid, aid)
    await MemoryService(factory, clock=FakeClock(_NOW)).record_event(
        ctx, session_id="R", kind="user_message", body="long-lived"
    )

    later = _NOW + datetime.timedelta(days=31)
    await MemoryExpiryWorker(factory, clock=FakeClock(later)).run()

    # Asserted on this tenant's own event, not on the worker's count. The
    # sweep is deliberately global, so its count reflects every tenant with a
    # backlog and says nothing about this one.
    events = await MemoryService(factory, clock=FakeClock(later)).list_events(ctx, session_id="R")
    assert [e.body for e in events] == ["long-lived"]


@pytest.mark.asyncio
async def test_shortening_a_window_does_not_retroactively_expire(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    """The deadline is materialised at write time, so a tenant tightening its
    policy applies it going forward rather than to everything already
    recorded -- which would be a surprise deletion, not a policy change."""
    tid, aid = await _seed_actor(factory, retention_days=180)
    ctx = _ctx(tid, aid)
    await MemoryService(factory, clock=FakeClock(_NOW)).record_event(
        ctx, session_id="R", kind="user_message", body="written under 180"
    )
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE tenants SET memory_retention_days = 30 WHERE tenant_id = :tid"),
            {"tid": tid},
        )

    later = _NOW + datetime.timedelta(days=31)
    await MemoryExpiryWorker(factory, clock=FakeClock(later)).run()

    events = await MemoryService(factory, clock=FakeClock(later)).list_events(ctx, session_id="R")
    assert [e.body for e in events] == ["written under 180"]


@pytest.mark.asyncio
async def test_the_sweep_is_idempotent(factory: async_sessionmaker[AsyncSession]) -> None:
    """A second pass must find nothing, so a crash mid-run and a double run
    are both harmless."""
    tid, aid = await _seed_actor(factory)
    await MemoryService(factory, clock=FakeClock(_NOW)).record_event(
        _ctx(tid, aid), session_id="R", kind="user_message", body="x"
    )
    later = _NOW + datetime.timedelta(days=31)
    worker = MemoryExpiryWorker(factory, clock=FakeClock(later))

    first = (await worker.run()).expired_count
    second = (await worker.run()).expired_count

    # The first pass swept at least this test's event; the second must find
    # nothing at all, which is the property that makes a crash mid-run safe.
    assert first >= 1
    assert second == 0


@pytest.mark.asyncio
async def test_a_backlog_is_cleared_across_batches(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    """Batching exists so one pass cannot hold locks over an unbounded set;
    the loop exists so a backlog still clears in one run."""
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    writer = MemoryService(factory, clock=FakeClock(_NOW))
    for i in range(5):
        await writer.record_event(ctx, session_id="R", kind="agent_action", body=f"e{i}")

    later = _NOW + datetime.timedelta(days=31)
    result = await MemoryExpiryWorker(factory, clock=FakeClock(later), batch_size=2).run()

    # At least this test's five, in more than one batch, having run to
    # completion rather than stopping on the ceiling.
    assert result.expired_count >= 5
    assert result.batches >= 3
    assert result.truncated is False
    assert await MemoryService(factory, clock=FakeClock(later)).list_events(
        ctx, session_id="R"
    ) == []
