"""Challenge nonces are derived, not stored — and bound to their challenge."""

from __future__ import annotations

import datetime
import hashlib
import uuid

import pytest

from registry.arc.service.challenge import (
    NONCE_BYTES,
    NONCE_DERIVATION_PROFILE,
    RETIRED_KEY_RETENTION,
    ChallengeNonceDeriver,
    nonce_digest,
)
from registry.arc.service.signing import KeyUnavailableError

_CID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_BINDINGS = {
    "host_id": "host-1",
    "session_id": "session-1",
    "manifest_claims_digest": "a" * 64,
}


def _deriver(active: str | None = "nk1") -> ChallengeNonceDeriver:
    return ChallengeNonceDeriver({"nk1": b"secret-one", "nk0": b"secret-zero"}, active_key_id=active)


def test_derivation_is_deterministic_so_exact_retry_reproduces_the_nonce() -> None:
    """This is the property that lets the table store only a digest."""
    d = _deriver()
    assert d.derive(_CID, **_BINDINGS) == d.derive(_CID, **_BINDINGS)


def test_nonce_is_the_expected_length() -> None:
    assert len(_deriver().derive(_CID, **_BINDINGS)) == NONCE_BYTES


def test_a_different_challenge_id_yields_a_different_nonce() -> None:
    d = _deriver()
    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert d.derive(_CID, **_BINDINGS) != d.derive(other, **_BINDINGS)


@pytest.mark.parametrize("field", ["host_id", "session_id", "manifest_claims_digest"])
def test_every_binding_changes_the_nonce(field: str) -> None:
    """A nonce keyed only on the challenge ID would be identical across two
    challenges differing in host or claims — exactly the substitution the
    binding exists to prevent."""
    d = _deriver()
    altered = dict(_BINDINGS)
    altered[field] = "b" * 64 if field.endswith("digest") else "other"
    assert d.derive(_CID, **_BINDINGS) != d.derive(_CID, **altered)  # type: ignore[arg-type]


def test_field_boundaries_cannot_be_shifted_to_collide() -> None:
    """Length-prefixed inputs: ("ab","c") must not hash like ("a","bc").

    Plain concatenation would make those two identical, letting one binding's
    characters be moved into the next while producing the same nonce.
    """
    d = _deriver()
    a = d.derive(_CID, host_id="ab", session_id="c", manifest_claims_digest="x" * 64)
    b = d.derive(_CID, host_id="a", session_id="bc", manifest_claims_digest="x" * 64)
    assert a != b


def test_a_different_key_yields_a_different_nonce() -> None:
    d = _deriver()
    assert d.derive(_CID, **_BINDINGS, key_id="nk1") != d.derive(_CID, **_BINDINGS, key_id="nk0")


def test_the_stored_digest_is_sha256_of_the_nonce() -> None:
    nonce = _deriver().derive(_CID, **_BINDINGS)
    assert nonce_digest(nonce) == hashlib.sha256(nonce).hexdigest()
    assert len(nonce_digest(nonce)) == 64


def test_the_nonce_is_not_recoverable_from_its_digest() -> None:
    """The table holds the digest; reproducing the nonce also needs the key.

    Asserted as a property of the interface: there is no reverse function, and a
    deriver without the secret cannot produce the nonce.
    """
    nonce = _deriver().derive(_CID, **_BINDINGS)
    keyless = ChallengeNonceDeriver({}, active_key_id="nk1")
    with pytest.raises(KeyUnavailableError):
        keyless.derive(_CID, **_BINDINGS)
    assert nonce_digest(nonce) != nonce.hex()


def test_no_active_key_refuses_to_derive() -> None:
    """Issuing a challenge whose nonce cannot be reproduced breaks exact retry."""
    with pytest.raises(KeyUnavailableError, match="refusing to issue challenges"):
        _deriver(active=None).derive(_CID, **_BINDINGS)


def test_a_dropped_key_cannot_reproduce_its_nonces() -> None:
    with pytest.raises(KeyUnavailableError, match="no longer held"):
        _deriver().derive(_CID, **_BINDINGS, key_id="rotated-away")


def test_retired_key_is_retained_across_the_challenge_window_plus_skew() -> None:
    """Dropping it sooner breaks retry for challenges issued just before rotation."""
    d = _deriver()
    retired = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
    assert d.retired_key_is_still_required(retired, retired) is True
    assert d.retired_key_is_still_required(retired, retired + RETIRED_KEY_RETENTION) is True
    assert (
        d.retired_key_is_still_required(retired, retired + RETIRED_KEY_RETENTION + datetime.timedelta(seconds=1))
        is False
    )


def test_retention_exceeds_the_challenge_lifetime() -> None:
    """Otherwise a challenge could outlive the key needed to reproduce its nonce."""
    assert RETIRED_KEY_RETENTION > datetime.timedelta(minutes=5)


def test_profile_is_versioned_and_part_of_the_derivation() -> None:
    """A derivation change must be an explicit version bump, not a silent shift."""
    assert NONCE_DERIVATION_PROFILE.endswith("_v1")
