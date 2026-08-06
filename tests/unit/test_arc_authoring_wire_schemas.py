"""Unit tests for the authoring-surface wire contract's own mechanics
(`registry/api/schemas/arc_authoring*.py`): closed-model refusal, required-
field enforcement, enum membership, and the two derive-vs-hand-type
relationships Appendix A's transcription draws between a wire component and
its canonicalization profile.

`tests/conformance/test_arc_authoring_schemas.py` already owns the contract-
drift half of this surface -- pinning every one of the ~70 components'
generated JSON Schema to a checked-in snapshot, sweeping the whole
`COMPONENTS` registry to prove every one of them rejects an unknown field,
and walking `PROFILE_ALIASED_COMPONENTS` to assert each of the four hand-
typed models' field-*name-set* equals `profile_field_names()` for the
profile it mirrors. None of that is repeated here. What this file adds is
mechanism-level: that the shared `_ClosedModel` base actually enforces
`extra="forbid"` (not just configures itself to, checked directly on the
base rather than swept across every subclass a second time), that a
representative model from each sibling module enforces its required fields
and enum membership, that the two custom cross-field validators
(`_ScopeColumnsMixin._check_scope_columns`, `EnrollmentChallengeRequest.
_check_binding_kind`) actually fire on both branches, that the three
enums built by import from another module's tuple (`RiskClassification`,
`DeltaCode`, `SignatureAlgorithm`) faithfully reproduce the tuple they were
built from, and that `_make_partial`'s derivation of `ArtifactSemanticsPartial`
from `ArtifactSemantics.model_fields` actually produces an all-optional
sibling with the same field set -- the specific "derived, not hand-typed"
relationship `arc_authoring_profiles.py`'s own docstring calls out, which
the conformance suite's registry sweep never singles out on its own.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from registry.api.schemas import arc_authoring as aa
from registry.api.schemas import arc_authoring_shared as shared
from registry.arc.schemas.authoring_profile_shapes import DELTA_CODES, RISK_CLASSIFICATIONS, SCHEMA_BY_PROFILE

_UUID = "11111111-1111-1111-1111-111111111111"
_DIGEST = "a" * 64
_TS = "2024-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Closed models: the base class's own mechanism, then a few concrete,
# by-name spot checks -- not the exhaustive `COMPONENTS` sweep, which is the
# conformance suite's job (see this module's docstring).
# ---------------------------------------------------------------------------


def test_closed_model_base_class_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        shared._ClosedModel.model_validate({"__unit_test_unknown__": 1})
    assert "extra_forbidden" in {err["type"] for err in exc_info.value.errors()}


@pytest.mark.parametrize(
    "model",
    [aa.EmptyRequest, aa.ReasonRequest, aa.ArtifactFamilyCreate, aa.DetachedSignatureProof],
    ids=lambda m: m.__name__,
)
def test_representative_components_forbid_an_unknown_field(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate({"__unit_test_unknown__": 1})
    assert "extra_forbidden" in {err["type"] for err in exc_info.value.errors()}


def test_empty_request_accepts_an_empty_body() -> None:
    assert aa.EmptyRequest.model_validate({}).model_dump() == {}


# ---------------------------------------------------------------------------
# Required-field enforcement.
# ---------------------------------------------------------------------------


def test_artifact_family_create_requires_every_declared_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        aa.ArtifactFamilyCreate.model_validate({})
    missing = {err["loc"][0] for err in exc_info.value.errors() if err["type"] == "missing"}
    assert missing == {"owning_scope", "slug", "kind", "title"}

    valid = aa.ArtifactFamilyCreate.model_validate(
        {"owning_scope": "global", "slug": "widget", "kind": "standard", "title": "Widget"}
    )
    assert valid.slug == "widget"


def test_activation_eligibility_response_requires_exactly_ten_predicates() -> None:
    status = aa.ActivationPredicateStatus(name=aa.ActivationPredicateName.LATEST_VERSION, satisfied=True)
    with pytest.raises(ValidationError, match="at least 10"):
        aa.ActivationEligibilityResponse.model_validate({"eligible": True, "predicates": [status] * 9})
    with pytest.raises(ValidationError, match="at most 10"):
        aa.ActivationEligibilityResponse.model_validate({"eligible": True, "predicates": [status] * 11})
    ok = aa.ActivationEligibilityResponse.model_validate({"eligible": True, "predicates": [status] * 10})
    assert len(ok.predicates) == 10


def test_expected_impact_envelope_requires_at_least_one_item() -> None:
    predicate = aa.ObservationClassPredicate()
    item = aa.ExpectedImpactEnvelopeItem(
        item_id="item-a",
        delta_code=aa.DeltaCode.NEWLY_SELECTED,  # type: ignore[attr-defined]  # DeltaCode is built dynamically; see arc_authoring_enums.py
        class_predicate=predicate,
        minimum_count=1,
        rationale_code="baseline",
    )
    with pytest.raises(ValidationError, match="at least 1"):
        aa.ExpectedImpactEnvelope.model_validate(
            {
                "envelope_id": _UUID,
                "proposal_id": _UUID,
                "proposal_version": 1,
                "items": [],
                "author_issuer": "iss",
                "author_subject": "sub",
                "created_at": _TS,
            }
        )
    ok = aa.ExpectedImpactEnvelope.model_validate(
        {
            "envelope_id": _UUID,
            "proposal_id": _UUID,
            "proposal_version": 1,
            "items": [item],
            "author_issuer": "iss",
            "author_subject": "sub",
            "created_at": _TS,
        }
    )
    assert len(ok.items) == 1


def test_source_connector_registration_enforces_the_max_bytes_ceiling() -> None:
    base = {
        "owning_scope": "global",
        "connector_id": "conn-1",
        "allowed_schemes": ["https"],
        "allowed_hosts": ["example.com"],
        "allowed_media_types": ["text/markdown"],
        "allowed_verifier_ids": ["verifier-1"],
    }
    with pytest.raises(ValidationError, match="less than or equal to 10485760"):
        aa.SourceConnectorRegistration.model_validate({**base, "max_bytes": 10_485_761})
    ok = aa.SourceConnectorRegistration.model_validate({**base, "max_bytes": 10_485_760})
    assert ok.max_bytes == 10_485_760


# ---------------------------------------------------------------------------
# Enum membership: a valid member is accepted, an out-of-set string is
# refused as an ordinary validation error naming the field.
# ---------------------------------------------------------------------------


def test_enum_typed_field_accepts_a_member_and_refuses_a_non_member() -> None:
    valid = aa.ArtifactFamilyCreate.model_validate(
        {"owning_scope": "global", "slug": "widget", "kind": "standard", "title": "Widget"}
    )
    assert valid.kind is aa.ArtifactKind.STANDARD

    with pytest.raises(ValidationError) as exc_info:
        aa.ArtifactFamilyCreate.model_validate(
            {"owning_scope": "global", "slug": "widget", "kind": "not_a_real_kind", "title": "Widget"}
        )
    assert any(err["loc"] == ("kind",) for err in exc_info.value.errors())


def test_reason_code_is_deliberately_open_not_a_closed_enum() -> None:
    """`ReasonCode` is `str`, not an enum -- Appendix A's stated source for
    it is a prose transition-reason table, not an enumerated code list.
    Any string is accepted; this is the behavior that keeps that
    deliberately open, checked directly rather than by introspecting the
    type alias.
    """
    accepted = aa.ReasonRequest.model_validate({"reason_code": "literally_anything_goes"})
    assert accepted.reason_code == "literally_anything_goes"


# ---------------------------------------------------------------------------
# Enums derived by import from a source tuple elsewhere in the schema
# layer: the derivation actually reproduces the source, not merely a
# same-shaped set someone kept in sync by hand.
# ---------------------------------------------------------------------------


def test_risk_classification_enum_reproduces_its_source_tuple_exactly() -> None:
    assert {member.value for member in aa.RiskClassification} == set(RISK_CLASSIFICATIONS)
    assert len(list(aa.RiskClassification)) == len(RISK_CLASSIFICATIONS)


def test_delta_code_enum_reproduces_its_source_tuple_exactly() -> None:
    assert {member.value for member in aa.DeltaCode} == set(DELTA_CODES)


def test_signature_algorithm_enum_reproduces_the_verifier_enrollment_schemas_enum() -> None:
    source = set(SCHEMA_BY_PROFILE["arc_approval_verifier_enrollment_v1"]["properties"]["signature_algorithm"]["enum"])
    assert {member.value for member in aa.SignatureAlgorithm} == source


# ---------------------------------------------------------------------------
# `_ScopeColumnsMixin`: both directions of the conditional-requiredness
# rule, exercised through a concrete component that mixes it in.
# ---------------------------------------------------------------------------


def test_scope_columns_mixin_requires_target_tenant_id_when_scope_is_tenant() -> None:
    with pytest.raises(ValidationError, match="required when owning_scope is 'tenant'"):
        aa.ArtifactFamilyCreate.model_validate(
            {"owning_scope": "tenant", "slug": "s", "kind": "standard", "title": "T"}
        )

    ok = aa.ArtifactFamilyCreate.model_validate(
        {"owning_scope": "tenant", "target_tenant_id": _UUID, "slug": "s", "kind": "standard", "title": "T"}
    )
    assert ok.target_tenant_id is not None


def test_scope_columns_mixin_forbids_target_tenant_id_when_scope_is_global() -> None:
    with pytest.raises(ValidationError, match="forbidden when owning_scope is 'global'"):
        aa.ArtifactFamilyCreate.model_validate(
            {"owning_scope": "global", "target_tenant_id": _UUID, "slug": "s", "kind": "standard", "title": "T"}
        )


# ---------------------------------------------------------------------------
# `EnrollmentChallengeRequest._check_binding_kind`: both binding kinds, both
# missing-required and forbidden-present halves of each.
# ---------------------------------------------------------------------------


def _enrollment_base() -> dict[str, object]:
    return {
        "owning_scope": "global",
        "evidence_types": ["artifact_activation"],
        "signature_algorithm": "Ed25519",
        "public_key_base64": "AAAA",
        "valid_from": _TS,
        "valid_to": _TS,
    }


def test_exact_principal_binding_requires_principal_issuer_and_subject() -> None:
    with pytest.raises(ValidationError, match="exact_principal binding requires"):
        aa.EnrollmentChallengeRequest.model_validate({**_enrollment_base(), "binding_kind": "exact_principal"})

    ok = aa.EnrollmentChallengeRequest.model_validate(
        {
            **_enrollment_base(),
            "binding_kind": "exact_principal",
            "principal_issuer": "iss",
            "principal_subject": "sub",
        }
    )
    assert ok.principal_subject == "sub"


def test_exact_principal_binding_forbids_provider_fields() -> None:
    with pytest.raises(ValidationError, match="exact_principal binding forbids"):
        aa.EnrollmentChallengeRequest.model_validate(
            {
                **_enrollment_base(),
                "binding_kind": "exact_principal",
                "principal_issuer": "iss",
                "principal_subject": "sub",
                "provider_id": "prov",
                "provider_allowed_principal_issuer": "prov-iss",
            }
        )


def test_provider_delegated_binding_requires_provider_fields() -> None:
    with pytest.raises(ValidationError, match="provider_delegated binding requires"):
        aa.EnrollmentChallengeRequest.model_validate({**_enrollment_base(), "binding_kind": "provider_delegated"})

    ok = aa.EnrollmentChallengeRequest.model_validate(
        {
            **_enrollment_base(),
            "binding_kind": "provider_delegated",
            "provider_id": "prov",
            "provider_allowed_principal_issuer": "prov-iss",
        }
    )
    assert ok.provider_id == "prov"


# ---------------------------------------------------------------------------
# `ApprovalProof`'s discriminated union, plus the two pattern-constrained
# scalar wire types (`Digest`, `Base64Str`).
# ---------------------------------------------------------------------------


def test_approval_proof_discriminates_on_verification_method() -> None:
    adapter: TypeAdapter[object] = TypeAdapter(shared.ApprovalProof)
    detached = adapter.validate_python(
        {"verification_method": "detached_signature", "signature_algorithm": "Ed25519", "signature_base64": "AAAA"}
    )
    assert isinstance(detached, shared.DetachedSignatureProof)

    attested = adapter.validate_python(
        {
            "verification_method": "verifier_attestation",
            "provider_id": "prov",
            "assertion_format": "jwt",
            "assertion_base64": "AAAA",
        }
    )
    assert isinstance(attested, shared.VerifierAttestationProof)

    with pytest.raises(ValidationError):
        adapter.validate_python({"verification_method": "not_a_real_method"})


def test_digest_field_rejects_a_non_sha256_shaped_string() -> None:
    ok = shared.Citation.model_validate(
        {"field_path": "title", "source_evidence_id": _UUID, "source_anchor": "anchor", "excerpt_digest": _DIGEST}
    )
    assert ok.excerpt_digest == _DIGEST

    with pytest.raises(ValidationError):
        shared.Citation.model_validate(
            {
                "field_path": "title",
                "source_evidence_id": _UUID,
                "source_anchor": "anchor",
                "excerpt_digest": "too-short",
            }
        )


def test_base64_field_rejects_a_non_base64_shaped_string() -> None:
    with pytest.raises(ValidationError):
        shared.DetachedSignatureProof.model_validate(
            {"signature_algorithm": "Ed25519", "signature_base64": "not base64 at all!!"}
        )


# ---------------------------------------------------------------------------
# `FieldProvenanceInput` vs `FieldProvenance`: the response projection adds
# exactly one field, never accepted as input.
# ---------------------------------------------------------------------------


def test_field_provenance_response_adds_author_over_the_request_shape() -> None:
    base = {"field_path": "title", "provenance_class": "human_judgment", "author_role": "editor"}
    request = shared.FieldProvenanceInput.model_validate(base)
    assert type(request).model_fields.keys() == shared.FieldProvenance.model_fields.keys() - {"author"}

    response = shared.FieldProvenance.model_validate({**base, "author": {"issuer": "iss", "subject": "sub"}})
    assert response.author is not None
    assert response.author.subject == "sub"


# ---------------------------------------------------------------------------
# `ArtifactSemanticsPartial`: derived from `ArtifactSemantics.model_fields`,
# not hand-typed -- every field optional, same field set, same module (so it
# is picked up by `COMPONENTS`).
# ---------------------------------------------------------------------------


def test_artifact_semantics_partial_is_derived_with_the_same_field_set_all_optional() -> None:
    from registry.api.schemas import arc_authoring_profiles as profiles_module

    assert aa.ArtifactSemanticsPartial.model_fields.keys() == aa.ArtifactSemantics.model_fields.keys()
    for name, info in aa.ArtifactSemanticsPartial.model_fields.items():
        assert info.default is None, name
    assert aa.ArtifactSemanticsPartial.__module__ == profiles_module.__name__

    empty = aa.ArtifactSemanticsPartial.model_validate({})
    assert empty.model_dump()["kind"] is None

    with pytest.raises(ValidationError) as exc_info:
        aa.ArtifactSemanticsPartial.model_validate({"__unit_test_unknown__": 1})
    assert "extra_forbidden" in {err["type"] for err in exc_info.value.errors()}


# ---------------------------------------------------------------------------
# `arc_authoring.py`'s own module-level constructs: the registry excludes
# the two mixin base classes, and every request component the actor-field
# walker applies to is one this module actually declared as such.
# ---------------------------------------------------------------------------


def test_components_registry_excludes_the_shared_mixin_base_classes() -> None:
    assert shared._ClosedModel not in aa.COMPONENTS.values()
    assert shared._ScopeColumnsMixin not in aa.COMPONENTS.values()


def test_request_components_are_a_subset_of_the_full_component_registry() -> None:
    assert set(aa.REQUEST_COMPONENTS) <= set(aa.COMPONENTS.values())
    assert aa.EmptyRequest in aa.REQUEST_COMPONENTS
    assert aa.ArtifactFamilyResponse not in aa.REQUEST_COMPONENTS  # response-only


# ---------------------------------------------------------------------------
# Nested, non-profile wire shapes hand-transcribed from
# `authoring_profile_shapes.py`'s private schema constants.
# ---------------------------------------------------------------------------


def test_artifact_directive_and_applicability_rule_accept_a_minimal_instance() -> None:
    directive = aa.ArtifactDirective.model_validate(
        {
            "directive_id": _UUID,
            "directive_type": "citation_only",
            "compact_statement_plaintext": "text",
            "compact_statement_plaintext_digest": _DIGEST,
            "source_anchor": "anchor",
            "conflict_key_schema_version": 1,
            "delegable_exception": False,
            "created_at": _TS,
        }
    )
    assert directive.directive_type == "citation_only"

    rule = aa.ArtifactApplicabilityRule.model_validate({"rule_id": _UUID, "scope": "global", "is_mandatory": True})
    assert rule.is_mandatory is True


def test_source_approval_claim_alias_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        aa.SourceApprovalClaim.model_validate({"__unit_test_unknown__": 1})
    assert "extra_forbidden" in {err["type"] for err in exc_info.value.errors()}


# ---------------------------------------------------------------------------
# `RESERVED_ACTOR_FIELDS` / `REFUSAL_CODE_STATUS` totality: structural
# properties the conformance suite's value-set and walker tests do not
# check on their own (see this module's docstring for what those check
# instead).
# ---------------------------------------------------------------------------


def test_reserved_actor_fields_contains_the_named_actor_identity_fields() -> None:
    assert {"actor_issuer", "actor_subject", "role"} <= aa.RESERVED_ACTOR_FIELDS


def test_refusal_code_status_is_total_over_the_refusal_code_enum() -> None:
    assert set(aa.REFUSAL_CODE_STATUS) == set(aa.RefusalCode)
