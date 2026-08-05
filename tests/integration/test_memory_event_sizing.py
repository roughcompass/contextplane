"""Event bytes are recorded at ingest, because erasure makes them unrecoverable.

The compression story is claim bytes over event bytes. The claim side was always
recorded; the event side was not, so the ratio had a numerator and no
denominator. What makes this urgent rather than merely missing is that actor
erasure deletes event rows outright — a size not captured at ingest is gone, not
late.

These tests pin the shape as well as the value. The two tables must describe the
same quantity under the same column names with the same null-pairing rule,
because a ratio that silently compares unlike things is worse than one that is
obviously absent.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.memory.session_events import MemoryService
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_producer_ctx as _ctx

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)

# Bytes and characters diverge here, which is the case a naive len() gets wrong:
# 24 characters, 34 bytes in UTF-8.
_MULTIBYTE_BODY = "café ☕ naïve — résumé 日本"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"siz-{tid.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return tid, aid


@pytest.fixture
def memory(factory: async_sessionmaker[AsyncSession]) -> MemoryService:
    return MemoryService(factory, clock=FakeClock(_NOW))


async def _stored(
    factory: async_sessionmaker[AsyncSession], event_id: uuid.UUID
) -> tuple[int, int | None, str | None, int]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT size_bytes, token_count, tokenizer_id, octet_length(body) AS actual "
                    "FROM memory_session_events WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        ).one()
    return row.size_bytes, row.token_count, row.tokenizer_id, row.actual


@pytest.mark.asyncio(loop_scope="module")
async def test_a_new_event_records_its_body_bytes(
    factory: async_sessionmaker[AsyncSession], memory: MemoryService
) -> None:
    tid, aid = await _seed_tenant(factory)
    event = await memory.record_event(_ctx(tid, aid), session_id="s1", kind="user_message", body="hello world")

    size, tokens, tokenizer, actual = await _stored(factory, event.event_id)
    assert size == actual == len(b"hello world")
    assert tokens is None
    assert tokenizer is None


@pytest.mark.asyncio(loop_scope="module")
async def test_bytes_not_characters_for_a_multibyte_body(
    factory: async_sessionmaker[AsyncSession], memory: MemoryService
) -> None:
    """The case a naive `len()` gets wrong. Under a character count the
    denominator is understated and the compression ratio reads better than it
    is, in the direction nobody checks."""
    tid, aid = await _seed_tenant(factory)
    event = await memory.record_event(_ctx(tid, aid), session_id="s1", kind="user_message", body=_MULTIBYTE_BODY)

    size, _, _, actual = await _stored(factory, event.event_id)
    assert size == actual == len(_MULTIBYTE_BODY.encode("utf-8"))
    assert size != len(_MULTIBYTE_BODY), "test body must be multi-byte to prove anything"


@pytest.mark.asyncio(loop_scope="module")
async def test_an_empty_body_records_zero_rather_than_null(
    factory: async_sessionmaker[AsyncSession], memory: MemoryService
) -> None:
    """Zero bytes is a measurement. NULL would mean not-yet-counted, and the
    two must not collapse."""
    tid, aid = await _seed_tenant(factory)
    event = await memory.record_event(_ctx(tid, aid), session_id="s1", kind="user_message", body="")

    size, _, _, _ = await _stored(factory, event.event_id)
    assert size == 0


@pytest.mark.asyncio(loop_scope="module")
async def test_a_row_written_without_the_column_is_backfilled_not_left_null(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Standing in for a pre-migration row. The column is NOT NULL, so a row
    that omits it must be rejected outright rather than stored unmeasured —
    which is what makes the backfill a one-time job rather than a permanent
    tolerance for nulls."""
    tid, aid = await _seed_tenant(factory)
    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_session_events "
                    "  (tenant_id, actor_id, session_id, seq, kind, body, created_at, expires_at) "
                    "VALUES (:tid, :aid, 'legacy', 1, 'user_message', 'old', :now, :now)"
                ),
                {"tid": tid, "aid": aid, "now": _NOW},
            )


@pytest.mark.asyncio(loop_scope="module")
async def test_a_token_count_without_a_tokenizer_is_rejected(
    factory: async_sessionmaker[AsyncSession], memory: MemoryService
) -> None:
    """A count nobody can attribute to a tokenizer cannot be compared to any
    other count, so it is not a measurement."""
    tid, aid = await _seed_tenant(factory)
    event = await memory.record_event(_ctx(tid, aid), session_id="s1", kind="user_message", body="x")

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE memory_session_events SET token_count = 5 WHERE event_id = :eid"),
                {"eid": event.event_id},
            )


@pytest.mark.asyncio(loop_scope="module")
async def test_a_tokenizer_without_a_count_is_rejected(
    factory: async_sessionmaker[AsyncSession], memory: MemoryService
) -> None:
    tid, aid = await _seed_tenant(factory)
    event = await memory.record_event(_ctx(tid, aid), session_id="s1", kind="user_message", body="x")

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE memory_session_events SET tokenizer_id = 'x' WHERE event_id = :eid"),
                {"eid": event.event_id},
            )


@pytest.mark.asyncio(loop_scope="module")
async def test_both_together_are_accepted(factory: async_sessionmaker[AsyncSession], memory: MemoryService) -> None:
    """The pairing rule permits the counted case; it is not a ban on counting."""
    tid, aid = await _seed_tenant(factory)
    event = await memory.record_event(_ctx(tid, aid), session_id="s1", kind="user_message", body="x")

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE memory_session_events SET token_count = 5, tokenizer_id = 'heuristic-v1' "
                "WHERE event_id = :eid"
            ),
            {"eid": event.event_id},
        )

    _, tokens, tokenizer, _ = await _stored(factory, event.event_id)
    assert (tokens, tokenizer) == (5, "heuristic-v1")


@pytest.mark.asyncio(loop_scope="module")
async def test_the_two_tables_agree_on_column_names_and_nullability(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ratio's two sides must describe the same quantity the same way. Two
    names for one measurement is how a ratio ends up comparing unlike things."""
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, column_name, is_nullable, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_name IN ('memory_session_events', 'memory_claims') "
                    "  AND column_name IN ('size_bytes', 'token_count', 'tokenizer_id') "
                    "ORDER BY column_name, table_name"
                )
            )
        ).all()

    shape = {(r.table_name, r.column_name): (r.is_nullable, r.data_type) for r in rows}
    for column in ("size_bytes", "token_count", "tokenizer_id"):
        events = shape.get(("memory_session_events", column))
        claims = shape.get(("memory_claims", column))
        assert events is not None, f"memory_session_events is missing {column}"
        assert claims is not None, f"memory_claims is missing {column}"
        assert events == claims, f"{column} differs: events={events} claims={claims}"


@pytest.mark.asyncio(loop_scope="module")
async def test_a_bytes_over_bytes_ratio_is_computable(
    factory: async_sessionmaker[AsyncSession], memory: MemoryService
) -> None:
    """The whole point. Before this migration the query below could not be
    written at all, because one side of the division did not exist."""
    tid, aid = await _seed_tenant(factory)
    for body in ("the auth service times tokens out after 900 seconds", "and it is owned by core"):
        await memory.record_event(_ctx(tid, aid), session_id="ratio", kind="user_message", body=body)

    async with factory() as session:
        corpus = (
            await session.execute(
                text(
                    "SELECT sum(size_bytes) AS total FROM memory_session_events "
                    "WHERE tenant_id = :tid AND session_id = 'ratio'"
                ),
                {"tid": tid},
            )
        ).scalar_one()
        # The claim side of the same division, over the same tenant.
        claims = (
            await session.execute(
                text("SELECT COALESCE(sum(size_bytes), 0) AS total FROM memory_claims WHERE author_tenant_id = :tid"),
                {"tid": tid},
            )
        ).scalar_one()

    assert corpus > 0
    ratio = claims / corpus
    assert 0.0 <= ratio < 1.0
