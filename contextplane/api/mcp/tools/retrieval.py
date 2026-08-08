"""Search, traversal, and listing over the catalog.

Thin adapters over ``RetrievalService`` (and, for the traversal tools,
``CatalogService.resolve_entity_handle`` to accept a slug as well as a
UUID) — no business logic duplicated here.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.api.routers._common import search_result_to_item
from contextplane.exceptions import CatalogError
from contextplane.service.catalog.core import CatalogService
from contextplane.service.retrieval import RetrievalService
from contextplane.types import Clock
from contextplane.usage.results import set_mcp_result_count

# ---------------------------------------------------------------------------
# Tool: search_capabilities
# ---------------------------------------------------------------------------


async def search_capabilities(
    q: str,
    top_k: int = 10,
    as_of: str | None = None,
    entity_type: str | None = None,
    lifecycle: str | None = None,
    *,
    retrieval: RetrievalService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Hybrid semantic + lexical + graph search across capabilities.

    Args:
        q: Free-text search query (required).
        top_k: Maximum number of results to return (1–100, default 10).
        as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).
        entity_type: Filter by entity type slug (optional).
        lifecycle: Filter by lifecycle label (optional).

    Returns:
        JSON array of search results with entity metadata and scores.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    temporal_filter = context._parse_as_of(as_of)
    if not 1 <= top_k <= 100:
        raise ToolError("top_k must be between 1 and 100")
    try:
        results = await retrieval.search(
            ctx,
            q=q,
            top_k=top_k,
            temporal_filter=temporal_filter,
            entity_type=entity_type,
            lifecycle=lifecycle,
        )
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc
    set_mcp_result_count(len(results))
    # Serialised through the same helper the HTTP surface uses, rather than
    # by walking the service dataclass. Reflecting the internal shape put
    # storage column names, the owning tenant and every matched body on the
    # wire — the agent surface returning more, and in a different vocabulary,
    # than the endpoint answering the same question.
    return json.dumps(
        [search_result_to_item(r).model_dump(by_alias=True, exclude_unset=True, mode="json") for r in results]
    )


# ---------------------------------------------------------------------------
# Tool: get_dependencies
# ---------------------------------------------------------------------------


async def get_dependencies(
    entity_id: str,
    depth: int = 2,
    as_of: str | None = None,
    *,
    catalog: CatalogService,
    retrieval: RetrievalService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """k-hop dependency traversal from a capability.

    Args:
        entity_id: UUID of the root capability OR its slug-form name
            (e.g. 'salt-design-system'). Slug lookup is
            case-insensitive against the stored `name` column.
        depth: Traversal depth (1–5, default 2).
        as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).

    Returns:
        JSON object with root_entity_id, depth, as_of, and edges array.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    if not 1 <= depth <= 5:
        raise ToolError("depth must be between 1 and 5")
    temporal_filter = context._parse_as_of(as_of)
    try:
        resolved = await catalog.resolve_entity_handle(ctx, entity_id)
        edges = await retrieval.get_dependencies(
            ctx,
            entity_id=resolved.entity_id,
            depth=depth,
            temporal_filter=temporal_filter,
        )
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc
    set_mcp_result_count(len(edges))
    return json.dumps(
        {
            "root_entity_id": str(resolved.entity_id),
            "depth": depth,
            "as_of": temporal_filter.as_of.isoformat() if temporal_filter.as_of else None,
            "edges": context._serialize(edges),
        }
    )


# ---------------------------------------------------------------------------
# Tool: get_dependents
# Thin adapter over retrieval.get_reverse_traversal — no duplicated logic.
# ---------------------------------------------------------------------------


async def get_dependents(
    entity_id: str,
    depth: int = 2,
    edge_types: list[str] | None = None,
    as_of: str | None = None,
    *,
    catalog: CatalogService,
    retrieval: RetrievalService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Reverse traversal: capabilities that depend on the given entity.

    Returns all nodes that (transitively) point TO ``entity_id``, symmetric
    to ``get_dependencies`` (forward traversal).

    Args:
        entity_id: UUID of the root capability OR its slug-form name
            (e.g. 'salt-design-system'). Slug lookup is
            case-insensitive against the stored `name` column.
        depth: Max hop count (1–5, default 2). Capped at 5 by the service.
        edge_types: Edge relationship vocab values to follow. None follows
            all dependency rels (all vocab minus concept_of, operation_of,
            instance_of).
        as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).

    Returns:
        JSON object matching the REST TraversalResult shape:
        root_entity_id, depth, direction, as_of, nodes, edges,
        version_satisfied, cache_hit.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    if not 1 <= depth <= 5:
        raise ToolError("depth must be between 1 and 5")
    temporal_filter = context._parse_as_of(as_of)
    try:
        resolved = await catalog.resolve_entity_handle(ctx, entity_id)
        result = await retrieval.get_reverse_traversal(
            ctx=ctx,
            entity_id=resolved.entity_id,
            depth=depth,
            edge_types=edge_types,
            as_of=temporal_filter.as_of,
        )
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc
    set_mcp_result_count(len(result.nodes))
    return json.dumps(context._serialize(result))


# ---------------------------------------------------------------------------
# Tool: get_blast_radius
# Thin adapter over retrieval.get_blast_radius — no duplicated logic.
# ---------------------------------------------------------------------------


async def get_blast_radius(
    entity_id: str,
    direction: str = "reverse",
    edge_types: list[str] | None = None,
    depth: int = 5,
    as_of: str | None = None,
    *,
    catalog: CatalogService,
    retrieval: RetrievalService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Full transitive closure from a capability, backed by closure_cache.

    Falls back to the recursive CTE when the cache is cold or when
    ``as_of`` is older than 90 days (cache horizon).

    Args:
        entity_id: UUID of the root capability OR its slug-form name
            (e.g. 'salt-design-system'). Slug lookup is
            case-insensitive against the stored `name` column.
        direction: Traversal direction — ``'forward'`` (dependencies) or
            ``'reverse'`` (dependents). Default ``'reverse'``.
        edge_types: Edge relationship vocab values to follow. None follows
            all dependency rels.
        depth: Max hop count (1–5, default 5). Capped at 5 by the service.
        as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).
            Values older than 90 days force the CTE fallback path.

    Returns:
        JSON object matching the REST TraversalResult shape:
        root_entity_id, depth, direction, as_of, nodes, edges,
        version_satisfied, cache_hit.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    if direction not in ("forward", "reverse"):
        raise ToolError("direction must be 'forward' or 'reverse'")
    if not 1 <= depth <= 5:
        raise ToolError("depth must be between 1 and 5")
    temporal_filter = context._parse_as_of(as_of)
    try:
        resolved = await catalog.resolve_entity_handle(ctx, entity_id)
        result = await retrieval.get_blast_radius(
            ctx=ctx,
            entity_id=resolved.entity_id,
            direction=direction,
            depth=depth,
            edge_types=edge_types,
            as_of=temporal_filter.as_of,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc
    set_mcp_result_count(len(result.nodes))
    return json.dumps(context._serialize(result))


# ---------------------------------------------------------------------------
# Tool: list_capabilities
# ---------------------------------------------------------------------------


async def list_capabilities(
    lifecycle: str | None = None,
    entity_type: str | None = None,
    cursor: str | None = None,
    page_size: int = 20,
    as_of: str | None = None,
    *,
    retrieval: RetrievalService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Cursor-paginated list of capabilities visible to the caller's tenant.

    Args:
        lifecycle: Filter by lifecycle label (optional).
        entity_type: Filter by entity type slug (optional).
        cursor: Opaque cursor from a previous response's ``next_cursor``.
            Pass ``null`` (or omit) for the first page.
        page_size: Items per page (1–200, default 20).
        as_of: ISO-8601 UTC datetime for bi-temporal time-travel (optional).

    Returns:
        JSON object ``{items: [...], next_cursor: "..."}``. Pass
        ``next_cursor`` back as ``cursor`` on the next call. When
        ``next_cursor`` is ``null`` the page is the last one.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    if not 1 <= page_size <= 200:
        raise ToolError("page_size must be between 1 and 200")
    temporal_filter = context._parse_as_of(as_of)
    # RetrievalService.list_capabilities is cursor-paginated. The MCP
    # tool surfaces the cursor directly — offset/page parameters are
    # not supported here because the REST equivalent rejects them
    # with HTTP 422 (page_param_deprecated).
    decoded_cursor: dict[str, Any] = {}
    if cursor:
        try:
            decoded_cursor = json.loads(cursor)
            if not isinstance(decoded_cursor, dict):
                raise ToolError("cursor must decode to a JSON object")
        except json.JSONDecodeError as exc:
            raise ToolError(f"invalid cursor: {exc}") from exc
    try:
        entity_refs, next_cursor = await retrieval.list_capabilities(
            ctx,
            lifecycle=lifecycle,
            entity_type=entity_type,
            cursor=decoded_cursor,
            page_size=page_size,
            temporal_filter=temporal_filter,
        )
    except CatalogError as exc:
        raise context._map_catalog_error(exc) from exc
    set_mcp_result_count(len(entity_refs))
    next_cursor_str = json.dumps(next_cursor) if next_cursor else None
    return json.dumps(
        {
            "items": context._serialize(entity_refs),
            "next_cursor": next_cursor_str,
        }
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    mcp_server: FastMCP,
    *,
    retrieval: RetrievalService,
    catalog: CatalogService,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tools onto ``mcp_server``, bound to the given
    services."""
    mcp_server.tool()(
        context._bind_tool(search_capabilities, retrieval=retrieval, session_factory=session_factory, clock=clock)
    )
    mcp_server.tool()(
        context._bind_tool(
            get_dependencies, catalog=catalog, retrieval=retrieval, session_factory=session_factory, clock=clock
        )
    )
    mcp_server.tool()(
        context._bind_tool(
            get_dependents, catalog=catalog, retrieval=retrieval, session_factory=session_factory, clock=clock
        )
    )
    mcp_server.tool()(
        context._bind_tool(
            get_blast_radius, catalog=catalog, retrieval=retrieval, session_factory=session_factory, clock=clock
        )
    )
    mcp_server.tool()(
        context._bind_tool(list_capabilities, retrieval=retrieval, session_factory=session_factory, clock=clock)
    )


__all__: list[str] = [
    "search_capabilities",
    "get_dependencies",
    "get_dependents",
    "get_blast_radius",
    "list_capabilities",
    "register",
]
