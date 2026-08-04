"""Exact-retry replay: answering from the receipt rather than re-resolving.

A retry of a request whose response was lost in transit must return the same
answer, not perform a second resolution. Two things follow, and both are the
substance of this file:

- A replay grants nothing new. It accepts no fresh attestation, consumes no
  second challenge, and writes no second receipt.
- A replay still works when the signer key that authorized the original has
  since been revoked. Revocation stops *new* authorizations; it does not
  retroactively unmake what the key already authorized, and a caller must be
  able to recover its own lost response.

The same attestation ID with a *different* manifest is the opposite case: a
conflict, never resolved automatically.
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

from registry.arc.schemas.canonical import canonicalize_host_attestation_envelope
from registry.arc.schemas.canonical import manifest_claims_digest as compute_manifest_claims_digest
from registry.arc.service.attestation import (
    AttestationEnvelope,
    AttestationService,
    HostSignerKeyRegistry,
    ManifestClaims,
)
from registry.arc.service.challenge import CHALLENGE_TTL, ChallengeNonceDeriver, ChallengeService
from registry.arc.service.receipt import ReceiptService, ReplayEnvelope
from registry.arc.service.resolution import (
    IdempotencyConflict,
    ManifestUnverified,
    ResolutionRequest,
    ResolutionService,
    parse_manifest,
)
from registry.arc.service.selection import SelectionInput
from registry.arc.types import ArcRequestContext, ResolutionStatus
from registry.types import FakeClock, TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, provenance, seed_arc, signing_provider

_HOST_ID = "host-1"
_PROFILE = "arc_host_attestation_v1"
_SIGNING_DOMAIN = b"ARC-HOST-ATTESTATION-V1\x00"
_FINGERPRINT = "f" * 64
_OTHER_FINGERPRINT = "a" * 64


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-replay")


class _Harness:
    def __init__(self, factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> None:
        self.factory = factory
        self.seed = seed
        self.clock = FakeClock(ARC_NOW)
        private = Ed25519PrivateKey.generate()
        self.host_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        self.host_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.challenges = ChallengeService(
            factory, ChallengeNonceDeriver({"nk1": b"secret"}, active_key_id="nk1"), self.clock
        )
        self.service = ResolutionService(
            factory,
            attestation=AttestationService(HostSignerKeyRegistry(), clock=self.clock),
            challenges=self.challenges,
            receipts=ReceiptService(signing_provider(), self.clock),
            provenance=provenance(),
            clock=self.clock,
            seal=lambda rid, bundle: ReplayEnvelope(
                ciphertext=f"sealed:{rid}".encode(), nonce=b"nonce-12-byt", key_id="replay-1"
            ),
        )

    def ctx(self) -> ArcRequestContext:
        tenant = TenantContext(
            tenant_id=self.seed.tenant_id, actor_id=self.seed.actor_id, roles=["consumer"], oidc_subject="s"
        )
        return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id=_HOST_ID)

    def manifest(self, session_id: str = "sess-1") -> ManifestClaims:
        return ManifestClaims(
            session_id=session_id,
            task_kind="code_change",
            requested_action_classes=("merge",),
            capability_ids=(),
            domain_ids=("payments",),
            environment="production",
            data_sensitivity="confidential",
            repository_identity="git@example.test:org/repo.git",
            supported_context_bundle_content_profiles=("arc_context_bundle_content_v1",),
        )

    async def register_key(self) -> str:
        signer_key_id = f"hk-{uuid.uuid4().hex[:12]}"
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
                    "pub": base64.b64encode(self.host_public).decode("ascii"),
                    "vfrom": ARC_NOW - datetime.timedelta(days=1),
                },
            )
        return signer_key_id

    async def nonce(self, manifest: ManifestClaims) -> bytes:
        issued = await self.challenges.issue_challenge(
            self.ctx(),
            session_id=manifest.session_id,
            manifest_claims_digest=compute_manifest_claims_digest(manifest.as_claims_dict()),
            idempotency_key=uuid.uuid4().hex,
        )
        return issued.arc_nonce

    def envelope(
        self, manifest: ManifestClaims, nonce: bytes, signer_key_id: str, attestation_id: str
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
        envelope_dict: dict[str, object] = {
            "profile": _PROFILE,
            "signer_key_id": signer_key_id,
            "attestation_id": attestation_id,
            "issued_at": ARC_NOW,
            "expires_at": ARC_NOW + CHALLENGE_TTL,
            "payload": payload,
        }
        signing_input = _SIGNING_DOMAIN + canonicalize_host_attestation_envelope(envelope_dict)
        signature = Ed25519PrivateKey.from_private_bytes(self.host_private).sign(signing_input)
        return AttestationEnvelope(
            profile=_PROFILE,
            signer_key_id=signer_key_id,
            attestation_id=attestation_id,
            issued_at=ARC_NOW,
            expires_at=ARC_NOW + CHALLENGE_TTL,
            payload=payload,
            signature=base64.b64encode(signature).decode("ascii"),
        )

    def request(
        self, manifest: ManifestClaims, envelope: AttestationEnvelope, *, fingerprint: str = _FINGERPRINT
    ) -> ResolutionRequest:
        return ResolutionRequest(
            ctx=self.ctx(),
            host_id=_HOST_ID,
            manifest=manifest,
            envelope=envelope,
            manifest_fingerprint=fingerprint,
            candidates=SelectionInput(manifest=parse_manifest(manifest), tenant_id=self.seed.tenant_id, as_of=ARC_NOW),
            budget_limit_bytes=12288,
        )

    async def count(self, table: str) -> int:
        async with self.factory() as session:
            return (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :tid"),  # noqa: S608 - fixed literals
                    {"tid": self.seed.tenant_id},
                )
            ).scalar_one()


@pytest_asyncio.fixture
async def harness(factory: async_sessionmaker[AsyncSession], seed: ArcSeed) -> _Harness:
    return _Harness(factory, seed)


@pytest.mark.asyncio
async def test_an_exact_retry_returns_the_original_receipt(harness: _Harness) -> None:
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    first = await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    retry = await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    assert retry.receipt_id == first.receipt_id
    assert retry.replayed is True
    assert first.replayed is False
    assert retry.status is ResolutionStatus.READY


@pytest.mark.asyncio
async def test_a_replay_creates_no_second_receipt(harness: _Harness) -> None:
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    for _ in range(3):
        await harness.service.resolve(
            harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
        )

    assert await harness.count("arc_receipts") == 1


@pytest.mark.asyncio
async def test_a_replay_consumes_no_second_challenge(harness: _Harness) -> None:
    """The retry presents a fresh, unconsumed challenge. It must come back
    unconsumed: a replay grants nothing, so it spends nothing."""
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    retry_nonce = await harness.nonce(manifest)
    await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, retry_nonce, key, attestation_id))
    )

    async with harness.factory() as session:
        consumed = (
            await session.execute(
                text("SELECT count(*) FROM arc_context_challenges WHERE tenant_id = :tid AND consumed_at IS NOT NULL"),
                {"tid": harness.seed.tenant_id},
            )
        ).scalar_one()
    assert consumed == 1


@pytest.mark.asyncio
async def test_a_replay_appends_no_event_to_the_chain(harness: _Harness) -> None:
    """Replaying is not an event in the receipt's life; it returns what
    already happened."""
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    first = await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )
    await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    async with harness.factory() as session:
        events = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipt_events WHERE receipt_id = :rid"),
                {"rid": first.receipt_id},
            )
        ).scalar_one()
    assert events == 1


@pytest.mark.asyncio
async def test_a_replay_survives_revocation_of_the_key_that_signed_the_original(harness: _Harness) -> None:
    """The reason the replay check runs before signer-key validation.

    Revocation stops new authorizations. It does not retroactively unmake
    what the key already authorized, and a caller whose response was lost in
    transit must still be able to recover it.
    """
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    first = await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    async with harness.factory() as session, session.begin():
        await HostSignerKeyRegistry().revoke(session, key, revoked_at=ARC_NOW - datetime.timedelta(seconds=1))

    retry = await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    assert retry.receipt_id == first.receipt_id
    assert retry.replayed is True


@pytest.mark.asyncio
async def test_a_revoked_key_still_cannot_start_a_new_resolution(harness: _Harness) -> None:
    """The other half of the previous test: replay is the *only* thing a
    revoked key can still reach."""
    key = await harness.register_key()
    async with harness.factory() as session, session.begin():
        await HostSignerKeyRegistry().revoke(session, key, revoked_at=ARC_NOW - datetime.timedelta(seconds=1))

    manifest = harness.manifest()
    envelope = harness.envelope(manifest, await harness.nonce(manifest), key, f"att-{uuid.uuid4().hex[:12]}")
    with pytest.raises(ManifestUnverified):
        await harness.service.resolve(harness.request(manifest, envelope))


@pytest.mark.asyncio
async def test_the_same_attestation_id_with_a_different_manifest_is_a_conflict(harness: _Harness) -> None:
    """What an idempotency key exists to catch: one key, two meanings."""
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    with pytest.raises(IdempotencyConflict, match="different manifest"):
        await harness.service.resolve(
            harness.request(
                manifest,
                harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id),
                fingerprint=_OTHER_FINGERPRINT,
            )
        )


@pytest.mark.asyncio
async def test_a_conflict_creates_no_receipt_and_consumes_nothing(harness: _Harness) -> None:
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    conflict_nonce = await harness.nonce(manifest)
    with pytest.raises(IdempotencyConflict):
        await harness.service.resolve(
            harness.request(
                manifest,
                harness.envelope(manifest, conflict_nonce, key, attestation_id),
                fingerprint=_OTHER_FINGERPRINT,
            )
        )

    assert await harness.count("arc_receipts") == 1
    async with harness.factory() as session:
        consumed = (
            await session.execute(
                text("SELECT count(*) FROM arc_context_challenges WHERE tenant_id = :tid AND consumed_at IS NOT NULL"),
                {"tid": harness.seed.tenant_id},
            )
        ).scalar_one()
    assert consumed == 1


@pytest.mark.asyncio
async def test_a_different_attestation_id_is_a_fresh_resolution(harness: _Harness) -> None:
    """Replay is keyed on the attestation ID, so a genuinely new request
    with the same manifest resolves again rather than replaying."""
    manifest = harness.manifest()
    key = await harness.register_key()
    first = await harness.service.resolve(
        harness.request(
            manifest, harness.envelope(manifest, await harness.nonce(manifest), key, f"att-{uuid.uuid4().hex[:8]}")
        )
    )
    second = await harness.service.resolve(
        harness.request(
            manifest, harness.envelope(manifest, await harness.nonce(manifest), key, f"att-{uuid.uuid4().hex[:8]}")
        )
    )

    assert first.receipt_id != second.receipt_id
    assert second.replayed is False


@pytest.mark.asyncio
async def test_a_receipt_that_failed_integrity_is_not_replayed(harness: _Harness) -> None:
    """Handing back content whose chain may have been altered would let a
    tampered receipt keep serving its original answer forever."""
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    first = await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    async with harness.factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_receipts SET integrity_state = 'integrity_failed' WHERE receipt_id = :rid"),
            {"rid": first.receipt_id},
        )

    with pytest.raises(ManifestUnverified, match="integrity"):
        await harness.service.resolve(
            harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
        )


@pytest.mark.asyncio
async def test_replay_is_scoped_to_the_host_that_made_the_original(harness: _Harness) -> None:
    """The replay key is `(host_id, attestation_id)`. A different host
    presenting the same attestation ID must not receive another host's
    receipt."""
    manifest = harness.manifest()
    key = await harness.register_key()
    attestation_id = f"att-{uuid.uuid4().hex[:12]}"
    await harness.service.resolve(
        harness.request(manifest, harness.envelope(manifest, await harness.nonce(manifest), key, attestation_id))
    )

    async with harness.factory() as session:
        rows = (
            await session.execute(
                text("SELECT count(*) FROM arc_receipts WHERE host_id = :host AND attestation_id = :att"),
                {"host": "a-different-host", "att": attestation_id},
            )
        ).scalar_one()
    assert rows == 0
