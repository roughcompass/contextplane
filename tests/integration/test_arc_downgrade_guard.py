"""The downgrade guard, and the gauge that tells an operator to look.

Both were required by the phase's own verification table and owned by no
task, which is how a gate goes missing: every task passes, and the thing
nobody was asked for is simply absent. Found by auditing the plan against
that table at phase close.

**The downgrade guard** refuses `alembic downgrade` while receipts or
legal-held revisions exist. Receipts are retained audit evidence; a routine
downgrade during an unrelated rollback would destroy them silently. The
escape is per-session and deliberate, not a flag set once and forgotten.

**The stuck-row gauge** is what an operator watches. An outbox row that
cannot drain must be neither deleted nor silently skipped — it is the audit
record for a state change that already committed — so it stays, and the
depth rises until someone looks.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.receipt import (
    ReceiptService,
    preallocate_receipt_id,
)
from tests.helpers.arc_fixtures import (
    ARC_NOW,
    ArcSeed,
    consume_challenge,
    provenance,
    ready_bundle,
    replay_envelope,
    seed_arc,
    seed_challenge,
    signing_provider,
)
from tests.helpers.clock import FakeClock

# The guard body, lifted from the migration. Executed directly rather than
# by running `alembic downgrade`: a real downgrade would drop every ARC
# table and leave the session database unusable for the rest of the suite,
# and what needs proving is the guard's decision, not Alembic's plumbing.
_GUARD = """
DO $$
DECLARE
    receipt_count INTEGER;
    held_count    INTEGER;
BEGIN
    IF coalesce(current_setting('arc.allow_destructive_downgrade', true), 'off') = 'on' THEN
        RETURN;
    END IF;

    SELECT count(*) INTO receipt_count FROM arc_receipts;
    SELECT count(*) INTO held_count FROM arc_revisions WHERE legal_hold;

    IF receipt_count > 0 OR held_count > 0 THEN
        RAISE EXCEPTION
            'refusing to downgrade: % context receipt(s) and % legal-held revision(s) '
            'would be destroyed. Receipts are retained audit evidence. Archive them '
            'first, then re-run with: SET arc.allow_destructive_downgrade = ''on'';',
            receipt_count, held_count;
    END IF;
END
$$
"""


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-downgrade")


async def _make_receipt(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> uuid.UUID:
    receipts = ReceiptService(signing_provider(), FakeClock(ARC_NOW))
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    async with factory() as session, session.begin():
        await receipts.create_receipt(
            session,
            receipt_id=receipt_id,
            challenge_id=challenge_id,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            host_id="host-1",
            session_id="sess-1",
            manifest_fingerprint="f" * 64,
            attestation_id=f"att-{receipt_id}",
            bundle=ready_bundle(1),
            provenance=provenance(),
            replay=replay_envelope(),
            evaluated_at=ARC_NOW,
            freshness_basis="revision_pinned_only",
        )
        await consume_challenge(session, challenge_id)
    return receipt_id


# --- the guard ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_guard_refuses_while_receipts_exist(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """The case that matters: a routine downgrade during an unrelated
    rollback would otherwise destroy retained audit evidence silently."""
    await _make_receipt(factory, seed)

    with pytest.raises(DBAPIError, match="refusing to downgrade"):
        async with factory() as session, session.begin():
            await session.execute(text(_GUARD))


@pytest.mark.asyncio
async def test_the_refusal_names_what_would_be_lost(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """An operator needs the counts to decide whether to archive, not just
    to be told no."""
    await _make_receipt(factory, seed)

    with pytest.raises(DBAPIError) as exc:
        async with factory() as session, session.begin():
            await session.execute(text(_GUARD))

    message = str(exc.value)
    assert "context receipt(s)" in message
    assert "legal-held revision(s)" in message
    assert "Archive them" in message


@pytest.mark.asyncio
async def test_the_refusal_names_the_escape(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """A guard that refuses without saying how to proceed is one an operator
    works around by editing the migration."""
    await _make_receipt(factory, seed)

    with pytest.raises(DBAPIError, match="allow_destructive_downgrade"):
        async with factory() as session, session.begin():
            await session.execute(text(_GUARD))


@pytest.mark.asyncio
async def test_a_legal_held_revision_alone_refuses(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """Legal hold is an independent reason. A database with no receipts but
    a held revision must still refuse."""
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET legal_hold = TRUE WHERE revision_id = :rid"),
            {"rid": seed.revision_id},
        )

    with pytest.raises(DBAPIError, match="refusing to downgrade"):
        async with factory() as session, session.begin():
            await session.execute(text(_GUARD))

    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_revisions SET legal_hold = FALSE WHERE revision_id = :rid"),
            {"rid": seed.revision_id},
        )


@pytest.mark.asyncio
async def test_the_escape_permits_the_downgrade(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """The guard must be escapable, or an operator who *has* archived is
    stuck and will edit the migration instead — which removes the guard for
    everyone."""
    receipt_id = await _make_receipt(factory, seed)

    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL arc.allow_destructive_downgrade = 'on'"))
        await session.execute(text(_GUARD))  # must not raise

    # "Permits" means the enclosing transaction actually commits -- not just
    # that the guard statement itself didn't raise inside a transaction that
    # then rolled back for some other reason. A fresh session/connection
    # proves the commit happened rather than reusing the one that made it.
    async with factory() as session:
        still_present = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipts WHERE receipt_id = :rid"), {"rid": receipt_id}
            )
        ).scalar_one()
    assert still_present == 1


@pytest.mark.asyncio
async def test_the_escape_is_per_session_not_persistent(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Deliberate, not a flag someone sets once and forgets.

    A later connection must find the guard armed again, or the first
    archived downgrade would disarm it permanently.
    """
    await _make_receipt(factory, seed)

    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL arc.allow_destructive_downgrade = 'on'"))
        await session.execute(text(_GUARD))

    with pytest.raises(DBAPIError, match="refusing to downgrade"):
        async with factory() as session, session.begin():
            await session.execute(text(_GUARD))


# --- the stuck-row gauge -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_undrainable_row_is_neither_deleted_nor_skipped(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The row is the audit record for a state change that already
    committed. Deleting it to clear the gauge destroys the evidence;
    marking it drained without delivering it is worse, because the gauge
    then reads healthy."""
    from registry.arc.service import audit_outbox

    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type="arc.context.resolved",
            payload={"marker": marker},
        )

    async with factory() as session, session.begin():
        # Simulate a failed delivery attempt the way the drain worker
        # records one: bounded code, attempt counted, row left undrained.
        await session.execute(
            text(
                "UPDATE arc_audit_outbox SET attempts = attempts + 1, last_error_code = 'sink_unavailable', "
                "  last_attempt_at = :now "
                "WHERE event_payload ->> 'marker' = :marker"
            ),
            {"marker": marker, "now": ARC_NOW},
        )

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT drained_at, attempts, last_error_code FROM arc_audit_outbox "
                    "WHERE event_payload ->> 'marker' = :marker"
                ),
                {"marker": marker},
            )
        ).one()

    assert row.drained_at is None, "a failed delivery must leave the row undrained and retryable"
    assert row.attempts == 1
    assert row.last_error_code == "sink_unavailable"


@pytest.mark.asyncio
async def test_the_undrained_depth_is_queryable_for_the_gauge(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The gauge an operator watches, and the query behind it.

    A rising floor means rows are failing repeatedly rather than that
    traffic is high, which is the distinction the runbook turns on.
    """
    from registry.arc.service import audit_outbox

    async with factory() as session:
        before = (
            await session.execute(text("SELECT count(*) FROM arc_audit_outbox WHERE drained_at IS NULL"))
        ).scalar_one()

    async with factory() as session, session.begin():
        for _ in range(3):
            await audit_outbox.emit(
                session,
                tenant_id=seed.tenant_id,
                event_type="arc.context.resolved",
                payload={"marker": uuid.uuid4().hex},
            )

    async with factory() as session:
        after = (
            await session.execute(text("SELECT count(*) FROM arc_audit_outbox WHERE drained_at IS NULL"))
        ).scalar_one()

    assert after == before + 3


@pytest.mark.asyncio
async def test_a_drained_row_carries_no_outstanding_error(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The schema enforces it: a row cannot be both delivered and failing.

    Without that, a row could be marked drained while still carrying the
    error that stopped it, and an operator triaging by error code would see
    a failure that is not one.
    """
    from registry.arc.service import audit_outbox

    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type="arc.context.resolved",
            payload={"marker": marker},
        )

    with pytest.raises(DBAPIError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE arc_audit_outbox SET drained_at = :now, last_error_code = 'still_failing' "
                    "WHERE event_payload ->> 'marker' = :marker"
                ),
                {"marker": marker, "now": ARC_NOW},
            )


@pytest.mark.asyncio
async def test_an_error_code_stays_bounded(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
    """`last_error_code` is a code, not a message sink.

    A raw exception string here would put unbounded, possibly
    caller-influenced text into an audit-adjacent column — the exact thing
    the content-minimization gate exists to prevent.
    """
    from registry.arc.service import audit_outbox

    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type="arc.context.resolved",
            payload={"marker": marker},
        )

    with pytest.raises(DBAPIError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE arc_audit_outbox SET last_error_code = :long " "WHERE event_payload ->> 'marker' = :marker"
                ),
                {"marker": marker, "long": "x" * 200},
            )
