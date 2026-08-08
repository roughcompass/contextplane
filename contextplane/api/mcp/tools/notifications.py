"""list_notifications — payload-minimal, output mirrors REST /v1/notifications.

Registration is conditional: a deployment without a wired
``NotificationService`` gets no ``list_notifications`` tool rather than a
tool that always errors.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.exceptions import CatalogError
from contextplane.service.notifications.core import NotificationService, event_to_dict
from contextplane.types import Clock
from contextplane.usage.results import set_mcp_result_count

# ---------------------------------------------------------------------------
# Tool: list_notifications
# ---------------------------------------------------------------------------


async def list_notifications(
    cursor: str | None = None,
    status: str = "unread",
    page_size: int = 50,
    *,
    notifications: NotificationService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """List capability-event notifications for the caller's tenant.

    Args:
        cursor: The ``next_cursor`` from a previous page — an ISO-8601 ``ts``
            value. Returns rows strictly older than it. ``None`` returns the
            first page (newest first).
        status: ``unread`` (default) | ``read`` | ``all``.
        page_size: 1–500 (default 50).

    Returns:
        JSON object ``{"items": [...], "next_cursor": str | None}``.
        Item shape matches REST ``/v1/notifications``
        (CapabilityRegistryEvent — no body text or freeform content).
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    if not 1 <= page_size <= 500:
        raise ToolError("page_size must be between 1 and 500")
    try:
        events, next_cursor = await notifications.list_notifications(
            ctx=ctx,
            status=status,
            cursor=cursor,
            page_size=page_size,
        )
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc
    set_mcp_result_count(len(events))
    return json.dumps(
        {
            "items": [event_to_dict(e) for e in events],
            "next_cursor": next_cursor,
        }
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    mcp_server: FastMCP,
    *,
    notifications: NotificationService | None,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate ``list_notifications`` onto ``mcp_server`` when a
    NotificationService is wired. A no-op otherwise."""
    if notifications is None:
        return
    mcp_server.tool()(
        context._bind_tool(
            list_notifications,
            notifications=notifications,
            session_factory=session_factory,
            clock=clock,
        )
    )


__all__: list[str] = ["list_notifications", "register"]
