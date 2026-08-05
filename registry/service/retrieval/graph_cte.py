"""Recursive-CTE graph traversal — the engine reverse traversal and blast-radius share.

get_dependencies:
  - Recursive CTE depth capped at min(requested, 5).
  - depth_counter column counts inclusive hops (1-based): the root entity is
    hop 0; its direct neighbours are hop 1.
  - Predates ``_traverse_cte`` below and keeps its own query shape rather than
    being rebased onto the shared primitive: it has different depth_counter
    semantics and a narrower default edge-type set (``_GRAPH_EDGE_TYPES``,
    owned by ``_query_primitives`` because search's graph arm restricts to
    the same three relationship kinds), and rebasing it would change what it
    returns. It lives in this module rather than alongside search because its
    concern is traversal, not ranking, and it shares ``_MAX_DEPTH`` and the
    bi-temporal fragment builder with every other method here.

_traverse_cte:
  - Shared recursive CTE primitive for forward and reverse traversal.
  - direction='forward': follows src→dst (who does root depend on?).
  - direction='reverse': follows dst→src (who depends on root?).
  - Depth capped at _MAX_DEPTH (5). Default edge types exclude concept_of,
    operation_of, instance_of (structural-typing edges, not dependencies).
  - Returns list[dict] with member_entity_id, depth, edge_path, edge_rels.
  - Visibility filtering is the caller's responsibility; this method returns
    all reachable members without cross-tenant filtering. The reverse-
    traversal and blast-radius entry points apply visibility after calling it.
  - Callers provide an open AsyncSession; this method does not manage sessions.

traverse_for_closure_refresh:
  - Public entry point for background workers (the closure-cache refresh
    worker is the only current caller). Delegates straight to
    ``_traverse_cte``; keeping the boundary explicit means a signature change
    or caching added to ``_traverse_cte`` is caught at this call site rather
    than silently broken by a caller reaching past a private method.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.service.retrieval._query_primitives import (
    _GRAPH_EDGE_TYPES,
    _RetrievalState,
    temporal_sql_fragments,
)
from registry.types import EdgeRef, TemporalFilter, TenantContext

# Maximum recursion depth for any CTE (depth > 5 risks performance on large graphs).
_MAX_DEPTH = 5

# Edge rel values excluded from the default traversal set.
# These are structural / typing edges, not dependency relationships.
_TRAVERSAL_EXCLUDED_RELS: frozenset[str] = frozenset({"concept_of", "operation_of", "instance_of"})

# All vocab edge_rel values known to the system.
# Updated when new edge_rel vocab rows are added by migration.
_ALL_VOCAB_RELS: tuple[str, ...] = (
    "depends_on",
    "integrates_with",
    "event_source",
    "replaced_by",
    "requires",
    "conflicts_with",
    "composes",
    "provides_to",
    "concept_of",
    "operation_of",
    "instance_of",
)

# Default edge types for reverse/blast-radius traversal: all vocab minus excluded set.
_DEFAULT_TRAVERSAL_EDGE_TYPES: tuple[str, ...] = tuple(r for r in _ALL_VOCAB_RELS if r not in _TRAVERSAL_EXCLUDED_RELS)


class _GraphCteMethods(_RetrievalState):
    """``RetrievalService``'s recursive-CTE traversal engine and forward-dependency walk."""

    async def get_dependencies(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        depth: int,
        temporal_filter: TemporalFilter,
    ) -> list[EdgeRef]:
        """Recursive CTE on edges, depth capped at _MAX_DEPTH (G4 binding).

        depth_counter counts inclusive hops (1-based): the root entity is hop 0;
        its direct neighbours are hop 1.  Caller receives all edges up to depth.
        """
        capped_depth = min(depth, _MAX_DEPTH)
        now = self._clock.now()

        # Anchor branch: plain `FROM edges` — no join ambiguity, no alias needed.
        tf_sql_anchor, tf_params = temporal_sql_fragments(temporal_filter, now)
        # Recursive branch: `FROM edges e JOIN dep_cte …` — bare column names are
        # ambiguous because dep_cte also exposes the same temporal columns.  Use
        # the "e." prefix so PostgreSQL resolves references unambiguously.
        tf_sql_rec, _ = temporal_sql_fragments(temporal_filter, now, table_alias="e")
        # Both fragments share param names with identical values; merge is safe.

        # An f-string, not `+`-concatenation of the two temporal fragments: the
        # rendered SQL is identical either way, but a single joined string (rather
        # than a `+`-chain of separate literals and variables) is what lets the
        # suppression comment on the closing `"""` below actually attach to this
        # statement instead of silently matching nothing (ruff's line resolution
        # for suppression comments differs between the two forms for a
        # diagnostic spanning this many lines).
        sql = text(
            f"""
            WITH RECURSIVE dep_cte AS (
                SELECT
                    edge_id,
                    tenant_id,
                    src_entity_id,
                    rel,
                    dst_entity_id,
                    properties,
                    is_authoritative,
                    sync_run_id,
                    t_valid_from,
                    t_valid_to,
                    t_ingested_at,
                    t_invalidated_at,
                    1 AS depth_counter
                FROM edges
                WHERE src_entity_id = :root_id
                  AND tenant_id = :tid
                  AND rel = ANY(:edge_types)
                  AND {tf_sql_anchor}

                UNION ALL

                SELECT
                    e.edge_id,
                    e.tenant_id,
                    e.src_entity_id,
                    e.rel,
                    e.dst_entity_id,
                    e.properties,
                    e.is_authoritative,
                    e.sync_run_id,
                    e.t_valid_from,
                    e.t_valid_to,
                    e.t_ingested_at,
                    e.t_invalidated_at,
                    dep_cte.depth_counter + 1
                FROM edges e
                JOIN dep_cte ON e.src_entity_id = dep_cte.dst_entity_id
                WHERE e.tenant_id = :tid
                  AND e.rel = ANY(:edge_types)
                  AND dep_cte.depth_counter < :max_depth
                  AND {tf_sql_rec}
            )
            SELECT * FROM dep_cte ORDER BY depth_counter, edge_id
            """  # noqa: S608 - tf_sql_anchor/tf_sql_rec come from temporal_sql_fragments(), which only ever interpolates a fixed "" or "e." alias prefix into column names; every actual value is bound via :param
        )

        params: dict[str, Any] = {
            "root_id": entity_id,
            "tid": ctx.tenant_id,
            "edge_types": list(_GRAPH_EDGE_TYPES),
            "max_depth": capped_depth,
            **tf_params,
        }

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return [
            EdgeRef(
                edge_id=row["edge_id"],
                tenant_id=row["tenant_id"],
                src_entity_id=row["src_entity_id"],
                rel=row["rel"],
                dst_entity_id=row["dst_entity_id"],
                properties=row["properties"],
                t_valid_from=row["t_valid_from"],
                t_valid_to=row["t_valid_to"],
                t_ingested_at=row["t_ingested_at"],
                t_invalidated_at=row["t_invalidated_at"],
            )
            for row in rows
            if row["tenant_id"] == ctx.tenant_id  # tenant assertion
        ]

    # ------------------------------------------------------------------
    # Graph traversal primitive — shared by reverse-traversal and blast-radius
    # ------------------------------------------------------------------

    async def _traverse_cte(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        root_entity_id: uuid.UUID,
        direction: str,
        depth: int,
        edge_types: list[str] | None,
        temporal_filter: TemporalFilter,
        as_of: datetime.datetime,
    ) -> list[dict[str, Any]]:
        """Recursive CTE traversal primitive.  No version predicates; no visibility filter.

        Parameters
        ----------
        session:
            Active async session provided by the caller.  Callers are responsible
            for opening and closing the session; this method does not manage it.
        tenant_id:
            Tenant scope for all edge lookups.
        root_entity_id:
            Starting entity for the traversal.
        direction:
            ``'forward'`` — follows edges where ``src_entity_id = root`` outward
            through ``dst_entity_id`` (who does root depend on?).
            ``'reverse'`` — follows edges where ``dst_entity_id = root`` inward
            through ``src_entity_id`` (who depends on root?).
        depth:
            Maximum hop count from root.  Internally capped at ``_MAX_DEPTH`` (5).
        edge_types:
            Restrict traversal to these ``rel`` values.  ``None`` → all vocab
            edge_rel values minus the structural-typing exclusion set
            (``concept_of``, ``operation_of``, ``instance_of``).
        temporal_filter:
            Bi-temporal filter applied at every CTE hop.
        as_of:
            Pre-resolved ``now()`` value passed by the caller.  Used for the
            ``tf_now`` parameter in current-truth fragments.

        Returns
        -------
        list[dict]
            One dict per row: ``{member_entity_id, depth, edge_path, edge_rels}``.
            ``member_entity_id`` is the non-root end of each traversal path.
            ``depth`` is the hop count from root (1-based: immediate neighbours = 1).
            ``edge_path`` is an ordered list of edge UUIDs on the shortest path.
            ``edge_rels`` is a parallel list of rel values for each edge in the path.
            Rows are ordered by depth ascending, then member_entity_id.
        """
        if direction not in ("forward", "reverse"):
            raise ValueError(f"direction must be 'forward' or 'reverse', got {direction!r}")

        capped_depth = min(depth, _MAX_DEPTH)

        resolved_edge_types: list[str] = (
            list(edge_types) if edge_types is not None else list(_DEFAULT_TRAVERSAL_EDGE_TYPES)
        )

        # Build bi-temporal SQL fragments for anchor and recursive branches.
        # The anchor branch uses bare column names; the recursive branch uses the
        # "e." alias to disambiguate from the CTE's own columns.
        tf_anchor, tf_params = temporal_sql_fragments(temporal_filter, as_of)
        tf_rec, _ = temporal_sql_fragments(temporal_filter, as_of, table_alias="e")
        # Both fragments share param names with identical values — safe to merge once.

        if direction == "forward":
            # Seed: root is the source; we follow outward to destinations.
            seed_where = "src_entity_id = :root_id"
            # Recursive join: the previously visited destination is the next source.
            rec_join = "e.src_entity_id = cte.member_entity_id"
            rec_member = "e.dst_entity_id"
        else:
            # Seed: root is the destination; we follow inward to sources.
            seed_where = "dst_entity_id = :root_id"
            # Recursive join: the previously visited source is the next destination.
            rec_join = "e.dst_entity_id = cte.member_entity_id"
            rec_member = "e.src_entity_id"

        sql = text(
            f"""
            WITH RECURSIVE cte AS (
                -- Anchor: immediate neighbours of root
                SELECT
                    edge_id,
                    tenant_id,
                    rel,
                    src_entity_id,
                    dst_entity_id,
                    CASE
                        WHEN '{direction}' = 'forward' THEN dst_entity_id
                        ELSE src_entity_id
                    END                         AS member_entity_id,
                    1                           AS depth,
                    ARRAY[edge_id]              AS edge_path,
                    ARRAY[rel]                  AS edge_rels
                FROM edges
                WHERE {seed_where}
                  AND tenant_id = :tid
                  AND rel = ANY(:edge_types)
                  AND {tf_anchor}

                UNION ALL

                -- Recursive: extend path by one more hop
                SELECT
                    e.edge_id,
                    e.tenant_id,
                    e.rel,
                    e.src_entity_id,
                    e.dst_entity_id,
                    {rec_member}                AS member_entity_id,
                    cte.depth + 1               AS depth,
                    cte.edge_path || e.edge_id  AS edge_path,
                    cte.edge_rels || e.rel      AS edge_rels
                FROM edges e
                JOIN cte ON {rec_join}
                WHERE e.tenant_id = :tid
                  AND e.rel = ANY(:edge_types)
                  AND cte.depth < :max_depth
                  AND {tf_rec}
            )
            SELECT DISTINCT ON (member_entity_id)
                member_entity_id,
                depth,
                edge_path,
                edge_rels
            FROM cte
            WHERE member_entity_id != :root_id
            ORDER BY member_entity_id, depth ASC
            LIMIT 10000
            """
        )

        params: dict[str, Any] = {
            "root_id": root_entity_id,
            "tid": tenant_id,
            "edge_types": resolved_edge_types,
            "max_depth": capped_depth,
            **tf_params,
        }

        result = await session.execute(sql, params)
        raw_rows = result.mappings().all()

        return [
            {
                "member_entity_id": row["member_entity_id"],
                "depth": row["depth"],
                "edge_path": list(row["edge_path"]),
                "edge_rels": list(row["edge_rels"]),
            }
            for row in raw_rows
            if row["member_entity_id"] != root_entity_id  # defense-in-depth
        ]

    async def traverse_for_closure_refresh(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        root_entity_id: uuid.UUID,
        direction: str,
        depth: int,
        edge_types: list[str] | None,
        temporal_filter: TemporalFilter,
        as_of: datetime.datetime,
    ) -> list[dict[str, Any]]:
        """Public traversal entry point for background workers.

        This is the only surface the closure-refresh worker (and any other
        background job) should call when it needs raw CTE rows.  It delegates
        directly to ``_traverse_cte``; the private method remains the shared
        internal implementation used by the service's own read paths.

        Keeping this boundary explicit means that renaming, signature changes,
        or caching added to ``_traverse_cte`` will be caught at the call site
        rather than silently broken by a private-method refactor.

        Parameters and return value are identical to ``_traverse_cte``; see
        that method's docstring for full parameter descriptions.
        """
        return await self._traverse_cte(
            session=session,
            tenant_id=tenant_id,
            root_entity_id=root_entity_id,
            direction=direction,
            depth=depth,
            edge_types=edge_types,
            temporal_filter=temporal_filter,
            as_of=as_of,
        )
