"""Receipt creation, and the preallocated ID that makes retry safe.

The ID is generated before the bundle exists because the bundle refers to
its own receipt. That ordering is also what lets a serialization-failure
retry produce the same receipt rather than a second one -- tested here by
running creation twice with the same preallocated ID and asserting the
second is rejected as a duplicate rather than quietly inserting a twin.
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

from registry.arc.service.receipt import (
    CREATION_SEQUENCE,
    INTEGRITY_VALID,
    RECEIPT_CREATED_EVENT,
    ReceiptService,
    SelectedDirective,
    SelectedRevision,
    preallocate_receipt_id,
)
from registry.arc.service.signing import RECEIPT_EVENT_SIGNATURE_PROFILE
from registry.types import FakeClock
from tests.helpers.arc_fixtures import (
    ARC_NOW,
    SIGNING_KEY_ID,
    ArcSeed,
    blocked_bundle,
    consume_challenge,
    provenance,
    ready_bundle,
    replay_envelope,
    seed_arc,
    seed_challenge,
    signing_provider,
)

_FINGERPRINT = "f" * 64


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-receipt")


@pytest.fixture
def service() -> ReceiptService:
    return ReceiptService(signing_provider(), FakeClock(ARC_NOW))


async def _create(
    factory: async_sessionmaker[AsyncSession],
    service: ReceiptService,
    seed: ArcSeed,
    *,
    receipt_id: uuid.UUID,
    challenge_id: uuid.UUID,
    bundle=None,
    selected_revisions: tuple[SelectedRevision, ...] = (),
    selected_directives: tuple[SelectedDirective, ...] = (),
    attestation_id: str | None = None,
) -> str:
    async with factory() as session, session.begin():
        digest = await service.create_receipt(
            session,
            receipt_id=receipt_id,
            challenge_id=challenge_id,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            host_id="host-1",
            session_id="sess-1",
            manifest_fingerprint=_FINGERPRINT,
            attestation_id=attestation_id or f"att-{receipt_id}",
            bundle=bundle if bundle is not None else ready_bundle(2),
            provenance=provenance(),
            replay=replay_envelope(),
            evaluated_at=ARC_NOW,
            freshness_basis="revision_pinned_only",
            selected_revisions=selected_revisions,
            selected_directives=selected_directives,
        )
        await consume_challenge(session, challenge_id)
    return digest


@pytest.mark.asyncio
async def test_a_receipt_is_written_with_the_preallocated_id(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)

    await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT receipt_id, resolution_status, integrity_state, budget_limit_bytes, "
                    "       manifest_fingerprint, challenge_id "
                    "FROM arc_receipts WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one()

    assert row.receipt_id == receipt_id
    assert row.resolution_status == "ready"
    assert row.integrity_state == INTEGRITY_VALID
    assert row.manifest_fingerprint == _FINGERPRINT
    assert row.challenge_id == challenge_id


@pytest.mark.asyncio
async def test_the_id_is_known_before_the_bundle_is_built(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The ordering the contract is really about: a caller can put the
    receipt ID inside the bundle because it exists first."""
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)

    # A bundle that references its own receipt -- only expressible because
    # the ID was minted before assembly.
    bundle = ready_bundle(1)
    self_referential = tuple([{"receipt_id": str(receipt_id)}, *bundle.directives])
    bundle = type(bundle)(
        status=bundle.status,
        directives=self_referential,
        cap_facts=(),
        rendered_content_bytes=bundle.rendered_content_bytes,
        budget_limit_bytes=bundle.budget_limit_bytes,
    )

    await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id, bundle=bundle)

    async with factory() as session:
        stored = (
            await session.execute(
                text("SELECT mandatory_directive_count FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one()
    assert stored == 2


@pytest.mark.asyncio
async def test_reusing_a_preallocated_id_cannot_create_a_second_receipt(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """What makes retry-with-the-same-id safe.

    A retry after a serialization failure re-runs creation with the same ID.
    If the original had in fact committed, the primary key rejects the
    second -- so a retry can never silently produce two receipts for one
    resolution.
    """
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id)

    second_challenge = await seed_challenge(factory, tenant_id=seed.tenant_id)
    with pytest.raises(IntegrityError):
        await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=second_challenge)


@pytest.mark.asyncio
async def test_one_challenge_cannot_back_two_receipts(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The single-use invariant, enforced by the schema rather than by code."""
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    await _create(factory, service, seed, receipt_id=preallocate_receipt_id(), challenge_id=challenge_id)

    with pytest.raises(IntegrityError):
        await _create(factory, service, seed, receipt_id=preallocate_receipt_id(), challenge_id=challenge_id)


@pytest.mark.asyncio
async def test_creation_writes_the_sequence_zero_event_and_head(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """A receipt with no creation event has no chain to append to."""
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    digest = await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id)

    async with factory() as session:
        event = (
            await session.execute(
                text(
                    "SELECT sequence, event_type, event_source, previous_event_digest, "
                    "       event_digest, signer_key_id, signature_profile, idempotency_key_digest "
                    "FROM arc_receipt_events WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one()
        head = (
            await session.execute(
                text(
                    "SELECT next_sequence, last_event_digest FROM arc_receipt_event_heads "
                    "WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one()

    assert event.sequence == CREATION_SEQUENCE == 0
    assert event.event_type == RECEIPT_CREATED_EVENT
    assert event.event_source == "system"
    assert event.previous_event_digest is None
    assert event.event_digest == digest
    assert event.signer_key_id == SIGNING_KEY_ID
    assert event.signature_profile == RECEIPT_EVENT_SIGNATURE_PROFILE
    # System events carry no idempotency key; the schema requires exactly that.
    assert event.idempotency_key_digest is None

    assert head.next_sequence == 1
    assert head.last_event_digest == digest


@pytest.mark.asyncio
async def test_the_creation_event_signature_verifies(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Signed over the raw digest bytes, not their hex text."""
    signing = signing_provider()
    service = ReceiptService(signing, FakeClock(ARC_NOW))
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    digest = await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id)

    async with factory() as session:
        signature_hex = (
            await session.execute(
                text("SELECT signature FROM arc_receipt_events WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one()

    assert signing.verify(bytes.fromhex(digest), bytes.fromhex(signature_hex), key_id=SIGNING_KEY_ID) is True


@pytest.mark.asyncio
async def test_selected_revisions_and_directives_are_recorded(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)

    await _create(
        factory,
        service,
        seed,
        receipt_id=receipt_id,
        challenge_id=challenge_id,
        selected_revisions=(
            SelectedRevision(revision_id=seed.revision_id, artifact_id=seed.artifact_id, is_mandatory=True),
        ),
        selected_directives=(
            SelectedDirective(
                revision_id=seed.revision_id,
                directive_id=seed.directive_id,
                artifact_id=seed.artifact_id,
                is_mandatory=True,
                visibility_decision_id="vd-1",
                source_locator="loc://a",
                source_revision_locator="loc://a@1",
                content_digest="e" * 64,
                obligation_fields={"kind": "require"},
                context_handle_digest="h" * 64,
            ),
        ),
    )

    async with factory() as session:
        revision = (
            await session.execute(
                text(
                    "SELECT revision_id, is_mandatory, was_omitted FROM arc_receipt_selected_revisions "
                    "WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one()
        directive = (
            await session.execute(
                text(
                    "SELECT directive_id, is_mandatory, source_locator, context_handle_digest "
                    "FROM arc_receipt_selected_directives WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one()

    assert revision.revision_id == seed.revision_id
    assert revision.is_mandatory is True
    assert revision.was_omitted is False
    assert directive.directive_id == seed.directive_id
    assert directive.source_locator == "loc://a"
    assert directive.context_handle_digest == "h" * 64


@pytest.mark.asyncio
async def test_a_blocked_resolution_still_produces_a_receipt_with_its_reason(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """Blocked is an outcome, not an error: the caller is owed the evidence
    of why it was blocked just as much as of what it was granted."""
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)

    await _create(
        factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id, bundle=blocked_bundle()
    )

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT resolution_status, blocked_reasons FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).one()

    assert row.resolution_status == "blocked"
    assert row.blocked_reasons == ["blocked_budget_exceeded"]


@pytest.mark.asyncio
async def test_provenance_is_recorded_so_a_later_replay_is_interpretable(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """Without the engine version and config digest, "this resolves
    differently now" cannot be told apart from tampering."""
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT selection_engine_version, registry_build_revision, selection_config_digest, "
                    "       canonical_profile_versions, evaluated_at, freshness_basis "
                    "FROM arc_receipts WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one()

    assert row.selection_engine_version == "arc-selection/0.1.0"
    assert row.selection_config_digest == "c" * 64
    assert row.canonical_profile_versions == {"bundle": "arc_context_bundle_content_v1"}
    assert row.evaluated_at == ARC_NOW
    assert row.freshness_basis == "revision_pinned_only"


@pytest.mark.asyncio
async def test_the_replay_envelope_is_stored_as_ciphertext(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The receipt table must not double as a plaintext copy of every
    governed statement any agent was shown."""
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT response_replay_ciphertext, response_replay_nonce, response_replay_key_id "
                    "FROM arc_receipts WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one()

    assert row.response_replay_ciphertext == b"sealed-response"
    assert row.response_replay_key_id == "replay-key-1"


@pytest.mark.asyncio
async def test_a_receipt_without_its_challenge_consumed_cannot_commit(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The deferred trigger makes consumption and creation inseparable."""
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)

    with pytest.raises(Exception, match="not marked consumed"):
        async with factory() as session, session.begin():
            await service.create_receipt(
                session,
                receipt_id=preallocate_receipt_id(),
                challenge_id=challenge_id,
                tenant_id=seed.tenant_id,
                actor_id=seed.actor_id,
                host_id="host-1",
                session_id="sess-1",
                manifest_fingerprint=_FINGERPRINT,
                attestation_id=f"att-{uuid.uuid4()}",
                bundle=ready_bundle(1),
                provenance=provenance(),
                replay=replay_envelope(),
                evaluated_at=ARC_NOW,
                freshness_basis="revision_pinned_only",
            )
            # Deliberately no consume_challenge here.


@pytest.mark.asyncio
async def test_attestation_id_is_unique_per_host(
    factory: async_sessionmaker[AsyncSession], service: ReceiptService, seed: ArcSeed
) -> None:
    """The index that later makes exact-retry replay resolvable: one host
    cannot have two receipts under one attestation."""
    shared = f"att-shared-{uuid.uuid4()}"
    first = await seed_challenge(factory, tenant_id=seed.tenant_id)
    await _create(
        factory, service, seed, receipt_id=preallocate_receipt_id(), challenge_id=first, attestation_id=shared
    )

    second = await seed_challenge(factory, tenant_id=seed.tenant_id)
    with pytest.raises(IntegrityError):
        await _create(
            factory,
            service,
            seed,
            receipt_id=preallocate_receipt_id(),
            challenge_id=second,
            attestation_id=shared,
        )


@pytest.mark.asyncio
async def test_evaluated_at_is_the_caller_supplied_instant_not_now(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """`as_of` is chosen once for the whole resolution and every read uses
    it; a receipt stamping its own wall clock would disagree with the
    snapshot the selection was actually computed against."""
    service = ReceiptService(signing_provider(), FakeClock(ARC_NOW + datetime.timedelta(hours=3)))
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    await _create(factory, service, seed, receipt_id=receipt_id, challenge_id=challenge_id)

    async with factory() as session:
        evaluated_at = (
            await session.execute(
                text("SELECT evaluated_at FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one()

    assert evaluated_at == ARC_NOW
