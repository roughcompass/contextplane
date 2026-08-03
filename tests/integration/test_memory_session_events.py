"""An agent's session memory: the substrate the living-memory loop returns through.

These are the phase's exit scenarios, and the one that matters most is
resumption across a restart -- record a turn, lose the process, come back, replay
in reverse, and still be able to resolve a follow-up that refers to an earlier
subject only by pronoun. It passes with no LLM configured anywhere, because
replay returns the actual prior turns rather than inferring them.

The security property is unusual for this codebase and worth stating plainly:
scoping is by *actor*, not by tenant. Every other read path here is
tenant-scoped, and tenant scoping alone would expose every session to every
colleague. So the isolation tests come in two halves, and the second is the one
a tenant-shaped implementation would fail.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.exceptions import NotFoundError, ValidationError
from registry.service.memory import MemoryService
from registry.types import FakeClock, TenantContext

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
    """A tenant and one actor in it. Returns `(tenant_id, actor_id)`."""
    tid = tenant_id or uuid.uuid4()
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        if tenant_id is None:
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tid, "slug": f"mem-{tid.hex[:8]}", "now": _NOW},
            )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'memory-actor', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"sub-{aid.hex[:8]}", "now": _NOW},
        )
    return tid, aid


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID | None) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id, actor_id=actor_id, roles=["consumer"], oidc_subject="s"
    )


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession]) -> MemoryService:
    return MemoryService(factory, clock=FakeClock(_NOW))


# --- exit 1: immutable append and ordered replay ---------------------------------


@pytest.mark.asyncio
async def test_five_events_across_three_kinds_replay_in_order(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)

    await service.record_event(ctx, session_id="S1", kind="user_message", body="one")
    await service.record_event(ctx, session_id="S1", kind="agent_action", body="two")
    await service.record_event(
        ctx, session_id="S1", kind="tool_invocation", body="three", tool_name="grep"
    )
    await service.record_event(ctx, session_id="S1", kind="agent_action", body="four")
    await service.record_event(ctx, session_id="S1", kind="user_message", body="five")

    events = await service.list_events(ctx, session_id="S1")

    assert [e.body for e in events] == ["one", "two", "three", "four", "five"]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_a_single_event_is_fetchable_by_id(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    recorded = await service.record_event(ctx, session_id="S1", kind="user_message", body="hello")

    fetched = await service.get_event(ctx, session_id="S1", event_id=recorded.event_id)

    assert fetched.body == "hello"
    assert fetched.seq == recorded.seq


# --- exit 2: resuming across a restart, with no LLM ------------------------------


@pytest.mark.asyncio
async def test_a_new_process_resumes_a_session_and_replays_the_last_turns_in_reverse(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    """The phase's reason to exist.

    An agent establishes a subject, its process dies, a new process resumes the
    same `session_id` and reads back the last turns newest-first. The follow-up
    referring to that subject by pronoun is resolvable because the prior turn is
    *there*, verbatim -- not because anything inferred it. No LLM is configured
    in this test, and none is needed.
    """
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)

    first_process = MemoryService(factory, clock=FakeClock(_NOW))
    await first_process.record_event(
        ctx, session_id="S-resume", kind="user_message", body="Deploy the payments service."
    )
    await first_process.record_event(
        ctx, session_id="S-resume", kind="agent_action", body="Checked payments deploy preconditions."
    )
    del first_process  # the process is gone; nothing is carried in memory

    resumed = MemoryService(factory, clock=FakeClock(_NOW))
    recent = await resumed.list_events(ctx, session_id="S-resume", order="desc", limit=5)

    assert [e.seq for e in recent] == [2, 1]
    # The referent the follow-up "is it ready?" would depend on.
    assert "payments" in recent[-1].body


@pytest.mark.asyncio
async def test_forward_and_reverse_agree_on_the_boundary_under_one_timestamp(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    """A burst shares a `created_at` to the microsecond -- the clock here is
    frozen, which is the worst case rather than an artificial one.

    Ordering by timestamp would make "the last two" and "the first two
    reversed" disagree, silently, exactly when an agent is trying to resume.
    """
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    for i in range(6):
        await service.record_event(ctx, session_id="B", kind="agent_action", body=f"e{i}")

    forward = await service.list_events(ctx, session_id="B")
    reverse = await service.list_events(ctx, session_id="B", order="desc")

    assert [e.event_id for e in reverse] == [e.event_id for e in reversed(forward)]
    assert [e.created_at for e in forward] == [forward[0].created_at] * 6


# --- exit 3: resuming week-old work ----------------------------------------------


@pytest.mark.asyncio
async def test_sessions_list_most_recently_active_first_with_counts(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    older = MemoryService(factory, clock=FakeClock(_NOW - datetime.timedelta(days=8)))
    newer = MemoryService(factory, clock=FakeClock(_NOW))

    await older.record_event(ctx, session_id="S-old", kind="user_message", body="a")
    await older.record_event(ctx, session_id="S-old", kind="agent_action", body="b")
    await newer.record_event(ctx, session_id="S-new", kind="user_message", body="c")

    sessions = await newer.list_sessions(ctx)

    assert [s.session_id for s in sessions] == ["S-new", "S-old"]
    assert {s.session_id: s.event_count for s in sessions} == {"S-new": 1, "S-old": 2}
    assert sessions[1].first_activity_at == sessions[1].last_activity_at - datetime.timedelta(0)


@pytest.mark.asyncio
async def test_the_older_session_replays_unmodified(
    factory: async_sessionmaker[AsyncSession]
) -> None:
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    older = MemoryService(factory, clock=FakeClock(_NOW - datetime.timedelta(days=8)))
    await older.record_event(ctx, session_id="S-old", kind="user_message", body="unchanged")

    events = await MemoryService(factory, clock=FakeClock(_NOW)).list_events(ctx, session_id="S-old")

    assert [e.body for e in events] == ["unchanged"]


# --- exit 4: metadata-filtered replay --------------------------------------------


@pytest.mark.asyncio
async def test_a_metadata_filter_returns_exactly_the_matching_events(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    for i in range(10):
        meta = {"task": "T-42"} if i in (2, 5, 8) else {"task": "other"}
        await service.record_event(
            ctx, session_id="M", kind="agent_action", body=f"e{i}", metadata=meta
        )

    matched = await service.list_events(ctx, session_id="M", metadata_equals={"task": "T-42"})

    assert [e.body for e in matched] == ["e2", "e5", "e8"]


# --- exit 5: deletion --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deleted_event_leaves_replay_but_stays_addressable_for_audit(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    """Both halves. Leaving replay is what makes deletion mean anything;
    staying in the table is what stops the audit trail having a hole exactly
    where somebody chose to remove something."""
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    keep = await service.record_event(ctx, session_id="D", kind="user_message", body="keep")
    drop = await service.record_event(ctx, session_id="D", kind="user_message", body="drop")

    await service.delete_event(ctx, session_id="D", event_id=drop.event_id)

    assert [e.event_id for e in await service.list_events(ctx, session_id="D")] == [keep.event_id]
    with pytest.raises(NotFoundError):
        await service.get_event(ctx, session_id="D", event_id=drop.event_id)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT invalidated_at, invalidated_reason FROM memory_session_events "
                    "WHERE event_id = :eid"
                ),
                {"eid": drop.event_id},
            )
        ).one()
    assert row.invalidated_at is not None
    assert row.invalidated_reason == "actor_deleted"


@pytest.mark.asyncio
async def test_deleting_an_event_twice_is_reported_as_absent(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    event = await service.record_event(ctx, session_id="D", kind="user_message", body="x")
    await service.delete_event(ctx, session_id="D", event_id=event.event_id)

    with pytest.raises(NotFoundError):
        await service.delete_event(ctx, session_id="D", event_id=event.event_id)


# --- exit 9: isolation, both halves -----------------------------------------------


@pytest.mark.asyncio
async def test_another_tenant_cannot_read_these_sessions(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    tid_a, aid_a = await _seed_actor(factory)
    tid_b, aid_b = await _seed_actor(factory)
    await service.record_event(_ctx(tid_a, aid_a), session_id="X", kind="user_message", body="secret")

    assert await service.list_events(_ctx(tid_b, aid_b), session_id="X") == []
    assert await service.list_sessions(_ctx(tid_b, aid_b)) == []


@pytest.mark.asyncio
async def test_a_colleague_in_the_same_tenant_cannot_read_my_sessions(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    """The property unique to this substrate, and the one a tenant-shaped
    implementation would fail while passing every cross-tenant test.

    `VisibilityService` would return visible here: its same-tenant branch grants
    any actor in the owning tenant access. Correct for a catalog entity,
    catastrophic for a private conversation.
    """
    tid, mine = await _seed_actor(factory)
    _, colleague = await _seed_actor(factory, tenant_id=tid)
    recorded = await service.record_event(
        _ctx(tid, mine), session_id="P", kind="user_message", body="my private turn"
    )

    other = _ctx(tid, colleague)
    assert await service.list_events(other, session_id="P") == []
    assert await service.list_sessions(other) == []
    with pytest.raises(NotFoundError):
        await service.get_event(other, session_id="P", event_id=recorded.event_id)


@pytest.mark.asyncio
async def test_a_colleague_cannot_delete_my_event(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    """Deletion is scoped in the predicate, not checked after the fact -- an
    UPDATE that found the row and then refused would still have found it."""
    tid, mine = await _seed_actor(factory)
    _, colleague = await _seed_actor(factory, tenant_id=tid)
    recorded = await service.record_event(
        _ctx(tid, mine), session_id="P", kind="user_message", body="mine"
    )

    with pytest.raises(NotFoundError):
        await service.delete_event(_ctx(tid, colleague), session_id="P", event_id=recorded.event_id)

    assert len(await service.list_events(_ctx(tid, mine), session_id="P")) == 1


# --- validation -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_kind_is_refused(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    tid, aid = await _seed_actor(factory)
    with pytest.raises(ValidationError, match="unknown event kind"):
        await service.record_event(_ctx(tid, aid), session_id="S", kind="thinking", body="x")


@pytest.mark.asyncio
async def test_a_tool_invocation_without_a_tool_name_is_refused(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    tid, aid = await _seed_actor(factory)
    with pytest.raises(ValidationError, match="tool_name"):
        await service.record_event(_ctx(tid, aid), session_id="S", kind="tool_invocation", body="x")


@pytest.mark.asyncio
async def test_a_tool_name_on_another_kind_is_refused(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    """Both directions. Checking only the first lets a caller confusing the
    vocabulary through silently."""
    tid, aid = await _seed_actor(factory)
    with pytest.raises(ValidationError, match="tool_name"):
        await service.record_event(
            _ctx(tid, aid), session_id="S", kind="user_message", body="x", tool_name="grep"
        )


@pytest.mark.asyncio
async def test_the_body_cap_is_measured_in_bytes_not_characters(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    """A character count would admit roughly four times the cap in multi-byte
    text -- which is the text most likely to arrive from a real conversation."""
    tid, aid = await _seed_actor(factory)
    # Well under 16384 characters, comfortably over 16384 bytes.
    body = "日" * 6000
    assert len(body) < 16384
    with pytest.raises(ValidationError, match="bytes"):
        await service.record_event(_ctx(tid, aid), session_id="S", kind="user_message", body=body)


@pytest.mark.asyncio
async def test_a_context_without_an_actor_has_no_memory(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    """Refused rather than defaulted. A credential resolving to a tenant but no
    actor must not read whichever session happens to be first."""
    tid, _ = await _seed_actor(factory)
    with pytest.raises(ValidationError, match="actor identity"):
        await service.list_sessions(_ctx(tid, None))


# --- pagination ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cursor_never_repeats_or_skips_while_the_session_grows(
    factory: async_sessionmaker[AsyncSession], service: MemoryService
) -> None:
    """An offset pager over an append-only table re-reads shifting windows. A
    `seq` cursor cannot, because `seq` is immutable once assigned."""
    tid, aid = await _seed_actor(factory)
    ctx = _ctx(tid, aid)
    for i in range(5):
        await service.record_event(ctx, session_id="P", kind="agent_action", body=f"e{i}")

    first = await service.list_events(ctx, session_id="P", limit=2)
    # More arrive between pages, which is the normal case for a live agent.
    await service.record_event(ctx, session_id="P", kind="agent_action", body="e5")
    second = await service.list_events(ctx, session_id="P", limit=2, cursor=first[-1].seq)

    seen = [e.body for e in first] + [e.body for e in second]
    assert seen == ["e0", "e1", "e2", "e3"]
    assert len(set(seen)) == len(seen)
