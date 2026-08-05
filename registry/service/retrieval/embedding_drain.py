"""Outbox drain job — consumes `embedding_outbox`, writes to `embeddings`.

Registered as an APScheduler job in `registry/main.py`
lifespan. Runs every `settings.outbox_poll_interval_s` seconds with
`max_instances=1` and `coalesce=True` so overlapping ticks are harmless.

Design notes:
- `SELECT ... FOR UPDATE SKIP LOCKED` lets multiple instances (future) or
  concurrent test runs avoid double-processing without deadlocks.
- `chunk_plan` is materialized on the outbox row at enqueue time (whitespace
  token chunks, size=400, stride=200) so the drain job doesn't need to
  re-parse anything — it just calls `encode()` on the pre-computed chunks.
- All DB work for one row (insert embeddings + delete outbox row) runs inside
  a single `session.begin()` so a crash after encode but before commit leaves
  the outbox row intact for retry.
- Failures increment `attempts`; once `>= outbox_max_attempts` the row moves
  to `embedding_outbox_failed` and `catalog_outbox_pending_size` is decremented.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Any

import numpy as np
import numpy.typing as npt
from prometheus_client import Counter, Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.config import Settings
from registry.embedding.targets import EMBEDDING_TARGETS
from registry.service.retrieval.embedding_index import index_coverage
from registry.types import Embedder

_log = logging.getLogger(__name__)

# Registered once at module import; safe to call set/dec from async code.
_OUTBOX_PENDING_GAUGE: Gauge = Gauge(
    "catalog_outbox_pending_size",
    "Number of rows currently pending in embedding_outbox",
)

# Everything below is labelled by target kind. One pipeline is what makes these
# comparable at all -- with a queue per kind there would be two of each number and no
# way to read them together. The vision's standard is that a number nobody can check is
# not a signal, so each one is derived from recorded rows rather than asserted.
_OUTBOX_PENDING_BY_KIND: Gauge = Gauge(
    "embedding_outbox_pending",
    "Rows pending in embedding_outbox, by target kind.",
    ["target_type"],
)

# Age of the oldest waiting row. Depth alone cannot distinguish a queue that is short
# because it is keeping up from one that is short because nothing is being enqueued --
# and the second is the failure that hid an empty claim index for a whole phase.
_OUTBOX_OLDEST_SECONDS: Gauge = Gauge(
    "embedding_outbox_oldest_pending_seconds",
    "Age of the oldest pending row in embedding_outbox, by target kind.",
    ["target_type"],
)

# The coverage number: how much of what should be indexed actually is, under the model
# currently running. This is the one that would have made "semantic claim retrieval is
# empty" visible on a dashboard instead of discoverable by reading code.
_INDEX_COVERAGE: Gauge = Gauge(
    "embedding_index_coverage_ratio",
    "Fraction of indexable rows holding a vector under the running model, by kind.",
    ["target_type"],
)

_DRAINED: Counter = Counter(
    "embedding_drain_processed_total",
    "Outbox rows successfully drained, by target kind.",
    ["target_type"],
)

_DEAD_LETTERED: Counter = Counter(
    "embedding_drain_dead_lettered_total",
    "Outbox rows moved to the dead-letter table, by target kind.",
    ["target_type"],
)

# The affordability signal. Embedding is the metered part of this pipeline, so a steward
# accountable for cost needs the volume rather than only the row count.
_EMBEDDED_CHUNKS: Counter = Counter(
    "embedding_chunks_embedded_total",
    "Text chunks sent to the embedder, by target kind.",
    ["target_type"],
)
_EMBEDDED_BYTES: Counter = Counter(
    "embedding_bytes_embedded_total",
    "Bytes of text sent to the embedder, by target kind.",
    ["target_type"],
)

# The model coverage is measured against. Set by the drain each tick rather than read
# from settings here, so the number always describes the model that is actually running.
_model_id_for_coverage: str = ""


def _set_coverage_model(model_id: str) -> None:
    global _model_id_for_coverage
    _model_id_for_coverage = model_id


# Cooldown between retries for a failed row (seconds).
_COOLDOWN_S: int = 60

# Default sliding-window size. Overridable per call, and the fact producer passes the
# configured `EMBEDDING_CHUNK_TOKENS`; this default exists so the drain's own fallback
# path (an outbox row that arrived with an empty plan) still has a value.
_CHUNK_TOKENS: int = 400


# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------


def make_chunk_plan(
    body: str,
    chunk_tokens: int = _CHUNK_TOKENS,
    stride: int | None = None,
) -> list[dict[str, object]]:
    """Split *body* into overlapping whitespace-token windows.

    Returns a JSON-serialisable list so it can be stored in `chunk_plan`. A body that fits
    in one window yields exactly one entry with index 0.

    `stride` defaults to half the window. Deriving it rather than configuring it
    separately keeps one knob in charge of granularity: two independent settings can be
    set to contradict each other -- a stride wider than the window silently drops text
    between chunks -- and nothing would catch that.
    """
    if stride is None:
        stride = max(1, chunk_tokens // 2)
    tokens = body.split()
    if not tokens:
        return [{"index": 0, "start": 0, "end": 0, "text": ""}]

    entries: list[dict[str, object]] = []
    idx = 0
    start = 0
    while start < len(tokens):
        end = min(start + chunk_tokens, len(tokens))
        chunk_text = " ".join(tokens[start:end])
        entries.append({"index": idx, "start": start, "end": end, "text": chunk_text})
        if end >= len(tokens):
            break
        start += stride
        idx += 1

    return entries


# ---------------------------------------------------------------------------
# Drain job
# ---------------------------------------------------------------------------


async def drain_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
) -> None:
    """Drain one batch from `embedding_outbox`.

    Called by APScheduler; exceptions are caught internally and logged so the
    scheduler doesn't treat a transient DB error as a job failure.
    """
    try:
        await _drain_batch(session_factory, embedder, settings)
    except Exception:  # noqa: BLE001 - scheduled job boundary, see docstring above
        _log.exception("drain_outbox: unexpected error during batch; will retry next tick")


async def _drain_batch(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
) -> None:
    """Core drain logic. Raises on unexpected errors (caller wraps)."""
    batch_size = settings.outbox_batch_size
    max_attempts = settings.outbox_max_attempts

    async with session_factory() as session:
        # --- Claim a batch with SKIP LOCKED so concurrent drainers don't race.
        raw_rows: list[Any] = list(
            (
                await session.execute(
                    text(
                        """
                        SELECT outbox_id, tenant_id, target_type, target_id,
                               text_to_embed, chunk_plan, attempts, enqueued_at
                        FROM   embedding_outbox
                        WHERE  last_error IS NULL
                           OR  last_attempt_at < now() - interval ':cooldown seconds'
                        ORDER  BY enqueued_at
                        LIMIT  :batch_size
                        FOR UPDATE SKIP LOCKED
                        """.replace(":cooldown", str(_COOLDOWN_S))
                    ),
                    {"batch_size": batch_size},
                )
            )
            .mappings()
            .all()
        )
        rows: list[dict[str, Any]] = [dict(r) for r in raw_rows]

    if not rows:
        return

    for row in rows:
        await _process_row(session_factory, embedder, settings, row, max_attempts)

    # Update pending gauge with a fresh count (best-effort).
    _set_coverage_model(embedder.model_version)
    await _refresh_pending_gauge(session_factory)


async def _process_row(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    settings: Settings,
    row: dict[str, Any],
    max_attempts: int,
) -> None:
    outbox_id: uuid.UUID = row["outbox_id"]
    tenant_id: uuid.UUID = row["tenant_id"]
    target_type: str = row["target_type"]
    target_id: uuid.UUID = row["target_id"]
    text_to_embed: str = row["text_to_embed"]
    chunk_plan_raw: list[dict[str, Any]] = row["chunk_plan"] or []
    attempts: int = row["attempts"]
    # Carried so the delete can check the row was not refreshed while we encoded.
    enqueued_at: Any = row["enqueued_at"]

    # If chunk_plan is empty/malformed, re-compute it now.
    if not chunk_plan_raw:
        chunk_plan_raw = make_chunk_plan(text_to_embed)

    chunks = [str(entry["text"]) for entry in chunk_plan_raw]

    try:
        # Off the event loop — a batch inference pass or a remote embedding call
        # would otherwise block every other coroutine in the worker process.
        vectors: npt.NDArray[np.float32] = await asyncio.to_thread(embedder.encode, chunks)
    # embedder.encode is a pluggable local/remote embedder; one row's failure
    # must not stop the drain. _handle_failure logs before doing anything else.
    except Exception as exc:  # noqa: BLE001 - see comment above
        await _handle_failure(
            session_factory,
            outbox_id,
            tenant_id,
            target_type,
            target_id,
            text_to_embed,
            chunk_plan_raw,
            attempts,
            max_attempts,
            error_text=repr(exc),
        )
        return

    now = datetime.datetime.now(tz=datetime.UTC)

    try:
        async with session_factory() as session, session.begin():
            # Clear this target's vectors for the running model before writing the new
            # ones. `ON CONFLICT` alone is not enough: if the text now yields fewer chunks
            # than last time, the surplus high-index rows would survive and keep being
            # retrieved. Scoped to `model_id`, so a reindex under a new model still adds
            # rows rather than replacing the previous model's.
            await session.execute(
                text(
                    "DELETE FROM embeddings "
                    " WHERE tenant_id = :tenant_id AND target_type = :target_type "
                    "   AND target_id = :target_id AND model_id = :model_id"
                ),
                {
                    "tenant_id": tenant_id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "model_id": embedder.model_version,
                },
            )
            # Insert one embedding row per chunk.
            for i, (chunk_text, vector) in enumerate(zip(chunks, vectors, strict=False)):
                idx_val = chunk_plan_raw[i].get("index", i)
                chunk_idx = int(idx_val) if isinstance(idx_val, int | float | str) else i
                await session.execute(
                    text(
                        """
                        INSERT INTO embeddings
                            (embedding_id, tenant_id, target_type, target_id,
                             chunk_index, model_id, vector, text_chunk, created_at)
                        VALUES
                            (gen_random_uuid(), :tenant_id, :target_type, :target_id,
                             :chunk_index, :model_id, :vector, :text_chunk, :created_at)
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "target_type": target_type,
                        "target_id": target_id,
                        "chunk_index": chunk_idx,
                        "model_id": embedder.model_version,
                        # pgvector via asyncpg requires a string literal, not a Python list.
                        "vector": "[" + ",".join(str(x) for x in vector.tolist()) + "]",  # type: ignore[attr-defined]
                        "text_chunk": chunk_text,
                        "created_at": now,
                    },
                )
            # Delete the processed row only if it has not been re-enqueued while we were
            # encoding. Enqueue is an upsert that refreshes `enqueued_at`, so an
            # unconditional delete would discard a newer request that arrived mid-flight --
            # a lost update whose newer text would never be embedded.
            await session.execute(
                text("DELETE FROM embedding_outbox WHERE outbox_id = :oid AND enqueued_at = :enqueued_at"),
                {"oid": outbox_id, "enqueued_at": enqueued_at},
            )
        _DRAINED.labels(target_type=target_type).inc()
    # DB write for this row failing must not stop the drain; same isolation
    # boundary as the encode step above, logged first thing in _handle_failure.
    except Exception as exc:  # noqa: BLE001 - see comment above
        await _handle_failure(
            session_factory,
            outbox_id,
            tenant_id,
            target_type,
            target_id,
            text_to_embed,
            chunk_plan_raw,
            attempts,
            max_attempts,
            error_text=repr(exc),
        )


async def _handle_failure(
    session_factory: async_sessionmaker[AsyncSession],
    outbox_id: uuid.UUID,
    tenant_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    text_to_embed: str,
    chunk_plan: list[dict[str, Any]],
    attempts: int,
    max_attempts: int,
    error_text: str,
) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    new_attempts = attempts + 1
    _log.warning(
        "embedding_drain: attempt %d/%d failed for outbox_id=%s: %s",
        new_attempts,
        max_attempts,
        outbox_id,
        error_text[:200],
    )

    if new_attempts >= max_attempts:
        # Move to dead-letter table.
        try:
            async with session_factory() as session, session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO embedding_outbox_failed
                            (failed_id, tenant_id, claim_type, fact_id,
                             text_to_embed, chunk_plan, failed_at, error_text, attempts)
                        VALUES
                            (gen_random_uuid(), :tenant_id, :claim_type, :fact_id,
                             :text_to_embed, CAST(:chunk_plan AS jsonb),
                             :failed_at, :error_text, :attempts)
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "target_type": target_type,
                        "target_id": target_id,
                        "text_to_embed": text_to_embed,
                        "chunk_plan": _jsonb_dumps(chunk_plan),
                        "failed_at": now,
                        "error_text": error_text,
                        "attempts": new_attempts,
                    },
                )
                await session.execute(
                    text("DELETE FROM embedding_outbox WHERE outbox_id = :oid"),
                    {"oid": outbox_id},
                )
            _DEAD_LETTERED.labels(target_type=target_type).inc()
        except Exception:  # noqa: BLE001 - one row's dead-letter write failing must not stop the drain
            _log.exception("embedding_drain: could not move outbox_id=%s to failed table", outbox_id)
    else:
        # Increment attempts and record error for cooldown.
        try:
            async with session_factory() as session, session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE embedding_outbox
                        SET    attempts        = :attempts,
                               last_error      = :last_error,
                               last_attempt_at = :last_attempt_at
                        WHERE  outbox_id = :oid
                        """
                    ),
                    {
                        "attempts": new_attempts,
                        "last_error": error_text[:2000],
                        "last_attempt_at": now,
                        "oid": outbox_id,
                    },
                )
        except Exception:  # noqa: BLE001 - one row's attempt-count write failing must not stop the drain
            _log.exception("embedding_drain: could not update attempts for outbox_id=%s", outbox_id)


async def _refresh_pending_gauge(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Publish queue depth, queue age, and index coverage, per target kind.

    Three numbers rather than one because they fail differently. Depth says how much work
    is waiting. Age says whether the queue is moving -- a queue that is short because
    nothing is being enqueued looks identical to a healthy one on depth alone, and that is
    precisely how an empty claim index went unnoticed. Coverage says whether the index
    actually reflects the store, which is the claim a steward is accountable for.

    Kinds with nothing to report are still published as zero. A label that disappears
    when its value is zero makes a dashboard read as "no data" exactly when it should
    read as "nothing pending", and the two need different responses.
    """
    try:
        async with session_factory() as session:
            total: int = (await session.execute(text("SELECT COUNT(*) FROM embedding_outbox"))).scalar_one()

            pending = (
                (
                    await session.execute(
                        text(
                            "SELECT target_type, count(*) AS n, "
                            "       COALESCE(MAX(EXTRACT(EPOCH FROM (now() - enqueued_at))), 0) AS oldest "
                            "  FROM embedding_outbox GROUP BY target_type"
                        )
                    )
                )
                .mappings()
                .all()
            )
            by_kind = {row["target_type"]: (int(row["n"]), float(row["oldest"])) for row in pending}

        _OUTBOX_PENDING_GAUGE.set(total)
        for kind in EMBEDDING_TARGETS:
            count, oldest = by_kind.get(kind, (0, 0.0))
            _OUTBOX_PENDING_BY_KIND.labels(target_type=kind).set(count)
            _OUTBOX_OLDEST_SECONDS.labels(target_type=kind).set(oldest)
        # Coverage is computed by the index module, not here. This function is the
        # consumer's metric refresh, and the consumer is deliberately blind to what kinds
        # of thing it embeds -- teaching it the claim schema would undo the property that
        # makes adding a new target kind a producer-only change.
        for kind, ratio in (await index_coverage(session_factory, _model_id_for_coverage)).items():
            _INDEX_COVERAGE.labels(target_type=kind).set(ratio)
    except Exception:  # noqa: BLE001 - metric refresh is best-effort, must not break the drain tick
        _log.debug("embedding_drain: could not refresh queue and coverage metrics", exc_info=True)


def _jsonb_dumps(obj: object) -> str:
    """Minimal JSON serialiser for jsonb cast — uses stdlib json."""
    import json

    return json.dumps(obj)


__all__ = ["drain_outbox", "make_chunk_plan", "_OUTBOX_PENDING_GAUGE"]
