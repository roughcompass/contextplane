"""The first place anything consults an autonomy envelope.

Everything the autonomy epic built -- the binding, the matrix, the decision, the
advisory records, the graduation pre-flight -- was unreachable until a route
called it. A governance object nothing consults governs nothing, and the
graduation scan in particular cannot observe a population that never produces a
record, so before this a tenant could never leave `advisory` for a reason having
nothing to do with its agents.

Two properties, and the first is what makes wiring this in safe to do before
anybody has an envelope:

**An advisory tenant is never refused.** Every deployment starts there, and no
principal has an envelope on day one, so if this were wrong the gate would refuse
every write in every deployment the moment it shipped.

**And it is recorded anyway.** The record is the only thing that can later move
the tenant to enforcing, so an advisory stage that refused nothing *and* recorded
nothing would be indistinguishable from not having wired the gate at all --
which is exactly the failure this file exists to catch.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_EVENT = {"kind": "user_message", "body": "what did we decide about retries?"}


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


type _Gate = tuple[AsyncClient, TenantPersona, uuid.UUID]


@pytest_asyncio.fixture
async def gate(pg_container: str) -> AsyncIterator[_Gate]:
    """An authenticated writer, its persona, and the tenant its writes land in.

    The persona travels with the client because every request has to be made
    inside `patch_validator_for_actor` -- the harness validates a dummy JWT
    against the persona rather than minting a real one.
    """
    slug = f"envgate-{secrets.token_hex(4)}"
    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["admin"], actor_id=uuid.uuid4())
        harness.configure_fetcher_for(persona)
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert whoami.status_code == 200, whoami.text
            yield client, persona, uuid.UUID(whoami.json()["tenant_id"])


async def _write(client: AsyncClient, persona: TenantPersona, body: str | None = None) -> Response:
    """One session event, authenticated the way the harness requires."""
    payload = dict(_EVENT) if body is None else {"kind": "user_message", "body": body}
    with patch_validator_for_actor(persona):
        return await client.post(
            f"/v1/memory/sessions/{uuid.uuid4()}/events",
            json=payload,
            headers=bearer_headers(tenant_slug=persona.slug),
        )


async def _advisory_records(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT verdict FROM arc_envelope_advisory_records WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).all()
    return [r[0] for r in rows]


async def _graduate(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE tenants SET envelope_enforcement_stage = 'enforcing' WHERE tenant_id = :t"),
            {"t": tenant_id},
        )


@pytest.mark.asyncio
async def test_an_advisory_write_succeeds_and_is_recorded(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Both halves of the bargain, in one request.

    This principal has no envelope, which under enforcement is the hardest
    refusal there is. The write must still succeed -- and the record must exist,
    or the graduation scan has nothing to count and the tenant can never leave
    advisory.
    """
    client, persona, tenant_id = gate

    response = await _write(client, persona)

    assert response.status_code == 201, response.text
    assert "no_envelope" in await _advisory_records(factory, tenant_id)


@pytest.mark.asyncio
async def test_an_enforcing_tenant_is_refused_with_a_verdict_bearing_code(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The refusal names *which* refusal, and nothing else.

    A caller does need to tell "nobody has granted me an envelope" from "this act
    is outside the one I have": the first is an operator's ticket, the second is
    the agent doing something it should not. What it must not learn is the shape
    of the matrix governing it, which it could otherwise map one probe at a time.
    """
    client, persona, tenant_id = gate
    await _graduate(factory, tenant_id)

    response = await _write(client, persona)

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["errors"][0]["code"] == "envelope_absent"
    message = body["errors"][0]["message"]
    assert "envelope" in message
    for leak in ("revision", "rule", "binding", "scope"):
        assert leak not in message.lower(), f"the refusal discloses {leak}"


@pytest.mark.asyncio
async def test_an_enforcing_refusal_writes_no_advisory_record(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The record answers "what would this have done", and a refusal that
    actually refused has already answered it."""
    client, persona, tenant_id = gate
    before = len(await _advisory_records(factory, tenant_id))

    await _graduate(factory, tenant_id)
    await _write(client, persona)

    assert len(await _advisory_records(factory, tenant_id)) == before


@pytest.mark.asyncio
async def test_graduating_changes_the_next_write_with_no_restart(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The stage is read per request, so the flip lands on the next write.

    The same client and the same app instance, with nothing invalidated in
    between -- which is the property the whole no-cache design rests on, observed
    here through HTTP rather than through the service.
    """
    client, persona, tenant_id = gate
    assert (await _write(client, persona)).status_code == 201

    await _graduate(factory, tenant_id)

    assert (await _write(client, persona)).status_code == 403


@pytest.mark.asyncio
async def test_the_envelope_gate_runs_before_the_pii_scan(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Authority first, then content.

    A principal with no authority to write should be told that, rather than
    having its body scanned first and refused on content it was never entitled
    to submit. Ordering is observable only when both would refuse, so this sends
    a body that would also trip admission.
    """
    client, persona, tenant_id = gate
    await _graduate(factory, tenant_id)

    response = await _write(client, persona, body="card 4111 1111 1111 1111 and ssn 123-45-6789")

    assert response.status_code == 403, response.text
    assert response.json()["errors"][0]["code"] == "envelope_absent"


# --- E1-T11: the stream's declared tier reaches the decision -------------------


async def _register_stream(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    system: str,
    namespace: str,
    tier: str,
) -> None:
    """Declare a stream's handling tier, the way an operator would."""
    async with factory() as session, session.begin():
        actor = (
            await session.execute(text("SELECT actor_id FROM actors WHERE tenant_id = :t LIMIT 1"), {"t": tenant_id})
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO memory_source_namespaces "
                "(tenant_id, source_system, source_namespace, data_sensitivity, registered_by, reason) "
                "VALUES (:t, :sys, :ns, :tier, :actor, :reason)"
            ),
            {
                "t": tenant_id,
                "sys": system,
                "ns": namespace,
                "tier": tier,
                "actor": actor,
                "reason": "declared by the operator for this test, at least five words long",
            },
        )


async def _replay(client: AsyncClient, persona: TenantPersona, *, system: str, namespace: str) -> Response:
    """One event declaring the stream it was replayed from."""
    with patch_validator_for_actor(persona):
        return await client.post(
            f"/v1/memory/sessions/{uuid.uuid4()}/events",
            json={
                **_EVENT,
                "external": {
                    "source_system": system,
                    "source_namespace": namespace,
                    "external_record_id": f"msg-{uuid.uuid4()}",
                    "observed_at": "2026-08-01T10:00:00Z",
                },
            },
            headers=bearer_headers(tenant_slug=persona.slug),
        )


async def _recorded_tiers(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[str | None]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT data_sensitivity FROM arc_envelope_advisory_records "
                    "WHERE tenant_id = :t ORDER BY decided_at"
                ),
                {"t": tenant_id},
            )
        ).all()
    return [r[0] for r in rows]


@pytest.mark.asyncio
async def test_a_registered_stream_is_judged_at_the_tier_its_operator_declared(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The whole point of the registration: the tier is looked up, not accepted.

    The exporter says which stream an event came from; an operator says, once,
    what that stream's content is. Taking the tier from the request instead would
    make a payroll replay exactly as sensitive as its exporter felt like claiming.
    """
    client, persona, tenant_id = gate
    await _register_stream(factory, tenant_id, system="chat", namespace="slack", tier="internal")

    response = await _replay(client, persona, system="chat", namespace="slack")

    assert response.status_code == 201, response.text
    assert "internal" in await _recorded_tiers(factory, tenant_id)


@pytest.mark.asyncio
async def test_an_undeclared_stream_records_no_tier_rather_than_a_safe_looking_one(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Null, not `restricted`, and the distinction is the point.

    An absent tier is already read as most restrictive by the selection engine,
    so the *decision* is the strict one either way. Writing `restricted` into the
    record would make an operator's omission indistinguishable from a stream
    somebody looked at and classified, and only one of those is a thing to fix.
    """
    client, persona, tenant_id = gate

    response = await _replay(client, persona, system="chat", namespace="unregistered")

    assert response.status_code == 201, response.text
    assert await _recorded_tiers(factory, tenant_id) == [None]


@pytest.mark.asyncio
async def test_a_local_event_carries_no_stream_and_so_no_tier(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The common case stays untouched.

    An agent writing its own reasoning has no upstream stream to look up, so the
    route performs no registry read and the manifest carries no tier -- which is
    the same strict reading an unregistered stream gets, arrived at without a
    query.
    """
    client, persona, tenant_id = gate

    response = await _write(client, persona)

    assert response.status_code == 201, response.text
    assert await _recorded_tiers(factory, tenant_id) == [None]
