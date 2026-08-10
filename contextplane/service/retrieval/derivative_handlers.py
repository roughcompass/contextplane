"""Propagation handlers for the retrieval artefacts: vectors, chunks, FTS, caches.

A tombstone authorises a removal and an outbox item schedules it; neither one
touches the artefact. These are what make the removal true of the retrieval side,
and the retrieval side is where the densest copies live: an `embeddings` row holds
the source text verbatim in `text_chunk`, generates its `ts_vector` from that same
column, and a pending `embedding_outbox` row holds the text a vector has not been
built from yet. An erasure that reached the source rows and stopped would leave the
person's own words searchable through both retrieval arms.

**Handlers are locator-driven, not table-guessing.** Each one reads
`storage_locator` — written by the store that owns the artefact — and refuses a
locator it does not recognise instead of interpreting it. A handler that silently
did nothing with an address it could not parse would report success for a removal
it never performed, which is the failure this whole mechanism exists to end.

**One registration per target, covering three facets — the granularity decision,
recorded.** The registry admits `vector`, `embedding_chunk` and `fts_document` as
separate kinds, so a target's artefacts could be registered once or three times.
They are registered once, under `vector`, because the three are not three
artefacts: the vector, the chunk text and the full-text material are columns of one
`embeddings` row, generated from each other, removable only together. Three
registrations would be three rows describing one thing, three propagation items per
cause of which two delete nothing, and — the part that actually misleads — three
entries in the overdue count an operator reads as three outstanding problems. The
cost of choosing one is that two kinds have handlers with no production registrar,
which is stated below rather than left to be discovered.

**Which kinds nothing registers today, and why.** `embedding_chunk` and
`fts_document` for the reason above: their facets ride on the `vector` registration
and their handlers exist so the kinds are covered if a store ever registers under
them directly. `cache` because the closure cache is built out of the entity graph and
its edges — none of the record classes a person authors — so no erasure of a person's
records schedules work for it; its handler is written and tested so the kind is genuinely
handled rather than merely absent from the queue.

**The binding property these handlers hold up:** deleting every registered
derivative for a source leaves no vector, no chunk text, no full-text material and
no pending or dead-lettered `embedding_outbox` row derived from that source.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.embedding.targets import TARGET_CLAIM
from contextplane.exceptions import RegistryError
from contextplane.retention import derivatives
from contextplane.service.retrieval.embedding_index import (
    ARTEFACT_HANDLER_VERSION,
    erase_targets,
    parse_artefact_locator,
    project_claim,
)

#: How a closure-cache entry is addressed. One registration per root and
#: direction, which is the unit the refresh worker rebuilds and the unit a
#: reader asks for; a per-member locator would be one registration per edge of
#: the closure and none of them independently removable.
_CACHE_SCHEME = "closure_cache"
_CACHE_LOCATOR_PARTS = 3

#: Recorded on closure-cache registrations. Separate from the embedding
#: registrar's version because the two artefacts change shape independently.
CACHE_HANDLER_VERSION = "closure_cache-1"


class UnknownStorageLocator(RegistryError):
    """Raised when a handler is handed an address it cannot resolve.

    Loud rather than a no-op returning zero. A handler that shrugged at an
    unparseable locator would mark the work item done, the queue would drain
    clean, and the artefact it was scheduled to remove would still be there —
    a propagation record that says an erasure completed when it did not.
    """


def cache_locator(root_entity_id: uuid.UUID, direction: str) -> str:
    """Address one root's cached closure in one direction."""
    return f"{_CACHE_SCHEME}/{root_entity_id}/{direction}"


def _parse_cache_locator(locator: str) -> tuple[uuid.UUID, str] | None:
    parts = locator.split("/")
    if len(parts) != _CACHE_LOCATOR_PARTS or parts[0] != _CACHE_SCHEME:
        return None
    try:
        root_entity_id = uuid.UUID(parts[1])
    except ValueError:
        return None
    return root_entity_id, parts[2]


class _EmbeddingArtefactHandler:
    """Shared removal for the three facets of one `embeddings` row.

    Subclassed once per kind rather than registered once under three, because
    `HandlerRegistry` keys on the kind a handler declares and the three facets
    are removed by exactly the same statements. The subclasses differ only in
    which kind they answer for.
    """

    version = ARTEFACT_HANDLER_VERSION

    async def apply(self, session: AsyncSession, registration: derivatives.Registration, operation: str) -> int:
        """Remove or rebuild one target's embedding artefacts, and report how many rows moved."""
        parsed = parse_artefact_locator(registration.storage_locator)
        if parsed is None:
            msg = (
                f"derivative {registration.derivative_id} is registered at "
                f"{registration.storage_locator!r}, which does not address the embedding index"
            )
            raise UnknownStorageLocator(msg)
        target_type, target_id = parsed

        if operation == derivatives.OPERATION_REBUILD and target_type == TARGET_CLAIM:
            # Rebuilding a claim's artefacts means asking the claim what it is now,
            # not re-embedding what it was: a claim invalidated by the erasure that
            # scheduled this is retracted by the same call that would have queued a
            # live one. One code path for both outcomes is what keeps a rebuild from
            # re-indexing content the cause of the rebuild just removed.
            requeued = await project_claim(session, claim_id=target_id, now=datetime.datetime.now(datetime.UTC))
            return 1 if requeued else 0

        # Delete and redact are the same statements, and so is any operation the
        # schema's CHECK does not admit: a vector cannot be partially redacted —
        # the text is what was embedded — so the removing branch is both the
        # correct reading of redact and the safe direction for anything unforeseen.
        counts = await erase_targets(
            session,
            target_type=target_type,
            target_ids=[target_id],
            tenant_id=registration.tenant_id,
        )
        return sum(counts.values())


class VectorErasure(_EmbeddingArtefactHandler):
    """The vector itself — and, on the same row, everything generated from its text.

    The kind the embedding index registers under, and therefore the one that does
    the work in production for both of the two below.
    """

    kind = "vector"


class EmbeddingChunkErasure(_EmbeddingArtefactHandler):
    """The chunk text an embedding was computed from.

    Nothing registers under this kind today: `text_chunk` is a column of the
    `embeddings` row registered as a vector, so it is already removed when that
    registration is applied. The handler exists so the kind is covered if a store
    ever addresses chunk text on its own.
    """

    kind = "embedding_chunk"


class FullTextDocumentErasure(_EmbeddingArtefactHandler):
    """The full-text material derived from a chunk.

    Nothing registers under this kind today either: `embeddings.ts_vector` is
    generated from `text_chunk` on the same row and cannot outlive it. Covered
    here for the same reason as the chunk above.
    """

    kind = "fts_document"


class ClosureCacheErasure:
    """The cached graph closure.

    Nothing registers under this kind today, and the reason is a property of the
    artefact rather than an omission: the closure cache is built out of the entity
    graph and its edges, none of which are record classes a person authors, so no
    erasure of a person's records ever schedules work for it. The handler is written and
    tested anyway — a kind with no handler is release-gating, and the honest way
    to close that gate is a handler that works, not a kind quietly left out.
    """

    kind = "cache"
    version = CACHE_HANDLER_VERSION

    async def apply(self, session: AsyncSession, registration: derivatives.Registration, operation: str) -> int:
        """Drop one root's cached closure in one direction, and report the rows removed.

        Every operation drops it, rebuild included. The closure cache has its own
        refresh path driven by `closure_outbox`, so dropping the rows is the whole
        of a rebuild here: the next read repopulates them from the graph as it
        stands after the cause that scheduled this.
        """
        parsed = _parse_cache_locator(registration.storage_locator)
        if parsed is None:
            msg = (
                f"derivative {registration.derivative_id} is registered at "
                f"{registration.storage_locator!r}, which does not address the closure cache"
            )
            raise UnknownStorageLocator(msg)
        root_entity_id, direction = parsed

        result = await session.execute(
            text(
                "DELETE FROM closure_cache " " WHERE tenant_id = :tid AND root_entity_id = :root AND direction = :dir"
            ),
            {"tid": registration.tenant_id, "root": root_entity_id, "dir": direction},
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


def retrieval_derivative_handlers() -> tuple[derivatives.DerivativeHandler, ...]:
    """Every retrieval-side handler, for the composition root to register.

    A function rather than a module-level tuple so the composition root decides
    when these exist, and so a test can register them into a registry of its own
    without sharing one with the process.
    """
    return (
        VectorErasure(),
        EmbeddingChunkErasure(),
        FullTextDocumentErasure(),
        ClosureCacheErasure(),
    )


__all__ = [
    "CACHE_HANDLER_VERSION",
    "ClosureCacheErasure",
    "EmbeddingChunkErasure",
    "FullTextDocumentErasure",
    "UnknownStorageLocator",
    "VectorErasure",
    "cache_locator",
    "retrieval_derivative_handlers",
]
