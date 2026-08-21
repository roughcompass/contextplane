"""ARC Host Attestation v1: structural and cryptographic verification.

`resolve_context` accepts a task manifest only when a host has attested to it
-- signed a canonical envelope binding the manifest's claims digest, the
mirrored request fields, and a one-time challenge nonce to a registered,
currently-valid signer key. This module verifies that envelope.

It deliberately does not touch the challenges or receipts tables. Challenge
single-use and binding are `ChallengeService`'s invariant; composing the two
into one resolution transaction belongs to whatever orchestrates the full
resolution. Verification reaches the database for exactly one thing -- the
signer key -- and does so through an injected lookup, so it stays testable
against a fake while the shipped `HostSignerKeyRegistry` reads the row
`FOR SHARE` inside the caller's transaction.

Every failure here -- wrong profile, unknown/expired/revoked/wrongly-bound
signer key, bad signature, an attestation window wider than five minutes or
already expired, a mirrored field that disagrees with the manifest, a claims
digest that does not recompute, an unsupported bundle-content declaration --
collapses to the same outward behavior: reject, no partial trust, no
conservative fallback. That is why one exception type covers all of them.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import uuid
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.arc.schemas.canonical import CanonicalizationError, canonicalize_host_attestation_envelope
from contextplane.arc.schemas.canonical import manifest_claims_digest as compute_manifest_claims_digest
from contextplane.arc.service.challenge import NONCE_BYTES
from contextplane.exceptions import RegistryError
from contextplane.types import Clock

# The attestation's own issued_at/expires_at window, independent of (and
# matching in width) the challenge's own five-minute TTL. A host declaring a
# wider window is not trusted to have self-limited correctly -- ARC checks it.
ATTESTATION_FRESHNESS = datetime.timedelta(minutes=5)

REQUIRED_BUNDLE_CONTENT_PROFILE = "arc_context_bundle_content_v1"

# Domain separation over the canonical envelope bytes. A signature produced
# for some other profile must never verify here even if payload bytes
# happened to coincide.
_SIGNING_DOMAIN = b"ARC-HOST-ATTESTATION-V1\x00"

_MIRRORED_FIELDS: tuple[str, ...] = ("repository_identity", "environment", "data_sensitivity", "session_id")


class AttestationVerificationError(RegistryError):
    """An attestation failed one of the checks that make it trustworthy.

    One type for every failure. The caller's outward response is identical
    regardless of which check failed: `blocked_manifest_unverified`, no
    receipt -- so the difference between them matters only for the message,
    not for control flow.
    """


@dataclasses.dataclass(frozen=True)
class HostSignerKey:
    """A registered host attestation key, as verification needs it.

    Carries only what checking one signature and its binding requires. The
    row locking that makes revocation linearize against concurrent
    resolutions lives in `HostSignerKeyRegistry`, not here.
    """

    signer_key_id: str
    host_id: str
    tenant_id: uuid.UUID
    attestation_profile: str
    public_key: bytes
    valid_from: datetime.datetime
    valid_until: datetime.datetime | None
    revoked_at: datetime.datetime | None

    def usable_at(self, moment: datetime.datetime) -> bool:
        """Rotation alone never invalidates a key -- only revocation or its own
        validity window does. An old key remains valid for attestations
        already issued under it until its own expiry, which is why this takes
        the moment being verified rather than reading a clock.
        """
        if self.revoked_at is not None and moment >= self.revoked_at:
            return False
        if moment < self.valid_from:
            return False
        return not (self.valid_until is not None and moment >= self.valid_until)


class HostSignerKeyLookup(Protocol):
    """The one capability this module needs from key storage.

    Takes the caller's own session rather than opening one, and must run
    inside the caller's open transaction: the real implementation locks the
    row `FOR SHARE`, and revocation takes the same row in a plain `UPDATE`
    (which is an implicit `FOR UPDATE` in Postgres) -- the two only linearize
    against each other if both read and write inside a transaction rather
    than from a cached snapshot or a connection of their own.
    """

    async def get(self, session: AsyncSession, signer_key_id: str) -> HostSignerKey | None: ...


class SignatureVerifier(Protocol):
    def __call__(self, public_key: bytes, signature: bytes, payload: bytes) -> bool: ...


@dataclasses.dataclass(frozen=True)
class AttestationEnvelope:
    """`request.attestation`, already parsed into typed fields.

    `payload` stays a plain string-keyed mapping rather than its own
    dataclass: it is exactly the wire object the host canonicalized and
    signed, and re-typing individual fields (`arc_nonce` to `bytes`, say)
    would mean re-encoding them to reproduce the signed bytes -- an easy
    place for a byte-for-byte mismatch to creep in. Decoding happens only
    where a value is actually consumed as bytes.
    """

    profile: str
    signer_key_id: str
    attestation_id: str
    issued_at: datetime.datetime
    expires_at: datetime.datetime
    payload: dict[str, str]
    signature: str  # base64, exactly as received


@dataclasses.dataclass(frozen=True)
class ManifestClaims:
    """The manifest fields this module needs to recompute the claims digest
    and check mirrored fields.

    `SelectionService` reads the rest of the manifest (`entity_ids`,
    `domain_ids`, ...) directly; this subset exists only so attestation
    verification does not need to depend on ARC's selection layer.
    """

    session_id: str
    intent_kind: str
    requested_action_classes: tuple[str, ...]
    entity_ids: tuple[str, ...]
    domain_ids: tuple[str, ...]
    environment: str
    data_sensitivity: str
    repository_identity: str
    supported_context_bundle_content_profiles: tuple[str, ...]
    intent_summary: str | None = None

    def as_claims_dict(self) -> dict[str, object]:
        claims: dict[str, object] = {
            "session_id": self.session_id,
            "intent_kind": self.intent_kind,
            "requested_action_classes": list(self.requested_action_classes),
            "entity_ids": list(self.entity_ids),
            "domain_ids": list(self.domain_ids),
            "environment": self.environment,
            "data_sensitivity": self.data_sensitivity,
            "repository_identity": self.repository_identity,
            "supported_context_bundle_content_profiles": list(self.supported_context_bundle_content_profiles),
        }
        if self.intent_summary is not None:
            claims["intent_summary"] = self.intent_summary
        return claims


@dataclasses.dataclass(frozen=True)
class VerifiedAttestation:
    """What a successful verification hands back.

    `arc_nonce_b64` stays base64-encoded, matching the wire and the
    challenge-issuance response. Decoding it is the caller's job: only the
    caller knows it is about to pass this to `ChallengeService`, and this
    module has already proven it decodes to exactly `NONCE_BYTES`.
    """

    attestation_id: str
    signer_key_id: str
    host_id: str
    tenant_id: uuid.UUID
    session_id: str
    manifest_claims_digest: str
    arc_nonce_b64: str


def _decode_b64(value: str, *, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = f"{field} is not valid base64"
        raise AttestationVerificationError(msg) from exc


def _ed25519_verify(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    """Verify an Ed25519 signature, returning False rather than raising.

    A malformed key, a malformed signature, and a wrong signature are all
    "not verified" to the caller, and collapsing them here means the caller
    never has to catch a cryptography-library exception to get a boolean.
    """
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError):
        return False
    return True


class AttestationService:
    """Verifies one `arc_host_attestation_v1` envelope against a manifest."""

    def __init__(
        self,
        key_lookup: HostSignerKeyLookup,
        *,
        clock: Clock,
        verifier: SignatureVerifier | None = None,
    ) -> None:
        self._key_lookup = key_lookup
        self._clock = clock
        self._verifier = verifier or _ed25519_verify

    async def verify_attestation(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        host_id: str,
        envelope: AttestationEnvelope,
        manifest: ManifestClaims,
    ) -> VerifiedAttestation:
        """Verify `envelope` against `manifest`.

        `session` must be the caller's own, open resolution transaction: it
        is passed straight through to the key lookup so a `FOR SHARE` lock
        it takes is held for the lifetime of that transaction, not released
        the instant this method returns.
        """
        if envelope.profile != "arc_host_attestation_v1":
            msg = f"unsupported attestation profile {envelope.profile!r}"
            raise AttestationVerificationError(msg)

        now = self._clock.now()

        if envelope.expires_at - envelope.issued_at > ATTESTATION_FRESHNESS:
            msg = f"attestation {envelope.attestation_id!r} declares a validity window wider than five minutes"
            raise AttestationVerificationError(msg)
        if envelope.expires_at <= now:
            msg = f"attestation {envelope.attestation_id!r} expired at {envelope.expires_at.isoformat()}"
            raise AttestationVerificationError(msg)

        key = await self._key_lookup.get(session, envelope.signer_key_id)
        if key is None:
            msg = f"no host attestation key registered for {envelope.signer_key_id!r}"
            raise AttestationVerificationError(msg)
        if key.attestation_profile != envelope.profile:
            msg = f"host attestation key {envelope.signer_key_id!r} is not registered for {envelope.profile!r}"
            raise AttestationVerificationError(msg)
        if key.tenant_id != tenant_id or key.host_id != host_id:
            msg = f"host attestation key {envelope.signer_key_id!r} is not registered to this tenant and host"
            raise AttestationVerificationError(msg)
        if not key.usable_at(now):
            msg = f"host attestation key {envelope.signer_key_id!r} is expired or revoked"
            raise AttestationVerificationError(msg)

        signature = _decode_b64(envelope.signature, field="signature")
        try:
            canonical_envelope = canonicalize_host_attestation_envelope(
                {
                    "profile": envelope.profile,
                    "signer_key_id": envelope.signer_key_id,
                    "attestation_id": envelope.attestation_id,
                    "issued_at": envelope.issued_at,
                    "expires_at": envelope.expires_at,
                    "payload": envelope.payload,
                }
            )
        except CanonicalizationError as exc:
            # A malformed envelope (missing/unknown field, non-NFC string, ...)
            # is not trustworthy either -- it collapses to the same outward
            # failure as every other check here, not a different error class.
            msg = f"attestation {envelope.attestation_id!r} envelope does not canonicalize: {exc}"
            raise AttestationVerificationError(msg) from exc
        signing_input = _SIGNING_DOMAIN + canonical_envelope
        if not self._verifier(key.public_key, signature, signing_input):
            msg = f"attestation {envelope.attestation_id!r} signature did not verify"
            raise AttestationVerificationError(msg)

        payload = envelope.payload
        if payload.get("host_id") != host_id:
            msg = "attestation payload host_id does not match the authenticated host"
            raise AttestationVerificationError(msg)
        for field in _MIRRORED_FIELDS:
            if payload.get(field) != getattr(manifest, field):
                msg = f"attestation payload {field} does not mirror the manifest"
                raise AttestationVerificationError(msg)

        computed_digest = compute_manifest_claims_digest(manifest.as_claims_dict())
        if payload.get("manifest_claims_digest") != computed_digest:
            msg = "attestation payload manifest_claims_digest does not match the recomputed digest"
            raise AttestationVerificationError(msg)

        if REQUIRED_BUNDLE_CONTENT_PROFILE not in manifest.supported_context_bundle_content_profiles:
            msg = f"manifest does not declare support for {REQUIRED_BUNDLE_CONTENT_PROFILE!r}"
            raise AttestationVerificationError(msg)

        arc_nonce_b64 = payload.get("arc_nonce", "")
        nonce = _decode_b64(arc_nonce_b64, field="payload.arc_nonce")
        if len(nonce) != NONCE_BYTES:
            msg = f"attestation payload arc_nonce is {len(nonce)} bytes, expected {NONCE_BYTES}"
            raise AttestationVerificationError(msg)

        return VerifiedAttestation(
            attestation_id=envelope.attestation_id,
            signer_key_id=envelope.signer_key_id,
            host_id=host_id,
            tenant_id=tenant_id,
            session_id=payload["session_id"],
            manifest_claims_digest=computed_digest,
            arc_nonce_b64=arc_nonce_b64,
        )


class HostSignerKeyRegistry:
    """The database-backed `HostSignerKeyLookup`, plus revocation.

    Both sides take the same row, which is the whole point. Verification
    reads it `FOR SHARE`; revocation writes it (an `UPDATE` takes a row-level
    exclusive lock in Postgres, which conflicts with `FOR SHARE`). So a
    revocation running concurrently with a resolution either commits first --
    and the resolution then blocks, re-reads, and rejects the now-revoked key
    -- or commits second, waiting until the resolution's transaction ends.
    There is no interleaving in which a resolution verifies against a key
    whose revocation has already committed.

    That guarantee is entirely dependent on both operations running inside a
    transaction the caller keeps open, which is why neither method opens a
    session of its own.
    """

    async def get(self, session: AsyncSession, signer_key_id: str) -> HostSignerKey | None:
        row = (
            await session.execute(
                text(
                    "SELECT signer_key_id, host_id, tenant_id, attestation_profile, public_key, "
                    "       valid_from, valid_until, revoked_at "
                    "FROM arc_host_attestation_keys WHERE signer_key_id = :kid "
                    "FOR SHARE"
                ),
                {"kid": signer_key_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return HostSignerKey(
            signer_key_id=row.signer_key_id,
            host_id=row.host_id,
            tenant_id=row.tenant_id,
            attestation_profile=row.attestation_profile,
            public_key=base64.b64decode(row.public_key, validate=True),
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            revoked_at=row.revoked_at,
        )

    async def revoke(self, session: AsyncSession, signer_key_id: str, *, revoked_at: datetime.datetime) -> bool:
        """Revoke a key. Returns False if it was already revoked.

        Idempotent rather than an error on re-revocation: an operator
        revoking a key twice has got what they wanted both times, and the
        `revoked_at IS NULL` guard means the first revocation's timestamp is
        the one that stands -- moving it later could retroactively legitimize
        an attestation that was rejected in between.
        """
        result = await session.execute(
            text(
                "UPDATE arc_host_attestation_keys SET revoked_at = :at "
                "WHERE signer_key_id = :kid AND revoked_at IS NULL"
            ),
            {"kid": signer_key_id, "at": revoked_at},
        )
        affected: int = result.rowcount  # type: ignore[attr-defined]
        return affected == 1


__all__ = [
    "AttestationEnvelope",
    "AttestationService",
    "AttestationVerificationError",
    "HostSignerKey",
    "HostSignerKeyLookup",
    "HostSignerKeyRegistry",
    "ManifestClaims",
    "SignatureVerifier",
    "VerifiedAttestation",
]
