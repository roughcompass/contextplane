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

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass


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


class KeyPurposeMismatchError(Exception):
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


class KeyUnavailableError(Exception):
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

    @property
    def usable_for_signing(self) -> bool:
        """A compromised key stays visible for verification but never signs again."""
        return self.is_active and not self.is_compromised


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
