"""Closed-schema validation and canonical bytes for the authoring-surface
profiles: the objects a source admission, a proposal's semantic surface, an
approval's review basis, and observation/qualification evidence are made of.

This module is pure: it imports no service, session, or ORM type, and it
never reads a database row or a file. `authoring_profile_shapes.py` is its
sibling data module -- the sixteen profile literals and the plain-dict shape
each one enforces, with no validation logic of its own. Two independent
capabilities live here per profile, deliberately kept separate rather than
fused into one "validate and decide" call:

- `canonicalize_<profile>(obj) -> bytes` rewrites a structurally valid
  instance into its exact canonical UTF-8 bytes: object keys sorted, a
  set-valued array deduplicated and sorted by its own canonical bytes, an
  ordered array checked against its declared sort key, every string NFC
  normalized and NUL-free, every number integral. It raises on anything that
  cannot be canonicalized this way -- normalizing here would let two
  distinct inputs collide on one digest, which is the one thing a canonical
  form must never do.
- `validate_<profile>(obj) -> None` calls the canonicalizer above and, for
  the handful of profiles whose acceptance rule a closed field set cannot
  express (a provenance record's conditional field groups, an impact
  envelope's non-overlapping items, an actor-separation record's distinct
  principals, a source evidence envelope's embedded claim digest), applies
  that rule afterward. An instance can canonicalize cleanly and still be
  invalid by this second, business-level check -- which is exactly why the
  two are separate functions rather than one.

Every raised error is a subclass of `AuthoringProfileError`, one class per
distinguishable outcome, each carrying the exact refusal code a caller
reports. Signature verification, cross-object digest chains, and any check
that needs another record's own accepted state are deliberately out of
scope here -- this module knows how one instance is shaped, not whether the
world around it agrees with it.

This module and `canonical.py` are twins for the primitives both rely on --
NFC-only strings, no embedded NUL, integral-only numbers, sorted object
keys, and identical compact UTF-8 serialization -- each reached through its
own independently written engine rather than one shared call path.
`tests/conformance/test_canonicalization_agreement.py` is the contract
that keeps the two agreeing: a change to either engine that is not
deliberately mirrored in the other fails that test rather than silently
drifting. This module is also the only one of the two with a set-valued-
array concept (`x-array-kind`): `canonical.py`'s five profiles never label
an array at all, so a duplicate entry in one of its arrays is a defect only
this module's schemas currently know how to express -- a documented
asymmetry between the two engines, not a shared rule either already
enforces uniformly.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable
from typing import Any, ClassVar

from registry.arc.schemas.authoring_profile_shapes import (
    _ACTOR_SEPARATION_SCHEMA,
    _APPROVAL_PROVIDER_ASSERTION_SCHEMA,
    _APPROVAL_REVIEW_PACKAGE_SCHEMA,
    _APPROVAL_VERIFIER_ENROLLMENT_SCHEMA,
    _ARTIFACT_REVISION_SCHEMA,
    _ARTIFACT_SEMANTICS_SCHEMA,
    _EXPECTED_IMPACT_ENVELOPE_SCHEMA,
    _FIELD_PROVENANCE_SCHEMA,
    _OBSERVATION_CLASS_PREDICATE_SCHEMA,
    _OBSERVATION_COHORT_SCHEMA,
    _OBSERVATION_QUALIFICATION_SCHEMA,
    _OBSERVATION_REPLAY_CORPUS_SCHEMA,
    _OPERATIONAL_EVENT_SCHEMA,
    _SOURCE_APPROVAL_CLAIM_SCHEMA,
    _SOURCE_APPROVAL_EVIDENCE_SCHEMA,
    _SOURCE_VERIFIER_ATTESTATION_SCHEMA,
    ACTOR_SEPARATION_PROFILE,
    APPROVAL_PROVIDER_ASSERTION_PROFILE,
    APPROVAL_REVIEW_PACKAGE_PROFILE,
    APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
    ARTIFACT_REVISION_PROFILE,
    ARTIFACT_SEMANTICS_PROFILE,
    EXPECTED_IMPACT_ENVELOPE_PROFILE,
    FIELD_PROVENANCE_PROFILE,
    OBSERVATION_CLASS_PREDICATE_PROFILE,
    OBSERVATION_COHORT_PROFILE,
    OBSERVATION_QUALIFICATION_PROFILE,
    OBSERVATION_REPLAY_CORPUS_PROFILE,
    OPERATIONAL_EVENT_PROFILE,
    SCHEMA_BY_PROFILE,
    SOURCE_APPROVAL_CLAIM_PROFILE,
    SOURCE_APPROVAL_EVIDENCE_PROFILE,
    SOURCE_VERIFIER_ATTESTATION_PROFILE,
    Schema,
)
from registry.arc.schemas.canonical import CanonicalizationError
from registry.types import JSONValue

# ---------------------------------------------------------------------------
# Errors. One class per distinguishable outcome -- the type itself is the
# refusal code a caller reports, not a shared class plus an attribute a
# caller has to remember to read.
# ---------------------------------------------------------------------------


class AuthoringProfileError(CanonicalizationError):
    """Base of every authoring-profile acceptance failure."""

    refusal_code: ClassVar[str] = "arc_proposal_validation_failed"


class ProfileValidationFailed(AuthoringProfileError):
    """The instance does not have the shape its profile's closed schema
    requires: an unknown or missing field, a value of the wrong type or
    outside its enum/pattern, non-NFC text, an embedded NUL, a fractional
    number, a duplicate or out-of-order array element, or a `profile`
    literal that does not match what was submitted (profile confusion)."""

    refusal_code: ClassVar[str] = "arc_proposal_validation_failed"


class EnvelopeItemsOverlapError(AuthoringProfileError):
    """Two impact-envelope items share a delta code and an overlapping
    class predicate. A closed field-set schema cannot express "no two items
    overlap"; this is that rule applied after the shape check passes."""

    refusal_code: ClassVar[str] = "arc_envelope_invalid"


class FieldProvenanceConditionalError(AuthoringProfileError):
    """A field-provenance record's `provenance_class` does not match the
    required/forbidden field group its class fixes: source-backed records
    must carry an anchor and excerpt digest and no author fields, human
    judgment records must carry an author and no derivation profile, and
    server-derived records must carry a derivation profile and nothing
    else."""

    refusal_code: ClassVar[str] = "arc_provenance_invalid"


class ActorSeparationViolationError(AuthoringProfileError):
    """An actor-separation record names the same principal for two roles a
    mandatory classification requires to be distinct, or does not name
    enough distinct principals for the classification it declares."""

    refusal_code: ClassVar[str] = "arc_activation_predicate_failed"


class SourceAdmissionRefusedError(AuthoringProfileError):
    """A source-approval-evidence envelope's `claim_digest` does not equal
    the recomputed digest of its own embedded claim. The envelope embeds the
    claim precisely so this is checkable without any other record."""

    refusal_code: ClassVar[str] = "arc_source_admission_refused"


# ---------------------------------------------------------------------------
# The generic engine: one recursive pass that is both the structural check
# and the canonicalizer. A value that reaches the end of this function
# without raising is exactly its own canonical form.
# ---------------------------------------------------------------------------


def _serialize(value: JSONValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _check_and_canonicalize(schema: Schema, value: JSONValue, path: str = "$") -> JSONValue:
    types = schema.get("type")
    allowed = types if isinstance(types, list) else [types]

    if value is None:
        if "null" not in allowed:
            raise ProfileValidationFailed(f"{path}: null is not permitted here")
        return None

    if isinstance(value, bool):
        if "boolean" not in allowed:
            raise ProfileValidationFailed(f"{path}: expected {allowed}, got a boolean")
        return value

    if isinstance(value, str):
        if "string" not in allowed:
            raise ProfileValidationFailed(f"{path}: expected {allowed}, got a string")
        const = schema.get("const")
        if const is not None and value != const:
            raise ProfileValidationFailed(f"{path}: expected the fixed value {const!r}")
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            raise ProfileValidationFailed(f"{path}: {value!r} is not one of {enum}")
        pattern = schema.get("pattern")
        if pattern is not None and pattern.fullmatch(value) is None:
            raise ProfileValidationFailed(f"{path}: does not match the required format")
        if unicodedata.normalize("NFC", value) != value:
            raise ProfileValidationFailed(f"{path}: string is not Unicode NFC normalized")
        if "\x00" in value:
            raise ProfileValidationFailed(f"{path}: string contains an embedded NUL character")
        return value

    if isinstance(value, int | float):
        if "number" not in allowed:
            raise ProfileValidationFailed(f"{path}: expected {allowed}, got a number")
        if isinstance(value, float) and not value.is_integer():
            raise ProfileValidationFailed(f"{path}: fractional number has no canonical form")
        return int(value)

    if isinstance(value, list):
        if "array" not in allowed:
            raise ProfileValidationFailed(f"{path}: expected {allowed}, got an array")
        return _canonicalize_array(schema, value, path)

    if isinstance(value, dict):
        if "object" not in allowed:
            raise ProfileValidationFailed(f"{path}: expected {allowed}, got an object")
        return _canonicalize_object(schema, value, path)

    raise ProfileValidationFailed(f"{path}: unsupported value type {type(value).__name__}")


def _canonicalize_array(schema: Schema, value: list[JSONValue], path: str) -> JSONValue:
    min_items = schema.get("minItems")
    if min_items is not None and len(value) < min_items:
        raise ProfileValidationFailed(f"{path}: expected at least {min_items} item(s)")
    items_schema: Schema = schema["items"]
    canon_items = [_check_and_canonicalize(items_schema, item, f"{path}[{i}]") for i, item in enumerate(value)]

    kind = schema.get("x-array-kind")
    if kind == "set":
        keyed = sorted(((_serialize(item), item) for item in canon_items), key=lambda pair: pair[0])
        seen: set[bytes] = set()
        ordered: list[JSONValue] = []
        for serialized, item in keyed:
            if serialized in seen:
                raise ProfileValidationFailed(f"{path}: duplicate entry in a set-valued array")
            seen.add(serialized)
            ordered.append(item)
        return ordered
    if kind == "ordered":
        order_key = schema.get("x-order-key")
        if order_key is not None:
            previous: Any = None
            for item in canon_items:
                current = item[order_key] if isinstance(item, dict) else item
                if previous is not None and not previous < current:
                    raise ProfileValidationFailed(f"{path}: ordered array is not strictly ascending by {order_key!r}")
                previous = current
        return canon_items
    raise ProfileValidationFailed(f"{path}: array has no set/ordered label")


def _canonicalize_object(schema: Schema, value: dict[str, JSONValue], path: str) -> JSONValue:
    properties: dict[str, Schema] = schema["properties"]
    required: tuple[str, ...] = schema["required"]
    keys = list(value)
    if not all(isinstance(k, str) for k in keys):
        raise ProfileValidationFailed(f"{path}: non-string object key")
    if len(set(keys)) != len(keys):
        raise ProfileValidationFailed(f"{path}: duplicate object key")
    missing = [k for k in required if k not in value]
    if missing:
        raise ProfileValidationFailed(f"{path}: missing required field(s) {missing}")
    unknown = sorted(set(keys) - set(properties))
    if unknown:
        raise ProfileValidationFailed(f"{path}: unknown field(s) {unknown}")
    for key in keys:
        if unicodedata.normalize("NFC", key) != key:
            raise ProfileValidationFailed(f"{path}.{key}: object key is not Unicode NFC normalized")
    return {key: _check_and_canonicalize(properties[key], value[key], f"{path}.{key}") for key in sorted(keys)}


def profile_field_names(profile_literal: str) -> frozenset[str]:
    """The exact closed top-level field set this module enforces for one
    profile literal. Used to check that the schema this module encodes has
    not drifted from the schema a fixture or a snapshot separately declares,
    and to check that no profile's field set names its own digest or names a
    field belonging to a node later in a digest chain."""
    return frozenset(SCHEMA_BY_PROFILE[profile_literal]["properties"])


# ---------------------------------------------------------------------------
# Same-object business rules a closed field-set schema cannot express.
# ---------------------------------------------------------------------------

_PROVENANCE_GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    "source_backed": {
        "required": ("source_anchor", "quoted_excerpt_digest"),
        "forbidden": ("author_issuer", "author_subject", "author_role", "derivation_profile"),
    },
    "human_judgment": {
        "required": ("author_issuer", "author_subject", "author_role"),
        "forbidden": ("derivation_profile",),
    },
    "server_derived": {
        "required": ("derivation_profile",),
        "forbidden": ("source_anchor", "quoted_excerpt_digest", "author_issuer", "author_subject", "author_role"),
    },
}


def _check_field_provenance_conditional(obj: dict[str, Any]) -> None:
    spec = _PROVENANCE_GROUPS[obj["provenance_class"]]
    for name in spec["required"]:
        if obj.get(name) is None:
            raise FieldProvenanceConditionalError(f"{obj['provenance_class']} requires {name!r} to be non-null")
    for name in spec["forbidden"]:
        if obj.get(name) is not None:
            raise FieldProvenanceConditionalError(f"{obj['provenance_class']} forbids {name!r} from being non-null")


def _check_envelope_non_overlap(obj: dict[str, Any]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for item in obj["items"]:
        key = (item["delta_code"], json.dumps(item["class_predicate"], sort_keys=True, ensure_ascii=False))
        if key in seen:
            raise EnvelopeItemsOverlapError(
                f"items {seen[key]!r} and {item['item_id']!r} share delta code {item['delta_code']!r} "
                "with an overlapping class predicate"
            )
        seen[key] = item["item_id"]


def _check_actor_separation(obj: dict[str, Any]) -> None:
    submitter = (obj["submitter_issuer"], obj["submitter_subject"])
    approver = (obj["approver_issuer"], obj["approver_subject"])
    activator = (obj["activator_issuer"], obj["activator_subject"])
    if submitter == approver:
        raise ActorSeparationViolationError("submitter and approver must be distinct principals")
    if obj["risk_classification"] == "global_mandatory" and len({submitter, approver, activator}) != 3:
        raise ActorSeparationViolationError("a global mandatory classification requires three distinct principals")


def _check_evidence_claim_digest(obj: dict[str, Any]) -> None:
    real_digest = hashlib.sha256(canonicalize_source_approval_claim_v1(obj["claim"])).hexdigest()
    if obj["claim_digest"] != real_digest:
        raise SourceAdmissionRefusedError("claim_digest does not match the recomputed digest of the embedded claim")


# ---------------------------------------------------------------------------
# Public per-profile functions.
# ---------------------------------------------------------------------------


def canonicalize_source_approval_claim_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_SOURCE_APPROVAL_CLAIM_SCHEMA, obj))


def validate_source_approval_claim_v1(obj: dict[str, Any]) -> None:
    canonicalize_source_approval_claim_v1(obj)


def canonicalize_source_verifier_attestation_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_SOURCE_VERIFIER_ATTESTATION_SCHEMA, obj))


def validate_source_verifier_attestation_v1(obj: dict[str, Any]) -> None:
    canonicalize_source_verifier_attestation_v1(obj)


def canonicalize_source_approval_evidence_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_SOURCE_APPROVAL_EVIDENCE_SCHEMA, obj))


def validate_source_approval_evidence_v1(obj: dict[str, Any]) -> None:
    canonicalize_source_approval_evidence_v1(obj)
    _check_evidence_claim_digest(obj)


def canonicalize_observation_class_predicate_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_OBSERVATION_CLASS_PREDICATE_SCHEMA, obj))


def validate_observation_class_predicate_v1(obj: dict[str, Any]) -> None:
    canonicalize_observation_class_predicate_v1(obj)


def canonicalize_expected_impact_envelope_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_EXPECTED_IMPACT_ENVELOPE_SCHEMA, obj))


def validate_expected_impact_envelope_v1(obj: dict[str, Any]) -> None:
    canonicalize_expected_impact_envelope_v1(obj)
    _check_envelope_non_overlap(obj)


def canonicalize_field_provenance_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_FIELD_PROVENANCE_SCHEMA, obj))


def validate_field_provenance_v1(obj: dict[str, Any]) -> None:
    canonicalize_field_provenance_v1(obj)
    _check_field_provenance_conditional(obj)


def canonicalize_artifact_semantics_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_ARTIFACT_SEMANTICS_SCHEMA, obj))


def validate_artifact_semantics_v1(obj: dict[str, Any]) -> None:
    canonicalize_artifact_semantics_v1(obj)


def canonicalize_approval_review_package_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_APPROVAL_REVIEW_PACKAGE_SCHEMA, obj))


def validate_approval_review_package_v1(obj: dict[str, Any]) -> None:
    canonicalize_approval_review_package_v1(obj)


def canonicalize_artifact_revision_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_ARTIFACT_REVISION_SCHEMA, obj))


def validate_artifact_revision_v1(obj: dict[str, Any]) -> None:
    canonicalize_artifact_revision_v1(obj)


def canonicalize_actor_separation_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_ACTOR_SEPARATION_SCHEMA, obj))


def validate_actor_separation_v1(obj: dict[str, Any]) -> None:
    canonicalize_actor_separation_v1(obj)
    _check_actor_separation(obj)


def canonicalize_approval_verifier_enrollment_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_APPROVAL_VERIFIER_ENROLLMENT_SCHEMA, obj))


def validate_approval_verifier_enrollment_v1(obj: dict[str, Any]) -> None:
    canonicalize_approval_verifier_enrollment_v1(obj)


def canonicalize_approval_provider_assertion_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_APPROVAL_PROVIDER_ASSERTION_SCHEMA, obj))


def validate_approval_provider_assertion_v1(obj: dict[str, Any]) -> None:
    canonicalize_approval_provider_assertion_v1(obj)


def canonicalize_operational_event_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_OPERATIONAL_EVENT_SCHEMA, obj))


def validate_operational_event_v1(obj: dict[str, Any]) -> None:
    canonicalize_operational_event_v1(obj)


def canonicalize_observation_cohort_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_OBSERVATION_COHORT_SCHEMA, obj))


def validate_observation_cohort_v1(obj: dict[str, Any]) -> None:
    canonicalize_observation_cohort_v1(obj)


def canonicalize_observation_qualification_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_OBSERVATION_QUALIFICATION_SCHEMA, obj))


def validate_observation_qualification_v1(obj: dict[str, Any]) -> None:
    canonicalize_observation_qualification_v1(obj)


def canonicalize_observation_replay_corpus_v1(obj: dict[str, Any]) -> bytes:
    return _serialize(_check_and_canonicalize(_OBSERVATION_REPLAY_CORPUS_SCHEMA, obj))


def validate_observation_replay_corpus_v1(obj: dict[str, Any]) -> None:
    canonicalize_observation_replay_corpus_v1(obj)


PROFILE_FUNCTIONS: dict[str, tuple[Callable[[Any], None], Callable[[Any], bytes]]] = {
    SOURCE_APPROVAL_CLAIM_PROFILE: (validate_source_approval_claim_v1, canonicalize_source_approval_claim_v1),
    SOURCE_VERIFIER_ATTESTATION_PROFILE: (
        validate_source_verifier_attestation_v1,
        canonicalize_source_verifier_attestation_v1,
    ),
    SOURCE_APPROVAL_EVIDENCE_PROFILE: (
        validate_source_approval_evidence_v1,
        canonicalize_source_approval_evidence_v1,
    ),
    OBSERVATION_CLASS_PREDICATE_PROFILE: (
        validate_observation_class_predicate_v1,
        canonicalize_observation_class_predicate_v1,
    ),
    EXPECTED_IMPACT_ENVELOPE_PROFILE: (
        validate_expected_impact_envelope_v1,
        canonicalize_expected_impact_envelope_v1,
    ),
    FIELD_PROVENANCE_PROFILE: (validate_field_provenance_v1, canonicalize_field_provenance_v1),
    ARTIFACT_SEMANTICS_PROFILE: (validate_artifact_semantics_v1, canonicalize_artifact_semantics_v1),
    APPROVAL_REVIEW_PACKAGE_PROFILE: (
        validate_approval_review_package_v1,
        canonicalize_approval_review_package_v1,
    ),
    ARTIFACT_REVISION_PROFILE: (validate_artifact_revision_v1, canonicalize_artifact_revision_v1),
    ACTOR_SEPARATION_PROFILE: (validate_actor_separation_v1, canonicalize_actor_separation_v1),
    APPROVAL_VERIFIER_ENROLLMENT_PROFILE: (
        validate_approval_verifier_enrollment_v1,
        canonicalize_approval_verifier_enrollment_v1,
    ),
    APPROVAL_PROVIDER_ASSERTION_PROFILE: (
        validate_approval_provider_assertion_v1,
        canonicalize_approval_provider_assertion_v1,
    ),
    OPERATIONAL_EVENT_PROFILE: (validate_operational_event_v1, canonicalize_operational_event_v1),
    OBSERVATION_COHORT_PROFILE: (validate_observation_cohort_v1, canonicalize_observation_cohort_v1),
    OBSERVATION_QUALIFICATION_PROFILE: (
        validate_observation_qualification_v1,
        canonicalize_observation_qualification_v1,
    ),
    OBSERVATION_REPLAY_CORPUS_PROFILE: (
        validate_observation_replay_corpus_v1,
        canonicalize_observation_replay_corpus_v1,
    ),
}
