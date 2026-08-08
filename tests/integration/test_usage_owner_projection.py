"""The owner projection: what everyone did with the capabilities you publish.

Two things here are easy to get wrong in ways that produce a plausible answer, so
both are asserted directly.

**Scoping by the caller's tenant instead of by ownership.** The capability rollup is
keyed by the *calling* tenant, so the natural query — filter on `tenant_id` like every
other read in this module — silently answers "how much do I call my own capability".
That is a number, it is never zero, and it is not what a publisher asked. The
headline test seeds calls from a *different* tenant and asserts the owner sees them.

**Admitting only admins.** A resolved principal carries exactly one collapsed role, so
a gate written as "admin" excludes every actual publisher. There is a producer-only
test for that specifically.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.usage import reads
from contextplane.usage.rollups import roll_up_day
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

type _Harness = tuple[EntitlementAuthHarness, AsyncClient]

_TODAY = datetime.datetime.now(tz=datetime.UTC).date()


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


async def _factory(pg_url: str):
    engine = create_async_engine(pg_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _own_capability(factory, owner_tenant: uuid.UUID, name: str) -> uuid.UUID:
    entity_id = uuid.uuid4()
    async with factory() as session:
        await session.execute(
            text("INSERT INTO entities (entity_id, tenant_id, entity_type, name)" " VALUES (:e, :t, 'capability', :n)"),
            {"e": entity_id, "t": owner_tenant, "n": name},
        )
        await session.commit()
    return entity_id


async def _call(
    factory,
    *,
    caller_tenant: uuid.UUID,
    capability: uuid.UUID,
    count: int = 1,
    outcome: str = "ok",
    payload_bytes: int | None = 100,
    day: datetime.date | None = None,
) -> None:
    """Record `count` calls from `caller_tenant` touching `capability`."""
    occurred = datetime.datetime.combine(day or _TODAY, datetime.time(9, 0), tzinfo=datetime.UTC)
    async with factory() as session:
        for _ in range(count):
            await session.execute(
                text(
                    "INSERT INTO usage_events (event_id, occurred_at, tenant_id, actor_id, surface,"
                    " operation, outcome, status_class, latency_ms, subject_entity_ids, payload_bytes)"
                    " VALUES (:e,:o,:t,:a,'rest','/v1/capabilities/{entity_id}',:oc,:sc,10,:se,:pb)"
                ),
                {
                    "e": uuid.uuid4(),
                    "o": occurred,
                    "t": caller_tenant,
                    "a": uuid.uuid4(),
                    "oc": outcome,
                    "sc": "2xx" if outcome == "ok" else "5xx",
                    "se": [capability],
                    "pb": payload_bytes,
                },
            )
        await session.commit()


async def _get(harness: EntitlementAuthHarness, client: AsyncClient, persona: TenantPersona, path: str):
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        return await client.get(path, headers=bearer_headers(tenant_slug=persona.slug))


# ---------------------------------------------------------------------------
# The scoping the whole endpoint turns on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_owner_sees_calls_made_by_another_tenant(pg_container: str, harness: _Harness) -> None:
    """The question a publisher is actually asking.

    Scoping this like every other read in the module — on the rollup's `tenant_id` —
    would return only the owner's own calls to their own capability. That is a
    plausible non-zero number and the wrong answer, so the fixture makes the owner
    make *no* calls at all: every call here comes from somebody else.
    """
    app_harness, client = harness
    owner = app_harness.add_persona(f"owner-{uuid.uuid4().hex[:6]}", roles=["producer"])
    consumer = app_harness.add_persona(f"consumer-{uuid.uuid4().hex[:6]}", roles=["consumer"])
    owner_tenant = await _materialise(app_harness, owner)
    consumer_tenant = await _materialise(app_harness, consumer)

    engine, factory = await _factory(pg_container)
    try:
        cap = await _own_capability(factory, owner_tenant, "payments-api")
        await _call(factory, caller_tenant=consumer_tenant, capability=cap, count=4)
        await _call(factory, caller_tenant=consumer_tenant, capability=cap, count=1, outcome="error")
        await roll_up_day(factory, _TODAY)

        resp = await _get(app_harness, client, owner, "/v1/usage/owned-capabilities")

        assert resp.status_code == 200, resp.text
        (row,) = resp.json()["capabilities"]
        assert row["capability_id"] == str(cap)
        assert row["name"] == "payments-api"
        assert row["calls"] == 5
        assert row["ok_calls"] == 4
        assert row["error_calls"] == 1
        assert row["payload_bytes"] == 500
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_calls_from_several_tenants_are_summed_without_naming_them(pg_container: str, harness: _Harness) -> None:
    """Totals, and no per-consumer breakdown.

    Telling an owner their capability is used is the point. Telling them how heavily
    each named customer leans on it is a different disclosure, and not the one this
    endpoint was asked to make — so the response carries no tenant field at all.
    """
    app_harness, client = harness
    owner = app_harness.add_persona(f"owner-multi-{uuid.uuid4().hex[:6]}", roles=["producer"])
    owner_tenant = await _materialise(app_harness, owner)
    a = app_harness.add_persona(f"caller-a-{uuid.uuid4().hex[:6]}", roles=["consumer"])
    b = app_harness.add_persona(f"caller-b-{uuid.uuid4().hex[:6]}", roles=["consumer"])
    tenant_a = await _materialise(app_harness, a)
    tenant_b = await _materialise(app_harness, b)

    engine, factory = await _factory(pg_container)
    try:
        cap = await _own_capability(factory, owner_tenant, "ledger-api")
        await _call(factory, caller_tenant=tenant_a, capability=cap, count=3)
        await _call(factory, caller_tenant=tenant_b, capability=cap, count=7)
        await roll_up_day(factory, _TODAY)

        resp = await _get(app_harness, client, owner, "/v1/usage/owned-capabilities")

        assert resp.status_code == 200, resp.text
        (row,) = resp.json()["capabilities"]
        assert row["calls"] == 10

        body = resp.text
        assert (
            str(tenant_a) not in body and str(tenant_b) not in body
        ), "the response identifies a calling tenant; totals only is the disclosure boundary"
        assert "tenant" not in set(row), f"unexpected tenant field in the response: {sorted(row)}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_capability_owned_by_someone_else_is_not_in_the_answer(pg_container: str, harness: _Harness) -> None:
    """Ownership is the filter, and the fixture gives the wrong answer something to be.

    The other tenant's capability has *more* traffic than the owner's, so a missing
    ownership filter returns it first rather than returning nothing.
    """
    app_harness, client = harness
    owner = app_harness.add_persona(f"owner-iso-{uuid.uuid4().hex[:6]}", roles=["producer"])
    rival = app_harness.add_persona(f"rival-{uuid.uuid4().hex[:6]}", roles=["producer"])
    owner_tenant = await _materialise(app_harness, owner)
    rival_tenant = await _materialise(app_harness, rival)

    engine, factory = await _factory(pg_container)
    try:
        mine = await _own_capability(factory, owner_tenant, "mine")
        theirs = await _own_capability(factory, rival_tenant, "theirs")
        await _call(factory, caller_tenant=owner_tenant, capability=mine, count=2)
        await _call(factory, caller_tenant=owner_tenant, capability=theirs, count=9)
        await roll_up_day(factory, _TODAY)

        resp = await _get(app_harness, client, owner, "/v1/usage/owned-capabilities")

        assert resp.status_code == 200, resp.text
        rows = resp.json()["capabilities"]
        assert [r["name"] for r in rows] == ["mine"]
        assert rows[0]["calls"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_owned_capability_nobody_called_is_absent(pg_container: str, harness: _Harness) -> None:
    # This reads usage, not the catalog. Inventing a zero row for every owned entity
    # would make the response a catalog listing with a usage column bolted on, which
    # the entity endpoints already do properly.
    app_harness, client = harness
    owner = app_harness.add_persona(f"owner-quiet-{uuid.uuid4().hex[:6]}", roles=["producer"])
    owner_tenant = await _materialise(app_harness, owner)

    engine, factory = await _factory(pg_container)
    try:
        await _own_capability(factory, owner_tenant, "never-called")

        resp = await _get(app_harness, client, owner, "/v1/usage/owned-capabilities")

        assert resp.status_code == 200, resp.text
        assert resp.json()["capabilities"] == []
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# The gate, and the role-collapse trap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_producer_without_admin_can_read_it(pg_container: str, harness: _Harness) -> None:
    """The reader this endpoint exists for, and the one a natural gate excludes.

    A resolved principal carries exactly one collapsed role, so a publisher's context
    holds `producer` and nothing else. Gating on `admin` would keep out every actual
    owner while looking correct in review.
    """
    app_harness, client = harness
    producer = app_harness.add_persona(f"producer-only-{uuid.uuid4().hex[:6]}", roles=["producer"])
    tenant = await _materialise(app_harness, producer)

    # The context really does hold producer alone, or this test proves nothing.
    harness_ctx_roles = producer.roles
    assert harness_ctx_roles == ["producer"], f"fixture holds {harness_ctx_roles}, not producer alone"

    engine, factory = await _factory(pg_container)
    try:
        cap = await _own_capability(factory, tenant, "producer-readable")
        await _call(factory, caller_tenant=tenant, capability=cap, count=1)
        await roll_up_day(factory, _TODAY)

        resp = await _get(app_harness, client, producer, "/v1/usage/owned-capabilities")

        assert resp.status_code == 200, resp.text
        assert [r["name"] for r in resp.json()["capabilities"]] == ["producer-readable"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_admin_can_read_it_too(harness: _Harness) -> None:
    # Admin is not excluded either: in a small tenant the person publishing and the
    # person administering are the same, and making them switch principals to see
    # their own usage would be a gate that teaches people to over-grant roles.
    app_harness, client = harness
    admin = app_harness.add_persona(f"admin-owner-{uuid.uuid4().hex[:6]}", roles=["admin"])
    await _materialise(app_harness, admin)

    resp = await _get(app_harness, client, admin, "/v1/usage/owned-capabilities")

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_consumer_cannot_read_it(harness: _Harness) -> None:
    app_harness, client = harness
    consumer = app_harness.add_persona(f"consumer-denied-{uuid.uuid4().hex[:6]}", roles=["consumer"])
    await _materialise(app_harness, consumer)

    resp = await _get(app_harness, client, consumer, "/v1/usage/owned-capabilities")

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_this_is_a_separate_endpoint_from_the_operator_surface(harness: _Harness) -> None:
    """Two surfaces, not one with a widened role list.

    A single endpoint whose scoping changed with the caller's role would be read
    wrong in review and then misinterpreted in a dashboard: the same field would mean
    "what my organisation calls" for one reader and "what everyone calls of mine" for
    another.
    """
    app_harness, _ = harness
    paths = {getattr(r, "path", "") for r in app_harness.app.routes}

    assert "/v1/usage/owned-capabilities" in paths
    assert "/v1/admin/usage/capabilities" in paths
    # And the producer surface is not reachable under the admin prefix, which is what
    # a widened role list would have looked like.
    assert not any(p.startswith("/v1/admin/usage/owned") for p in paths)


# ---------------------------------------------------------------------------
# Window handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_day_outside_the_window_is_not_counted(pg_container: str, harness: _Harness) -> None:
    app_harness, client = harness
    owner = app_harness.add_persona(f"owner-window-{uuid.uuid4().hex[:6]}", roles=["producer"])
    tenant = await _materialise(app_harness, owner)

    engine, factory = await _factory(pg_container)
    try:
        cap = await _own_capability(factory, tenant, "windowed")
        long_ago = _TODAY - datetime.timedelta(days=60)
        await _call(factory, caller_tenant=tenant, capability=cap, count=3, day=long_ago)
        await _call(factory, caller_tenant=tenant, capability=cap, count=1)
        await roll_up_day(factory, long_ago)
        await roll_up_day(factory, _TODAY)

        default_window = await _get(app_harness, client, owner, "/v1/usage/owned-capabilities")
        wide = await _get(
            app_harness,
            client,
            owner,
            f"/v1/usage/owned-capabilities?from={long_ago.isoformat()}&to={_TODAY.isoformat()}",
        )

        # The default window is the last 30 days, so the 60-day-old calls are out.
        assert default_window.json()["capabilities"][0]["calls"] == 1
        assert wide.json()["capabilities"][0]["calls"] == 4
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_window_wider_than_the_cap_is_a_422(harness: _Harness) -> None:
    app_harness, client = harness
    owner = app_harness.add_persona(f"owner-wide-{uuid.uuid4().hex[:6]}", roles=["producer"])
    await _materialise(app_harness, owner)

    resp = await _get(app_harness, client, owner, "/v1/usage/owned-capabilities?from=2020-01-01&to=2026-01-01")

    assert resp.status_code == 422, resp.text
    assert "maximum" in resp.text


# ---------------------------------------------------------------------------
# The rollup columns this projection needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_capability_rollup_records_the_outcome_mix(pg_container: str) -> None:
    """Added for this projection, and asserted at the rollup rather than through HTTP.

    "Two thousand calls" and "two thousand calls, four hundred of them errors" are the
    same number and completely different situations, and the second is the owner's to
    fix.
    """
    engine, factory = await _factory(pg_container)
    tenant = uuid.uuid4()
    cap = uuid.uuid4()
    try:
        await _call(factory, caller_tenant=tenant, capability=cap, count=3, payload_bytes=10)
        await _call(factory, caller_tenant=tenant, capability=cap, count=2, outcome="error", payload_bytes=None)
        await roll_up_day(factory, _TODAY)

        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT calls, ok_calls, error_calls, payload_bytes"
                        " FROM usage_rollup_capability_day WHERE tenant_id = :t AND capability_id = :c"
                    ),
                    {"t": tenant, "c": cap},
                )
            ).one()

        assert row == (5, 3, 2, 30)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unmeasured_payload_size_stays_null_rather_than_zero(pg_container: str) -> None:
    # Null means nothing measured it — an MCP call, a streaming response. Zero would
    # claim the capability returned nothing, which is a different and wrong statement.
    engine, factory = await _factory(pg_container)
    tenant, cap = uuid.uuid4(), uuid.uuid4()
    try:
        await _call(factory, caller_tenant=tenant, capability=cap, count=2, payload_bytes=None)
        await roll_up_day(factory, _TODAY)

        async with factory() as session:
            payload = (
                await session.execute(
                    text(
                        "SELECT payload_bytes FROM usage_rollup_capability_day"
                        " WHERE tenant_id = :t AND capability_id = :c"
                    ),
                    {"t": tenant, "c": cap},
                )
            ).scalar_one()

        assert payload is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_projection_never_returns_more_than_the_limit(pg_container: str, harness: _Harness) -> None:
    app_harness, client = harness
    owner = app_harness.add_persona(f"owner-limit-{uuid.uuid4().hex[:6]}", roles=["producer"])
    tenant = await _materialise(app_harness, owner)

    engine, factory = await _factory(pg_container)
    try:
        for i in range(5):
            cap = await _own_capability(factory, tenant, f"cap-{i}")
            await _call(factory, caller_tenant=tenant, capability=cap, count=i + 1)
        await roll_up_day(factory, _TODAY)

        resp = await _get(app_harness, client, owner, "/v1/usage/owned-capabilities?limit=2")

        assert resp.status_code == 200, resp.text
        rows = resp.json()["capabilities"]
        # Top two by calls, not an arbitrary two.
        assert [r["name"] for r in rows] == ["cap-4", "cap-3"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_read_function_refuses_a_backwards_window(pg_container: str) -> None:
    engine, factory = await _factory(pg_container)
    try:
        with pytest.raises(reads.InvalidRangeError):
            await reads.read_owned_capability_usage(
                factory,
                owner_tenant_id=uuid.uuid4(),
                start=_TODAY,
                end=_TODAY - datetime.timedelta(days=1),
            )
    finally:
        await engine.dispose()
