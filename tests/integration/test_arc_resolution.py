"""The resolution transaction, end to end and atomic.

Each test here is about the *ordering and atomicity* of the composed
transaction rather than about any one service — those have their own files.
What matters is that the pieces land together or not at all, and that a
rejected attempt leaves nothing behind but an audit trail.
"""

from __future__ import annotations

import base64
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.schemas.canonical import canonicalize_host_attestation_envelope
from contextplane.arc.schemas.canonical import manifest_claims_digest as compute_manifest_claims_digest
from contextplane.arc.service.attestation import (
    AttestationEnvelope,
    AttestationService,
    HostSignerKeyRegistry,
    ManifestClaims,
)
from contextplane.arc.service.challenge import (
    CHALLENGE_TTL,
    ChallengeNonceDeriver,
    ChallengeService,
    nonce_digest,
)
from contextplane.arc.service.receipt import ReceiptService, ReplayEnvelope
from contextplane.arc.service.resolution import (
    ManifestUnverified,
    ResolutionRequest,
    ResolutionService,
    parse_manifest,
)
from contextplane.arc.service.selection import SelectionInput
from contextplane.arc.types import ArcRequestContext, ResolutionStatus
from contextplane.audit import actions
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, AllowAllIntegrity, ArcSeed, provenance, seed_arc, signing_provider
from tests.helpers.clock import FakeClock

_HOST_ID = "host-1"
_SESSION_ID = "sess-1"
_PROFILE = "arc_host_attestation_v1"
_SIGNING_DOMAIN = b"ARC-HOST-ATTESTATION-V1\x00"
_NONCE_KEY = "nk1"
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
    return await seed_arc(factory, slug_prefix="arc-resolution")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(ARC_NOW)


def _manifest() -> ManifestClaims:
    return ManifestClaims(
        session_id=_SESSION_ID,
        intent_kind="code_change",
        requested_action_classes=("merge",),
        entity_ids=(),
        domain_ids=("payments",),
        environment="production",
        data_sensitivity="confidential",
        repository_identity="git@example.test:org/repo.git",
        supported_context_bundle_content_profiles=("arc_context_bundle_content_v1",),
    )


def _ctx(seed: ArcSeed) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=["consumer"], oidc_subject="sub-1")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id=_HOST_ID)


class _Harness:
    """Everything a resolution needs, wired together with real services."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], seed: ArcSeed, clock: FakeClock) -> None:
        self.factory = factory
        self.seed = seed
        self.clock = clock
        private = Ed25519PrivateKey.generate()
        self.host_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        self.host_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.deriver = ChallengeNonceDeriver({_NONCE_KEY: b"nonce-secret"}, active_key_id=_NONCE_KEY)
        self.challenges = ChallengeService(factory, self.deriver, clock)
        self.service = ResolutionService(
            factory,
            attestation=AttestationService(HostSignerKeyRegistry(), clock=clock),
            challenges=self.challenges,
            receipts=ReceiptService(signing_provider(), clock),
            provenance=provenance(),
            clock=clock,
            integrity=AllowAllIntegrity(),  # type: ignore[arg-type]
            seal=lambda rid, bundle: ReplayEnvelope(
                ciphertext=f"sealed:{rid}".encode(), nonce=b"nonce-12-byt", key_id="replay-1"
            ),
        )

    async def register_host_key(self, signer_key_id: str, *, public: bytes | None = None) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO arc_host_attestation_keys ("
                    "  signer_key_id, host_id, tenant_id, attestation_profile, public_key,"
                    "  valid_from, created_by_operator"
                    ") VALUES (:kid, :host, :tid, :profile, :pub, :vfrom, 'test')"
                ),
                {
                    "kid": signer_key_id,
                    "host": _HOST_ID,
                    "tid": self.seed.tenant_id,
                    "profile": _PROFILE,
                    "pub": base64.b64encode(public if public is not None else self.host_public).decode("ascii"),
                    "vfrom": ARC_NOW - datetime.timedelta(days=1),
                },
            )

    async def issue_challenge(self, manifest: ManifestClaims) -> bytes:
        claims_digest = compute_manifest_claims_digest(manifest.as_claims_dict())
        issued = await self.challenges.issue_challenge(
            _ctx(self.seed),
            session_id=manifest.session_id,
            manifest_claims_digest=claims_digest,
            idempotency_key=uuid.uuid4().hex,
        )
        return issued.arc_nonce

    def envelope(
        self,
        manifest: ManifestClaims,
        nonce: bytes,
        signer_key_id: str,
        *,
        attestation_id: str | None = None,
        private: bytes | None = None,
    ) -> AttestationEnvelope:
        payload = {
            "host_id": _HOST_ID,
            "repository_identity": manifest.repository_identity,
            "immutable_source_revision": "deadbeef",
            "environment": manifest.environment,
            "data_sensitivity": manifest.data_sensitivity,
            "session_id": manifest.session_id,
            "manifest_claims_digest": compute_manifest_claims_digest(manifest.as_claims_dict()),
            "arc_nonce": base64.b64encode(nonce).decode("ascii"),
        }
        att_id = attestation_id or f"att-{uuid.uuid4().hex[:12]}"
        envelope_dict: dict[str, object] = {
            "profile": _PROFILE,
            "signer_key_id": signer_key_id,
            "attestation_id": att_id,
            "issued_at": ARC_NOW,
            "expires_at": ARC_NOW + CHALLENGE_TTL,
            "payload": payload,
        }
        signing_input = _SIGNING_DOMAIN + canonicalize_host_attestation_envelope(envelope_dict)
        signature = Ed25519PrivateKey.from_private_bytes(private or self.host_private).sign(signing_input)
        return AttestationEnvelope(
            profile=_PROFILE,
            signer_key_id=signer_key_id,
            attestation_id=att_id,
            issued_at=ARC_NOW,
            expires_at=ARC_NOW + CHALLENGE_TTL,
            payload=payload,
            signature=base64.b64encode(signature).decode("ascii"),
        )

    def request(
        self, manifest: ManifestClaims, envelope: AttestationEnvelope, *, fingerprint: str = _FINGERPRINT
    ) -> ResolutionRequest:
        return ResolutionRequest(
            ctx=_ctx(self.seed),
            host_id=_HOST_ID,
            manifest=manifest,
            envelope=envelope,
            manifest_fingerprint=fingerprint,
            candidates=SelectionInput(manifest=parse_manifest(manifest), tenant_id=self.seed.tenant_id, as_of=ARC_NOW),
            budget_limit_bytes=12288,
        )


@pytest_asyncio.fixture
async def harness(factory: async_sessionmaker[AsyncSession], seed: ArcSeed, clock: FakeClock) -> _Harness:
    return _Harness(factory, seed, clock)


async def _resolve_once(harness: _Harness, *, attestation_id: str | None = None, fingerprint: str = _FINGERPRINT):
    manifest = _manifest()
    signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
    await harness.register_host_key(signer_key_id)
    nonce = await harness.issue_challenge(manifest)
    envelope = harness.envelope(manifest, nonce, signer_key_id, attestation_id=attestation_id)
    return await harness.service.resolve(harness.request(manifest, envelope, fingerprint=fingerprint))


@pytest.mark.asyncio
async def test_a_valid_resolution_produces_a_receipt_and_consumes_its_challenge(harness: _Harness) -> None:
    outcome = await _resolve_once(harness)

    assert outcome.status is ResolutionStatus.READY
    assert outcome.replayed is False

    async with harness.factory() as session:
        receipt = (
            await session.execute(
                text(
                    "SELECT r.receipt_id, r.resolution_status, c.consumed_at "
                    "FROM arc_receipts r JOIN arc_context_challenges c ON c.challenge_id = r.challenge_id "
                    "WHERE r.receipt_id = :rid"
                ),
                {"rid": outcome.receipt_id},
            )
        ).one()

    assert receipt.resolution_status == "ready"
    assert receipt.consumed_at is not None


@pytest.mark.asyncio
async def test_the_creation_event_and_head_land_in_the_same_transaction(harness: _Harness) -> None:
    outcome = await _resolve_once(harness)

    async with harness.factory() as session:
        events = (
            (
                await session.execute(
                    text("SELECT sequence FROM arc_receipt_events WHERE receipt_id = :rid"),
                    {"rid": outcome.receipt_id},
                )
            )
            .scalars()
            .all()
        )
        head = (
            await session.execute(
                text("SELECT next_sequence FROM arc_receipt_event_heads WHERE receipt_id = :rid"),
                {"rid": outcome.receipt_id},
            )
        ).scalar_one()

    assert events == [0]
    assert head == 1


@pytest.mark.asyncio
async def test_the_audit_row_lands_with_the_receipt(harness: _Harness) -> None:
    outcome = await _resolve_once(harness)

    async with harness.factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT event_type, tenant_id FROM arc_audit_outbox " "WHERE event_payload ->> 'receipt_id' = :rid"
                ),
                {"rid": str(outcome.receipt_id)},
            )
        ).one()

    assert row.event_type == actions.ARC_CONTEXT_RESOLVED
    assert row.tenant_id == harness.seed.tenant_id


@pytest.mark.asyncio
async def test_an_unverifiable_attestation_creates_no_receipt(harness: _Harness) -> None:
    """A rejected attempt is not a blocked outcome: there was never a
    trustworthy request to record, so no receipt exists at all."""
    manifest = _manifest()
    signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
    await harness.register_host_key(signer_key_id)
    nonce = await harness.issue_challenge(manifest)

    # Signed by a key that is not the registered one.
    wrong = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    envelope = harness.envelope(manifest, nonce, signer_key_id, private=wrong)

    with pytest.raises(ManifestUnverified):
        await harness.service.resolve(harness.request(manifest, envelope))

    async with harness.factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipts WHERE tenant_id = :tid"),
                {"tid": harness.seed.tenant_id},
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_a_rejected_attempt_leaves_its_challenge_unconsumed(harness: _Harness) -> None:
    """The challenge must remain usable: the caller was never authenticated,
    so burning their challenge would let an attacker deny them service."""
    manifest = _manifest()
    signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
    await harness.register_host_key(signer_key_id)
    nonce = await harness.issue_challenge(manifest)
    wrong = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

    with pytest.raises(ManifestUnverified):
        await harness.service.resolve(
            harness.request(manifest, harness.envelope(manifest, nonce, signer_key_id, private=wrong))
        )

    async with harness.factory() as session:
        consumed = (
            await session.execute(
                text("SELECT consumed_at FROM arc_context_challenges WHERE arc_nonce_digest = :d"),
                {"d": nonce_digest(nonce)},
            )
        ).scalar_one()
    assert consumed is None


@pytest.mark.asyncio
async def test_a_rejected_attempt_is_audited_in_its_own_transaction(harness: _Harness) -> None:
    """The resolution transaction is abandoned, so the audit row cannot ride
    on it -- and a failed authentication that leaves no trace is exactly the
    one an operator most needs to see."""
    manifest = _manifest()
    signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
    await harness.register_host_key(signer_key_id)
    nonce = await harness.issue_challenge(manifest)
    wrong = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    envelope = harness.envelope(manifest, nonce, signer_key_id, private=wrong)

    with pytest.raises(ManifestUnverified):
        await harness.service.resolve(harness.request(manifest, envelope))

    async with harness.factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT event_type, event_payload FROM arc_audit_outbox "
                    "WHERE event_payload ->> 'attestation_id' = :att"
                ),
                {"att": envelope.attestation_id},
            )
        ).one()

    assert row.event_type == actions.ARC_MANIFEST_UNVERIFIED
    assert row.event_payload["reason_code"] == "blocked_manifest_unverified"


@pytest.mark.asyncio
async def test_an_unknown_signer_key_is_rejected(harness: _Harness) -> None:
    manifest = _manifest()
    nonce = await harness.issue_challenge(manifest)
    envelope = harness.envelope(manifest, nonce, "hk-never-registered")

    with pytest.raises(ManifestUnverified):
        await harness.service.resolve(harness.request(manifest, envelope))


@pytest.mark.asyncio
async def test_a_revoked_key_cannot_start_a_new_resolution(harness: _Harness) -> None:
    manifest = _manifest()
    signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
    await harness.register_host_key(signer_key_id)
    async with harness.factory() as session, session.begin():
        await HostSignerKeyRegistry().revoke(session, signer_key_id, revoked_at=ARC_NOW - datetime.timedelta(seconds=1))

    nonce = await harness.issue_challenge(manifest)
    envelope = harness.envelope(manifest, nonce, signer_key_id)

    with pytest.raises(ManifestUnverified):
        await harness.service.resolve(harness.request(manifest, envelope))


@pytest.mark.asyncio
async def test_a_reused_nonce_cannot_produce_a_second_receipt(harness: _Harness) -> None:
    """Single use, enforced through the composed transaction rather than in
    isolation."""
    manifest = _manifest()
    signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
    await harness.register_host_key(signer_key_id)
    nonce = await harness.issue_challenge(manifest)

    await harness.service.resolve(harness.request(manifest, harness.envelope(manifest, nonce, signer_key_id)))

    with pytest.raises(ManifestUnverified, match="already consumed"):
        await harness.service.resolve(harness.request(manifest, harness.envelope(manifest, nonce, signer_key_id)))


@pytest.mark.asyncio
async def test_every_read_uses_one_as_of_recorded_on_the_receipt(harness: _Harness) -> None:
    """`evaluated_at` is the single instant the whole resolution was
    computed against, so a later replay can reproduce it."""
    outcome = await _resolve_once(harness)

    async with harness.factory() as session:
        evaluated_at = (
            await session.execute(
                text("SELECT evaluated_at FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": outcome.receipt_id},
            )
        ).scalar_one()

    assert evaluated_at == ARC_NOW


@pytest.mark.asyncio
async def test_the_replay_envelope_is_sealed_against_the_preallocated_id(harness: _Harness) -> None:
    """Proof that the receipt ID existed before the bundle was sealed."""
    outcome = await _resolve_once(harness)

    async with harness.factory() as session:
        ciphertext = (
            await session.execute(
                text("SELECT response_replay_ciphertext FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": outcome.receipt_id},
            )
        ).scalar_one()

    assert ciphertext == f"sealed:{outcome.receipt_id}".encode()


@pytest.mark.asyncio
async def test_two_resolutions_by_the_same_host_are_independent(harness: _Harness) -> None:
    first = await _resolve_once(harness)
    second = await _resolve_once(harness)

    assert first.receipt_id != second.receipt_id
    async with harness.factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipts WHERE tenant_id = :tid"),
                {"tid": harness.seed.tenant_id},
            )
        ).scalar_one()
    assert count == 2
