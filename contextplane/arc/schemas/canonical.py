"""Canonicalization profiles.

Each profile here serves the same purpose: a byte string that is a function
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
- `arc_approval_evidence_v1` — over signed or verifier-attested approval
  evidence, everything except its own `signature`. An operator's detached
  signature and a trusted provider's attestation both have to agree with ARC
  on this same canonical object, or the thing being vouched for is not the
  thing that gets checked.

All of them **reject** rather than normalize. Accepting a non-NFC string and quietly
folding it, or accepting a duplicate key and keeping the last, would make
canonicalization total but the guarantee hollow: two different inputs would map
to one output, and the digest would no longer identify what the caller sent. The
negative vectors below are the substance of this module — a canonicalizer without
them is untested where it matters.

This module and its sibling `authoring_profiles.py` are twins for the
primitives every profile in either one depends on: NFC-only strings, no
embedded NUL, integral-only numbers, lexicographically sorted object keys,
and the exact same compact `ensure_ascii=False`, `(",", ":")`-separated
UTF-8 encoding. Each reaches those primitives through its own,
independently written code — this module's schema-less `_canonical` and
`_serialize`, and the sibling's schema-driven `_check_and_canonicalize` and
`_serialize` — because nothing about correctness requires a shared call
path, and a change to one that is not deliberately mirrored in the other
would mean two profile families disagree about what the same content
hashes to. `tests/conformance/test_canonicalization_agreement.py` is what
keeps them agreeing: it feeds a shared corpus through both engines and
asserts byte-identical output or a matching accept/refuse decision.

One asymmetry that test documents rather than resolves: this module has no
concept of a set-valued array at all. Every list here is canonicalized in
whatever order the caller supplied, with duplicates left exactly as given,
because none of this module's five profiles currently canonicalizes an
array field with set semantics. `authoring_profiles.py` does have that
concept (every array is labelled `set` or `ordered`, and a `set`-labelled
one is deduplicated and sorted by its own canonical bytes). Teaching this
module the same distinction would change its accepted byte output for any
existing caller whose array happens to contain a duplicate today, so this
stays a deliberate, tracked gap rather than something this file silently
claims to already do.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import unicodedata
from typing import Any

from contextplane.exceptions import RegistryError
from contextplane.types import JSONValue

MANIFEST_CLAIMS_PROFILE = "arc_manifest_claims_v1"
BUNDLE_CONTENT_PROFILE = "arc_context_bundle_content_v1"
HOST_ATTESTATION_ENVELOPE_PROFILE = "arc_host_attestation_v1_payload"
RECEIPT_EVENT_PROFILE = "arc_receipt_event_v1"
APPROVAL_EVIDENCE_PROFILE = "arc_approval_evidence_v1"

SUPPORTED_PROFILES = frozenset(
    {
        MANIFEST_CLAIMS_PROFILE,
        BUNDLE_CONTENT_PROFILE,
        HOST_ATTESTATION_ENVELOPE_PROFILE,
        RECEIPT_EVENT_PROFILE,
        APPROVAL_EVIDENCE_PROFILE,
        # The authoring-surface profiles: closed-schema validation and
        # canonical bytes for each of these sixteen literals live in
        # `authoring_profiles.py`, a sibling module in this package. Listed
        # as plain literals here rather than imported, so this foundational
        # module never depends on the higher-level one that owns them.
        "arc_source_approval_claim_v1",
        "arc_source_verifier_attestation_v1",
        "arc_source_approval_evidence_v1",
        # Three families carry two live versions: the `_v1` half verifies
        # bytes accepted under the Task field spelling, the `_v2` half is
        # what authoring emits. Both are supported because supported means
        # verifiable here, not writable.
        "arc_observation_class_predicate_v1",
        "arc_observation_class_predicate_v2",
        "arc_expected_impact_envelope_v1",
        "arc_expected_impact_envelope_v2",
        "arc_field_provenance_v1",
        "arc_artifact_semantics_v1",
        "arc_artifact_semantics_v2",
        "arc_approval_review_package_v1",
        "arc_artifact_revision_v1",
        "arc_actor_separation_v1",
        "arc_approval_verifier_enrollment_v1",
        "arc_approval_provider_assertion_v1",
        "arc_operational_event_v1",
        "arc_observation_cohort_v1",
        "arc_observation_qualification_v1",
        "arc_observation_replay_corpus_v1",
    }
)

# The profile set a receipt records as provenance, and the same set the
# published verification metadata advertises. One mapping rather than a
# literal list at each site: an external verifier that was told one set and
# handed a receipt claiming another has no way to tell which is wrong.
CANONICAL_PROFILE_VERSIONS: dict[str, str] = {
    "manifest_claims": MANIFEST_CLAIMS_PROFILE,
    "bundle_content": BUNDLE_CONTENT_PROFILE,
    "host_attestation": HOST_ATTESTATION_ENVELOPE_PROFILE,
    "receipt_event": RECEIPT_EVENT_PROFILE,
    "approval_evidence": APPROVAL_EVIDENCE_PROFILE,
}

# Caller-writable manifest fields, in the only order the profile permits. Server
# -derived identity and tenant fields are deliberately absent: including them
# would let a caller assert them, and they are not the caller's to assert.
MANIFEST_CLAIM_FIELDS: tuple[str, ...] = (
    "session_id",
    "intent_kind",
    "requested_action_classes",
    "capability_ids",
    "domain_ids",
    "environment",
    "data_sensitivity",
    "repository_identity",
    "supported_context_bundle_content_profiles",
    "intent_summary",
)

# `intent_summary` is optional search text and is excluded from mandatory
# selection, but it *is* part of what the host attested to, so it is canonicalized.
_OPTIONAL_FIELDS = frozenset({"intent_summary"})

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
# The complete event identity, chained through `previous_event_digest`.
# `signature` is absent by design -- see `canonicalize_receipt_event`.
_RECEIPT_EVENT_FIELDS: tuple[str, ...] = (
    "event_id",
    "receipt_id",
    "tenant_id",
    "sequence",
    "event_type",
    "event_source",
    "request_payload_digest",
    "previous_event_digest",
    "event_payload",
    "signer_key_id",
    "created_at",
)

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

# Every `arc_approval_evidence` column except `evidence_id` and `created_at`
# (assigned only once the row exists, so neither can be part of what a
# not-yet-persisted approval was signed over) and `signature` itself (the
# signature is computed over this, so including it would be circular).
# Fields only one `evidence_type` or `verification_method` makes meaningful
# are still required keys here, held to `null` when they do not apply: a
# closed field set only holds if presence is unconditional, or a caller could
# change which keys exist without changing the digest's shape.
_APPROVAL_EVIDENCE_FIELDS: tuple[str, ...] = (
    "evidence_type",
    "scope_kind",
    "scope_tenant_id",
    "approved_artifact_id",
    "approved_revision_id",
    "approved_exception_id",
    "approved_payload_digest",
    "approving_principal",
    "approving_role",
    "source_system_approval_locator",
    "approval_timestamp",
    "expires_at",
    "policy_version",
    "action_instance_id",
    "verification_method",
    "signer_key_id",
    "approval_verifier_id",
    "verifier_attestation",
    "verifier_identity",
    "audit_log_reference",
)


class CanonicalizationError(RegistryError):
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


def _canonical(value: JSONValue, path: str = "") -> JSONValue:
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


def _serialize(canonical: JSONValue) -> bytes:
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


def canonicalize_bundle_content(content: JSONValue, *, profile: str = BUNDLE_CONTENT_PROFILE) -> bytes:
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


def bundle_content_bytes(content: JSONValue) -> int:
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


def canonicalize_approval_evidence(evidence: dict[str, Any]) -> bytes:
    """Canonical bytes for `arc_approval_evidence_v1`.

    This is the exact byte string an operator signs, or that a verifier
    attestation is trusted to cover, prefixed by the caller with its own
    domain-separation tag before signing or verifying. Like the host
    attestation envelope, there is no outer `{"profile": ..., ...: body}`
    wrapper -- the signed structure is fixed to exactly the columns in
    `_APPROVAL_EVIDENCE_FIELDS`, and passing `signature` here is rejected as
    an unknown field rather than silently ignored, so a caller cannot believe
    the signature was itself covered when it was not.
    """
    if not isinstance(evidence, dict):
        raise CanonicalizationError("approval evidence must be an object")

    missing = [f for f in _APPROVAL_EVIDENCE_FIELDS if f not in evidence]
    if missing:
        raise CanonicalizationError(f"approval evidence missing field(s): {', '.join(missing)}")
    unknown = sorted(set(evidence) - set(_APPROVAL_EVIDENCE_FIELDS))
    if unknown:
        raise CanonicalizationError(f"unknown approval evidence field(s): {', '.join(unknown)}")

    body = {k: _canonical(evidence[k], k) for k in _APPROVAL_EVIDENCE_FIELDS}
    return _serialize(body)


def canonicalize_receipt_event(event: dict[str, Any]) -> bytes:
    """Canonical bytes for `arc_receipt_event_v1`, the event-digest input.

    `signature` is deliberately absent from the field set: the signature is
    over the digest, so including it would be circular. Passing one is
    rejected rather than ignored -- silently dropping it would let a caller
    believe the signature was covered when it was not.

    `previous_event_digest` is part of the digest, which is what makes the
    chain a chain: altering any earlier event changes every later digest,
    so tampering cannot be localized to the event that was changed.
    """
    if not isinstance(event, dict):
        raise CanonicalizationError("receipt event must be an object")

    missing = [f for f in _RECEIPT_EVENT_FIELDS if f not in event]
    if missing:
        raise CanonicalizationError(f"receipt event missing field(s): {', '.join(missing)}")
    unknown = sorted(set(event) - set(_RECEIPT_EVENT_FIELDS))
    if unknown:
        raise CanonicalizationError(f"unknown receipt event field(s): {', '.join(unknown)}")

    body = {k: _canonical(event[k], k) for k in _RECEIPT_EVENT_FIELDS}
    return _serialize({"profile": RECEIPT_EVENT_PROFILE, "event": body})


def receipt_event_digest(event: dict[str, Any]) -> str:
    """The digest a receipt event is signed over and chained by."""
    return hashlib.sha256(canonicalize_receipt_event(event)).hexdigest()
