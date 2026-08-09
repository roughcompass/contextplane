"""Retrieval-area service construction: the embedder and the hybrid search service.

One registration entry point per area, called by the composition root — see
`contextplane.service.governance.wiring` for the shape.

The embedder is built here rather than in the root because retrieval is its
only consumer in the request-serving graph: `RetrievalService` holds it, and
a second `build_embedder` call would be a second model load. The scheduler's
embedding-drain job takes the same instance from the container.

The startup assertion that refuses a vector-width mismatch also lives here,
beside the wiring whose configuration it checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.config import Settings
from contextplane.embedding import build_embedder
from contextplane.service.governance.visibility import VisibilityService
from contextplane.service.retrieval import RetrievalService
from contextplane.types import Clock, Embedder


@dataclass(frozen=True)
class RetrievalServices:
    """What this area contributes to the typed container, by container field name."""

    embedder: Embedder
    retrieval: RetrievalService


def build_retrieval_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    visibility: VisibilityService,
) -> RetrievalServices:
    """Construct the retrieval area's services."""
    embedder = build_embedder(settings)
    return RetrievalServices(
        embedder=embedder,
        retrieval=RetrievalService(session_factory, clock, embedder, settings, visibility=visibility),
    )


async def assert_embedding_dim_matches(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """Refuse to start when the configured vector width disagrees with the schema.

    Caught here, this is a one-line startup error. Caught later, it is an insert
    failure in the drain — after the outbox has already accepted the work, on a
    background job whose errors surface as a retry count rather than a crash.
    Every fact ingested in between looks accepted and silently never becomes
    searchable.

    A column declared as bare ``vector`` with no width reports ``atttypmod`` -1;
    that is not a mismatch, it just means the schema imposes no constraint.
    """
    async with session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT a.atttypmod
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = 'embeddings' AND a.attname = 'vector' AND a.attnum > 0
                """
            )
        )
        row = result.first()

    if row is None:
        return
    column_dim = int(row[0])
    if column_dim < 0 or column_dim == settings.embedding_dim:
        return

    raise RuntimeError(
        f"embedding dimension mismatch: EMBEDDING_DIM is {settings.embedding_dim} but the "
        f"embeddings.vector column stores {column_dim}-d vectors. Either set "
        f"EMBEDDING_DIM={column_dim} to match the schema, or run "
        f"`EMBEDDING_DIM_ALLOW_REBUILD=true alembic upgrade head` to rebuild the column at "
        f"{settings.embedding_dim} — which deletes and recomputes every embedding."
    )
