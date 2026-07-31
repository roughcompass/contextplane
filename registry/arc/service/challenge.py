"""Challenge-nonce derivation.

A challenge's nonce is never stored. Only its digest is, and the raw value is
re-derived on demand from the challenge's immutable ID under a versioned key.

That indirection buys a specific property. An exact challenge retry has to return
the *same* nonce — a caller that retries with an identical payload must not get a
second, different challenge — but storing a recoverable nonce to satisfy that
would put the secret at rest in the table, where a database read is enough to
forge an attestation. Deriving instead means the table holds only a digest, and
reproducing a nonce additionally requires the derivation key.

Rotation is why the key ID is stored alongside the digest: a nonce derived under
a retired key must stay reproducible for the five-minute challenge window plus
clock skew, so a rotation cannot invalidate challenges already in flight.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import uuid

from registry.arc.service.signing import (
    KeyPurpose,
    KeyRecord,
    KeyUnavailableError,
    PurposeBoundKeyProvider,
)

# Versioned so a derivation change is explicit rather than silently producing
# different nonces for the same inputs.
NONCE_DERIVATION_PROFILE = "arc_challenge_nonce_v1"

NONCE_BYTES = 32

# The challenge lifetime, plus the skew allowance a retired derivation key must
# remain available for. A rotation must not invalidate in-flight challenges.
CHALLENGE_TTL = datetime.timedelta(minutes=5)
ROTATION_SKEW_ALLOWANCE = datetime.timedelta(minutes=1)
RETIRED_KEY_RETENTION = CHALLENGE_TTL + ROTATION_SKEW_ALLOWANCE


def nonce_digest(nonce: bytes) -> str:
    """The value stored in `arc_context_challenges.arc_nonce_digest`."""
    return hashlib.sha256(nonce).hexdigest()


class ChallengeNonceDeriver(PurposeBoundKeyProvider):
    """Derives a challenge nonce from its immutable ID under a versioned key.

    Deterministic by design: the same challenge ID and bindings under the same key
    always yield the same nonce, which is what makes exact retry work without
    persisting the secret.
    """

    purpose = KeyPurpose.CHALLENGE_NONCE_DERIVATION

    def __init__(
        self,
        secrets: dict[str, bytes],
        *,
        active_key_id: str | None,
        records: dict[str, KeyRecord] | None = None,
    ) -> None:
        self._secrets = secrets
        self._active_key_id = active_key_id
        self._records = records or {}

    def _load(self, key_id: str) -> KeyRecord | None:
        return self._records.get(key_id)

    @property
    def active_key_id(self) -> str:
        if self._active_key_id is None:
            msg = (
                "no active ARC challenge-nonce derivation key is configured; "
                "refusing to issue challenges whose nonce cannot be reproduced"
            )
            raise KeyUnavailableError(msg)
        return self._active_key_id

    def derive(
        self,
        challenge_id: uuid.UUID,
        *,
        host_id: str,
        session_id: str,
        manifest_claims_digest: str,
        key_id: str | None = None,
    ) -> bytes:
        """Derive the nonce for a challenge.

        The authenticated bindings are part of the derivation input, not just of
        the surrounding row. A nonce that depended on the challenge ID alone would
        be identical across two challenges that differ only in host or claims,
        which is exactly the substitution the binding exists to prevent.
        """
        resolved = key_id or self.active_key_id
        secret = self._secrets.get(resolved)
        if secret is None:
            msg = (
                f"no derivation secret for key {resolved!r}; a nonce derived under "
                "a key that is no longer held cannot be reproduced"
            )
            raise KeyUnavailableError(msg)

        # Length-prefixed so no two different field splits can produce the same
        # byte string. Plain concatenation would let ("ab", "c") and ("a", "bc")
        # collide.
        parts = (
            NONCE_DERIVATION_PROFILE.encode("ascii"),
            challenge_id.bytes,
            host_id.encode("utf-8"),
            session_id.encode("utf-8"),
            manifest_claims_digest.encode("ascii"),
        )
        message = b"".join(len(p).to_bytes(4, "big") + p for p in parts)
        return hmac.new(secret, message, hashlib.sha256).digest()[:NONCE_BYTES]

    def retired_key_is_still_required(self, retired_at: datetime.datetime, now: datetime.datetime) -> bool:
        """Whether a rotated-out key must still be held to reproduce nonces.

        Dropping it sooner breaks exact retry for challenges issued just before
        the rotation.
        """
        return now - retired_at <= RETIRED_KEY_RETENTION
