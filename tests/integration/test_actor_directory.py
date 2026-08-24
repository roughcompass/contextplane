"""The roster, and the principals it refuses to guess about.

E22-T7, on ADR 0019. `/agents` was an "any principal" screen with a text box
asking for an `Agent actor UUID` — the field the user named first — because
there was no roster to populate a list from.

The two properties that carry the decision:

**An undeclared principal is `unknown`, never `human`.** The old default made
every unregistered agent invisible on the screens built to watch agents, and the
failure read as *"we have no agents"* rather than as *"nobody has declared any"*.

**`unknown` rows are returned rather than filtered.** That is the answer to the
dissent ADR 0019 records — integrators will skip the declaration — and it is a
requirement rather than a hope, so it is asserted here.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.auth.entitlements.actor_store import upsert_entitlement_actor
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.service.governance.actors import (
    KIND_UNKNOWN,
    MAX_PAGE_SIZE,
    ActorDirectoryService,
    parse_cursor,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)
_OWNER = "platform-team@example.test"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _tenant(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"dir-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'operator', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"dir-{aid.hex[:8]}", "n": _NOW},
        )
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["admin"])


async def _principal(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    name: str = "some-integration",
    at: datetime.datetime = _NOW,
) -> uuid.UUID:
    """One principal, arriving the way the auth path creates them: undeclared."""
    actor_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, :n, :sub, :at)"
            ),
            {"a": actor_id, "t": ctx.tenant_id, "n": name, "sub": f"s-{actor_id.hex[:10]}", "at": at},
        )
    return actor_id


def _directory(factory: async_sessionmaker[AsyncSession]) -> ActorDirectoryService:
    return ActorDirectoryService(factory, clock=FakeClock(_NOW))


@pytest.mark.asyncio
async def test_a_principal_nobody_declared_is_unknown_and_not_human(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The default flip, and the whole reason for it.

    Under the old default this row would have said `human` — a word nobody
    chose, on a screen built to find agents.
    """
    ctx = await _tenant(factory)
    actor = await _principal(factory, ctx)

    rows = {p.actor_id: p for p in (await _directory(factory).list_principals(ctx)).items}

    assert rows[actor].actor_kind == KIND_UNKNOWN
    assert rows[actor].is_declared is False
    assert rows[actor].owner_principal is None


@pytest.mark.asyncio
async def test_undeclared_principals_are_returned_rather_than_hidden(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The answer to the recorded dissent, asserted rather than hoped for.

    A roster that hid what it did not know would answer "we have no agents" to a
    deployment that has several nobody has declared.
    """
    ctx = await _tenant(factory)
    undeclared = [await _principal(factory, ctx) for _ in range(3)]

    found = {p.actor_id for p in (await _directory(factory).list_principals(ctx)).items}

    assert set(undeclared) <= found


@pytest.mark.asyncio
async def test_declaring_records_what_was_said_and_who_said_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A declaration, not a classification: nothing reads the principal's
    behaviour, transport or event mix."""
    ctx = await _tenant(factory)
    actor = await _principal(factory, ctx)

    declared = await _directory(factory).declare(ctx, actor_id=actor, actor_kind="agent", owner_principal=_OWNER)

    assert declared.actor_kind == "agent"
    assert declared.owner_principal == _OWNER
    assert declared.declared_by == ctx.actor_id
    assert declared.declared_at == _NOW
    assert declared.is_declared is True


@pytest.mark.asyncio
async def test_a_declaration_can_be_corrected(factory: async_sessionmaker[AsyncSession]) -> None:
    """A principal that was a person's session and is now an unattended agent is
    a real change; refusing it would leave the roster wrong in the direction
    that matters."""
    ctx = await _tenant(factory)
    actor = await _principal(factory, ctx)
    service = _directory(factory)

    await service.declare(ctx, actor_id=actor, actor_kind="human", owner_principal=_OWNER)
    corrected = await service.declare(ctx, actor_id=actor, actor_kind="agent", owner_principal=_OWNER)

    assert corrected.actor_kind == "agent"


@pytest.mark.asyncio
async def test_declaring_requires_an_owner_somebody_could_contact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A principal whose owner is unrecorded is one nobody is accountable for,
    and ADR 0019's third assumption is that the roster answers "who do I talk to
    about this agent"."""
    ctx = await _tenant(factory)
    actor = await _principal(factory, ctx)

    with pytest.raises(ValidationError, match="who do I talk to"):
        await _directory(factory).declare(ctx, actor_id=actor, actor_kind="agent", owner_principal="me")


@pytest.mark.asyncio
async def test_a_principal_cannot_be_declared_into_an_internal_kind(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`sync_worker` and `system_curator` are this service's own provisioning,
    and `unknown` is what a principal is before anybody declares it — none of
    the three is a thing a person declares."""
    ctx = await _tenant(factory)
    actor = await _principal(factory, ctx)

    for kind in ("sync_worker", "system_curator", KIND_UNKNOWN):
        with pytest.raises(ValidationError, match="declared as one of"):
            await _directory(factory).declare(ctx, actor_id=actor, actor_kind=kind, owner_principal=_OWNER)


@pytest.mark.asyncio
async def test_declaring_requires_the_operator_bar(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Saying "this is an autonomous agent" changes what every screen built to
    watch agents reports, which is a decision rather than something a
    principal's own evidence implies."""
    ctx = await _tenant(factory)
    actor = await _principal(factory, ctx)
    consumer = TenantContext(tenant_id=ctx.tenant_id, actor_id=ctx.actor_id, roles=["consumer"])

    with pytest.raises(PermissionError, match="requires one of"):
        await _directory(factory).declare(consumer, actor_id=actor, actor_kind="agent", owner_principal=_OWNER)


@pytest.mark.asyncio
async def test_another_tenants_principal_cannot_be_declared_or_seen(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both the read and the write are tenant-scoped in the service, so a second
    transport reaching them cannot arrive without the scoping."""
    mine, theirs = await _tenant(factory), await _tenant(factory)
    ours = await _principal(factory, mine)
    yours = await _principal(factory, theirs)

    found = {p.actor_id for p in (await _directory(factory).list_principals(mine)).items}
    assert ours in found
    assert yours not in found

    with pytest.raises(NotFoundError, match="no such principal"):
        await _directory(factory).declare(mine, actor_id=yours, actor_kind="agent", owner_principal=_OWNER)


@pytest.mark.asyncio
async def test_the_kind_filter_finds_the_agents(factory: async_sessionmaker[AsyncSession]) -> None:
    """The filter a screen called `/agents` actually needs — and the reason the
    default is everybody, so its absence is visible."""
    ctx = await _tenant(factory)
    service = _directory(factory)
    agent = await _principal(factory, ctx)
    human = await _principal(factory, ctx)
    await service.declare(ctx, actor_id=agent, actor_kind="agent", owner_principal=_OWNER)
    await service.declare(ctx, actor_id=human, actor_kind="human", owner_principal=_OWNER)

    found = {p.actor_id for p in (await service.list_principals(ctx, actor_kind="agent")).items}

    assert agent in found
    assert human not in found


@pytest.mark.asyncio
async def test_an_unknown_kind_filter_is_refused(factory: async_sessionmaker[AsyncSession]) -> None:
    ctx = await _tenant(factory)
    with pytest.raises(ValidationError, match="unknown actor kind"):
        await _directory(factory).list_principals(ctx, actor_kind="robot")


@pytest.mark.asyncio
async def test_the_database_refuses_a_kind_nobody_declared(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The vocabulary is closed in the schema as well as the service.

    `actor_kind` had no CHECK, which is how it came to mean "not a sync worker";
    a sixth spelling of one of the five now fails rather than accumulating.
    """
    ctx = await _tenant(factory)
    with pytest.raises(Exception, match="ck_actors_kind"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, actor_kind, created_at) "
                    "VALUES (:a, :t, 'x', :sub, 'robot', :n)"
                ),
                {"a": uuid.uuid4(), "t": ctx.tenant_id, "sub": f"x-{uuid.uuid4().hex[:8]}", "n": _NOW},
            )


@pytest.mark.asyncio
async def test_the_database_refuses_a_declaration_with_no_declarer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A declaration with no declarer is a record of somebody having decided
    that nobody is accountable for."""
    ctx = await _tenant(factory)
    with pytest.raises(Exception, match="ck_actors_declaration_is_attributed"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors "
                    "(actor_id, tenant_id, display_name, oidc_subject, actor_kind, declared_at, created_at) "
                    "VALUES (:a, :t, 'x', :sub, 'agent', :n, :n)"
                ),
                {"a": uuid.uuid4(), "t": ctx.tenant_id, "sub": f"y-{uuid.uuid4().hex[:8]}", "n": _NOW},
            )


@pytest.mark.asyncio
async def test_the_database_refuses_a_declared_principal_with_no_kind(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`unknown` with a declaration attached is a form filled in and left blank,
    which reads afterwards as a decision nobody made."""
    ctx = await _tenant(factory)
    with pytest.raises(Exception, match="ck_actors_declared_kind_is_known"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors "
                    "(actor_id, tenant_id, display_name, oidc_subject, actor_kind,"
                    " declared_at, declared_by, created_at) "
                    "VALUES (:a, :t, 'x', :sub, 'unknown', :n, :by, :n)"
                ),
                {
                    "a": uuid.uuid4(),
                    "t": ctx.tenant_id,
                    "sub": f"z-{uuid.uuid4().hex[:8]}",
                    "n": _NOW,
                    "by": ctx.actor_id,
                },
            )


@pytest.mark.asyncio
async def test_the_cursor_walks_the_roster_without_repeating_or_skipping(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _tenant(factory)
    written = [await _principal(factory, ctx, at=_NOW - datetime.timedelta(minutes=index)) for index in range(5)]

    seen: list[uuid.UUID] = []
    cursor = None
    for _ in range(20):
        page = await _directory(factory).list_principals(ctx, cursor=cursor, page_size=2)
        seen.extend(p.actor_id for p in page.items)
        if page.next_cursor is None:
            break
        cursor = parse_cursor(page.next_cursor)

    assert [actor for actor in seen if actor in set(written)] == written
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_a_page_larger_than_the_ceiling_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _tenant(factory)
    with pytest.raises(ValidationError, match="page_size"):
        await _directory(factory).list_principals(ctx, page_size=MAX_PAGE_SIZE + 1)


@pytest.mark.asyncio
async def test_a_person_signing_in_is_declared_human_rather_than_left_unknown(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The regression `0084` shipped, and the reason the default is not the whole
    story.

    Flipping the column default to `unknown` is right for a principal nobody
    declared. It is wrong for one the identity provider just asserted: the
    entitlement service materialises an actor row for an authenticated OIDC
    end-user, and that row *is* the declaration landing in the database.

    Left to the default, every person who signs in became `unknown`, and every
    guard that asks whether the caller is human refused them -- claim
    confirmation most visibly, which failed with a 403 telling a person that
    "only a human principal may confirm a claim". Three integration files caught
    it; none of them was about actors, and the one gate that would have named the
    cause did not exist. It does now.
    """
    ctx = await _tenant(factory)
    async with factory() as session, session.begin():
        actor_id = await upsert_entitlement_actor(
            session,
            tenant_id=ctx.tenant_id,
            oidc_subject=f"oidc-{uuid.uuid4().hex[:8]}",
            display_name="A Person",
        )

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT actor_kind, declared_at, declared_by FROM actors WHERE actor_id = :a"),
                {"a": actor_id},
            )
        ).one()

    assert row.actor_kind == "human"
    assert row.declared_at is not None, "a kind with no declaration is one the schema calls undeclared"
    assert row.declared_by == actor_id, (
        "attributed to the row itself: the entitlement service has no actor row to point at, "
        "and the schema requires an attribution alongside the timestamp"
    )
