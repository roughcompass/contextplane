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
  - tsvector @@ a disjunction of the query's terms, on facts.ts_vector (GIN
    index). Any term matches; `ts_rank_cd` over both the disjunction and the
    conjunction is what orders them, so a row carrying the whole query still
    outranks one carrying part of it. See `any_term_tsquery`.

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
from collections.abc import Sequence
from typing import Any, Final, TypeVar

from sqlalchemy import RowMapping, text

from contextplane.embedding.stub import STUB_MODEL_VERSION
from contextplane.embedding.targets import TARGET_FACT
from contextplane.profile.scoring import resolve_weights
from contextplane.service.retrieval._query_primitives import (
    _GRAPH_EDGE_TYPES,
    _RetrievalState,
    any_term_tsquery,
    temporal_sql_fragments,
)
from contextplane.service.retrieval.fusion import fuse_hybrid_arms
from contextplane.types import EntityRef, FactRef, SearchResult, TemporalFilter, TenantContext

_log = logging.getLogger(__name__)

# Search-time graph hop limit (separate from get_dependencies's cap).
_SEARCH_GRAPH_DEPTH = 2

# How many name-matching entities may seed the graph arm's recursive expansion.
# Seeds cost more than rows: each one expands two hops. The bound is deliberately
# well above a realistic seed set rather than tuned to one — on the development
# catalog the broadest single-word query seeds 41 — so it acts as a ceiling on
# the pathological case (a query term shared by most of the catalog) and not as
# a relevance cut on ordinary ones. `_GRAPH_SEED_RANK_FLOOR` is what actually
# decides relevance.
_GRAPH_SEED_LIMIT = 50

# How good a name match has to be, relative to the best name match the same query
# found, to be worth expanding from.
#
# A count-based cut cannot tell the two cases apart, and they need opposite
# answers. `Who owns salt design system?` matches 41 entity names, but one of
# them carries every term of the query and the other 40 carry only "salt";
# expanding from all 41 returns `salt-drawer` as a top answer to a question about
# ownership of the design system, which is the "results unrelated to the prompt"
# complaint in its exact form. `salt` also matches 41, and there every one of
# them is equally and genuinely what was asked about.
#
# Measured on the development catalog, the discriminating query scores its best
# seed at 0.30 and every distractor at 0.10, while the non-discriminating one
# scores every match identically. A relative floor therefore cuts hard exactly
# when the query distinguishes and keeps everything exactly when it does not,
# without either case being special-cased. One half is the midpoint of that
# measured gap rather than a tuned value; the rule is what matters, and it
# survives a catalog whose ranks look nothing like this one's.
_GRAPH_SEED_RANK_FLOOR = 0.5

# Both the lexical arm and the graph arm's seed match on any of the query's
# terms. Shared with claim retrieval so a prompt is parsed one way across the
# product — see `any_term_tsquery` for why the conjunction was wrong.
_ANY_TERM = any_term_tsquery("query")

_T = TypeVar("_T")


def _cache_key(query_text: str, model_version: str) -> str:
    """SHA-256 digest of query_text + model_version used as LRU cache key."""
    payload = (query_text + model_version).encode()
    return hashlib.sha256(payload).hexdigest()


#: The governed magnitude this module fuses with. Its value, and the reason it
#: holds that value, live in `contextplane/ranking_registry.json`.
_FUSION_MODEL_ID: Final = "entity-search-hybrid-fusion@1"


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
        Weights are this tenant's, not the deployment's. `resolve_weights` returns
        the tenant's bound override where one exists and the core default
        otherwise, which is most tenants -- reading `ranking.weights` here
        instead would serve every tenant the core value and give a tenant whose
        override activated no way to tell it had not taken effect.
        Dedup by entity_id (max fused score wins). Final tenant assertion applied.
        """
        async with self._session_factory() as session:
            resolved = await resolve_weights(session, tenant_id=ctx.tenant_id, model_id=_FUSION_MODEL_ID)
        base_weights = resolved.value

        # An embedder that cannot rank is *absent*, not a quiet contributor.
        #
        # `StubEmbedder` returns zero vectors and says so: "every distance is
        # identical, so the ranking is arbitrary". Arbitrary is not neutral. This
        # arm carries the largest of the three weights, so on any deployment
        # running the stub — every `make dev-up`, every smoke stack — half the
        # fused score was being assigned to an arbitrary handful of rows.
        #
        # Measured on the fifty-question corpus, changing nothing else:
        # precision@1 0.66-0.74 with the arm present, 0.98 with it absent, and
        # the run-to-run variance goes with it. The corpus was reporting a
        # different number each run because an arm with no signal was choosing
        # which rows it happened to return.
        #
        # `fuse_hybrid_arms` already models this: an arm left out of both maps is
        # gone rather than empty, and `redistribute_weights` gives its share to
        # the arms that can still answer. Dropping it from `arms` alone would
        # leave it holding its weight while contributing nothing, which lowers
        # every score by omission and is the case that docstring warns about.
        arms: dict[str, Any] = {
            "lexical": self._lexical_arm(ctx, q, top_k, temporal_filter, entity_type),
            "graph": self._graph_arm(ctx, q, top_k, temporal_filter, entity_type),
        }
        weights = dict(base_weights)
        if self._embedder.model_version == STUB_MODEL_VERSION:
            weights.pop("semantic", None)
        else:
            arms["semantic"] = self._semantic_arm(ctx, q, top_k, temporal_filter, entity_type)

        fused, _failed_arms = await fuse_hybrid_arms(
            arms=arms,
            weights=weights,
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
                        fused_rank_score=fused_row.score,
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
                        fused_rank_score=fused_row.score,
                        retrieval_arms=fused_row.arm_scores,
                    )
                )

        # Entity id breaks a tied fused score. Python's sort is stable, so without
        # it the final order inherits whichever arm happened to contribute the row
        # first -- a detail that is not itself pinned. The arms below are ordered
        # deterministically; this is what carries that property through the fusion
        # to the answer a receipt records.
        results.sort(key=lambda r: (-r.fused_rank_score, r.entity.name.lower(), r.entity.entity_id))
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
            -- Tiebroken, and not as decoration. Distances tie constantly: an
            -- embedder returning zero vectors makes every distance identical, and
            -- a real model still ties exactly on duplicated text. Without a second
            -- key Postgres may return a different subset for the same query
            -- against unchanged data, so the LIMIT keeps different rows on
            -- different runs and a receipt stops being reproducible -- the one
            -- property a receipt must have. The listing and traversal queries in
            -- this package already tiebreak; these two did not.
            --
            -- Name before id. Both give a total order, but an id is a random
            -- UUID: the order it produces is fixed for one dataset and arbitrary
            -- across any two, so a measurement over a reseeded corpus would still
            -- move. `lower(name)` is unique per tenant by index and is a property
            -- of the data rather than of when the row happened to be created.
            ORDER BY emb.vector <=> CAST(:vec AS vector), lower(ent.name), f.fact_id
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
        """Full-text search over facts, matching any query term and ranking by coverage.

        **Any term, not every term, and the difference is the whole arm.**
        `plainto_tsquery` conjoins: `Who owns salt design system?` becomes
        `'own' & 'salt' & 'design' & 'system'`, which requires one fact to
        contain all four. Against a real corpus that matches nothing — measured,
        not supposed: on the dev seed the conjunction returns 0 facts and the
        disjunction returns 15. Dropping the word "owns" from the same question
        made it match. A retrieval arm behind a prompt box cannot require the
        user to phrase a keyword query, because the box asks for a question.

        **Coverage still decides the order, so widening the match does not
        flatten the ranking.** Two ranks are computed. `rank_all` scores the
        conjunction, so a fact carrying every term sorts above one carrying some;
        `rank_any` scores the disjunction and orders the rest by how much of the
        query they cover and how close together it appears. A fact that merely
        contains "system" ranks last rather than being excluded, which is the
        right answer for an arm whose output is fused with two others and cut to
        `top_k`.

        **The disjunction is derived from the conjunction rather than parsed
        again.** `plainto_tsquery` already did the tokenising, stemming and
        stopword removal; rewriting its operators keeps one parse and one
        normalisation. Building a second query from the raw string would be a
        second lexer that drifts from the first.

        A query of only stopwords yields an empty tsquery, which matches nothing.
        That is unchanged and correct: there is no term to search for, and
        returning the whole corpus would be worse than returning none of it.
        """
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
            WITH parsed AS (
                -- One parse, two shapes. The disjunction is the conjunction with
                -- its operators rewritten, so tokenising, stemming and stopword
                -- removal happen exactly once and the two can never disagree
                -- about what the query's terms are.
                SELECT
                    plainto_tsquery('english', CAST(:query AS TEXT)) AS all_terms,
                    {_ANY_TERM} AS any_term
            )
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
                ts_rank_cd(f.ts_vector, parsed.all_terms) AS rank_all,
                ts_rank_cd(f.ts_vector, parsed.any_term) AS rank_any
            FROM facts f
            JOIN entities ent ON ent.entity_id = f.entity_id
            CROSS JOIN parsed
            WHERE f.tenant_id = :tid
              AND ent.tenant_id = :tid
              AND ent.is_active = TRUE
              AND f.ts_vector @@ parsed.any_term
              {entity_filter}
              AND {tf_sql}
            -- Every term first, then coverage of any. A fact carrying the whole
            -- query outranks one carrying part of it, and the partial matches are
            -- ordered by how much they cover rather than being discarded.
            --
            -- Tiebroken for the same reason as the semantic arm above:
            -- `ts_rank_cd` returns equal ranks routinely across a corpus of short
            -- similar documents, and an untiebroken LIMIT then selects a
            -- different set on each run.
            ORDER BY rank_all DESC, rank_any DESC, lower(ent.name), f.fact_id
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

        Starting from entities whose names match the query's terms, expand
        outward via graph edges up to `_SEARCH_GRAPH_DEPTH` hops. Returns
        entity-level rows for the neighbour entities.

        **The seed matches terms, not the query as a substring.** It previously
        ran `ent.name ILIKE '%' || query || '%'`, which asks whether some entity
        is *named* the thing the user typed. `Who owns salt design system?`
        seeded nothing, and so did `salt design system`, because no entity is
        called that — the catalog spells it `salt-design-system`. An arm that
        contributes only when the user already knows an entity's exact name
        contributes nothing to the case it exists for, which is the user who does
        not. Matching `to_tsvector(name)` against the same disjunction the
        lexical arm builds seeds `salt-design-system` first for all three
        spellings, because Postgres tokenises the hyphenated name into its parts.

        **Seeds are ranked and capped.** An unbounded seed set was tolerable
        while the match was a rare exact-substring hit; matching any term makes a
        common word like "system" a plausible seed for much of the catalog, and
        each seed then pays for a recursive expansion. `ts_rank_cd` puts the
        entity that matches the most of the query first, and `_GRAPH_SEED_LIMIT`
        bounds what expands.
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
            "query": q,
            "edge_types": list(_GRAPH_EDGE_TYPES),
            "limit": top_k,
            "seed_limit": _GRAPH_SEED_LIMIT,
            "seed_rank_floor": _GRAPH_SEED_RANK_FLOOR,
            **tf_fact_params,
            **tf_edge_params_renamed,
        }
        if entity_type is not None:
            entity_filter = "AND ent.entity_type = :entity_type"
            params["entity_type"] = entity_type

        sql = text(
            f"""
            WITH RECURSIVE graph_cte AS (
                -- Seed: entities whose names carry any of the query's terms,
                -- best coverage first, capped before anything expands.
                SELECT
                    ent.entity_id,
                    ent.tenant_id,
                    ent.entity_type,
                    ent.name,
                    ent.external_id,
                    ent.is_active,
                    ent.created_at,
                    0 AS depth_counter,
                    ent.seed_rank
                FROM (
                    -- Wrapped rather than filtered in place: the floor compares
                    -- each candidate against the best candidate, so the whole
                    -- matched set has to be ranked before any of it can be cut.
                    -- Postgres also rejects ORDER BY/LIMIT directly in a
                    -- recursive CTE's non-recursive term.
                    SELECT
                        scored.entity_id,
                        scored.tenant_id,
                        scored.entity_type,
                        scored.name,
                        scored.external_id,
                        scored.is_active,
                        scored.created_at,
                        scored.seed_rank
                    FROM (
                        SELECT
                            ent.entity_id,
                            ent.tenant_id,
                            ent.entity_type,
                            ent.name,
                            ent.external_id,
                            ent.is_active,
                            ent.created_at,
                            ts_rank_cd(
                                to_tsvector('english', ent.name),
                                {_ANY_TERM}
                            ) AS seed_rank,
                            MAX(
                                ts_rank_cd(
                                    to_tsvector('english', ent.name),
                                    {_ANY_TERM}
                                )
                            ) OVER () AS best_rank
                        FROM entities ent
                        WHERE ent.tenant_id = :tid
                          AND ent.is_active = TRUE
                          AND to_tsvector('english', ent.name) @@
                              {_ANY_TERM}
                          {entity_filter}
                    ) scored
                    WHERE scored.seed_rank >= scored.best_rank * :seed_rank_floor
                    ORDER BY scored.seed_rank DESC, lower(scored.name), scored.entity_id
                    LIMIT :seed_limit
                ) ent

                UNION

                SELECT
                    ent2.entity_id,
                    ent2.tenant_id,
                    ent2.entity_type,
                    ent2.name,
                    ent2.external_id,
                    ent2.is_active,
                    ent2.created_at,
                    graph_cte.depth_counter + 1,
                    -- A neighbour inherits the anchor it was reached from, so
                    -- the outer ordering can put the neighbours of the best
                    -- match above the neighbours of a weak one.
                    graph_cte.seed_rank
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
            SELECT * FROM (
            SELECT DISTINCT ON (g.entity_id)
                g.depth_counter,
                g.seed_rank,
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
            -- DISTINCT ON dictates this ORDER BY: its leading expression must
            -- be the distinct key, so the only choice left here is which of an
            -- entity's duplicate rows survives, and the shallowest should.
            --
            -- Total, not merely shallowest-first. An entity reachable from two
            -- seeds at the same depth has two rows carrying different
            -- `seed_rank`s, and an entity with several facts has one row per
            -- fact; under an ordering that does not separate them Postgres
            -- keeps whichever it happens to reach first, and the arm returns a
            -- different ranking on each run from identical data. Measured: with
            -- only `(entity_id, depth_counter)` this suite's precision@1 moved
            -- between 0.50 and 0.74 across runs. The strongest anchor wins, and
            -- `fact_id` makes the rest of the order reproducible.
            ORDER BY g.entity_id, g.depth_counter, g.seed_rank DESC, f.fact_id
            ) deduped
            -- The ordering that decides what the arm actually returns, applied
            -- after DISTINCT ON has had the ordering it requires. Ordering by
            -- `entity_id` was the whole result order before, which meant a UUID
            -- chose the top ten: the arm returned an arbitrary slice of
            -- everything it could reach and called it a ranking. Direct name
            -- matches come first, then how well the anchor matched, so a
            -- neighbour of the best match outranks a neighbour of a weak one.
            ORDER BY
                deduped.depth_counter,
                deduped.seed_rank DESC,
                lower(deduped.name),
                deduped.entity_id
            LIMIT :limit
            """  # noqa: S608 - every interpolated fragment is module-level SQL text (`_ANY_TERM`, the temporal fragments, the entity-type clause) built from fixed strings and `:param` binds; the query text is bound, never formatted in
        )
        params["search_depth"] = _SEARCH_GRAPH_DEPTH

        async with self._session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.mappings().all()

        return self._group_rows_by_entity(rows, top_k)

    @staticmethod
    def _group_rows_by_entity(
        rows: Sequence[RowMapping],
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
