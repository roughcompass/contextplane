"""whoami + single-entity catalog lookups.

``get_capability`` and ``lookup_by_external_id`` both resolve to one
capability record via ``CatalogService``; ``whoami`` sits alongside them
because it is the tool a session opens with, before any catalog access.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api.mcp import context
from registry.exceptions import CatalogError
from registry.service.catalog.core import CatalogService
from registry.service.catalog.includes import IncludeService
from registry.types import Clock

# ---------------------------------------------------------------------------
# Tool: whoami
# ---------------------------------------------------------------------------


async def whoami(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Return the actor + tenant + roles the current credential resolves to.

    Use this as the first call in a session to discover which tenant
    the bearer token is scoped to and what roles the caller has —
    before attempting writes that may 403.

    Returns:
        JSON object: {actor_id, actor_display_name, actor_email,
        tenant_id, tenant_slug, tenant_display_name, roles[]}.
    """
    from registry.service.catalog.identity import resolve_whoami

    ctx = await context._resolve_tenant(session_factory, clock)
    payload = await resolve_whoami(session_factory, ctx)
    return json.dumps(
        {
            "actor_id": str(payload.actor_id),
            "actor_display_name": payload.actor_display_name,
            "actor_email": payload.actor_email,
            "tenant_id": str(payload.tenant_id),
            "tenant_slug": payload.tenant_slug,
            "tenant_display_name": payload.tenant_display_name,
            "roles": payload.roles,
        }
    )


# ---------------------------------------------------------------------------
# Tool: get_capability
# ---------------------------------------------------------------------------


async def get_capability(
    entity_id: str,
    as_of: str | None = None,
    include: str | None = None,
    *,
    catalog: CatalogService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    includes: IncludeService | None,
) -> str:
    """Retrieve a single capability record by UUID or slug-form name.

    Args:
        entity_id: UUID of the capability OR its slug-form name
            (e.g. 'salt-design-system'). Slug lookup is
            case-insensitive against the stored `name` column.
        as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).
        include: Comma-separated sub-resources to expand inline. Accepted
            values: ``components``, ``depends_on``, ``external_ids``,
            ``interface``. Each expansion is capped at 200 items —
            ``truncated: true`` + a ``next`` URL signal overflow.
            Unknown values are silently ignored.

    Returns:
        JSON object with entity metadata, attributes, facts, and edges.
        When ``include`` is provided, the response also contains the
        requested sub-resource objects (``components``, ``depends_on``,
        ``external_ids``, ``interface``).
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    temporal_filter = context._parse_as_of(as_of)
    as_of_dt = temporal_filter.as_of
    try:
        resolved = await catalog.resolve_entity_handle(ctx, entity_id, as_of=as_of_dt)
        record = await catalog.get_full_capability(ctx, resolved.entity_id, as_of=as_of_dt)
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc

    result = context._serialize(record)

    # Expand bounded sub-resources when ``include`` is requested and the
    # IncludeService is wired in.  Unknown values are silently ignored so
    # callers can pass a superset without getting a 422.
    if include and includes is not None:
        requested = {v.strip() for v in include.split(",") if v.strip()}
        if "components" in requested:
            exp = await includes.expand_components(ctx, resolved.entity_id, handle_for_next=entity_id)
            result["components"] = context._serialize(exp.model_dump(mode="json"))
        if "depends_on" in requested:
            exp = await includes.expand_depends_on(ctx, resolved.entity_id, handle_for_next=entity_id)
            result["depends_on"] = context._serialize(exp.model_dump(mode="json"))
        if "external_ids" in requested:
            exp = await includes.expand_external_ids(ctx, resolved.entity_id)  # type: ignore[assignment]
            result["external_ids"] = context._serialize(exp.model_dump(mode="json"))
        if "interface" in requested:
            exp = await includes.expand_interface(ctx, resolved.entity_id, as_of=as_of_dt)  # type: ignore[assignment]
            result["interface"] = context._serialize(exp.model_dump(mode="json"))

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Tool: lookup_by_external_id
# ---------------------------------------------------------------------------


async def lookup_by_external_id(
    external_system: str,
    external_id: str,
    *,
    catalog: CatalogService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Resolve a capability by its external-system mapping.

    Use this when you know a capability's identifier in an upstream
    registry (npm package name, GitHub repo slug, internal ID, …)
    but not its UUID or catalog name. For example, a copilot looking
    at a frontend dev's package.json can call
    lookup_by_external_id("npm", "@salt-ds/core") to find the Salt
    Design System entry in the catalog without first searching.

    Args:
        external_system: The external-system slug as registered
            in /v1/admin/external-systems (e.g. "npm", "github").
        external_id: The identifier inside that system
            (e.g. "@salt-ds/core", "jpmorganchase/salt-ds").

    Returns:
        JSON object with the full capability record (same shape as
        get_capability) or a "not found" object if no mapping exists.
    """
    from sqlalchemy import text

    ctx = await context._resolve_tenant(session_factory, clock)
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT entity_id FROM entity_external_ids "
                    "WHERE tenant_id = :tid "
                    "AND external_system_slug = :system "
                    "AND external_id = :eid "
                    "LIMIT 1"
                ),
                {"tid": ctx.tenant_id, "system": external_system, "eid": external_id},
            )
        ).first()
    if row is None:
        return json.dumps(
            {
                "found": False,
                "external_system": external_system,
                "external_id": external_id,
            }
        )
    try:
        record = await catalog.get_full_capability(ctx, row[0])
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc
    return json.dumps(context._serialize(record))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    mcp_server: FastMCP,
    *,
    catalog: CatalogService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    includes: IncludeService | None = None,
) -> None:
    """Decorate this module's tools onto ``mcp_server``, bound to the given
    services."""
    mcp_server.tool()(context._bind_tool(whoami, session_factory=session_factory, clock=clock))
    mcp_server.tool()(
        context._bind_tool(
            get_capability,
            catalog=catalog,
            session_factory=session_factory,
            clock=clock,
            includes=includes,
        )
    )
    mcp_server.tool()(
        context._bind_tool(
            lookup_by_external_id,
            catalog=catalog,
            session_factory=session_factory,
            clock=clock,
        )
    )


__all__: list[str] = ["whoami", "get_capability", "lookup_by_external_id", "register"]
