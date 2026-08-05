"""Receipt-event signing: Ed25519, domain-separated, fail-closed."""

from __future__ import annotations

import base64
import datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from registry.arc.service.signing import (
    RECEIPT_EVENT_SIGNATURE_PROFILE,
    RECEIPT_SIGNING_ALGORITHM,
    KeyPurpose,
    KeyRecord,
    KeyUnavailableError,
    ReceiptSigningProvider,
    SignatureVerificationError,
    ed25519_signer,
)

_PAYLOAD = b"receipt-event-digest-under-test"


def _keypair() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return raw_private, raw_public


def _provider(
    *, active: str | None = "k-active", extra: dict[str, KeyRecord] | None = None
) -> tuple[ReceiptSigningProvider, dict[str, bytes]]:
    raw_private, raw_public = _keypair()
    records = {
        "k-active": KeyRecord(
            key_id="k-active",
            purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
            algorithm=RECEIPT_SIGNING_ALGORITHM,
            public_key=raw_public,
        )
    }
    if extra:
        records.update(extra)
    private_keys = {"k-active": raw_private}
    provider = ReceiptSigningProvider(records, active_key_id=active, signer=ed25519_signer(private_keys))
    return provider, private_keys


def test_sign_then_verify_round_trips() -> None:
    provider, _ = _provider()
    signature = provider.sign(_PAYLOAD)
    assert provider.verify(_PAYLOAD, signature, key_id="k-active") is True


def test_a_tampered_payload_does_not_verify() -> None:
    provider, _ = _provider()
    signature = provider.sign(_PAYLOAD)
    assert provider.verify(_PAYLOAD + b"!", signature, key_id="k-active") is False


def test_a_malformed_signature_returns_false_rather_than_raising() -> None:
    """ "Not verified" is one answer; call sites should not catch a library error."""
    provider, _ = _provider()
    assert provider.verify(_PAYLOAD, b"not-a-signature", key_id="k-active") is False


def test_signatures_are_domain_separated_by_profile() -> None:
    """A raw signature over the bare payload must not verify as a receipt event.

    Without the profile tag, a signature produced for some other ARC purpose over
    the same bytes could be replayed as a receipt-event signature.
    """
    provider, private_keys = _provider()
    bare = Ed25519PrivateKey.from_private_bytes(private_keys["k-active"]).sign(_PAYLOAD)
    assert provider.verify(_PAYLOAD, bare, key_id="k-active") is False


def test_profile_tag_is_part_of_the_signed_input() -> None:
    provider, _ = _provider()
    signed = provider._signing_input(_PAYLOAD)
    assert signed.startswith(RECEIPT_EVENT_SIGNATURE_PROFILE.encode("ascii"))
    assert signed.endswith(_PAYLOAD)


def test_no_active_key_refuses_to_sign_rather_than_producing_nothing() -> None:
    """A deployment with no signing key must fail, not emit unsigned receipts."""
    provider, _ = _provider(active=None)
    with pytest.raises(KeyUnavailableError, match="refusing to produce unsigned"):
        provider.sign(_PAYLOAD)


def test_self_test_passes_on_a_working_key() -> None:
    provider, _ = _provider()
    assert provider.self_test() is None


def test_self_test_fails_when_the_public_half_does_not_match() -> None:
    """The misconfiguration this catches: a key ring whose halves disagree.

    Without an eager self-test it surfaces on the first real receipt, which is
    both the worst moment and a request that then cannot complete.
    """
    _, other_public = _keypair()
    raw_private, _ = _keypair()
    records = {
        "k-active": KeyRecord(
            key_id="k-active",
            purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
            algorithm=RECEIPT_SIGNING_ALGORITHM,
            public_key=other_public,
        )
    }
    provider = ReceiptSigningProvider(
        records,
        active_key_id="k-active",
        signer=ed25519_signer({"k-active": raw_private}),
    )
    with pytest.raises(KeyUnavailableError, match="self-test failed"):
        provider.self_test()


def test_a_retired_key_still_verifies_but_cannot_sign() -> None:
    """Old receipts must stay verifiable after rotation."""
    raw_private, raw_public = _keypair()
    records = {
        "k-old": KeyRecord(
            key_id="k-old",
            purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
            algorithm=RECEIPT_SIGNING_ALGORITHM,
            public_key=raw_public,
            is_active=False,
        )
    }
    provider = ReceiptSigningProvider(records, active_key_id=None, signer=ed25519_signer({"k-old": raw_private}))
    signature = Ed25519PrivateKey.from_private_bytes(raw_private).sign(provider._signing_input(_PAYLOAD))
    assert provider.verify(_PAYLOAD, signature, key_id="k-old") is True
    with pytest.raises(KeyUnavailableError, match="inactive"):
        provider.sign(_PAYLOAD, key_id="k-old")


def test_verify_without_a_recorded_public_key_raises() -> None:
    """Silently returning False would look like a bad signature, not missing config."""
    records = {
        "k-nopub": KeyRecord(
            key_id="k-nopub",
            purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
            algorithm=RECEIPT_SIGNING_ALGORITHM,
            public_key=None,
        )
    }
    provider = ReceiptSigningProvider(records, active_key_id=None)
    with pytest.raises(SignatureVerificationError, match="no public key"):
        provider.verify(_PAYLOAD, b"x", key_id="k-nopub")


def test_signing_without_a_bound_signer_fails_closed() -> None:
    """Private material lives in custody; absent custody means no signature."""
    _, raw_public = _keypair()
    records = {
        "k-active": KeyRecord(
            key_id="k-active",
            purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
            algorithm=RECEIPT_SIGNING_ALGORITHM,
            public_key=raw_public,
        )
    }
    provider = ReceiptSigningProvider(records, active_key_id="k-active")
    with pytest.raises(KeyUnavailableError, match="no signer bound"):
        provider.sign(_PAYLOAD)


def test_manifest_publishes_retired_and_compromised_keys() -> None:
    """Dropping them would make old receipts unverifiable and hide a compromise."""
    _, retired_public = _keypair()
    _, compromised_public = _keypair()
    provider, _ = _provider(
        extra={
            "k-retired": KeyRecord(
                key_id="k-retired",
                purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
                algorithm=RECEIPT_SIGNING_ALGORITHM,
                public_key=retired_public,
                is_active=False,
            ),
            "k-compromised": KeyRecord(
                key_id="k-compromised",
                purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
                algorithm=RECEIPT_SIGNING_ALGORITHM,
                public_key=compromised_public,
                is_compromised=True,
            ),
        }
    )
    manifest = provider.key_manifest()
    assert {e.key_id for e in manifest} == {"k-active", "k-retired", "k-compromised"}
    assert all(e.purpose == str(KeyPurpose.RECEIPT_EVENT_SIGNING) for e in manifest)
    assert all(e.algorithm == RECEIPT_SIGNING_ALGORITHM for e in manifest)


def test_manifest_public_keys_are_base64_and_decode_to_the_raw_key() -> None:
    provider, _ = _provider()
    entry = next(e for e in provider.key_manifest() if e.key_id == "k-active")
    decoded = base64.b64decode(entry.public_key_b64)
    assert decoded == provider.get("k-active").public_key


def test_manifest_excludes_keys_of_other_purposes() -> None:
    """A content key appearing in the receipt manifest would invite misuse."""
    _, other_public = _keypair()
    provider, _ = _provider(
        extra={
            "k-content": KeyRecord(
                key_id="k-content",
                purpose=KeyPurpose.CONTENT_ENCRYPTION,
                algorithm="AES-256-GCM",
                public_key=other_public,
            )
        }
    )
    assert "k-content" not in {e.key_id for e in provider.key_manifest()}


def test_manifest_states_validity_window_and_compromise_time() -> None:
    """A verifier weighing an old receipt needs both, not placeholders."""
    _, raw_public = _keypair()
    valid_from = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    valid_until = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    compromised_at = datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC)
    records = {
        "k1": KeyRecord(
            key_id="k1",
            purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
            algorithm=RECEIPT_SIGNING_ALGORITHM,
            public_key=raw_public,
            valid_from=valid_from,
            valid_until=valid_until,
            compromised_at=compromised_at,
            replacement_key_id="k2",
        )
    }
    entry = ReceiptSigningProvider(records, active_key_id=None).key_manifest()[0]
    assert entry.valid_from == valid_from.isoformat()
    assert entry.valid_until == valid_until.isoformat()
    assert entry.compromised_at == compromised_at.isoformat()
    assert entry.replacement_key_id == "k2"
    assert entry.signature_profile == RECEIPT_EVENT_SIGNATURE_PROFILE


def test_validity_window_is_evaluated_at_the_moment_of_signing() -> None:
    """Verifying an old receipt asks about then, not about now."""
    record = KeyRecord(
        key_id="k1",
        purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
        algorithm=RECEIPT_SIGNING_ALGORITHM,
        valid_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        valid_until=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
    )
    assert record.usable_at(datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)) is True
    assert record.usable_at(datetime.datetime(2025, 12, 31, tzinfo=datetime.UTC)) is False
    assert record.usable_at(datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)) is False


def test_an_unbounded_window_is_always_usable() -> None:
    record = KeyRecord(
        key_id="k1",
        purpose=KeyPurpose.RECEIPT_EVENT_SIGNING,
        algorithm=RECEIPT_SIGNING_ALGORITHM,
    )
    assert record.usable_at(datetime.datetime(1999, 1, 1, tzinfo=datetime.UTC)) is True
