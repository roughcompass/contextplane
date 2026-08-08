"""Pinned ToolError text/shape for every exception the MCP layer translates.

The MCP transport does not have HTTP status codes — it maps by exception
*type* through ordered except chains and isinstance ladders, mainly
``contextplane.api.mcp.context._map_catalog_error`` (the MCP-side counterpart of
``contextplane.api.errors.map_catalog_error``) and each tool module's own
except arms. This is the regression gate for the exception-hierarchy rebase
on that surface: every row must produce the exact same ``ToolError`` text
before and after the rebase (the one deliberate exception being the
``contextplane.arc.types`` vocabulary-error rename).

Tools are plain module-level coroutines (see
``contextplane.api.mcp.context._bind_tool``'s docstring), so they are called
directly here rather than through a live FastMCP server + SSE transport —
``context._resolve_tenant`` (and, for ARC, ``_arc_preflight``/``_arc_state``)
is patched to skip real auth/service wiring.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from contextplane.api.mcp import context
from contextplane.api.mcp.tools import arc as arc_tools
from contextplane.api.mcp.tools import catalog as catalog_tools
from contextplane.api.mcp.tools import memory as memory_tools
from contextplane.api.mcp.tools import notifications as notifications_tools
from contextplane.api.mcp.tools import retrieval as retrieval_tools
from contextplane.api.mcp.tools.workspace import _ws_exc_to_tool_error
from contextplane.arc.service.preflight import PREFLIGHT_REQUIRED, PreflightError
from contextplane.exceptions import (
    CatalogError,
    ConflictError,
    LifecycleError,
    NotFoundError,
    TenantIsolationError,
    ValidationError,
    VocabularyError,
)
from contextplane.service.workspace.core import WorkspaceNotFound, WorkspaceOperationDenied
from contextplane.service.workspace.entries import WorkspacePiiBlocked
from contextplane.types import TenantContext

_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT_ID, actor_id=_ACTOR_ID, roles=["consumer"])


_RESOLVE_TENANT = "contextplane.api.mcp.context._resolve_tenant"


# ---------------------------------------------------------------------------
# Table 1 — context._map_catalog_error(): the shared MCP translator, called
# from catalog.py, notifications.py, and retrieval.py's tools after
# `except CatalogError as exc: raise context._map_catalog_error(exc)`.
# ---------------------------------------------------------------------------

_MAP_CATALOG_ERROR_TABLE = [
    pytest.param(NotFoundError("widget missing"), "not found: widget missing", id="NotFoundError"),
    # TenantIsolationError's message is deliberately NOT str(exc) — same
    # leak concern as the REST side.
    pytest.param(TenantIsolationError("cross-tenant peek"), "not found", id="TenantIsolationError"),
    pytest.param(ConflictError("dup key"), "dup key", id="ConflictError"),
    pytest.param(ValidationError("bad shape"), "bad shape", id="ValidationError"),
    pytest.param(VocabularyError("bad vocab"), "bad vocab", id="VocabularyError"),
    pytest.param(LifecycleError("bad transition"), "bad transition", id="LifecycleError"),
    pytest.param(CatalogError("generic catalog"), "generic catalog", id="CatalogError-bare"),
]


@pytest.mark.parametrize("exc, expected_message", _MAP_CATALOG_ERROR_TABLE)
def test_map_catalog_error_contract(exc, expected_message) -> None:
    tool_error = context._map_catalog_error(exc)
    assert isinstance(tool_error, ToolError)
    assert str(tool_error) == expected_message


# ---------------------------------------------------------------------------
# Table 2 — workspace.py's _ws_exc_to_tool_error: the workspace trio +
# WorkspacePiiBlocked's exact category-list message.
# ---------------------------------------------------------------------------


def test_ws_exc_to_tool_error_pii_blocked() -> None:
    exc = WorkspacePiiBlocked(field="workspace_entry.body", categories=["email", "ssn"])
    tool_error = _ws_exc_to_tool_error(exc)
    assert str(tool_error) == "Entry rejected: PII detected in body [email, ssn]"


def test_ws_exc_to_tool_error_operation_denied_with_workspace_id() -> None:
    exc = WorkspaceOperationDenied("Only the owning producer may write entries.")
    tool_error = _ws_exc_to_tool_error(exc, workspace_id="ws-1")
    assert str(tool_error) == "Not authorized to write to workspace ws-1"


def test_ws_exc_to_tool_error_operation_denied_without_workspace_id() -> None:
    tool_error = _ws_exc_to_tool_error(WorkspaceOperationDenied("denied"))
    assert str(tool_error) == "Not authorized"


def test_ws_exc_to_tool_error_permission_error_with_workspace_id() -> None:
    tool_error = _ws_exc_to_tool_error(PermissionError("denied"), workspace_id="ws-2")
    assert str(tool_error) == "Not authorized to write to workspace ws-2"


def test_ws_exc_to_tool_error_not_found_with_workspace_id() -> None:
    tool_error = _ws_exc_to_tool_error(WorkspaceNotFound("gone"), workspace_id="ws-3")
    assert str(tool_error) == "Workspace ws-3 not found."


def test_ws_exc_to_tool_error_not_found_without_workspace_id() -> None:
    tool_error = _ws_exc_to_tool_error(NotFoundError("entry 9 gone"))
    assert str(tool_error) == "entry 9 gone"


def test_ws_exc_to_tool_error_validation_error_passthrough() -> None:
    tool_error = _ws_exc_to_tool_error(ValidationError("invalid kind 'bogus'"))
    assert str(tool_error) == "invalid kind 'bogus'"


# ---------------------------------------------------------------------------
# Table 3 — memory.py tools: inline except arms, no shared helper.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_event_not_found_maps_to_fixed_message() -> None:
    service = MagicMock()
    service.get_event = AsyncMock(side_effect=NotFoundError("event row missing"))
    with (
        patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())),
        patch.object(context, "_memory_service", return_value=service),
    ):
        with pytest.raises(ToolError) as exc_info:
            await memory_tools.get_session_event(
                "session-1", str(uuid.uuid4()), session_factory=MagicMock(), clock=MagicMock()
            )
    assert str(exc_info.value) == "event not found"


@pytest.mark.asyncio
async def test_get_session_event_bad_uuid_maps_to_fixed_message() -> None:
    # _memory_service() is evaluated (as part of building the .get_event(...)
    # call) before the bad event_id argument raises, so it must be patched
    # even though this test only cares about the UUID-parse failure.
    with (
        patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())),
        patch.object(context, "_memory_service", return_value=MagicMock()),
    ):
        with pytest.raises(ToolError) as exc_info:
            await memory_tools.get_session_event(
                "session-1", "not-a-uuid", session_factory=MagicMock(), clock=MagicMock()
            )
    assert str(exc_info.value) == "event_id must be a UUID"


@pytest.mark.asyncio
async def test_delete_session_event_not_found_maps_to_fixed_message() -> None:
    service = MagicMock()
    service.delete_event = AsyncMock(side_effect=NotFoundError("event row missing"))
    with (
        patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())),
        patch.object(context, "_memory_service", return_value=service),
    ):
        with pytest.raises(ToolError) as exc_info:
            await memory_tools.delete_session_event(
                "session-1", str(uuid.uuid4()), session_factory=MagicMock(), clock=MagicMock()
            )
    assert str(exc_info.value) == "event not found"


@pytest.mark.asyncio
async def test_record_session_event_validation_error_passthrough() -> None:
    service = MagicMock()
    service.record_event = AsyncMock(side_effect=ValidationError("kind must be one of ..."))
    with (
        patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())),
        patch.object(context, "_memory_service", return_value=service),
    ):
        with pytest.raises(ToolError) as exc_info:
            await memory_tools.record_session_event(
                "session-1", "user_message", "hi", session_factory=MagicMock(), clock=MagicMock()
            )
    assert str(exc_info.value) == "kind must be one of ..."


@pytest.mark.asyncio
async def test_get_claim_bad_uuid_maps_to_fixed_message() -> None:
    with patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())):
        with pytest.raises(ToolError) as exc_info:
            await memory_tools.get_claim("not-a-uuid", session_factory=MagicMock(), clock=MagicMock())
    assert str(exc_info.value) == "claim_id must be a UUID"


@pytest.mark.asyncio
async def test_get_claim_none_maps_to_fixed_message() -> None:
    service = MagicMock()
    service.get = AsyncMock(return_value=None)
    with (
        patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())),
        patch.object(context, "_claim_serving", return_value=service),
    ):
        with pytest.raises(ToolError) as exc_info:
            await memory_tools.get_claim(str(uuid.uuid4()), session_factory=MagicMock(), clock=MagicMock())
    assert str(exc_info.value) == "no such claim"


# ---------------------------------------------------------------------------
# Table 4 — arc.py tools: ordered except chains distinct from the shared
# helper (PreflightError carries its own bounded code; ConflictError and a
# bare ValueError get different codes in the same try block).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arc_preflight_error_carries_bounded_code() -> None:
    registry = MagicMock()
    registry.require = MagicMock(side_effect=PreflightError("no completed preflight for this connection"))
    with (
        patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())),
        patch.object(context, "_arc_state", return_value=registry),
    ):
        with pytest.raises(ToolError) as exc_info:
            await arc_tools._arc_preflight(session_factory=MagicMock(), clock=MagicMock())
    payload = json.loads(str(exc_info.value))
    assert payload == {
        "code": PREFLIGHT_REQUIRED,
        "message": "no completed preflight for this connection",
        "details": {},
    }


@pytest.mark.asyncio
async def test_arc_issue_context_challenge_conflict_error() -> None:
    challenges = MagicMock()
    challenges.issue_challenge = AsyncMock(side_effect=ConflictError("idempotency key reused with a different body"))
    with (
        patch.object(arc_tools, "_arc_preflight", new=AsyncMock(return_value=object())),
        patch.object(context, "_arc_state", return_value=challenges),
    ):
        with pytest.raises(ToolError) as exc_info:
            await arc_tools.arc_issue_context_challenge(
                "session-1", "digest", "idem-1", session_factory=MagicMock(), clock=MagicMock()
            )
    payload = json.loads(str(exc_info.value))
    assert payload == {
        "code": "idempotency_conflict",
        "message": "idempotency key reused with a different body",
        "details": {},
    }


@pytest.mark.asyncio
async def test_arc_issue_context_challenge_value_error_maps_to_forbidden() -> None:
    challenges = MagicMock()
    challenges.issue_challenge = AsyncMock(side_effect=ValueError("session does not belong to this host"))
    with (
        patch.object(arc_tools, "_arc_preflight", new=AsyncMock(return_value=object())),
        patch.object(context, "_arc_state", return_value=challenges),
    ):
        with pytest.raises(ToolError) as exc_info:
            await arc_tools.arc_issue_context_challenge(
                "session-1", "digest", "idem-1", session_factory=MagicMock(), clock=MagicMock()
            )
    payload = json.loads(str(exc_info.value))
    assert payload == {
        "code": "forbidden",
        "message": "session does not belong to this host",
        "details": {},
    }


@pytest.mark.asyncio
async def test_arc_get_receipt_value_error_maps_to_validation_error() -> None:
    # context._arc_state(...) resolves the reader before the try block runs,
    # so it must be patched even though this test only cares about the
    # bad-UUID ValueError raised while building the .get_receipt(...) call.
    with (
        patch.object(arc_tools, "_arc_preflight", new=AsyncMock(return_value=object())),
        patch.object(context, "_arc_state", return_value=MagicMock()),
    ):
        with pytest.raises(ToolError) as exc_info:
            await arc_tools.arc_get_context_resolution_receipt(
                "not-a-uuid", session_factory=MagicMock(), clock=MagicMock()
            )
    payload = json.loads(str(exc_info.value))
    assert payload["code"] == "validation_error"


@pytest.mark.asyncio
async def test_arc_get_receipt_unexpected_exception_maps_to_not_found() -> None:
    """Anything other than ValueError from the receipt reader — including a
    genuinely unexpected bug — is folded into a bounded not-found response so
    a receipt in another tenant reports as absent rather than leaking detail."""
    reader = MagicMock()
    reader.get_receipt = AsyncMock(side_effect=RuntimeError("db exploded"))
    with (
        patch.object(arc_tools, "_arc_preflight", new=AsyncMock(return_value=object())),
        patch.object(context, "_arc_state", return_value=reader),
    ):
        with pytest.raises(ToolError) as exc_info:
            await arc_tools.arc_get_context_resolution_receipt(
                str(uuid.uuid4()), session_factory=MagicMock(), clock=MagicMock()
            )
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "not_found", "message": "receipt not found", "details": {}}


# ---------------------------------------------------------------------------
# Smoke tests — one per remaining tool family, confirming the
# `except CatalogError: raise context._map_catalog_error(exc)` wiring is
# actually reached from each module (not just testable in isolation).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_get_capability_wires_map_catalog_error() -> None:
    catalog = MagicMock()
    catalog.resolve_entity_handle = AsyncMock(side_effect=NotFoundError("capability missing"))
    with patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())):
        with pytest.raises(ToolError) as exc_info:
            await catalog_tools.get_capability(
                str(uuid.uuid4()),
                catalog=catalog,
                session_factory=MagicMock(),
                clock=MagicMock(),
                includes=None,
            )
    assert str(exc_info.value) == "not found: capability missing"


@pytest.mark.asyncio
async def test_notifications_list_wires_map_catalog_error() -> None:
    notifications = MagicMock()
    notifications.list_notifications = AsyncMock(side_effect=ValidationError("bad status filter"))
    with patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())):
        with pytest.raises(ToolError) as exc_info:
            await notifications_tools.list_notifications(
                notifications=notifications, session_factory=MagicMock(), clock=MagicMock()
            )
    assert str(exc_info.value) == "bad status filter"


@pytest.mark.asyncio
async def test_retrieval_list_capabilities_wires_map_catalog_error() -> None:
    retrieval = MagicMock()
    retrieval.list_capabilities = AsyncMock(side_effect=TenantIsolationError("cross-tenant"))
    with patch(_RESOLVE_TENANT, new=AsyncMock(return_value=_ctx())):
        with pytest.raises(ToolError) as exc_info:
            await retrieval_tools.list_capabilities(retrieval=retrieval, session_factory=MagicMock(), clock=MagicMock())
    assert str(exc_info.value) == "not found"
