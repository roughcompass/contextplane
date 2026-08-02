"""Continuation tokens: sealed, self-describing, and single-use.

A page token is state the client holds and hands back. Everything about its
design follows from that one fact: it is in the hands of the party whose
access it constrains, so nothing in it can be trusted merely because it came
back looking right.

**Sealed, not signed.** The token is an AEAD envelope, so its contents are
both unreadable and unforgeable. A signed-but-readable token would leak the
cursor position, the artifact-state digest, and the cumulative byte counts --
a map of what the paging is doing, handed to the caller.

**Bound to its whole context.** The bindings are additional authenticated
data rather than payload: tenant, actor, host, receipt, context handle, and
the canonical base request. A token issued for one receipt therefore fails
outright when presented against another, instead of decrypting to a position
that happens to be valid there too.

**Short-lived and single-use.** Five minutes, and the consuming event records
the token's digest under a unique index, so a replayed token is refused by
the database rather than by a check that could be forgotten. Expiry is
carried *inside* the sealed payload: a client-visible expiry field would be
a client-editable one.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import json
import secrets as _secrets_module
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from registry.arc.service.signing import KeyPurpose, KeyRecord, KeyUnavailableError, PurposeBoundKeyProvider

# Versioned so a format change is an explicit bump rather than a token that
# silently decodes under new rules.
CONTINUATION_TOKEN_PROFILE = "arc_continuation_token_v1"

TOKEN_TTL = datetime.timedelta(minutes=5)

# AES-GCM standard nonce length. Never reused under one key: a repeat would
# leak the XOR of two plaintexts and forfeit integrity for both.
_NONCE_BYTES = 12

# The total a single paging chain may return, however many pages it takes.
# Without it, paging is an unbounded read of everything the receipt selected,
# one bounded page at a time.
MAX_CHAIN_BYTES = 64 * 1024


class ContinuationTokenError(Exception):
    """A token was absent, malformed, expired, replayed, or wrongly bound.

    One type for all of them, deliberately. Telling a caller *which* of those
    it was distinguishes "this token is expired" from "this token was never
    yours", and that difference is exactly the probing signal a token's
    opacity exists to deny.
    """


class ContinuationTokenProvider(PurposeBoundKeyProvider):
    """Holds the AEAD key that seals page tokens.

    Its own purpose and its own provider class, like every other ARC key: a
    key shared with content encryption would mean a leaked page token could
    decrypt stored content.
    """

    purpose = KeyPurpose.CONTINUATION_TOKEN

    def __init__(self, secrets: dict[str, bytes], *, active_key_id: str | None) -> None:
        self._secrets = secrets
        self._active_key_id = active_key_id
        self._records = {
            key_id: KeyRecord(key_id=key_id, purpose=self.purpose, algorithm="AES-256-GCM")
            for key_id in secrets
        }

    def _load(self, key_id: str) -> KeyRecord | None:
        return self._records.get(key_id)

    @property
    def active_key_id(self) -> str:
        if self._active_key_id is None:
            msg = "no active ARC continuation-token key is configured; refusing to issue unsealed page state"
            raise KeyUnavailableError(msg)
        return self._active_key_id

    def secret(self, key_id: str) -> bytes:
        """The raw key bytes, after the base class has checked the purpose."""
        self.get(key_id)
        secret = self._secrets.get(key_id)
        if secret is None:
            msg = f"no continuation-token secret for key {key_id!r}"
            raise KeyUnavailableError(msg)
        return secret


@dataclasses.dataclass(frozen=True)
class PageBinding:
    """What a token is tied to. Never inside the ciphertext -- it is the AAD.

    Putting these in the authenticated data rather than the payload means a
    mismatch fails at decryption, before any field is read. If they were
    payload, the token would decrypt successfully and the caller would then
    be relying on someone remembering to compare each one.
    """

    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    host_id: str
    receipt_id: uuid.UUID
    context_handle_digest: str
    base_request_digest: str

    def as_aad(self) -> bytes:
        """Length-prefixed, so no two different bindings share an encoding.

        Plain concatenation would let `("ab", "c")` and `("a", "bc")` produce
        identical AAD -- and a token bound to one of those would verify
        against the other.
        """
        parts = (
            CONTINUATION_TOKEN_PROFILE.encode("ascii"),
            self.tenant_id.bytes,
            self.actor_id.bytes,
            self.host_id.encode("utf-8"),
            self.receipt_id.bytes,
            self.context_handle_digest.encode("ascii"),
            self.base_request_digest.encode("ascii"),
        )
        return b"".join(len(p).to_bytes(4, "big") + p for p in parts)


@dataclasses.dataclass(frozen=True)
class PageState:
    """The cursor, sealed inside the token.

    `artifact_state_digest` is what makes a token stop being valid when the
    underlying artifacts change. Paging through a set that is being revoked
    or superseded underneath the caller would otherwise return a mix of old
    and new -- pages that were never simultaneously true.
    """

    page_number: int
    next_position: int
    cumulative_bytes: int
    cumulative_results: int
    artifact_state_digest: str
    issued_at: datetime.datetime
    expires_at: datetime.datetime

    def as_payload(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "next_position": self.next_position,
            "cumulative_bytes": self.cumulative_bytes,
            "cumulative_results": self.cumulative_results,
            "artifact_state_digest": self.artifact_state_digest,
            "issued_at": self.issued_at.astimezone(datetime.UTC).isoformat(),
            "expires_at": self.expires_at.astimezone(datetime.UTC).isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> PageState:
        try:
            return cls(
                page_number=_as_int(payload["page_number"]),
                next_position=_as_int(payload["next_position"]),
                cumulative_bytes=_as_int(payload["cumulative_bytes"]),
                cumulative_results=_as_int(payload["cumulative_results"]),
                artifact_state_digest=str(payload["artifact_state_digest"]),
                issued_at=datetime.datetime.fromisoformat(str(payload["issued_at"])),
                expires_at=datetime.datetime.fromisoformat(str(payload["expires_at"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A token that decrypted but does not parse means the key is
            # right and the format is not -- a version skew, not an attack,
            # but equally unusable.
            msg = "continuation token payload is malformed"
            raise ContinuationTokenError(msg) from exc


def _as_int(value: object) -> int:
    """Accept only a real integer from a decoded payload.

    `int("7")` would happily accept a string here, which would let a token
    whose counters were the wrong type still parse. The counters gate the
    per-chain byte cap, so a loose parse there is a loose cap.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"expected an integer in continuation token payload, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def token_digest(token: str) -> str:
    """What the consuming receipt event records, under a unique index.

    The digest rather than the token: the table then proves single use
    without storing material that would let a database reader resume
    someone else's paging.
    """
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def issue(
    provider: ContinuationTokenProvider,
    *,
    binding: PageBinding,
    state: PageState,
) -> str:
    """Seal `state` into an opaque token bound to `binding`."""
    key_id = provider.active_key_id
    secret = provider.secret(key_id)
    nonce = _fresh_nonce()
    payload = json.dumps(
        {"profile": CONTINUATION_TOKEN_PROFILE, "state": state.as_payload()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESGCM(secret).encrypt(nonce, payload, binding.as_aad())
    # Key ID travels in the clear so a rotated-out token can still be opened
    # by the key that sealed it. It is not a secret, and the AAD covers the
    # bindings regardless.
    envelope = b"".join(
        [
            len(key_id).to_bytes(2, "big"),
            key_id.encode("ascii"),
            nonce,
            ciphertext,
        ]
    )
    return base64.urlsafe_b64encode(envelope).decode("ascii")


def open_token(
    provider: ContinuationTokenProvider,
    token: str,
    *,
    binding: PageBinding,
    now: datetime.datetime,
) -> PageState:
    """Unseal and validate a token, or refuse it.

    Expiry is checked against `now` supplied by the caller rather than a
    clock read here, so the whole page request is evaluated at one instant.
    """
    try:
        envelope = base64.urlsafe_b64decode(token.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        msg = "continuation token is not valid base64url"
        raise ContinuationTokenError(msg) from exc

    if len(envelope) < 2:
        msg = "continuation token is truncated"
        raise ContinuationTokenError(msg)
    key_len = int.from_bytes(envelope[:2], "big")
    if len(envelope) < 2 + key_len + _NONCE_BYTES:
        msg = "continuation token is truncated"
        raise ContinuationTokenError(msg)

    key_id = envelope[2 : 2 + key_len].decode("ascii", errors="replace")
    nonce = envelope[2 + key_len : 2 + key_len + _NONCE_BYTES]
    ciphertext = envelope[2 + key_len + _NONCE_BYTES :]

    try:
        secret = provider.secret(key_id)
    except KeyUnavailableError as exc:
        # A token sealed under a key this deployment no longer holds is
        # indistinguishable, to the caller, from one that was never valid.
        msg = "continuation token cannot be opened"
        raise ContinuationTokenError(msg) from exc

    try:
        plaintext = AESGCM(secret).decrypt(nonce, ciphertext, binding.as_aad())
    except InvalidTag as exc:
        # Wrong binding, tampered ciphertext, and wrong key all land here,
        # and all mean the same thing to the caller.
        msg = "continuation token is invalid for this request"
        raise ContinuationTokenError(msg) from exc

    try:
        decoded = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        msg = "continuation token payload is malformed"
        raise ContinuationTokenError(msg) from exc
    if not isinstance(decoded, dict) or decoded.get("profile") != CONTINUATION_TOKEN_PROFILE:
        msg = "continuation token profile is unsupported"
        raise ContinuationTokenError(msg)

    state = PageState.from_payload(decoded.get("state", {}))
    if state.expires_at <= now:
        msg = "continuation token has expired"
        raise ContinuationTokenError(msg)
    return state


def _fresh_nonce() -> bytes:
    return _secrets_module.token_bytes(_NONCE_BYTES)


__all__ = [
    "CONTINUATION_TOKEN_PROFILE",
    "MAX_CHAIN_BYTES",
    "TOKEN_TTL",
    "ContinuationTokenError",
    "ContinuationTokenProvider",
    "PageBinding",
    "PageState",
    "issue",
    "open_token",
    "token_digest",
]
