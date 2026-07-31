"""Canonicalization profiles.

Three profiles, all serving the same purpose: a byte string that is a function
of meaning alone, so two parties computing it over the same content agree
exactly.

- `arc_manifest_claims_v1` — over the caller-writable task-manifest fields. The
  host signs its digest, and ARC binds the challenge to that digest, so a
  disagreement about canonical form is a disagreement about what was attested.
- `arc_context_bundle_content_v1` — over rendered bundle content, for byte
  counting against the budget. A disagreement here means a bundle that is
  `ready` on one path and `blocked_budget_exceeded` on another.
- `arc_host_attestation_v1_payload` — over the host attestation envelope. This
  is the exact byte string a host signs and ARC re-derives to verify that
  signature, so a disagreement about canonical form here is a signature that
  never verifies.

All three **reject** rather than normalize. Accepting a non-NFC string and quietly
folding it, or accepting a duplicate key and keeping the last, would make
canonicalization total but the guarantee hollow: two different inputs would map
to one output, and the digest would no longer identify what the caller sent. The
negative vectors below are the substance of this module — a canonicalizer without
them is untested where it matters.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import unicodedata
from typing import Any

MANIFEST_CLAIMS_PROFILE = "arc_manifest_claims_v1"
BUNDLE_CONTENT_PROFILE = "arc_context_bundle_content_v1"
HOST_ATTESTATION_ENVELOPE_PROFILE = "arc_host_attestation_v1_payload"

SUPPORTED_PROFILES = frozenset({MANIFEST_CLAIMS_PROFILE, BUNDLE_CONTENT_PROFILE, HOST_ATTESTATION_ENVELOPE_PROFILE})

# Caller-writable manifest fields, in the only order the profile permits. Server
# -derived identity and tenant fields are deliberately absent: including them
# would let a caller assert them, and they are not the caller's to assert.
MANIFEST_CLAIM_FIELDS: tuple[str, ...] = (
    "session_id",
    "task_kind",
    "requested_action_classes",
    "capability_ids",
    "domain_ids",
    "environment",
    "data_sensitivity",
    "repository_identity",
    "supported_context_bundle_content_profiles",
    "task_summary",
)

# `task_summary` is optional search text and is excluded from mandatory
# selection, but it *is* part of what the host attested to, so it is canonicalized.
_OPTIONAL_FIELDS = frozenset({"task_summary"})

# The exact six fields a host signs, in the order that also fixes their
# canonical serialization. Unlike the manifest-claims profile, this is not a
# mandatory request schema evolving over time -- it is the fixed shape of one
# signed envelope -- so it is a plain closed set rather than a
# required/optional split.
_ATTESTATION_ENVELOPE_FIELDS: tuple[str, ...] = (
    "profile",
    "signer_key_id",
    "attestation_id",
    "issued_at",
    "expires_at",
    "payload",
)

# The attestation payload is itself a closed object. A stray or missing field
# here would silently change what was actually signed, which is exactly what
# canonicalization exists to catch.
_ATTESTATION_PAYLOAD_FIELDS: tuple[str, ...] = (
    "host_id",
    "repository_identity",
    "immutable_source_revision",
    "environment",
    "data_sensitivity",
    "session_id",
    "manifest_claims_digest",
    "arc_nonce",
)


class CanonicalizationError(ValueError):
    """Input cannot be canonicalized. Always a rejection, never a repair."""


def _reject(reason: str, path: str) -> None:
    raise CanonicalizationError(f"{reason} at {path or '<root>'}")


def _canonical_string(value: str, path: str) -> str:
    """Require NFC. Normalizing here would map two inputs to one output.

    If ARC folded a non-NFC string, a host could attest to one byte sequence and
    ARC could record a different one, both claiming the same digest.
    """
    if unicodedata.normalize("NFC", value) != value:
        _reject("string is not Unicode NFC normalized", path)
    if "\x00" in value:
        _reject("string contains a NUL character", path)
    return value


def _canonical_number(value: int | float, path: str) -> int | float:
    """Reject anything without an exact decimal serialization.

    A float that serializes differently on two platforms, or at all specially
    (NaN, Infinity), cannot be part of a digest two parties must agree on.
    """
    if isinstance(value, bool):  # bool is an int subclass; handled separately
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            _reject("non-finite number", path)
        if not value.is_integer():
            _reject(
                "fractional float; use a string or an integer so the digest is " "platform independent",
                path,
            )
        return int(value)
    return value


def _canonical(value: Any, path: str = "") -> Any:
    """Recursively validate and order a JSON-like value."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _canonical_string(value, path)
    if isinstance(value, int | float):
        return _canonical_number(value, path)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            _reject("naive datetime; canonical form requires an explicit offset", path)
        return value.astimezone(datetime.UTC).isoformat()
    if isinstance(value, list):
        return [_canonical(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, tuple):
        _reject("tuple is not a JSON type; use a list", path)
    if isinstance(value, dict):
        keys = list(value)
        if not all(isinstance(k, str) for k in keys):
            _reject("non-string object key", path)
        if len(set(keys)) != len(keys):
            _reject("duplicate object key", path)
        # Keys are held to the same NFC requirement as values, which is also
        # what makes a post-normalization collision check unnecessary: two
        # spellings of one key cannot both get this far, because the non-NFC one
        # is rejected here. A collision guard after this point would be
        # unreachable, and an unreachable guard reads like protection that is not
        # there.
        for key in keys:
            _canonical_string(key, f"{path}.{key}")
        # Sorted so object ordering cannot change the digest.
        return {k: _canonical(value[k], f"{path}.{k}") for k in sorted(keys)}
    _reject(f"unsupported type {type(value).__name__}", path)
    raise AssertionError("unreachable")  # pragma: no cover


def _serialize(canonical: Any) -> bytes:
    """UTF-8 JSON with no incidental whitespace and no escaping of non-ASCII.

    `ensure_ascii=False` keeps the bytes a function of the NFC text rather than of
    Python's escaping choices.
    """
    return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonicalize_manifest_claims(claims: dict[str, Any]) -> bytes:
    """Canonical bytes for `arc_manifest_claims_v1`.

    Unknown fields are rejected. A manifest carrying a field ARC does not know is
    a manifest ARC cannot fully attest to, and silently dropping it would let a
    caller believe it had declared something ARC never saw.
    """
    if not isinstance(claims, dict):
        raise CanonicalizationError("manifest claims must be an object")

    unknown = sorted(set(claims) - set(MANIFEST_CLAIM_FIELDS))
    if unknown:
        raise CanonicalizationError(
            f"unknown manifest claim field(s): {', '.join(unknown)}; "
            f"{MANIFEST_CLAIMS_PROFILE} is a closed field set"
        )
    missing = [f for f in MANIFEST_CLAIM_FIELDS if f not in claims and f not in _OPTIONAL_FIELDS]
    if missing:
        raise CanonicalizationError(f"missing required manifest claim field(s): {', '.join(missing)}")

    body = {k: _canonical(claims[k], k) for k in MANIFEST_CLAIM_FIELDS if k in claims}
    return _serialize({"profile": MANIFEST_CLAIMS_PROFILE, "claims": body})


def manifest_claims_digest(claims: dict[str, Any]) -> str:
    """The digest a host signs and a challenge is bound to."""
    return hashlib.sha256(canonicalize_manifest_claims(claims)).hexdigest()


def canonicalize_bundle_content(content: Any, *, profile: str = BUNDLE_CONTENT_PROFILE) -> bytes:
    """Canonical bytes for `arc_context_bundle_content_v1`.

    An unsupported profile version is rejected rather than assumed current: a
    caller declaring a profile ARC does not implement must be told so, not handed
    bytes counted under different rules.
    """
    if profile != BUNDLE_CONTENT_PROFILE:
        raise CanonicalizationError(
            f"unsupported bundle content profile {profile!r}; this build implements " f"{BUNDLE_CONTENT_PROFILE}"
        )
    return _serialize({"profile": BUNDLE_CONTENT_PROFILE, "content": _canonical(content)})


def bundle_content_bytes(content: Any) -> int:
    """Rendered size the budget is enforced against.

    Counted over canonical bytes rather than over a rendered string, so the number
    cannot drift with presentation.
    """
    return len(canonicalize_bundle_content(content))


def canonicalize_host_attestation_envelope(envelope: dict[str, Any]) -> bytes:
    """Canonical bytes for `arc_host_attestation_v1_payload`.

    This is the exact byte string a host signs, prefixed by the caller with
    the `ARC-HOST-ATTESTATION-V1` domain-separation tag before signing or
    verifying. Unlike the other two profiles, there is no outer `{"profile":
    ..., ...: body}` wrapper: the signed structure is fixed to exactly
    `{profile, signer_key_id, attestation_id, issued_at, expires_at,
    payload}`, and `profile` here is one of those six fields (the
    attestation scheme's own name), not this canonicalization profile's.
    """
    if not isinstance(envelope, dict):
        raise CanonicalizationError("attestation envelope must be an object")

    missing = [f for f in _ATTESTATION_ENVELOPE_FIELDS if f not in envelope]
    if missing:
        raise CanonicalizationError(f"attestation envelope missing field(s): {', '.join(missing)}")
    unknown = sorted(set(envelope) - set(_ATTESTATION_ENVELOPE_FIELDS))
    if unknown:
        raise CanonicalizationError(f"unknown attestation envelope field(s): {', '.join(unknown)}")

    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise CanonicalizationError("attestation envelope payload must be an object")
    missing_payload = [f for f in _ATTESTATION_PAYLOAD_FIELDS if f not in payload]
    if missing_payload:
        raise CanonicalizationError(f"attestation payload missing field(s): {', '.join(missing_payload)}")
    unknown_payload = sorted(set(payload) - set(_ATTESTATION_PAYLOAD_FIELDS))
    if unknown_payload:
        raise CanonicalizationError(f"unknown attestation payload field(s): {', '.join(unknown_payload)}")

    body = {k: _canonical(envelope[k], k) for k in _ATTESTATION_ENVELOPE_FIELDS}
    return _serialize(body)
