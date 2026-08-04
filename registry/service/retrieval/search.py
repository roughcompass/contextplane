"""Hybrid search — semantic + lexical + graph arms, fused into one ranking.

Architecture:
- Three arms run concurrently via ``fuse_hybrid_arms`` (``asyncio.gather``,
  ``return_exceptions=True``).
- A failing arm is logged at WARN and excluded from fusion; the call never
  raises to the caller.
- Fusion uses rank-based decay (1/rank) within each arm, then linearly
  combines with weights 0.5 semantic + 0.3 lexical + 0.2 graph. If an arm is
  absent (an exception, not an empty result) its weight is redistributed
  proportionally across surviving arms.
- Dedup by entity_id: max fused score per entity wins.
- Final tenant assertion: any row whose tenant_id != ctx.tenant_id is
  silently dropped post-fusion (defense-in-depth on top of query filters).

Semantic arm:
  - Embedding is LRU-cached by sha256(query_text + model_version).
  - Must run inside an explicit transaction so SET LOCAL hnsw.ef_search has
    effect (SET LOCAL is a no-op outside a transaction).
  - Over-fetches top_k * 4 rows and returns the nearest top_k after SET LOCAL.

Lexical arm:
  - tsvector @@ plainto_tsquery on facts.ts_vector (GIN index).
  - Ranked via ts_rank_cd.

Graph arm:
  - Recursive CTE on edges, depth <= 2 (hardcoded for search), edge types
    depends_on | integrates_with | event_source.

``fuse_hybrid_arms`` is the orchestration primitive underneath ``search``: run
N ranked arms concurrently, redistribute a failed arm's weight across the
survivors, fuse by rank decay, dedup by caller-supplied key. It is public
because claim retrieval fuses two arms the same way, and a second
implementation would drift from this one — a caller comparing a capability
result with a claim result would then be comparing numbers produced by
different arithmetic.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from sqlalchemy import text

from registry.embedding.targets import TARGET_FACT
from registry.service.retrieval._query_primitives import (
    _GRAPH_EDGE_TYPES,
    _RetrievalState,
    temporal_sql_fragments,
)
from registry.types import EntityRef, FactRef, SearchResult, TemporalFilter, TenantContext

_log = logging.getLogger(__name__)

# Search-time graph hop limit (separate from get_dependencies's cap).
_SEARCH_GRAPH_DEPTH = 2

_T = TypeVar("_T")


def _cache_key(query_text: str, model_version: str) -> str:
    """SHA-256 digest of query_text + model_version used as LRU cache key."""
    payload = (query_text + model_version).encode()
    return hashlib.sha256(payload).hexdigest()


def rank_decay_weights(n: int) -> list[float]:
    """Rank-based decay: weight for rank r (0-based) = 1/(r+1).

    Public because claim retrieval fuses through it too. A second
    implementation would drift from this one, and then a caller comparing a
    capability result with a claim result would be comparing numbers produced
    by different arithmetic.

    Takes the arm's row *count*, not the rows or their scores — the decay
    curve depends only on how many ranked positions there are, never on what
    occupies them, so a caller cannot pass the wrong data by passing the
    right length. Returns ``[]`` for ``n <= 0``.
    """
    return [1.0 / (rank + 1) for rank in range(n)]


def redistribute_weights(
    weights: dict[str, float],
    failed_arms: set[str],
) -> dict[str, float]:
    """Return new weights with failed arms removed and remaining scaled to sum=1.

    Public for the same reason as `rank_decay_weights`: how a missing arm is
    handled is part of what a fused score means, so every fusion in the
    product handles it the same way.
    """
    surviving = {arm: w for arm, w in weights.items() if arm not in failed_arms}
    total = sum(surviving.values())
    if total == 0.0:
        return {}
    return {arm: w / total for arm, w in surviving.items()}


@dataclass(frozen=True)
class FusedRow(Generic[_T]):
    """One fused result: the winning arm's row plus its accumulated score.

    ``row`` is whatever the first arm to introduce this key returned. Later
    arms that rank the same key only add to ``score`` and ``arm_scores`` —
    they never replace ``row`` — because the identity of a result does not
    depend on which arm found it first, only on being found.
    """

    row: _T
    score: float
    arm_scores: dict[str, float]


async def fuse_hybrid_arms(
    arms: Mapping[str, Awaitable[Sequence[_T]]],
    weights: Mapping[str, float],
    key: Callable[[_T], Hashable],
) -> tuple[dict[Hashable, FusedRow[_T]], set[str]]:
    """Run N ranked retrieval arms concurrently and fuse them into one ranking.

    This is the orchestration ``search`` runs its three arms through, pulled
    out as a public primitive so a second hybrid ranker can reuse the exact
    arithmetic instead of reimplementing it and drifting from it.

    Parameters
    ----------
    arms:
        One already-invoked awaitable per named arm (a coroutine, a task —
        anything ``asyncio.gather`` accepts). Each arm owns its own per-arm
        over-fetch: fusion re-ranks across arms, so a row an individual arm
        placed fourth can finish first once weights and the other arms'
        contributions are added in, and an arm that only returned the
        caller's final desired count would already have discarded it before
        fusion had a chance to promote it. This function does not truncate;
        callers slice the fused, sorted result to whatever size they need.
    weights:
        Base per-arm weight, expected to sum to 1.0 by convention (not
        enforced — weights that don't sum to 1 produce scores that don't
        either).
    key:
        Extracts the dedup identity from one row of one arm's results. Two
        arms returning a row for the same key contribute additively to that
        key's score.

    Arm failure vs. an empty arm
    -----------------------------
    An arm whose awaitable raises is excluded from fusion and its weight is
    redistributed proportionally across the surviving arms (see
    ``redistribute_weights``) — a missing arm should not silently lower every
    score by omission, or the ranking would look like every result got worse
    rather than like one signal went away. An arm that raises nothing but
    returns an empty list is treated differently: it keeps its weight slot
    and simply contributes nothing, because an empty result is a legitimate
    answer ("nothing matched"), not a failure.

    Returns
    -------
    A ``(fused, failed_arms)`` pair. ``fused`` maps each row's dedup key to
    its winning row, accumulated score, and per-arm score breakdown. Rows are
    unordered; callers sort by ``.score`` themselves so they can apply their
    own tie-break.
    """
    names = list(arms.keys())
    raw_results = await asyncio.gather(*arms.values(), return_exceptions=True)

    arm_rows: dict[str, Sequence[_T]] = {}
    failed_arms: set[str] = set()
    for name, result in zip(names, raw_results, strict=True):
        if isinstance(result, BaseException):
            _log.warning(
                "retrieval arm failed — excluding from fusion",
                extra={"arm": name, "error": str(result)},
            )
            failed_arms.add(name)
        else:
            arm_rows[name] = result

    effective_weights = redistribute_weights(dict(weights), failed_arms)

    fused: dict[Hashable, FusedRow[_T]] = {}
    for arm_name, weight in effective_weights.items():
        rows = arm_rows.get(arm_name, [])
        if not rows:
            continue
        rank_scores = rank_decay_weights(len(rows))
        for rank, row in enumerate(rows):
            row_key = key(row)
            contribution = weight * rank_scores[rank]
            existing = fused.get(row_key)
            if existing is None:
                fused[row_key] = FusedRow(row=row, score=contribution, arm_scores={arm_name: contribution})
            else:
                new_arm_scores = dict(existing.arm_scores)
                new_arm_scores[arm_name] = new_arm_scores.get(arm_name, 0.0) + contribution
                fused[row_key] = FusedRow(
                    row=existing.row,
                    score=existing.score + contribution,
                    arm_scores=new_arm_scores,
                )

    return fused, failed_arms


class _SearchMethods(_RetrievalState):
    """``RetrievalService.search`` and the arms/helpers it alone depends on."""

    async def _encode_query(self, query_text: str) -> list[float]:
        """Return embedding vector for query_text, using LRU cache.

        The lock ensures that concurrent coroutines waiting on the same key
        only call the embedder once — the second caller finds the result
        already written when it acquires the lock.
        """
        key = _cache_key(query_text, self._embedder.model_version)
        async with self._embed_lock:
            cached = self._embed_cache.get(key)
            if cached is not None:
                return cached  # type: ignore[no-any-return]
            # Off the event loop: every embedder blocks for the length of an
            # inference pass or a network round trip, and doing that inline
            # stalls every other request on this worker, not just this one.
            vec = await asyncio.to_thread(self._embedder.encode, [query_text])
            result: list[float] = vec[0].tolist()
            self._embed_cache[key] = result
            return result

    async def search(
        self,
        ctx: TenantContext,
        q: str,
        top_k: int,
        temporal_filter: TemporalFilter,
        entity_type: str | None = None,
        lifecycle: str | None = None,
    ) -> list[SearchResult]:
        """Three-arm hybrid search.

        Arms run concurrently; a failing arm is excluded without raising.
        Weights: 0.5 semantic + 0.3 lexical + 0.2 graph.
        Dedup by entity_id (max fused score wins). Final tenant assertion applied.
        """
        base_weights: dict[str, float] = {
            "semantic": 0.5,
            "lexical": 0.3,
            "graph": 0.2,
        }

        fused, _failed_arms = await fuse_hybrid_arms(
            arms={
                "semantic": self._semantic_arm(ctx, q, top_k, temporal_filter, entity_type),
                "lexical": self._lexical_arm(ctx, q, top_k, temporal_filter, entity_type),
                "graph": self._graph_arm(ctx, q, top_k, temporal_filter, entity_type),
            },
            weights=base_weights,
            key=lambda row: row[0],
        )

        # Cross-tenant chokepoint: filter fused entity IDs through VisibilityService.
        # When no VisibilityService is wired (unit-test paths), fall back to the
        # strict same-tenant defense-in-depth assertion.
        results: list[SearchResult] = []
        if self._visibility is not None:
            visible_ids = await self._apply_visibility(ctx, list(fused.keys()))  # type: ignore[arg-type]
            for entity_id, fused_row in fused.items():
                if entity_id not in visible_ids:
                    continue
                _, entity_ref, facts = fused_row.row
                results.append(
                    SearchResult(
                        entity=entity_ref,
                        matching_facts=facts,
                        score=fused_row.score,
                        retrieval_arms=fused_row.arm_scores,
                    )
                )
        else:
            for _entity_id, fused_row in fused.items():
                _, entity_ref, facts = fused_row.row
                if entity_ref.tenant_id != ctx.tenant_id:
                    _log.warning(
                        "post-fusion tenant assertion failed — dropping row",
                        extra={
                            "entity_id": str(_entity_id),
                            "tenant_id": str(entity_ref.tenant_id),
                        },
                    )
                    continue
                results.append(
                    SearchResult(
                        entity=entity_ref,
                        matching_facts=facts,
                        score=fused_row.score,
                        retrieval_arms=fused_row.arm_scores,
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _semantic_arm(
        self,
        ctx: TenantContext,
        q: str,
        top_k: int,
        temporal_filter: TemporalFilter,
        entity_type: str | None,
    ) -> list[tuple[uuid.UUID, EntityRef, list[FactRef]]]:
        """ANN search via pgvector HNSW index.

        SET LOCAL hnsw.ef_search must run inside the same transaction as the
        SELECT (SET LOCAL is a no-op outside a transaction).

        Restricted to rows written by the embedder now in use. One index can hold
        vectors from several models at once — a reindex adds rows under a new
        model id without removing the old ones — and distances between different
        embedding spaces are not comparable, so an unfiltered ORDER BY would
        interleave rankings from two unrelated coordinate systems.
        """
        query_vec = await self._encode_query(q)
        ef_search = top_k * 4
        fetch_k = top_k * 4  # over-fetch before dedup

        now = self._clock.now()
        tf_sql, tf_params = temporal_sql_fragments(temporal_filter, now, table_alias="f")

        entity_filter = ""
        params: dict[str, Any] = {
            "tid": ctx.tenant_id,
            # pgvector over asyncpg takes a string literal, not a Python list —
            # the same encoding the drain applies on the write side.
            "vec": "[" + ",".join(str(component) for component in query_vec) + "]",
            "fetch_k": fetch_k,
            "ef_search": ef_search,
            "model_id": self._embedder.model_version,
            "target_type": TARGET_FACT,
            **tf_params,
        }
        if entity_type is not None:
            entity_filter = "AND ent.entity_type = :entity_type"
            params["entity_type"] = entity_type

        sql = text(
            f"""
            SELECT
                emb.embedding_id,
                emb.target_id AS fact_id,
                emb.tenant_id AS emb_tenant_id,
                f.entity_id,
                f.tenant_id AS fact_tenant_id,
                f.category,
                f.body,
                f.is_authoritative,
                f.is_authoritative_superseded,
                f.sync_run_id,
                f.t_valid_from,
                f.t_valid_to,
                f.t_ingested_at,
                f.t_invalidated_at,
                ent.entity_id AS ent_entity_id,
                ent.tenant_id AS ent_tenant_id,
                ent.entity_type,
                ent.name,
                ent.external_id,
                ent.is_active,
                ent.created_at,
                (emb.vector <=> CAST(:vec AS vector)) AS distance
            FROM embeddings emb
            JOIN facts f ON f.fact_id = emb.target_id
            JOIN entities ent ON ent.entity_id = f.entity_id
            WHERE emb.tenant_id = :tid
              AND f.tenant_id = :tid
              AND ent.tenant_id = :tid
              AND ent.is_active = TRUE
              AND emb.model_id = :model_id
              -- The shared index holds vectors for more than one kind of row. Filtered
              -- explicitly rather than left to the inner join below: a join that happens
              -- to exclude the others is a control nobody can find, nobody can test, and
              -- nobody can break loudly -- it survives only until someone widens it.
              AND emb.target_type = :target_type
              {entity_filter}
              AND {tf_sql}
            ORDER BY emb.vector <=> CAST(:vec AS vector)
            LIMIT :fetch_k
            """
        )

        # Must run inside an explicit transaction so the setting is transaction-local.
        #
        # set_config(..., is_local => true) rather than `SET LOCAL`: SET is
        # utility syntax and takes no bind parameters, so `SET LOCAL x = :v`
        # reaches the server as `SET LOCAL x = $1` and Postgres rejects it
        # outright. set_config is an ordinary function call, so the value binds
        # normally and nothing has to be interpolated into SQL text.
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('hnsw.ef_search', :v, true)"),
                    {"v": str(ef_search)},
                )
                result = await session.execute(sql, params)
                rows = result.mappings().all()

        return self._group_rows_by_entity(rows, top_k)

    async def _lexical_arm(
        self,
        ctx: TenantContext,
        q: str,
        top_k: int,
        temporal_filter: TemporalFilter,
        entity_type: str | None,
    ) -> list[tuple[uuid.UUID, EntityRef, list[FactRef]]]:
        """Full-text search via tsvector @@ plainto_tsquery, ranked by ts_rank_cd."""
        now = self._clock.now()
        tf_sql, tf_params = temporal_sql_fragments(temporal_filter, now, table_alias="f")

        entity_filter = ""
        params: dict[str, Any] = {
            "tid": ctx.tenant_id,
            "query": q,
            "limit": top_k,
            **tf_params,
        }
        if entity_type is not None:
            entity_filter = "AND ent.entity_type = :entity_type"
            params["entity_type"] = entity_type

        sql = text(
            f"""
            SELECT
                f.fact_id,
                f.entity_id,
                f.tenant_id AS fact_tenant_id,
                f.category,
                f.body,
                f.is_authoritative,
                f.is_authoritative_superseded,
                f.sync_run_id,
                f.t_valid_from,
                f.t_valid_to,
                f.t_ingested_at,
                f.t_invalidated_at,
                ent.entity_id AS ent_entity_id,
                ent.tenant_id AS ent_tenant_id,
                ent.entity_type,
                ent.name,
                ent.external_id,
                ent.is_active,
                ent.created_at,
                ts_rank_cd(f.ts_vector, plainto_tsquery('english', :query)) AS rank
            FROM facts f
            JOIN entities ent ON ent.entity_id = f.entity_id
            WHERE f.tenant_id = :tid
              AND ent.tenant_id = :tid
              AND ent.is_active = TRUE
              AND f.ts_vector @@ plainto_tsquery('english', :query)
              {entity_filter}
              AND {tf_sql}
            ORDER BY rank DESC
            LIMIT :limit
            """
        )

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return self._group_rows_by_entity(rows, top_k)

    async def _graph_arm(
        self,
        ctx: TenantContext,
        q: str,
        top_k: int,
        temporal_filter: TemporalFilter,
        entity_type: str | None,
    ) -> list[tuple[uuid.UUID, EntityRef, list[FactRef]]]:
        """Graph-neighbour expansion via recursive CTE.

        Starting from entities whose names match the query text (lexical match),
        expand outward via graph edges up to _SEARCH_GRAPH_DEPTH hops.
        Returns entity-level rows for the neighbour entities.
        """
        now = self._clock.now()
        tf_fact_sql, tf_fact_params = temporal_sql_fragments(temporal_filter, now, table_alias="f")
        tf_edge_sql, tf_edge_params = temporal_sql_fragments(temporal_filter, now, table_alias="e")

        # De-duplicate param keys from two temporal fragments by renaming one set.
        tf_edge_params_renamed = {f"edge_{k}": v for k, v in tf_edge_params.items()}
        tf_edge_sql_renamed = tf_edge_sql
        for k in tf_edge_params:
            tf_edge_sql_renamed = tf_edge_sql_renamed.replace(f":{k}", f":edge_{k}")

        entity_filter = ""
        params: dict[str, Any] = {
            "tid": ctx.tenant_id,
            "query": f"%{q}%",
            "edge_types": list(_GRAPH_EDGE_TYPES),
            "limit": top_k,
            **tf_fact_params,
            **tf_edge_params_renamed,
        }
        if entity_type is not None:
            entity_filter = "AND ent.entity_type = :entity_type"
            params["entity_type"] = entity_type

        sql = text(
            f"""
            WITH RECURSIVE graph_cte AS (
                -- Seed: entities matching query text
                SELECT
                    ent.entity_id,
                    ent.tenant_id,
                    ent.entity_type,
                    ent.name,
                    ent.external_id,
                    ent.is_active,
                    ent.created_at,
                    0 AS depth_counter
                FROM entities ent
                WHERE ent.tenant_id = :tid
                  AND ent.is_active = TRUE
                  AND ent.name ILIKE :query
                  {entity_filter}

                UNION

                SELECT
                    ent2.entity_id,
                    ent2.tenant_id,
                    ent2.entity_type,
                    ent2.name,
                    ent2.external_id,
                    ent2.is_active,
                    ent2.created_at,
                    graph_cte.depth_counter + 1
                FROM graph_cte
                JOIN edges e ON e.src_entity_id = graph_cte.entity_id
                JOIN entities ent2 ON ent2.entity_id = e.dst_entity_id
                WHERE e.tenant_id = :tid
                  AND ent2.tenant_id = :tid
                  AND ent2.is_active = TRUE
                  AND e.rel = ANY(:edge_types)
                  AND graph_cte.depth_counter < :search_depth
                  AND {tf_edge_sql_renamed}
            )
            SELECT DISTINCT ON (g.entity_id)
                g.entity_id,
                g.tenant_id AS ent_tenant_id,
                g.entity_type,
                g.name,
                g.external_id,
                g.is_active,
                g.created_at,
                f.fact_id,
                f.entity_id AS f_entity_id,
                f.tenant_id AS fact_tenant_id,
                f.category,
                f.body,
                f.is_authoritative,
                f.is_authoritative_superseded,
                f.sync_run_id,
                f.t_valid_from,
                f.t_valid_to,
                f.t_ingested_at,
                f.t_invalidated_at
            FROM graph_cte g
            LEFT JOIN facts f ON f.entity_id = g.entity_id
              AND f.tenant_id = :tid
              AND {tf_fact_sql}
            ORDER BY g.entity_id, g.depth_counter
            LIMIT :limit
            """
        )
        params["search_depth"] = _SEARCH_GRAPH_DEPTH

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return self._group_rows_by_entity(rows, top_k)

    @staticmethod
    def _group_rows_by_entity(
        rows: Any,
        top_k: int,
    ) -> list[tuple[uuid.UUID, EntityRef, list[FactRef]]]:
        """Group flat result rows into (entity_id, EntityRef, [FactRef]) tuples.

        Preserves original row order for ranking; deduplicates entity_id.
        """
        seen: dict[uuid.UUID, tuple[EntityRef, list[FactRef]]] = {}
        order: list[uuid.UUID] = []

        for row in rows:
            eid = row["entity_id"]
            if eid not in seen:
                entity_ref = EntityRef(
                    entity_id=eid,
                    tenant_id=row["ent_tenant_id"],
                    entity_type=row["entity_type"],
                    name=row["name"],
                    external_id=row["external_id"],
                    is_active=row["is_active"],
                    created_at=row["created_at"],
                )
                seen[eid] = (entity_ref, [])
                order.append(eid)

            # Attach fact if present (LEFT JOIN may return NULLs).
            if row.get("fact_id") is not None:
                fact_ref = FactRef(
                    fact_id=row["fact_id"],
                    tenant_id=row["fact_tenant_id"],
                    entity_id=eid,
                    category=row["category"],
                    body=row["body"],
                    is_authoritative=row["is_authoritative"],
                    is_authoritative_superseded=row["is_authoritative_superseded"],
                    sync_run_id=row["sync_run_id"],
                    t_valid_from=row["t_valid_from"],
                    t_valid_to=row["t_valid_to"],
                    t_ingested_at=row["t_ingested_at"],
                    t_invalidated_at=row["t_invalidated_at"],
                )
                seen[eid][1].append(fact_ref)

        return [(eid, seen[eid][0], seen[eid][1]) for eid in order[:top_k]]
