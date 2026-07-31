"""Key-purpose separation is structural, not a convention.

These assert the property the design depends on: offering a key to a provider for
a different purpose raises, and it raises from the base class so no subclass can
forget the check.
"""

from __future__ import annotations

import pytest

from registry.arc.service.signing import (
    KeyPurpose,
    KeyPurposeMismatchError,
    KeyRecord,
    KeyUnavailableError,
    PurposeBoundKeyProvider,
)


class _StubProvider(PurposeBoundKeyProvider):
    """Minimal provider over an in-memory dict, for testing the base."""

    purpose = KeyPurpose.RECEIPT_EVENT_SIGNING

    def __init__(self, records: dict[str, KeyRecord]) -> None:
        self._records = records

    def _load(self, key_id: str) -> KeyRecord | None:
        return self._records.get(key_id)


class _VerifierProvider(PurposeBoundKeyProvider):
    purpose = KeyPurpose.HOST_ATTESTATION_VERIFICATION

    def __init__(self, records: dict[str, KeyRecord]) -> None:
        self._records = records

    def _load(self, key_id: str) -> KeyRecord | None:
        return self._records.get(key_id)


def _record(purpose: KeyPurpose, **kwargs: object) -> KeyRecord:
    defaults: dict[str, object] = {
        "key_id": "k1",
        "purpose": purpose,
        "algorithm": "Ed25519",
    }
    defaults.update(kwargs)
    return KeyRecord(**defaults)  # type: ignore[arg-type]


def test_there_are_exactly_seven_purposes() -> None:
    """Overview invariant 10 lists seven. A drift here means a shared key."""
    assert len(list(KeyPurpose)) == 7
    assert len({p.value for p in KeyPurpose}) == 7, "duplicate purpose value"


def test_a_key_is_accepted_by_its_own_purpose() -> None:
    provider = _StubProvider({"k1": _record(KeyPurpose.RECEIPT_EVENT_SIGNING)})
    assert provider.get("k1").purpose is KeyPurpose.RECEIPT_EVENT_SIGNING


def test_a_key_from_another_purpose_is_refused() -> None:
    """The case that matters: a signing key offered to a verifier, or vice versa.

    A receipt-signing key that also verified host attestations would let a host
    mint receipts.
    """
    provider = _StubProvider({"k1": _record(KeyPurpose.CONTENT_ENCRYPTION)})
    with pytest.raises(KeyPurposeMismatchError) as excinfo:
        provider.get("k1")
    assert excinfo.value.requested is KeyPurpose.RECEIPT_EVENT_SIGNING
    assert excinfo.value.recorded is KeyPurpose.CONTENT_ENCRYPTION


def test_every_purpose_is_refused_by_every_other_purposes_provider() -> None:
    """Exhaustive rather than representative — one hole is enough to matter."""
    for recorded in KeyPurpose:
        provider = _StubProvider({"k1": _record(recorded)})
        if recorded is KeyPurpose.RECEIPT_EVENT_SIGNING:
            assert provider.get("k1").purpose is recorded
        else:
            with pytest.raises(KeyPurposeMismatchError):
                provider.get("k1")


def test_the_check_lives_in_the_base_so_a_subclass_cannot_skip_it() -> None:
    """`_load` is documented not to validate purpose; `get` is what enforces it."""
    provider = _VerifierProvider({"k1": _record(KeyPurpose.RECEIPT_EVENT_SIGNING)})
    # The subclass's own loader happily returns the wrong-purpose record...
    assert provider._load("k1") is not None
    # ...and the base refuses it anyway.
    with pytest.raises(KeyPurposeMismatchError):
        provider.get("k1")


def test_a_provider_without_a_declared_purpose_fails_at_class_creation() -> None:
    """Caught when the class is defined, not on first use in production."""
    with pytest.raises(TypeError, match="must declare a KeyPurpose"):

        class _Bad(PurposeBoundKeyProvider):
            def _load(self, key_id: str) -> KeyRecord | None:
                return None


def test_missing_key_fails_closed() -> None:
    """No key means the operation fails, not that it proceeds unsigned."""
    provider = _StubProvider({})
    with pytest.raises(KeyUnavailableError):
        provider.get("absent")


def test_compromised_key_verifies_but_never_signs_again() -> None:
    """Verification history must survive compromise or old receipts become unreadable."""
    record = _record(KeyPurpose.RECEIPT_EVENT_SIGNING, is_compromised=True)
    provider = _StubProvider({"k1": record})
    assert provider.get("k1") is record
    with pytest.raises(KeyUnavailableError, match="compromised"):
        provider.get_for_signing("k1")


def test_retired_key_verifies_but_never_signs_again() -> None:
    record = _record(KeyPurpose.RECEIPT_EVENT_SIGNING, is_active=False)
    provider = _StubProvider({"k1": record})
    assert provider.get("k1") is record
    with pytest.raises(KeyUnavailableError, match="inactive"):
        provider.get_for_signing("k1")


def test_key_record_has_nowhere_to_put_private_material() -> None:
    """Private bytes live in custody, never in a record ARC passes around."""
    fields = set(KeyRecord.__dataclass_fields__)
    assert "public_key" in fields
    assert not {f for f in fields if "private" in f or "secret" in f}
