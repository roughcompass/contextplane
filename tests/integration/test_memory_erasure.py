"""Right-to-be-forgotten reaches session memory, not just workspaces.

Exit scenario 8. The property that matters is coverage: an erasure that reports
success while leaving a person's rows behind is worse than one that fails,
because they are told they are gone.

So these tests check the fan-out as well as the deletion — a subsystem missing
from the registry is a gap nothing else would reveal.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.erasure import ErasureRegistry, SessionMemoryErasure
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
    factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    tid = tenant_id or uuid.uuid4()
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        if tenant_id is None:
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tid, "slug": f"era-{tid.hex[:8]}", "now": _NOW},
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
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["admin"], oidc_subject="s")


async def _count(factory: async_sessionmaker[AsyncSession], actor_id: uuid.UUID) -> int:
    async with factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM memory_session_events WHERE actor_id = :aid"),
                    {"aid": actor_id},
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_erasure_physically_deletes_the_actors_events(factory: async_sessionmaker[AsyncSession]) -> None:
    tid, aid = await _seed_actor(factory)
    service = MemoryService(factory, clock=FakeClock(_NOW))
    for i in range(3):
        await service.record_event(_ctx(tid, aid), session_id="E", kind="agent_action", body=f"e{i}")

    removed = await service.erase_actor_events(_ctx(tid, aid), target_actor_id=aid)

    assert removed["session_events"] == 3
    assert await _count(factory, aid) == 0


@pytest.mark.asyncio
async def test_erasure_also_removes_already_invalidated_events(factory: async_sessionmaker[AsyncSession]) -> None:
    """Soft-invalidated rows survive an ordinary removal precisely so they stay
    answerable for audit. An erasure request is the one thing that overrides
    that -- otherwise "delete everything about me" would quietly leave behind
    exactly the events the person had already asked to remove."""
    tid, aid = await _seed_actor(factory)
    service = MemoryService(factory, clock=FakeClock(_NOW))
    deleted = await service.record_event(_ctx(tid, aid), session_id="E", kind="user_message", body="deleted by me")
    await service.record_event(_ctx(tid, aid), session_id="E", kind="user_message", body="live")
    await service.delete_event(_ctx(tid, aid), session_id="E", event_id=deleted.event_id)
    # And one removed by retention.
    expired_tid, expired_aid = tid, aid
    await MemoryExpiryWorker(factory, clock=FakeClock(_NOW + datetime.timedelta(days=31))).run()

    removed = await service.erase_actor_events(_ctx(expired_tid, expired_aid), target_actor_id=expired_aid)

    assert removed["session_events"] == 2
    assert await _count(factory, aid) == 0


@pytest.mark.asyncio
async def test_erasure_leaves_other_actors_untouched(factory: async_sessionmaker[AsyncSession]) -> None:
    """Including a colleague in the same tenant. An erasure scoped only by
    tenant would delete a whole team's memory."""
    tid, mine = await _seed_actor(factory)
    _, colleague = await _seed_actor(factory, tenant_id=tid)
    service = MemoryService(factory, clock=FakeClock(_NOW))
    await service.record_event(_ctx(tid, mine), session_id="E", kind="user_message", body="mine")
    await service.record_event(_ctx(tid, colleague), session_id="E", kind="user_message", body="theirs")

    await service.erase_actor_events(_ctx(tid, mine), target_actor_id=mine)

    assert await _count(factory, mine) == 0
    assert await _count(factory, colleague) == 1


@pytest.mark.asyncio
async def test_erasure_is_idempotent(factory: async_sessionmaker[AsyncSession]) -> None:
    """Retrying a partly-failed erasure is the normal case, not the exception."""
    tid, aid = await _seed_actor(factory)
    service = MemoryService(factory, clock=FakeClock(_NOW))
    await service.record_event(_ctx(tid, aid), session_id="E", kind="user_message", body="x")

    first = await service.erase_actor_events(_ctx(tid, aid), target_actor_id=aid)
    second = await service.erase_actor_events(_ctx(tid, aid), target_actor_id=aid)

    assert first["session_events"] == 1
    assert second == dict.fromkeys(first, 0), "a repeat must remove nothing, from any table"


@pytest.mark.asyncio
async def test_the_registry_fans_out_and_reports_each_subsystem(factory: async_sessionmaker[AsyncSession]) -> None:
    tid, aid = await _seed_actor(factory)
    service = MemoryService(factory, clock=FakeClock(_NOW))
    await service.record_event(_ctx(tid, aid), session_id="E", kind="user_message", body="x")

    registry = ErasureRegistry()
    registry.register(SessionMemoryErasure(service))

    counts = await registry.erase_actor(_ctx(tid, aid), aid)

    assert [c.subsystem for c in counts] == ["session_memory"]
    assert counts[0].removed["session_events"] == 1
    # Reported per table rather than as one number: an erasure receipt saying
    # "12" cannot be checked against anything.
    assert set(counts[0].removed) == {
        "session_events",
        "extraction_queued",
        "extraction_dead_lettered",
    }


def test_a_subsystem_cannot_register_twice() -> None:
    """Double registration would double-count and imply the second replaced
    the first, which it does not."""
    registry = ErasureRegistry()
    registry.register(SessionMemoryErasure(object()))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(SessionMemoryErasure(object()))


def test_the_registry_reports_its_coverage() -> None:
    """A subsystem missing from erasure is missing silently. This is how a
    deployment, or a test, can see what a request would actually reach."""
    registry = ErasureRegistry()
    registry.register(SessionMemoryErasure(object()))
    assert registry.subsystems == ("session_memory",)


@pytest.mark.asyncio
async def test_the_running_app_wires_every_subsystem_that_holds_personal_data(
    pg_container: str,
) -> None:
    """The gap this whole design exists to prevent, asserted against the real app.

    A subsystem written but never registered erases nothing and says nothing.
    This is the one check that would notice.
    """
    from tests.helpers.auth_harness import EntitlementAuthHarness

    async with EntitlementAuthHarness(pg_container) as harness:
        registry = harness.app.state.erasure

    # Exact, not a subset. A subsystem written but never registered erases nothing and
    # says nothing, and a subset assertion would not notice one going missing either.
    #
    # `embeddings` holds the source text verbatim in `text_chunk`, so before it was
    # registered a right-to-be-forgotten request reported success while the erased
    # person's own words stayed searchable through the semantic arm.
    assert set(registry.subsystems) == {"workspace", "session_memory", "embeddings"}


@pytest.mark.asyncio
async def test_erasure_removes_the_actors_extraction_queue(factory: async_sessionmaker[AsyncSession]) -> None:
    """Queue rows name the actor, the session, and the window, so leaving them
    would keep the actor's session identifiers after their conversations were
    erased. Found by writing an operations runbook that claimed a foreign key
    handled this; it did not.
    """
    from registry.extraction.strategies import OBSERVATION  # noqa: PLC0415
    from registry.workers.extraction_drain import enqueue_extraction  # noqa: PLC0415

    tid, aid = await _seed_actor(factory)
    service = MemoryService(factory, clock=FakeClock(_NOW))
    event = await service.record_event(_ctx(tid, aid), session_id="E", kind="user_message", body="x")
    async with factory() as session, session.begin():
        await enqueue_extraction(
            session,
            tenant_id=tid,
            actor_id=aid,
            session_id="E",
            seq=event.seq,
            strategies=(OBSERVATION,),
        )

    removed = await service.erase_actor_events(_ctx(tid, aid), target_actor_id=aid)

    assert removed["extraction_queued"] == 1
    async with factory() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM lmm_extraction_outbox WHERE actor_id = :aid"),
                {"aid": aid},
            )
        ).scalar_one()
    assert remaining == 0


@pytest.mark.asyncio
async def test_erasure_removes_the_actors_dead_lettered_rows(factory: async_sessionmaker[AsyncSession]) -> None:
    """The dead-letter table holds the same identifiers plus a stored error
    string, so it is erasable material for the same reason."""
    tid, aid = await _seed_actor(factory)
    service = MemoryService(factory, clock=FakeClock(_NOW))
    await service.record_event(_ctx(tid, aid), session_id="E", kind="user_message", body="x")
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO lmm_extraction_outbox_failed "
                "  (tenant_id, actor_id, session_id, strategy_id, from_seq, through_seq, "
                "   attempts, last_error, enqueued_at) "
                "VALUES (:tid, :aid, 'E', 'capability_observation', 1, 1, 3, 'nope', :now)"
            ),
            {"tid": tid, "aid": aid, "now": _NOW},
        )

    removed = await service.erase_actor_events(_ctx(tid, aid), target_actor_id=aid)

    assert removed["extraction_dead_lettered"] == 1


@pytest.mark.asyncio
async def test_erasure_leaves_another_actors_queue_alone(factory: async_sessionmaker[AsyncSession]) -> None:
    """Scoped by actor as well as tenant, matching every other query here."""
    from registry.extraction.strategies import OBSERVATION  # noqa: PLC0415
    from registry.workers.extraction_drain import enqueue_extraction  # noqa: PLC0415

    tid, mine = await _seed_actor(factory)
    _, theirs = await _seed_actor(factory)
    service = MemoryService(factory, clock=FakeClock(_NOW))
    for actor in (mine, theirs):
        event = await service.record_event(_ctx(tid, actor), session_id="E", kind="user_message", body="x")
        async with factory() as session, session.begin():
            await enqueue_extraction(
                session,
                tenant_id=tid,
                actor_id=actor,
                session_id="E",
                seq=event.seq,
                strategies=(OBSERVATION,),
            )

    await service.erase_actor_events(_ctx(tid, mine), target_actor_id=mine)

    async with factory() as session:
        survived = (
            await session.execute(
                text("SELECT count(*) FROM lmm_extraction_outbox WHERE actor_id = :aid"),
                {"aid": theirs},
            )
        ).scalar_one()
    assert survived == 1
