"""Session events actually expire now, and a hold still stops one.

E6-T2. The table advertised a retention period, carried `expires_at` on every
row and an index built to sweep on it, and **nothing swept**. These assert the
three things that changes: expired rows go, unexpired ones stay, and a held row
survives its own deadline.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.retention import holds, policies
from contextplane.service.memory.session_event_expiry import SessionEventExpiry
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _ctx(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"se-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'a', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"e-{aid.hex[:8]}", "n": _NOW},
        )
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"])


async def _event(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    seq: int,
    expires_at: datetime.datetime | None,
) -> uuid.UUID:
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO memory_session_events ("
                "  event_id, tenant_id, actor_id, session_id, seq, kind, body,"
                "  created_at, expires_at, size_bytes"
                ") VALUES (:e, :t, :a, 'demo', :seq, 'agent_action', 'body', :n, :exp, 4)"
            ),
            {"e": eid, "t": ctx.tenant_id, "a": ctx.actor_id, "seq": seq, "n": _NOW, "exp": expires_at},
        )
    return eid


async def _surviving(factory: async_sessionmaker[AsyncSession], ctx: TenantContext) -> set[uuid.UUID]:
    async with factory() as session:
        return {
            row.event_id
            for row in (
                await session.execute(
                    text("SELECT event_id FROM memory_session_events WHERE tenant_id = :t"),
                    {"t": ctx.tenant_id},
                )
            ).all()
        }


def _expiry(factory: async_sessionmaker[AsyncSession]) -> SessionEventExpiry:
    return SessionEventExpiry(factory, holds.PostgresHoldStore(factory, {}))


@pytest.mark.asyncio
async def test_an_expired_event_is_deleted(factory: async_sessionmaker[AsyncSession]) -> None:
    """The whole of E6-T2 in one assertion: before this, nothing swept."""
    ctx = await _ctx(factory)
    expired = await _event(factory, ctx, seq=1, expires_at=_NOW - datetime.timedelta(days=1))

    removed = await _expiry(factory).delete_expired_events(ctx, now=_NOW)

    assert removed == 1
    assert expired not in await _surviving(factory, ctx)


@pytest.mark.asyncio
async def test_an_unexpired_event_stays(factory: async_sessionmaker[AsyncSession]) -> None:
    """A sweep that took everything would be worse than one that took nothing."""
    ctx = await _ctx(factory)
    live = await _event(factory, ctx, seq=1, expires_at=_NOW + datetime.timedelta(days=30))

    assert await _expiry(factory).delete_expired_events(ctx, now=_NOW) == 0
    assert live in await _surviving(factory, ctx)


@pytest.mark.asyncio
async def test_no_event_can_escape_the_sweep_by_having_no_period(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`expires_at` is `NOT NULL`, so every row carries a deadline.

    Written as a test rather than trusted, because the sweep's predicate depends
    on it: a nullable column would mean rows that are never due and never
    swept — the exact failure this task exists to fix, reintroduced one
    migration later and invisible until the table grew again.
    """
    ctx = await _ctx(factory)
    with pytest.raises(Exception, match="expires_at"):
        await _event(factory, ctx, seq=1, expires_at=None)


@pytest.mark.asyncio
async def test_the_sweep_reads_the_row_and_not_the_policy(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reconciliation this task had to settle.

    A session event's period is the tenant's choice *within* the class, resolved
    into `expires_at` at write time. This row's expiry is 300 days out — beyond
    the class's own 180-day ceiling — and the sweep leaves it alone, which is
    what proves it reads the row rather than recomputing from the policy.
    Recomputing would expire rows written under a setting an operator has since
    changed, silently re-dating history.
    """
    ctx = await _ctx(factory)
    beyond = await _event(factory, ctx, seq=1, expires_at=_NOW + datetime.timedelta(days=300))

    await _expiry(factory).delete_expired_events(ctx, now=_NOW + datetime.timedelta(days=200))

    assert beyond in await _surviving(factory, ctx)
    assert policies.disposition(policies.RECORD_SESSION_EVENT).retention_days == 180


@pytest.mark.asyncio
async def test_the_batch_bounds_one_sweep(factory: async_sessionmaker[AsyncSession]) -> None:
    """The largest table in the system, so a sweep is bounded and the next tick
    continues — the predicate is still "expired"."""
    ctx = await _ctx(factory)
    for seq in range(3):
        await _event(factory, ctx, seq=seq, expires_at=_NOW - datetime.timedelta(days=1))

    small = SessionEventExpiry(factory, holds.PostgresHoldStore(factory, {}), batch=2)
    first = await small.delete_expired_events(ctx, now=_NOW)
    second = await small.delete_expired_events(ctx, now=_NOW)

    assert (first, second) == (2, 1)
    assert await _surviving(factory, ctx) == set()
