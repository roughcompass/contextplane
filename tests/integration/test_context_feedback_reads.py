"""A judgement read back to the reader who made it, and to nobody else.

E22-T9. `/v1/context/feedback` was `post`-only: a reader could record that a
served item was irrelevant and never see what their assessment did. That is the
open end of the one loop the product has for *"how might this be improved."*

Against a real database, because every property here is a property of the SQL —
what the scope predicate admits, and what it withholds.

**The differencing constraint is honoured by not being an aggregate.** An
explorer that recomputes is the attack; this returns rows the caller wrote,
filtered on their own reporter id, so there is no cell to compute twice and no
remainder to subtract. The tests that matter are therefore about *disclosure*
rather than about arithmetic: another reporter's rows are absent, and their
notes are absent even where their ratings are visible.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ValidationError
from contextplane.signals.feedback_reads import (
    MAX_PAGE_SIZE,
    FeedbackReadService,
    RefusedScope,
    parse_cursor,
)
from contextplane.types import TenantContext

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _tenant(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"fbr-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'reader', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"fbr-{aid.hex[:8]}", "n": _NOW},
        )
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["consumer"])


async def _judgement(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    reporter: str | None = None,
    receipt_id: uuid.UUID | None = None,
    rating: str = "irrelevant",
    note: str | None = "this had nothing to do with the question",
    at: datetime.datetime = _NOW,
) -> uuid.UUID:
    """One recorded judgement, written directly so the read is what is tested.

    `learning_eligible` follows the kind rather than being passed: a diagnostic
    observation cites nothing, so nothing can check what it refers to, and the
    schema refuses to let one be learning-eligible.
    """
    feedback_id = uuid.uuid4()
    async with factory() as session, session.begin():
        if receipt_id is not None:
            # The feedback row carries a foreign key to the receipt it judges.
            await session.execute(
                text(
                    "INSERT INTO context_receipts "
                    "(receipt_id, tenant_id, state, cacheable, resolved_at, requested_by) "
                    "VALUES (:r, :t, 'complete', FALSE, :n, 'test') ON CONFLICT DO NOTHING"
                ),
                {"r": receipt_id, "t": ctx.tenant_id, "n": at},
            )
        await session.execute(
            text(
                "INSERT INTO context_feedback ("
                "  feedback_id, tenant_id, kind, receipt_id, receipt_item_id, rating,"
                "  learning_eligible, note, reporter_id, reporter_type, idempotency_key,"
                "  content_digest, created_at"
                ") VALUES ("
                "  :f, :t, :kind, :r, NULL, :rating, :learns, :note, :reporter, 'human',"
                "  :key, :digest, :at)"
            ),
            {
                "f": feedback_id,
                "t": ctx.tenant_id,
                "kind": "receipt_level" if receipt_id else "diagnostic_observation",
                "learns": receipt_id is not None,
                "r": receipt_id,
                "rating": rating,
                "note": note,
                "reporter": reporter or str(ctx.actor_id),
                "key": f"key-{feedback_id.hex[:10]}",
                "digest": feedback_id.hex,
                "at": at,
            },
        )
    return feedback_id


def _reads(factory: async_sessionmaker[AsyncSession]) -> FeedbackReadService:
    return FeedbackReadService(factory)


@pytest.mark.asyncio
async def test_a_reader_can_see_what_their_own_judgement_did(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole of E22-T9 in one assertion. Before this, there was no read."""
    ctx = await _tenant(factory)
    recorded = await _judgement(factory, ctx)

    page = await _reads(factory).mine(ctx)

    assert [item.feedback_id for item in page.items] == [recorded]
    assert page.items[0].rating == "irrelevant"
    assert page.items[0].note == "this had nothing to do with the question"


@pytest.mark.asyncio
async def test_another_reporters_judgements_are_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The disclosure direction, and the reason the reporter is never an argument.

    The write path refuses a caller reporting as somebody else. Accepting a
    reporter id on the read would reopen exactly that, so the scope comes from
    the caller's own identity and a second reporter's rows are seeded here to
    prove it.
    """
    ctx = await _tenant(factory)
    mine = await _judgement(factory, ctx)
    theirs = await _judgement(factory, ctx, reporter=str(uuid.uuid4()))

    found = {item.feedback_id for item in (await _reads(factory).mine(ctx)).items}

    assert mine in found
    assert theirs not in found


@pytest.mark.asyncio
async def test_another_tenants_judgements_are_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two tenants can hold the same reporter id string; their judgements must
    not meet."""
    mine, theirs = await _tenant(factory), await _tenant(factory)
    ours = await _judgement(factory, mine)
    await _judgement(factory, theirs, reporter=str(mine.actor_id))

    assert [item.feedback_id for item in (await _reads(factory).mine(mine)).items] == [ours]


@pytest.mark.asyncio
async def test_a_receipts_judgements_are_visible_and_their_notes_are_not(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second permitted scope, and the line it draws.

    A receipt is a resolution the caller can already read in full, so the
    *ratings* attached to it disclose nothing further about the resolution. The
    note is a fact about a person, and `context_feedback.note` is the field
    0041 calls the one most likely to carry something personal.
    """
    ctx = await _tenant(factory)
    receipt = uuid.uuid4()
    await _judgement(factory, ctx, receipt_id=receipt, reporter=str(uuid.uuid4()), note="I was annoyed")

    page = await _reads(factory).for_receipt(ctx, receipt_id=receipt)

    assert len(page.items) == 1
    assert page.items[0].rating == "irrelevant"
    assert page.items[0].note is None, "a receipt read returned another reporter's note"


@pytest.mark.asyncio
async def test_a_receipt_read_is_scoped_to_that_receipt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _tenant(factory)
    wanted, other = uuid.uuid4(), uuid.uuid4()
    mine = await _judgement(factory, ctx, receipt_id=wanted)
    await _judgement(factory, ctx, receipt_id=other)

    assert [item.feedback_id for item in (await _reads(factory).for_receipt(ctx, receipt_id=wanted)).items] == [mine]


@pytest.mark.asyncio
async def test_the_newest_judgement_comes_first(factory: async_sessionmaker[AsyncSession]) -> None:
    """A reader checking what their judgement did is asking about the one they
    just made."""
    ctx = await _tenant(factory)
    older = await _judgement(factory, ctx, at=_NOW - datetime.timedelta(days=2))
    newer = await _judgement(factory, ctx, at=_NOW)

    assert [item.feedback_id for item in (await _reads(factory).mine(ctx)).items] == [newer, older]


@pytest.mark.asyncio
async def test_the_cursor_walks_the_whole_list_without_repeating_or_skipping(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Keyset on `(created_at, feedback_id)`, so a row written mid-pagination
    cannot displace one the caller has not seen."""
    ctx = await _tenant(factory)
    written = [await _judgement(factory, ctx, at=_NOW - datetime.timedelta(minutes=index)) for index in range(5)]

    seen: list[uuid.UUID] = []
    cursor = None
    for _ in range(5):
        page = await _reads(factory).mine(ctx, cursor=cursor, page_size=2)
        seen.extend(item.feedback_id for item in page.items)
        if page.next_cursor is None:
            break
        cursor = parse_cursor(page.next_cursor)

    assert seen == written
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_a_caller_with_no_identity_is_refused_by_name(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty page would read as "you have recorded nothing", which is a
    different and false statement."""
    ctx = await _tenant(factory)
    anonymous = TenantContext(tenant_id=ctx.tenant_id, actor_id=None, roles=["consumer"])

    with pytest.raises(RefusedScope) as refused:
        await _reads(factory).mine(anonymous)

    assert refused.value.code == "not_your_feedback"


@pytest.mark.asyncio
async def test_a_page_larger_than_the_ceiling_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A reader scanning their own judgements is reading, not exporting."""
    ctx = await _tenant(factory)
    with pytest.raises(ValidationError, match="page_size"):
        await _reads(factory).mine(ctx, page_size=MAX_PAGE_SIZE + 1)


def test_a_cursor_this_surface_did_not_issue_is_refused() -> None:
    """Silently discarding it would restart the page at the beginning, which a
    caller paging through their own judgements reads as the list having changed
    under them."""
    with pytest.raises(ValidationError, match="not issued by this surface"):
        parse_cursor("not-a-cursor")

    assert parse_cursor(None) is None
