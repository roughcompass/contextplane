"""Unit tests for the authoring-surface profile validation/canonicalization
engine (`contextplane/arc/schemas/authoring_profiles.py`): pure functions with
no I/O, and -- at 211 statements -- this phase's most load-bearing logic.

Each of the sixteen profiles gets a minimal, hand-built valid instance below
(never read from `tests/fixtures/arc_authoring/`, which is a different
tier's fixture set with its own manifest and signing keys; duplicating that
coupling here would make these tests break for someone else's reason). Every
fixture is exercised through both halves of `PROFILE_FUNCTIONS` -- validate
then canonicalize, in that order -- and every refusal reason this module can
raise through its shared recursive engine is triggered independently at
least once: non-NFC text, an embedded NUL, a fractional number, an unknown
field, a missing required field, a duplicate entry in a set-labelled array,
and an ordered-array sort violation. The four same-object business rules a
closed field set cannot express (`validate_field_provenance_v1`'s
conditional field groups, `validate_expected_impact_envelope_v1`'s item
non-overlap, `validate_actor_separation_v1`'s distinct-principal rule, and
`validate_source_approval_evidence_v1`'s embedded claim-digest check) each
get an accept case and a violation case of their own.

Two canonical-bytes assertions are checked against an independently
recomputed expected value -- a fresh `json.dumps(..., separators=(",",
":"))` call over the instance's own items sorted by this test, never by
calling anything `_serialize` touches -- so they only pass if the production
sort-then-compact-encode behavior is real, not merely self-consistent.

`tests/conformance/test_arc_authoring_vectors.py` owns agreement with the
version-controlled vector manifest (187 cases across all sixteen profiles,
including the signature/expiry/digest-chain business rules that live
*outside* this module, at the layer a caller applies on top of it) and
`test_canonicalization_agreement.py` owns the two-engine contract with
`canonical.py`. Neither exercises this module's own refusal branches or
business-rule functions directly by name the way the tests below do, so
nothing here duplicates either.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import pytest

from contextplane.arc.schemas import authoring_profile_shapes as shapes
from contextplane.arc.schemas import authoring_profiles as ap

# ---------------------------------------------------------------------------
# Shared scalar values. Fixed rather than randomly generated: a refusal test
# that mutates one field needs the rest of the instance to stay valid, which
# is only checkable by eye against a fixed baseline.
# ---------------------------------------------------------------------------

_UUID1 = "11111111-1111-1111-1111-111111111111"
_UUID2 = "22222222-2222-2222-2222-222222222222"
_UUID3 = "33333333-3333-3333-3333-333333333333"
_DIGEST1 = "a" * 64
_DIGEST2 = "b" * 64
_TS1 = "2024-01-01T00:00:00Z"
_TS2 = "2024-06-01T00:00:00Z"
_ISSUER = "https://issuer.example"


# ---------------------------------------------------------------------------
# One minimal-valid-instance builder per profile. Each call returns a fresh
# dict -- callers that mutate one field to build a refusal case never risk
# corrupting another test's fixture.
# ---------------------------------------------------------------------------


def _claim() -> dict[str, Any]:
    return {
        "profile": shapes.SOURCE_APPROVAL_CLAIM_PROFILE,
        "source_system": "github",
        "source_revision_locator": "owner/repo@abcdef",
        "source_content_digest_algorithm": "sha256",
        "source_content_digest": _DIGEST1,
        "source_content_type": "text/markdown",
        "approval_locator": "pr#42",
        "approving_authority_issuer": _ISSUER,
        "approving_authority_subject": "approver-1",
        "approval_scope": "repo:owner/repo",
        "approved_at": _TS1,
        "expires_at": _TS2,
    }


def _attestation() -> dict[str, Any]:
    return {
        "profile": shapes.SOURCE_VERIFIER_ATTESTATION_PROFILE,
        "attestation_id": _UUID1,
        "provider_id": "verifier-co",
        "provider_configuration_digest": _DIGEST1,
        "claim_digest": _DIGEST2,
        "approving_authority_issuer": _ISSUER,
        "approving_authority_subject": "approver-1",
        "source_system": "github",
        "approval_scope": "repo:owner/repo",
        "issued_at": _TS1,
        "expires_at": _TS2,
    }


def _evidence() -> dict[str, Any]:
    claim = _claim()
    real_digest = hashlib.sha256(ap.canonicalize_source_approval_claim_v1(claim)).hexdigest()
    return {
        "profile": shapes.SOURCE_APPROVAL_EVIDENCE_PROFILE,
        "evidence_id": _UUID1,
        "claim": claim,
        "claim_digest": real_digest,
        "verification_method": "source_signed",
        "verifier_id": "verifier-1",
        "signature": "sig-bytes-1",
        "verifier_attestation": None,
        "admission_method": "configured_connector",
        "connector_id": "connector-1",
        "admitted_at": _TS1,
        "admitted_by_issuer": _ISSUER,
        "admitted_by_subject": "admitter-1",
        "verified_at": _TS1,
        "idempotency_key_digest": _DIGEST1,
        "admission_request_payload_digest": _DIGEST2,
    }


def _class_predicate(*, populated: bool = False) -> dict[str, Any]:
    if populated:
        return {
            "profile": shapes.OBSERVATION_CLASS_PREDICATE_PROFILE,
            "task_kind": ["draft_change"],
            "requested_action_classes": ["write"],
            "environment": ["prod"],
            "data_sensitivity_tier": ["low"],
            "capability_ids": [_UUID1],
            "domain_ids": ["domain-a"],
        }
    return {
        "profile": shapes.OBSERVATION_CLASS_PREDICATE_PROFILE,
        "task_kind": None,
        "requested_action_classes": None,
        "environment": None,
        "data_sensitivity_tier": None,
        "capability_ids": None,
        "domain_ids": None,
    }


def _envelope_item(item_id: str, delta_code: str) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "delta_code": delta_code,
        "class_predicate": _class_predicate(),
        "minimum_count": 1,
        "maximum_count": None,
        "rationale_code": "baseline",
    }


def _envelope() -> dict[str, Any]:
    return {
        "profile": shapes.EXPECTED_IMPACT_ENVELOPE_PROFILE,
        "envelope_id": _UUID1,
        "proposal_id": _UUID2,
        "proposal_version": 1,
        "items": [
            _envelope_item("item-a", "newly_selected"),
            _envelope_item("item-b", "no_longer_selected"),
        ],
        "author_issuer": _ISSUER,
        "author_subject": "author-1",
        "created_at": _TS1,
    }


def _field_provenance(*, provenance_class: str) -> dict[str, Any]:
    base = {
        "profile": shapes.FIELD_PROVENANCE_PROFILE,
        "field_path": "title",
        "provenance_class": provenance_class,
        "source_anchor": None,
        "quoted_excerpt_digest": None,
        "author_issuer": None,
        "author_subject": None,
        "author_role": None,
        "derivation_profile": None,
    }
    if provenance_class == "source_backed":
        base["source_anchor"] = "anchor#1"
        base["quoted_excerpt_digest"] = _DIGEST1
    elif provenance_class == "human_judgment":
        base["author_issuer"] = _ISSUER
        base["author_subject"] = "reviewer-1"
        base["author_role"] = "editor"
    elif provenance_class == "server_derived":
        base["derivation_profile"] = "derivation-v1"
    return base


def _artifact_semantics() -> dict[str, Any]:
    return {
        "profile": shapes.ARTIFACT_SEMANTICS_PROFILE,
        "projection_schema_version": 1,
        "materialiser_profile": "directive_bundle_v1",
        "materialiser_version": "1.0.0",
        "applicability_baseline_version": "1.0.0",
        "artifact_id": _UUID1,
        "revision_id": _UUID2,
        "kind": "directive_bundle",
        "owning_scope": "global",
        "owning_tenant_id": None,
        "visibility": "standard",
        "source_system": "github",
        "source_revision_locator": "owner/repo@abcdef",
        "source_content_digest": _DIGEST1,
        "source_approval_evidence_digest": _DIGEST2,
        "directives": [],
        "applicability": [],
        "detail_audience": "agent_and_human",
        "review_expires_at": _TS2,
        "content_classification": "internal",
        "approved_retention_floor_days": 90,
        "initial_freshness_basis": "connector_verified",
        "reviewed_baseline_revision_id": None,
    }


def _review_package() -> dict[str, Any]:
    return {
        "profile": shapes.APPROVAL_REVIEW_PACKAGE_PROFILE,
        "artifact_semantics_digest": _DIGEST1,
        "source_approval_evidence_digest": _DIGEST2,
        "field_provenance": [],
        "semantic_tests": [],
        "risk_classification": "tenant_non_mandatory",
        "risk_algorithm_version": "v1",
        "expected_impact_envelope_digest": _DIGEST1,
        "baseline_diff_digest": _DIGEST2,
        "proposal_id": _UUID1,
        "proposal_version": 1,
        "submitted_by_issuer": _ISSUER,
        "submitted_by_subject": "submitter-1",
        "submitted_at": _TS1,
    }


def _artifact_revision() -> dict[str, Any]:
    return {
        "profile": shapes.ARTIFACT_REVISION_PROFILE,
        "artifact_id": _UUID1,
        "revision_id": _UUID2,
        "artifact_semantics_digest": _DIGEST1,
        "review_package_digest": _DIGEST2,
        "actor_separation_profile": shapes.ACTOR_SEPARATION_PROFILE,
    }


def _actor_separation(*, risk: str = "tenant_non_mandatory") -> dict[str, Any]:
    return {
        "profile": shapes.ACTOR_SEPARATION_PROFILE,
        "risk_classification": risk,
        "submitter_issuer": "iss-a",
        "submitter_subject": "sub-a",
        "approver_issuer": "iss-b",
        "approver_subject": "sub-b",
        "accepter_issuer": None,
        "accepter_subject": None,
        "activator_issuer": "iss-c",
        "activator_subject": "sub-c",
        "required_distinct_count": 2,
        "satisfied": True,
    }


def _verifier_enrollment() -> dict[str, Any]:
    return {
        "profile": shapes.APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
        "enrollment_challenge_id": _UUID1,
        "nonce": "nonce-1",
        "verifier_id": "verifier-1",
        "binding_kind": "exact_principal",
        "principal_issuer": _ISSUER,
        "principal_subject": "principal-1",
        "provider_allowed_principal_issuer": None,
        "scope_kind": "global",
        "target_tenant_id": None,
        "allowed_evidence_types": ["artifact_activation"],
        "signature_algorithm": "Ed25519",
        "key_digest": _DIGEST1,
        "valid_from": _TS1,
        "valid_to": _TS2,
        "issued_at": _TS1,
        "expires_at": _TS2,
    }


def _provider_assertion() -> dict[str, Any]:
    return {
        "profile": shapes.APPROVAL_PROVIDER_ASSERTION_PROFILE,
        "assertion_id": _UUID1,
        "provider_id": "provider-1",
        "provider_configuration_digest": _DIGEST1,
        "approval_challenge_id": _UUID2,
        "approval_evidence_digest": _DIGEST2,
        "principal_issuer": _ISSUER,
        "principal_subject": "principal-1",
        "issued_at": _TS1,
        "expires_at": _TS2,
    }


def _operational_event() -> dict[str, Any]:
    return {
        "profile": shapes.OPERATIONAL_EVENT_PROFILE,
        "event_id": _UUID1,
        "artifact_id": _UUID2,
        "revision_id": _UUID3,
        "sequence": 1,
        "event_type": "operational_state_initialized",
        "event_payload": {
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
        },
        "actor_issuer": _ISSUER,
        "actor_subject": "system",
        "actor_role": "system",
        "authorization_decision_reference": "decision-1",
        "authority_evidence_digest": _DIGEST1,
        "idempotency_key_digest": _DIGEST2,
        "previous_event_digest": None,
        "signer_key_id": "key-1",
        "created_at": _TS1,
    }


def _observation_cohort() -> dict[str, Any]:
    return {
        "profile": shapes.OBSERVATION_COHORT_PROFILE,
        "cohort_id": _UUID1,
        "risk_classification": "tenant_non_mandatory",
        "scope_predicate_digest": _DIGEST1,
        "tenant_membership_digest": _DIGEST2,
        "eligibility_predicate_digest": _DIGEST1,
        "frozen_at": _TS1,
        "window_started_at": _TS1,
        "window_deadline": _TS2,
    }


def _observation_qualification() -> dict[str, Any]:
    return {
        "profile": shapes.OBSERVATION_QUALIFICATION_PROFILE,
        "qualification_id": _UUID1,
        "idempotency_key_digest": _DIGEST1,
        "candidate_review_package_digest": _DIGEST2,
        "candidate_revision_id": _UUID2,
        "proposal_id": _UUID3,
        "proposal_version": 1,
        "risk_classification": "tenant_non_mandatory",
        "risk_algorithm_version": "v1",
        "baseline_revision_id": None,
        "selection_engine_version": "v1",
        "engine_configuration_version": "v1",
        "cohort_id": _UUID1,
        "cohort_digest": _DIGEST1,
        "window_started_at": _TS1,
        "window_ended_at": _TS2,
        "eligible_count": 10,
        "observed_count": 8,
        "expected_impact_envelope_digest": _DIGEST2,
        "counters_by_delta_code": [],
        "unexplained_count": 0,
        "out_of_envelope_count": 0,
        "replay_corpus_digest": None,
        "replay_result_digest": None,
        "qualification_algorithm_version": "v1",
        "computed_decision": "qualified",
        "reason_codes": [],
        "accepted_by_issuer": None,
        "accepted_by_subject": None,
        "accepted_by_role": None,
        "accepted_at": None,
        "acceptance_audit_reference": None,
        "expires_at": None,
    }


def _observation_replay_corpus() -> dict[str, Any]:
    return {
        "profile": shapes.OBSERVATION_REPLAY_CORPUS_PROFILE,
        "corpus_id": _UUID1,
        "generator_version": "v1",
        "generator_input_digest": _DIGEST1,
        "canonical_corpus_digest": _DIGEST2,
        "fixture_class_count": 5,
        "scope": "global",
        "target_tenant_id": None,
        "approving_authority_issuer": _ISSUER,
        "approving_authority_subject": "approver-1",
        "approved_at": _TS1,
        "expires_at": _TS2,
    }


_FIXTURE_BUILDERS: dict[str, Any] = {
    shapes.SOURCE_APPROVAL_CLAIM_PROFILE: _claim,
    shapes.SOURCE_VERIFIER_ATTESTATION_PROFILE: _attestation,
    shapes.SOURCE_APPROVAL_EVIDENCE_PROFILE: _evidence,
    shapes.OBSERVATION_CLASS_PREDICATE_PROFILE: _class_predicate,
    shapes.EXPECTED_IMPACT_ENVELOPE_PROFILE: _envelope,
    shapes.FIELD_PROVENANCE_PROFILE: lambda: _field_provenance(provenance_class="source_backed"),
    shapes.ARTIFACT_SEMANTICS_PROFILE: _artifact_semantics,
    shapes.APPROVAL_REVIEW_PACKAGE_PROFILE: _review_package,
    shapes.ARTIFACT_REVISION_PROFILE: _artifact_revision,
    shapes.ACTOR_SEPARATION_PROFILE: _actor_separation,
    shapes.APPROVAL_VERIFIER_ENROLLMENT_PROFILE: _verifier_enrollment,
    shapes.APPROVAL_PROVIDER_ASSERTION_PROFILE: _provider_assertion,
    shapes.OPERATIONAL_EVENT_PROFILE: _operational_event,
    shapes.OBSERVATION_COHORT_PROFILE: _observation_cohort,
    shapes.OBSERVATION_QUALIFICATION_PROFILE: _observation_qualification,
    shapes.OBSERVATION_REPLAY_CORPUS_PROFILE: _observation_replay_corpus,
}


def test_fixture_builders_cover_exactly_the_sixteen_authoring_profiles() -> None:
    assert set(_FIXTURE_BUILDERS) == shapes.AUTHORING_PROFILES
    assert set(_FIXTURE_BUILDERS) == set(ap.PROFILE_FUNCTIONS)


# ---------------------------------------------------------------------------
# PROFILE_FUNCTIONS: the exact bug this task calls out. `validate_fn` and
# `canonicalize_fn` raise identically on bad input (`validate_<profile>` is
# defined as "call canonicalize, discard the result"), so a reversed unpack
# is invisible by behavior alone -- checked here by function identity/name
# instead.
# ---------------------------------------------------------------------------


def test_profile_functions_unpacks_as_validate_then_canonicalize_in_that_order() -> None:
    for profile_literal, (validate_fn, canonicalize_fn) in ap.PROFILE_FUNCTIONS.items():
        suffix = profile_literal[len("arc_") :]
        assert validate_fn.__name__ == f"validate_{suffix}", profile_literal
        assert canonicalize_fn.__name__ == f"canonicalize_{suffix}", profile_literal


def test_profile_field_names_returns_the_schemas_declared_top_level_fields() -> None:
    for literal, schema in shapes.SCHEMA_BY_PROFILE.items():
        assert ap.profile_field_names(literal) == frozenset(schema["properties"]), literal


# ---------------------------------------------------------------------------
# Accept path: every profile's minimal valid instance passes validate_* and
# round-trips through canonicalize_* to well-formed, re-parseable, and
# idempotent bytes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_literal", sorted(_FIXTURE_BUILDERS))
def test_validate_accepts_and_canonicalize_round_trips_a_minimal_valid_instance(profile_literal: str) -> None:
    instance = _FIXTURE_BUILDERS[profile_literal]()
    validate_fn, canonicalize_fn = ap.PROFILE_FUNCTIONS[profile_literal]

    assert validate_fn(instance) is None

    canonical = canonicalize_fn(instance)
    assert isinstance(canonical, bytes)
    assert json.loads(canonical) == instance
    # Canonicalization is a pure function of the instance: calling it again
    # on the same (unmutated) object produces byte-identical output.
    assert canonicalize_fn(instance) == canonical


# ---------------------------------------------------------------------------
# Canonical bytes for representative values, checked against an
# independently recomputed expectation rather than against this module's
# own `_serialize` helper.
# ---------------------------------------------------------------------------


def _independently_sorted_canonical_bytes(instance: dict[str, Any]) -> bytes:
    return json.dumps(dict(sorted(instance.items())), separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_canonicalize_source_approval_claim_matches_an_independently_sorted_encoding() -> None:
    instance = _claim()
    assert ap.canonicalize_source_approval_claim_v1(instance) == _independently_sorted_canonical_bytes(instance)


def test_canonicalize_observation_class_predicate_matches_an_independently_sorted_encoding() -> None:
    instance = _class_predicate(populated=False)
    assert ap.canonicalize_observation_class_predicate_v1(instance) == _independently_sorted_canonical_bytes(instance)


# ---------------------------------------------------------------------------
# Every refusal reason the shared recursive engine can raise, each
# independently reachable.
# ---------------------------------------------------------------------------


def test_non_nfc_string_value_is_refused() -> None:
    claim = _claim()
    claim["source_system"] = "é"  # decomposed ("NFD") form of the composed character e-acute
    with pytest.raises(ap.ProfileValidationFailed, match="NFC"):
        ap.validate_source_approval_claim_v1(claim)


def test_embedded_nul_in_a_string_value_is_refused() -> None:
    claim = _claim()
    claim["source_system"] = "gith\x00ub"
    with pytest.raises(ap.ProfileValidationFailed, match="NUL"):
        ap.validate_source_approval_claim_v1(claim)


def test_fractional_number_is_refused() -> None:
    envelope = _envelope()
    envelope["proposal_version"] = 1.5
    with pytest.raises(ap.ProfileValidationFailed, match="fractional"):
        ap.validate_expected_impact_envelope_v1(envelope)


def test_unknown_field_is_refused() -> None:
    claim = _claim()
    claim["extra_bogus_field"] = "x"
    with pytest.raises(ap.ProfileValidationFailed, match="unknown field"):
        ap.validate_source_approval_claim_v1(claim)


def test_missing_required_field_is_refused() -> None:
    claim = _claim()
    del claim["approval_scope"]
    with pytest.raises(ap.ProfileValidationFailed, match="missing required field"):
        ap.validate_source_approval_claim_v1(claim)


def test_duplicate_entry_in_a_set_labelled_array_is_refused() -> None:
    predicate = _class_predicate(populated=True)
    predicate["task_kind"] = ["draft_change", "draft_change"]
    with pytest.raises(ap.ProfileValidationFailed, match="duplicate entry"):
        ap.validate_observation_class_predicate_v1(predicate)


def test_ordered_array_out_of_sort_order_is_refused() -> None:
    envelope = _envelope()
    envelope["items"] = list(reversed(envelope["items"]))
    with pytest.raises(ap.ProfileValidationFailed, match="ascending"):
        ap.validate_expected_impact_envelope_v1(envelope)


def test_null_is_refused_where_the_schema_does_not_permit_it() -> None:
    revision = _artifact_revision()
    revision["artifact_id"] = None  # not a nullable field on this profile
    with pytest.raises(ap.ProfileValidationFailed, match="null is not permitted"):
        ap.validate_artifact_revision_v1(revision)


def test_wrong_json_type_for_a_field_is_refused() -> None:
    claim = _claim()
    claim["approved_at"] = 12345  # a timestamp field given a number instead of a string
    with pytest.raises(ap.ProfileValidationFailed, match="expected"):
        ap.validate_source_approval_claim_v1(claim)


# Every remaining branch of the type-mismatch dispatch in
# `_check_and_canonicalize`, one JSON type standing in for "not this field's
# declared type" at a time.


def test_boolean_value_is_refused_where_the_schema_declares_a_different_type() -> None:
    claim = _claim()
    claim["source_system"] = True  # a string-typed field given a boolean
    with pytest.raises(ap.ProfileValidationFailed, match="got a boolean"):
        ap.validate_source_approval_claim_v1(claim)


def test_string_value_is_refused_where_the_schema_declares_a_different_type() -> None:
    record = _actor_separation()
    record["satisfied"] = "yes"  # a boolean-typed field given a string
    with pytest.raises(ap.ProfileValidationFailed, match="got a string"):
        ap.validate_actor_separation_v1(record)


def test_array_value_is_refused_where_the_schema_declares_a_different_type() -> None:
    claim = _claim()
    claim["source_system"] = ["github"]  # a string-typed field given an array
    with pytest.raises(ap.ProfileValidationFailed, match="got an array"):
        ap.validate_source_approval_claim_v1(claim)


def test_object_value_is_refused_where_the_schema_declares_a_different_type() -> None:
    claim = _claim()
    claim["source_system"] = {"name": "github"}  # a string-typed field given an object
    with pytest.raises(ap.ProfileValidationFailed, match="got an object"):
        ap.validate_source_approval_claim_v1(claim)


def test_a_value_of_no_recognized_json_type_is_refused() -> None:
    claim = _claim()
    claim["source_system"] = object()  # not None/bool/str/int/float/list/dict
    with pytest.raises(ap.ProfileValidationFailed, match="unsupported value type"):
        ap.validate_source_approval_claim_v1(claim)


def test_profile_literal_confusion_is_refused_as_a_fixed_value_mismatch() -> None:
    """The `profile` field is a `const` schema; submitting a different
    profile's literal here is the "profile confusion" case named in
    `ProfileValidationFailed`'s own docstring."""
    claim = _claim()
    claim["profile"] = shapes.SOURCE_VERIFIER_ATTESTATION_PROFILE
    with pytest.raises(ap.ProfileValidationFailed, match="fixed value"):
        ap.validate_source_approval_claim_v1(claim)


def test_enum_value_outside_the_declared_set_is_refused() -> None:
    claim = _claim()
    claim["source_content_digest_algorithm"] = "md5"
    with pytest.raises(ap.ProfileValidationFailed, match="is not one of"):
        ap.validate_source_approval_claim_v1(claim)


def test_string_not_matching_the_declared_pattern_is_refused() -> None:
    claim = _claim()
    claim["source_content_digest"] = "not-a-valid-digest"
    with pytest.raises(ap.ProfileValidationFailed, match="does not match"):
        ap.validate_source_approval_claim_v1(claim)


def test_array_shorter_than_min_items_is_refused() -> None:
    envelope = _envelope()
    envelope["items"] = []
    with pytest.raises(ap.ProfileValidationFailed, match="at least 1"):
        ap.validate_expected_impact_envelope_v1(envelope)


# ---------------------------------------------------------------------------
# The four same-object business rules a closed field set cannot express.
# ---------------------------------------------------------------------------


def test_field_provenance_source_backed_requires_anchor_and_excerpt_digest() -> None:
    record = _field_provenance(provenance_class="source_backed")
    ap.validate_field_provenance_v1(record)  # accepts: no exception

    record["source_anchor"] = None
    with pytest.raises(ap.FieldProvenanceConditionalError, match="source_anchor"):
        ap.validate_field_provenance_v1(record)


def test_field_provenance_human_judgment_forbids_derivation_profile() -> None:
    record = _field_provenance(provenance_class="human_judgment")
    ap.validate_field_provenance_v1(record)  # accepts: no exception

    record["derivation_profile"] = "some-profile"
    with pytest.raises(ap.FieldProvenanceConditionalError, match="derivation_profile"):
        ap.validate_field_provenance_v1(record)


def test_field_provenance_server_derived_requires_derivation_profile() -> None:
    record = _field_provenance(provenance_class="server_derived")
    ap.validate_field_provenance_v1(record)  # accepts: no exception

    record["derivation_profile"] = None
    with pytest.raises(ap.FieldProvenanceConditionalError, match="derivation_profile"):
        ap.validate_field_provenance_v1(record)


def test_expected_impact_envelope_items_must_not_overlap() -> None:
    envelope = _envelope()
    ap.validate_expected_impact_envelope_v1(envelope)  # accepts: no exception

    overlapping = copy.deepcopy(envelope)
    overlapping["items"][1]["delta_code"] = overlapping["items"][0]["delta_code"]
    overlapping["items"][1]["class_predicate"] = overlapping["items"][0]["class_predicate"]
    with pytest.raises(ap.EnvelopeItemsOverlapError, match="overlapping class predicate"):
        ap.validate_expected_impact_envelope_v1(overlapping)


def test_actor_separation_requires_submitter_and_approver_to_be_distinct() -> None:
    record = _actor_separation()
    ap.validate_actor_separation_v1(record)  # accepts: no exception

    record["approver_issuer"] = record["submitter_issuer"]
    record["approver_subject"] = record["submitter_subject"]
    with pytest.raises(ap.ActorSeparationViolationError, match="distinct principals"):
        ap.validate_actor_separation_v1(record)


def test_actor_separation_global_mandatory_requires_three_distinct_principals() -> None:
    record = _actor_separation(risk="global_mandatory")
    ap.validate_actor_separation_v1(record)  # accepts: submitter/approver/activator already all distinct

    record["activator_issuer"] = record["submitter_issuer"]
    record["activator_subject"] = record["submitter_subject"]
    with pytest.raises(ap.ActorSeparationViolationError, match="three distinct principals"):
        ap.validate_actor_separation_v1(record)


def test_source_approval_evidence_claim_digest_must_match_the_embedded_claim() -> None:
    evidence = _evidence()
    ap.validate_source_approval_evidence_v1(evidence)  # accepts: no exception

    evidence["claim_digest"] = "0" * 64
    with pytest.raises(ap.SourceAdmissionRefusedError, match="claim_digest"):
        ap.validate_source_approval_evidence_v1(evidence)


# ---------------------------------------------------------------------------
# Error hierarchy: each distinguishable outcome carries its own named
# refusal code, not a shared class plus an attribute a caller has to
# remember to read.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_cls", "expected_code"),
    [
        (ap.ProfileValidationFailed, "arc_proposal_validation_failed"),
        (ap.EnvelopeItemsOverlapError, "arc_envelope_invalid"),
        (ap.FieldProvenanceConditionalError, "arc_provenance_invalid"),
        (ap.ActorSeparationViolationError, "arc_activation_predicate_failed"),
        (ap.SourceAdmissionRefusedError, "arc_source_admission_refused"),
    ],
)
def test_each_authoring_profile_error_carries_its_own_refusal_code(
    error_cls: type[ap.AuthoringProfileError], expected_code: str
) -> None:
    assert error_cls.refusal_code == expected_code
    assert issubclass(error_cls, ap.AuthoringProfileError)
