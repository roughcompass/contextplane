"""Intervention-rate from session events, against a real database.

The whole subtlety is one boundary: **the human's opening message is not an
intervention.** Every session starts with somebody saying what to do, so
counting that as steering marks every session intervened and the metric reports
a constant.
`test_the_opening_message_is_the_brief_and_not_an_intervention` is the test that
fails if the boundary moves; the rest would pass either way.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.service.memory.agent_autonomy import AgentAutonomyService
from contextplane.types import TenantContext

_SEEDED = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_WINDOW = (datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC), datetime.datetime(2026, 8, 22, tzinfo=datetime.UTC))


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _World:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.tenant_id = uuid.uuid4()
        self.agent_id = uuid.uuid4()

    async def build(self) -> _World:
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"t": self.tenant_id, "s": f"au-{self.tenant_id.hex[:8]}", "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'agent', :sub, :now)"
                ),
                {"a": self.agent_id, "t": self.tenant_id, "sub": f"ag-{self.agent_id.hex[:8]}", "now": _SEEDED},
            )
        return self

    async def session_of(
        self,
        *kinds: str,
        actor: uuid.UUID | None = None,
        at: datetime.datetime | None = None,
        session_id: str | None = None,
    ) -> str:
        """One session whose events are `kinds`, in order."""
        sid = session_id or f"s-{uuid.uuid4().hex[:10]}"
        moment = at or _SEEDED
        async with self.factory() as session, session.begin():
            for seq, kind in enumerate(kinds, start=1):
                await session.execute(
                    text(
                        "INSERT INTO memory_session_events "
                        "  (tenant_id, actor_id, session_id, seq, kind, body, tool_name, "
                        "   created_at, expires_at, size_bytes) "
                        "VALUES (:t, :a, :s, :q, :k, :body, :tool, :now, :expires, :size)"
                    ),
                    {
                        "t": self.tenant_id,
                        "a": actor or self.agent_id,
                        "s": sid,
                        "q": seq,
                        "k": kind,
                        # The schema's CHECK ties tool_name to the kind exactly.
                        "tool": "grep" if kind == "tool_invocation" else None,
                        "body": f"{kind} {seq}",
                        "now": moment,
                        "expires": moment + datetime.timedelta(days=30),
                        "size": 16,
                    },
                )
        return sid

    def ctx(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, actor_id=self.agent_id, roles=["admin"], oidc_subject="a")

    def service(self) -> AgentAutonomyService:
        return AgentAutonomyService(self.factory)

    async def read(self) -> object:
        return await self.service().autonomy_for(
            self.ctx(), author_actor_id=self.agent_id, window_start=_WINDOW[0], window_end=_WINDOW[1]
        )


@pytest_asyncio.fixture
async def world(factory: async_sessionmaker[AsyncSession]) -> _World:
    return await _World(factory).build()


@pytest.mark.asyncio
async def test_the_opening_message_is_the_brief_and_not_an_intervention(world: _World) -> None:
    """The boundary this whole module turns on.

    A session that opens with a human's message and then runs on agent actions
    is autonomous. Treating that opening message as steering would mark every
    session intervened, and the metric would report a constant nobody could act
    on.
    """
    await world.session_of("user_message", "agent_action", "tool_invocation", "agent_action")

    result = await world.read()

    assert result.n_sessions == 1
    assert result.n_intervened == 0, (
        "the session's opening user_message was counted as an intervention; the boundary is the "
        "first agent_action, and a message before it is the brief"
    )
    assert result.autonomy_rate == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_a_message_after_the_agent_started_is_an_intervention(world: _World) -> None:
    await world.session_of("user_message", "agent_action", "user_message", "agent_action")

    result = await world.read()

    assert result.n_sessions == 1
    assert result.n_intervened == 1
    assert result.intervention_rate == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_the_rate_is_over_sessions_not_over_events(world: _World) -> None:
    """One session steered three times is one intervened session, not three.

    A per-event rate would make a single messy session look like a systemic
    problem, and would move when an agent happened to emit more actions.
    """
    await world.session_of("user_message", "agent_action", "user_message", "user_message", "user_message")
    await world.session_of("user_message", "agent_action")
    await world.session_of("user_message", "agent_action")
    await world.session_of("user_message", "agent_action")

    result = await world.read()

    assert result.n_sessions == 4
    assert result.n_intervened == 1
    assert result.intervention_rate == pytest.approx(0.25)
    assert result.n_autonomous == 3


@pytest.mark.asyncio
async def test_a_session_the_agent_never_acted_in_is_not_evidence_either_way(world: _World) -> None:
    """Nothing ran autonomously and nothing was corrected.

    Counting it as autonomous would reward an agent for sessions it never
    started; counting it as intervened would punish it for the same.
    """
    await world.session_of("user_message", "user_message")
    await world.session_of("user_message", "agent_action")

    result = await world.read()

    assert result.n_sessions == 1, "a session with no agent_action must not be counted"
    assert result.n_intervened == 0


@pytest.mark.asyncio
async def test_sessions_are_separated_from_each_other(world: _World) -> None:
    """The boundary is per session. A window function partitioned wrongly would
    classify one session against another's first action."""
    await world.session_of("user_message", "agent_action")
    await world.session_of("user_message", "agent_action", "user_message")

    result = await world.read()

    assert result.n_sessions == 2
    assert result.n_intervened == 1


@pytest.mark.asyncio
async def test_another_actors_sessions_do_not_count(world: _World) -> None:
    other = uuid.uuid4()
    async with world.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'other', :sub, :now)"
            ),
            {"a": other, "t": world.tenant_id, "sub": f"o-{other.hex[:8]}", "now": _SEEDED},
        )
    await world.session_of("user_message", "agent_action", "user_message", actor=other)
    await world.session_of("user_message", "agent_action")

    result = await world.read()

    assert result.n_sessions == 1
    assert result.n_intervened == 0


@pytest.mark.asyncio
async def test_the_window_is_half_open(world: _World) -> None:
    await world.session_of("user_message", "agent_action", at=_WINDOW[0])
    await world.session_of("user_message", "agent_action", at=_WINDOW[1])

    result = await world.read()

    assert result.n_sessions == 1, "the start is inclusive and the end exclusive"


@pytest.mark.asyncio
async def test_an_agent_with_no_sessions_has_no_rate_rather_than_a_perfect_one(world: _World) -> None:
    """Zero sessions is an unknown intervention rate. Reporting 0.0 would be the
    flattering claim that it never needed help."""
    result = await world.read()

    assert result.n_sessions == 0
    assert result.intervention_rate is None
    assert result.autonomy_rate is None
