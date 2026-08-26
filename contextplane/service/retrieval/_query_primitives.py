"""Shared plumbing used by more than one retrieval concern.

Splitting ``retrieval.py`` into search / graph_traversal / listing modules would
otherwise force a choice between putting shared logic in one of those files (so
the other imports across a concern boundary it doesn't otherwise cross) or
writing it twice and letting the two copies quietly drift apart. This module is
the third option: a leaf with no imports back into the package, so every
concern module can depend on it without a cycle.

``temporal_sql_fragments`` compiles the bi-temporal WHERE-clause fragment every
concern filters rows by (current-truth, or as-of a caller-supplied instant).
Search's three arms and graph traversal's version-predicate lookups each build
one per query; a second implementation would risk the two disagreeing about
what "current" means.

``_GRAPH_EDGE_TYPES`` is the edge-relationship set search's graph arm and the
forward-dependency endpoint both restrict traversal to. Two copies would let
"what counts as a dependency" drift between the two without either author
noticing.

``_RetrievalState`` declares the instance attributes every concern's methods
read off ``self`` — session factory, clock, embedder, the optional
VisibilityService, and the embedding LRU cache/lock — and carries
``_apply_visibility``, the one cross-tenant chokepoint every read path funnels
through before returning rows to a caller. Declaring them once here is what
lets each concern module define its slice of ``RetrievalService``'s methods
without re-declaring the same six attributes for mypy's benefit; the class is
never instantiated on its own — ``RetrievalService.__init__`` is the only place
that assigns them. Every concern mixin inherits from this class, directly or
transitively: ``search.py`` and ``listing.py`` inherit it directly; graph
traversal's three mixins (``graph_cte.py``, ``graph_traversal.py``,
``graph_closure_cache.py``) form their own chain on top of it, since those
three genuinely depend on each other rather than merely sitting next to
each other.

Nothing here is meant to be imported from outside this package. A caller
reaching into ``_query_primitives`` directly instead of through
``RetrievalService`` has stepped around the facade the split exists to keep
thin.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from typing import Any

from cachetools import LRUCache  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.governance.temporal import build_as_of_filter
from contextplane.service.governance.visibility import VisibilityService
from contextplane.types import Clock, Embedder, TemporalFilter, TenantContext

# Permitted edge relationship types for the search graph arm and for
# get_dependencies (the forward-dependency endpoint). Both answer "what does
# this depend on" over the same three relationship kinds.
_GRAPH_EDGE_TYPES: tuple[str, ...] = ("depends_on", "integrates_with", "event_source")


def any_term_tsquery(bind: str = "query") -> str:
    """SQL for a tsquery matching *any* of a user's terms, not all of them.

    `plainto_tsquery` conjoins. That is the right default for a keyword box and
    the wrong one everywhere this product puts a prompt box, because a question
    carries words the corpus does not: `Who owns salt design system?` parses to
    `'own' & 'salt' & 'design' & 'system'` and requires one row to contain all
    four. Measured on the development catalog that returned 0 facts where the
    disjunction returned 15, and deleting the single word "owns" was enough to
    make the same question match.

    Rewriting the parsed query's operators, rather than parsing the raw string a
    second time, is what keeps this honest: tokenising, stemming and stopword
    removal have already happened, so the disjunction covers exactly the terms
    the conjunction did. A separately-built OR query would be a second lexer,
    and the two would eventually disagree about what the query's terms are.

    Shared rather than written twice for the reason `fuse_hybrid_arms` is shared:
    entity search and claim retrieval both have a lexical arm, and a caller
    comparing an entity result with a claim result should not be comparing
    matches produced by different parsing.

    Callers keep binding the raw query text to ``bind``; only the operator
    changes. Relevance is unaffected by the widening as long as the caller ranks
    with `ts_rank`/`ts_rank_cd`, which score how much of the query a row covers.
    """
    return f"replace(plainto_tsquery('english', CAST(:{bind} AS TEXT))::text, '&', '|')::tsquery"


def temporal_sql_fragments(
    temporal_filter: TemporalFilter,
    now: datetime.datetime,
    table_alias: str = "",
) -> tuple[str, dict[str, Any]]:
    """Build SQL WHERE fragment + params for a bi-temporal filter.

    Fragments do not start with AND; callers add connectives.
    Columns are prefixed with table_alias if provided.
    """
    prefix = f"{table_alias}." if table_alias else ""
    params: dict[str, Any] = {}
    clauses: list[str] = []

    if temporal_filter.as_of is not None:
        as_of = temporal_filter.as_of
        spec = build_as_of_filter(as_of)
        clauses.append(f"{prefix}t_valid_from <= :tf_valid_from")
        params["tf_valid_from"] = spec["t_valid_from"][1]
        clauses.append(f"({prefix}t_valid_to IS NULL OR {prefix}t_valid_to > :tf_valid_to)")
        params["tf_valid_to"] = spec["t_valid_to"][1]
        clauses.append(f"({prefix}t_invalidated_at IS NULL OR {prefix}t_invalidated_at > :tf_invalidated_at)")
        params["tf_invalidated_at"] = spec["t_invalidated_at"][1]
    else:
        # t_invalidated_at IS NULL
        clauses.append(f"{prefix}t_invalidated_at IS NULL")
        # t_valid_to IS NULL OR t_valid_to > now
        clauses.append(f"({prefix}t_valid_to IS NULL OR {prefix}t_valid_to > :tf_now)")
        params["tf_now"] = now

    return " AND ".join(clauses), params


class _RetrievalState:
    """Instance attributes every retrieval concern's methods read off ``self``.

    Not instantiated on its own. ``RetrievalService.__init__`` (in
    ``retrieval/__init__.py``) is the only place that assigns these; every
    concern mixin (``search.py``, ``listing.py``, and — at the bottom of its
    own chain — ``graph_cte.py``) inherits from this class so its methods
    type-check without redeclaring the same six attributes.
    """

    _session_factory: async_sessionmaker[AsyncSession]
    _clock: Clock
    _embedder: Embedder
    # VisibilityService is the cross-tenant chokepoint. When wired,
    # `_apply_visibility` delegates to it for private/tenant-shared/public
    # enforcement. When None (unit-test paths that don't inject it),
    # `_apply_visibility` falls back to same-tenant filtering at fetch
    # time — a strict subset of cross-tenant filtering, so still secure.
    _visibility: VisibilityService | None
    _embed_cache: LRUCache[str, list[float]]
    # Guards the cache-miss check + encode + write sequence so concurrent
    # coroutines on the same key don't call the embedder more than once.
    # Cache hits release the lock immediately; the contention cost is
    # negligible compared to a single encode call.
    _embed_lock: asyncio.Lock

    async def _apply_visibility(
        self,
        ctx: TenantContext,
        entity_ids: list[uuid.UUID] | set[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Filter *entity_ids* through VisibilityService when available.

        Returns the subset of *entity_ids* visible to ``ctx.tenant_id``
        (private / tenant-shared / public). When no VisibilityService is
        injected, returns the full set unchanged — the caller's downstream
        entity fetch then applies a same-tenant SQL filter, which is a strict
        subset of cross-tenant filtering and remains secure.

        The one cross-tenant chokepoint every retrieval concern (search
        fusion, reverse traversal, blast-radius cache hydration) calls
        before returning rows to a caller.
        """
        if not entity_ids:
            return set()
        ids_list = list(entity_ids)
        if self._visibility is not None:
            visible = await self._visibility.filter_entities(ctx, ids_list)
            return set(visible)
        return set(ids_list)
