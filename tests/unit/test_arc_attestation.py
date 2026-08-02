"""AttestationService: verifying one ARC Host Attestation v1 envelope.

No database here -- the real signer-key lookup (ARC-T24) locks a row `FOR
SHARE` inside the resolution transaction, but this module only needs the
lookup's *shape*, so tests supply an in-memory fake instead.
"""

from __future__ import annotations

import base64
import datetime
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from registry.arc.schemas.canonical import canonicalize_host_attestation_envelope
from registry.arc.schemas.canonical import manifest_claims_digest as compute_manifest_claims_digest
from registry.arc.service.attestation import (
    AttestationEnvelope,
    AttestationService,
    AttestationVerificationError,
    HostSignerKey,
    ManifestClaims,
    VerifiedAttestation,
)
from registry.arc.service.challenge import NONCE_BYTES
from registry.types import FakeClock

_NOW = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_HOST_ID = "host-1"
_SIGNER_KEY_ID = "hk-1"
_PROFILE = "arc_host_attestation_v1"
_SIGNING_DOMAIN = b"ARC-HOST-ATTESTATION-V1\x00"


def _keypair() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return raw_private, raw_public


def _nonce_b64(marker: bytes = b"n") -> str:
    nonce = (marker * NONCE_BYTES)[:NONCE_BYTES]
    return base64.b64encode(nonce).decode("ascii")


def _manifest(**overrides: object) -> ManifestClaims:
    base: dict[str, object] = {
        "session_id": "sess-1",
        "task_kind": "code_change",
        "requested_action_classes": ("merge",),
        "capability_ids": ("7b1f0c22-0000-4000-8000-000000000001",),
        "domain_ids": ("payments",),
        "environment": "production",
        "data_sensitivity": "confidential",
        "repository_identity": "git@example.test:org/repo.git",
        "supported_context_bundle_content_profiles": ("arc_context_bundle_content_v1",),
    }
    base.update(overrides)
    return ManifestClaims(**base)  # type: ignore[arg-type]


def _envelope_dict(
    manifest: ManifestClaims,
    *,
    payload_overrides: dict[str, str] | None = None,
    attestation_id: str = "att-1",
    issued_at: datetime.datetime = _NOW,
    expires_at: datetime.datetime | None = None,
    profile: str = _PROFILE,
    signer_key_id: str = _SIGNER_KEY_ID,
) -> dict[str, object]:
    payload = {
        "host_id": _HOST_ID,
        "repository_identity": manifest.repository_identity,
        "immutable_source_revision": "deadbeef",
        "environment": manifest.environment,
        "data_sensitivity": manifest.data_sensitivity,
        "session_id": manifest.session_id,
        "manifest_claims_digest": compute_manifest_claims_digest(manifest.as_claims_dict()),
        "arc_nonce": _nonce_b64(),
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return {
        "profile": profile,
        "signer_key_id": signer_key_id,
        "attestation_id": attestation_id,
        "issued_at": issued_at,
        "expires_at": expires_at if expires_at is not None else issued_at + datetime.timedelta(minutes=5),
        "payload": payload,
    }


def _sign(private_raw: bytes, envelope_dict: dict[str, object]) -> str:
    signing_input = _SIGNING_DOMAIN + canonicalize_host_attestation_envelope(envelope_dict)
    signature = Ed25519PrivateKey.from_private_bytes(private_raw).sign(signing_input)
    return base64.b64encode(signature).decode("ascii")


def _envelope(
    private_raw: bytes, envelope_dict: dict[str, object], *, signature: str | None = None
) -> AttestationEnvelope:
    return AttestationEnvelope(
        profile=envelope_dict["profile"],  # type: ignore[arg-type]
        signer_key_id=envelope_dict["signer_key_id"],  # type: ignore[arg-type]
        attestation_id=envelope_dict["attestation_id"],  # type: ignore[arg-type]
        issued_at=envelope_dict["issued_at"],  # type: ignore[arg-type]
        expires_at=envelope_dict["expires_at"],  # type: ignore[arg-type]
        payload=envelope_dict["payload"],  # type: ignore[arg-type]
        signature=signature if signature is not None else _sign(private_raw, envelope_dict),
    )


def _key(public_raw: bytes, **overrides: object) -> HostSignerKey:
    base: dict[str, object] = {
        "signer_key_id": _SIGNER_KEY_ID,
        "host_id": _HOST_ID,
        "tenant_id": _TENANT_ID,
        "attestation_profile": _PROFILE,
        "public_key": public_raw,
        "valid_from": _NOW - datetime.timedelta(days=1),
        "valid_until": None,
        "revoked_at": None,
    }
    base.update(overrides)
    return HostSignerKey(**base)  # type: ignore[arg-type]


class _FakeKeyLookup:
    """Stands in for the row-locking registry.

    Ignores the session it is handed -- the lock is what the real
    implementation adds, and it is proven against a live database in the
    host-key integration tests rather than simulated here.
    """

    def __init__(self, keys: dict[str, HostSignerKey]) -> None:
        self._keys = keys

    async def get(self, session: object, signer_key_id: str) -> HostSignerKey | None:
        return self._keys.get(signer_key_id)


def _service(keys: dict[str, HostSignerKey], *, clock: FakeClock | None = None) -> AttestationService:
    return AttestationService(_FakeKeyLookup(keys), clock=clock or FakeClock(_NOW))


async def _verify(
    service: AttestationService, envelope: AttestationEnvelope, manifest: ManifestClaims
) -> VerifiedAttestation:
    # The fake lookup ignores the session; None keeps these tests free of a
    # database they do not need.
    return await service.verify_attestation(
        None,  # type: ignore[arg-type]
        tenant_id=_TENANT_ID,
        host_id=_HOST_ID,
        envelope=envelope,
        manifest=manifest,
    )


# --- happy path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_correctly_signed_attestation_verifies() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    verified = await _verify(service, envelope, manifest)

    assert verified.attestation_id == "att-1"
    assert verified.signer_key_id == _SIGNER_KEY_ID
    assert verified.host_id == _HOST_ID
    assert verified.tenant_id == _TENANT_ID
    assert verified.session_id == manifest.session_id
    assert verified.manifest_claims_digest == compute_manifest_claims_digest(manifest.as_claims_dict())
    assert verified.arc_nonce_b64 == _nonce_b64()


@pytest.mark.asyncio
async def test_a_rotated_out_key_still_verifies_within_its_own_window() -> None:
    """Rotation lineage: a key superseded by a newer one is not thereby
    invalid. Only its own valid_from/valid_until and revoked_at govern it --
    presence of some other, newer key elsewhere in the registry is
    irrelevant to verifying an attestation signed under this one."""
    old_private, old_public = _keypair()
    _, new_public = _keypair()
    manifest = _manifest()
    envelope = _envelope(old_private, _envelope_dict(manifest, signer_key_id="hk-old"))
    service = _service(
        {
            "hk-old": _key(old_public, signer_key_id="hk-old", valid_until=_NOW + datetime.timedelta(days=1)),
            "hk-new": _key(new_public, signer_key_id="hk-new"),
        }
    )

    verified = await _verify(service, envelope, manifest)
    assert verified.signer_key_id == "hk-old"


# --- profile, freshness ------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_profile_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope_dict = _envelope_dict(manifest, profile="arc_host_attestation_v2")
    envelope = _envelope(private_raw, envelope_dict)
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="unsupported attestation profile"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_a_validity_window_wider_than_five_minutes_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope_dict = _envelope_dict(manifest, expires_at=_NOW + datetime.timedelta(minutes=6))
    envelope = _envelope(private_raw, envelope_dict)
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="wider than five minutes"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_an_expired_attestation_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    issued_at = _NOW - datetime.timedelta(minutes=10)
    envelope_dict = _envelope_dict(manifest, issued_at=issued_at, expires_at=issued_at + datetime.timedelta(minutes=5))
    envelope = _envelope(private_raw, envelope_dict)
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="expired"):
        await _verify(service, envelope, manifest)


# --- signer key validity -----------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_signer_key_is_rejected() -> None:
    private_raw, _ = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    service = _service({})

    with pytest.raises(AttestationVerificationError, match="no host attestation key registered"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_revoked_key_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    service = _service({_SIGNER_KEY_ID: _key(public_raw, revoked_at=_NOW - datetime.timedelta(minutes=1))})

    with pytest.raises(AttestationVerificationError, match="expired or revoked"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_key_not_yet_valid_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    service = _service({_SIGNER_KEY_ID: _key(public_raw, valid_from=_NOW + datetime.timedelta(days=1))})

    with pytest.raises(AttestationVerificationError, match="expired or revoked"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_expired_key_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    service = _service({_SIGNER_KEY_ID: _key(public_raw, valid_until=_NOW - datetime.timedelta(minutes=1))})

    with pytest.raises(AttestationVerificationError, match="expired or revoked"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_key_bound_to_a_different_tenant_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    other_tenant = uuid.UUID("22222222-2222-2222-2222-222222222222")
    service = _service({_SIGNER_KEY_ID: _key(public_raw, tenant_id=other_tenant)})

    with pytest.raises(AttestationVerificationError, match="not registered to this tenant"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_key_bound_to_a_different_host_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    service = _service({_SIGNER_KEY_ID: _key(public_raw, host_id="a-different-host")})

    with pytest.raises(AttestationVerificationError, match="not registered to this tenant"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_key_registered_for_a_different_profile_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    service = _service({_SIGNER_KEY_ID: _key(public_raw, attestation_profile="something_else_v1")})

    with pytest.raises(AttestationVerificationError, match="not registered for"):
        await _verify(service, envelope, manifest)


# --- signature ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tampered_payload_fails_signature_verification() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope_dict = _envelope_dict(manifest)
    envelope = _envelope(private_raw, envelope_dict)
    # Mutate after signing: the signature covers the original bytes.
    tampered = AttestationEnvelope(
        profile=envelope.profile,
        signer_key_id=envelope.signer_key_id,
        attestation_id=envelope.attestation_id,
        issued_at=envelope.issued_at,
        expires_at=envelope.expires_at,
        payload={**envelope.payload, "immutable_source_revision": "tampered"},
        signature=envelope.signature,
    )
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="signature did not verify"):
        await _verify(service, tampered, manifest)


@pytest.mark.asyncio
async def test_signed_by_a_different_key_fails_verification() -> None:
    signer_private, _ = _keypair()
    _, registered_public = _keypair()  # a different keypair than the signer used
    manifest = _manifest()
    envelope = _envelope(signer_private, _envelope_dict(manifest))
    service = _service({_SIGNER_KEY_ID: _key(registered_public)})

    with pytest.raises(AttestationVerificationError, match="signature did not verify"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_malformed_base64_signature_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest), signature="not-valid-base64!!!")
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="signature is not valid base64"):
        await _verify(service, envelope, manifest)


# --- mirrored fields and claims digest ---------------------------------------


@pytest.mark.asyncio
async def test_payload_host_id_mismatch_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest, payload_overrides={"host_id": "a-different-host"}))
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="host_id does not match"):
        await _verify(service, envelope, manifest)


@pytest.mark.parametrize(
    ("field", "signed_value"),
    [
        ("repository_identity", "git@example.test:org/other.git"),
        ("environment", "staging"),
        ("data_sensitivity", "public"),
        ("session_id", "a-different-session"),
    ],
)
@pytest.mark.asyncio
async def test_a_mirrored_field_that_disagrees_with_the_manifest_is_rejected(field: str, signed_value: str) -> None:
    """The payload is signed *as-is* (so the signature itself still verifies);
    the mismatch is between what was signed and what the manifest separately
    claims -- exactly the substitution mirrored-field checking exists to catch."""
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest, payload_overrides={field: signed_value}))
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match=f"{field} does not mirror"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_claims_digest_mismatch_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest, payload_overrides={"manifest_claims_digest": "b" * 64}))
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="does not match the recomputed digest"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_unsupported_bundle_content_profile_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest(supported_context_bundle_content_profiles=("some_other_profile_v1",))
    envelope = _envelope(private_raw, _envelope_dict(manifest))
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="does not declare support for"):
        await _verify(service, envelope, manifest)


# --- nonce --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_nonce_base64_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    envelope = _envelope(private_raw, _envelope_dict(manifest, payload_overrides={"arc_nonce": "!!!not-base64!!!"}))
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="arc_nonce is not valid base64"):
        await _verify(service, envelope, manifest)


@pytest.mark.asyncio
async def test_wrong_nonce_length_is_rejected() -> None:
    private_raw, public_raw = _keypair()
    manifest = _manifest()
    short_nonce = base64.b64encode(b"too-short").decode("ascii")
    envelope = _envelope(private_raw, _envelope_dict(manifest, payload_overrides={"arc_nonce": short_nonce}))
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="expected 32"):
        await _verify(service, envelope, manifest)


# --- malformed envelope structure ---------------------------------------------


@pytest.mark.asyncio
async def test_an_envelope_missing_a_payload_field_is_rejected() -> None:
    """A missing field makes the envelope uncanonicalizable, so there is no
    "correctly signed" version of it to construct -- signing itself requires
    canonicalizing first. The signature value is therefore irrelevant here:
    verification must fail before it ever reaches signature checking."""
    _, public_raw = _keypair()
    manifest = _manifest()
    envelope_dict = _envelope_dict(manifest)
    del envelope_dict["payload"]["immutable_source_revision"]  # type: ignore[attr-defined]
    envelope = _envelope(b"", envelope_dict, signature=base64.b64encode(b"0" * 64).decode("ascii"))
    service = _service({_SIGNER_KEY_ID: _key(public_raw)})

    with pytest.raises(AttestationVerificationError, match="does not canonicalize"):
        await _verify(service, envelope, manifest)
