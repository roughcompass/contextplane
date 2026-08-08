"""Workspace CRUD + entry CRUD + search — thin adapters over WorkspaceService.

All six tools register unconditionally; ``workspace_service`` is required at
``create_contextplane_mcp_server`` construction time so missing wiring raises
immediately rather than silently skipping registration.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.service.workspace import WorkspaceService
from contextplane.service.workspace.core import WorkspaceNotFound, WorkspaceOperationDenied
from contextplane.service.workspace.entries import WorkspacePiiBlocked
from contextplane.types import Clock
from contextplane.usage.results import set_mcp_result_count

# Every domain exception a WorkspaceService call reachable from this module
# can raise. Tools catch this tuple rather than a bare `except Exception` so
# a genuinely unexpected error still propagates instead of being silently
# reworded into a ToolError.
_WS_EXCEPTIONS = (
    WorkspaceNotFound,
    WorkspaceOperationDenied,
    NotFoundError,
    ValidationError,
    PermissionError,
)


def _ws_exc_to_tool_error(exc: Exception, workspace_id: str | None = None) -> ToolError:
    """Translate a WorkspaceService domain exception to a ToolError.

    Translation rules per the MCP tool contract:
    - WorkspacePiiBlocked → "Entry rejected: PII detected in body [<cats>]"
      (checked first: it subclasses ValidationError).
    - WorkspaceOperationDenied / PermissionError with workspace_id context →
      workspace-specific not-authorized message.
    - WorkspaceOperationDenied / PermissionError without context → generic
      not-authorized message.
    - WorkspaceNotFound / NotFoundError with workspace_id → "Workspace <id> not found."
    - WorkspaceNotFound / NotFoundError without context → str(exc).
    - ValidationError (any other) → str(exc) — pass through the service's own
      message (regulated-tenant block, invalid kind, empty body, etc.).
    - anything else in _WS_EXCEPTIONS → str(exc).
    """
    if isinstance(exc, WorkspacePiiBlocked):
        cats_str = ", ".join(exc.categories)
        return ToolError(f"Entry rejected: PII detected in body [{cats_str}]")
    if isinstance(exc, WorkspaceOperationDenied | PermissionError):
        if workspace_id:
            return ToolError(f"Not authorized to write to workspace {workspace_id}")
        return ToolError("Not authorized")
    if isinstance(exc, WorkspaceNotFound | NotFoundError):
        if workspace_id:
            return ToolError(f"Workspace {workspace_id} not found.")
        return ToolError(str(exc))
    return ToolError(str(exc))


# ---------------------------------------------------------------------------
# Tool: create_workspace
# ---------------------------------------------------------------------------


async def create_workspace(
    name: str,
    owner_kind: str,
    description: str | None = None,
    *,
    workspace_service: WorkspaceService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Create a new workspace for the calling actor.

    Args:
        name: Workspace name (required).
        owner_kind: Ownership model — ``'actor'`` for a personal workspace
            owned by the calling actor, or ``'tenant'`` for a team workspace
            owned by the tenant.
        description: Optional human-readable description.

    Returns:
        JSON object with the created workspace fields (workspace_id,
        name, owner_kind, tenant_id, created_at, …).
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        ref = await workspace_service.create_workspace(
            ctx,
            name=name,
            owner_kind=owner_kind,
            description=description,
        )
    except _WS_EXCEPTIONS as exc:
        raise _ws_exc_to_tool_error(exc) from exc
    return json.dumps(context._serialize(ref))


# ---------------------------------------------------------------------------
# Tool: list_workspaces
# ---------------------------------------------------------------------------


async def list_workspaces(
    include_archived: bool = False,
    *,
    workspace_service: WorkspaceService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """List workspaces visible to the calling actor.

    Returns workspaces that the caller can access: actor-owned workspaces,
    tenant-owned workspaces visible to the caller's role, or any workspace
    the caller's tenant role grants access to.

    Args:
        include_archived: When ``True``, includes archived workspaces
            (archived_at IS NOT NULL). Default ``False``.

    Returns:
        JSON array of workspace objects (WorkspaceRef shape).
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        refs, _next_cursor = await workspace_service.list_workspaces(
            ctx,
            include_archived=include_archived,
        )
    except _WS_EXCEPTIONS as exc:
        raise _ws_exc_to_tool_error(exc) from exc
    set_mcp_result_count(len(refs))
    return json.dumps(context._serialize(refs))


# ---------------------------------------------------------------------------
# Tool: get_workspace
# ---------------------------------------------------------------------------


async def get_workspace(
    workspace_id: str,
    *,
    workspace_service: WorkspaceService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Get a specific workspace by ID.

    Args:
        workspace_id: UUID of the workspace to retrieve.

    Returns:
        JSON object with the workspace fields (WorkspaceRef shape).
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        ws_uuid = uuid.UUID(workspace_id)
    except ValueError as exc:
        raise ToolError(f"workspace_id must be a valid UUID: {exc}") from exc
    try:
        ref = await workspace_service.get_workspace(ctx, ws_uuid)
    except WorkspaceOperationDenied as exc:
        raise ToolError(f"Workspace {workspace_id} is not visible to the calling actor.") from exc
    except WorkspaceNotFound as exc:
        raise ToolError(f"Workspace {workspace_id} not found.") from exc
    return json.dumps(context._serialize(ref))


# ---------------------------------------------------------------------------
# Tool: add_workspace_entry
# ---------------------------------------------------------------------------


async def add_workspace_entry(
    workspace_id: str,
    kind: str,
    body_md: str,
    reference_ids: list[str] | None = None,
    references_jsonb: dict[str, Any] | None = None,
    expires_at: str | None = None,
    *,
    workspace_service: WorkspaceService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Add an entry to a workspace.

    The PII scanner runs on body_md (and references_jsonb when provided)
    before storage. A block-level hit raises a ToolError naming the
    detected categories.

    Args:
        workspace_id: UUID of the target workspace.
        kind: Entry kind — one of: note, decision, open_question,
            saved_query, saved_view.
        body_md: Entry body in Markdown (required, non-empty).
        reference_ids: Optional list of UUID strings referencing catalog
            entities.
        references_jsonb: Optional structured reference metadata (JSON
            object).
        expires_at: Optional ISO-8601 UTC expiry datetime. After this
            timestamp the entry is soft-invalidated by the expiry worker.

    Returns:
        JSON object with the created entry fields (WorkspaceEntryRef
        shape). Includes ``warnings`` key when the PII scanner returned
        a warn-level hit.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        ws_uuid = uuid.UUID(workspace_id)
    except ValueError as exc:
        raise ToolError(f"workspace_id must be a valid UUID: {exc}") from exc

    ref_uuids: list[uuid.UUID] = []
    if reference_ids is not None:
        for rid in reference_ids:
            try:
                ref_uuids.append(uuid.UUID(rid))
            except ValueError as exc:
                raise ToolError(f"reference_ids contains an invalid UUID: {rid!r}: {exc}") from exc

    expires_at_dt = None
    if expires_at is not None:
        try:
            expires_at_dt = datetime.fromisoformat(expires_at)
        except (ValueError, TypeError) as exc:
            raise ToolError(f"expires_at must be a timezone-aware ISO-8601 datetime: {exc}") from exc

    try:
        ref = await workspace_service.create_entry(
            ctx,
            workspace_id=ws_uuid,
            kind=kind,
            body_md=body_md,
            reference_ids=ref_uuids,
            references_jsonb=references_jsonb,
            expires_at=expires_at_dt,
        )
    except _WS_EXCEPTIONS as exc:
        raise _ws_exc_to_tool_error(exc, workspace_id=workspace_id) from exc
    return json.dumps(context._serialize(ref))


# ---------------------------------------------------------------------------
# Tool: update_workspace_entry
# ---------------------------------------------------------------------------


async def update_workspace_entry(
    entry_id: str,
    body_md: str | None = None,
    reference_ids: list[str] | None = None,
    references_jsonb: dict[str, Any] | None = None,
    *,
    workspace_service: WorkspaceService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Update an existing workspace entry.

    Only provided fields are updated; omitted fields retain their current
    values. The PII scanner runs on body_md and references_jsonb when
    provided; a block-level hit raises a ToolError.

    Args:
        entry_id: UUID of the entry to update.
        body_md: New entry body in Markdown (optional).
        reference_ids: Replacement list of UUID strings referencing catalog
            entities (optional).
        references_jsonb: Replacement structured reference metadata
            (optional).

    Returns:
        JSON object with the updated entry fields (WorkspaceEntryRef
        shape). Includes ``warnings`` key when the PII scanner returned
        a warn-level hit.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    try:
        entry_uuid = uuid.UUID(entry_id)
    except ValueError as exc:
        raise ToolError(f"entry_id must be a valid UUID: {exc}") from exc

    ref_uuids: list[uuid.UUID] | None = None
    if reference_ids is not None:
        ref_uuids = []
        for rid in reference_ids:
            try:
                ref_uuids.append(uuid.UUID(rid))
            except ValueError as exc:
                raise ToolError(f"reference_ids contains an invalid UUID: {rid!r}: {exc}") from exc

    try:
        ref = await workspace_service.update_entry(
            ctx,
            entry_id=entry_uuid,
            body_md=body_md,
            reference_ids=ref_uuids,
            references_jsonb=references_jsonb,
        )
    except _WS_EXCEPTIONS as exc:
        raise _ws_exc_to_tool_error(exc) from exc
    return json.dumps(context._serialize(ref))


# ---------------------------------------------------------------------------
# Tool: search_workspace_entries
# ---------------------------------------------------------------------------


async def search_workspace_entries(
    q: str | None = None,
    kind: str | None = None,
    reference_ids: list[str] | None = None,
    *,
    workspace_service: WorkspaceService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Search across workspace entries visible to the calling actor.

    Results are scoped to workspaces the actor owns, their tenant owns,
    or that have been explicitly shared with the actor. No cross-actor
    content is ever returned.

    Args:
        q: Optional full-text search query. When ``None``, all visible
            entries are returned (paginated).
        kind: Optional entry kind filter — one of: note, decision,
            open_question, saved_query, saved_view.
        reference_ids: Optional list of UUID strings; restricts results
            to entries that reference ALL listed entities.

    Returns:
        JSON object ``{"items": [...], "next_cursor": str | null,
        "total_count": int | null}``. Each item matches the
        WorkspaceEntryRef shape.
    """
    ctx = await context._resolve_tenant(session_factory, clock)

    ref_uuids: list[uuid.UUID] | None = None
    if reference_ids is not None:
        ref_uuids = []
        for rid in reference_ids:
            try:
                ref_uuids.append(uuid.UUID(rid))
            except ValueError as exc:
                raise ToolError(f"reference_ids contains an invalid UUID: {rid!r}: {exc}") from exc

    try:
        result = await workspace_service.search_workspaces(
            ctx,
            q=q,
            kind=kind,
            reference_ids=ref_uuids,
        )
    except _WS_EXCEPTIONS as exc:
        raise _ws_exc_to_tool_error(exc) from exc
    set_mcp_result_count(len(result.items))
    return json.dumps(
        {
            "items": context._serialize(result.items),
            "next_cursor": result.next_cursor,
            "total_count": result.total_count,
        }
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    mcp_server: FastMCP,
    *,
    workspace_service: WorkspaceService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tools onto ``mcp_server``, bound to the given
    services."""
    deps: dict[str, Any] = {
        "workspace_service": workspace_service,
        "session_factory": session_factory,
        "clock": clock,
    }
    mcp_server.tool()(context._bind_tool(create_workspace, **deps))
    mcp_server.tool()(context._bind_tool(list_workspaces, **deps))
    mcp_server.tool()(context._bind_tool(get_workspace, **deps))
    mcp_server.tool()(context._bind_tool(add_workspace_entry, **deps))
    mcp_server.tool()(context._bind_tool(update_workspace_entry, **deps))
    mcp_server.tool()(context._bind_tool(search_workspace_entries, **deps))


__all__: list[str] = [
    "create_workspace",
    "list_workspaces",
    "get_workspace",
    "add_workspace_entry",
    "update_workspace_entry",
    "search_workspace_entries",
    "register",
]
