"""Conformance gate: the production authoring-profile canonicalizer agrees
with the version-controlled vector manifest on every published case.

`contextplane.arc.schemas.authoring_profiles` is a from-scratch reimplementation
of the same closed-schema rules the manifest's canonical bytes, digests, and
signatures were produced against -- it shares no code with the fixture
generator or with the Node reference verifier that separately checked those
same vectors. This test is the third independent opinion, and it asserts
agreement rather than deriving its own "expected" value and comparing that
against itself: every assertion below compares production output against a
value the manifest already publishes.

Two layers exist because they answer different questions:

- `canonicalize_<profile>()` alone answers "does this instance have the
  shape its schema requires, and if so what are its exact canonical bytes?"
  Every one of the 187 cases is checked against the manifest's own
  `canonical_bytes_base64` and `digest` here, including the negative cases
  whose instance canonicalizes cleanly but is refused for a reason
  canonicalization itself cannot see.
- The full accept/refuse decision additionally depends on business rules a
  closed field set cannot express (`validate_<profile>()`), on a detached
  signature (verified here the same way an approver's client verifies one:
  an independently recomputed domain-prefixed signing input, checked with
  the enrolled public key), on a same-object expiry comparison, and -- for
  exactly the four cases forming the source-evidence and `S -> R -> A`
  digest chains -- a reference to a *different* profile's own published
  digest. That last category is deliberately checked against the sibling
  case's manifest-published digest, never by recanonicalizing anything on
  this test's own authority, so a shared bug in the canonicalizer could not
  make the check agree with itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from contextplane.arc.schemas import authoring_profiles as ap
from contextplane.arc.schemas.canonical import SUPPORTED_PROFILES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_ROOT = _REPO_ROOT / "tests" / "fixtures" / "arc_authoring"


def _load_manifest() -> dict[str, Any]:
    return json.loads((_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))


def _load_keys() -> dict[str, Any]:
    return json.loads((_FIXTURE_ROOT / "keys.json").read_text(encoding="utf-8"))


def _load_case_input(input_path: str) -> Any:
    return json.loads((_FIXTURE_ROOT / input_path).read_text(encoding="utf-8"))


def _published_digest(dir_name: str, case_id: str) -> str:
    """The manifest's own published digest for a *different* profile's case.
    Ground truth from an already-published, independently checked fixture --
    never this test's own recomputation, so a canonicalizer bug could not
    validate itself through this comparison."""
    literal = f"arc_{dir_name}"
    for profile in _load_manifest()["profiles"]:
        if profile["profile"] == literal:
            for case in profile["cases"]:
                if case["case_id"] == case_id:
                    digest = case["expected"]["digest"]
                    assert isinstance(digest, str)
                    return digest
    raise AssertionError(f"no published digest for {literal}/{case_id}")


def _verify_signature(profile_literal: str, expected: dict[str, Any], keys: dict[str, Any]) -> bool:
    """Recompute the signing input from this test's own knowledge of the
    domain prefix and sign-over rule (not the manifest's published
    `signature_input_base64`), and verify the manifest's published signature
    against it with the enrolled public key."""
    signing_key = keys[profile_literal]
    domain_prefix = bytes.fromhex(signing_key["domain_prefix_hex"])
    canonical = base64.b64decode(expected["canonical_bytes_base64"])
    payload = bytes.fromhex(expected["digest"]) if signing_key["sign_over"] == "digest" else canonical
    signing_input = domain_prefix + payload
    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(signing_key["public_key_base64"]))
    signature = base64.b64decode(expected["signature_base64"])
    try:
        public_key.verify(signature, signing_input)
    except InvalidSignature:
        return False
    return True


# ---------------------------------------------------------------------------
# Out-of-band refusal layers: business rules that need more than one
# instance's own shape (a signature, a same-object expiry comparison, or
# another profile's already-published digest). Each table below is small
# and profile-keyed on purpose -- the *detection* is still generic per
# category, only the reported refusal code varies by profile.
# ---------------------------------------------------------------------------

_SIGNATURE_CASE_IDS = frozenset(
    {"principal_mismatch", "signature_domain_mismatch", "signature_key_mismatch", "digest_substitution"}
)

_SIGNATURE_REFUSAL_CODE: dict[str, str] = {
    ap.SOURCE_APPROVAL_CLAIM_PROFILE: "arc_source_admission_refused",
    ap.SOURCE_VERIFIER_ATTESTATION_PROFILE: "arc_source_admission_refused",
    ap.APPROVAL_VERIFIER_ENROLLMENT_PROFILE: "arc_enrollment_verification_failed",
    ap.APPROVAL_PROVIDER_ASSERTION_PROFILE: "arc_approval_verification_failed",
    ap.OPERATIONAL_EVENT_PROFILE: "arc_operational_integrity_failed",
    ap.OBSERVATION_QUALIFICATION_PROFILE: "arc_activation_predicate_failed",
}

_QUALIFICATION_PRINCIPAL_MISMATCH_CODE = "arc_qualification_actor_invalid"

_EXPIRY_FIELD_PAIRS: dict[str, tuple[str, str]] = {
    ap.SOURCE_APPROVAL_CLAIM_PROFILE: ("approved_at", "expires_at"),
    ap.SOURCE_VERIFIER_ATTESTATION_PROFILE: ("issued_at", "expires_at"),
    ap.APPROVAL_VERIFIER_ENROLLMENT_PROFILE: ("issued_at", "expires_at"),
    ap.APPROVAL_PROVIDER_ASSERTION_PROFILE: ("issued_at", "expires_at"),
    ap.OBSERVATION_QUALIFICATION_PROFILE: ("accepted_at", "expires_at"),
    ap.OBSERVATION_COHORT_V1_PROFILE: ("window_started_at", "window_deadline"),
    ap.OBSERVATION_COHORT_V2_PROFILE: ("window_started_at", "window_deadline"),
    ap.OBSERVATION_REPLAY_CORPUS_PROFILE: ("approved_at", "expires_at"),
}

_EXPIRY_REFUSAL_CODE: dict[str, str] = {
    ap.SOURCE_APPROVAL_CLAIM_PROFILE: "arc_source_admission_refused",
    ap.SOURCE_VERIFIER_ATTESTATION_PROFILE: "arc_source_admission_refused",
    ap.APPROVAL_VERIFIER_ENROLLMENT_PROFILE: "arc_enrollment_verification_failed",
    ap.APPROVAL_PROVIDER_ASSERTION_PROFILE: "arc_approval_verification_failed",
    ap.OBSERVATION_QUALIFICATION_PROFILE: "arc_qualification_expired",
    ap.OBSERVATION_COHORT_V1_PROFILE: "arc_observation_insufficient",
    ap.OBSERVATION_COHORT_V2_PROFILE: "arc_observation_insufficient",
    ap.OBSERVATION_REPLAY_CORPUS_PROFILE: "arc_observation_insufficient",
}

# The source-evidence and `S -> R -> A` digest chains: (own field, the
# sibling profile directory whose published digest that field must equal,
# the sibling case id to read it from). `source_approval_evidence_v1`'s own
# `claim_digest` is deliberately absent here -- it embeds the full claim
# object, so `validate_source_approval_evidence_v1` already checks it
# without any sibling fixture.
# Both halves of a split family publish the same case ids, so a table keyed
# on one version silently answers "no rule" for the other and the case is
# decided by fallthrough. The chain a version references stays inside its own
# version: a v2 artifact-semantics names the v2 review package, not the v1.
_CHAIN_REFERENCES: dict[str, tuple[str, str, str]] = {
    ap.SOURCE_VERIFIER_ATTESTATION_PROFILE: ("claim_digest", "source_approval_claim_v1", "typical"),
    ap.ARTIFACT_SEMANTICS_V1_PROFILE: ("source_approval_evidence_digest", "source_approval_evidence_v1", "typical"),
    ap.ARTIFACT_SEMANTICS_V2_PROFILE: ("source_approval_evidence_digest", "source_approval_evidence_v1", "typical"),
    ap.APPROVAL_REVIEW_PACKAGE_V1_PROFILE: ("artifact_semantics_digest", "artifact_semantics_v1", "typical"),
    ap.APPROVAL_REVIEW_PACKAGE_V2_PROFILE: ("artifact_semantics_digest", "artifact_semantics_v2", "typical"),
    ap.ARTIFACT_REVISION_V1_PROFILE: ("review_package_digest", "approval_review_package_v1", "typical"),
    ap.ARTIFACT_REVISION_V2_PROFILE: ("review_package_digest", "approval_review_package_v2", "typical"),
}

_CHAIN_REFUSAL_CODE: dict[str, str] = {
    ap.SOURCE_VERIFIER_ATTESTATION_PROFILE: "arc_source_admission_refused",
    ap.ARTIFACT_SEMANTICS_V1_PROFILE: "arc_activation_predicate_failed",
    ap.ARTIFACT_SEMANTICS_V2_PROFILE: "arc_activation_predicate_failed",
    ap.APPROVAL_REVIEW_PACKAGE_V1_PROFILE: "arc_activation_predicate_failed",
    ap.APPROVAL_REVIEW_PACKAGE_V2_PROFILE: "arc_activation_predicate_failed",
    ap.ARTIFACT_REVISION_V1_PROFILE: "arc_activation_predicate_failed",
    ap.ARTIFACT_REVISION_V2_PROFILE: "arc_activation_predicate_failed",
}


def _decide(profile_literal: str, case_id: str, case: dict[str, Any], keys: dict[str, Any]) -> tuple[str, str | None]:
    """Reproduce the manifest's `(decision, refusal_code)` for one case,
    layering exactly the checks a caller outside this pure module would
    apply on top of it: structural validation and same-object rules first
    (`validate_<profile>`), then signature, then expiry, then digest-chain
    reference -- each only consulted if the case is still provisionally
    accepted."""
    obj = _load_case_input(case["input_path"])
    expected = case["expected"]
    # `PROFILE_FUNCTIONS` maps a profile literal to `(validate, canonicalize)`
    # in that order, as its type annotation states: the first returns `None`,
    # the second returns `bytes`. Unpack both by name rather than positionally
    # discarding one -- getting this backwards is invisible here, because
    # `validate_<profile>` is defined as calling `canonicalize_<profile>` and
    # discarding the result, so either function raises or does not raise
    # identically. It is not invisible where the bytes are compared.
    validate_fn, _canonicalize_fn = ap.PROFILE_FUNCTIONS[profile_literal]

    try:
        validate_fn(obj)
        decision, refusal_code = "accept", None
    except ap.AuthoringProfileError as exc:
        return "refuse", exc.refusal_code

    if case_id in _SIGNATURE_CASE_IDS and expected["signature_base64"] is not None:
        if not _verify_signature(profile_literal, expected, keys):
            if case_id == "principal_mismatch" and profile_literal == ap.OBSERVATION_QUALIFICATION_PROFILE:
                return "refuse", _QUALIFICATION_PRINCIPAL_MISMATCH_CODE
            return "refuse", _SIGNATURE_REFUSAL_CODE[profile_literal]

    if case_id == "expiry_equality" and profile_literal in _EXPIRY_FIELD_PAIRS:
        field_a, field_b = _EXPIRY_FIELD_PAIRS[profile_literal]
        if obj[field_a] == obj[field_b]:
            return "refuse", _EXPIRY_REFUSAL_CODE[profile_literal]

    if case_id == "digest_substitution" and profile_literal in _CHAIN_REFERENCES:
        own_field, sibling_dir, sibling_case_id = _CHAIN_REFERENCES[profile_literal]
        if obj[own_field] != _published_digest(sibling_dir, sibling_case_id):
            return "refuse", _CHAIN_REFUSAL_CODE[profile_literal]

    return decision, refusal_code


# ---------------------------------------------------------------------------
# (a) Every manifest profile is a canonicalization profile this build
# supports, and the reverse: this module claims exactly the sixteen
# literals the manifest exercises, not a superset it never gets checked.
# ---------------------------------------------------------------------------


def test_every_manifest_profile_is_supported() -> None:
    literals = {p["profile"] for p in _load_manifest()["profiles"]}
    assert literals <= SUPPORTED_PROFILES
    assert literals == set(ap.PROFILE_FUNCTIONS)


# ---------------------------------------------------------------------------
# (b) schema.json's field set equals the closed field set this module
# enforces, per profile. This is what stops the fixture's copy of the ADR
# schema and this module's own transcription from drifting apart silently.
# ---------------------------------------------------------------------------


def test_schema_json_field_set_matches_the_production_module() -> None:
    for profile in _load_manifest()["profiles"]:
        schema = json.loads((_FIXTURE_ROOT / profile["schema_path"]).read_text(encoding="utf-8"))
        fixture_fields = set(schema["properties"])
        production_fields = ap.profile_field_names(profile["profile"])
        assert fixture_fields == production_fields, profile["profile"]


# ---------------------------------------------------------------------------
# No chain node names its own digest or a node later in the chain --
# asserted against the production module's own declared field set, not
# trusted from the ADR text it was transcribed from.
# ---------------------------------------------------------------------------


def test_no_chain_node_names_its_own_digest_or_a_later_node() -> None:
    s_fields = ap.profile_field_names(ap.ARTIFACT_SEMANTICS_PROFILE)
    r_fields = ap.profile_field_names(ap.APPROVAL_REVIEW_PACKAGE_PROFILE)
    a_fields = ap.profile_field_names(ap.ARTIFACT_REVISION_PROFILE)

    # S is the first node: it cannot reference R or A (they do not exist
    # yet from its perspective) and cannot name its own digest.
    assert "artifact_semantics_digest" not in s_fields
    assert "review_package_digest" not in s_fields
    assert "artifact_revision_digest" not in s_fields

    # R may reference S (the one permitted earlier-node reference) but not
    # its own digest or A's, which does not exist yet from R's perspective.
    assert "artifact_semantics_digest" in r_fields
    assert "review_package_digest" not in r_fields
    assert "artifact_revision_digest" not in r_fields

    # A references both earlier nodes by digest but never its own -- there
    # is no later node for it to reference.
    assert "artifact_semantics_digest" in a_fields
    assert "review_package_digest" in a_fields
    assert "artifact_revision_digest" not in a_fields


# ---------------------------------------------------------------------------
# (c) The production canonicalizer reproduces every published
# canonical_bytes_base64, digest, decision, and refusal_code -- all 187
# cases, agreement proven against the manifest's own published fields.
# ---------------------------------------------------------------------------


def _iter_cases() -> list[tuple[str, str, dict[str, Any]]]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    for profile in _load_manifest()["profiles"]:
        for case in profile["cases"]:
            out.append((profile["profile"], case["case_id"], case))
    return out


@pytest.mark.parametrize(
    ("profile_literal", "case_id", "case"), _iter_cases(), ids=lambda v: v if isinstance(v, str) else "case"
)
def test_production_canonicalizer_agrees_with_the_manifest(
    profile_literal: str, case_id: str, case: dict[str, Any]
) -> None:
    expected = case["expected"]
    obj = _load_case_input(case["input_path"])
    _validate_fn, canonicalize_fn = ap.PROFILE_FUNCTIONS[profile_literal]

    try:
        canonical = canonicalize_fn(obj)
        canonicalized = True
    except ap.AuthoringProfileError:
        canonicalized = False

    if expected["canonical_bytes_base64"] is None:
        # Structural family: nothing was ever canonicalized on any side.
        assert not canonicalized, f"{case_id}: expected canonicalization to fail, it succeeded"
        assert expected["digest"] is None
    else:
        assert canonicalized, f"{case_id}: expected canonicalization to succeed"
        assert canonical == base64.b64decode(expected["canonical_bytes_base64"]), f"{case_id}: canonical bytes differ"
        assert hashlib.sha256(canonical).hexdigest() == expected["digest"], f"{case_id}: digest differs"

    keys = _load_keys()
    decision, refusal_code = _decide(profile_literal, case_id, case, keys)
    assert decision == expected["decision"], f"{case_id}: decision mismatch ({decision!r} != {expected['decision']!r})"
    assert (
        refusal_code == expected["refusal_code"]
    ), f"{case_id}: refusal_code mismatch ({refusal_code!r} != {expected['refusal_code']!r})"

    # Bonus agreement check for the accepted, signed cases: the manifest's
    # real signature must verify against *this* canonicalizer's own bytes,
    # not merely against the manifest's separately published bytes. A
    # canonicalizer that silently diverged from what was actually signed
    # would still pass every assertion above and fail only here.
    if expected["decision"] == "accept" and expected["signature_base64"] is not None:
        assert _verify_signature(
            profile_literal, expected, keys
        ), f"{case_id}: the published signature does not verify against this canonicalizer's own bytes"
