"""The retrieval subdomain: hybrid search, graph traversal, and capability listing.

Three read concerns live here, plus the write side that feeds the first of
them:

``search`` runs the three-arm hybrid ranker (semantic + lexical + graph) a
capability query answers through, and owns the embedding LRU cache that makes
repeated queries within a session cheap. ``graph_traversal`` owns the
recursive-CTE primitive both directions of dependency walk share (reverse
traversal and blast-radius), the version-predicate evaluation both apply, and
the forward dependency walk (``get_dependencies``) that predates the shared
primitive and still has its own query shape. ``listing`` is
``list_capabilities`` alone — keyset pagination over the entity table, with no
overlap with the other two. ``_query_primitives`` holds what the other three
genuinely share rather than each merely needing something similar: the
bi-temporal SQL fragment builder, and the cross-tenant visibility chokepoint
every read path calls before returning rows.

``embedding_index`` and ``embedding_drain`` are retrieval's write side — the
outbox enqueue/retract surface and the drain job that turns a queued request
into a row in ``embeddings``. They moved in alongside the read side because
the semantic arm cannot be understood apart from what populates the index it
reads; keeping them in a flat ``service/`` directory next to unrelated
modules made that connection something a reader had to already know rather
than something the layout showed.

Unlike this repo's other subdomain packages, ``RetrievalService`` is
re-exported from here (nothing else is). Every router, worker, and MCP tool
that touches retrieval imports ``RetrievalService`` by name from
``registry.service.retrieval`` — that import path is this package's public
contract, and rewriting it everywhere the day the internals moved would have
been a second, unrelated change bundled into a refactor that owed nobody one.
A future caller wanting ``rank_decay_weights`` or ``fuse_hybrid_arms``
imports ``registry.service.retrieval.search`` directly, the same way a
catalog caller imports ``registry.service.catalog.entity`` directly — nothing
below the facade is re-exported, and reaching for it through here is reaching
past the facade this split exists to keep thin.

``RetrievalService`` itself is not a facade that delegates to three separate
service objects the way ``CatalogService`` delegates to ``EntityService`` and
``FactService``. Composition would have broken an existing conformance check
that reads ``RetrievalService._semantic_arm``'s source directly (to assert
the capability arm excludes non-fact rows) — a delegating one-line wrapper
would have made that assertion pass against the wrapper's body, not the
query. So the split here is by method, not by object: each concern module
defines a mixin holding its slice of ``RetrievalService``'s methods, and this
class is their combination. ``RetrievalService.search`` (or
``._traverse_cte``, or any other method) is still the exact function object
defined in the concern module that owns it; nothing wraps it.
"""

from __future__ import annotations

import asyncio

from cachetools import LRUCache  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.config import Settings
from registry.service.governance.visibility import VisibilityService
from registry.service.retrieval.graph_traversal import _GraphTraversalMethods
from registry.service.retrieval.listing import _ListingMethods
from registry.service.retrieval.search import _SearchMethods
from registry.types import Clock, Embedder

__all__ = ["RetrievalService"]


class RetrievalService(_SearchMethods, _GraphTraversalMethods, _ListingMethods):
    """Consumer read surface — hybrid search, dependency traversal, listing."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        embedder: Embedder,
        settings: Settings | None = None,
        visibility: VisibilityService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._embedder = embedder
        # VisibilityService is the cross-tenant chokepoint. When wired,
        # `_apply_visibility` delegates to it for private/tenant-shared/public
        # enforcement. When None (unit-test paths that don't inject it),
        # `_apply_visibility` falls back to same-tenant filtering at fetch
        # time — a strict subset of cross-tenant filtering, so still secure.
        self._visibility = visibility
        _maxsize = settings.embedding_cache_maxsize if settings is not None else 1024
        self._embed_cache: LRUCache[str, list[float]] = LRUCache(maxsize=_maxsize)
        # Guards the cache-miss check + encode + write sequence so concurrent
        # coroutines on the same key don't call the embedder more than once.
        # Cache hits release the lock immediately; the contention cost is
        # negligible compared to a single encode call.
        self._embed_lock: asyncio.Lock = asyncio.Lock()
