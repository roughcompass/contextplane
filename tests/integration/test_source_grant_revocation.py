"""Withdrawing a source grant, and what it does and does not reach.

E14-T2. The behaviour that matters is not that the column exists — it is that
admission refuses afterwards and that what was already admitted is untouched.
Both are things only a database can answer.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.queries import source_admission as queries

_NOW = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.UTC)
_REASON = "Registered with a wildcard host during the migration and never narrowed."


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _actor(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"gr-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'a', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"g-{aid.hex[:8]}", "n": _NOW},
        )
    return tid, aid


async def _connector(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> str:
    cid = f"conn-{uuid.uuid4().hex[:8]}"
    async with factory() as session, session.begin():
        await queries.insert_connector(
            session,
            connector_id=cid,
            owning_scope="tenant",
            tenant_id=tenant_id,
            allowed_schemes=["https"],
            allowed_hosts=["policy.example"],
            allowed_media_types=["application/pdf"],
            allowed_verifier_ids=["verifier-a"],
            max_bytes=1024,
            credential_ref=None,
            registered_at=_NOW,
        )
    return cid


@pytest.mark.asyncio
async def test_a_connector_can_be_withdrawn_at_all(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole of E14-T2 in one assertion.

    Before this there was no column, no route and no query that could write one:
    a connector registered permissively was permanent.
    """
    tid, aid = await _actor(factory)
    cid = await _connector(factory, tid)

    async with factory() as session, session.begin():
        changed = await queries.revoke_connector(session, connector_id=cid, actor_id=aid, reason=_REASON, now=_NOW)

    assert changed
    async with factory() as session:
        row = await queries.load_connector(session, cid)
    assert row is not None
    assert row.revoked_at == _NOW


@pytest.mark.asyncio
async def test_a_second_withdrawal_is_refused_rather_than_overwriting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two operators withdrawing the same connector leave the first decision and
    the first reason, not the last writer's."""
    tid, aid = await _actor(factory)
    cid = await _connector(factory, tid)

    async with factory() as session, session.begin():
        first = await queries.revoke_connector(session, connector_id=cid, actor_id=aid, reason=_REASON, now=_NOW)
    later = _NOW + datetime.timedelta(hours=1)
    async with factory() as session, session.begin():
        second = await queries.revoke_connector(
            session, connector_id=cid, actor_id=aid, reason="A different reason entirely.", now=later
        )

    assert (first, second) == (True, False)
    async with factory() as session:
        row = await queries.load_connector(session, cid)
    assert row is not None
    assert row.revoked_at == _NOW, "the first withdrawal is the one that stands"


@pytest.mark.asyncio
async def test_the_database_refuses_a_withdrawal_nobody_is_accountable_for(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `revoked_at` with no actor and no reason is a withdrawal that cannot be
    reviewed. Written directly, because the point is that the constraint holds
    against a writer bypassing the service."""
    tid, _aid = await _actor(factory)
    cid = await _connector(factory, tid)

    with pytest.raises(Exception, match="ck_arc_source_connectors_revocation_is_attributed"):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE arc_source_connectors SET revoked_at = :n WHERE connector_id = :c"),
                {"n": _NOW, "c": cid},
            )


@pytest.mark.asyncio
async def test_a_one_word_reason_is_refused_by_the_database_too(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The floor is in the CHECK rather than trusted to the service, because a
    reason of "oops" is the same as none for anybody reading it later."""
    tid, aid = await _actor(factory)
    cid = await _connector(factory, tid)

    with pytest.raises(Exception, match="ck_arc_source_connectors_revocation_is_attributed"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE arc_source_connectors "
                    "SET revoked_at = :n, revoked_by = :a, revocation_reason = 'oops' "
                    "WHERE connector_id = :c"
                ),
                {"n": _NOW, "a": aid, "c": cid},
            )


@pytest.mark.asyncio
async def test_an_upload_policy_withdraws_the_same_way(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same grant, pushed rather than pulled. Asserted separately because
    two tables that are supposed to behave identically are exactly the pair that
    drifts."""
    tid, aid = await _actor(factory)
    pid = f"pol-{uuid.uuid4().hex[:8]}"
    async with factory() as session, session.begin():
        await queries.insert_upload_policy(
            session,
            policy_id=pid,
            owning_scope="tenant",
            tenant_id=tid,
            allowed_media_types=["application/pdf"],
            allowed_verifier_ids=["verifier-a"],
            max_bytes=1024,
            registered_at=_NOW,
        )
        changed = await queries.revoke_upload_policy(session, policy_id=pid, actor_id=aid, reason=_REASON, now=_NOW)

    assert changed
    async with factory() as session:
        row = await queries.load_upload_policy(session, pid)
    assert row is not None
    assert row.revoked_at == _NOW
