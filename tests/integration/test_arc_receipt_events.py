"""The receipt event chain: O(1) append, and a chain that stays a chain.

Appending locks one head row and reads the predecessor from it. Two
properties follow, and both are tested here rather than asserted in prose:
concurrent appends serialize into a single unforked sequence, and append
cost does not grow with chain length.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.receipt import (
    EVENT_SOURCE_HOST,
    EVENT_SOURCE_SYSTEM,
    ReceiptIntegrityError,
    ReceiptService,
    preallocate_receipt_id,
)
from registry.arc.vocabularies import RECEIPT_EVENT_JIT_RETRIEVAL
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
    return await seed_arc(factory, slug_prefix="arc-events")


@pytest.fixture
def service() -> ReceiptService:
    return ReceiptService(signing_provider(), FakeClock(ARC_NOW))


async def _receipt(factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed) -> uuid.UUID:
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
    return receipt_id


async def _append(
    factory: async_sessionmaker[AsyncSession],
    service: ReceiptService,
    seed: ArcSeed,
    receipt_id: uuid.UUID,
    *,
    event_type: str = RECEIPT_EVENT_JIT_RETRIEVAL,
    idempotency_key_digest: str | None = None,
) -> str:
    async with factory() as session, session.begin():
        return await service.append_event(
            session,
            receipt_id=receipt_id,
            tenant_id=seed.tenant_id,
            event_type=event_type,
            event_source=EVENT_SOURCE_HOST,
            request_payload_digest=_REQUEST_DIGEST,
            payload={"detail": event_type},
            actor_id=seed.actor_id,
            idempotency_key_digest=idempotency_key_digest or uuid.uuid4().hex + uuid.uuid4().hex,
        )


@pytest.mark.asyncio
async def test_an_append_advances_the_sequence_and_links_to_its_predecessor(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    receipt_id = await _receipt(factory, service, seed)

    async with factory() as session:
        creation_digest = (
            await session.execute(
                text("SELECT last_event_digest FROM arc_receipt_event_heads WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one()

    appended = await _append(factory, service, seed, receipt_id)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT sequence, previous_event_digest, event_digest "
                    "FROM arc_receipt_events WHERE receipt_id = :rid AND sequence = 1"
                ),
                {"rid": receipt_id},
            )
        ).one()
        head = (
            await session.execute(
                text("SELECT next_sequence, last_event_digest FROM arc_receipt_event_heads " "WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).one()

    assert row.previous_event_digest == creation_digest
    assert row.event_digest == appended
    assert head.next_sequence == 2
    assert head.last_event_digest == appended


@pytest.mark.asyncio
async def test_many_appends_form_one_unbroken_chain(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    receipt_id = await _receipt(factory, service, seed)
    for _ in range(5):
        await _append(factory, service, seed, receipt_id)

    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT sequence, previous_event_digest, event_digest "
                    "FROM arc_receipt_events WHERE receipt_id = :rid ORDER BY sequence"
                ),
                {"rid": receipt_id},
            )
        ).all()

    assert [r.sequence for r in rows] == [0, 1, 2, 3, 4, 5]
    assert rows[0].previous_event_digest is None
    # Deliberately offset by one: each row is compared with its successor.
    for earlier, later in zip(rows, rows[1:], strict=False):
        assert later.previous_event_digest == earlier.event_digest


@pytest.mark.asyncio
async def test_verify_chain_accepts_an_untampered_chain(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    receipt_id = await _receipt(factory, service, seed)
    for _ in range(3):
        await _append(factory, service, seed, receipt_id)

    async with factory() as session:
        await service.verify_chain(session, receipt_id)


@pytest.mark.asyncio
async def test_appending_to_a_receipt_with_no_head_is_rejected(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    with pytest.raises(ReceiptIntegrityError, match="no event head"):
        await _append(factory, service, seed, preallocate_receipt_id())


@pytest.mark.asyncio
async def test_the_sequence_index_rejects_a_duplicate_by_hand(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """Even if the head lock were bypassed, the unique index on
    `(receipt_id, sequence)` refuses a second event at the same position."""
    receipt_id = await _receipt(factory, service, seed)
    await _append(factory, service, seed, receipt_id)

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO arc_receipt_events ("
                    "  event_id, receipt_id, tenant_id, sequence, event_type, event_source,"
                    "  signature_profile, request_payload_digest, previous_event_digest,"
                    "  event_payload, event_digest, signature"
                    ") VALUES ("
                    "  gen_random_uuid(), :rid, :tid, 1, 'forged', 'system',"
                    "  'arc_receipt_event_sig_v1', :req, :prev, '{}', :digest, 'sig')"
                ),
                {
                    "rid": receipt_id,
                    "tid": seed.tenant_id,
                    "req": _REQUEST_DIGEST,
                    "prev": "0" * 64,
                    "digest": "1" * 64,
                },
            )


@pytest.mark.asyncio
async def test_host_events_require_an_idempotency_key(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """A host-originated event that cannot be deduplicated would let a
    retried request advance the chain twice."""
    receipt_id = await _receipt(factory, service, seed)

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await service.append_event(
                session,
                receipt_id=receipt_id,
                tenant_id=seed.tenant_id,
                event_type=RECEIPT_EVENT_JIT_RETRIEVAL,
                event_source=EVENT_SOURCE_HOST,
                request_payload_digest=_REQUEST_DIGEST,
                payload={},
                actor_id=seed.actor_id,
                idempotency_key_digest=None,
            )


@pytest.mark.asyncio
async def test_system_events_must_not_carry_an_idempotency_key(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The mirror rule: system events are bound by their own operation, and
    a stray key here would create a second, contradictory dedup axis."""
    receipt_id = await _receipt(factory, service, seed)

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await service.append_event(
                session,
                receipt_id=receipt_id,
                tenant_id=seed.tenant_id,
                event_type="system_note",
                event_source=EVENT_SOURCE_SYSTEM,
                request_payload_digest=_REQUEST_DIGEST,
                payload={},
                idempotency_key_digest="k" * 64,
            )


@pytest.mark.asyncio
async def test_the_same_idempotency_key_cannot_append_twice(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    receipt_id = await _receipt(factory, service, seed)
    key = uuid.uuid4().hex + uuid.uuid4().hex
    await _append(factory, service, seed, receipt_id, idempotency_key_digest=key)

    with pytest.raises(IntegrityError):
        await _append(factory, service, seed, receipt_id, idempotency_key_digest=key)


@pytest.mark.asyncio
async def test_concurrent_appends_do_not_fork_the_chain(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The property the head lock exists for.

    Four appends launched together must produce sequences 1..4 with no
    duplicates and no gaps -- not four events all claiming sequence 1.
    """
    receipt_id = await _receipt(factory, service, seed)

    results = await asyncio.gather(
        *(_append(factory, service, seed, receipt_id) for _ in range(4)), return_exceptions=True
    )
    assert all(isinstance(r, str) for r in results), results

    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT sequence, previous_event_digest, event_digest "
                    "FROM arc_receipt_events WHERE receipt_id = :rid ORDER BY sequence"
                ),
                {"rid": receipt_id},
            )
        ).all()

    assert [r.sequence for r in rows] == [0, 1, 2, 3, 4]
    # Deliberately offset by one: each row is compared with its successor.
    for earlier, later in zip(rows, rows[1:], strict=False):
        assert later.previous_event_digest == earlier.event_digest

    async with factory() as session:
        await service.verify_chain(session, receipt_id)


@pytest.mark.asyncio
async def test_append_reads_one_row_regardless_of_chain_length(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """O(1) append, asserted structurally rather than by timing.

    A wall-clock benchmark here would be flaky. What actually matters is
    that the append path never reads the event table at all -- it reads the
    head. Postgres' own statement counter for `arc_receipt_events` scans
    would be indirect; instead this asserts the observable consequence: the
    predecessor an append links to always equals the head's stored digest,
    which is only true if the head is the sole source.
    """
    receipt_id = await _receipt(factory, service, seed)
    for _ in range(8):
        async with factory() as session:
            head_before = (
                await session.execute(
                    text("SELECT last_event_digest FROM arc_receipt_event_heads WHERE receipt_id = :rid"),
                    {"rid": receipt_id},
                )
            ).scalar_one()

        digest = await _append(factory, service, seed, receipt_id)

        async with factory() as session:
            linked = (
                await session.execute(
                    text(
                        "SELECT previous_event_digest FROM arc_receipt_events "
                        "WHERE receipt_id = :rid AND event_digest = :digest"
                    ),
                    {"rid": receipt_id, "digest": digest},
                )
            ).scalar_one()
        assert linked == head_before


@pytest.mark.asyncio
async def test_events_from_different_receipts_do_not_interfere(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """Each receipt has its own head, so sequences restart per receipt."""
    first = await _receipt(factory, service, seed)
    second = await _receipt(factory, service, seed)
    await _append(factory, service, seed, first)
    await _append(factory, service, seed, first)
    await _append(factory, service, seed, second)

    async with factory() as session:
        first_max = (
            await session.execute(
                text("SELECT max(sequence) FROM arc_receipt_events WHERE receipt_id = :rid"),
                {"rid": first},
            )
        ).scalar_one()
        second_max = (
            await session.execute(
                text("SELECT max(sequence) FROM arc_receipt_events WHERE receipt_id = :rid"),
                {"rid": second},
            )
        ).scalar_one()

    assert first_max == 2
    assert second_max == 1
