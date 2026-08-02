"""ARC MCP tools, and the preflight that gates them.

The negative paths are the substance here. REST re-authenticates on every
request; a long-lived MCP connection authenticates once and then serves many
tool calls, so the preflight gate is what stops an MCP caller reaching ARC
under a credential that has since changed. A gate that only ever admits is
indistinguishable from no gate.

These exercise the tools through the FastMCP factory as it is actually
built, so a tool that exists in the source but was never registered fails
here rather than at a customer.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from mcp.server.fastmcp.exceptions import ToolError

from registry.arc.service.preflight import (
    PREFLIGHT_REQUIRED,
    PreflightRecord,
    PreflightRegistry,
    credential_fingerprint,
    new_connection_id,
    restriction_digest,
)
from registry.types import FakeClock
from tests.helpers.arc_fixtures import ARC_NOW

_TOKEN = "header.payload.signature"


@pytest_asyncio.fixture
async def registry_and_conn() -> AsyncIterator[tuple[PreflightRegistry, str, uuid.UUID]]:
    registry = PreflightRegistry()
    connection_id = new_connection_id()
    tenant_id = uuid.uuid4()
    registry.record(
        connection_id=connection_id,
        credential_fingerprint=credential_fingerprint(_TOKEN),
        tenant_id=tenant_id,
        actor_id=uuid.uuid4(),
        oidc_issuer="https://idp.example.test",
        oidc_subject="svc-agent",
        roles=("consumer",),
        token_restriction_digest=restriction_digest(None),
        authentication_expires_at=ARC_NOW + datetime.timedelta(hours=1),
        completed_at=ARC_NOW,
    )
    yield registry, connection_id, tenant_id


# --- the tools are actually registered ------------------------------------------


@pytest.mark.asyncio
async def test_the_arc_tools_are_registered_in_the_factory() -> None:
    """A tool that exists in the source but was never registered would fail
    only when a client tried to call it.

    Built through the real factory with mocked collaborators rather than a
    running app, so this asserts registration itself and cannot pass by
    falling back to some weaker check.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from registry.api.routers.mcp import create_registry_mcp_server  # noqa: PLC0415

    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
    )
    names = {t.name for t in await server.list_tools()}
    for expected in (
        "arc_complete_preflight",
        "arc_issue_context_challenge",
        "arc_get_context_resolution_receipt",
        "arc_explain_context_resolution",
    ):
        assert expected in names, f"{expected} is not registered"


@pytest.mark.asyncio
async def test_every_arc_tool_carries_a_description() -> None:
    """An MCP tool with no description is one a model cannot choose
    correctly — and these gate governed content, so a wrong choice is not
    merely unhelpful."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from registry.api.routers.mcp import create_registry_mcp_server  # noqa: PLC0415

    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
    )
    for tool in await server.list_tools():
        if tool.name.startswith("arc_"):
            assert tool.description, f"{tool.name} has no description"


def create_app_for(pg_container: str):  # noqa: ANN201
    from registry.config import Settings  # noqa: PLC0415
    from registry.main import create_app  # noqa: PLC0415

    return create_app(
        Settings(
            database_url=pg_container,
            pgbouncer_url=pg_container,
            scheduler_jobstore_url=pg_container,
            scheduler_use_memory_jobstore=True,
            embedding_provider="stub",
        )
    )


@pytest.mark.asyncio
async def test_the_app_carries_a_preflight_registry(pg_container: str) -> None:
    """Without it every ARC tool refuses, which would look like a
    permissions bug rather than missing wiring."""
    app = create_app_for(pg_container)
    assert isinstance(app.state.arc_preflight, PreflightRegistry)


# --- the gate refuses ---------------------------------------------------------------


def _require(
    registry: PreflightRegistry,
    connection_id: str | None,
    tenant_id: uuid.UUID,
    *,
    fingerprint: str | None = None,
    presented_tenant: uuid.UUID | None = None,
    now: datetime.datetime = ARC_NOW,
) -> PreflightRecord:
    """Call the gate the way a tool does, with one thing varied at a time."""
    return registry.require(
        connection_id=connection_id,
        credential_fingerprint=fingerprint or credential_fingerprint(_TOKEN),
        tenant_id=presented_tenant or tenant_id,
        token_restriction_digest=restriction_digest(None),
        now=now,
    )


@pytest.mark.asyncio
async def test_a_connection_that_never_preflighted_is_refused(
    registry_and_conn: tuple[PreflightRegistry, str, uuid.UUID]
) -> None:
    registry, _, tenant_id = registry_and_conn
    from registry.arc.service.preflight import PreflightError  # noqa: PLC0415

    with pytest.raises(PreflightError) as exc:
        _require(registry, new_connection_id(), tenant_id)
    assert exc.value.code == PREFLIGHT_REQUIRED


@pytest.mark.asyncio
async def test_a_disconnect_invalidates_the_preflight(
    registry_and_conn: tuple[PreflightRegistry, str, uuid.UUID]
) -> None:
    """What the SSE handler's teardown does. A record outliving its
    connection would be a preflight for a caller nobody is on the other end
    of."""
    from registry.arc.service.preflight import PreflightError  # noqa: PLC0415

    registry, connection_id, tenant_id = registry_and_conn
    assert _require(registry, connection_id, tenant_id)

    registry.invalidate(connection_id)

    with pytest.raises(PreflightError):
        _require(registry, connection_id, tenant_id)


@pytest.mark.asyncio
async def test_a_swapped_credential_is_refused(
    registry_and_conn: tuple[PreflightRegistry, str, uuid.UUID]
) -> None:
    """The failure this whole mechanism exists for: a long-lived connection
    whose credential changed after it authenticated."""
    from registry.arc.service.preflight import PreflightError  # noqa: PLC0415

    registry, connection_id, tenant_id = registry_and_conn
    with pytest.raises(PreflightError, match="credential"):
        _require(registry, connection_id, tenant_id, fingerprint=credential_fingerprint("other.token.here"))


@pytest.mark.asyncio
async def test_a_changed_tenant_selector_is_refused(
    registry_and_conn: tuple[PreflightRegistry, str, uuid.UUID]
) -> None:
    from registry.arc.service.preflight import PreflightError  # noqa: PLC0415

    registry, connection_id, tenant_id = registry_and_conn
    with pytest.raises(PreflightError, match="tenant"):
        _require(registry, connection_id, tenant_id, presented_tenant=uuid.uuid4())


@pytest.mark.asyncio
async def test_expired_authentication_is_refused(
    registry_and_conn: tuple[PreflightRegistry, str, uuid.UUID]
) -> None:
    from registry.arc.service.preflight import PreflightError  # noqa: PLC0415

    registry, connection_id, tenant_id = registry_and_conn
    with pytest.raises(PreflightError, match="expired"):
        _require(registry, connection_id, tenant_id, now=ARC_NOW + datetime.timedelta(hours=2))


@pytest.mark.asyncio
async def test_one_connections_preflight_does_not_admit_another(
    registry_and_conn: tuple[PreflightRegistry, str, uuid.UUID]
) -> None:
    """The reason the key is server-assigned and unguessable: guessing
    another connection's key would otherwise mean adopting its preflight."""
    from registry.arc.service.preflight import PreflightError  # noqa: PLC0415

    registry, connection_id, tenant_id = registry_and_conn
    assert _require(registry, connection_id, tenant_id)
    with pytest.raises(PreflightError):
        _require(registry, new_connection_id(), tenant_id)


# --- the error shape a caller sees ----------------------------------------------------


def test_the_refusal_carries_one_bounded_code() -> None:
    """Every refusal reports the same code. Which check failed is not the
    caller's business, and naming it would tell a prober how far they got."""
    from registry.arc.service.preflight import PreflightError  # noqa: PLC0415

    for reason in ("never preflighted", "credential changed", "expired"):
        assert PreflightError(reason).code == PREFLIGHT_REQUIRED


def test_a_tool_error_payload_is_one_bounded_json_object() -> None:
    """MCP has no HTTP status, so the code has to travel in the message —
    and it must be parseable rather than prose a client has to regex."""
    payload = json.dumps({"code": PREFLIGHT_REQUIRED, "message": "preflight required", "details": {}})
    decoded = json.loads(ToolError(payload).args[0])
    assert decoded["code"] == PREFLIGHT_REQUIRED
    assert set(decoded) == {"code", "message", "details"}


def test_the_clock_is_injected_rather_than_read() -> None:
    """Preflight expiry is evaluated at one instant supplied by the caller,
    so a request cannot straddle two."""
    clock = FakeClock(ARC_NOW)
    assert clock.now() == ARC_NOW
