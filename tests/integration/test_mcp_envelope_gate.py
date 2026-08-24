"""The envelope reaches the transport an agent actually uses.

E7-T3a. `tests/integration/test_memory_events_envelope_gate.py` proves this gate
over HTTP. Every assertion in it passed while the same act, performed through
the MCP tool, was governed by nothing at all — `enforce_envelope` was an HTTP
adapter called from one route, and no tool called anything.

That matters more here than on the REST side. The envelope governs *agent*
autonomy, and an agent reaches this substrate over MCP; a human or a script
reaches it over REST. So the transport the gate was missing from is the one the
governed party uses.

Driven in-process through a real `FastMCP` (`mcp.call_tool`, no SSE) against the
same wired app the REST suite drives, the pattern
`test_memory_curation_mcp_tools.py` established. A mocked enforcement service
would prove the tool calls something; this proves it calls the same thing the
route does, against the same rows.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.api.mcp.context import _request_app, _request_token, _request_x_tenant_id
from contextplane.api.mcp.server import create_contextplane_mcp_server
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


type _Gate = tuple[EntitlementAuthHarness, object, TenantPersona, uuid.UUID]


@pytest_asyncio.fixture
async def gate(pg_container: str) -> AsyncIterator[_Gate]:
    """An authenticated agent, a real MCP server over the harness app, and the
    tenant its writes land in."""
    slug = f"mcpenv-{secrets.token_hex(4)}"
    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["admin"], actor_id=uuid.uuid4())
        harness.configure_fetcher_for(persona)
        mcp = create_contextplane_mcp_server(
            retrieval=harness.app.state.retrieval,
            catalog=harness.app.state.catalog,
            session_factory=harness.app.state.session_factory,
            workspace_service=harness.app.state.workspace_service,
            clock=FakeClock(_NOW),
        )
        # The tenant is materialised on first authenticated request, so it has
        # to be asked for rather than assumed -- the same `/v1/whoami` step the
        # REST twin of this file takes, and for the same reason.
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert whoami.status_code == 200, whoami.text
            yield harness, mcp, persona, uuid.UUID(whoami.json()["tenant_id"])


async def _record(gate: _Gate, *, body: str = "what did we decide about retries?") -> object:
    """Drive `record_session_event` the way `handle_sse` drives every tool."""
    harness, mcp, persona, _ = gate
    harness.configure_fetcher_for(persona)
    cv_token = _request_token.set("harness.dummy.jwt")
    cv_app = _request_app.set(harness.app)
    cv_tenant = _request_x_tenant_id.set(persona.slug)
    try:
        with patch_validator_for_actor(persona):
            return await mcp.call_tool(  # type: ignore[attr-defined]
                "record_session_event",
                {"session_id": "mcp-envelope-gate", "kind": "user_message", "body": body},
            )
    finally:
        _request_x_tenant_id.reset(cv_tenant)
        _request_app.reset(cv_app)
        _request_token.reset(cv_token)


async def _advisory_records(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT verdict FROM arc_envelope_advisory_records WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).all()
    return [str(row.verdict) for row in rows]


async def _graduate(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE tenants SET envelope_enforcement_stage = 'enforcing' WHERE tenant_id = :t"),
            {"t": tenant_id},
        )


@pytest.mark.asyncio
async def test_an_advisory_agent_is_not_refused(gate: _Gate) -> None:
    """The rollout bargain is a property of the decision, not of the transport.

    Every deployment starts in `advisory` and no principal has an envelope on
    day one, so if this were wrong the gate would refuse every agent write in
    every deployment the moment it shipped — and it would do so only over MCP,
    which is the harder failure to attribute.
    """
    assert await _record(gate) is not None


@pytest.mark.asyncio
async def test_an_enforcing_agent_is_refused_over_mcp(gate: _Gate, factory: async_sessionmaker[AsyncSession]) -> None:
    """The whole of E7-T3a in one assertion.

    Before this, graduating a tenant to `enforcing` refused it on REST and left
    it permitted here — so an operator who graduated a tenant would see the
    refusals they expected on the surface they were testing with, and none at
    all on the surface their agents use.
    """
    _, _, _, tenant_id = gate
    await _graduate(factory, tenant_id)

    with pytest.raises(ToolError) as refused:
        await _record(gate)

    # The verdict, because the remedy differs: nobody has granted this principal
    # an envelope, which is an operator's job rather than the agent's.
    assert "envelope_absent" in str(refused.value)


@pytest.mark.asyncio
async def test_the_refusal_says_the_same_thing_on_both_transports(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """One vocabulary, so a client that meets both cannot be told two things
    about one decision. The message names the verdict and not the envelope: a
    caller that learned *why* could map the matrix governing it, one probe at a
    time."""
    from contextplane.arc import REFUSAL_MESSAGE

    _, _, _, tenant_id = gate
    await _graduate(factory, tenant_id)

    with pytest.raises(ToolError) as refused:
        await _record(gate)

    assert REFUSAL_MESSAGE in str(refused.value)


@pytest.mark.asyncio
async def test_an_advisory_agent_write_is_recorded(gate: _Gate, factory: async_sessionmaker[AsyncSession]) -> None:
    """And it is recorded, which is the half that makes advisory worth running.

    The record is the only thing that can later move a tenant to enforcing, so
    an advisory stage that refused nothing *and* recorded nothing would be
    indistinguishable from the gate not being wired — which is precisely the
    state this transport was in.
    """
    _, _, _, tenant_id = gate
    await _record(gate)

    assert await _advisory_records(factory, tenant_id), (
        "the MCP transport consulted no envelope, so this tenant's agents can never "
        "produce the population a graduation scan reads"
    )


@pytest.mark.asyncio
async def test_the_envelope_gate_runs_before_the_pii_scan(
    gate: _Gate, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Same ordering as the REST twin, for the same reason.

    The envelope decides whether this principal may perform the act at all;
    admission decides whether this *content* may be stored. Scanning first would
    mean a caller outside its envelope still had its body read, and a refusal
    would write an admission record about a write that was never permitted to be
    attempted.
    """
    _, _, _, tenant_id = gate
    await _graduate(factory, tenant_id)

    with pytest.raises(ToolError) as refused:
        await _record(gate, body="my national insurance number is QQ123456C")

    assert "envelope_absent" in str(refused.value)
    async with factory() as session:
        scanned = (
            await session.execute(
                text("SELECT count(*) FROM pii_detection_log WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()
    assert scanned == 0, "the body was scanned for a write the envelope had already refused"
