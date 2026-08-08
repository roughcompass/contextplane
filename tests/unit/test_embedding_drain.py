"""Unit tests for contextplane/service/retrieval/embedding_drain.py.

All DB interactions are mocked — no Docker or real Postgres required.
Tests exercise:
  - make_chunk_plan: chunking, stride, single-chunk short bodies
  - drain_outbox: gauge update, cooldown predicate presence, max-attempts move-to-failed
  - _process_row: per-row insert+delete in one transaction
  - _handle_failure: increment path vs. move-to-failed path
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from contextplane.config import Settings
from contextplane.service.retrieval.embedding_drain import (
    _OUTBOX_PENDING_GAUGE,
    _handle_failure,
    _process_row,
    drain_outbox,
    make_chunk_plan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base = Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        outbox_poll_interval_s=5,
        outbox_batch_size=32,
        outbox_max_attempts=5,
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def _stub_embedder(dim: int = 384) -> MagicMock:
    emb = MagicMock()
    emb.model_version = "stub-zero"
    emb.encode = MagicMock(side_effect=lambda texts: np.zeros((len(texts), dim), dtype=np.float32))
    return emb


def _fake_session_factory(rows: list[dict[str, Any]]) -> AsyncMock:
    """Return a mock session_factory whose sessions replay *rows* once then return empty."""
    # Build a mock session that returns rows on first execute, then empty.
    row_iter = iter([rows, []])

    async def _execute(stmt: Any, params: Any = None) -> MagicMock:
        result = MagicMock()
        try:
            batch = next(row_iter)
        except StopIteration:
            batch = []
        mappings_mock = MagicMock()
        mappings_mock.all.return_value = batch
        result.mappings.return_value = mappings_mock
        result.scalar_one.return_value = len(batch)
        result.scalar_one_or_none.return_value = len(batch) if batch else None
        return result

    session = AsyncMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)
    return factory  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# make_chunk_plan
# ---------------------------------------------------------------------------


class TestMakeChunkPlan:
    def test_short_body_single_chunk(self) -> None:
        body = "hello world this is a short fact"
        plan = make_chunk_plan(body)
        assert len(plan) == 1
        assert plan[0]["index"] == 0
        assert plan[0]["text"] == body

    def test_empty_body(self) -> None:
        plan = make_chunk_plan("")
        assert len(plan) == 1
        assert plan[0]["text"] == ""

    def test_exact_chunk_size_no_overflow(self) -> None:
        # 400 tokens — should produce exactly one chunk
        body = " ".join(f"w{i}" for i in range(400))
        plan = make_chunk_plan(body, chunk_tokens=400, stride=200)
        assert len(plan) == 1

    def test_sliding_window_multiple_chunks(self) -> None:
        # 600 tokens → chunk 0: [0,400), chunk 1: [200,400+200)=[200,600)
        body = " ".join(f"w{i}" for i in range(600))
        plan = make_chunk_plan(body, chunk_tokens=400, stride=200)
        assert len(plan) == 2
        assert plan[0]["index"] == 0
        assert plan[0]["start"] == 0
        assert plan[0]["end"] == 400
        assert plan[1]["index"] == 1
        assert plan[1]["start"] == 200
        assert plan[1]["end"] == 600

    def test_three_chunks(self) -> None:
        # 800 tokens → 0-400, 200-600, 400-800
        body = " ".join(f"w{i}" for i in range(800))
        plan = make_chunk_plan(body, chunk_tokens=400, stride=200)
        assert len(plan) == 3
        assert plan[2]["end"] == 800

    def test_plan_is_serialisable(self) -> None:
        import json

        plan = make_chunk_plan("a b c d e")
        serialised = json.dumps(plan)
        # Round-trips exactly -- not just "did not raise", but no data lost
        # or coerced into a different shape by the JSON encoding.
        assert json.loads(serialised) == plan


# ---------------------------------------------------------------------------
# drain_outbox — top-level exception swallowing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_outbox_swallows_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    """drain_outbox must not propagate exceptions — scheduler must survive."""
    broken_factory = MagicMock(side_effect=RuntimeError("db down"))
    embedder = _stub_embedder()
    settings = _settings()

    with caplog.at_level(logging.ERROR, logger="contextplane.service.retrieval.embedding_drain"):
        result = await drain_outbox(broken_factory, embedder, settings)

    assert result is None
    # The failure path was actually exercised, not skipped -- and it was
    # logged, so an operator can see the batch failed rather than the
    # failure disappearing silently.
    broken_factory.assert_called_once()
    assert any("unexpected error during batch" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# _process_row — happy path: encode + insert + delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_row_calls_encode_and_writes() -> None:
    embedder = _stub_embedder()
    settings = _settings()

    outbox_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    target_id = uuid.uuid4()
    chunk_plan = make_chunk_plan("alpha beta gamma")

    row: dict[str, Any] = {
        "outbox_id": outbox_id,
        "tenant_id": tenant_id,
        "target_type": "fact",
        "target_id": target_id,
        "text_to_embed": "alpha beta gamma",
        "chunk_plan": chunk_plan,
        "attempts": 0,
        "enqueued_at": "2026-01-01T00:00:00Z",
    }

    executed_stmts: list[str] = []

    async def _execute(stmt: Any, params: Any = None) -> MagicMock:
        executed_stmts.append(str(stmt))
        result = MagicMock()
        result.scalar_one.return_value = 0
        return result

    session = AsyncMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)

    await _process_row(factory, embedder, settings, row, max_attempts=5)  # type: ignore[arg-type]

    # encode was called once with the chunk texts
    embedder.encode.assert_called_once()
    call_args = embedder.encode.call_args[0][0]
    assert isinstance(call_args, list)
    assert len(call_args) >= 1

    # At least one INSERT INTO embeddings and one DELETE FROM embedding_outbox
    insert_calls = [s for s in executed_stmts if "INSERT INTO embeddings" in s]
    delete_calls = [s for s in executed_stmts if "DELETE FROM embedding_outbox" in s]
    assert len(insert_calls) >= 1, "expected INSERT INTO embeddings"
    assert len(delete_calls) >= 1, "expected DELETE FROM embedding_outbox"


# ---------------------------------------------------------------------------
# _handle_failure — increment path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_failure_increments_attempts() -> None:
    outbox_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    target_id = uuid.uuid4()
    chunk_plan = make_chunk_plan("text")
    executed_stmts: list[str] = []

    async def _execute(stmt: Any, params: Any = None) -> MagicMock:
        executed_stmts.append(str(stmt))
        return MagicMock()

    session = AsyncMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    await _handle_failure(
        factory,  # type: ignore[arg-type]
        outbox_id,
        tenant_id,
        "fact",
        target_id,
        "text",
        chunk_plan,
        attempts=1,
        max_attempts=5,
        error_text="boom",
    )

    update_calls = [s for s in executed_stmts if "UPDATE embedding_outbox" in s]
    assert len(update_calls) == 1, "should UPDATE attempts when below max"


# ---------------------------------------------------------------------------
# _handle_failure — move-to-failed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_failure_moves_to_failed_at_max_attempts() -> None:
    outbox_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    target_id = uuid.uuid4()
    chunk_plan = make_chunk_plan("text")
    executed_stmts: list[str] = []

    async def _execute(stmt: Any, params: Any = None) -> MagicMock:
        executed_stmts.append(str(stmt))
        return MagicMock()

    session = AsyncMock()
    session.execute = _execute
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    # attempts=4, max_attempts=5 → new_attempts=5 → move-to-failed
    await _handle_failure(
        factory,  # type: ignore[arg-type]
        outbox_id,
        tenant_id,
        "fact",
        target_id,
        "text",
        chunk_plan,
        attempts=4,
        max_attempts=5,
        error_text="persistent error",
    )

    insert_failed = [s for s in executed_stmts if "embedding_outbox_failed" in s]
    delete_outbox = [s for s in executed_stmts if "DELETE FROM embedding_outbox" in s]
    assert len(insert_failed) >= 1, "should INSERT INTO embedding_outbox_failed"
    assert len(delete_outbox) >= 1, "should DELETE from embedding_outbox"


# ---------------------------------------------------------------------------
# Gauge: confirm it is a Gauge and can be set
# ---------------------------------------------------------------------------


def test_outbox_pending_gauge_is_settable() -> None:
    # A settable gauge means the value read back reflects what was set, not
    # merely that .set() can be called without raising.
    _OUTBOX_PENDING_GAUGE.set(42)
    assert _OUTBOX_PENDING_GAUGE._value.get() == 42
    _OUTBOX_PENDING_GAUGE.set(0)
    assert _OUTBOX_PENDING_GAUGE._value.get() == 0


# ---------------------------------------------------------------------------
# Cooldown predicate: the SQL in _drain_batch references last_attempt_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_batch_sql_contains_cooldown_predicate() -> None:
    """Verify the drain SELECT includes the cooldown condition in its SQL text."""
    import inspect

    from contextplane.service.retrieval import embedding_drain

    src = inspect.getsource(embedding_drain._drain_batch)
    assert "last_attempt_at" in src, "cooldown predicate missing from drain query"
    assert "SKIP LOCKED" in src, "SKIP LOCKED must be present for safe concurrent drain"


def test_the_stride_is_half_the_window_whatever_the_window_is() -> None:
    """One knob controls granularity.

    Two independent settings can be set to contradict each other -- a stride wider than
    the window silently drops the text between chunks, and nothing would catch it. So the
    stride is derived, and this pins the derivation across sizes rather than at one value.
    """
    body = " ".join(f"t{i}" for i in range(100))

    tight = make_chunk_plan(body, chunk_tokens=20)
    # 100 tokens, window 20, stride 10. Windows start every 10 tokens and the walk stops
    # once one reaches the end, so the last starts at 80 and covers 80..100 -- there is no
    # window at 90, because it would repeat text already covered and end nowhere new.
    assert [entry["start"] for entry in tight] == list(range(0, 81, 10))
    assert tight[-1]["end"] == 100

    wide = make_chunk_plan(body, chunk_tokens=100)
    assert len(wide) == 1, "a window covering the whole body is one chunk"


def test_a_configured_window_changes_the_plan() -> None:
    """The setting has to reach the plan, or `EMBEDDING_CHUNK_TOKENS` is decoration.

    It was decoration: the value was parsed from the environment into Settings and read
    by nothing, while the producer used a module constant.
    """
    body = " ".join(f"t{i}" for i in range(1000))
    assert len(make_chunk_plan(body, chunk_tokens=100)) > len(make_chunk_plan(body, chunk_tokens=500))
