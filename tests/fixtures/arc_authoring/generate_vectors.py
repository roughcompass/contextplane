"""Generator for the ARC authoring-surface canonical vector fixtures.

This script is reviewable data-generation tooling, not part of the shipped
application: nothing under `registry/registry/` imports it, and nothing at
request-serving time runs it. Re-run it by hand whenever a vector is added or
changed:

    .venv/bin/python tests/fixtures/arc_authoring/generate_vectors.py

and bump `MANIFEST_VERSION` below in the same change. The committed output —
`manifest.json`, `keys.json`, and the sixteen profile directories — is the
artifact that ships; this file is how that artifact was produced and how a
reviewer can reproduce it byte-for-byte (every identifier and key seed below
is a fixed literal, so two runs on two machines emit identical files).

Sixteen closed profiles are covered, one directory each. Each profile's
`schema.json` fixes its exact field set — required keys, nullability, enum
values, and which arrays are order-independent sets versus meaning-bearing
sequences. Every case pins three things that must always agree: the canonical
byte encoding of a profile instance, the SHA-256 digest of those bytes, and
(for the five profiles that are directly signed) an Ed25519 signature over a
domain-separated version of those bytes. Two families of negative case:

- *structural* — the input cannot be canonicalized at all (an unknown field,
  a missing field, non-NFC text, and so on). Its `canonical_bytes_base64`,
  `digest`, and signature fields are all null: nothing to publish, because
  nothing was ever produced.
- *semantic* — the input canonicalizes just fine (it is shaped correctly) but
  is wrong in a way only a cross-check catches: a tampered digest reference
  into another profile's real fixture, a signature produced under the wrong
  domain or the wrong key, or a business rule this profile's own schema
  cannot express as a type/shape constraint. Its canonical bytes and digest
  are present and correct; what is wrong is a signature or a cross-reference,
  which is exactly what a verifier has to catch instead of a shape check.

The reference verifier at `registry/tools/arc-reference-verifier/verify.mjs`
re-derives every one of these three published fields independently, in a
different language, from nothing but the raw case input and the published
public keys in `keys.json`. Agreement is the acceptance criterion; this
generator's own job is only to produce the first opinion.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

FIXTURE_ROOT = Path(__file__).resolve().parent
MANIFEST_VERSION = 1

# ---------------------------------------------------------------------------
# Shared value formats
# ---------------------------------------------------------------------------

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
# RFC 3339 UTC, `Z` suffix only. A numeric offset naming the identical
# instant is rejected rather than normalized -- see `NON_NFC_SUFFIX` below for
# why this module rejects instead of folding wherever two inputs could
# otherwise map to one digest.
TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"

_UUID_RE = re.compile(UUID_PATTERN)
_DIGEST_RE = re.compile(DIGEST_PATTERN)
_TIMESTAMP_RE = re.compile(TIMESTAMP_PATTERN)


def uid(n: int) -> str:
    """A fixed, readable, deterministic UUID literal -- never `uuid4()`.

    Fixtures are reviewed as text; an opaque random UUID tells a reviewer
    nothing, while `...-000000000005` at least sorts and diffs predictably
    across the fixture set.
    """
    return f"00000000-0000-4000-8000-{n:012d}"


def dig(label: str) -> str:
    """A deterministic 64-hex-character stand-in for a digest this generator
    does not independently compute (e.g. a hash of bytes belonging to a
    system outside this fixture set, like a source system's own content).
    Every digest field that *does* reference another profile's real fixture
    in this set is computed for real -- see the cross-references built in
    `build_profiles()` -- so this helper is only ever the placeholder for a
    reference this fixture set has no way to independently verify.
    """
    return hashlib.sha256(f"arc-fixture-placeholder-digest:{label}".encode()).hexdigest()


# A non-NFC string: "e" followed by a combining acute accent (U+0301). Its
# NFC form re-composes to "é" (a single code point), so this string never
# equals its own NFC normalization -- a reliable, readable way to build a
# non-NFC negative without relying on any specific pre-composed character.
NON_NFC_SUFFIX = "é"


def make_non_nfc(value: str) -> str:
    return value + NON_NFC_SUFFIX


def make_embedded_nul(value: str) -> str:
    return value + "\x00"


def flip_hex(value: str) -> str:
    """Change exactly one hex digit, keeping the value a syntactically valid
    64-character digest that is nonetheless wrong."""
    first = value[0]
    replacement = "1" if first == "0" else "0"
    return replacement + value[1:]


# ---------------------------------------------------------------------------
# A minimal JSON-Schema-shaped validator + canonicalizer.
#
# This is the *fixture-generation-time* implementation. The Node reference
# verifier under registry/tools/arc-reference-verifier/ reimplements this
# same rule set from scratch in JavaScript, sharing no code with this file --
# that is what makes agreement between the two worth anything.
# ---------------------------------------------------------------------------


class SchemaError(Exception):
    """The input does not have the shape `schema.json` requires."""


class CanonError(Exception):
    """The input has the right shape but cannot be canonicalized as-is."""


class ProfileRuleError(Exception):
    """The input canonicalizes fine but violates a same-profile business
    rule that a closed field-set schema cannot express as a type constraint
    (a conditional-requiredness group, a non-overlap constraint, an identity
    that must differ from another identity in the same object)."""


def _type_matches(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise ValueError(f"unknown schema type {expected!r}")


def validate(schema: dict[str, Any], value: Any, path: str = "$") -> None:
    """Structural validation only: types, enums, patterns, closed object
    shape, and array minimums. Does not enforce NFC, NUL-freedom, integral
    numbers, or array ordering/dedup -- that is `canonicalize`'s job, so a
    value can pass here and still fail there."""
    types = schema.get("type")
    if types is not None:
        candidates = types if isinstance(types, list) else [types]
        if not any(_type_matches(t, value) for t in candidates):
            raise SchemaError(f"{path}: expected type {candidates}, got {type(value).__name__}")
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: expected constant {schema['const']!r}, got {value!r}")
    if value is None:
        return
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} is not one of {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and re.fullmatch(schema["pattern"], value) is None:
        raise SchemaError(f"{path}: {value!r} does not match the required format")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise SchemaError(f"{path}: missing required field(s) {missing}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(props))
            if unknown:
                raise SchemaError(f"{path}: unknown field(s) {unknown}")
        for key, sub_value in value.items():
            if key in props:
                validate(props[key], sub_value, f"{path}.{key}")
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            raise SchemaError(f"{path}: expected at least {min_items} item(s), got {len(value)}")
        items_schema = schema.get("items")
        if items_schema is not None:
            for index, item in enumerate(value):
                validate(items_schema, item, f"{path}[{index}]")


def _canonical_json_bytes(value: Any) -> bytes:
    """Compact UTF-8 JSON, keys already sorted by the caller. No incidental
    whitespace, no ASCII-escaping of non-ASCII text -- the byte string is a
    function of the value alone."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonicalize(schema: dict[str, Any], value: Any, path: str = "$") -> Any:
    """Recursively rewrite `value` into its canonical form, rejecting -- never
    normalizing -- anything that would make two distinct inputs collide on
    one digest."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        if isinstance(value, float) and not value.is_integer():
            raise CanonError(f"{path}: fractional number has no canonical form")
        return int(value)
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise CanonError(f"{path}: string is not Unicode NFC normalized")
        if "\x00" in value:
            raise CanonError(f"{path}: string contains a NUL character")
        return value
    if isinstance(value, list):
        items_schema = schema.get("items", {})
        canon_items = [canonicalize(items_schema, item, f"{path}[{i}]") for i, item in enumerate(value)]
        kind = schema.get("x-array-kind")
        if kind == "set":
            keyed = [(_canonical_json_bytes(item), item) for item in canon_items]
            seen: set[bytes] = set()
            for serialized, _ in keyed:
                if serialized in seen:
                    raise CanonError(f"{path}: duplicate entry in a set-valued array")
                seen.add(serialized)
            keyed.sort(key=lambda pair: pair[0])
            return [item for _, item in keyed]
        if kind == "ordered":
            order_key = schema.get("x-order-key")
            if order_key is not None:
                previous: Any = None
                for item in canon_items:
                    current = item[order_key] if isinstance(item, dict) else item
                    if previous is not None and not previous < current:
                        raise CanonError(f"{path}: ordered array is not in strictly ascending {order_key!r} order")
                    previous = current
            return canon_items
        raise CanonError(f"{path}: array has no set/ordered label")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        keys = list(value)
        if len(set(keys)) != len(keys):
            raise CanonError(f"{path}: duplicate object key")
        for key in keys:
            if unicodedata.normalize("NFC", key) != key:
                raise CanonError(f"{path}.{key}: object key is not Unicode NFC normalized")
        return {key: canonicalize(props.get(key, {}), value[key], f"{path}.{key}") for key in sorted(keys)}
    raise CanonError(f"{path}: unsupported value type {type(value).__name__}")


def canonical_bytes(schema: dict[str, Any], obj: dict[str, Any]) -> bytes:
    validate(schema, obj)
    return _canonical_json_bytes(canonicalize(schema, obj))


def digest_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Small schema-building helpers
# ---------------------------------------------------------------------------


def STR() -> dict[str, Any]:
    return {"type": "string"}


def UUID() -> dict[str, Any]:
    return {"type": "string", "pattern": UUID_PATTERN}


def DIGEST() -> dict[str, Any]:
    return {"type": "string", "pattern": DIGEST_PATTERN}


def TS() -> dict[str, Any]:
    return {"type": "string", "pattern": TIMESTAMP_PATTERN}


def ENUM(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def NUM() -> dict[str, Any]:
    return {"type": "number"}


def BOOL() -> dict[str, Any]:
    return {"type": "boolean"}


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    out = dict(schema)
    base_type = out.get("type")
    out["type"] = [base_type, "null"] if not isinstance(base_type, list) else [*base_type, "null"]
    return out


def ARR(
    items: dict[str, Any], *, kind: str, order_key: str | None = None, min_items: int | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "array", "items": items, "x-array-kind": kind}
    if order_key is not None:
        out["x-order-key"] = order_key
    if min_items is not None:
        out["minItems"] = min_items
    return out


def OBJ(properties: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def PROFILE_SCHEMA(literal: str, fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A top-level profile object: `profile` is a fixed constant plus every
    named field, all required as keys (nullability is expressed on the
    field's own schema, not by omitting the key -- an explicit null and an
    absent key are different things everywhere in this fixture set)."""
    properties = {"profile": {"type": "string", "const": literal}, **fields}
    return OBJ(properties, ["profile", *fields.keys()])


# ---------------------------------------------------------------------------
# Deterministic Ed25519 keys, one primary + one "wrong" per signed profile.
# Seeded from a fixed label so regenerating this file reproduces identical
# keys and therefore identical signatures -- reviewable, not random.
# ---------------------------------------------------------------------------


def _seeded_private_key(label: str) -> Ed25519PrivateKey:
    seed = hashlib.sha256(f"arc-fixture-signing-key:{label}".encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def _public_bytes(private_key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


@dataclass(frozen=True)
class SignInfo:
    """How one profile is signed. `sign_over` names what the domain prefix
    is prepended to: the profile's own canonical bytes for every signed
    profile except the operational event, which signs the raw digest bytes
    instead (its own text says so explicitly -- the signature covers the
    digest, not the canonical object, so a verifier must know which)."""

    domain_prefix: bytes
    human_domain: str
    sign_over: str  # "canonical_bytes" | "digest"
    key_label: str

    @property
    def wrong_key_label(self) -> str:
        return f"{self.key_label}:wrong"

    def primary_private_key(self) -> Ed25519PrivateKey:
        return _seeded_private_key(self.key_label)

    def wrong_private_key(self) -> Ed25519PrivateKey:
        return _seeded_private_key(self.wrong_key_label)

    def signing_input(self, canonical: bytes, digest: str) -> bytes:
        payload = bytes.fromhex(digest) if self.sign_over == "digest" else canonical
        return self.domain_prefix + payload

    def sign(self, canonical: bytes, digest: str, *, private_key: Ed25519PrivateKey | None = None) -> bytes:
        key = private_key or self.primary_private_key()
        return key.sign(self.signing_input(canonical, digest))

    def verify(
        self, canonical: bytes, digest: str, signature: bytes, *, public_key: Ed25519PublicKey | None = None
    ) -> bool:
        key = public_key or self.primary_private_key().public_key()
        try:
            key.verify(signature, self.signing_input(canonical, digest))
        except InvalidSignature:
            return False
        return True


def sign_object(
    signing: SignInfo, schema: dict[str, Any], obj: dict[str, Any], *, private_key: Ed25519PrivateKey | None = None
) -> bytes:
    """Sign `obj` as it stands right now. Used to produce a *stale* signature
    for a tamper vector: sign the untampered object, then hand that signature
    to a case built from the tampered one -- the recorded signature input for
    the tampered bytes will not match what was actually signed."""
    canon = canonical_bytes(schema, obj)
    return signing.sign(canon, digest_hex(canon), private_key=private_key)


def _sign_domain_mismatch(info: SignInfo, canonical: bytes, digest: str) -> bytes:
    """Sign under a different, unrelated domain tag. The stored signature
    input recorded in the case is still the *correct* domain-prefixed
    payload (what a real verifier recomputes), so this signature will not
    verify against it."""
    wrong_domain = b"ARC-WRONG-DOMAIN-V1\x00"
    payload = bytes.fromhex(digest) if info.sign_over == "digest" else canonical
    return info.primary_private_key().sign(wrong_domain + payload)


# ---------------------------------------------------------------------------
# Case + manifest bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class Case:
    case_id: str
    kind: str  # "minimal" | "typical" | "maximal" | "negative"
    obj: dict[str, Any] | None
    decision: str  # "accept" | "refuse"
    refusal_code: str | None = None
    canonical: bytes | None = None
    digest: str | None = None
    signature_input: bytes | None = None
    signature: bytes | None = None


@dataclass
class ProfileFixture:
    dir_name: str
    literal: str
    schema: dict[str, Any]
    cases: list[Case] = field(default_factory=list)
    signed: SignInfo | None = None


VALIDATION_FAILED = "arc_proposal_validation_failed"


# ---------------------------------------------------------------------------
# Case-building helpers. Every helper here is a self-check as much as a
# builder: a structural negative that fails to raise, or a semantic negative
# whose rule check fails to raise, aborts fixture generation instead of
# publishing a vector that does not test what its name claims.
# ---------------------------------------------------------------------------


def positive_case(
    case_id: str, kind: str, schema: dict[str, Any], obj: dict[str, Any], signed: SignInfo | None
) -> Case:
    canon = canonical_bytes(schema, obj)
    digest = digest_hex(canon)
    sig_input = sig = None
    if signed is not None:
        sig_input = signed.signing_input(canon, digest)
        sig = signed.sign(canon, digest)
    return Case(
        case_id=case_id,
        kind=kind,
        obj=obj,
        decision="accept",
        canonical=canon,
        digest=digest,
        signature_input=sig_input,
        signature=sig,
    )


def structural_negative(case_id: str, schema: dict[str, Any], obj: dict[str, Any], code: str) -> Case:
    try:
        canonical_bytes(schema, obj)
    except (SchemaError, CanonError):
        pass
    else:
        raise AssertionError(f"{case_id}: expected a structural rejection, but canonicalization succeeded")
    return Case(case_id=case_id, kind="negative", obj=obj, decision="refuse", refusal_code=code)


def semantic_negative(
    case_id: str,
    schema: dict[str, Any],
    obj: dict[str, Any],
    code: str,
    *,
    signed: SignInfo | None = None,
    signature_override: bytes | None = None,
) -> Case:
    canon = canonical_bytes(schema, obj)  # must still canonicalize -- the shape is fine, the value is not
    digest = digest_hex(canon)
    sig_input = sig = None
    if signed is not None:
        sig_input = signed.signing_input(canon, digest)
        sig = signature_override if signature_override is not None else signed.sign(canon, digest)
    return Case(
        case_id=case_id,
        kind="negative",
        obj=obj,
        decision="refuse",
        refusal_code=code,
        canonical=canon,
        digest=digest,
        signature_input=sig_input,
        signature=sig,
    )


def rule_negative(
    case_id: str,
    schema: dict[str, Any],
    obj: dict[str, Any],
    code: str,
    rule_check: Any,
) -> Case:
    canon = canonical_bytes(schema, obj)
    try:
        rule_check(obj)
    except ProfileRuleError:
        pass
    else:
        raise AssertionError(f"{case_id}: expected a rule violation, but the rule check accepted the object")
    digest = digest_hex(canon)
    return Case(
        case_id=case_id, kind="negative", obj=obj, decision="refuse", refusal_code=code, canonical=canon, digest=digest
    )


def confusion_case(case_id: str, target_schema: dict[str, Any], donor_obj: dict[str, Any], code: str) -> Case:
    """`donor_obj` is another profile's own valid instance, submitted as this
    profile. It must be rejected by this profile's schema -- if the `profile`
    field alone did not already prove it, the closed field set will."""
    try:
        validate(target_schema, donor_obj)
    except SchemaError:
        pass
    else:
        raise AssertionError(f"{case_id}: expected profile confusion to be rejected, but it validated")
    return Case(case_id=case_id, kind="negative", obj=donor_obj, decision="refuse", refusal_code=code)


# ---------------------------------------------------------------------------
# Profile-specific business rules a closed field-set schema cannot express.
# ---------------------------------------------------------------------------


def check_field_provenance_conditional(obj: dict[str, Any]) -> None:
    groups: dict[str, dict[str, list[str]]] = {
        "source_backed": {
            "required": ["source_anchor", "quoted_excerpt_digest"],
            "forbidden": ["author_issuer", "author_subject", "author_role", "derivation_profile"],
        },
        "human_judgment": {
            "required": ["author_issuer", "author_subject", "author_role"],
            "forbidden": ["derivation_profile"],
        },
        "server_derived": {
            "required": ["derivation_profile"],
            "forbidden": ["source_anchor", "quoted_excerpt_digest", "author_issuer", "author_subject", "author_role"],
        },
    }
    spec = groups[obj["provenance_class"]]
    for name in spec["required"]:
        if obj.get(name) is None:
            raise ProfileRuleError(f"{obj['provenance_class']} requires {name!r} to be non-null")
    for name in spec["forbidden"]:
        if obj.get(name) is not None:
            raise ProfileRuleError(f"{obj['provenance_class']} forbids {name!r} to be non-null")


def _predicate_key(predicate: dict[str, Any]) -> str:
    return json.dumps(predicate, sort_keys=True, ensure_ascii=False)


def check_envelope_non_overlap(obj: dict[str, Any]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for item in obj["items"]:
        key = (item["delta_code"], _predicate_key(item["class_predicate"]))
        if key in seen:
            raise ProfileRuleError(
                f"items {seen[key]!r} and {item['item_id']!r} share delta code {item['delta_code']!r} "
                "with an overlapping predicate"
            )
        seen[key] = item["item_id"]


def check_actor_separation(obj: dict[str, Any]) -> None:
    submitter = (obj["submitter_issuer"], obj["submitter_subject"])
    approver = (obj["approver_issuer"], obj["approver_subject"])
    activator = (obj["activator_issuer"], obj["activator_subject"])
    if submitter == approver:
        raise ProfileRuleError("submitter and approver must be distinct principals")
    if obj["risk_classification"] == "global_mandatory" and len({submitter, approver, activator}) != 3:
        raise ProfileRuleError("a global mandatory classification requires three distinct principals")


# ---------------------------------------------------------------------------
# Shared enums and fixed identifiers, reused across profiles so the fixture
# set reads as one coherent authoring thread instead of sixteen unrelated
# objects.
# ---------------------------------------------------------------------------

RISK_CLASSIFICATIONS = (
    "global_mandatory",
    "global_non_mandatory",
    "tenant_mandatory",
    "tenant_non_mandatory",
    "domain_mandatory",
    "domain_non_mandatory",
    "capability_mandatory",
    "capability_non_mandatory",
    "task_mandatory",
    "task_non_mandatory",
)
DELTA_CODES = (
    "newly_selected",
    "no_longer_selected",
    "conflict_changed",
    "mandatory_block_added",
    "mandatory_block_removed",
)

ARTIFACT_ID = uid(1)
REVISION_ID = uid(2)
PROPOSAL_ID = uid(3)
TENANT_ID = uid(5)
CAPABILITY_ID = uid(6)
DIRECTIVE_ID_1 = uid(101)
DIRECTIVE_ID_2 = uid(102)
RULE_ID_1 = uid(111)
RULE_ID_2 = uid(112)


def build_profiles() -> dict[str, ProfileFixture]:
    profiles: dict[str, ProfileFixture] = {}

    # === 1. source_approval_claim_v1 ===================================
    claim_schema = PROFILE_SCHEMA(
        "arc_source_approval_claim_v1",
        {
            "source_system": STR(),
            "source_revision_locator": STR(),
            "source_content_digest_algorithm": ENUM("sha256"),
            "source_content_digest": DIGEST(),
            "source_content_type": STR(),
            "approval_locator": STR(),
            "approving_authority_issuer": STR(),
            "approving_authority_subject": STR(),
            "approval_scope": STR(),
            "approved_at": TS(),
            "expires_at": TS(),
        },
    )
    claim_signing = SignInfo(
        domain_prefix=b"ARC-SOURCE-APPROVAL-CLAIM-V1\x00",
        human_domain="arc.source_approval.v1",
        sign_over="canonical_bytes",
        key_label="source_approval_claim_v1",
    )
    claim_typical = {
        "profile": "arc_source_approval_claim_v1",
        "source_system": "docs-cms",
        "source_revision_locator": "docs-cms://policy/data-retention/rev-42",
        "source_content_digest_algorithm": "sha256",
        "source_content_digest": dig("source-body:data-retention-rev-42"),
        "source_content_type": "text/markdown",
        "approval_locator": "docs-cms://policy/data-retention/rev-42/approval/7",
        "approving_authority_issuer": "https://idp.upstream.example/",
        "approving_authority_subject": "policy-owner:data-governance",
        "approval_scope": "docs-cms:policy:data-retention",
        "approved_at": "2026-01-05T12:00:00Z",
        "expires_at": "2026-07-05T12:00:00Z",
    }
    claim_minimal = {
        **claim_typical,
        "source_system": "s",
        "source_revision_locator": "s://x/1",
        "approval_locator": "s://x/1/a",
        "approving_authority_subject": "u",
        "approval_scope": "s:*",
        "approved_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-01T00:00:01Z",
    }
    claim_maximal = {
        **claim_typical,
        "source_system": "docs-cms-équipe-gouvernance-des-données",
        "approval_scope": "docs-cms:policy:data-retention:eu-central:all-tenants:extended-review",
        "expires_at": "2026-12-31T23:59:59.999999Z",
    }
    claim_cases = [
        positive_case("minimal", "minimal", claim_schema, claim_minimal, claim_signing),
        positive_case("typical", "typical", claim_schema, claim_typical, claim_signing),
        positive_case("maximal", "maximal", claim_schema, claim_maximal, claim_signing),
        structural_negative(
            "missing_field",
            claim_schema,
            {k: v for k, v in claim_typical.items() if k != "approval_scope"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", claim_schema, {**claim_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            claim_schema,
            {**claim_typical, "approving_authority_subject": make_non_nfc("policy-owner")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            claim_schema,
            {**claim_typical, "approval_scope": make_embedded_nul("docs-cms:policy")},
            VALIDATION_FAILED,
        ),
        # No numeric field exists on this profile, so `fractional_number` has
        # no host field to apply to -- skipped rather than faked.
        structural_negative(
            "equivalent_timezone_offset",
            claim_schema,
            {**claim_typical, "expires_at": "2026-07-05T14:00:00+02:00"},
            VALIDATION_FAILED,
        ),
        # `source_content_digest` names the admitted bytes' digest, which are
        # not themselves a fixture object in this set -- there is no
        # independently checkable ground truth to substitute against, so
        # `digest_substitution` is skipped here rather than asserted on faith.
        # (It is covered where a real cross-fixture reference exists: the
        # evidence/attestation envelopes below re-embed this exact claim.)
        semantic_negative(
            "expiry_equality",
            claim_schema,
            {**claim_typical, "expires_at": claim_typical["approved_at"]},
            "arc_source_admission_refused",
        ),
        semantic_negative(
            "principal_mismatch",
            claim_schema,
            {**claim_typical, "approving_authority_subject": "someone-else"},
            "arc_source_admission_refused",
            signed=claim_signing,
            signature_override=sign_object(claim_signing, claim_schema, claim_typical),
        ),
        semantic_negative(
            "signature_domain_mismatch",
            claim_schema,
            claim_typical,
            "arc_source_admission_refused",
            signed=claim_signing,
            signature_override=_sign_domain_mismatch(
                claim_signing,
                canonical_bytes(claim_schema, claim_typical),
                digest_hex(canonical_bytes(claim_schema, claim_typical)),
            ),
        ),
        semantic_negative(
            "signature_key_mismatch",
            claim_schema,
            claim_typical,
            "arc_source_admission_refused",
            signed=claim_signing,
            signature_override=sign_object(
                claim_signing, claim_schema, claim_typical, private_key=claim_signing.wrong_private_key()
            ),
        ),
    ]
    profiles["source_approval_claim_v1"] = ProfileFixture(
        dir_name="source_approval_claim_v1",
        literal="arc_source_approval_claim_v1",
        schema=claim_schema,
        cases=claim_cases,
        signed=claim_signing,
    )
    claim_digest_real = digest_hex(canonical_bytes(claim_schema, claim_typical))

    # === 2. source_verifier_attestation_v1 ==============================
    attestation_schema = PROFILE_SCHEMA(
        "arc_source_verifier_attestation_v1",
        {
            "attestation_id": UUID(),
            "provider_id": STR(),
            "provider_configuration_digest": DIGEST(),
            "claim_digest": DIGEST(),
            "approving_authority_issuer": STR(),
            "approving_authority_subject": STR(),
            "source_system": STR(),
            "approval_scope": STR(),
            "issued_at": TS(),
            "expires_at": TS(),
        },
    )
    attestation_signing = SignInfo(
        domain_prefix=b"ARC-SOURCE-VERIFIER-ATTESTATION-V1\x00",
        human_domain="arc.source_verifier_attestation.v1",
        sign_over="canonical_bytes",
        key_label="source_verifier_attestation_v1",
    )
    attestation_typical = {
        "profile": "arc_source_verifier_attestation_v1",
        "attestation_id": uid(20),
        "provider_id": "trusted-attestation-provider-1",
        "provider_configuration_digest": dig("provider-config:trusted-attestation-provider-1"),
        "claim_digest": claim_digest_real,
        "approving_authority_issuer": claim_typical["approving_authority_issuer"],
        "approving_authority_subject": claim_typical["approving_authority_subject"],
        "source_system": claim_typical["source_system"],
        "approval_scope": claim_typical["approval_scope"],
        "issued_at": "2026-01-05T12:05:00Z",
        "expires_at": "2026-06-05T12:05:00Z",
    }
    attestation_minimal = {**attestation_typical, "provider_id": "p", "expires_at": "2026-01-05T12:05:01Z"}
    attestation_maximal = {
        **attestation_typical,
        "provider_id": "trusted-attestation-provider-1-régionale",
        "expires_at": "2026-12-31T23:59:59.5Z",
    }
    attestation_cases = [
        positive_case("minimal", "minimal", attestation_schema, attestation_minimal, attestation_signing),
        positive_case("typical", "typical", attestation_schema, attestation_typical, attestation_signing),
        positive_case("maximal", "maximal", attestation_schema, attestation_maximal, attestation_signing),
        structural_negative(
            "missing_field",
            attestation_schema,
            {k: v for k, v in attestation_typical.items() if k != "provider_id"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", attestation_schema, {**attestation_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            attestation_schema,
            {**attestation_typical, "provider_id": make_non_nfc("trusted-provider")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            attestation_schema,
            {**attestation_typical, "source_system": make_embedded_nul("docs-cms")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "equivalent_timezone_offset",
            attestation_schema,
            {**attestation_typical, "expires_at": "2026-06-05T14:05:00+02:00"},
            VALIDATION_FAILED,
        ),
        semantic_negative(
            "digest_substitution",
            attestation_schema,
            {**attestation_typical, "claim_digest": flip_hex(attestation_typical["claim_digest"])},
            "arc_source_admission_refused",
        ),
        semantic_negative(
            "expiry_equality",
            attestation_schema,
            {**attestation_typical, "expires_at": attestation_typical["issued_at"]},
            "arc_source_admission_refused",
        ),
        semantic_negative(
            "principal_mismatch",
            attestation_schema,
            {**attestation_typical, "approving_authority_subject": "someone-else"},
            "arc_source_admission_refused",
            signed=attestation_signing,
            signature_override=sign_object(attestation_signing, attestation_schema, attestation_typical),
        ),
        semantic_negative(
            "signature_domain_mismatch",
            attestation_schema,
            attestation_typical,
            "arc_source_admission_refused",
            signed=attestation_signing,
            signature_override=_sign_domain_mismatch(
                attestation_signing,
                canonical_bytes(attestation_schema, attestation_typical),
                digest_hex(canonical_bytes(attestation_schema, attestation_typical)),
            ),
        ),
        semantic_negative(
            "signature_key_mismatch",
            attestation_schema,
            attestation_typical,
            "arc_source_admission_refused",
            signed=attestation_signing,
            signature_override=sign_object(
                attestation_signing,
                attestation_schema,
                attestation_typical,
                private_key=attestation_signing.wrong_private_key(),
            ),
        ),
    ]
    profiles["source_verifier_attestation_v1"] = ProfileFixture(
        dir_name="source_verifier_attestation_v1",
        literal="arc_source_verifier_attestation_v1",
        schema=attestation_schema,
        cases=attestation_cases,
        signed=attestation_signing,
    )

    # === 3. source_approval_evidence_v1 (unsigned envelope; trust comes from
    # the nested claim signature or attestation signature) ================
    evidence_schema = PROFILE_SCHEMA(
        "arc_source_approval_evidence_v1",
        {
            "evidence_id": UUID(),
            "claim": claim_schema,
            "claim_digest": DIGEST(),
            "verification_method": ENUM("source_signed", "verifier_attested"),
            "verifier_id": STR(),
            "signature": nullable(STR()),
            "verifier_attestation": nullable(attestation_schema),
            "admission_method": ENUM("configured_connector", "authorized_upload"),
            "connector_id": nullable(STR()),
            "admitted_at": TS(),
            "admitted_by_issuer": STR(),
            "admitted_by_subject": STR(),
            "verified_at": TS(),
            "idempotency_key_digest": DIGEST(),
            "admission_request_payload_digest": DIGEST(),
        },
    )
    claim_signature_typical = sign_object(claim_signing, claim_schema, claim_typical)
    evidence_typical = {
        "profile": "arc_source_approval_evidence_v1",
        "evidence_id": uid(30),
        "claim": claim_typical,
        "claim_digest": claim_digest_real,
        "verification_method": "source_signed",
        "verifier_id": "verifier-docs-cms-1",
        "signature": base64.b64encode(claim_signature_typical).decode("ascii"),
        "verifier_attestation": None,
        "admission_method": "configured_connector",
        "connector_id": "connector-docs-cms",
        "admitted_at": "2026-01-05T12:10:00Z",
        "admitted_by_issuer": "https://idp.registry.example/",
        "admitted_by_subject": "svc-arc-admission",
        "verified_at": "2026-01-05T12:10:05Z",
        "idempotency_key_digest": dig("idempotency:docs-cms:rev-42"),
        "admission_request_payload_digest": dig("admission-request:docs-cms:rev-42"),
    }
    evidence_minimal = {
        **evidence_typical,
        "verification_method": "verifier_attested",
        "signature": None,
        "verifier_attestation": attestation_typical,
        "connector_id": None,
        "admission_method": "authorized_upload",
    }
    evidence_maximal = {**evidence_typical, "verifier_id": "verifier-docs-cms-régionale-1"}
    evidence_cases = [
        positive_case("minimal", "minimal", evidence_schema, evidence_minimal, None),
        positive_case("typical", "typical", evidence_schema, evidence_typical, None),
        positive_case("maximal", "maximal", evidence_schema, evidence_maximal, None),
        structural_negative(
            "missing_field",
            evidence_schema,
            {k: v for k, v in evidence_typical.items() if k != "verifier_id"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", evidence_schema, {**evidence_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            evidence_schema,
            {**evidence_typical, "verifier_id": make_non_nfc("verifier-docs-cms")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            evidence_schema,
            {**evidence_typical, "admitted_by_subject": make_embedded_nul("svc-arc-admission")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "equivalent_timezone_offset",
            evidence_schema,
            {**evidence_typical, "verified_at": "2026-01-05T14:10:05+02:00"},
            VALIDATION_FAILED,
        ),
        # `claim_digest` is recomputable straight from the embedded `claim`
        # object without any other fixture file -- a real, self-contained
        # cross-check, not an opaque asserted value.
        semantic_negative(
            "digest_substitution",
            evidence_schema,
            {**evidence_typical, "claim_digest": flip_hex(evidence_typical["claim_digest"])},
            "arc_source_admission_refused",
        ),
        # `admitted_by_*` records who at ARC admitted the bytes, not a
        # principal the signed claim binds -- there is no independently
        # checkable ground truth for a "wrong principal" here, so
        # `principal_mismatch` is skipped for this envelope profile rather
        # than asserted without a way to verify it. The claim it wraps
        # carries that check instead.
    ]
    profiles["source_approval_evidence_v1"] = ProfileFixture(
        dir_name="source_approval_evidence_v1",
        literal="arc_source_approval_evidence_v1",
        schema=evidence_schema,
        cases=evidence_cases,
        signed=None,
    )

    # === 4. observation_class_predicate_v1 ==============================
    # Null means unconstrained; an empty set is a distinct, invalid state --
    # not "no constraint" and not a legal constraint either. Every field is a
    # nullable set of at least one element when present.
    def _predicate_field(item_schema: dict[str, Any]) -> dict[str, Any]:
        return nullable(ARR(item_schema, kind="set", min_items=1))

    predicate_schema = PROFILE_SCHEMA(
        "arc_observation_class_predicate_v1",
        {
            "task_kind": _predicate_field(STR()),
            "requested_action_classes": _predicate_field(STR()),
            "environment": _predicate_field(STR()),
            "data_sensitivity_tier": _predicate_field(STR()),
            "capability_ids": _predicate_field(UUID()),
            "domain_ids": _predicate_field(STR()),
        },
    )
    predicate_typical = {
        "profile": "arc_observation_class_predicate_v1",
        "task_kind": ["code_change"],
        "requested_action_classes": ["merge"],
        "environment": ["production"],
        "data_sensitivity_tier": ["confidential"],
        "capability_ids": [CAPABILITY_ID],
        "domain_ids": ["payments"],
    }
    predicate_minimal = dict.fromkeys(
        (
            "task_kind",
            "requested_action_classes",
            "environment",
            "data_sensitivity_tier",
            "capability_ids",
            "domain_ids",
        ),
        None,
    )
    predicate_minimal["profile"] = "arc_observation_class_predicate_v1"
    predicate_maximal = {
        "profile": "arc_observation_class_predicate_v1",
        # Deliberately unsorted in the source file -- canonicalization must
        # sort a set-valued array, and this proves it rather than assuming it.
        "task_kind": ["migration", "code_change"],
        "requested_action_classes": ["merge", "close"],
        "environment": ["production", "staging"],
        "data_sensitivity_tier": ["confidential", "internal"],
        "capability_ids": [uid(7), CAPABILITY_ID],
        "domain_ids": ["payments", "billing"],
    }
    predicate_cases = [
        positive_case("minimal", "minimal", predicate_schema, predicate_minimal, None),
        positive_case("typical", "typical", predicate_schema, predicate_typical, None),
        positive_case("maximal", "maximal", predicate_schema, predicate_maximal, None),
        structural_negative(
            "missing_field",
            predicate_schema,
            {k: v for k, v in predicate_typical.items() if k != "task_kind"},
            VALIDATION_FAILED,
        ),
        # The exact forbidden-key example this profile's own rule names:
        # identity and free-text dimensions are not observation-class
        # selectors, however plausible the key looks.
        structural_negative(
            "unknown_field", predicate_schema, {**predicate_typical, "tenant_id": TENANT_ID}, VALIDATION_FAILED
        ),
        structural_negative(
            "null_vs_empty", predicate_schema, {**predicate_typical, "task_kind": []}, VALIDATION_FAILED
        ),
        structural_negative(
            "duplicate_set_entry",
            predicate_schema,
            {**predicate_typical, "task_kind": ["code_change", "code_change"]},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "non_nfc_text",
            predicate_schema,
            {**predicate_typical, "environment": [make_non_nfc("production")]},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            predicate_schema,
            {**predicate_typical, "domain_ids": [make_embedded_nul("payments")]},
            VALIDATION_FAILED,
        ),
    ]
    profiles["observation_class_predicate_v1"] = ProfileFixture(
        dir_name="observation_class_predicate_v1",
        literal="arc_observation_class_predicate_v1",
        schema=predicate_schema,
        cases=predicate_cases,
        signed=None,
    )

    # === 5. expected_impact_envelope_v1 ==================================
    envelope_item_schema = OBJ(
        {
            "item_id": STR(),
            "delta_code": ENUM(*DELTA_CODES),
            "class_predicate": predicate_schema,
            "minimum_count": NUM(),
            "maximum_count": nullable(NUM()),
            "rationale_code": STR(),
        },
        ["item_id", "delta_code", "class_predicate", "minimum_count", "maximum_count", "rationale_code"],
    )
    envelope_schema = PROFILE_SCHEMA(
        "arc_expected_impact_envelope_v1",
        {
            "envelope_id": UUID(),
            "proposal_id": UUID(),
            "proposal_version": NUM(),
            "items": ARR(envelope_item_schema, kind="ordered", order_key="item_id", min_items=1),
            "author_issuer": STR(),
            "author_subject": STR(),
            "created_at": TS(),
        },
    )
    envelope_item_1 = {
        "item_id": "item-1",
        "delta_code": "newly_selected",
        "class_predicate": predicate_typical,
        "minimum_count": 1,
        "maximum_count": 50,
        "rationale_code": "mandatory_rule_widens_reach",
    }
    envelope_item_2 = {
        "item_id": "item-2",
        "delta_code": "conflict_changed",
        "class_predicate": {**predicate_typical, "task_kind": ["release"], "domain_ids": ["billing"]},
        "minimum_count": 0,
        "maximum_count": None,
        "rationale_code": "supersedes_prior_billing_directive",
    }
    envelope_typical = {
        "profile": "arc_expected_impact_envelope_v1",
        "envelope_id": uid(40),
        "proposal_id": PROPOSAL_ID,
        "proposal_version": 1,
        "items": [envelope_item_1, envelope_item_2],
        "author_issuer": "https://idp.registry.example/",
        "author_subject": "author-1",
        "created_at": "2026-01-06T09:00:00Z",
    }
    envelope_minimal = {**envelope_typical, "items": [{**envelope_item_1, "item_id": "item-1", "maximum_count": None}]}
    envelope_maximal = {
        **envelope_typical,
        "items": [
            envelope_item_1,
            envelope_item_2,
            {
                "item_id": "item-3",
                "delta_code": "mandatory_block_added",
                "class_predicate": {**predicate_typical, "environment": ["staging"]},
                "minimum_count": 1,
                "maximum_count": 10,
                "rationale_code": "new_global_mandatory_rule",
            },
        ],
    }
    envelope_cases = [
        positive_case("minimal", "minimal", envelope_schema, envelope_minimal, None),
        positive_case("typical", "typical", envelope_schema, envelope_typical, None),
        positive_case("maximal", "maximal", envelope_schema, envelope_maximal, None),
        structural_negative(
            "missing_field",
            envelope_schema,
            {k: v for k, v in envelope_typical.items() if k != "author_subject"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", envelope_schema, {**envelope_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative("null_vs_empty", envelope_schema, {**envelope_typical, "items": []}, VALIDATION_FAILED),
        structural_negative(
            "array_ordering",
            envelope_schema,
            {**envelope_typical, "items": [envelope_item_2, envelope_item_1]},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "non_nfc_text",
            envelope_schema,
            {**envelope_typical, "author_subject": make_non_nfc("author")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            envelope_schema,
            {**envelope_typical, "author_subject": make_embedded_nul("author-1")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "fractional_number", envelope_schema, {**envelope_typical, "proposal_version": 1.5}, VALIDATION_FAILED
        ),
        rule_negative(
            "overlapping_items",
            envelope_schema,
            {**envelope_typical, "items": [envelope_item_1, {**envelope_item_1, "item_id": "item-1b"}]},
            "arc_envelope_invalid",
            check_envelope_non_overlap,
        ),
    ]
    profiles["expected_impact_envelope_v1"] = ProfileFixture(
        dir_name="expected_impact_envelope_v1",
        literal="arc_expected_impact_envelope_v1",
        schema=envelope_schema,
        cases=envelope_cases,
        signed=None,
    )
    envelope_digest_real = digest_hex(canonical_bytes(envelope_schema, envelope_typical))

    # === 6. field_provenance_v1 ==========================================
    provenance_schema = PROFILE_SCHEMA(
        "arc_field_provenance_v1",
        {
            "field_path": STR(),
            "provenance_class": ENUM("source_backed", "human_judgment", "server_derived"),
            "source_anchor": nullable(STR()),
            "quoted_excerpt_digest": nullable(DIGEST()),
            "author_issuer": nullable(STR()),
            "author_subject": nullable(STR()),
            "author_role": nullable(STR()),
            "derivation_profile": nullable(STR()),
        },
    )
    provenance_typical = {
        "profile": "arc_field_provenance_v1",
        "field_path": "directives[0].compact_statement_plaintext",
        "provenance_class": "source_backed",
        "source_anchor": "docs-cms://policy/data-retention/rev-42#section-3",
        "quoted_excerpt_digest": dig("excerpt:data-retention-rev-42:section-3"),
        "author_issuer": None,
        "author_subject": None,
        "author_role": None,
        "derivation_profile": None,
    }
    provenance_minimal = {
        **provenance_typical,
        "field_path": "revision_id",
        "provenance_class": "server_derived",
        "source_anchor": None,
        "quoted_excerpt_digest": None,
        "derivation_profile": "materialiser:v1",
    }
    provenance_maximal = {
        **provenance_typical,
        "field_path": "applicability[0].is_mandatory",
        "provenance_class": "human_judgment",
        "source_anchor": "docs-cms://policy/data-retention/rev-42#section-5",
        "quoted_excerpt_digest": dig("excerpt:data-retention-rev-42:section-5"),
        "author_issuer": "https://idp.registry.example/",
        "author_subject": "author-1",
        "author_role": "policy_reviewer",
    }
    provenance_cases = [
        positive_case("minimal", "minimal", provenance_schema, provenance_minimal, None),
        positive_case("typical", "typical", provenance_schema, provenance_typical, None),
        positive_case("maximal", "maximal", provenance_schema, provenance_maximal, None),
        structural_negative(
            "missing_field",
            provenance_schema,
            {k: v for k, v in provenance_typical.items() if k != "field_path"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", provenance_schema, {**provenance_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            provenance_schema,
            {**provenance_typical, "field_path": make_non_nfc("directives[0]")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            provenance_schema,
            {**provenance_typical, "source_anchor": make_embedded_nul(provenance_typical["source_anchor"])},
            VALIDATION_FAILED,
        ),
        # `source_backed` requires `source_anchor`/`quoted_excerpt_digest` and
        # forbids the human-judgment fields -- a closed field-set schema
        # cannot express "this key must be null when that other key is
        # non-null", so this is a dedicated rule check rather than a type
        # constraint.
        rule_negative(
            "conditional_requiredness_violation",
            provenance_schema,
            {**provenance_typical, "author_role": "policy_reviewer"},
            "arc_provenance_invalid",
            check_field_provenance_conditional,
        ),
        # No fixture object backs `quoted_excerpt_digest`'s referent (a
        # source-text excerpt is not itself one of these sixteen profiles),
        # so `digest_substitution` is skipped for the same reason it was
        # skipped on the raw source claim: nothing here could independently
        # tell a substituted digest from a correct one.
    ]
    profiles["field_provenance_v1"] = ProfileFixture(
        dir_name="field_provenance_v1",
        literal="arc_field_provenance_v1",
        schema=provenance_schema,
        cases=provenance_cases,
        signed=None,
    )

    # === 7. artifact_semantics_v1 (the `S` node) =========================
    directive_schema = OBJ(
        {
            "directive_id": UUID(),
            "directive_type": ENUM("citation_only", "verify_before_action"),
            "compact_statement_plaintext": STR(),
            "compact_statement_plaintext_digest": DIGEST(),
            "source_anchor": STR(),
            "conflict_key_schema_version": NUM(),
            "conflict_key_namespace": nullable(STR()),
            "conflict_key_subject_selector": nullable(STR()),
            "conflict_key_operation": nullable(STR()),
            "conflict_key_action_class": nullable(STR()),
            "conflict_key_target_selector": nullable(STR()),
            "conflict_key_modality": nullable(STR()),
            "conflict_key_constraint_operator": nullable(STR()),
            "conflict_key_constraint_value": nullable(STR()),
            "conflict_subject_digest": nullable(DIGEST()),
            "delegable_exception": BOOL(),
            "satisfaction_mode": nullable(ENUM("self_attested", "signed_result")),
            "verification_max_age_seconds": nullable(NUM()),
            "accepted_verifier_classes": nullable(ARR(STR(), kind="set")),
            "accepted_verifier_ids": nullable(ARR(STR(), kind="set")),
            "required_evidence_type": nullable(STR()),
            "created_at": TS(),
        },
        [
            "directive_id",
            "directive_type",
            "compact_statement_plaintext",
            "compact_statement_plaintext_digest",
            "source_anchor",
            "conflict_key_schema_version",
            "conflict_key_namespace",
            "conflict_key_subject_selector",
            "conflict_key_operation",
            "conflict_key_action_class",
            "conflict_key_target_selector",
            "conflict_key_modality",
            "conflict_key_constraint_operator",
            "conflict_key_constraint_value",
            "conflict_subject_digest",
            "delegable_exception",
            "satisfaction_mode",
            "verification_max_age_seconds",
            "accepted_verifier_classes",
            "accepted_verifier_ids",
            "required_evidence_type",
            "created_at",
        ],
    )
    rule_schema = OBJ(
        {
            "rule_id": UUID(),
            "scope": ENUM("global", "tenant", "domain", "capability", "task"),
            "target_tenant_id": nullable(UUID()),
            "capability_ids": nullable(ARR(UUID(), kind="set")),
            "capability_labels": nullable(ARR(STR(), kind="set")),
            "domain_ids": nullable(ARR(STR(), kind="set")),
            "task_kinds": nullable(ARR(STR(), kind="set")),
            "action_classes": nullable(ARR(STR(), kind="set")),
            "environments": nullable(ARR(STR(), kind="set")),
            "data_sensitivity_tiers": nullable(ARR(STR(), kind="set")),
            "effective_from": nullable(TS()),
            "effective_until": nullable(TS()),
            "is_mandatory": BOOL(),
        },
        [
            "rule_id",
            "scope",
            "target_tenant_id",
            "capability_ids",
            "capability_labels",
            "domain_ids",
            "task_kinds",
            "action_classes",
            "environments",
            "data_sensitivity_tiers",
            "effective_from",
            "effective_until",
            "is_mandatory",
        ],
    )
    semantics_schema = PROFILE_SCHEMA(
        "arc_artifact_semantics_v1",
        {
            "projection_schema_version": NUM(),
            "materialiser_profile": STR(),
            "materialiser_version": STR(),
            "applicability_baseline_version": STR(),
            "artifact_id": UUID(),
            "revision_id": UUID(),
            "kind": ENUM("directive_bundle", "task_summary_template"),
            "owning_scope": ENUM("global", "tenant"),
            "owning_tenant_id": nullable(UUID()),
            "visibility": ENUM("standard", "restricted"),
            "source_system": STR(),
            "source_revision_locator": STR(),
            "source_content_digest": DIGEST(),
            "source_approval_evidence_digest": DIGEST(),
            "directives": ARR(directive_schema, kind="ordered", order_key="directive_id"),
            "applicability": ARR(rule_schema, kind="ordered", order_key="rule_id"),
            "detail_audience": ENUM("agent_only", "human_only", "agent_and_human"),
            "review_expires_at": TS(),
            "content_classification": ENUM("public", "internal", "confidential"),
            "approved_retention_floor_days": NUM(),
            "initial_freshness_basis": ENUM("connector_verified", "revision_pinned_only"),
            "reviewed_baseline_revision_id": nullable(UUID()),
        },
    )
    directive_1 = {
        "directive_id": DIRECTIVE_ID_1,
        "directive_type": "verify_before_action",
        "compact_statement_plaintext": "Data retention for policy documents must not exceed 400 days.",
        "compact_statement_plaintext_digest": dig("compact-statement:directive-1"),
        "source_anchor": "docs-cms://policy/data-retention/rev-42#section-2",
        "conflict_key_schema_version": 1,
        "conflict_key_namespace": "arc.retention",
        "conflict_key_subject_selector": "capability:*",
        "conflict_key_operation": "retain",
        "conflict_key_action_class": "data_retention",
        "conflict_key_target_selector": "domain:payments",
        "conflict_key_modality": "must_not",
        "conflict_key_constraint_operator": "lte_days",
        "conflict_key_constraint_value": "400",
        "conflict_subject_digest": dig("conflict-subject:directive-1"),
        "delegable_exception": False,
        "satisfaction_mode": "signed_result",
        "verification_max_age_seconds": 86400,
        "accepted_verifier_classes": ["retention_attestation_provider"],
        "accepted_verifier_ids": ["verifier-retention-1"],
        "required_evidence_type": "retention_attestation",
        "created_at": "2026-01-05T12:15:00Z",
    }
    directive_2 = {
        "directive_id": DIRECTIVE_ID_2,
        "directive_type": "citation_only",
        "compact_statement_plaintext": "Payments data is classified confidential per policy section 5.",
        "compact_statement_plaintext_digest": dig("compact-statement:directive-2"),
        "source_anchor": "docs-cms://policy/data-retention/rev-42#section-5",
        "conflict_key_schema_version": 1,
        "conflict_key_namespace": None,
        "conflict_key_subject_selector": None,
        "conflict_key_operation": None,
        "conflict_key_action_class": None,
        "conflict_key_target_selector": None,
        "conflict_key_modality": None,
        "conflict_key_constraint_operator": None,
        "conflict_key_constraint_value": None,
        "conflict_subject_digest": None,
        "delegable_exception": True,
        "satisfaction_mode": None,
        "verification_max_age_seconds": None,
        "accepted_verifier_classes": None,
        "accepted_verifier_ids": None,
        "required_evidence_type": None,
        "created_at": "2026-01-05T12:16:00Z",
    }
    rule_1 = {
        "rule_id": RULE_ID_1,
        "scope": "tenant",
        "target_tenant_id": TENANT_ID,
        "capability_ids": [CAPABILITY_ID],
        "capability_labels": ["payments.write"],
        "domain_ids": ["payments"],
        "task_kinds": ["code_change"],
        "action_classes": ["merge"],
        "environments": ["production"],
        "data_sensitivity_tiers": ["confidential"],
        "effective_from": "2026-01-06T00:00:00Z",
        "effective_until": None,
        "is_mandatory": True,
    }
    rule_2 = {
        "rule_id": RULE_ID_2,
        "scope": "global",
        "target_tenant_id": None,
        "capability_ids": None,
        "capability_labels": None,
        "domain_ids": None,
        "task_kinds": None,
        "action_classes": None,
        "environments": None,
        "data_sensitivity_tiers": None,
        "effective_from": None,
        "effective_until": None,
        "is_mandatory": False,
    }
    semantics_typical = {
        "profile": "arc_artifact_semantics_v1",
        "projection_schema_version": 1,
        "materialiser_profile": "arc_materialiser_v1",
        "materialiser_version": "1.0.0",
        "applicability_baseline_version": "2026-01-01",
        "artifact_id": ARTIFACT_ID,
        "revision_id": REVISION_ID,
        "kind": "directive_bundle",
        "owning_scope": "tenant",
        "owning_tenant_id": TENANT_ID,
        "visibility": "standard",
        "source_system": claim_typical["source_system"],
        "source_revision_locator": claim_typical["source_revision_locator"],
        "source_content_digest": claim_typical["source_content_digest"],
        "source_approval_evidence_digest": digest_hex(canonical_bytes(evidence_schema, evidence_typical)),
        "directives": [directive_1, directive_2],
        "applicability": [rule_1, rule_2],
        "detail_audience": "agent_and_human",
        "review_expires_at": "2027-01-05T12:00:00Z",
        "content_classification": "confidential",
        "approved_retention_floor_days": 400,
        "initial_freshness_basis": "connector_verified",
        "reviewed_baseline_revision_id": None,
    }
    semantics_minimal = {
        **semantics_typical,
        "kind": "task_summary_template",
        "owning_scope": "global",
        "owning_tenant_id": None,
        "directives": [],
        "applicability": [],
    }
    semantics_maximal = {**semantics_typical, "reviewed_baseline_revision_id": uid(4)}
    semantics_cases = [
        positive_case("minimal", "minimal", semantics_schema, semantics_minimal, None),
        positive_case("typical", "typical", semantics_schema, semantics_typical, None),
        positive_case("maximal", "maximal", semantics_schema, semantics_maximal, None),
        structural_negative(
            "missing_field",
            semantics_schema,
            {k: v for k, v in semantics_typical.items() if k != "kind"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", semantics_schema, {**semantics_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            semantics_schema,
            {**semantics_typical, "materialiser_profile": make_non_nfc("arc_materialiser_v1")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            semantics_schema,
            {**semantics_typical, "applicability_baseline_version": make_embedded_nul("2026-01-01")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "fractional_number",
            semantics_schema,
            {**semantics_typical, "approved_retention_floor_days": 400.5},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "equivalent_timezone_offset",
            semantics_schema,
            {**semantics_typical, "review_expires_at": "2027-01-05T14:00:00+02:00"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "array_ordering",
            semantics_schema,
            {**semantics_typical, "directives": [directive_2, directive_1]},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "duplicate_set_entry",
            semantics_schema,
            {
                **semantics_typical,
                "applicability": [{**rule_1, "capability_labels": ["payments.write", "payments.write"]}, rule_2],
            },
            VALIDATION_FAILED,
        ),
        semantic_negative(
            "digest_substitution",
            semantics_schema,
            {
                **semantics_typical,
                "source_approval_evidence_digest": flip_hex(semantics_typical["source_approval_evidence_digest"]),
            },
            "arc_activation_predicate_failed",
        ),
    ]
    profiles["artifact_semantics_v1"] = ProfileFixture(
        dir_name="artifact_semantics_v1",
        literal="arc_artifact_semantics_v1",
        schema=semantics_schema,
        cases=semantics_cases,
        signed=None,
    )
    s_real = digest_hex(canonical_bytes(semantics_schema, semantics_typical))

    # === 8. approval_review_package_v1 (the `R` node) ====================
    provenance_summary_schema = OBJ(
        {
            "field_path": STR(),
            "provenance_class": ENUM("source_backed", "human_judgment", "server_derived"),
            "evidence_digest": nullable(DIGEST()),
            "author_issuer": nullable(STR()),
            "author_subject": nullable(STR()),
        },
        ["field_path", "provenance_class", "evidence_digest", "author_issuer", "author_subject"],
    )
    semantic_test_summary_schema = OBJ(
        {
            "test_id": STR(),
            "canonical_input_digest": DIGEST(),
            "expected_result_digest": DIGEST(),
            "actual_result_digest": DIGEST(),
            "passed": BOOL(),
        },
        ["test_id", "canonical_input_digest", "expected_result_digest", "actual_result_digest", "passed"],
    )
    review_package_schema = PROFILE_SCHEMA(
        "arc_approval_review_package_v1",
        {
            "artifact_semantics_digest": DIGEST(),
            "source_approval_evidence_digest": DIGEST(),
            "field_provenance": ARR(provenance_summary_schema, kind="ordered", order_key="field_path"),
            "semantic_tests": ARR(semantic_test_summary_schema, kind="ordered", order_key="test_id"),
            "risk_classification": ENUM(*RISK_CLASSIFICATIONS),
            "risk_algorithm_version": STR(),
            "expected_impact_envelope_digest": DIGEST(),
            "baseline_diff_digest": DIGEST(),
            "proposal_id": UUID(),
            "proposal_version": NUM(),
            "submitted_by_issuer": STR(),
            "submitted_by_subject": STR(),
            "submitted_at": TS(),
        },
    )
    review_field_provenance_1 = {
        "field_path": "directives[0].compact_statement_plaintext",
        "provenance_class": "source_backed",
        "evidence_digest": provenance_typical["quoted_excerpt_digest"],
        "author_issuer": None,
        "author_subject": None,
    }
    review_field_provenance_2 = {
        "field_path": "revision_id",
        "provenance_class": "server_derived",
        "evidence_digest": None,
        "author_issuer": None,
        "author_subject": None,
    }
    review_package_typical = {
        "profile": "arc_approval_review_package_v1",
        "artifact_semantics_digest": s_real,
        "source_approval_evidence_digest": semantics_typical["source_approval_evidence_digest"],
        "field_provenance": [review_field_provenance_1, review_field_provenance_2],
        "semantic_tests": [
            {
                "test_id": "test-1",
                "canonical_input_digest": dig("semantic-test-input:test-1"),
                "expected_result_digest": dig("semantic-test-expected:test-1"),
                "actual_result_digest": dig("semantic-test-expected:test-1"),
                "passed": True,
            }
        ],
        "risk_classification": "tenant_mandatory",
        "risk_algorithm_version": "arc_risk_classification_v1.0",
        "expected_impact_envelope_digest": envelope_digest_real,
        "baseline_diff_digest": dig("baseline-diff:proposal-1-v1"),
        "proposal_id": PROPOSAL_ID,
        "proposal_version": 1,
        "submitted_by_issuer": "https://idp.registry.example/",
        "submitted_by_subject": "author-1",
        "submitted_at": "2026-01-06T09:05:00Z",
    }
    review_package_minimal = {
        **review_package_typical,
        "field_provenance": [],
        "semantic_tests": [],
        "risk_classification": "task_non_mandatory",
    }
    review_field_provenance_3 = {
        "field_path": "applicability[0].is_mandatory",
        "provenance_class": "human_judgment",
        "evidence_digest": None,
        "author_issuer": "https://idp.registry.example/",
        "author_subject": "author-1",
    }
    review_package_maximal = {
        **review_package_typical,
        # Ascending by `field_path`: "applicability..." < "directives..." < "revision_id".
        "field_provenance": [review_field_provenance_3, review_field_provenance_1, review_field_provenance_2],
    }
    review_package_cases = [
        positive_case("minimal", "minimal", review_package_schema, review_package_minimal, None),
        positive_case("typical", "typical", review_package_schema, review_package_typical, None),
        positive_case("maximal", "maximal", review_package_schema, review_package_maximal, None),
        structural_negative(
            "missing_field",
            review_package_schema,
            {k: v for k, v in review_package_typical.items() if k != "risk_algorithm_version"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field",
            review_package_schema,
            {**review_package_typical, "unexpected_field": "x"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "non_nfc_text",
            review_package_schema,
            {**review_package_typical, "submitted_by_subject": make_non_nfc("author")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            review_package_schema,
            {**review_package_typical, "submitted_by_subject": make_embedded_nul("author-1")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "fractional_number",
            review_package_schema,
            {**review_package_typical, "proposal_version": 1.5},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "equivalent_timezone_offset",
            review_package_schema,
            {**review_package_typical, "submitted_at": "2026-01-06T11:05:00+02:00"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "array_ordering",
            review_package_schema,
            {**review_package_typical, "field_provenance": [review_field_provenance_2, review_field_provenance_1]},
            VALIDATION_FAILED,
        ),
        # Recomputable straight from the sibling `artifact_semantics_v1`
        # fixture -- a real cross-fixture check, not an opaque assertion.
        semantic_negative(
            "digest_substitution",
            review_package_schema,
            {
                **review_package_typical,
                "artifact_semantics_digest": flip_hex(review_package_typical["artifact_semantics_digest"]),
            },
            "arc_activation_predicate_failed",
        ),
    ]
    profiles["approval_review_package_v1"] = ProfileFixture(
        dir_name="approval_review_package_v1",
        literal="arc_approval_review_package_v1",
        schema=review_package_schema,
        cases=review_package_cases,
        signed=None,
    )
    r_real = digest_hex(canonical_bytes(review_package_schema, review_package_typical))

    # === 9. artifact_revision_v1 (the `A` node) ==========================
    revision_schema = PROFILE_SCHEMA(
        "arc_artifact_revision_v1",
        {
            "artifact_id": UUID(),
            "revision_id": UUID(),
            "artifact_semantics_digest": DIGEST(),
            "review_package_digest": DIGEST(),
            "actor_separation_profile": {"type": "string", "const": "arc_actor_separation_v1"},
        },
    )
    revision_typical = {
        "profile": "arc_artifact_revision_v1",
        "artifact_id": ARTIFACT_ID,
        "revision_id": REVISION_ID,
        "artifact_semantics_digest": s_real,
        "review_package_digest": r_real,
        "actor_separation_profile": "arc_actor_separation_v1",
    }
    # No fixture object backs these two identities independently of the
    # typical case above (that is the point -- `typical` is what the rest of
    # this fixture set cross-references as the real `A` node's inputs), so
    # minimal/maximal here vary only identity, not structure: this profile
    # has five required fields and no optional ones.
    revision_minimal = {
        "profile": "arc_artifact_revision_v1",
        "artifact_id": uid(1000),
        "revision_id": uid(1001),
        "artifact_semantics_digest": dig("s-minimal"),
        "review_package_digest": dig("r-minimal"),
        "actor_separation_profile": "arc_actor_separation_v1",
    }
    revision_maximal = {
        "profile": "arc_artifact_revision_v1",
        "artifact_id": uid(2000),
        "revision_id": uid(2001),
        "artifact_semantics_digest": dig("s-maximal"),
        "review_package_digest": dig("r-maximal"),
        "actor_separation_profile": "arc_actor_separation_v1",
    }
    revision_cases = [
        positive_case("minimal", "minimal", revision_schema, revision_minimal, None),
        positive_case("typical", "typical", revision_schema, revision_typical, None),
        positive_case("maximal", "maximal", revision_schema, revision_maximal, None),
        structural_negative(
            "missing_field",
            revision_schema,
            {k: v for k, v in revision_typical.items() if k != "revision_id"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", revision_schema, {**revision_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        # Every field here is a UUID, a digest, or a fixed constant -- there
        # is no free-text field to host a non-NFC-string or embedded-NUL
        # vector, and no number/timestamp/array field either, so those
        # categories are skipped rather than forced onto a field that cannot
        # carry them.
        semantic_negative(
            "digest_substitution",
            revision_schema,
            {**revision_typical, "review_package_digest": flip_hex(revision_typical["review_package_digest"])},
            "arc_activation_predicate_failed",
        ),
    ]
    profiles["artifact_revision_v1"] = ProfileFixture(
        dir_name="artifact_revision_v1",
        literal="arc_artifact_revision_v1",
        schema=revision_schema,
        cases=revision_cases,
        signed=None,
    )

    # === 10. actor_separation_v1 =========================================
    separation_schema = PROFILE_SCHEMA(
        "arc_actor_separation_v1",
        {
            "risk_classification": ENUM(*RISK_CLASSIFICATIONS),
            "submitter_issuer": STR(),
            "submitter_subject": STR(),
            "approver_issuer": STR(),
            "approver_subject": STR(),
            "accepter_issuer": nullable(STR()),
            "accepter_subject": nullable(STR()),
            "activator_issuer": STR(),
            "activator_subject": STR(),
            "required_distinct_count": NUM(),
            "satisfied": BOOL(),
        },
    )
    separation_typical = {
        "profile": "arc_actor_separation_v1",
        "risk_classification": "global_mandatory",
        "submitter_issuer": "https://idp.registry.example/",
        "submitter_subject": "author-1",
        "approver_issuer": "https://idp.registry.example/",
        "approver_subject": "approver-1",
        "accepter_issuer": "https://idp.registry.example/",
        "accepter_subject": "accepter-1",
        "activator_issuer": "https://idp.registry.example/",
        "activator_subject": "activator-1",
        "required_distinct_count": 3,
        "satisfied": True,
    }
    separation_minimal = {
        **separation_typical,
        "risk_classification": "tenant_non_mandatory",
        "accepter_issuer": None,
        "accepter_subject": None,
        "activator_issuer": "https://idp.registry.example/",
        "activator_subject": "approver-1",
        "required_distinct_count": 2,
    }
    separation_maximal = {**separation_typical, "submitter_subject": "author-une-équipe-1"}
    separation_cases = [
        positive_case("minimal", "minimal", separation_schema, separation_minimal, None),
        positive_case("typical", "typical", separation_schema, separation_typical, None),
        positive_case("maximal", "maximal", separation_schema, separation_maximal, None),
        structural_negative(
            "missing_field",
            separation_schema,
            {k: v for k, v in separation_typical.items() if k != "satisfied"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", separation_schema, {**separation_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            separation_schema,
            {**separation_typical, "submitter_subject": make_non_nfc("author")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            separation_schema,
            {**separation_typical, "submitter_subject": make_embedded_nul("author-1")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "fractional_number",
            separation_schema,
            {**separation_typical, "required_distinct_count": 2.5},
            VALIDATION_FAILED,
        ),
        rule_negative(
            "identity_collision",
            separation_schema,
            {
                **separation_typical,
                "approver_issuer": separation_typical["submitter_issuer"],
                "approver_subject": separation_typical["submitter_subject"],
            },
            "arc_activation_predicate_failed",
            check_actor_separation,
        ),
    ]
    profiles["actor_separation_v1"] = ProfileFixture(
        dir_name="actor_separation_v1",
        literal="arc_actor_separation_v1",
        schema=separation_schema,
        cases=separation_cases,
        signed=None,
    )

    # === 11. approval_verifier_enrollment_v1 =============================
    enrollment_schema = PROFILE_SCHEMA(
        "arc_approval_verifier_enrollment_v1",
        {
            "enrollment_challenge_id": UUID(),
            "nonce": STR(),
            "verifier_id": STR(),
            "binding_kind": ENUM("exact_principal", "provider_delegated"),
            "principal_issuer": nullable(STR()),
            "principal_subject": nullable(STR()),
            "provider_allowed_principal_issuer": nullable(STR()),
            "scope_kind": ENUM("global", "tenant"),
            "target_tenant_id": nullable(UUID()),
            "allowed_evidence_types": ARR(ENUM("artifact_activation", "exception_approval"), kind="set", min_items=1),
            "signature_algorithm": ENUM("Ed25519"),
            "key_digest": DIGEST(),
            "valid_from": TS(),
            "valid_to": TS(),
            "issued_at": TS(),
            "expires_at": TS(),
        },
    )
    enrollment_signing = SignInfo(
        domain_prefix=b"ARC-APPROVAL-VERIFIER-ENROLLMENT-V1\x00",
        human_domain="arc.approval_verifier_enrollment.v1",
        sign_over="canonical_bytes",
        key_label="approval_verifier_enrollment_v1",
    )
    enrollment_typical = {
        "profile": "arc_approval_verifier_enrollment_v1",
        "enrollment_challenge_id": uid(50),
        "nonce": "nonce-abc-123",
        "verifier_id": "verifier-approval-1",
        "binding_kind": "exact_principal",
        "principal_issuer": "https://idp.registry.example/",
        "principal_subject": "approver-1",
        "provider_allowed_principal_issuer": None,
        "scope_kind": "tenant",
        "target_tenant_id": TENANT_ID,
        "allowed_evidence_types": ["exception_approval"],
        "signature_algorithm": "Ed25519",
        "key_digest": dig("public-key:approver-1"),
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2027-01-01T00:00:00Z",
        "issued_at": "2026-01-06T10:00:00Z",
        "expires_at": "2026-01-06T10:05:00Z",
    }
    enrollment_minimal = {
        **enrollment_typical,
        "binding_kind": "provider_delegated",
        "principal_issuer": None,
        "principal_subject": None,
        "provider_allowed_principal_issuer": "https://idp.upstream.example/",
        "scope_kind": "global",
        "target_tenant_id": None,
        "expires_at": "2026-01-06T10:00:01Z",
    }
    enrollment_maximal = {
        **enrollment_typical,
        # Deliberately unsorted -- proves set canonicalization sorts it.
        "allowed_evidence_types": ["exception_approval", "artifact_activation"],
        "expires_at": "2026-01-06T10:04:59.999999Z",
    }
    enrollment_cases = [
        positive_case("minimal", "minimal", enrollment_schema, enrollment_minimal, enrollment_signing),
        positive_case("typical", "typical", enrollment_schema, enrollment_typical, enrollment_signing),
        positive_case("maximal", "maximal", enrollment_schema, enrollment_maximal, enrollment_signing),
        structural_negative(
            "missing_field",
            enrollment_schema,
            {k: v for k, v in enrollment_typical.items() if k != "nonce"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", enrollment_schema, {**enrollment_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            enrollment_schema,
            {**enrollment_typical, "verifier_id": make_non_nfc("verifier-approval")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            enrollment_schema,
            {**enrollment_typical, "nonce": make_embedded_nul("nonce-abc-123")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "equivalent_timezone_offset",
            enrollment_schema,
            {**enrollment_typical, "expires_at": "2026-01-06T12:05:00+02:00"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "null_vs_empty", enrollment_schema, {**enrollment_typical, "allowed_evidence_types": []}, VALIDATION_FAILED
        ),
        structural_negative(
            "duplicate_set_entry",
            enrollment_schema,
            {**enrollment_typical, "allowed_evidence_types": ["exception_approval", "exception_approval"]},
            VALIDATION_FAILED,
        ),
        semantic_negative(
            "expiry_equality",
            enrollment_schema,
            {**enrollment_typical, "expires_at": enrollment_typical["issued_at"]},
            "arc_enrollment_verification_failed",
        ),
        semantic_negative(
            "principal_mismatch",
            enrollment_schema,
            {**enrollment_typical, "principal_subject": "someone-else"},
            "arc_enrollment_verification_failed",
            signed=enrollment_signing,
            signature_override=sign_object(enrollment_signing, enrollment_schema, enrollment_typical),
        ),
        semantic_negative(
            "signature_domain_mismatch",
            enrollment_schema,
            enrollment_typical,
            "arc_enrollment_verification_failed",
            signed=enrollment_signing,
            signature_override=_sign_domain_mismatch(
                enrollment_signing,
                canonical_bytes(enrollment_schema, enrollment_typical),
                digest_hex(canonical_bytes(enrollment_schema, enrollment_typical)),
            ),
        ),
        semantic_negative(
            "signature_key_mismatch",
            enrollment_schema,
            enrollment_typical,
            "arc_enrollment_verification_failed",
            signed=enrollment_signing,
            signature_override=sign_object(
                enrollment_signing,
                enrollment_schema,
                enrollment_typical,
                private_key=enrollment_signing.wrong_private_key(),
            ),
        ),
    ]
    profiles["approval_verifier_enrollment_v1"] = ProfileFixture(
        dir_name="approval_verifier_enrollment_v1",
        literal="arc_approval_verifier_enrollment_v1",
        schema=enrollment_schema,
        cases=enrollment_cases,
        signed=enrollment_signing,
    )

    # === 12. approval_provider_assertion_v1 ==============================
    assertion_schema = PROFILE_SCHEMA(
        "arc_approval_provider_assertion_v1",
        {
            "assertion_id": UUID(),
            "provider_id": STR(),
            "provider_configuration_digest": DIGEST(),
            "approval_challenge_id": UUID(),
            "approval_evidence_digest": DIGEST(),
            "principal_issuer": STR(),
            "principal_subject": STR(),
            "issued_at": TS(),
            "expires_at": TS(),
        },
    )
    assertion_signing = SignInfo(
        domain_prefix=b"ARC-APPROVAL-PROVIDER-ASSERTION-V1\x00",
        human_domain="arc.approval_provider_assertion.v1",
        sign_over="canonical_bytes",
        key_label="approval_provider_assertion_v1",
    )
    assertion_typical = {
        "profile": "arc_approval_provider_assertion_v1",
        "assertion_id": uid(60),
        "provider_id": "trusted-approval-provider-1",
        "provider_configuration_digest": dig("provider-config:trusted-approval-provider-1"),
        "approval_challenge_id": uid(61),
        "approval_evidence_digest": dig("approval-evidence:proposal-1-v1"),
        "principal_issuer": "https://idp.registry.example/",
        "principal_subject": "approver-1",
        "issued_at": "2026-01-06T11:00:00Z",
        "expires_at": "2026-01-06T11:05:00Z",
    }
    assertion_minimal = {**assertion_typical, "provider_id": "p", "expires_at": "2026-01-06T11:00:01Z"}
    assertion_maximal = {
        **assertion_typical,
        "provider_id": "trusted-approval-provider-1-régionale",
        "expires_at": "2026-01-06T11:04:59.5Z",
    }
    assertion_cases = [
        positive_case("minimal", "minimal", assertion_schema, assertion_minimal, assertion_signing),
        positive_case("typical", "typical", assertion_schema, assertion_typical, assertion_signing),
        positive_case("maximal", "maximal", assertion_schema, assertion_maximal, assertion_signing),
        structural_negative(
            "missing_field",
            assertion_schema,
            {k: v for k, v in assertion_typical.items() if k != "provider_id"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", assertion_schema, {**assertion_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            assertion_schema,
            {**assertion_typical, "provider_id": make_non_nfc("trusted-provider")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            assertion_schema,
            {**assertion_typical, "provider_id": make_embedded_nul("trusted-provider")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "equivalent_timezone_offset",
            assertion_schema,
            {**assertion_typical, "expires_at": "2026-01-06T13:05:00+02:00"},
            VALIDATION_FAILED,
        ),
        semantic_negative(
            "expiry_equality",
            assertion_schema,
            {**assertion_typical, "expires_at": assertion_typical["issued_at"]},
            "arc_approval_verification_failed",
        ),
        semantic_negative(
            "principal_mismatch",
            assertion_schema,
            {**assertion_typical, "principal_subject": "someone-else"},
            "arc_approval_verification_failed",
            signed=assertion_signing,
            signature_override=sign_object(assertion_signing, assertion_schema, assertion_typical),
        ),
        semantic_negative(
            "signature_domain_mismatch",
            assertion_schema,
            assertion_typical,
            "arc_approval_verification_failed",
            signed=assertion_signing,
            signature_override=_sign_domain_mismatch(
                assertion_signing,
                canonical_bytes(assertion_schema, assertion_typical),
                digest_hex(canonical_bytes(assertion_schema, assertion_typical)),
            ),
        ),
        semantic_negative(
            "signature_key_mismatch",
            assertion_schema,
            assertion_typical,
            "arc_approval_verification_failed",
            signed=assertion_signing,
            signature_override=sign_object(
                assertion_signing,
                assertion_schema,
                assertion_typical,
                private_key=assertion_signing.wrong_private_key(),
            ),
        ),
    ]
    profiles["approval_provider_assertion_v1"] = ProfileFixture(
        dir_name="approval_provider_assertion_v1",
        literal="arc_approval_provider_assertion_v1",
        schema=assertion_schema,
        cases=assertion_cases,
        signed=assertion_signing,
    )

    # === 13. operational_event_v1 ========================================
    # Signs the raw digest bytes, not the canonical object -- the ADR text
    # for this one profile says so explicitly, unlike every other signed
    # profile in this set, which is why `SignInfo.sign_over` exists.
    event_payload_schema = OBJ(
        {
            "initial_freshness_basis": nullable(ENUM("connector_verified", "revision_pinned_only")),
            "retention_floor_days": nullable(NUM()),
            "legal_hold_active": nullable(BOOL()),
            "artifact_semantics_digest": nullable(DIGEST()),
            "hold_id": nullable(UUID()),
            "reason_code": nullable(STR()),
            "authority_evidence_digest": nullable(DIGEST()),
            "placed_at": nullable(TS()),
            "released_at": nullable(TS()),
            "prior_deadline": nullable(TS()),
            "later_deadline": nullable(TS()),
        },
        [
            "initial_freshness_basis",
            "retention_floor_days",
            "legal_hold_active",
            "artifact_semantics_digest",
            "hold_id",
            "reason_code",
            "authority_evidence_digest",
            "placed_at",
            "released_at",
            "prior_deadline",
            "later_deadline",
        ],
    )
    event_schema = PROFILE_SCHEMA(
        "arc_operational_event_v1",
        {
            "event_id": UUID(),
            "artifact_id": UUID(),
            "revision_id": UUID(),
            "sequence": NUM(),
            "event_type": ENUM(
                "operational_state_initialized",
                "freshness_downgraded",
                "legal_hold_placed",
                "legal_hold_released",
                "retention_extended",
            ),
            "event_payload": event_payload_schema,
            "actor_issuer": STR(),
            "actor_subject": STR(),
            "actor_role": ENUM("system", "human"),
            "authorization_decision_reference": STR(),
            "authority_evidence_digest": DIGEST(),
            "idempotency_key_digest": DIGEST(),
            "previous_event_digest": nullable(DIGEST()),
            "signer_key_id": STR(),
            "created_at": TS(),
        },
    )
    event_signing = SignInfo(
        domain_prefix=b"ARC-OPERATIONAL-EVENT-V1\x00",
        human_domain="arc.operational_event.v1",
        sign_over="digest",
        key_label="operational_event_v1",
    )
    _empty_payload = {
        "initial_freshness_basis": None,
        "retention_floor_days": None,
        "legal_hold_active": None,
        "artifact_semantics_digest": None,
        "hold_id": None,
        "reason_code": None,
        "authority_evidence_digest": None,
        "placed_at": None,
        "released_at": None,
        "prior_deadline": None,
        "later_deadline": None,
    }
    event_0 = {
        "profile": "arc_operational_event_v1",
        "event_id": uid(70),
        "artifact_id": ARTIFACT_ID,
        "revision_id": REVISION_ID,
        "sequence": 0,
        "event_type": "operational_state_initialized",
        "event_payload": {
            **_empty_payload,
            "initial_freshness_basis": "connector_verified",
            "retention_floor_days": 400,
            "legal_hold_active": False,
            "artifact_semantics_digest": s_real,
        },
        "actor_issuer": "registry://deployment",
        "actor_subject": "arc-operational-state",
        "actor_role": "system",
        "authorization_decision_reference": "materialisation:proposal-1-v1",
        "authority_evidence_digest": s_real,
        "idempotency_key_digest": dig("idempotency:event:revision-2:seq-0"),
        "previous_event_digest": None,
        "signer_key_id": "arc-operational-event-signing-1",
        "created_at": "2026-01-06T09:10:00Z",
    }
    event_0_digest = digest_hex(canonical_bytes(event_schema, event_0))
    event_1 = {
        "profile": "arc_operational_event_v1",
        "event_id": uid(71),
        "artifact_id": ARTIFACT_ID,
        "revision_id": REVISION_ID,
        "sequence": 1,
        "event_type": "freshness_downgraded",
        "event_payload": {
            **_empty_payload,
            "initial_freshness_basis": "revision_pinned_only",
            "reason_code": "connector_evidence_unavailable",
            "authority_evidence_digest": dig("status-evidence:connector-timeout"),
        },
        "actor_issuer": "registry://deployment",
        "actor_subject": "arc-operational-state",
        "actor_role": "system",
        "authorization_decision_reference": "source-status:connector-timeout",
        "authority_evidence_digest": dig("status-evidence:connector-timeout"),
        "idempotency_key_digest": dig("idempotency:event:revision-2:seq-1"),
        "previous_event_digest": event_0_digest,
        "signer_key_id": "arc-operational-event-signing-1",
        "created_at": "2026-02-01T00:00:00Z",
    }
    event_1_digest = digest_hex(canonical_bytes(event_schema, event_1))
    event_2 = {
        "profile": "arc_operational_event_v1",
        "event_id": uid(72),
        "artifact_id": ARTIFACT_ID,
        "revision_id": REVISION_ID,
        "sequence": 2,
        "event_type": "legal_hold_placed",
        "event_payload": {
            **_empty_payload,
            "legal_hold_active": True,
            "hold_id": uid(73),
            "reason_code": "litigation_hold",
            "authority_evidence_digest": dig("hold-authority:litigation-hold-1"),
            "placed_at": "2026-03-01T00:00:00Z",
        },
        "actor_issuer": "https://idp.registry.example/",
        "actor_subject": "operator-1",
        "actor_role": "human",
        "authorization_decision_reference": "legal-hold:litigation-hold-1",
        "authority_evidence_digest": dig("hold-authority:litigation-hold-1"),
        "idempotency_key_digest": dig("idempotency:event:revision-2:seq-2"),
        "previous_event_digest": event_1_digest,
        "signer_key_id": "arc-operational-event-signing-1",
        "created_at": "2026-03-01T00:00:05Z",
    }
    event_cases = [
        # `minimal`/`typical`/`maximal` are the real chain -- genesis, a
        # system-authored transition, and a human-authored one -- rather than
        # three unrelated snapshots. `previous_event_digest` on the latter
        # two is a real digest of the fixture immediately before it.
        positive_case("minimal", "minimal", event_schema, event_0, event_signing),
        positive_case("typical", "typical", event_schema, event_1, event_signing),
        positive_case("maximal", "maximal", event_schema, event_2, event_signing),
        structural_negative(
            "missing_field", event_schema, {k: v for k, v in event_1.items() if k != "actor_role"}, VALIDATION_FAILED
        ),
        structural_negative("unknown_field", event_schema, {**event_1, "unexpected_field": "x"}, VALIDATION_FAILED),
        structural_negative(
            "non_nfc_text",
            event_schema,
            {**event_1, "authorization_decision_reference": make_non_nfc("source-status")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            event_schema,
            {**event_1, "signer_key_id": make_embedded_nul("arc-operational-event-signing-1")},
            VALIDATION_FAILED,
        ),
        structural_negative("fractional_number", event_schema, {**event_1, "sequence": 1.5}, VALIDATION_FAILED),
        structural_negative(
            "equivalent_timezone_offset",
            event_schema,
            {**event_1, "created_at": "2026-02-01T02:00:00+02:00"},
            VALIDATION_FAILED,
        ),
        # Breaks the real chain link to `minimal` (sequence 0) -- an
        # independent verifier that recomputes that fixture's digest and
        # compares will catch this without trusting either side on faith.
        semantic_negative(
            "digest_substitution",
            event_schema,
            {**event_1, "previous_event_digest": flip_hex(event_1["previous_event_digest"])},
            "arc_operational_integrity_failed",
            signed=event_signing,
            signature_override=sign_object(event_signing, event_schema, event_1),
        ),
        semantic_negative(
            "principal_mismatch",
            event_schema,
            {**event_1, "actor_subject": "someone-else"},
            "arc_operational_integrity_failed",
            signed=event_signing,
            signature_override=sign_object(event_signing, event_schema, event_1),
        ),
        semantic_negative(
            "signature_domain_mismatch",
            event_schema,
            event_1,
            "arc_operational_integrity_failed",
            signed=event_signing,
            signature_override=_sign_domain_mismatch(
                event_signing,
                canonical_bytes(event_schema, event_1),
                digest_hex(canonical_bytes(event_schema, event_1)),
            ),
        ),
        semantic_negative(
            "signature_key_mismatch",
            event_schema,
            event_1,
            "arc_operational_integrity_failed",
            signed=event_signing,
            signature_override=sign_object(
                event_signing, event_schema, event_1, private_key=event_signing.wrong_private_key()
            ),
        ),
    ]
    profiles["operational_event_v1"] = ProfileFixture(
        dir_name="operational_event_v1",
        literal="arc_operational_event_v1",
        schema=event_schema,
        cases=event_cases,
        signed=event_signing,
    )

    # === 14. observation_cohort_v1 ========================================
    cohort_schema = PROFILE_SCHEMA(
        "arc_observation_cohort_v1",
        {
            "cohort_id": UUID(),
            "risk_classification": ENUM(*RISK_CLASSIFICATIONS),
            "scope_predicate_digest": DIGEST(),
            "tenant_membership_digest": DIGEST(),
            "eligibility_predicate_digest": DIGEST(),
            "frozen_at": TS(),
            "window_started_at": TS(),
            "window_deadline": TS(),
        },
    )
    cohort_typical = {
        "profile": "arc_observation_cohort_v1",
        "cohort_id": uid(80),
        "risk_classification": "tenant_mandatory",
        "scope_predicate_digest": dig("scope-predicate:proposal-1-v1"),
        "tenant_membership_digest": dig("tenant-membership:proposal-1-v1"),
        "eligibility_predicate_digest": dig("eligibility-predicate:proposal-1-v1"),
        "frozen_at": "2026-01-06T09:06:00Z",
        "window_started_at": "2026-01-06T09:06:00Z",
        "window_deadline": "2026-01-09T09:06:00Z",
    }
    cohort_minimal = {
        **cohort_typical,
        "risk_classification": "global_non_mandatory",
        "window_deadline": "2026-01-09T09:06:00Z",
    }
    cohort_maximal = {
        **cohort_typical,
        "risk_classification": "global_mandatory",
        "window_deadline": "2026-01-13T09:06:00Z",
    }
    cohort_cases = [
        positive_case("minimal", "minimal", cohort_schema, cohort_minimal, None),
        positive_case("typical", "typical", cohort_schema, cohort_typical, None),
        positive_case("maximal", "maximal", cohort_schema, cohort_maximal, None),
        structural_negative(
            "missing_field",
            cohort_schema,
            {k: v for k, v in cohort_typical.items() if k != "frozen_at"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", cohort_schema, {**cohort_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        # Every field is a UUID, an enum, a digest, or a timestamp -- no
        # plain free-text field exists to host a non-NFC/embedded-NUL
        # vector, and no numeric field exists either.
        structural_negative(
            "equivalent_timezone_offset",
            cohort_schema,
            {**cohort_typical, "window_deadline": "2026-01-09T11:06:00+02:00"},
            VALIDATION_FAILED,
        ),
        semantic_negative(
            "expiry_equality",
            cohort_schema,
            {**cohort_typical, "window_deadline": cohort_typical["window_started_at"]},
            "arc_observation_insufficient",
        ),
    ]
    profiles["observation_cohort_v1"] = ProfileFixture(
        dir_name="observation_cohort_v1",
        literal="arc_observation_cohort_v1",
        schema=cohort_schema,
        cases=cohort_cases,
        signed=None,
    )
    cohort_real = digest_hex(canonical_bytes(cohort_schema, cohort_typical))

    # === 15. observation_qualification_v1 ================================
    delta_counter_schema = OBJ(
        {"delta_code": ENUM(*DELTA_CODES), "explained_count": NUM(), "unexplained_count": NUM()},
        ["delta_code", "explained_count", "unexplained_count"],
    )
    qualification_schema = PROFILE_SCHEMA(
        "arc_observation_qualification_v1",
        {
            "qualification_id": UUID(),
            "idempotency_key_digest": DIGEST(),
            "candidate_review_package_digest": DIGEST(),
            "candidate_revision_id": UUID(),
            "proposal_id": UUID(),
            "proposal_version": NUM(),
            "risk_classification": ENUM(*RISK_CLASSIFICATIONS),
            "risk_algorithm_version": STR(),
            "baseline_revision_id": nullable(UUID()),
            "selection_engine_version": STR(),
            "engine_configuration_version": STR(),
            "cohort_id": UUID(),
            "cohort_digest": DIGEST(),
            "window_started_at": TS(),
            "window_ended_at": TS(),
            "eligible_count": NUM(),
            "observed_count": NUM(),
            "expected_impact_envelope_digest": DIGEST(),
            "counters_by_delta_code": ARR(delta_counter_schema, kind="ordered", order_key="delta_code"),
            "unexplained_count": NUM(),
            "out_of_envelope_count": NUM(),
            "replay_corpus_digest": nullable(DIGEST()),
            "replay_result_digest": nullable(DIGEST()),
            "qualification_algorithm_version": STR(),
            "computed_decision": ENUM("qualified", "qualified_low_traffic", "insufficient", "failed"),
            "reason_codes": ARR(STR(), kind="ordered"),
            "accepted_by_issuer": nullable(STR()),
            "accepted_by_subject": nullable(STR()),
            "accepted_by_role": nullable(STR()),
            "accepted_at": nullable(TS()),
            "acceptance_audit_reference": nullable(STR()),
            "expires_at": nullable(TS()),
        },
    )
    qualification_signing = SignInfo(
        domain_prefix=b"ARC-OBSERVATION-QUALIFICATION-V1\x00",
        human_domain="arc.observation_qualification.v1",
        sign_over="digest",
        key_label="observation_qualification_v1",
    )
    qualification_typical = {
        "profile": "arc_observation_qualification_v1",
        "qualification_id": uid(90),
        "idempotency_key_digest": dig("idempotency:qualification:proposal-1-v1"),
        "candidate_review_package_digest": r_real,
        "candidate_revision_id": REVISION_ID,
        "proposal_id": PROPOSAL_ID,
        "proposal_version": 1,
        "risk_classification": "tenant_mandatory",
        "risk_algorithm_version": "arc_risk_classification_v1.0",
        "baseline_revision_id": None,
        "selection_engine_version": "arc_selection_v1.4",
        "engine_configuration_version": "2026-01-01",
        "cohort_id": cohort_typical["cohort_id"],
        "cohort_digest": cohort_real,
        "window_started_at": "2026-01-06T09:06:00Z",
        "window_ended_at": "2026-01-07T09:10:00Z",
        "eligible_count": 120,
        "observed_count": 120,
        "expected_impact_envelope_digest": envelope_digest_real,
        "counters_by_delta_code": [
            {"delta_code": "conflict_changed", "explained_count": 4, "unexplained_count": 0},
            {"delta_code": "newly_selected", "explained_count": 8, "unexplained_count": 0},
        ],
        "unexplained_count": 0,
        "out_of_envelope_count": 0,
        "replay_corpus_digest": None,
        "replay_result_digest": None,
        "qualification_algorithm_version": "arc_observation_qualification_v1",
        "computed_decision": "qualified",
        "reason_codes": ["window_met", "coverage_complete"],
        "accepted_by_issuer": "https://idp.registry.example/",
        "accepted_by_subject": "activator-1",
        "accepted_by_role": "activator",
        "accepted_at": "2026-01-07T09:15:00Z",
        "acceptance_audit_reference": "audit:qualification:proposal-1-v1",
        "expires_at": "2026-01-08T09:15:00Z",
    }
    qualification_minimal = {
        **qualification_typical,
        "risk_classification": "task_non_mandatory",
        "counters_by_delta_code": [],
        "computed_decision": "insufficient",
        "reason_codes": ["window_not_met"],
        "eligible_count": 40,
        "observed_count": 40,
        "accepted_by_issuer": None,
        "accepted_by_subject": None,
        "accepted_by_role": None,
        "accepted_at": None,
        "acceptance_audit_reference": None,
        "expires_at": None,
    }
    qualification_maximal = {
        **qualification_typical,
        "baseline_revision_id": uid(4),
        "replay_corpus_digest": dig("replay-corpus:proposal-1-v1"),
        "replay_result_digest": dig("replay-result:proposal-1-v1"),
        "computed_decision": "qualified_low_traffic",
        "reason_codes": ["window_met", "low_traffic_replay_accepted"],
    }
    qualification_cases = [
        positive_case("minimal", "minimal", qualification_schema, qualification_minimal, qualification_signing),
        positive_case("typical", "typical", qualification_schema, qualification_typical, qualification_signing),
        positive_case("maximal", "maximal", qualification_schema, qualification_maximal, qualification_signing),
        structural_negative(
            "missing_field",
            qualification_schema,
            {k: v for k, v in qualification_typical.items() if k != "risk_algorithm_version"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", qualification_schema, {**qualification_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            qualification_schema,
            {**qualification_typical, "acceptance_audit_reference": make_non_nfc("audit")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            qualification_schema,
            {**qualification_typical, "acceptance_audit_reference": make_embedded_nul("audit:qualification")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "fractional_number",
            qualification_schema,
            {**qualification_typical, "proposal_version": 1.5},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "equivalent_timezone_offset",
            qualification_schema,
            {**qualification_typical, "window_ended_at": "2026-01-07T11:10:00+02:00"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "array_ordering",
            qualification_schema,
            {
                **qualification_typical,
                "counters_by_delta_code": list(reversed(qualification_typical["counters_by_delta_code"])),
            },
            VALIDATION_FAILED,
        ),
        # Recomputable from the sibling `approval_review_package_v1`
        # fixture's own real digest.
        semantic_negative(
            "digest_substitution",
            qualification_schema,
            {
                **qualification_typical,
                "candidate_review_package_digest": flip_hex(qualification_typical["candidate_review_package_digest"]),
            },
            "arc_activation_predicate_failed",
            signed=qualification_signing,
            signature_override=sign_object(qualification_signing, qualification_schema, qualification_typical),
        ),
        semantic_negative(
            "expiry_equality",
            qualification_schema,
            {**qualification_typical, "expires_at": qualification_typical["accepted_at"]},
            "arc_qualification_expired",
            signed=qualification_signing,
            signature_override=sign_object(qualification_signing, qualification_schema, qualification_typical),
        ),
        semantic_negative(
            "principal_mismatch",
            qualification_schema,
            {**qualification_typical, "accepted_by_subject": "someone-else"},
            "arc_qualification_actor_invalid",
            signed=qualification_signing,
            signature_override=sign_object(qualification_signing, qualification_schema, qualification_typical),
        ),
        semantic_negative(
            "signature_domain_mismatch",
            qualification_schema,
            qualification_typical,
            "arc_activation_predicate_failed",
            signed=qualification_signing,
            signature_override=_sign_domain_mismatch(
                qualification_signing,
                canonical_bytes(qualification_schema, qualification_typical),
                digest_hex(canonical_bytes(qualification_schema, qualification_typical)),
            ),
        ),
        semantic_negative(
            "signature_key_mismatch",
            qualification_schema,
            qualification_typical,
            "arc_activation_predicate_failed",
            signed=qualification_signing,
            signature_override=sign_object(
                qualification_signing,
                qualification_schema,
                qualification_typical,
                private_key=qualification_signing.wrong_private_key(),
            ),
        ),
    ]
    profiles["observation_qualification_v1"] = ProfileFixture(
        dir_name="observation_qualification_v1",
        literal="arc_observation_qualification_v1",
        schema=qualification_schema,
        cases=qualification_cases,
        signed=qualification_signing,
    )

    # === 16. observation_replay_corpus_v1 ================================
    corpus_schema = PROFILE_SCHEMA(
        "arc_observation_replay_corpus_v1",
        {
            "corpus_id": UUID(),
            "generator_version": STR(),
            "generator_input_digest": DIGEST(),
            "canonical_corpus_digest": DIGEST(),
            "fixture_class_count": NUM(),
            "scope": ENUM("global", "tenant"),
            "target_tenant_id": nullable(UUID()),
            "approving_authority_issuer": STR(),
            "approving_authority_subject": STR(),
            "approved_at": TS(),
            "expires_at": TS(),
        },
    )
    corpus_typical = {
        "profile": "arc_observation_replay_corpus_v1",
        "corpus_id": uid(100),
        "generator_version": "arc_replay_corpus_generator_v1.2",
        "generator_input_digest": dig("replay-generator-input:proposal-1-v1"),
        "canonical_corpus_digest": dig("replay-corpus:proposal-1-v1"),
        "fixture_class_count": 128,
        "scope": "tenant",
        "target_tenant_id": TENANT_ID,
        "approving_authority_issuer": "https://idp.registry.example/",
        "approving_authority_subject": "tenant-admin-1",
        "approved_at": "2026-01-08T00:00:00Z",
        "expires_at": "2026-07-08T00:00:00Z",
    }
    corpus_minimal = {**corpus_typical, "scope": "global", "target_tenant_id": None, "fixture_class_count": 100}
    corpus_maximal = {
        **corpus_typical,
        "generator_version": "arc_replay_corpus_generator_v1.2-régionale",
        "fixture_class_count": 512,
    }
    corpus_cases = [
        positive_case("minimal", "minimal", corpus_schema, corpus_minimal, None),
        positive_case("typical", "typical", corpus_schema, corpus_typical, None),
        positive_case("maximal", "maximal", corpus_schema, corpus_maximal, None),
        structural_negative(
            "missing_field",
            corpus_schema,
            {k: v for k, v in corpus_typical.items() if k != "generator_version"},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "unknown_field", corpus_schema, {**corpus_typical, "unexpected_field": "x"}, VALIDATION_FAILED
        ),
        structural_negative(
            "non_nfc_text",
            corpus_schema,
            {**corpus_typical, "generator_version": make_non_nfc("arc_replay_corpus_generator")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "embedded_nul",
            corpus_schema,
            {**corpus_typical, "approving_authority_subject": make_embedded_nul("tenant-admin-1")},
            VALIDATION_FAILED,
        ),
        structural_negative(
            "fractional_number", corpus_schema, {**corpus_typical, "fixture_class_count": 100.5}, VALIDATION_FAILED
        ),
        structural_negative(
            "equivalent_timezone_offset",
            corpus_schema,
            {**corpus_typical, "expires_at": "2026-07-08T02:00:00+02:00"},
            VALIDATION_FAILED,
        ),
        semantic_negative(
            "expiry_equality",
            corpus_schema,
            {**corpus_typical, "expires_at": corpus_typical["approved_at"]},
            "arc_observation_insufficient",
        ),
        # Unsigned, and no fixture in this set backs either digest field, so
        # `digest_substitution` and `principal_mismatch` are both skipped:
        # nothing here could independently tell a tampered value from a
        # correct one.
    ]
    profiles["observation_replay_corpus_v1"] = ProfileFixture(
        dir_name="observation_replay_corpus_v1",
        literal="arc_observation_replay_corpus_v1",
        schema=corpus_schema,
        cases=corpus_cases,
        signed=None,
    )

    # -------------------------------------------------------------------
    # Cross-profile pass: one `profile_confusion` negative per profile,
    # using the next profile's own typical object as the donor. Deferred to
    # here because it needs every profile's schema and typical case to
    # already exist.
    # -------------------------------------------------------------------
    order = list(profiles)
    for index, key in enumerate(order):
        donor_key = order[(index + 1) % len(order)]
        donor_typical = next(c for c in profiles[donor_key].cases if c.case_id == "typical")
        profiles[key].cases.append(
            confusion_case("profile_confusion", profiles[key].schema, donor_typical.obj, VALIDATION_FAILED)
        )

    for key, fixture in profiles.items():
        expected_literal = f"arc_{fixture.dir_name}"
        if fixture.literal != expected_literal or fixture.dir_name != key:
            raise AssertionError(f"{key}: directory name does not derive from the profile literal by stripping 'arc_'")

    return profiles


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _b64(payload: bytes | None) -> str | None:
    return base64.b64encode(payload).decode("ascii") if payload is not None else None


def _expected_block(case: Case, signed: SignInfo | None) -> dict[str, Any]:
    signing_domain = signed.human_domain if signed is not None and case.signature_input is not None else None
    return {
        "canonical_bytes_base64": _b64(case.canonical),
        "digest": case.digest,
        "signing_domain": signing_domain,
        "signature_input_base64": _b64(case.signature_input),
        "signature_base64": _b64(case.signature),
        "decision": case.decision,
        "refusal_code": case.refusal_code,
    }


def main() -> None:
    profiles = build_profiles()

    manifest_profiles: list[dict[str, Any]] = []
    keys: dict[str, Any] = {}

    for dir_name in sorted(profiles, key=lambda k: profiles[k].literal):
        fixture = profiles[dir_name]
        profile_dir = FIXTURE_ROOT / fixture.dir_name
        _write_json(profile_dir / "schema.json", fixture.schema)

        if fixture.signed is not None:
            keys[fixture.literal] = {
                "algorithm": "Ed25519",
                "domain_prefix_hex": fixture.signed.domain_prefix.hex(),
                "signing_domain": fixture.signed.human_domain,
                "sign_over": fixture.signed.sign_over,
                "public_key_base64": _b64(_public_bytes(fixture.signed.primary_private_key())),
            }

        case_entries: list[dict[str, Any]] = []
        for case in sorted(fixture.cases, key=lambda c: c.case_id):
            sub_dir = "positive" if case.decision == "accept" else "negative"
            relative_input = f"{fixture.dir_name}/{sub_dir}/{case.case_id}.json"
            _write_json(FIXTURE_ROOT / relative_input, case.obj)
            case_entries.append(
                {
                    "case_id": case.case_id,
                    "kind": case.kind,
                    "input_path": relative_input,
                    "expected": _expected_block(case, fixture.signed),
                }
            )

        manifest_profiles.append(
            {
                "profile": fixture.literal,
                "schema_path": f"{fixture.dir_name}/schema.json",
                "cases": case_entries,
            }
        )

    manifest = {"manifest_version": MANIFEST_VERSION, "profiles": manifest_profiles}
    _write_json(FIXTURE_ROOT / "manifest.json", manifest)
    _write_json(FIXTURE_ROOT / "keys.json", keys)

    total_cases = sum(len(p["cases"]) for p in manifest_profiles)
    print(f"wrote {len(manifest_profiles)} profiles, {total_cases} cases, to {FIXTURE_ROOT}")


if __name__ == "__main__":
    main()
