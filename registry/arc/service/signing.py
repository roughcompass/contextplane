"""Key purposes, and the provider base that keeps them apart.

ARC uses seven distinct cryptographic key purposes. Sharing a key between any two
of them is a real vulnerability rather than an untidiness: a receipt-signing key
that also verifies host attestations lets a host mint receipts, and a
content-encryption key that also seals continuation tokens lets a leaked page
token decrypt stored content.

The usual way to express that is a `purpose` constant passed to a shared
provider. This module deliberately does not do that, because a constant is one
mistyped argument away from being wrong and nothing fails until an auditor
notices. Instead each purpose gets its own provider class, and the base refuses
to load a key whose recorded purpose does not match the class asking for it.
Purpose confusion then requires editing this file, which is a reviewable act.
"""

from __future__ import annotations

import base64
import datetime
import enum
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from registry.exceptions import RegistryError


class KeyPurpose(enum.StrEnum):
    """The seven key purposes. One provider class each; never shared.

    `StrEnum` so a purpose serializes to a stable string in key manifests and
    audit records without a separate mapping to drift.
    """

    RECEIPT_EVENT_SIGNING = "arc_receipt_event_v1"
    """Server signature over receipt-event digests. Asymmetric; public half is
    published so an external verifier can check a receipt without a secret."""

    HOST_ATTESTATION_VERIFICATION = "arc_host_attestation_v1"
    """Verify-only. ARC holds host *public* keys and never signs with these."""

    APPROVAL_EVIDENCE_VERIFICATION = "arc_approval_evidence_v1"
    """Verify-only, for operator-signed approval evidence."""

    CHALLENGE_NONCE_DERIVATION = "arc_challenge_nonce_v1"
    """Symmetric derivation of a challenge nonce from its immutable ID, so no
    recoverable nonce is ever stored."""

    CONTENT_ENCRYPTION = "arc_content_v1"
    """Envelope encryption for governed source bodies and directive prose."""

    RESPONSE_REPLAY_ENCRYPTION = "arc_response_replay_v1"
    """Seals the retained bounded response so an exact retry replays it."""

    CONTINUATION_TOKEN = "arc_continuation_token_v1"
    """AEAD for JIT page tokens."""


class KeyPurposeMismatchError(RegistryError):
    """A key was offered to a provider for a different purpose.

    Never caught to continue. Reaching this means either a misconfiguration or an
    attempt to reuse key material across purposes, and both are fail-closed.
    """

    def __init__(self, key_id: str, recorded: KeyPurpose | str, requested: KeyPurpose) -> None:
        super().__init__(
            f"key {key_id!r} is recorded for purpose {recorded!s} but was offered "
            f"to a {requested!s} provider; ARC never shares key material across "
            "purposes"
        )
        self.key_id = key_id
        self.recorded = recorded
        self.requested = requested


class KeyUnavailableError(RegistryError):
    """No usable key for a required purpose. Fail closed, never degrade.

    ARC's value is evidence. An operation that would otherwise produce an
    unsigned receipt or unencrypted content must fail instead.
    """


@dataclass(frozen=True)
class KeyRecord:
    """A key as the provider sees it. Never carries private material.

    Private key bytes live only inside a concrete provider's own storage or an
    external custody service. Nothing that reaches ARC's tables or logs holds
    them, which is why this record has no field for them.
    """

    key_id: str
    purpose: KeyPurpose
    algorithm: str
    public_key: bytes | None = None
    is_active: bool = True
    is_compromised: bool = False
    # Lifecycle, carried here because the published manifest must state it: a
    # verifier checking an old receipt needs to know when the key was valid and
    # whether it was later compromised, or it cannot weigh what it verified.
    valid_from: datetime.datetime | None = None
    valid_until: datetime.datetime | None = None
    compromised_at: datetime.datetime | None = None
    replacement_key_id: str | None = None

    @property
    def usable_for_signing(self) -> bool:
        """A compromised key stays visible for verification but never signs again."""
        return self.is_active and not self.is_compromised

    def usable_at(self, moment: datetime.datetime) -> bool:
        """Whether this key was within its validity window at `moment`.

        Verification of an old receipt asks about the moment of signing, not
        about now — which is why this takes an argument instead of reading a
        clock.
        """
        if self.valid_from is not None and moment < self.valid_from:
            return False
        return not (self.valid_until is not None and moment > self.valid_until)


class PurposeBoundKeyProvider(ABC):
    """Base for every ARC key provider. One subclass per purpose.

    Subclasses declare `purpose` and implement `_load`. The base is what enforces
    that a loaded key actually belongs to the declaring purpose — subclasses do
    not repeat the check, so they cannot forget it.
    """

    #: Set by each subclass. The base validates every loaded key against it.
    purpose: KeyPurpose

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Catch a provider that forgot to declare its purpose at class-creation
        # time rather than on first use in production.
        if not getattr(cls, "__abstractmethods__", None) and not isinstance(getattr(cls, "purpose", None), KeyPurpose):
            msg = f"{cls.__name__} must declare a KeyPurpose as `purpose`"
            raise TypeError(msg)

    @abstractmethod
    def _load(self, key_id: str) -> KeyRecord | None:
        """Return the key as recorded, or None. Must not validate purpose."""

    def get(self, key_id: str) -> KeyRecord:
        """Load `key_id`, refusing it if it belongs to a different purpose."""
        record = self._load(key_id)
        if record is None:
            msg = f"no key {key_id!r} available for purpose {self.purpose!s}"
            raise KeyUnavailableError(msg)
        if record.purpose is not self.purpose:
            raise KeyPurposeMismatchError(key_id, record.purpose, self.purpose)
        return record

    def get_for_signing(self, key_id: str) -> KeyRecord:
        """Like `get`, and additionally refuses a retired or compromised key."""
        record = self.get(key_id)
        if not record.usable_for_signing:
            state = "compromised" if record.is_compromised else "inactive"
            msg = f"key {key_id!r} is {state} and cannot sign"
            raise KeyUnavailableError(msg)
        return record


# ---------------------------------------------------------------------------
# Receipt-event signing
# ---------------------------------------------------------------------------

RECEIPT_SIGNING_ALGORITHM = "Ed25519"

# Domain separation. Every signature ARC produces covers this tag plus the
# payload, so a signature made for one profile cannot be replayed as another even
# if the payload bytes coincide. Bumping the profile is how a signing-format
# change becomes visible rather than silent.
RECEIPT_EVENT_SIGNATURE_PROFILE = "arc_receipt_event_sig_v1"

_PROFILE_SEPARATOR = b"\x00"


def _iso(moment: datetime.datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


class SignatureVerificationError(RegistryError):
    """A signature did not verify. Fail closed; never treat as unsigned."""


@dataclass(frozen=True)
class KeyManifestEntry:
    """One published row of the receipt-signing key manifest.

    This is what an external verifier reads to check a receipt it was handed.
    Deliberately includes retired and compromised keys: a receipt signed two
    years ago must still be verifiable, and a verifier needs to know the key was
    later compromised in order to weigh it — silently dropping the row would make
    the receipt unverifiable and hide the compromise.
    """

    key_id: str
    algorithm: str
    purpose: str
    public_key_b64: str
    signature_profile: str
    valid_from: str | None
    valid_until: str | None
    compromised_at: str | None
    replacement_key_id: str | None


class ReceiptSigningProvider(PurposeBoundKeyProvider):
    """Signs receipt-event digests with Ed25519, and publishes the public half.

    Deployment-scoped rather than per-tenant: per-tenant signing keys would
    multiply the custody, rotation, and recovery surface without improving tenant
    isolation, which ARC already enforces at the authorization layer.

    Private key material is held by the injected `signer` callable, not by this
    class. That keeps an external custody service (KMS, HSM) a drop-in
    replacement for a local key, and it means nothing here can accidentally
    serialize a secret.
    """

    purpose = KeyPurpose.RECEIPT_EVENT_SIGNING

    def __init__(
        self,
        records: dict[str, KeyRecord],
        *,
        active_key_id: str | None,
        signer: Callable[[str, bytes], bytes] | None = None,
        verifier: Callable[[bytes, bytes, bytes], bool] | None = None,
    ) -> None:
        self._records = records
        self._active_key_id = active_key_id
        self._signer = signer
        self._verifier = verifier or _ed25519_verify

    def _load(self, key_id: str) -> KeyRecord | None:
        return self._records.get(key_id)

    @property
    def active_key_id(self) -> str:
        """The key new signatures use. Raises when none is configured.

        Fail closed: a deployment with no signing key must not produce unsigned
        receipts, because a receipt's whole value is that it is evidence.
        """
        if self._active_key_id is None:
            msg = "no active ARC receipt-signing key is configured; refusing to " "produce unsigned receipt events"
            raise KeyUnavailableError(msg)
        return self._active_key_id

    def self_test(self) -> None:
        """Sign and verify a known value at startup.

        Worth doing eagerly: a misconfigured custody backend otherwise surfaces on
        the first real receipt, which is both the worst time to find out and a
        request that then cannot be completed.
        """
        probe = b"arc-receipt-signing-self-test"
        key_id = self.active_key_id
        signature = self.sign(probe, key_id=key_id)
        if not self.verify(probe, signature, key_id=key_id):
            msg = (
                f"receipt-signing self-test failed for key {key_id!r}: a signature "
                "it just produced did not verify against its own public key"
            )
            raise KeyUnavailableError(msg)

    def _signing_input(self, payload: bytes) -> bytes:
        return RECEIPT_EVENT_SIGNATURE_PROFILE.encode("ascii") + _PROFILE_SEPARATOR + payload

    def sign(self, payload: bytes, *, key_id: str | None = None) -> bytes:
        """Sign `payload` under the domain-separated receipt-event profile."""
        resolved = key_id or self.active_key_id
        record = self.get_for_signing(resolved)
        if self._signer is None:
            msg = (
                f"key {record.key_id!r} has no signer bound; private material lives "
                "in the configured custody backend and none was provided"
            )
            raise KeyUnavailableError(msg)
        return self._signer(record.key_id, self._signing_input(payload))

    def verify(self, payload: bytes, signature: bytes, *, key_id: str) -> bool:
        """Verify against `key_id`'s public half, including retired keys.

        Uses `get`, not `get_for_signing`: verification of an old receipt must
        keep working after its key is retired or found compromised.
        """
        record = self.get(key_id)
        if record.public_key is None:
            msg = f"key {key_id!r} has no public key recorded; cannot verify"
            raise SignatureVerificationError(msg)
        return self._verifier(record.public_key, signature, self._signing_input(payload))

    def key_manifest(self) -> list[KeyManifestEntry]:
        """Every key an external verifier might need, oldest first."""
        entries = [
            KeyManifestEntry(
                key_id=r.key_id,
                algorithm=r.algorithm,
                purpose=str(self.purpose),
                public_key_b64=(base64.b64encode(r.public_key).decode("ascii") if r.public_key else ""),
                signature_profile=RECEIPT_EVENT_SIGNATURE_PROFILE,
                valid_from=_iso(r.valid_from),
                valid_until=_iso(r.valid_until),
                compromised_at=_iso(r.compromised_at),
                replacement_key_id=r.replacement_key_id,
            )
            for r in self._records.values()
            if r.purpose is self.purpose
        ]
        return sorted(entries, key=lambda e: e.key_id)


def _ed25519_verify(public_key: bytes, signature: bytes, payload: bytes) -> bool:
    """Verify an Ed25519 signature, returning False rather than raising.

    A malformed signature and a wrong signature are the same answer to the caller
    — "not verified" — and collapsing them here stops call sites having to catch
    a library exception to reach a boolean.
    """
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError):
        return False
    return True


def ed25519_signer(private_keys: dict[str, bytes]) -> Callable[[str, bytes], bytes]:
    """A local-key signer, for development and tests.

    Production custody is an activation gate, not something this function
    satisfies: a real deployment injects a signer backed by its own KMS or HSM
    instead of holding raw private bytes in process memory.
    """

    def _sign(key_id: str, payload: bytes) -> bytes:
        raw = private_keys.get(key_id)
        if raw is None:
            msg = f"no local private key for {key_id!r}"
            raise KeyUnavailableError(msg)
        return Ed25519PrivateKey.from_private_bytes(raw).sign(payload)

    return _sign
