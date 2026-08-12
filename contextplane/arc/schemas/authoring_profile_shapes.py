"""Closed shapes for the authoring-surface profiles: the profile literals
and the schema each one enforces, expressed as small composable data -- no
validation or canonicalization logic lives here.

A schema in this module is a plain dict describing one JSON-shaped value:
its type, its enum/pattern/const constraint if it has one, and for an
object, its complete property set (every declared property is required as
a key; nullability lives on the property's own schema, not on making the
key optional). Every array is explicitly labeled "set" (order-independent,
deduplicated by its own canonical bytes) or "ordered" (a meaning-bearing
sequence, optionally checked against an ascending sort key named on an item
field). `authoring_profiles.py` is the module that walks these shapes to
validate and canonicalize an instance; this module only says what shape
each profile is.

Three profile families carry two live versions, because the Intent
vocabulary changed the field names inside them and previously accepted
bytes still have to verify under the names they were signed with. For each
of those, `<NAME>_V1_PROFILE` is verification-only and frozen at its
original spelling, `<NAME>_V2_PROFILE` is the active one, and the
unsuffixed `<NAME>_PROFILE` is bound to the active version so a call site
that means "the profile we author today" keeps saying so. Nothing here
guesses a version: `AUTHORING_PROFILES` is what may be verified,
`ACTIVE_AUTHORING_PROFILES` is what may be written, and a caller resolves a
shape from an exact literal or is refused.
"""

from __future__ import annotations

import re
from typing import Any

type Schema = dict[str, Any]

_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")

RISK_CLASSIFICATIONS: tuple[str, ...] = (
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
DELTA_CODES: tuple[str, ...] = (
    "newly_selected",
    "no_longer_selected",
    "conflict_changed",
    "mandatory_block_added",
    "mandatory_block_removed",
)


# ---------------------------------------------------------------------------
# Small composable schema shapes.
# ---------------------------------------------------------------------------


def _const(value: str) -> Schema:
    return {"type": "string", "const": value}


def _string() -> Schema:
    return {"type": "string"}


def _uuid() -> Schema:
    return {"type": "string", "pattern": _UUID_PATTERN}


def _digest() -> Schema:
    return {"type": "string", "pattern": _DIGEST_PATTERN}


def _timestamp() -> Schema:
    return {"type": "string", "pattern": _TIMESTAMP_PATTERN}


def _enum(*values: str) -> Schema:
    return {"type": "string", "enum": values}


def _number() -> Schema:
    return {"type": "number"}


def _boolean() -> Schema:
    return {"type": "boolean"}


def _nullable(schema: Schema) -> Schema:
    out = dict(schema)
    base = out["type"]
    out["type"] = [*base, "null"] if isinstance(base, list) else [base, "null"]
    return out


def _array(items: Schema, *, kind: str, order_key: str | None = None, min_items: int | None = None) -> Schema:
    schema: Schema = {"type": "array", "items": items, "x-array-kind": kind}
    if order_key is not None:
        schema["x-order-key"] = order_key
    if min_items is not None:
        schema["minItems"] = min_items
    return schema


def _object(properties: dict[str, Schema]) -> Schema:
    # Every object in this profile family requires every declared property as
    # a key (nullability lives on the field's own schema, not on omitting the
    # key) and forbids anything else -- a closed field set only holds if
    # presence is unconditional, so "required" is never a subset.
    return {"type": "object", "properties": properties, "required": tuple(properties)}


def _profile(literal: str, fields: dict[str, Schema]) -> Schema:
    return _object({"profile": _const(literal), **fields})


# ---------------------------------------------------------------------------
# Nested (non-profile) object and array shapes, reused by more than one
# top-level profile below.
# ---------------------------------------------------------------------------

def _observation_class_predicate(literal: str, selector: str) -> Schema:
    # The two versions differ in exactly one field name -- `task_kind` became
    # `intent_kind` -- so they are built from one description. Writing them
    # out twice would let the frozen V1 shape drift the next time an
    # unrelated field is added to the active one, and a V1 shape that drifts
    # stops verifying bytes it already accepted.
    return _profile(
        literal,
        {
            selector: _nullable(_array(_string(), kind="set", min_items=1)),
            "requested_action_classes": _nullable(_array(_string(), kind="set", min_items=1)),
            "environment": _nullable(_array(_string(), kind="set", min_items=1)),
            "data_sensitivity_tier": _nullable(_array(_string(), kind="set", min_items=1)),
            "capability_ids": _nullable(_array(_uuid(), kind="set", min_items=1)),
            "domain_ids": _nullable(_array(_string(), kind="set", min_items=1)),
        },
    )


_OBSERVATION_CLASS_PREDICATE_V1_SCHEMA = _observation_class_predicate(
    "arc_observation_class_predicate_v1", "task_kind"
)
_OBSERVATION_CLASS_PREDICATE_V2_SCHEMA = _observation_class_predicate(
    "arc_observation_class_predicate_v2", "intent_kind"
)
_OBSERVATION_CLASS_PREDICATE_SCHEMA = _OBSERVATION_CLASS_PREDICATE_V2_SCHEMA


def _envelope_item(predicate: Schema) -> Schema:
    return _object(
        {
            "item_id": _string(),
            "delta_code": _enum(*DELTA_CODES),
            "class_predicate": predicate,
            "minimum_count": _number(),
            "maximum_count": _nullable(_number()),
            "rationale_code": _string(),
        }
    )


_ENVELOPE_ITEM_V1_SCHEMA = _envelope_item(_OBSERVATION_CLASS_PREDICATE_V1_SCHEMA)
_ENVELOPE_ITEM_V2_SCHEMA = _envelope_item(_OBSERVATION_CLASS_PREDICATE_V2_SCHEMA)
_ENVELOPE_ITEM_SCHEMA = _ENVELOPE_ITEM_V2_SCHEMA

_DIRECTIVE_SCHEMA = _object(
    {
        "directive_id": _uuid(),
        "directive_type": _enum("citation_only", "verify_before_action"),
        "compact_statement_plaintext": _string(),
        "compact_statement_plaintext_digest": _digest(),
        "source_anchor": _string(),
        "conflict_key_schema_version": _number(),
        "conflict_key_namespace": _nullable(_string()),
        "conflict_key_subject_selector": _nullable(_string()),
        "conflict_key_operation": _nullable(_string()),
        "conflict_key_action_class": _nullable(_string()),
        "conflict_key_target_selector": _nullable(_string()),
        "conflict_key_modality": _nullable(_string()),
        "conflict_key_constraint_operator": _nullable(_string()),
        "conflict_key_constraint_value": _nullable(_string()),
        "conflict_subject_digest": _nullable(_digest()),
        "delegable_exception": _boolean(),
        "satisfaction_mode": _nullable(_enum("authorized_retrieval", "signed_result")),
        "verification_max_age_seconds": _nullable(_number()),
        "accepted_verifier_classes": _nullable(_array(_string(), kind="set")),
        "accepted_verifier_ids": _nullable(_array(_string(), kind="set")),
        "required_evidence_type": _nullable(_string()),
        "created_at": _timestamp(),
    }
)

def _applicability_rule(narrowest_scope: str, selector: str) -> Schema:
    # `scope` and its selector array rename together or not at all: a rule
    # saying scope="intent" while selecting on `task_kinds` is the mixed
    # shape §6.1 requires be refused, and building both versions from one
    # description is what makes that mixture unconstructible here.
    return _object(
        {
            "rule_id": _uuid(),
            "scope": _enum("global", "tenant", "domain", "capability", narrowest_scope),
            "target_tenant_id": _nullable(_uuid()),
            "capability_ids": _nullable(_array(_uuid(), kind="set")),
            "capability_labels": _nullable(_array(_string(), kind="set")),
            "domain_ids": _nullable(_array(_string(), kind="set")),
            selector: _nullable(_array(_string(), kind="set")),
            "action_classes": _nullable(_array(_string(), kind="set")),
            "environments": _nullable(_array(_string(), kind="set")),
            "data_sensitivity_tiers": _nullable(_array(_string(), kind="set")),
            "effective_from": _nullable(_timestamp()),
            "effective_until": _nullable(_timestamp()),
            "is_mandatory": _boolean(),
        }
    )


_APPLICABILITY_RULE_V1_SCHEMA = _applicability_rule("task", "task_kinds")
_APPLICABILITY_RULE_V2_SCHEMA = _applicability_rule("intent", "intent_kinds")
_APPLICABILITY_RULE_SCHEMA = _APPLICABILITY_RULE_V2_SCHEMA

_PROVENANCE_SUMMARY_SCHEMA = _object(
    {
        "field_path": _string(),
        "provenance_class": _enum("source_backed", "human_judgment", "server_derived"),
        "evidence_digest": _nullable(_digest()),
        "author_issuer": _nullable(_string()),
        "author_subject": _nullable(_string()),
    }
)

_SEMANTIC_TEST_SUMMARY_SCHEMA = _object(
    {
        "test_id": _string(),
        "canonical_input_digest": _digest(),
        "expected_result_digest": _digest(),
        "actual_result_digest": _digest(),
        "passed": _boolean(),
    }
)

_EVENT_PAYLOAD_SCHEMA = _object(
    {
        "initial_freshness_basis": _nullable(_enum("connector_verified", "revision_pinned_only")),
        "retention_floor_days": _nullable(_number()),
        "legal_hold_active": _nullable(_boolean()),
        "artifact_semantics_digest": _nullable(_digest()),
        "hold_id": _nullable(_uuid()),
        "reason_code": _nullable(_string()),
        "authority_evidence_digest": _nullable(_digest()),
        "placed_at": _nullable(_timestamp()),
        "released_at": _nullable(_timestamp()),
        "prior_deadline": _nullable(_timestamp()),
        "later_deadline": _nullable(_timestamp()),
    }
)

_DELTA_COUNTER_SCHEMA = _object(
    {
        "delta_code": _enum(*DELTA_CODES),
        "explained_count": _number(),
        "unexplained_count": _number(),
    }
)

# ---------------------------------------------------------------------------
# Profile literals and their schemas.
# ---------------------------------------------------------------------------

SOURCE_APPROVAL_CLAIM_PROFILE = "arc_source_approval_claim_v1"
SOURCE_VERIFIER_ATTESTATION_PROFILE = "arc_source_verifier_attestation_v1"
SOURCE_APPROVAL_EVIDENCE_PROFILE = "arc_source_approval_evidence_v1"
FIELD_PROVENANCE_PROFILE = "arc_field_provenance_v1"
APPROVAL_REVIEW_PACKAGE_PROFILE = "arc_approval_review_package_v1"
ARTIFACT_REVISION_PROFILE = "arc_artifact_revision_v1"
ACTOR_SEPARATION_PROFILE = "arc_actor_separation_v1"
APPROVAL_VERIFIER_ENROLLMENT_PROFILE = "arc_approval_verifier_enrollment_v1"
APPROVAL_PROVIDER_ASSERTION_PROFILE = "arc_approval_provider_assertion_v1"
OPERATIONAL_EVENT_PROFILE = "arc_operational_event_v1"
OBSERVATION_COHORT_PROFILE = "arc_observation_cohort_v1"
OBSERVATION_QUALIFICATION_PROFILE = "arc_observation_qualification_v1"
OBSERVATION_REPLAY_CORPUS_PROFILE = "arc_observation_replay_corpus_v1"

# The three families the Intent rename split. V1 verifies, V2 authors; the
# unsuffixed name is the active one. A digest-only family is deliberately
# not versioned here -- a review package or an artifact revision that only
# carries the digest of an object does not change shape because that
# object's profile did.
OBSERVATION_CLASS_PREDICATE_V1_PROFILE = "arc_observation_class_predicate_v1"
OBSERVATION_CLASS_PREDICATE_V2_PROFILE = "arc_observation_class_predicate_v2"
OBSERVATION_CLASS_PREDICATE_PROFILE = OBSERVATION_CLASS_PREDICATE_V2_PROFILE

EXPECTED_IMPACT_ENVELOPE_V1_PROFILE = "arc_expected_impact_envelope_v1"
EXPECTED_IMPACT_ENVELOPE_V2_PROFILE = "arc_expected_impact_envelope_v2"
EXPECTED_IMPACT_ENVELOPE_PROFILE = EXPECTED_IMPACT_ENVELOPE_V2_PROFILE

ARTIFACT_SEMANTICS_V1_PROFILE = "arc_artifact_semantics_v1"
ARTIFACT_SEMANTICS_V2_PROFILE = "arc_artifact_semantics_v2"
ARTIFACT_SEMANTICS_PROFILE = ARTIFACT_SEMANTICS_V2_PROFILE

#: Every literal this package can verify, both versions of a split family
#: included. Membership here is not permission to write one.
AUTHORING_PROFILES: frozenset[str] = frozenset(
    {
        SOURCE_APPROVAL_CLAIM_PROFILE,
        SOURCE_VERIFIER_ATTESTATION_PROFILE,
        SOURCE_APPROVAL_EVIDENCE_PROFILE,
        OBSERVATION_CLASS_PREDICATE_V1_PROFILE,
        OBSERVATION_CLASS_PREDICATE_V2_PROFILE,
        EXPECTED_IMPACT_ENVELOPE_V1_PROFILE,
        EXPECTED_IMPACT_ENVELOPE_V2_PROFILE,
        FIELD_PROVENANCE_PROFILE,
        ARTIFACT_SEMANTICS_V1_PROFILE,
        ARTIFACT_SEMANTICS_V2_PROFILE,
        APPROVAL_REVIEW_PACKAGE_PROFILE,
        ARTIFACT_REVISION_PROFILE,
        ACTOR_SEPARATION_PROFILE,
        APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
        APPROVAL_PROVIDER_ASSERTION_PROFILE,
        OPERATIONAL_EVENT_PROFILE,
        OBSERVATION_COHORT_PROFILE,
        OBSERVATION_QUALIFICATION_PROFILE,
        OBSERVATION_REPLAY_CORPUS_PROFILE,
    }
)

#: What a new authoring write may emit. The three superseded V1 literals are
#: absent, which is the whole mechanism: a writer resolves its profile from
#: this set, so authoring V1 is not a policy anybody has to remember to
#: enforce -- there is no name for it to reach.
ACTIVE_AUTHORING_PROFILES: frozenset[str] = AUTHORING_PROFILES - {
    OBSERVATION_CLASS_PREDICATE_V1_PROFILE,
    EXPECTED_IMPACT_ENVELOPE_V1_PROFILE,
    ARTIFACT_SEMANTICS_V1_PROFILE,
}

_SOURCE_APPROVAL_CLAIM_SCHEMA = _profile(
    SOURCE_APPROVAL_CLAIM_PROFILE,
    {
        "source_system": _string(),
        "source_revision_locator": _string(),
        "source_content_digest_algorithm": _enum("sha256"),
        "source_content_digest": _digest(),
        "source_content_type": _string(),
        "approval_locator": _string(),
        "approving_authority_issuer": _string(),
        "approving_authority_subject": _string(),
        "approval_scope": _string(),
        "approved_at": _timestamp(),
        "expires_at": _timestamp(),
    },
)

_SOURCE_VERIFIER_ATTESTATION_SCHEMA = _profile(
    SOURCE_VERIFIER_ATTESTATION_PROFILE,
    {
        "attestation_id": _uuid(),
        "provider_id": _string(),
        "provider_configuration_digest": _digest(),
        "claim_digest": _digest(),
        "approving_authority_issuer": _string(),
        "approving_authority_subject": _string(),
        "source_system": _string(),
        "approval_scope": _string(),
        "issued_at": _timestamp(),
        "expires_at": _timestamp(),
    },
)

_SOURCE_APPROVAL_EVIDENCE_SCHEMA = _profile(
    SOURCE_APPROVAL_EVIDENCE_PROFILE,
    {
        "evidence_id": _uuid(),
        "claim": _SOURCE_APPROVAL_CLAIM_SCHEMA,
        "claim_digest": _digest(),
        "verification_method": _enum("source_signed", "verifier_attested"),
        "verifier_id": _string(),
        "signature": _nullable(_string()),
        "verifier_attestation": _nullable(_SOURCE_VERIFIER_ATTESTATION_SCHEMA),
        "admission_method": _enum("configured_connector", "authorized_upload"),
        "connector_id": _nullable(_string()),
        "admitted_at": _timestamp(),
        "admitted_by_issuer": _string(),
        "admitted_by_subject": _string(),
        "verified_at": _timestamp(),
        "idempotency_key_digest": _digest(),
        "admission_request_payload_digest": _digest(),
    },
)

def _expected_impact_envelope(literal: str, item: Schema) -> Schema:
    return _profile(
        literal,
        {
            "envelope_id": _uuid(),
            "proposal_id": _uuid(),
            "proposal_version": _number(),
            "items": _array(item, kind="ordered", order_key="item_id", min_items=1),
            "author_issuer": _string(),
            "author_subject": _string(),
            "created_at": _timestamp(),
        },
    )


# An envelope's own fields did not change; what changed is the predicate its
# items embed, so the version of the outer profile is what tells a verifier
# which predicate shape to expect inside. That is why the envelope gets a V2
# at all despite having no renamed field of its own.
_EXPECTED_IMPACT_ENVELOPE_V1_SCHEMA = _expected_impact_envelope(
    EXPECTED_IMPACT_ENVELOPE_V1_PROFILE, _ENVELOPE_ITEM_V1_SCHEMA
)
_EXPECTED_IMPACT_ENVELOPE_V2_SCHEMA = _expected_impact_envelope(
    EXPECTED_IMPACT_ENVELOPE_V2_PROFILE, _ENVELOPE_ITEM_V2_SCHEMA
)
_EXPECTED_IMPACT_ENVELOPE_SCHEMA = _EXPECTED_IMPACT_ENVELOPE_V2_SCHEMA

_FIELD_PROVENANCE_SCHEMA = _profile(
    FIELD_PROVENANCE_PROFILE,
    {
        "field_path": _string(),
        "provenance_class": _enum("source_backed", "human_judgment", "server_derived"),
        "source_anchor": _nullable(_string()),
        "quoted_excerpt_digest": _nullable(_digest()),
        "author_issuer": _nullable(_string()),
        "author_subject": _nullable(_string()),
        "author_role": _nullable(_string()),
        "derivation_profile": _nullable(_string()),
    },
)

def _artifact_semantics(literal: str, summary_kind: str, rule: Schema) -> Schema:
    return _profile(
        literal,
        {
            "projection_schema_version": _number(),
            "materialiser_profile": _string(),
            "materialiser_version": _string(),
            "applicability_baseline_version": _string(),
            "artifact_id": _uuid(),
            "revision_id": _uuid(),
            "kind": _enum("directive_bundle", summary_kind),
            "owning_scope": _enum("global", "tenant"),
            "owning_tenant_id": _nullable(_uuid()),
            "visibility": _enum("standard", "restricted"),
            "source_system": _string(),
            "source_revision_locator": _string(),
            "source_content_digest": _digest(),
            "source_approval_evidence_digest": _digest(),
            "directives": _array(_DIRECTIVE_SCHEMA, kind="ordered", order_key="directive_id"),
            "applicability": _array(rule, kind="ordered", order_key="rule_id"),
            "detail_audience": _enum("agent_only", "human_only", "agent_and_human"),
            "review_expires_at": _timestamp(),
            "content_classification": _enum("public", "internal", "confidential"),
            "approved_retention_floor_days": _number(),
            "initial_freshness_basis": _enum("connector_verified", "revision_pinned_only"),
            "reviewed_baseline_revision_id": _nullable(_uuid()),
        },
    )


# `task_summary_template` is a V1 artifact *kind* value, not a relational
# `arc_artifacts.kind`: no migration rewrites it, and a V1 instance carrying
# it keeps verifying here forever.
_ARTIFACT_SEMANTICS_V1_SCHEMA = _artifact_semantics(
    ARTIFACT_SEMANTICS_V1_PROFILE, "task_summary_template", _APPLICABILITY_RULE_V1_SCHEMA
)
_ARTIFACT_SEMANTICS_V2_SCHEMA = _artifact_semantics(
    ARTIFACT_SEMANTICS_V2_PROFILE, "intent_summary_template", _APPLICABILITY_RULE_V2_SCHEMA
)
_ARTIFACT_SEMANTICS_SCHEMA = _ARTIFACT_SEMANTICS_V2_SCHEMA

_APPROVAL_REVIEW_PACKAGE_SCHEMA = _profile(
    APPROVAL_REVIEW_PACKAGE_PROFILE,
    {
        "artifact_semantics_digest": _digest(),
        "source_approval_evidence_digest": _digest(),
        "field_provenance": _array(_PROVENANCE_SUMMARY_SCHEMA, kind="ordered", order_key="field_path"),
        "semantic_tests": _array(_SEMANTIC_TEST_SUMMARY_SCHEMA, kind="ordered", order_key="test_id"),
        "risk_classification": _enum(*RISK_CLASSIFICATIONS),
        "risk_algorithm_version": _string(),
        "expected_impact_envelope_digest": _digest(),
        "baseline_diff_digest": _digest(),
        "proposal_id": _uuid(),
        "proposal_version": _number(),
        "submitted_by_issuer": _string(),
        "submitted_by_subject": _string(),
        "submitted_at": _timestamp(),
    },
)

_ARTIFACT_REVISION_SCHEMA = _profile(
    ARTIFACT_REVISION_PROFILE,
    {
        "artifact_id": _uuid(),
        "revision_id": _uuid(),
        "artifact_semantics_digest": _digest(),
        "review_package_digest": _digest(),
        "actor_separation_profile": _const(ACTOR_SEPARATION_PROFILE),
    },
)

_ACTOR_SEPARATION_SCHEMA = _profile(
    ACTOR_SEPARATION_PROFILE,
    {
        "risk_classification": _enum(*RISK_CLASSIFICATIONS),
        "submitter_issuer": _string(),
        "submitter_subject": _string(),
        "approver_issuer": _string(),
        "approver_subject": _string(),
        "accepter_issuer": _nullable(_string()),
        "accepter_subject": _nullable(_string()),
        "activator_issuer": _string(),
        "activator_subject": _string(),
        "required_distinct_count": _number(),
        "satisfied": _boolean(),
    },
)

_APPROVAL_VERIFIER_ENROLLMENT_SCHEMA = _profile(
    APPROVAL_VERIFIER_ENROLLMENT_PROFILE,
    {
        "enrollment_challenge_id": _uuid(),
        "nonce": _string(),
        "verifier_id": _string(),
        "binding_kind": _enum("exact_principal", "provider_delegated"),
        "principal_issuer": _nullable(_string()),
        "principal_subject": _nullable(_string()),
        "provider_allowed_principal_issuer": _nullable(_string()),
        "scope_kind": _enum("global", "tenant"),
        "target_tenant_id": _nullable(_uuid()),
        "allowed_evidence_types": _array(_enum("artifact_activation", "exception_approval"), kind="set", min_items=1),
        "signature_algorithm": _enum("Ed25519"),
        "key_digest": _digest(),
        "valid_from": _timestamp(),
        "valid_to": _timestamp(),
        "issued_at": _timestamp(),
        "expires_at": _timestamp(),
    },
)

_APPROVAL_PROVIDER_ASSERTION_SCHEMA = _profile(
    APPROVAL_PROVIDER_ASSERTION_PROFILE,
    {
        "assertion_id": _uuid(),
        "provider_id": _string(),
        "provider_configuration_digest": _digest(),
        "approval_challenge_id": _uuid(),
        "approval_evidence_digest": _digest(),
        "principal_issuer": _string(),
        "principal_subject": _string(),
        "issued_at": _timestamp(),
        "expires_at": _timestamp(),
    },
)

_OPERATIONAL_EVENT_SCHEMA = _profile(
    OPERATIONAL_EVENT_PROFILE,
    {
        "event_id": _uuid(),
        "artifact_id": _uuid(),
        "revision_id": _uuid(),
        "sequence": _number(),
        "event_type": _enum(
            "operational_state_initialized",
            "freshness_downgraded",
            "legal_hold_placed",
            "legal_hold_released",
            "retention_extended",
        ),
        "event_payload": _EVENT_PAYLOAD_SCHEMA,
        "actor_issuer": _string(),
        "actor_subject": _string(),
        "actor_role": _enum("system", "human"),
        "authorization_decision_reference": _string(),
        "authority_evidence_digest": _digest(),
        "idempotency_key_digest": _digest(),
        "previous_event_digest": _nullable(_digest()),
        "signer_key_id": _string(),
        "created_at": _timestamp(),
    },
)

_OBSERVATION_COHORT_SCHEMA = _profile(
    OBSERVATION_COHORT_PROFILE,
    {
        "cohort_id": _uuid(),
        "risk_classification": _enum(*RISK_CLASSIFICATIONS),
        "scope_predicate_digest": _digest(),
        "tenant_membership_digest": _digest(),
        "eligibility_predicate_digest": _digest(),
        "frozen_at": _timestamp(),
        "window_started_at": _timestamp(),
        "window_deadline": _timestamp(),
    },
)

_OBSERVATION_QUALIFICATION_SCHEMA = _profile(
    OBSERVATION_QUALIFICATION_PROFILE,
    {
        "qualification_id": _uuid(),
        "idempotency_key_digest": _digest(),
        "candidate_review_package_digest": _digest(),
        "candidate_revision_id": _uuid(),
        "proposal_id": _uuid(),
        "proposal_version": _number(),
        "risk_classification": _enum(*RISK_CLASSIFICATIONS),
        "risk_algorithm_version": _string(),
        "baseline_revision_id": _nullable(_uuid()),
        "selection_engine_version": _string(),
        "engine_configuration_version": _string(),
        "cohort_id": _uuid(),
        "cohort_digest": _digest(),
        "window_started_at": _timestamp(),
        "window_ended_at": _timestamp(),
        "eligible_count": _number(),
        "observed_count": _number(),
        "expected_impact_envelope_digest": _digest(),
        "counters_by_delta_code": _array(_DELTA_COUNTER_SCHEMA, kind="ordered", order_key="delta_code"),
        "unexplained_count": _number(),
        "out_of_envelope_count": _number(),
        "replay_corpus_digest": _nullable(_digest()),
        "replay_result_digest": _nullable(_digest()),
        "qualification_algorithm_version": _string(),
        "computed_decision": _enum("qualified", "qualified_low_traffic", "insufficient", "failed"),
        "reason_codes": _array(_string(), kind="ordered"),
        "accepted_by_issuer": _nullable(_string()),
        "accepted_by_subject": _nullable(_string()),
        "accepted_by_role": _nullable(_string()),
        "accepted_at": _nullable(_timestamp()),
        "acceptance_audit_reference": _nullable(_string()),
        "expires_at": _nullable(_timestamp()),
    },
)

_OBSERVATION_REPLAY_CORPUS_SCHEMA = _profile(
    OBSERVATION_REPLAY_CORPUS_PROFILE,
    {
        "corpus_id": _uuid(),
        "generator_version": _string(),
        "generator_input_digest": _digest(),
        "canonical_corpus_digest": _digest(),
        "fixture_class_count": _number(),
        "scope": _enum("global", "tenant"),
        "target_tenant_id": _nullable(_uuid()),
        "approving_authority_issuer": _string(),
        "approving_authority_subject": _string(),
        "approved_at": _timestamp(),
        "expires_at": _timestamp(),
    },
)

#: Exact-literal dispatch. A caller holding a profile string resolves its
#: shape here or is refused; there is no "try the new shape, fall back to
#: the old one" path, because a V1 instance and a V2 instance of a split
#: family differ by one renamed key and a fallback would silently accept
#: either under whichever name it tried second.
SCHEMA_BY_PROFILE: dict[str, Schema] = {
    SOURCE_APPROVAL_CLAIM_PROFILE: _SOURCE_APPROVAL_CLAIM_SCHEMA,
    SOURCE_VERIFIER_ATTESTATION_PROFILE: _SOURCE_VERIFIER_ATTESTATION_SCHEMA,
    SOURCE_APPROVAL_EVIDENCE_PROFILE: _SOURCE_APPROVAL_EVIDENCE_SCHEMA,
    OBSERVATION_CLASS_PREDICATE_V1_PROFILE: _OBSERVATION_CLASS_PREDICATE_V1_SCHEMA,
    OBSERVATION_CLASS_PREDICATE_V2_PROFILE: _OBSERVATION_CLASS_PREDICATE_V2_SCHEMA,
    EXPECTED_IMPACT_ENVELOPE_V1_PROFILE: _EXPECTED_IMPACT_ENVELOPE_V1_SCHEMA,
    EXPECTED_IMPACT_ENVELOPE_V2_PROFILE: _EXPECTED_IMPACT_ENVELOPE_V2_SCHEMA,
    FIELD_PROVENANCE_PROFILE: _FIELD_PROVENANCE_SCHEMA,
    ARTIFACT_SEMANTICS_V1_PROFILE: _ARTIFACT_SEMANTICS_V1_SCHEMA,
    ARTIFACT_SEMANTICS_V2_PROFILE: _ARTIFACT_SEMANTICS_V2_SCHEMA,
    APPROVAL_REVIEW_PACKAGE_PROFILE: _APPROVAL_REVIEW_PACKAGE_SCHEMA,
    ARTIFACT_REVISION_PROFILE: _ARTIFACT_REVISION_SCHEMA,
    ACTOR_SEPARATION_PROFILE: _ACTOR_SEPARATION_SCHEMA,
    APPROVAL_VERIFIER_ENROLLMENT_PROFILE: _APPROVAL_VERIFIER_ENROLLMENT_SCHEMA,
    APPROVAL_PROVIDER_ASSERTION_PROFILE: _APPROVAL_PROVIDER_ASSERTION_SCHEMA,
    OPERATIONAL_EVENT_PROFILE: _OPERATIONAL_EVENT_SCHEMA,
    OBSERVATION_COHORT_PROFILE: _OBSERVATION_COHORT_SCHEMA,
    OBSERVATION_QUALIFICATION_PROFILE: _OBSERVATION_QUALIFICATION_SCHEMA,
    OBSERVATION_REPLAY_CORPUS_PROFILE: _OBSERVATION_REPLAY_CORPUS_SCHEMA,
}
