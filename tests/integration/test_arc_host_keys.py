"""Host signer key registry: the lock that linearizes revocation against resolution.

Verification reads the signer-key row `FOR SHARE`; revocation `UPDATE`s the
same row, which in Postgres takes a row-level exclusive lock that conflicts
with `FOR SHARE`. That conflict is the whole mechanism: a revocation racing a
resolution either commits first (and the resolution then blocks, re-reads,
and rejects the revoked key) or waits until the resolution's transaction
ends. What must never happen is a resolution verifying successfully against a
key whose revocation has already committed.

These tests use two concurrent sessions against a live database, because
that interleaving is precisely what a fake or a single-session test cannot
exercise.
"""

from __future__ import annotations

import asyncio
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

from contextplane.arc.schemas.canonical import (
    canonicalize_host_attestation_envelope,
)
from contextplane.arc.schemas.canonical import manifest_claims_digest as compute_manifest_claims_digest
from contextplane.arc.service.attestation import (
    AttestationEnvelope,
    AttestationService,
    AttestationVerificationError,
    HostSignerKeyRegistry,
    ManifestClaims,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
_HOST_ID = "host-1"
_PROFILE = "arc_host_attestation_v1"
_SIGNING_DOMAIN = b"ARC-HOST-ATTESTATION-V1\x00"


def _keypair() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return raw_private, raw_public


def _manifest() -> ManifestClaims:
    return ManifestClaims(
        session_id="sess-1",
        intent_kind="code_change",
        requested_action_classes=("merge",),
        capability_ids=("7b1f0c22-0000-4000-8000-000000000001",),
        domain_ids=("payments",),
        environment="production",
        data_sensitivity="confidential",
        repository_identity="git@example.test:org/repo.git",
        supported_context_bundle_content_profiles=("arc_context_bundle_content_v1",),
    )


def _envelope(private_raw: bytes, signer_key_id: str, manifest: ManifestClaims) -> AttestationEnvelope:
    payload = {
        "host_id": _HOST_ID,
        "repository_identity": manifest.repository_identity,
        "immutable_source_revision": "deadbeef",
        "environment": manifest.environment,
        "data_sensitivity": manifest.data_sensitivity,
        "session_id": manifest.session_id,
        "manifest_claims_digest": compute_manifest_claims_digest(manifest.as_claims_dict()),
        "arc_nonce": base64.b64encode(b"n" * 32).decode("ascii"),
    }
    envelope_dict: dict[str, object] = {
        "profile": _PROFILE,
        "signer_key_id": signer_key_id,
        "attestation_id": f"att-{signer_key_id}",
        "issued_at": _NOW,
        "expires_at": _NOW + datetime.timedelta(minutes=5),
        "payload": payload,
    }
    signing_input = _SIGNING_DOMAIN + canonicalize_host_attestation_envelope(envelope_dict)
    signature = Ed25519PrivateKey.from_private_bytes(private_raw).sign(signing_input)
    return AttestationEnvelope(
        profile=_PROFILE,
        signer_key_id=signer_key_id,
        attestation_id=f"att-{signer_key_id}",
        issued_at=_NOW,
        expires_at=_NOW + datetime.timedelta(minutes=5),
        payload=payload,
        signature=base64.b64encode(signature).decode("ascii"),
    )


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"arc-hostkeys-{tid.hex[:8]}", "now": datetime.datetime.now(tz=datetime.UTC)},
        )
    return tid


async def _register_key(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    signer_key_id: str,
    public_raw: bytes,
    valid_until: datetime.datetime | None = None,
) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_host_attestation_keys ("
                "  signer_key_id, host_id, tenant_id, attestation_profile, public_key,"
                "  valid_from, valid_until, created_by_operator"
                ") VALUES (:kid, :host, :tid, :profile, :pub, :vfrom, :vuntil, 'test-operator')"
            ),
            {
                "kid": signer_key_id,
                "host": _HOST_ID,
                "tid": tenant_id,
                "profile": _PROFILE,
                "pub": base64.b64encode(public_raw).decode("ascii"),
                "vfrom": _NOW - datetime.timedelta(days=1),
                "vuntil": valid_until,
            },
        )


def _service() -> AttestationService:
    return AttestationService(HostSignerKeyRegistry(), clock=FakeClock(_NOW))


@pytest.fixture
def signer_key_id() -> str:
    """`signer_key_id` is the table's primary key and is *not* tenant-scoped,
    so a fixed literal would collide between tests sharing the session
    database."""
    return f"hk-{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio
async def test_a_registered_key_verifies_from_the_database(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, signer_key_id: str
) -> None:
    """The registry reads what registration wrote, including round-tripping
    the base64-encoded public key back to the raw bytes Ed25519 needs."""
    private_raw, public_raw = _keypair()
    await _register_key(factory, tenant_id=tenant_id, signer_key_id=signer_key_id, public_raw=public_raw)
    manifest = _manifest()
    envelope = _envelope(private_raw, signer_key_id, manifest)

    async with factory() as session, session.begin():
        verified = await _service().verify_attestation(
            session, tenant_id=tenant_id, host_id=_HOST_ID, envelope=envelope, manifest=manifest
        )

    assert verified.signer_key_id == signer_key_id


@pytest.mark.asyncio
async def test_a_revoked_key_no_longer_verifies(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, signer_key_id: str
) -> None:
    private_raw, public_raw = _keypair()
    await _register_key(factory, tenant_id=tenant_id, signer_key_id=signer_key_id, public_raw=public_raw)
    manifest = _manifest()
    envelope = _envelope(private_raw, signer_key_id, manifest)
    registry = HostSignerKeyRegistry()

    async with factory() as session, session.begin():
        assert await registry.revoke(session, signer_key_id, revoked_at=_NOW - datetime.timedelta(seconds=1)) is True

    with pytest.raises(AttestationVerificationError, match="expired or revoked"):
        async with factory() as session, session.begin():
            await _service().verify_attestation(
                session, tenant_id=tenant_id, host_id=_HOST_ID, envelope=envelope, manifest=manifest
            )


@pytest.mark.asyncio
async def test_revoking_twice_is_idempotent_and_keeps_the_first_timestamp(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, signer_key_id: str
) -> None:
    """Moving `revoked_at` later on a re-revoke could retroactively legitimize
    an attestation that was correctly rejected in between."""
    _, public_raw = _keypair()
    await _register_key(factory, tenant_id=tenant_id, signer_key_id=signer_key_id, public_raw=public_raw)
    registry = HostSignerKeyRegistry()
    first_at = _NOW
    later_at = _NOW + datetime.timedelta(hours=1)

    async with factory() as session, session.begin():
        assert await registry.revoke(session, signer_key_id, revoked_at=first_at) is True
    async with factory() as session, session.begin():
        assert await registry.revoke(session, signer_key_id, revoked_at=later_at) is False

    async with factory() as session:
        stored = (
            await session.execute(
                text("SELECT revoked_at FROM arc_host_attestation_keys WHERE signer_key_id = :kid"),
                {"kid": signer_key_id},
            )
        ).scalar_one()
    assert stored == first_at


@pytest.mark.asyncio
async def test_revoking_an_unknown_key_reports_no_change(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        assert await HostSignerKeyRegistry().revoke(session, "no-such-key", revoked_at=_NOW) is False


@pytest.mark.asyncio
async def test_revocation_committed_before_a_resolution_starts_is_always_seen(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, signer_key_id: str
) -> None:
    """The ordering that must never be violated, stated directly.

    Not a race: revocation fully commits, then a resolution begins. If this
    could ever pass verification, no amount of locking would help -- it would
    mean the read is not seeing committed state at all.
    """
    private_raw, public_raw = _keypair()
    await _register_key(factory, tenant_id=tenant_id, signer_key_id=signer_key_id, public_raw=public_raw)
    manifest = _manifest()
    envelope = _envelope(private_raw, signer_key_id, manifest)

    async with factory() as session, session.begin():
        await HostSignerKeyRegistry().revoke(session, signer_key_id, revoked_at=_NOW - datetime.timedelta(seconds=1))

    with pytest.raises(AttestationVerificationError):
        async with factory() as session, session.begin():
            await _service().verify_attestation(
                session, tenant_id=tenant_id, host_id=_HOST_ID, envelope=envelope, manifest=manifest
            )


@pytest.mark.asyncio
async def test_revocation_blocks_until_a_concurrent_resolution_finishes(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, signer_key_id: str
) -> None:
    """`FOR SHARE` vs `UPDATE`: the two cannot overlap on the same row.

    The resolution takes its share lock first and holds it. The revocation
    then attempts its `UPDATE` and must block -- proven by the revocation
    still being unfinished while the resolution is mid-transaction, and
    completing only once the resolution commits.

    This is the guarantee the contract asks for: a resolution that has begun
    verifying cannot have the key revoked out from under it partway through,
    so the receipt it goes on to write is never signed against a key that was
    already revoked at the time it read it.
    """
    private_raw, public_raw = _keypair()
    await _register_key(factory, tenant_id=tenant_id, signer_key_id=signer_key_id, public_raw=public_raw)
    manifest = _manifest()
    envelope = _envelope(private_raw, signer_key_id, manifest)

    lock_taken = asyncio.Event()
    revocation_finished = asyncio.Event()
    resolution_committed = asyncio.Event()

    async def _resolution() -> bool:
        async with factory() as session, session.begin():
            await _service().verify_attestation(
                session, tenant_id=tenant_id, host_id=_HOST_ID, envelope=envelope, manifest=manifest
            )
            lock_taken.set()
            # Hold the share lock open long enough for the revocation to
            # have to wait on it.
            # Real wall-clock wait, not FakeClock: this holds a real Postgres
            # transaction's `FOR SHARE` lock open; the concurrent revocation
            # must genuinely block on that row lock, which only a real
            # elapsed wait can force.
            await asyncio.sleep(0.3)
            assert not revocation_finished.is_set(), "revocation completed while the share lock was held"
        resolution_committed.set()
        return True

    async def _revocation() -> bool:
        await lock_taken.wait()
        async with factory() as session, session.begin():
            revoked = await HostSignerKeyRegistry().revoke(session, signer_key_id, revoked_at=_NOW)
        revocation_finished.set()
        return revoked

    resolved, revoked = await asyncio.gather(_resolution(), _revocation())

    assert resolved is True
    assert revoked is True
    assert resolution_committed.is_set()

    # And once revoked, the next resolution is rejected.
    with pytest.raises(AttestationVerificationError, match="expired or revoked"):
        async with factory() as session, session.begin():
            await _service().verify_attestation(
                session, tenant_id=tenant_id, host_id=_HOST_ID, envelope=envelope, manifest=manifest
            )


@pytest.mark.asyncio
async def test_a_resolution_starting_after_revocation_commits_is_rejected_under_contention(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, signer_key_id: str
) -> None:
    """The mirror image: revocation goes first and holds its lock.

    The resolution's `FOR SHARE` blocks until the revocation commits, then
    reads the revoked row and rejects it. Together with the previous test,
    this covers both interleavings -- neither produces a successful
    verification against a committed revocation.
    """
    private_raw, public_raw = _keypair()
    await _register_key(factory, tenant_id=tenant_id, signer_key_id=signer_key_id, public_raw=public_raw)
    manifest = _manifest()
    envelope = _envelope(private_raw, signer_key_id, manifest)

    revocation_started = asyncio.Event()

    async def _revocation() -> None:
        async with factory() as session, session.begin():
            await HostSignerKeyRegistry().revoke(
                session, signer_key_id, revoked_at=_NOW - datetime.timedelta(seconds=1)
            )
            revocation_started.set()
            # Real wall-clock wait, not FakeClock: this holds the revocation's
            # transaction open so the concurrent resolution's `FOR SHARE`
            # genuinely blocks on the real row lock rather than racing it.
            await asyncio.sleep(0.3)

    async def _resolution() -> str:
        await revocation_started.wait()
        try:
            async with factory() as session, session.begin():
                await _service().verify_attestation(
                    session, tenant_id=tenant_id, host_id=_HOST_ID, envelope=envelope, manifest=manifest
                )
        except AttestationVerificationError:
            return "rejected"
        return "verified"

    _, outcome = await asyncio.gather(_revocation(), _resolution())
    assert outcome == "rejected"
