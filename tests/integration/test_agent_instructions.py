"""Instruction lineage: proposed, activated against evidence, rolled back.

Three properties, and two of them are only visible against a real database.

`test_activating_demotes_the_incumbent_in_one_transaction` — the partial unique
index makes two active versions unrepresentable, so activating in the wrong
order fails at the database rather than producing a wrong answer.

`test_an_instruction_citing_another_agents_report_is_refused` — the one way to
build a fully-constrained row that means nothing. The foreign key and the
activation CHECK are both satisfied; only the service catches it.

`test_rollback_returns_to_what_was_in_force_not_to_the_previous_number` — after
a rollback and a re-activation, "previous version" by number and by time are
different rows, and only one of them is what a caller wants back.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory.agent_instructions import (
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    AgentInstructionService,
)
from contextplane.types import TenantContext

_SEEDED = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)


def _at(day: int) -> datetime.datetime:
    return datetime.datetime(2026, 8, day, 12, 0, tzinfo=datetime.UTC)


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
                {"t": self.tenant_id, "s": f"in-{self.tenant_id.hex[:8]}", "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'agent', :sub, :now)"
                ),
                {"a": self.agent_id, "t": self.tenant_id, "sub": f"ag-{self.agent_id.hex[:8]}", "now": _SEEDED},
            )
        return self

    async def report(self, *, actor: uuid.UUID | None = None) -> uuid.UUID:
        """A stored failure-pattern report for an instruction to cite."""
        async with self.factory() as session, session.begin():
            return (
                await session.execute(
                    text(
                        "INSERT INTO agent_failure_pattern_report ("
                        "  tenant_id, author_actor_id, window_start, window_end,"
                        "  n_adjudicated, n_incorrect, n_intervention_sessions, n_sessions,"
                        "  groups, generated_at"
                        ") VALUES (:t, :a, :ws, :we, 4, 2, 1, 3, '[]'::jsonb, :now) RETURNING report_id"
                    ),
                    {
                        "t": self.tenant_id,
                        "a": actor or self.agent_id,
                        "ws": _at(1),
                        "we": _at(20),
                        "now": _at(20),
                    },
                )
            ).scalar_one()

    def ctx(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, actor_id=self.agent_id, roles=["admin"], oidc_subject="a")

    def service(self) -> AgentInstructionService:
        return AgentInstructionService(self.factory)


@pytest_asyncio.fixture
async def world(factory: async_sessionmaker[AsyncSession]) -> _World:
    return await _World(factory).build()


@pytest.mark.asyncio
async def test_a_proposal_is_not_active(world: _World) -> None:
    """Proposing and activating are separate decisions: writing a candidate is
    cheap and reversible, putting it in force is neither."""
    report = await world.report()
    await world.service().propose(
        world.ctx(), author_actor_id=world.agent_id, version=1, content="be specific", motivated_by_report_id=report
    )

    assert await world.service().active_instruction(world.ctx(), author_actor_id=world.agent_id) is None


@pytest.mark.asyncio
async def test_activating_demotes_the_incumbent_in_one_transaction(world: _World) -> None:
    """The partial unique index allows one active version per agent, so this
    would fail at the database if the order were wrong."""
    service, report = world.service(), await world.report()
    first = await service.propose(
        world.ctx(), author_actor_id=world.agent_id, version=1, content="v1", motivated_by_report_id=report
    )
    second = await service.propose(
        world.ctx(), author_actor_id=world.agent_id, version=2, content="v2", motivated_by_report_id=report
    )

    await service.activate(world.ctx(), instruction_id=first, now=_at(10))
    await service.activate(world.ctx(), instruction_id=second, now=_at(11))

    active = await service.active_instruction(world.ctx(), author_actor_id=world.agent_id)
    assert active is not None
    assert active.instruction_id == second
    assert active.activated_at == _at(11)

    history = {i.instruction_id: i for i in await service.history(world.ctx(), author_actor_id=world.agent_id)}
    assert history[first].status == STATUS_SUPERSEDED
    assert history[first].superseded_at == _at(11)


@pytest.mark.asyncio
async def test_an_instruction_citing_another_agents_report_is_refused(world: _World) -> None:
    """The one way to build a fully-constrained row that means nothing.

    The foreign key resolves and the activation CHECK is satisfied — the report
    is real. It is just evidence about a different agent's work, which only the
    service can see.
    """
    other = uuid.uuid4()
    async with world.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'other', :sub, :now)"
            ),
            {"a": other, "t": world.tenant_id, "sub": f"o-{other.hex[:8]}", "now": _SEEDED},
        )
    foreign = await world.report(actor=other)

    with pytest.raises(ValidationError, match="must cite a report about the agent it governs"):
        await world.service().propose(
            world.ctx(), author_actor_id=world.agent_id, version=1, content="v1", motivated_by_report_id=foreign
        )


@pytest.mark.asyncio
async def test_a_report_that_does_not_exist_is_refused_by_name(world: _World) -> None:
    """Before an opaque foreign-key violation would surface."""
    missing = uuid.uuid4()
    with pytest.raises(NotFoundError, match=str(missing)):
        await world.service().propose(
            world.ctx(), author_actor_id=world.agent_id, version=1, content="v1", motivated_by_report_id=missing
        )


@pytest.mark.asyncio
async def test_the_database_refuses_an_active_version_with_no_evidence(world: _World) -> None:
    """The service's check gives the better message; this is the one that is
    true for every writer, including one nobody has written yet.

    Driven with raw SQL on purpose -- the service cannot produce this state, so
    the only way to test the constraint is to go around the service, which is
    exactly the writer the constraint exists for.
    """
    async with world.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO agent_instruction "
                "  (tenant_id, author_actor_id, version, content, status, created_at) "
                "VALUES (:t, :a, 9, 'no evidence', 'superseded', :now)"
            ),
            {"t": world.tenant_id, "a": world.agent_id, "now": _SEEDED},
        )

    with pytest.raises(Exception, match="ck_agent_instruction_active_cites_evidence"):
        async with world.factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE agent_instruction SET status = 'active', activated_at = :now "
                    " WHERE author_actor_id = :a AND version = 9"
                ),
                {"a": world.agent_id, "now": _at(10)},
            )


@pytest.mark.asyncio
async def test_rollback_returns_to_what_was_in_force_not_to_the_previous_number(world: _World) -> None:
    """After a rollback and a re-activation, "previous" by number and by time
    are different rows.

    v1 active, then v2, then roll back to v1, then activate v3. The version
    numerically before v3 is v2 — but the version that was actually in force is
    v1, and that is what a rollback must restore.
    """
    service, report = world.service(), await world.report()
    ids = {}
    for version in (1, 2, 3):
        ids[version] = await service.propose(
            world.ctx(),
            author_actor_id=world.agent_id,
            version=version,
            content=f"v{version}",
            motivated_by_report_id=report,
        )

    await service.activate(world.ctx(), instruction_id=ids[1], now=_at(10))
    await service.activate(world.ctx(), instruction_id=ids[2], now=_at(11))
    await service.rollback(world.ctx(), author_actor_id=world.agent_id, now=_at(12))  # back to v1
    await service.activate(world.ctx(), instruction_id=ids[3], now=_at(13))

    restored = await service.rollback(world.ctx(), author_actor_id=world.agent_id, now=_at(14))

    assert restored == ids[1], "rollback returned the numerically-previous version rather than the one in force"
    active = await service.active_instruction(world.ctx(), author_actor_id=world.agent_id)
    assert active is not None
    assert active.version == 1


@pytest.mark.asyncio
async def test_rolling_back_a_first_version_returns_none_rather_than_raising(world: _World) -> None:
    """ "There is nothing to roll back to" is a fact the caller acts on."""
    service, report = world.service(), await world.report()
    first = await service.propose(
        world.ctx(), author_actor_id=world.agent_id, version=1, content="v1", motivated_by_report_id=report
    )
    await service.activate(world.ctx(), instruction_id=first, now=_at(10))

    assert await service.rollback(world.ctx(), author_actor_id=world.agent_id, now=_at(11)) is None
    active = await service.active_instruction(world.ctx(), author_actor_id=world.agent_id)
    assert (
        active is not None and active.status == STATUS_ACTIVE
    ), "a rollback with no predecessor must leave the incumbent in force rather than demoting it into a gap"


@pytest.mark.asyncio
async def test_activating_an_already_active_version_is_a_conflict(world: _World) -> None:
    service, report = world.service(), await world.report()
    first = await service.propose(
        world.ctx(), author_actor_id=world.agent_id, version=1, content="v1", motivated_by_report_id=report
    )
    await service.activate(world.ctx(), instruction_id=first, now=_at(10))

    with pytest.raises(ConflictError, match="already active"):
        await service.activate(world.ctx(), instruction_id=first, now=_at(11))


@pytest.mark.asyncio
async def test_another_tenants_instruction_is_not_found(world: _World) -> None:
    stranger = await _World(world.factory).build()
    report = await stranger.report()
    theirs = await stranger.service().propose(
        stranger.ctx(), author_actor_id=stranger.agent_id, version=1, content="v1", motivated_by_report_id=report
    )

    with pytest.raises(NotFoundError):
        await world.service().activate(world.ctx(), instruction_id=theirs, now=_at(10))
