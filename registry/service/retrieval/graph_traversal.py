"""Reverse traversal, plus the hydration and version-predicate plumbing every
graph read path (reverse traversal and blast-radius alike) assembles a
``TraversalResult`` through.

get_reverse_traversal:
  - Wires ``_traverse_cte`` (``graph_cte``) through visibility filtering and
    version-predicate evaluation to answer "who depends on entity_id?".
  - ``cache_hit`` is always ``False`` — the closure cache backs blast-radius
    only; reverse traversal always walks the CTE fresh.

Hydration helpers (``_fetch_entity_refs``, ``_fetch_edge_refs``):
  - Batch-fetch ``EntityRef`` / ``EdgeRef`` rows for a set of IDs collected
    from CTE or closure-cache paths, so a traversal with N members issues one
    round-trip per ref type rather than N.
  - ``_fetch_entity_refs`` takes an ``enforce_same_tenant`` flag: reverse
    traversal and blast-radius both fetch by entity_id alone (no tenant
    filter) once a member set has already cleared the visibility chokepoint,
    since a visible member may legitimately belong to another tenant.

Version predicate evaluation:
  - _evaluate_edge_predicates: for each hydrated EdgeRef, checks the
    properties.version predicate against the target entity's resolved version
    (looked up from attributes table key='version').
  - A missing predicate means the edge is always satisfied (True).
  - A malformed predicate or missing entity version returns False.
  - When as_of_version is set, _filter_cte_rows_by_version removes CTE rows
    whose terminal edge predicate is not satisfied; the rest of the path is
    still returned but those edges are excluded from traversal following.
  - _version_edge_satisfied is the single-edge form of the same check, used
    when a caller (here, or blast-radius's cache/CTE result builders) needs a
    per-edge boolean rather than a batch dict.
  - Both get_reverse_traversal and blast-radius wire this through.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from registry.service.catalog.version_predicates import evaluate_version_predicate
from registry.service.retrieval._query_primitives import temporal_sql_fragments
from registry.service.retrieval.graph_cte import _MAX_DEPTH, _GraphCteMethods
from registry.types import Clock, EdgeRef, EntityRef, TemporalFilter, TenantContext, TraversalResult

_log = logging.getLogger(__name__)


def _version_edge_satisfied(
    edge: EdgeRef | None,
    as_of_version: str,
    entity_versions: dict[uuid.UUID, str | None],
) -> bool:
    """Return True if ``edge`` is satisfied by ``as_of_version``.

    - If ``edge`` is None (not hydrated): treated as satisfied.
    - If ``edge.properties`` has no ``version`` key: always satisfied.
    - If the entity version is unknown: unsatisfied (False).
    - Otherwise: delegates to ``evaluate_version_predicate``.

    This is the per-edge predicate check used when ``as_of_version`` is set
    to decide whether to include an edge in the traversal result.
    """
    if edge is None:
        return True
    predicate: str | None = None
    if edge.properties and isinstance(edge.properties, dict):
        predicate = edge.properties.get("version")
    if not predicate:
        return True
    target_version = entity_versions.get(edge.dst_entity_id)
    if target_version is None:
        return False
    return evaluate_version_predicate(target_version, predicate)


class _GraphTraversalMethods(_GraphCteMethods):
    """``RetrievalService``'s reverse-traversal entry point and its shared hydration plumbing.

    Builds on ``_GraphCteMethods`` (``get_reverse_traversal`` calls
    ``self._traverse_cte`` directly) rather than only on ``_RetrievalState``,
    so the dependency on the CTE engine is a real base class mypy can check
    rather than a runtime-only assumption about how ``RetrievalService``
    happens to combine its mixins.
    """

    async def get_reverse_traversal(
        self,
        ctx: TenantContext,
        entity_id: uuid.UUID,
        depth: int = 2,
        edge_types: list[str] | None = None,
        as_of: Any | None = None,
        as_of_version: str | None = None,
        clock: Clock | None = None,
    ) -> TraversalResult:
        """Reverse traversal: who depends ON entity_id?

        Symmetric to ``get_dependencies`` (forward).  Uses ``_traverse_cte``
        with ``direction='reverse'``.

        Parameters
        ----------
        ctx:
            Tenant + actor context.  Ownership checks are applied here.
        entity_id:
            Root entity for the traversal (the node being depended upon).
        depth:
            Maximum hop count (1–5).  Capped at 5 by the service layer
            regardless of caller input (G4 binding).
        edge_types:
            Restrict traversal to these ``rel`` values.  ``None`` → all vocab
            minus structural-typing edges (``concept_of``, ``operation_of``,
            ``instance_of``).
        as_of:
            Optional ISO-8601 UTC datetime for time-travel queries.  ``None``
            → current-truth filter (``t_invalidated_at IS NULL``).
        as_of_version:
            Optional semver string.  When set, traversal only follows edges
            whose ``properties.version`` predicate is satisfied by this version.
            Edges with no predicate are always included.
            Unsatisfied predicates are flagged in ``version_satisfied`` but do
            not prune paths unless ``as_of_version`` is supplied.
        clock:
            Injectable clock.  Defaults to the service's own clock when
            ``None``.

        Returns
        -------
        TraversalResult
            ``direction='reverse'``, ``cache_hit=False`` (cache is T06).
            ``version_satisfied[edge_id]`` reflects predicate evaluation.
            When ``as_of_version`` is set, only edges whose predicate is
            satisfied are included in the result; other edges are omitted.
            Visibility filter applied after traversal: only nodes visible to
            ``ctx.tenant_id`` are included in the returned ``nodes`` set.
        """
        effective_clock = clock if clock is not None else self._clock
        now = effective_clock.now()

        capped_depth = min(depth, _MAX_DEPTH)

        # Build temporal filter from the caller-supplied as_of datetime.
        temporal_filter = TemporalFilter(as_of=as_of)

        async with self._session_factory() as session:
            cte_rows = await self._traverse_cte(
                session=session,
                tenant_id=ctx.tenant_id,
                root_entity_id=entity_id,
                direction="reverse",
                depth=capped_depth,
                edge_types=edge_types,
                temporal_filter=temporal_filter,
                as_of=now,
            )

        # Collect all edge IDs from all CTE paths for batch hydration.
        all_edge_ids: set[uuid.UUID] = set()
        for row in cte_rows:
            for eid in row["edge_path"]:
                all_edge_ids.add(eid)

        # Batch-fetch real edge rows (with properties) so we can evaluate
        # version predicates.  This replaces the prior stub approach.
        hydrated_edges_map: dict[uuid.UUID, EdgeRef] = {}
        if all_edge_ids:
            fetched = await self._fetch_edge_refs(
                ctx=ctx,
                edge_ids=list(all_edge_ids),
                now=now,
            )
            hydrated_edges_map = {e.edge_id: e for e in fetched}

        # Evaluate version predicates for all hydrated edges.
        # member_entity_id is the SOURCE in a reverse traversal (the node that
        # depends on root); the edge dst is what the predicate guards.
        # We resolve the target entity's version from the attributes table.
        # For reverse traversal, the edge goes src→dst where dst is the target
        # from the predicate's point of view (the entity being required).
        # We collect dst_entity_ids from fetched edges to resolve versions.
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
            edges=list(hydrated_edges_map.values()),
            entity_versions=entity_versions,
        )

        # When as_of_version is set, filter CTE rows to only those whose paths
        # consist entirely of predicate-satisfied edges.  Rows where any edge
        # on the path is unsatisfied are excluded from traversal results.
        if as_of_version is not None:
            cte_rows = self._filter_cte_rows_by_version(
                cte_rows=cte_rows,
                edge_predicates_satisfied={
                    eid: _version_edge_satisfied(hydrated_edges_map.get(eid), as_of_version, entity_versions)
                    for eid in all_edge_ids
                },
            )

        # Collect unique member entity IDs reached by the (filtered) traversal.
        member_entity_ids: list[uuid.UUID] = []
        seen_members: set[uuid.UUID] = set()
        for row in cte_rows:
            mid = row["member_entity_id"]
            if mid not in seen_members:
                seen_members.add(mid)
                member_entity_ids.append(mid)

        # Cross-tenant chokepoint: filter members through VisibilityService.
        # Falls back to same-tenant when no visibility is wired (test paths).
        visible_member_ids: set[uuid.UUID] = await self._apply_visibility(ctx, member_entity_ids)

        # Collect unique edge IDs from the (filtered) traversal paths.
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
                        # Edge was not returned from DB (invalidated or missing);
                        # emit a minimal stub so the path is still traceable.
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

        # Trim version_satisfied to only the edges present in the (filtered) result.
        version_satisfied = {eid: version_satisfied.get(eid, True) for eid in seen_edge_ids}

        # Hydrate EntityRef stubs for visible members.
        # A secondary SELECT fetches entity metadata for all member IDs in one
        # round-trip (avoids N+1). Post-visibility IDs may span tenants, so
        # the SQL filter relaxes to entity_id only (VisibilityService has already vetted).
        nodes: list[EntityRef] = []
        if visible_member_ids:
            nodes = await self._fetch_entity_refs(
                ctx=ctx,
                entity_ids=list(visible_member_ids),
                enforce_same_tenant=self._visibility is None,
            )

        _log.debug(
            "reverse_traversal completed",
            extra={
                "root_entity_id": str(entity_id),
                "depth": capped_depth,
                "nodes": len(nodes),
                "edges": len(edges),
                "as_of_version": as_of_version,
                "tenant_id": str(ctx.tenant_id),
            },
        )

        return TraversalResult(
            root_entity_id=entity_id,
            depth=capped_depth,
            direction="reverse",
            as_of=as_of,
            nodes=nodes,
            edges=edges,
            version_satisfied=version_satisfied,
            cache_hit=False,
        )

    async def _fetch_entity_refs(
        self,
        ctx: TenantContext,
        entity_ids: list[uuid.UUID],
        enforce_same_tenant: bool = True,
    ) -> list[EntityRef]:
        """Batch-fetch EntityRef objects for a list of entity IDs.

        Two modes:

        * ``enforce_same_tenant=True`` (default): filters at SQL by
          ``ctx.tenant_id``, with a defense-in-depth assertion when rows come
          back. Callers without a VisibilityService in play stay on this path.

        * ``enforce_same_tenant=False`` (post-visibility path): fetches by
          entity_id alone. Callers MUST have already gated ``entity_ids``
          through :py:meth:`_apply_visibility` — the cross-tenant chokepoint
          — otherwise this leaks cross-tenant rows. The defense-in-depth
          assertion is intentionally dropped because visible entities
          legitimately belong to other tenants.

        Returns only active entities found in the DB; missing IDs are
        silently omitted (deleted/purged entities).
        """
        if not entity_ids:
            return []

        if enforce_same_tenant:
            sql = text(
                """
                SELECT entity_id, tenant_id, entity_type, name,
                       external_id, is_active, created_at
                FROM entities
                WHERE tenant_id = :tid
                  AND entity_id = ANY(:ids)
                  AND is_active = TRUE
                ORDER BY created_at DESC, entity_id
                """
            )
            params: dict[str, Any] = {"tid": ctx.tenant_id, "ids": entity_ids}
        else:
            sql = text(
                """
                SELECT entity_id, tenant_id, entity_type, name,
                       external_id, is_active, created_at
                FROM entities
                WHERE entity_id = ANY(:ids)
                  AND is_active = TRUE
                ORDER BY created_at DESC, entity_id
                """
            )
            params = {"ids": entity_ids}

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return [
            EntityRef(
                entity_id=row["entity_id"],
                tenant_id=row["tenant_id"],
                entity_type=row["entity_type"],
                name=row["name"],
                external_id=row["external_id"],
                is_active=row["is_active"],
                created_at=row["created_at"],
            )
            for row in rows
            if (not enforce_same_tenant) or row["tenant_id"] == ctx.tenant_id
        ]

    # ------------------------------------------------------------------
    # Version predicate helpers
    # ------------------------------------------------------------------

    async def _resolve_entity_versions(
        self,
        tenant_id: uuid.UUID,
        entity_ids: list[uuid.UUID],
        as_of: Any | None,
        now: Any,
    ) -> dict[uuid.UUID, str | None]:
        """Batch-fetch the ``version`` attribute for a list of entity IDs.

        Version is stored in the ``attributes`` table as key='version' with
        a JSONB string value (e.g. ``"2.4.0"``).  Returns a mapping of
        entity_id → version string (or ``None`` if not found).

        When ``as_of`` is set, fetches the version valid at that point in time;
        otherwise fetches the current-truth version (``t_invalidated_at IS NULL``).
        """
        if not entity_ids:
            return {}

        temporal_filter = TemporalFilter(as_of=as_of)
        tf_sql, tf_params = temporal_sql_fragments(temporal_filter, now, table_alias="a")

        sql = text(
            f"""
            SELECT DISTINCT ON (a.entity_id)
                a.entity_id,
                a.value
            FROM attributes a
            WHERE a.tenant_id = :tid
              AND a.entity_id = ANY(:ids)
              AND a.key = 'version'
              AND {tf_sql}
            ORDER BY a.entity_id, a.t_valid_from DESC
            """
        )

        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sql,
                    {"tid": tenant_id, "ids": entity_ids, **tf_params},
                )
                rows = result.mappings().all()
        except Exception:
            _log.warning(
                "version_predicate: entity version lookup failed; treating all as no-version",
                extra={"tenant_id": str(tenant_id)},
                exc_info=True,
            )
            return {eid: None for eid in entity_ids}

        result_map: dict[uuid.UUID, str | None] = {eid: None for eid in entity_ids}
        for row in rows:
            # JSONB string value is returned as Python str with surrounding quotes
            # by psycopg2/asyncpg when the JSONB type is a JSON string.
            # asyncpg returns the deserialized Python value directly.
            raw = row["value"]
            if isinstance(raw, str):
                ver = raw.strip('"')
            elif isinstance(raw, dict):
                # Unexpected; skip
                ver = None
            else:
                ver = str(raw) if raw is not None else None
            result_map[row["entity_id"]] = ver

        return result_map

    @staticmethod
    def _evaluate_edge_predicates(
        edges: list[EdgeRef],
        entity_versions: dict[uuid.UUID, str | None],
    ) -> dict[uuid.UUID, bool]:
        """Evaluate ``properties.version`` predicates for each edge.

        For each edge:
        - If ``properties`` is None or has no ``version`` key: ``True``
          (no constraint = always satisfied).
        - If the predicate string is empty: ``True``.
        - Otherwise: evaluate via ``evaluate_version_predicate`` against the
          target entity's resolved version.  If the entity version is unknown
          or the predicate is malformed: ``False``.

        Returns a dict mapping edge_id → bool.
        """
        result: dict[uuid.UUID, bool] = {}
        for edge in edges:
            predicate: str | None = None
            if edge.properties and isinstance(edge.properties, dict):
                predicate = edge.properties.get("version")

            if not predicate:
                # No predicate → always satisfied.
                result[edge.edge_id] = True
                continue

            target_version = entity_versions.get(edge.dst_entity_id)
            if target_version is None:
                # Cannot resolve target version → predicate is unsatisfied.
                result[edge.edge_id] = False
                continue

            result[edge.edge_id] = evaluate_version_predicate(target_version, predicate)

        return result

    @staticmethod
    def _filter_cte_rows_by_version(
        cte_rows: list[dict[str, Any]],
        edge_predicates_satisfied: dict[uuid.UUID, bool],
    ) -> list[dict[str, Any]]:
        """Filter CTE rows to only those whose entire edge_path is version-satisfied.

        When ``as_of_version`` is set, traversal must only follow edges whose
        predicate is satisfied.  A row is included only if every edge on its
        path satisfies its predicate (or has no predicate).

        Edges not present in ``edge_predicates_satisfied`` (e.g. invalidated
        edges not returned from the DB) are treated as satisfied (True) to
        avoid incorrectly pruning paths with missing edge metadata.
        """
        filtered: list[dict[str, Any]] = []
        for row in cte_rows:
            if all(edge_predicates_satisfied.get(eid, True) for eid in row["edge_path"]):
                filtered.append(row)
        return filtered

    async def _fetch_edge_refs(
        self,
        ctx: TenantContext,
        edge_ids: list[uuid.UUID],
        now: Any,
    ) -> list[EdgeRef]:
        """Batch-fetch EdgeRef objects for a list of edge IDs.

        Filters to ctx.tenant_id.  Missing or invalidated edges are silently
        omitted.  Returns edges active at current truth (t_invalidated_at IS NULL).
        Falls back to returning stub EdgeRef objects on DB error.
        """
        if not edge_ids:
            return []

        sql = text(
            """
            SELECT edge_id, tenant_id, src_entity_id, rel, dst_entity_id,
                   properties, t_valid_from, t_valid_to, t_ingested_at, t_invalidated_at
            FROM   edges
            WHERE  tenant_id = :tid
              AND  edge_id   = ANY(:ids)
              AND  t_invalidated_at IS NULL
            """
        )

        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    sql,
                    {"tid": ctx.tenant_id, "ids": edge_ids},
                )
                rows = result.mappings().all()
        except Exception:
            _log.warning(
                "blast_radius: edge batch-fetch failed; returning stub EdgeRefs",
                extra={"tenant_id": str(ctx.tenant_id)},
                exc_info=True,
            )
            return []

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
            if row["tenant_id"] == ctx.tenant_id  # defense-in-depth
        ]
