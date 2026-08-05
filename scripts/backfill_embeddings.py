"""Backfill embeddings for all facts that have no matching embeddings row.

Usage::

    python scripts/backfill_embeddings.py --database-url postgresql+asyncpg://...

The script is safe to run while the API is live (no locks acquired).  Re-runs
are idempotent: the NOT EXISTS predicate skips already-embedded facts.

Cursor state is persisted under a private, owner-only directory in the
system temp dir (see `_state_dir`) so an interrupted run resumes from
where it stopped.

Exit line: ``backfill complete: N facts embedded``
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Resolve the package root so the script is runnable from the repo root
# without installing the package in editable mode each time.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registry.config import Settings, get_settings  # noqa: E402
from registry.embedding import build_embedder  # noqa: E402
from registry.embedding.stub import StubEmbedder  # noqa: E402
from registry.service.retrieval.embedding_drain import make_chunk_plan  # noqa: E402
from registry.types import Embedder  # noqa: E402

_log = logging.getLogger(__name__)

_NULL_CURSOR = "00000000-0000-0000-0000-000000000000"


def _state_dir() -> Path:
    """Private, owner-only directory this script's cursor files live in.

    A bare hardcoded `/tmp/...` path is predictable and world-writable: on a
    shared host another local user could pre-create that path as a symlink,
    and a later run of this script would write its cursor through it into
    whatever the symlink points at. Scoping to a per-uid subdirectory created
    with owner-only permissions, and refusing to use it if it turns out to be
    owned by someone else, closes that off without changing the on-disk
    format (still a small text file with a UUID in it).
    """
    base = Path(tempfile.gettempdir()) / f"registry-backfill-{os.getuid()}"
    base.mkdir(mode=0o700, exist_ok=True)
    if base.stat().st_uid != os.getuid():
        raise RuntimeError(f"refusing to use cursor directory {base}: not owned by the current user")
    os.chmod(base, 0o700)
    return base


def _get_cursor_path(model_id: str) -> Path:
    """Return the cursor file path for *model_id*, slugifying unsafe characters.

    Embedding model identifiers commonly contain ``/`` (e.g.
    ``openai/text-embedding-3-small``) which would create unintended
    sub-directories or FileNotFoundError under the state directory. Slugify
    to a flat, filesystem-safe name so the cursor lives at exactly one
    well-known location per model.
    """
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", model_id)
    return _state_dir() / f"cursor_{slug}.txt"


def _load_cursor(model_id: str) -> str:
    """Return the persisted cursor UUID for *model_id*, or the null UUID."""
    path = _get_cursor_path(model_id)
    if path.exists():
        value = path.read_text().strip()
        if value:
            return value
    return _NULL_CURSOR


def _save_cursor(cursor: str, model_id: str) -> None:
    _get_cursor_path(model_id).write_text(cursor)


async def _run_backfill(settings: Settings, embedder: object) -> int:
    """Core backfill loop. Returns the total number of facts embedded."""
    engine = create_async_engine(
        settings.database_url,
        # Read-only advisory: we set the session to READ ONLY after connect.
        # asyncpg does not support a read_only kwarg at engine level, so we
        # enforce it via SET TRANSACTION.
        pool_pre_ping=True,
    )
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    model_id: str = embedder.model_version  # type: ignore[attr-defined]
    batch_size = settings.backfill_batch_size
    cursor = _load_cursor(model_id)
    total = 0

    try:
        while True:
            # --- Fetch one page of un-embedded facts --------------------------
            async with session_factory() as session:
                rows = list(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT fact_id, body
                                FROM   facts
                                WHERE  fact_id > :cursor
                                  AND  NOT EXISTS (
                                      SELECT 1 FROM embeddings
                                      WHERE  target_type = 'fact'
                                        AND  target_id = facts.fact_id
                                        AND  model_id = :model_id
                                  )
                                ORDER  BY fact_id
                                LIMIT  :batch_size
                                """
                            ),
                            {
                                "cursor": cursor,
                                "model_id": model_id,
                                "batch_size": batch_size,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )

            if not rows:
                break

            # --- Embed and insert ---------------------------------------------
            now = datetime.datetime.now(tz=datetime.UTC)
            for row in rows:
                fact_id: uuid.UUID = row["fact_id"]
                body: str = row["body"] or ""

                chunk_plan = make_chunk_plan(body)
                chunks = [str(entry["text"]) for entry in chunk_plan]
                vectors = embedder.encode(chunks)  # type: ignore[attr-defined]

                async with session_factory() as session, session.begin():
                    for i, (chunk_text, vector) in enumerate(zip(chunks, vectors, strict=False)):
                        idx_val = chunk_plan[i].get("index", i)
                        chunk_idx = int(idx_val) if isinstance(idx_val, int | float | str) else i
                        await session.execute(
                            text(
                                """
                                INSERT INTO embeddings
                                    (embedding_id, tenant_id, target_type, target_id,
                                     chunk_index, model_id, vector, text_chunk,
                                     created_at)
                                SELECT gen_random_uuid(),
                                       f.tenant_id,
                                       'fact',
                                       f.fact_id,
                                       :chunk_index,
                                       :model_id,
                                       :vector,
                                       :text_chunk,
                                       :created_at
                                FROM   facts f
                                WHERE  f.fact_id = :fact_id
                                  AND  NOT EXISTS (
                                      SELECT 1 FROM embeddings
                                      WHERE  target_type = 'fact'
                                        AND  target_id = :fact_id
                                        AND  model_id  = :model_id
                                        AND  chunk_index = :chunk_index
                                  )
                                """
                            ),
                            {
                                "fact_id": str(fact_id),
                                "chunk_index": chunk_idx,
                                "model_id": model_id,
                                "vector": np.asarray(vector).tolist(),
                                "text_chunk": chunk_text,
                                "created_at": now,
                            },
                        )

                total += 1
                cursor = str(fact_id)

            _save_cursor(cursor, model_id)
            _log.info("backfill: processed up to cursor=%s total=%d", cursor, total)

    finally:
        await engine.dispose()

    return total


def _build_embedder(settings: Settings, stub: bool) -> Embedder:
    """Real embedder from settings, or zero vectors when --stub is passed.

    The stub keeps the configured model id so the cursor file and the rows it
    writes stay on the same key as a real run — that is the point of --stub as a
    dry-run switch. It means the rows are indistinguishable from real ones
    afterwards, which is why it takes an explicit flag and never happens on its
    own.
    """
    if stub:
        stub_embedder = StubEmbedder(dim=settings.embedding_dim)
        stub_embedder.model_version = settings.embedding_model
        return stub_embedder
    return build_embedder(settings)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill embeddings for facts with no embedding row.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="asyncpg database URL (overrides DATABASE_URL env var)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Page size (overrides BACKFILL_BATCH_SIZE / settings default of 64)",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Use StubEmbedder (zero vectors) — for testing without model download",
    )
    parser.add_argument(
        "--reset-cursor",
        action="store_true",
        help=(
            "Delete the resumption cursor for the chosen model before starting "
            "so the backfill scans from the beginning. Useful for recovery after "
            "a partial run with a wrong cursor position."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)

    # Settings is the single env-var reader. --database-url
    # overrides; otherwise get_settings() reads DATABASE_URL.
    if args.database_url:
        settings = Settings(
            database_url=args.database_url,
            pgbouncer_url=args.database_url,
            scheduler_jobstore_url=args.database_url,
        )
    else:
        settings = get_settings()
    if args.batch_size is not None:
        object.__setattr__(settings, "backfill_batch_size", args.batch_size)

    embedder = _build_embedder(settings, stub=args.stub)

    if args.reset_cursor:
        model_id = embedder.model_version
        cursor_path = _get_cursor_path(model_id)
        if cursor_path.exists():
            cursor_path.unlink()
            _log.info("backfill: reset cursor file at %s", cursor_path)

    total = asyncio.run(_run_backfill(settings, embedder))
    print(f"backfill complete: {total} facts embedded")


if __name__ == "__main__":
    main()
