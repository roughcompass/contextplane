"""Integrity failure detection: gap, fork, tamper, and bad signature.

The contract asks for 100% detection of three shapes. This file injects each
one directly into the database -- bypassing the service, exactly as an
attacker with write access would -- and asserts the chain verifier catches
it. A test that only exercised the service's own append path would prove
that ARC does not corrupt its own chains, which is not the same claim.

Marking a receipt `integrity_failed` happens in a separate committed
transaction. The append that detected the problem is rolling back, and a
mark written on that same session would roll back with it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.receipt import (
    EVENT_SOURCE_HOST,
    INTEGRITY_FAILED,
    ReceiptIntegrityError,
    ReceiptService,
    preallocate_receipt_id,
)
from contextplane.arc.vocabularies import RECEIPT_EVENT_JIT_RETRIEVAL
from contextplane.audit import actions
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

_FINGERPRINT = "f" * 64
_REQUEST_DIGEST = "9" * 64


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-integrity")


@pytest.fixture
def service() -> ReceiptService:
    return ReceiptService(signing_provider(), FakeClock(ARC_NOW))


async def _chain_of(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed, length: int = 3
) -> uuid.UUID:
    """A receipt with a creation event plus `length` appended events."""
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    async with factory() as session, session.begin():
        await service.create_receipt(
            session,
            receipt_id=receipt_id,
            challenge_id=challenge_id,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            host_id="host-1",
            session_id="sess-1",
            manifest_fingerprint=_FINGERPRINT,
            attestation_id=f"att-{receipt_id}",
            bundle=ready_bundle(1),
            provenance=provenance(),
            replay=replay_envelope(),
            evaluated_at=ARC_NOW,
            freshness_basis="revision_pinned_only",
        )
        await consume_challenge(session, challenge_id)

    for _ in range(length):
        async with factory() as session, session.begin():
            await service.append_event(
                session,
                receipt_id=receipt_id,
                tenant_id=seed.tenant_id,
                event_type=RECEIPT_EVENT_JIT_RETRIEVAL,
                event_source=EVENT_SOURCE_HOST,
                request_payload_digest=_REQUEST_DIGEST,
                payload={"n": 1},
                actor_id=seed.actor_id,
                idempotency_key_digest=uuid.uuid4().hex + uuid.uuid4().hex,
            )
    return receipt_id


# --- the three tampering shapes --------------------------------------------


@pytest.mark.asyncio
async def test_a_gap_is_detected(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """An event deleted from the middle of the chain."""
    receipt_id = await _chain_of(factory, service, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM arc_receipt_events WHERE receipt_id = :rid AND sequence = 2"),
            {"rid": receipt_id},
        )

    with pytest.raises(ReceiptIntegrityError, match="gap"):
        async with factory() as session:
            await service.verify_chain(session, receipt_id)


@pytest.mark.asyncio
async def test_a_fork_is_detected(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """An event rewritten to point at a predecessor that is not its own.

    This is the shape that a re-parented chain takes: the sequence numbers
    still look contiguous, and only the link check catches it.
    """
    receipt_id = await _chain_of(factory, service, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_receipt_events SET previous_event_digest = :bogus "
                "WHERE receipt_id = :rid AND sequence = 2"
            ),
            {"rid": receipt_id, "bogus": "b" * 64},
        )

    with pytest.raises(ReceiptIntegrityError, match="does not link to its predecessor"):
        async with factory() as session:
            await service.verify_chain(session, receipt_id)


@pytest.mark.asyncio
async def test_a_tampered_payload_is_detected(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The payload changed but the stored digest left alone.

    Recomputing the digest from the row is what catches this; comparing the
    stored digest to itself never would.
    """
    receipt_id = await _chain_of(factory, service, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_receipt_events SET event_payload = CAST(:payload AS JSONB) "
                "WHERE receipt_id = :rid AND sequence = 1"
            ),
            {"rid": receipt_id, "payload": '{"n": 999}'},
        )

    with pytest.raises(ReceiptIntegrityError, match="tampered payload"):
        async with factory() as session:
            await service.verify_chain(session, receipt_id)


@pytest.mark.asyncio
async def test_a_forged_digest_fails_its_signature(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """A tamperer who also recomputes the digest still cannot sign it.

    Rewriting the payload *and* its digest defeats the recomputation check,
    which is exactly why the signature exists: without the private key the
    forged digest cannot be signed, so the chain still fails to verify.
    """
    receipt_id = await _chain_of(factory, service, seed)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT event_id, tenant_id, event_type, event_source, request_payload_digest, "
                    "       previous_event_digest, signer_key_id, created_at "
                    "FROM arc_receipt_events WHERE receipt_id = :rid AND sequence = 1"
                ),
                {"rid": receipt_id},
            )
        ).one()

    from contextplane.arc.schemas.canonical import receipt_event_digest

    forged_payload = {"n": 999}
    forged_digest = receipt_event_digest(
        {
            "event_id": str(row.event_id),
            "receipt_id": str(receipt_id),
            "tenant_id": str(row.tenant_id),
            "sequence": 1,
            "event_type": row.event_type,
            "event_source": row.event_source,
            "request_payload_digest": row.request_payload_digest,
            "previous_event_digest": row.previous_event_digest,
            "event_payload": forged_payload,
            "signer_key_id": row.signer_key_id,
            "created_at": row.created_at,
        }
    )

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_receipt_events SET event_payload = CAST(:payload AS JSONB), event_digest = :digest "
                "WHERE receipt_id = :rid AND sequence = 1"
            ),
            {"rid": receipt_id, "payload": '{"n": 999}', "digest": forged_digest},
        )

    with pytest.raises(ReceiptIntegrityError, match="invalid signature"):
        async with factory() as session:
            await service.verify_chain(session, receipt_id)


@pytest.mark.asyncio
async def test_a_truncated_chain_is_detected_by_the_head(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """Events lopped off the end, leaving a self-consistent prefix.

    Every remaining event links correctly and every signature verifies --
    checking events alone would pass. Only the head, which still points past
    them, reveals it.
    """
    receipt_id = await _chain_of(factory, service, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM arc_receipt_events WHERE receipt_id = :rid AND sequence >= 2"),
            {"rid": receipt_id},
        )

    with pytest.raises(ReceiptIntegrityError, match="head does not match"):
        async with factory() as session:
            await service.verify_chain(session, receipt_id)


@pytest.mark.asyncio
async def test_an_intact_chain_passes_all_checks(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The negative control. Without it, a verifier that rejected everything
    would score 100% detection."""
    receipt_id = await _chain_of(factory, service, seed, length=5)
    async with factory() as session:
        event_count = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipt_events WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one()
        result = await service.verify_chain(session, receipt_id)

    # verify_chain signals success by returning None and failure by raising
    # ReceiptIntegrityError -- the five tests above exercise the raising
    # path. Pinning the event count with the `None` return makes explicit
    # that this ran against a real six-event chain (one creation event plus
    # five appends) rather than an edge case too small to be meaningful.
    assert event_count == 6
    assert result is None


@pytest.mark.asyncio
async def test_a_receipt_with_no_events_is_rejected(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    receipt_id = await _chain_of(factory, service, seed, length=0)
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM arc_receipt_events WHERE receipt_id = :rid"), {"rid": receipt_id})

    with pytest.raises(ReceiptIntegrityError, match="no events"):
        async with factory() as session:
            await service.verify_chain(session, receipt_id)


# --- marking, and what the mark means ----------------------------------------


@pytest.mark.asyncio
async def test_marking_survives_the_rollback_of_the_transaction_that_detected_it(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The reason marking gets its own transaction.

    The real sequence: a transaction detects a broken chain, rolls back
    (losing its own writes), and only then marks. The mark, committed
    separately, survives -- which is the whole point. Marking on the
    detecting session would roll back with it and leave a receipt that
    fails verification still reading as `valid`.
    """
    receipt_id = await _chain_of(factory, service, seed)
    async with factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM arc_receipt_events WHERE receipt_id = :rid AND sequence = 2"),
            {"rid": receipt_id},
        )

    detected = False
    try:
        async with factory() as session, session.begin():
            # A write the doomed transaction makes before discovering the problem.
            await session.execute(
                text("UPDATE arc_receipts SET host_id = 'doomed' WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
            await service.verify_chain(session, receipt_id)
    except ReceiptIntegrityError:
        detected = True

    assert detected
    # Only now, with the detecting transaction rolled back and its lock
    # released, is it safe to mark.
    await service.mark_integrity_failed(factory, receipt_id, reason="chain_broken")

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT integrity_state, host_id FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).one()

    assert row.integrity_state == INTEGRITY_FAILED
    # The doomed transaction's write really did roll back, so the mark is
    # not simply a transaction that unexpectedly committed.
    assert row.host_id == "host-1"


@pytest.mark.asyncio
async def test_marking_while_the_caller_still_holds_the_row_fails_fast(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The misuse the lock timeout exists for.

    Marking runs on its own connection. A caller that calls it while still
    holding a lock on the same receipt row deadlocks against itself: the
    mark waits for a transaction that is waiting for the mark. Postgres
    cannot detect this as a deadlock because the two are not both waiting on
    *locks* -- one is waiting on the application. Without `lock_timeout`
    this hangs until something times out at the transport layer, if ever.
    """
    receipt_id = await _chain_of(factory, service, seed)

    with pytest.raises(DBAPIError):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE arc_receipts SET host_id = 'held' WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
            await service.mark_integrity_failed(factory, receipt_id, reason="misuse")


@pytest.mark.asyncio
async def test_a_marked_receipt_is_not_usable(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """A receipt whose chain may have been altered authorizes nothing."""
    receipt_id = await _chain_of(factory, service, seed)

    async with factory() as session:
        assert await service.is_usable(session, receipt_id) is True

    await service.mark_integrity_failed(factory, receipt_id, reason="chain_broken")

    async with factory() as session:
        assert await service.is_usable(session, receipt_id) is False


@pytest.mark.asyncio
async def test_an_unknown_receipt_is_not_usable(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService
) -> None:
    """Absent is not the same as valid."""
    async with factory() as session:
        assert await service.is_usable(session, preallocate_receipt_id()) is False


@pytest.mark.asyncio
async def test_marking_emits_an_audit_row_for_the_operator(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """An integrity failure nobody is told about is not much of a detection."""
    receipt_id = await _chain_of(factory, service, seed)
    await service.mark_integrity_failed(factory, receipt_id, reason="chain_broken")

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT tenant_id, event_type, event_payload FROM arc_audit_outbox "
                    "WHERE event_payload ->> 'receipt_id' = :rid"
                ),
                {"rid": str(receipt_id)},
            )
        ).one()

    assert row.event_type == actions.ARC_RECEIPT_INTEGRITY_FAILED
    assert row.tenant_id == seed.tenant_id
    assert row.event_payload["reason"] == "chain_broken"


@pytest.mark.asyncio
async def test_marking_twice_is_harmless(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """Detection can happen on more than one path; the second must not fail."""
    receipt_id = await _chain_of(factory, service, seed)
    await service.mark_integrity_failed(factory, receipt_id, reason="first")
    await service.mark_integrity_failed(factory, receipt_id, reason="second")

    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT integrity_state FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one()
    assert state == INTEGRITY_FAILED
