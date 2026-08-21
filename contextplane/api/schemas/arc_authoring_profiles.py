"""The four profile-named alias components (plus the derived
`ArtifactSemanticsPartial`) and the nested shapes they need.

See `arc_authoring.py`'s module docstring for the full derive-vs-assert
reasoning. In short: `SourceApprovalClaim`, `ArtifactSemantics`,
`ObservationClassPredicate`, and `ExpectedImpactEnvelope` are hand-typed to
mirror a canonicalization profile's closed field set from
`contextplane.arc.schemas.authoring_profile_shapes`, and
`PROFILE_ALIASED_COMPONENTS` drives a conformance assertion (in
`tests/conformance/test_arc_authoring_schemas.py`) that each one's
top-level field-name set equals `authoring_profiles.profile_field_names()`
for the matching profile literal -- so a field added to or removed from
either side fails a test rather than silently drifting.

The three nested, non-profile shapes below (`ArtifactDirective`,
`ArtifactApplicabilityRule`, `ExpectedImpactEnvelopeItem`) are hand-
transcribed from the corresponding private schema constants in
`authoring_profile_shapes.py` (`_DIRECTIVE_SCHEMA`, `_APPLICABILITY_RULE_
SCHEMA`, `_ENVELOPE_ITEM_SCHEMA`) and cross-checked by hand rather than by
an automated assertion, because `profile_field_names()` only covers a
profile's own top-level field set, not shapes nested inside it. This is a
real residual gap, not a hidden one -- a reasonable follow-up would export
a field-name helper for named nested shapes too.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from contextplane.api.schemas.arc_authoring_enums import DeltaCode, OwningScope
from contextplane.api.schemas.arc_authoring_shared import Digest, _ClosedModel
from contextplane.arc import (
    ARTIFACT_SEMANTICS_PROFILE,
    EXPECTED_IMPACT_ENVELOPE_PROFILE,
    OBSERVATION_CLASS_PREDICATE_PROFILE,
    SOURCE_APPROVAL_CLAIM_PROFILE,
)


class SourceApprovalClaim(_ClosedModel):
    """Exactly the closed `arc_source_approval_claim_v1` profile object."""

    profile: Literal["arc_source_approval_claim_v1"] = "arc_source_approval_claim_v1"
    source_system: str
    source_revision_locator: str
    source_content_digest_algorithm: Literal["sha256"] = "sha256"
    source_content_digest: Digest
    source_content_type: str
    approval_locator: str
    approving_authority_issuer: str
    approving_authority_subject: str
    approval_scope: str
    approved_at: datetime.datetime
    expires_at: datetime.datetime


class ArtifactDirective(_ClosedModel):
    """Nested element of `ArtifactSemantics.directives[]`; mirrors the
    private `_DIRECTIVE_SCHEMA` constant. See this module's docstring for
    the residual-gap note on nested-shape parity.
    """

    directive_id: uuid.UUID
    directive_type: Literal["citation_only", "verify_before_action"]
    compact_statement_plaintext: str
    compact_statement_plaintext_digest: Digest
    source_anchor: str
    conflict_key_schema_version: int
    conflict_key_namespace: str | None = None
    conflict_key_subject_selector: str | None = None
    conflict_key_operation: str | None = None
    conflict_key_action_class: str | None = None
    conflict_key_target_selector: str | None = None
    conflict_key_modality: str | None = None
    conflict_key_constraint_operator: str | None = None
    conflict_key_constraint_value: str | None = None
    conflict_subject_digest: Digest | None = None
    delegable_exception: bool
    satisfaction_mode: Literal["authorized_retrieval", "signed_result"] | None = None
    verification_max_age_seconds: int | None = None
    accepted_verifier_classes: list[str] | None = None
    accepted_verifier_ids: list[str] | None = None
    required_evidence_type: str | None = None
    created_at: datetime.datetime


class ArtifactApplicabilityRule(_ClosedModel):
    """Nested element of `ArtifactSemantics.applicability[]`; mirrors the
    private `_APPLICABILITY_RULE_SCHEMA` constant. Same residual-gap note
    as `ArtifactDirective`.
    """

    rule_id: uuid.UUID
    scope: Literal["global", "tenant", "domain", "entity", "intent"]
    target_tenant_id: uuid.UUID | None = None
    entity_ids: list[uuid.UUID] | None = None
    entity_labels: list[str] | None = None
    domain_ids: list[str] | None = None
    intent_kinds: list[str] | None = None
    action_classes: list[str] | None = None
    environments: list[str] | None = None
    data_sensitivity_tiers: list[str] | None = None
    effective_from: datetime.datetime | None = None
    effective_until: datetime.datetime | None = None
    is_mandatory: bool


class ArtifactSemantics(_ClosedModel):
    """Exactly the closed `arc_artifact_semantics_v2` profile object."""

    profile: Literal["arc_artifact_semantics_v2"] = "arc_artifact_semantics_v2"
    projection_schema_version: int
    materialiser_profile: str
    materialiser_version: str
    applicability_baseline_version: str
    artifact_id: uuid.UUID
    revision_id: uuid.UUID
    kind: Literal["directive_bundle", "intent_summary_template"]
    owning_scope: OwningScope
    owning_tenant_id: uuid.UUID | None = None
    visibility: Literal["standard", "restricted"]
    source_system: str
    source_revision_locator: str
    source_content_digest: Digest
    source_approval_evidence_digest: Digest
    directives: list[ArtifactDirective]
    applicability: list[ArtifactApplicabilityRule]
    detail_audience: Literal["agent_only", "human_only", "agent_and_human"]
    review_expires_at: datetime.datetime
    content_classification: Literal["public", "internal", "confidential"]
    approved_retention_floor_days: int
    initial_freshness_basis: Literal["connector_verified", "revision_pinned_only"]
    reviewed_baseline_revision_id: uuid.UUID | None = None


def _make_partial(model: type[BaseModel], name: str) -> type[BaseModel]:
    """Derive a patch-shaped sibling of `model`: same field set, every
    field optional, defaulting to `None`. Used for `ArtifactSemanticsPartial`
    so its field set is always exactly `ArtifactSemantics`'s by
    construction -- the same anti-drift property `PROFILE_ALIASED_COMPONENTS`
    gives the four top-level aliases, applied here to a relationship
    internal to this module rather than across a module boundary.
    """
    annotations: dict[str, Any] = {}
    for field_name, info in model.model_fields.items():
        # `FieldInfo.annotation` is typed `type[Any] | None` in the pydantic
        # stubs (it is only ever `None` for an unresolved forward reference,
        # which does not happen here), so the `is not None` guard is what
        # lets the `| None` below type-check rather than being applied to a
        # statically-`None`-shaped operand.
        annotations[field_name] = (info.annotation | None) if info.annotation is not None else None
    defaults = {field_name: None for field_name in model.model_fields}
    # `__module__` must be set explicitly: a `class` statement gets it for free
    # from the compiler, but a bare `type(...)` call otherwise leaves pydantic's
    # metaclass to infer one from the call stack, which does not land on this
    # module -- and `arc_authoring.py`'s COMPONENTS registry filters by exactly
    # this attribute, so a wrong value here would silently drop this model from
    # every conformance check that walks that registry.
    return type(name, (_ClosedModel,), {"__annotations__": annotations, "__module__": __name__, **defaults})


ArtifactSemanticsPartial = _make_partial(ArtifactSemantics, "ArtifactSemanticsPartial")


class ObservationClassPredicate(_ClosedModel):
    """Exactly the closed `arc_observation_class_predicate_v2` profile object."""

    profile: Literal["arc_observation_class_predicate_v2"] = "arc_observation_class_predicate_v2"
    intent_kind: list[str] | None = None
    requested_action_classes: list[str] | None = None
    environment: list[str] | None = None
    data_sensitivity_tier: list[str] | None = None
    entity_ids: list[uuid.UUID] | None = None
    domain_ids: list[str] | None = None


class ExpectedImpactEnvelopeItem(_ClosedModel):
    """Nested element of `ExpectedImpactEnvelope.items[]`; mirrors the
    private `_ENVELOPE_ITEM_SCHEMA` constant. Same residual-gap note as
    `ArtifactDirective`.
    """

    item_id: str
    delta_code: DeltaCode
    class_predicate: ObservationClassPredicate
    minimum_count: int
    maximum_count: int | None = None
    rationale_code: str


class ExpectedImpactEnvelope(_ClosedModel):
    """Exactly the closed `arc_expected_impact_envelope_v2` profile object."""

    profile: Literal["arc_expected_impact_envelope_v2"] = "arc_expected_impact_envelope_v2"
    envelope_id: uuid.UUID
    proposal_id: uuid.UUID
    proposal_version: int
    items: list[ExpectedImpactEnvelopeItem] = Field(min_length=1)
    author_issuer: str
    author_subject: str
    created_at: datetime.datetime


# Drives the top-level field-name-set parity assertion described in this
# module's docstring: for each pair, the wire model's declared field names
# must equal `authoring_profiles.profile_field_names(profile)`.
PROFILE_ALIASED_COMPONENTS: dict[type[BaseModel], str] = {
    SourceApprovalClaim: SOURCE_APPROVAL_CLAIM_PROFILE,
    ArtifactSemantics: ARTIFACT_SEMANTICS_PROFILE,
    ObservationClassPredicate: OBSERVATION_CLASS_PREDICATE_PROFILE,
    ExpectedImpactEnvelope: EXPECTED_IMPACT_ENVELOPE_PROFILE,
}


__all__ = [
    "PROFILE_ALIASED_COMPONENTS",
    "ArtifactApplicabilityRule",
    "ArtifactDirective",
    "ArtifactSemantics",
    "ArtifactSemanticsPartial",
    "ExpectedImpactEnvelope",
    "ExpectedImpactEnvelopeItem",
    "ObservationClassPredicate",
    "SourceApprovalClaim",
]
