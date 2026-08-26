"""Authentication must not lock the caller's own row for the whole request.

`upsert_entitlement_actor` runs on the request-scoped session, inside the
transaction that stays open until the response is written. While it wrote
unconditionally — `INSERT … ON CONFLICT DO UPDATE`, whose purpose on the common
path is only to return an `actor_id` that already exists — it took a row-exclusive
lock on the caller's own actor row and held it for the rest of the request.

Every service in this repository opens its own session. So any endpoint writing
to `actors` deadlocked the request against itself: the handler's connection waited
on a lock held by the *same request's* authentication on a different connection,
and nothing could release it. `POST /v1/admin/actors/{id}/declare` on the caller's
own actor is the reachable case. Measured before the fix: HTTP 500 after exactly
30.03 s — the statement timeout, not a crash — with `pg_blocking_pids` naming the
request's own authentication backend as the blocker. Declaring a *different*
actor returned 200, which is what made it look like a permissions problem rather
than a locking one.

**These tests assert on the lock, not on the endpoint.** A test that declared
through HTTP would go red for this bug and also for a routing change, a
permission change or a schema change, and its failure would not say which. The
invariant is narrower and outlives the endpoint that exposed it: *after
authentication has resolved an actor whose row is unchanged, another connection
can still write that row.* `lock_timeout` turns the violation into a fast, legible
error instead of a hang — before the fix these fail in about a second, rather
than blocking a worker until something else gives up.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.auth.entitlements.actor_store import upsert_entitlement_actor

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tenant_id, "s": f"lock-{tenant_id.hex[:8]}", "n": _NOW},
        )
    return tenant_id


async def _write_from_another_connection(factory: async_sessionmaker[AsyncSession], actor_id: uuid.UUID) -> None:
    """What a service handler does: its own session, its own transaction.

    `lock_timeout` rather than the deployment default, so a regression surfaces
    as a failed assertion in about a second instead of as a test run that appears
    to hang.
    """
    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL lock_timeout = '1s'"))
        await session.execute(
            text("UPDATE actors SET owner_principal = 'platform-team' WHERE actor_id = :a"),
            {"a": actor_id},
        )


@pytest.mark.asyncio
async def test_a_resolved_caller_does_not_hold_a_write_lock_on_their_own_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bug, at the smallest scope that can express it.

    Two sessions, because that is the shape of the failure: authentication holds
    the request-scoped transaction open, and the handler that follows it writes
    from a different connection. One session could never reproduce this — a
    transaction never blocks on a lock it holds itself.
    """
    tenant_id = await _tenant(factory)
    subject = f"caller-{uuid.uuid4().hex[:10]}"

    async with factory() as first_sight, first_sight.begin():
        actor_id = await upsert_entitlement_actor(first_sight, tenant_id, subject, "Morgan Morris")

    async with factory() as auth, auth.begin():
        resolved = await upsert_entitlement_actor(auth, tenant_id, subject, "Morgan Morris")
        assert resolved == actor_id, "re-sight must return the same actor, not mint a second one"

        # `auth`'s transaction is deliberately still open here — that is the
        # whole point. Before the fix this blocked until lock_timeout fired.
        try:
            await _write_from_another_connection(factory, actor_id)
        except OperationalError as exc:  # pragma: no cover - the regression path
            pytest.fail(
                "authentication is holding a write lock on the caller's own actor row for the "
                f"duration of the request, so any handler that writes that row deadlocks: {exc}"
            )


@pytest.mark.asyncio
async def test_first_sight_still_creates_the_actor(factory: async_sessionmaker[AsyncSession]) -> None:
    """Reading before writing must not turn a first sight into a missing row.

    The fix short-circuits on an existing, unchanged row. A principal nobody has
    seen has neither, so this path still writes — and it is the path whose whole
    purpose is to write.
    """
    tenant_id = await _tenant(factory)
    subject = f"newcomer-{uuid.uuid4().hex[:10]}"

    async with factory() as session, session.begin():
        actor_id = await upsert_entitlement_actor(session, tenant_id, subject, "Newly Arrived")

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT display_name, actor_kind FROM actors WHERE actor_id = :a"),
                {"a": actor_id},
            )
        ).one()
    assert row[0] == "Newly Arrived"
    assert row[1] == "human", "an identity the entitlement service asserted is declared human, not unknown"


@pytest.mark.asyncio
async def test_a_new_display_name_from_the_identity_provider_still_propagates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The documented re-sight behaviour, which the read-first path could have dropped.

    `display_name` is refreshed on conflict so that a token whose `name` claim has
    since been populated upstream lands without a separate sync. Short-circuiting
    on *existence* alone would have silently ended that; the short-circuit is on
    existence **and** an unchanged name, and this is the test that says so.
    """
    tenant_id = await _tenant(factory)
    subject = f"renamed-{uuid.uuid4().hex[:10]}"

    async with factory() as session, session.begin():
        actor_id = await upsert_entitlement_actor(session, tenant_id, subject, subject)

    async with factory() as session, session.begin():
        again = await upsert_entitlement_actor(session, tenant_id, subject, "Morgan Morris")
    assert again == actor_id

    async with factory() as session:
        name = (
            await session.execute(text("SELECT display_name FROM actors WHERE actor_id = :a"), {"a": actor_id})
        ).scalar_one()
    assert name == "Morgan Morris"


@pytest.mark.asyncio
async def test_a_missing_display_name_falls_back_to_the_subject(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`display_name` is NOT NULL, and the comparison has to agree with the write.

    The write substitutes `oidc_subject` when no display name is supplied. If the
    read-first comparison did not substitute identically, every request for a
    principal without a name claim would see "stored ≠ supplied", take the write
    path, and reinstate exactly the lock this change removed — while still
    returning the right answer, so nothing would look wrong.
    """
    tenant_id = await _tenant(factory)
    subject = f"nameless-{uuid.uuid4().hex[:10]}"

    async with factory() as session, session.begin():
        actor_id = await upsert_entitlement_actor(session, tenant_id, subject, None)

    async with factory() as auth, auth.begin():
        assert await upsert_entitlement_actor(auth, tenant_id, subject, None) == actor_id
        try:
            await _write_from_another_connection(factory, actor_id)
        except OperationalError as exc:  # pragma: no cover - the regression path
            pytest.fail(f"a principal with no display-name claim still takes the write path: {exc}")
