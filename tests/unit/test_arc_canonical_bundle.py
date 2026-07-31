"""Positive and negative vectors for `arc_context_bundle_content_v1`.

This profile decides byte counts against the budget, so a disagreement about
canonical form is a bundle that is `ready` on one path and
`blocked_budget_exceeded` on another.
"""

from __future__ import annotations

import pytest

from registry.arc.schemas.canonical import (
    BUNDLE_CONTENT_PROFILE,
    MANIFEST_CLAIMS_PROFILE,
    CanonicalizationError,
    bundle_content_bytes,
    canonicalize_bundle_content,
)

_CONTENT = {
    "status": "ready",
    "directives": [
        {"directive_id": "d1", "projection": "require code review before merge"},
        {"directive_id": "d2", "projection": "run the security scan"},
    ],
    "cap_facts": [{"capability_id": "c1", "owner": "payments"}],
}


# --- positive vectors ------------------------------------------------------


def test_canonical_form_is_stable() -> None:
    assert canonicalize_bundle_content(_CONTENT) == canonicalize_bundle_content(_CONTENT)


def test_the_profile_is_part_of_the_canonical_bytes() -> None:
    assert BUNDLE_CONTENT_PROFILE.encode() in canonicalize_bundle_content(_CONTENT)


def test_object_key_order_does_not_change_the_bytes() -> None:
    """Otherwise the same bundle could be over budget depending on dict order."""
    reordered = {k: _CONTENT[k] for k in reversed(list(_CONTENT))}
    assert canonicalize_bundle_content(reordered) == canonicalize_bundle_content(_CONTENT)


def test_list_order_does_change_the_bytes() -> None:
    """Directive order is meaning — the bundle is an ordered set of obligations."""
    swapped = {**_CONTENT, "directives": list(reversed(_CONTENT["directives"]))}  # type: ignore[arg-type]
    assert canonicalize_bundle_content(swapped) != canonicalize_bundle_content(_CONTENT)


def test_byte_count_is_the_length_of_the_canonical_bytes() -> None:
    assert bundle_content_bytes(_CONTENT) == len(canonicalize_bundle_content(_CONTENT))


def test_byte_count_grows_with_content() -> None:
    bigger = {**_CONTENT, "extra": "x" * 500}
    assert bundle_content_bytes(bigger) > bundle_content_bytes(_CONTENT)


def test_count_is_over_bytes_not_characters() -> None:
    """A multi-byte character costs its real size in the budget.

    Counting characters would let a bundle of non-ASCII text exceed the byte
    budget while measuring as if it fit.
    """
    ascii_content = {"note": "aaaa"}
    wide_content = {"note": "éééé"}  # 4 chars, 8 UTF-8 bytes
    assert bundle_content_bytes(wide_content) > bundle_content_bytes(ascii_content)


def test_empty_content_is_canonicalizable() -> None:
    """A blocked bundle may carry no content and still needs a byte count."""
    assert bundle_content_bytes({}) > 0


# --- negative vectors ------------------------------------------------------


def test_an_unsupported_profile_is_rejected_not_assumed_current() -> None:
    """A caller naming a profile ARC does not implement must be told so.

    Silently counting under current rules would return a number the caller cannot
    interpret.
    """
    with pytest.raises(CanonicalizationError, match="unsupported bundle content profile"):
        canonicalize_bundle_content(_CONTENT, profile="arc_context_bundle_content_v2")


def test_the_manifest_profile_is_not_accepted_here() -> None:
    """The two profiles are not interchangeable even though both are canonical."""
    with pytest.raises(CanonicalizationError, match="unsupported"):
        canonicalize_bundle_content(_CONTENT, profile=MANIFEST_CLAIMS_PROFILE)


def test_non_nfc_content_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="NFC"):
        canonicalize_bundle_content({"note": "café"})


def test_fractional_float_in_content_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="fractional float"):
        canonicalize_bundle_content({"score": 0.5})


def test_non_finite_number_in_content_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="non-finite"):
        canonicalize_bundle_content({"score": float("inf")})


def test_nul_byte_in_content_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="NUL"):
        canonicalize_bundle_content({"note": "a\x00b"})


def test_unsupported_type_in_content_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="unsupported type"):
        canonicalize_bundle_content({"note": object()})


def test_rejection_names_the_path_so_the_offender_is_findable() -> None:
    """A bundle is nested; "not NFC" without a location is not actionable."""
    with pytest.raises(CanonicalizationError, match=r"directives\[1\]\.projection"):
        canonicalize_bundle_content({"directives": [{"projection": "ok"}, {"projection": "café"}]})
