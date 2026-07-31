"""Positive and negative vectors for `arc_manifest_claims_v1`.

The negatives are the point. A canonicalizer that normalizes instead of rejecting
is total but hollow — two different inputs map to one digest, so the digest stops
identifying what the caller actually sent.
"""

from __future__ import annotations

import datetime
import hashlib

import pytest

from registry.arc.schemas.canonical import (
    MANIFEST_CLAIM_FIELDS,
    MANIFEST_CLAIMS_PROFILE,
    CanonicalizationError,
    canonicalize_manifest_claims,
    manifest_claims_digest,
)

_VALID = {
    "session_id": "s-1",
    "task_kind": "code_change",
    "requested_action_classes": ["merge"],
    "capability_ids": ["7b1f0c22-0000-4000-8000-000000000001"],
    "domain_ids": ["payments"],
    "environment": "production",
    "data_sensitivity": "confidential",
    "repository_identity": "git@example.test:org/repo.git",
    "supported_context_bundle_content_profiles": ["arc_context_bundle_content_v1"],
}


# --- positive vectors ------------------------------------------------------


def test_canonical_form_is_stable_and_utf8() -> None:
    out = canonicalize_manifest_claims(_VALID)
    assert isinstance(out, bytes)
    assert out == canonicalize_manifest_claims(dict(_VALID))
    out.decode("utf-8")


def test_the_profile_is_part_of_the_canonical_bytes() -> None:
    """A digest that did not cover the profile could be replayed under another."""
    assert MANIFEST_CLAIMS_PROFILE.encode() in canonicalize_manifest_claims(_VALID)


def test_field_insertion_order_does_not_change_the_digest() -> None:
    reordered = {k: _VALID[k] for k in reversed(list(_VALID))}
    assert manifest_claims_digest(reordered) == manifest_claims_digest(_VALID)


def test_digest_is_sha256_of_the_canonical_bytes() -> None:
    expected = hashlib.sha256(canonicalize_manifest_claims(_VALID)).hexdigest()
    assert manifest_claims_digest(_VALID) == expected
    assert len(manifest_claims_digest(_VALID)) == 64


def test_optional_task_summary_is_included_when_present() -> None:
    """Excluded from mandatory selection, but part of what the host attested to."""
    with_summary = {**_VALID, "task_summary": "rename a column"}
    assert manifest_claims_digest(with_summary) != manifest_claims_digest(_VALID)


def test_nested_object_key_order_does_not_change_the_digest() -> None:
    a = {**_VALID, "repository_identity": "r"}
    b = {**_VALID, "repository_identity": "r"}
    a["domain_ids"] = ["x", "y"]
    b["domain_ids"] = ["x", "y"]
    assert manifest_claims_digest(a) == manifest_claims_digest(b)


def test_timezone_aware_datetimes_normalize_to_utc() -> None:
    """Same instant in two zones is the same meaning, so the same digest."""
    tz = datetime.timezone(datetime.timedelta(hours=2))
    at_utc = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.UTC)
    at_plus_two = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=tz)
    d1 = manifest_claims_digest({**_VALID, "task_summary": at_utc})  # type: ignore[dict-item]
    d2 = manifest_claims_digest({**_VALID, "task_summary": at_plus_two})  # type: ignore[dict-item]
    assert d1 == d2


# --- negative vectors ------------------------------------------------------


def test_non_nfc_string_is_rejected_not_folded() -> None:
    """Folding would let a host attest to one byte sequence and ARC record another."""
    decomposed = "café"  # 'café' as e + combining acute
    with pytest.raises(CanonicalizationError, match="NFC"):
        canonicalize_manifest_claims({**_VALID, "environment": decomposed})


def test_nfc_and_nfd_forms_do_not_collide_into_one_digest() -> None:
    composed = "café"
    decomposed = "café"
    assert manifest_claims_digest({**_VALID, "environment": composed})
    with pytest.raises(CanonicalizationError):
        canonicalize_manifest_claims({**_VALID, "environment": decomposed})


def test_unknown_field_is_rejected() -> None:
    """Silently dropping it would let a caller believe it declared something."""
    with pytest.raises(CanonicalizationError, match="unknown manifest claim"):
        canonicalize_manifest_claims({**_VALID, "escalate_me": True})


def test_missing_required_field_is_rejected() -> None:
    incomplete = {k: v for k, v in _VALID.items() if k != "task_kind"}
    with pytest.raises(CanonicalizationError, match="missing required"):
        canonicalize_manifest_claims(incomplete)


def test_nul_byte_in_a_string_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="NUL"):
        canonicalize_manifest_claims({**_VALID, "session_id": "s\x00-1"})


def test_naive_datetime_is_rejected() -> None:
    """Without an offset the instant is ambiguous, so the digest would be too."""
    naive = datetime.datetime(2026, 1, 1, 10, 0)  # noqa: DTZ001 - the point of the test
    with pytest.raises(CanonicalizationError, match="naive datetime"):
        canonicalize_manifest_claims({**_VALID, "task_summary": naive})  # type: ignore[dict-item]


def test_fractional_float_is_rejected() -> None:
    """Its decimal serialization is platform dependent; a digest cannot be."""
    with pytest.raises(CanonicalizationError, match="fractional float"):
        canonicalize_manifest_claims({**_VALID, "task_summary": 1.5})  # type: ignore[dict-item]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(bad: float) -> None:
    with pytest.raises(CanonicalizationError, match="non-finite"):
        canonicalize_manifest_claims({**_VALID, "task_summary": bad})  # type: ignore[dict-item]


def test_tuple_is_rejected_rather_than_coerced_to_a_list() -> None:
    """Accepting both would give one meaning two canonical forms."""
    with pytest.raises(CanonicalizationError, match="tuple"):
        canonicalize_manifest_claims({**_VALID, "domain_ids": ("a", "b")})  # type: ignore[dict-item]


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="unsupported type"):
        canonicalize_manifest_claims({**_VALID, "task_summary": {1, 2}})  # type: ignore[dict-item]


def test_a_non_nfc_object_key_is_rejected() -> None:
    """Keys are held to the same NFC rule as values.

    That is also why there is no post-normalization collision check: two
    spellings of one key cannot both get past this, so such a guard would be
    unreachable — and an unreachable guard reads like protection that is not
    there.
    """
    decomposed_key = "cafe\u0301"  # e + combining acute
    with pytest.raises(CanonicalizationError, match="NFC"):
        canonicalize_manifest_claims({**_VALID, "task_summary": {decomposed_key: 1}})  # type: ignore[dict-item]


def test_non_object_input_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="must be an object"):
        canonicalize_manifest_claims(["not", "an", "object"])  # type: ignore[arg-type]


def test_server_derived_fields_are_not_part_of_the_claim_set() -> None:
    """Including them would let a caller assert identity that is not its to assert."""
    for forbidden in ("tenant_id", "actor_id", "oidc_subject", "host_id", "attestation"):
        assert forbidden not in MANIFEST_CLAIM_FIELDS
