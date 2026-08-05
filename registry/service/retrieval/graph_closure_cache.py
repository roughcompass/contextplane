"""Blast-radius — transitive closure over the graph, read cache-first.

get_blast_radius:
  - Read path:
    1. If ``as_of < now() - 90 days``: CTE fallback (beyond cache horizon).
    2. Else: query ``closure_cache``.  Empty result → CTE fallback.
    3. Apply visibility filter after traversal.
    4. Evaluate version predicates; apply ``as_of_version`` filter if set.
    5. Return ``TraversalResult(cache_hit=True|False, ...)``.
  - The cache stores the full depth-5 closure computed by the closure-refresh
    worker off the ``closure_outbox``; ``depth`` and ``edge_types`` are applied
    as post-filters on cache rows rather than re-derived at read time.

_query_closure_cache:
  - Reads the precomputed closure for a (tenant, root, direction) triple.
  - Empty result (no rows, or any DB error) is a cache miss, not a failure:
    the caller falls back to the CTE rather than surfacing an error to a
    read request that has a perfectly good fallback.

_build_result_from_cache / _build_result_from_cte:
  - Symmetric result-assembly paths for the two branches of the read above:
    hydrate member/edge IDs into ``EntityRef``/``EdgeRef`` objects, resolve
    target-entity versions, evaluate version predicates, apply the cross-
    tenant visibility chokepoint, and assemble the ``TraversalResult``.
  - They reuse the hydration and version-predicate helpers ``graph_traversal``
    owns (``_fetch_entity_refs``, ``_fetch_edge_refs``, ``_resolve_entity_versions``,
    ``_evaluate_edge_predicates``, ``_filter_cte_rows_by_version``,
    ``_version_edge_satisfied``) rather than a second copy of the same
    hydration logic reverse traversal already has — a second copy would risk
    the two paths disagreeing about what "satisfied" or "visible" means.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import text

from registry.service.retrieval.graph_cte import _MAX_DEPTH
from registry.service.retrieval.graph_traversal import _GraphTraversalMethods, _version_edge_satisfied
from registry.types import Clock, EdgeRef, EntityRef, TemporalFilter, TenantContext, TraversalResult

_log = logging.getLogger(__name__)

# Cache horizon for get_blast_radius's closure_cache read: as_of values older
# than this many days bypass the cache (it only stores the current closure)
# and fall straight to the CTE. Unrelated to the embedding cache in search.py —
# that one has no time-based horizon at all.
_CACHE_HORIZON_DAYS: int = 90


class _GraphClosureCacheMethods(_GraphTraversalMethods):
    """``RetrievalService.get_blast_radius`` and the closure_cache read path behind it.

    Builds on ``_GraphTraversalMethods`` for the same reason ``_GraphTraversalMethods``
    builds on ``_GraphCteMethods``: the cache-hit and CTE-fallback result builders
    below call ``self._fetch_entity_refs``, ``self._fetch_edge_refs``,
    ``self._resolve_entity_versions``, ``self._evaluate_edge_predicates``, and
    ``self._filter_cte_rows_by_version`` directly, and ``get_blast_radius`` calls
    ``self._traverse_cte`` (inherited transitively). Basing on the concrete class
    that defines them lets mypy check those calls instead of assuming a sibling
    mixin will supply them at composition time.
    """

    async def get_blast_radius(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        direction: str = "reverse",
        depth: int = 5,
        edge_types: list[str] | None = None,
        as_of: datetime.datetime | None = None,
        as_of_version: str | None = None,
        clock: Clock | None = None,
    ) -> TraversalResult:
        """Transitive closure from entity_id with cache-first read path.

        Read path:
        1. If ``as_of < now() - 90 days``: CTE fallback (beyond cache horizon).
        2. Else: query ``closure_cache``.  Empty result → CTE fallback.
        3. Apply visibility filter after traversal.
        4. Evaluate version predicates; apply ``as_of_version`` filter if set.
        5. Return ``TraversalResult(cache_hit=True|False, ...)``.

        Parameters
        ----------
        ctx:
            Tenant + actor context.
        entity_id:
            Root entity for the closure.
        direction:
            ``'forward'`` or ``'reverse'``.  Defaults to ``'reverse'``.
        depth:
            Maximum hop count (1–5).  Capped at 5 by the service layer.
            Only applied on the CTE fallback path; the cache stores the full
            depth-5 closure and the depth parameter is used for post-filtering.
        edge_types:
            Restrict traversal/cache results to these ``rel`` values.
            ``None`` → all vocab minus structural-typing edges.
        as_of:
            Optional ISO-8601 UTC datetime.  When set and before the cache
            horizon (90 days), the CTE fallback is forced.
        as_of_version:
            Optional semver string.  When set, traversal only follows edges
            whose ``properties.version`` predicate is satisfied by this version.
            Edges with no predicate are always included.
            Unsatisfied predicates are flagged in ``version_satisfied``.
        clock:
            Injectable clock.  Defaults to the service's own clock when None.

        Returns
        -------
        TraversalResult
            ``cache_hit=True`` when served from ``closure_cache``.
            ``version_satisfied[edge_id]`` reflects predicate evaluation.
            Nodes are filtered through VisibilityService (or same-tenant
            when VisibilityService is not wired).
        """
        if direction not in ("forward", "reverse"):
            raise ValueError(f"direction must be 'forward' or 'reverse', got {direction!r}")

        effective_clock = clock if clock is not None else self._clock
        now = effective_clock.now()

        capped_depth = min(depth, _MAX_DEPTH)

        # --- Cache horizon check ---
        _cache_horizon = datetime.timedelta(days=_CACHE_HORIZON_DAYS)
        use_cte = False
        if as_of is not None and as_of < (now - _cache_horizon):
            # as_of is before the 90-day cache horizon → must use CTE fallback
            use_cte = True

        temporal_filter = TemporalFilter(as_of=as_of)

        if not use_cte:
            # --- Primary path: closure_cache lookup ---
            cache_rows = await self._query_closure_cache(
                tenant_id=ctx.tenant_id,
                root_entity_id=entity_id,
                direction=direction,
            )
            if cache_rows:
                return await self._build_result_from_cache(
                    ctx=ctx,
                    entity_id=entity_id,
                    direction=direction,
                    depth=capped_depth,
                    edge_types=edge_types,
                    as_of=as_of,
                    as_of_version=as_of_version,
                    now=now,
                    cache_rows=cache_rows,
                )
            # Cache miss → fall through to CTE

        # --- CTE fallback path ---
        async with self._session_factory() as session:
            cte_rows = await self._traverse_cte(
                session=session,
                tenant_id=ctx.tenant_id,
                root_entity_id=entity_id,
                direction=direction,
                depth=capped_depth,
                edge_types=edge_types,
                temporal_filter=temporal_filter,
                as_of=now,
            )

        return await self._build_result_from_cte(
            ctx=ctx,
            entity_id=entity_id,
            direction=direction,
            depth=capped_depth,
            as_of=as_of,
            as_of_version=as_of_version,
            now=now,
            cte_rows=cte_rows,
        )

    async def _query_closure_cache(
        self,
        tenant_id: uuid.UUID,
        root_entity_id: uuid.UUID,
        direction: str,
    ) -> list[dict[str, Any]]:
        """Query closure_cache for the given root + direction.

        Returns a list of dicts with keys:
        ``{member_entity_id, depth, edge_path, edge_rels}``.
        Returns an empty list on cache miss (no rows) or any DB error (logs warning).
        """
        sql = text(
            """
            SELECT member_entity_id, depth, edge_path, edge_rels
            FROM   closure_cache
            WHERE  tenant_id        = :tid
              AND  root_entity_id   = :root_id
              AND  direction        = :direction
            ORDER  BY depth ASC, member_entity_id
            """
        )
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sql,
                    {"tid": tenant_id, "root_id": root_entity_id, "direction": direction},
                )
                rows = result.mappings().all()
        except Exception:
            _log.warning(
                "blast_radius: closure_cache query failed — falling back to CTE",
                extra={"root_entity_id": str(root_entity_id), "direction": direction},
                exc_info=True,
            )
            return []

        return [
            {
                "member_entity_id": row["member_entity_id"],
                "depth": row["depth"],
                "edge_path": list(row["edge_path"]),
                "edge_rels": list(row["edge_rels"]),
            }
            for row in rows
        ]

    async def _build_result_from_cache(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        direction: str,
        depth: int,
        edge_types: list[str] | None,
        as_of: datetime.datetime | None,
        as_of_version: str | None,
        now: datetime.datetime,
        cache_rows: list[dict[str, Any]],
    ) -> TraversalResult:
        """Hydrate a TraversalResult from closure_cache rows.

        Applies depth and edge_types filters post-fetch, then batch-fetches
        entity metadata and edge metadata for visible members.  Evaluates
        version predicates and applies ``as_of_version`` filter when set.
        """
        resolved_edge_types: frozenset[str] | None = frozenset(edge_types) if edge_types is not None else None

        # Filter to depth cap and (optionally) edge_types.
        filtered_rows: list[dict[str, Any]] = []
        for row in cache_rows:
            if row["depth"] > depth:
                continue
            if resolved_edge_types is not None:
                # Only include rows where all edge_rels on the path satisfy
                # the filter (the path's rels are a slice of the full path).
                # We include the row if ANY rel on the path matches the filter;
                # the final edge traversed is the rel that determined reachability.
                row_rels = set(row["edge_rels"])
                if not row_rels.intersection(resolved_edge_types):
                    continue
            filtered_rows.append(row)

        # Collect all edge IDs from all paths for batch hydration.
        all_edge_ids: set[uuid.UUID] = set()
        for row in filtered_rows:
            for eid in row["edge_path"]:
                all_edge_ids.add(eid)

        # Batch-hydrate edges from DB for full edge metadata (including properties).
        fetched_edges: list[EdgeRef] = []
        if all_edge_ids:
            fetched_edges = await self._fetch_edge_refs(
                ctx=ctx,
                edge_ids=list(all_edge_ids),
                now=now,
            )
        hydrated_edges_map: dict[uuid.UUID, EdgeRef] = {e.edge_id: e for e in fetched_edges}

        # Resolve target entity versions for predicate evaluation.
        dst_entity_ids: set[uuid.UUID] = {e.dst_entity_id for e in hydrated_edges_map.values()}
        entity_versions: dict[uuid.UUID, str | None] = {}
        if dst_entity_ids:
            entity_versions = await self._resolve_entity_versions(
                tenant_id=ctx.tenant_id,
                entity_ids=list(dst_entity_ids),
                as_of=as_of,
                now=now,
            )

        # Compute version_satisfied per edge.
        version_satisfied: dict[uuid.UUID, bool] = self._evaluate_edge_predicates(
            edges=fetched_edges,
            entity_versions=entity_versions,
        )

        # When as_of_version is set, filter rows to only predicate-satisfied paths.
        if as_of_version is not None:
            edge_satisfied_for_filter: dict[uuid.UUID, bool] = {
                eid: _version_edge_satisfied(hydrated_edges_map.get(eid), as_of_version, entity_versions)
                for eid in all_edge_ids
            }
            filtered_rows = self._filter_cte_rows_by_version(
                cte_rows=filtered_rows,
                edge_predicates_satisfied=edge_satisfied_for_filter,
            )

        # Collect unique member entity IDs.
        member_entity_ids: list[uuid.UUID] = []
        seen_members: set[uuid.UUID] = set()
        for row in filtered_rows:
            mid = row["member_entity_id"]
            if mid not in seen_members:
                seen_members.add(mid)
                member_entity_ids.append(mid)

        # Cross-tenant visibility filter (cache hit path).
        visible_member_ids: set[uuid.UUID] = await self._apply_visibility(ctx, member_entity_ids)

        # Collect edge objects from the final filtered paths.
        edges: list[EdgeRef] = []
        seen_edge_ids: set[uuid.UUID] = set()
        for row in filtered_rows:
            if row["member_entity_id"] not in visible_member_ids:
                continue
            for eid in row["edge_path"]:
                if eid not in seen_edge_ids:
                    seen_edge_ids.add(eid)
                    edge_obj = hydrated_edges_map.get(eid)
                    if edge_obj is not None:
                        edges.append(edge_obj)

        # Trim version_satisfied to edges in result.
        version_satisfied = {eid: version_satisfied.get(eid, True) for eid in seen_edge_ids}

        # Batch-hydrate entity metadata (cache hit path). See note in
        # get_reverse_traversal: cross-tenant fetch when visibility is wired.
        nodes: list[EntityRef] = []
        if visible_member_ids:
            nodes = await self._fetch_entity_refs(
                ctx=ctx,
                entity_ids=list(visible_member_ids),
                enforce_same_tenant=self._visibility is None,
            )

        _log.debug(
            "blast_radius completed (cache hit)",
            extra={
                "root_entity_id": str(entity_id),
                "direction": direction,
                "depth": depth,
                "nodes": len(nodes),
                "edges": len(edges),
                "as_of_version": as_of_version,
                "tenant_id": str(ctx.tenant_id),
            },
        )

        return TraversalResult(
            root_entity_id=entity_id,
            depth=depth,
            direction=direction,  # type: ignore[arg-type]
            as_of=as_of,
            nodes=nodes,
            edges=edges,
            version_satisfied=version_satisfied,
            cache_hit=True,
        )

    async def _build_result_from_cte(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        direction: str,
        depth: int,
        as_of: datetime.datetime | None,
        as_of_version: str | None,
        now: datetime.datetime,
        cte_rows: list[dict[str, Any]],
    ) -> TraversalResult:
        """Hydrate a TraversalResult from CTE rows.

        Batch-fetches real edge rows (with properties) to support version
        predicate evaluation.  Applies ``as_of_version`` filter when set.
        """
        # Collect all edge IDs for batch hydration.
        all_edge_ids: set[uuid.UUID] = set()
        for row in cte_rows:
            for eid in row["edge_path"]:
                all_edge_ids.add(eid)

        # Batch-fetch real edge rows (with properties).
        fetched_edges: list[EdgeRef] = []
        if all_edge_ids:
            fetched_edges = await self._fetch_edge_refs(
                ctx=ctx,
                edge_ids=list(all_edge_ids),
                now=now,
            )
        hydrated_edges_map: dict[uuid.UUID, EdgeRef] = {e.edge_id: e for e in fetched_edges}

        # Resolve target entity versions for predicate evaluation.
        dst_entity_ids: set[uuid.UUID] = {e.dst_entity_id for e in hydrated_edges_map.values()}
        entity_versions: dict[uuid.UUID, str | None] = {}
        if dst_entity_ids:
            entity_versions = await self._resolve_entity_versions(
                tenant_id=ctx.tenant_id,
                entity_ids=list(dst_entity_ids),
                as_of=as_of,
                now=now,
            )

        # Compute version_satisfied per edge.
        version_satisfied: dict[uuid.UUID, bool] = self._evaluate_edge_predicates(
            edges=fetched_edges,
            entity_versions=entity_versions,
        )

        # When as_of_version is set, filter CTE rows to predicate-satisfied paths.
        if as_of_version is not None:
            edge_satisfied_for_filter: dict[uuid.UUID, bool] = {
                eid: _version_edge_satisfied(hydrated_edges_map.get(eid), as_of_version, entity_versions)
                for eid in all_edge_ids
            }
            cte_rows = self._filter_cte_rows_by_version(
                cte_rows=cte_rows,
                edge_predicates_satisfied=edge_satisfied_for_filter,
            )

        member_entity_ids: list[uuid.UUID] = []
        seen_members: set[uuid.UUID] = set()
        for row in cte_rows:
            mid = row["member_entity_id"]
            if mid not in seen_members:
                seen_members.add(mid)
                member_entity_ids.append(mid)

        # Cross-tenant visibility filter (CTE fallback path).
        visible_member_ids: set[uuid.UUID] = await self._apply_visibility(ctx, member_entity_ids)

        edges: list[EdgeRef] = []
        seen_edge_ids: set[uuid.UUID] = set()
        for row in cte_rows:
            if row["member_entity_id"] not in visible_member_ids:
                continue
            for eid in row["edge_path"]:
                if eid not in seen_edge_ids:
                    seen_edge_ids.add(eid)
                    edge_obj = hydrated_edges_map.get(eid)
                    if edge_obj is not None:
                        edges.append(edge_obj)
                    else:
                        # Edge not returned from DB (invalidated/purged); emit stub.
                        edges.append(
                            EdgeRef(
                                edge_id=eid,
                                tenant_id=ctx.tenant_id,
                                src_entity_id=uuid.UUID(int=0),
                                rel=row["edge_rels"][row["edge_path"].index(eid)],
                                dst_entity_id=uuid.UUID(int=0),
                                properties=None,
                                t_valid_from=now,
                                t_valid_to=None,
                                t_ingested_at=now,
                                t_invalidated_at=None,
                            )
                        )

        # Trim version_satisfied to edges in result.
        version_satisfied = {eid: version_satisfied.get(eid, True) for eid in seen_edge_ids}

        # CTE fallback path: visibility-vetted IDs may span tenants.
        nodes: list[EntityRef] = []
        if visible_member_ids:
            nodes = await self._fetch_entity_refs(
                ctx=ctx,
                entity_ids=list(visible_member_ids),
                enforce_same_tenant=self._visibility is None,
            )

        _log.debug(
            "blast_radius completed (CTE fallback)",
            extra={
                "root_entity_id": str(entity_id),
                "direction": direction,
                "depth": depth,
                "nodes": len(nodes),
                "edges": len(edges),
                "as_of_version": as_of_version,
                "tenant_id": str(ctx.tenant_id),
            },
        )

        return TraversalResult(
            root_entity_id=entity_id,
            depth=depth,
            direction=direction,  # type: ignore[arg-type]
            as_of=as_of,
            nodes=nodes,
            edges=edges,
            version_satisfied=version_satisfied,
            cache_hit=False,
        )
