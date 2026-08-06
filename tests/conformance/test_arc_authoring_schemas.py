"""Conformance gate for the authoring-surface wire contract (Appendix A.6).

`registry.api.schemas.arc_authoring` transcribes Appendix A into Pydantic
models before any route exists. This test is what keeps that transcription
honest going forward: it pins every component's generated JSON Schema to a
checked-in snapshot (so a later route task cannot quietly widen or narrow a
field it imports), validates one canonical example per REST route against
its component and, for the refusal half, against the shared REST-status
mapping, and asserts three closedness properties Appendix A itself states
but a schema file cannot self-certify:

- every component actively refuses an unknown field, not merely configures
  itself to (see `test_every_component_rejects_unknown_fields` for why
  "actively" is checked without needing a fully valid fixture);
- no request component -- at any nesting depth -- declares one of the ten
  reserved actor-field names (Appendix A.4);
- the four profile-named aliases have not drifted from the canonicalization
  profile they are supposed to mirror exactly (see
  `arc_authoring_profiles.py`'s module docstring for why this is an
  assertion rather than a generated derivation).

Every enum this module transcribes literally from Appendix A.3 is checked
against its *value set*, transcribed a second time by hand directly from
the appendix text below -- deliberately not by reading it back off the
enum under test, which would make the check pass no matter what the enum
said.
"""

from __future__ import annotations

import enum
import json
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from registry.api.schemas import arc_authoring as aa
from registry.arc.schemas.authoring_profiles import profile_field_names

_SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
_SCHEMAS_SNAPSHOT = _SNAPSHOT_DIR / "arc_authoring_schemas.json"
_EXAMPLES_SNAPSHOT = _SNAPSHOT_DIR / "arc_authoring_examples.json"

# Generic, pre-existing refusal codes (`registry.api.errors._STATUS_TO_CODE`)
# that some canonical examples use for a route with no ARC-specific code
# naming its refusal -- e.g. a plain `GET` by id, or an admin registration
# route with no dedicated conditional-requiredness code. Kept separate from
# `REFUSAL_CODE_STATUS` because these are not members of the closed
# `RefusalCode` enum; they are the REST layer's own generic fallback.
_GENERIC_REFUSAL_STATUS: dict[str, int] = {
    "not_found": 404,
    "validation_error": 422,
    "conflict": 409,
    "bad_request": 400,
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Component / snapshot parity.
# ---------------------------------------------------------------------------


def test_component_snapshot_keys_match_registry() -> None:
    """The snapshot's key set is exactly `COMPONENTS`' key set -- catches a
    component removed or renamed without the snapshot following, and a
    component added without a snapshot entry ever being generated for it.
    """
    snapshot = _load_json(_SCHEMAS_SNAPSHOT)
    assert set(snapshot.keys()) == set(aa.COMPONENTS.keys())


@pytest.mark.parametrize("name", sorted(aa.COMPONENTS.keys()))
def test_component_schema_matches_snapshot(name: str) -> None:
    """Each model's generated JSON Schema equals its snapshot entry. This is
    the assertion every later route task re-runs locally instead of
    touching `openapi.json`: if a route task's import needs a field this
    model does not have, this test fails here, not as a surprise once a
    router exists.
    """
    model = aa.COMPONENTS[name]
    snapshot = _load_json(_SCHEMAS_SNAPSHOT)
    assert model.model_json_schema() == snapshot[name]


# ---------------------------------------------------------------------------
# Profile-named aliases: field-name-set parity against the canonicalization
# profile each one mirrors (see arc_authoring_profiles.py for why this is
# an assertion rather than a generated derivation).
# ---------------------------------------------------------------------------


_PROFILE_ALIASED_PAIRS: list[tuple[type[BaseModel], str]] = sorted(
    aa.PROFILE_ALIASED_COMPONENTS.items(), key=lambda pair: pair[0].__name__
)


@pytest.mark.parametrize(("model", "profile"), _PROFILE_ALIASED_PAIRS)
def test_profile_aliased_component_matches_profile_field_names(model: type[BaseModel], profile: str) -> None:
    wire_fields = frozenset(model.model_fields.keys())
    assert wire_fields == profile_field_names(profile)


def test_field_provenance_input_has_no_author() -> None:
    """Appendix A.6: `FieldProvenanceInput` carries no `author` -- it is
    server-written from the authenticated caller, never client-supplied.
    `FieldProvenance`, the response projection, is the one place `author`
    legitimately appears.
    """
    assert "author" not in aa.FieldProvenanceInput.model_fields
    assert "author" in aa.FieldProvenance.model_fields


# ---------------------------------------------------------------------------
# Closed means closed: every component actively refuses an unknown field.
#
# This does not need a fully valid instance. Pydantic collects every
# validation error in one pass rather than short-circuiting on the first
# one, so submitting *only* a bogus key -- with every required field still
# missing -- still produces an `extra_forbidden` error for that key
# alongside the `missing` errors for everything else. That is a stronger
# proof than checking `model_config["extra"] == "forbid"`: it demonstrates
# the refusal actually fires, for every component, without hand-building
# seventy valid fixtures first.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(aa.COMPONENTS.keys()))
def test_every_component_rejects_unknown_fields(name: str) -> None:
    model = aa.COMPONENTS[name]
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate({"__unknown_field_for_conformance__": "x"})
    error_types = {err["type"] for err in exc_info.value.errors()}
    assert (
        "extra_forbidden" in error_types
    ), f"{name} did not refuse an unknown field (errors: {exc_info.value.errors()})"


# ---------------------------------------------------------------------------
# Reserved actor fields (Appendix A.4): no request component, at any
# nesting depth, may declare one of the ten reserved names. This is the
# static half of the rule; the request-time half (an actual HTTP request
# rejected for supplying one) is a later route task's test, once a route
# exists to send the request to.
# ---------------------------------------------------------------------------


def _model_field_names(model: type[BaseModel], seen: set[type[BaseModel]]) -> set[str]:
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for field_name, info in model.model_fields.items():
        names.add(field_name)
        names |= _type_field_names(info.annotation, seen)
    return names


def _type_field_names(annotation: Any, seen: set[type[BaseModel]]) -> set[str]:
    names: set[str] = set()
    origin = typing.get_origin(annotation)
    if origin is not None:
        for arg in typing.get_args(annotation):
            names |= _type_field_names(arg, seen)
        return names
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        names |= _model_field_names(annotation, seen)
    return names


@pytest.mark.parametrize("model", aa.REQUEST_COMPONENTS, ids=lambda m: m.__name__)
def test_request_components_declare_no_reserved_actor_field(model: type[BaseModel]) -> None:
    names = _model_field_names(model, seen=set())
    hit = names & aa.RESERVED_ACTOR_FIELDS
    assert not hit, f"{model.__name__} reaches reserved actor field(s) {sorted(hit)}"


def test_reserved_actor_field_walker_is_not_vacuous() -> None:
    """A walker that matches nothing passes vacuously regardless of whether
    it works. Plants a reserved name on a throwaway model (not part of
    `COMPONENTS`) and proves the walker catches it, including through one
    level of nesting -- the same shape `REQUEST_COMPONENTS` entries have.
    """

    class _PlantedLeaf(BaseModel):
        role: str

    class _PlantedParent(BaseModel):
        nested: _PlantedLeaf

    assert _model_field_names(_PlantedParent, seen=set()) & aa.RESERVED_ACTOR_FIELDS == {"role"}


# ---------------------------------------------------------------------------
# Closed enums (Appendix A.3): value-set membership, not just type. Each
# expected set below is transcribed directly from the appendix text, not
# derived from the enum under test.
# ---------------------------------------------------------------------------

_EXPECTED_ENUM_VALUES: dict[type[enum.Enum], frozenset[str]] = {
    aa.ProposalState: frozenset(
        {"open", "submitted", "approved", "activated", "rejected", "stale", "superseded", "withdrawn"}
    ),
    aa.OperationalIntegrityState: frozenset({"pending", "verified", "failed", "unavailable"}),
    aa.ProvenanceClass: frozenset({"source_backed", "human_judgment", "server_derived"}),
    aa.AdmissionMethod: frozenset({"connector_fetch", "authorized_upload"}),
    aa.VerificationMethod: frozenset({"detached_signature", "verifier_attestation"}),
    aa.PrincipalBindingKind: frozenset({"exact_principal", "provider_delegated"}),
    aa.SourceApprovalStatus: frozenset({"current", "expired", "revoked", "unknown", "overdue"}),
    aa.OwningScope: frozenset({"global", "tenant"}),
    aa.ObservationDecision: frozenset({"qualified", "insufficient", "failed"}),
    aa.EvidenceType: frozenset({"artifact_activation", "exception_approval"}),
    aa.ChangeKind: frozenset({"added", "removed", "changed"}),
    aa.AvailableAction: frozenset(
        {
            "edit",
            "validate",
            "run_semantic_tests",
            "confirm_reach",
            "draft",
            "submit",
            "withdraw",
            "reject",
            "supersede",
            "request_approval",
            "qualify",
            "accept_qualification",
            "activate",
        }
    ),
    aa.ActivationPredicateName: frozenset(
        {
            "latest_version",
            "state_approved",
            "digest_chain",
            "baseline_current",
            "source_valid",
            "risk_reproducible",
            "observation_qualified",
            "projection_evidence_valid",
            "actor_separation",
            "operational_integrity",
        }
    ),
    aa.RefusalCode: frozenset(
        {
            "arc_source_admission_refused",
            "arc_source_status_unavailable",
            "arc_evidence_type_not_writable",
            "arc_enrollment_challenge_required",
            "arc_enrollment_verification_failed",
            "arc_approval_challenge_expired",
            "arc_approval_challenge_failed",
            "arc_approval_challenge_superseded",
            "arc_approval_already_completed",
            "arc_approval_verification_failed",
            "arc_approval_challenge_limit_reached",
            "arc_proposal_state_conflict",
            "arc_proposal_validation_failed",
            "arc_provenance_invalid",
            "arc_reach_confirmation_required",
            "arc_envelope_invalid",
            "arc_observation_insufficient",
            "arc_observation_failed",
            "arc_qualification_actor_invalid",
            "arc_qualification_expired",
            "arc_activation_predicate_failed",
            "arc_operational_integrity_pending",
            "arc_operational_integrity_failed",
            "arc_drafter_model_disabled",
            "arc_idempotency_conflict",
            "arc_actor_not_caller_supplied",
        }
    ),
}


@pytest.mark.parametrize(
    ("enum_cls", "expected"), _EXPECTED_ENUM_VALUES.items(), ids=lambda x: getattr(x, "__name__", None)
)
def test_enum_value_set_matches_appendix(enum_cls: type[enum.Enum], expected: frozenset[str]) -> None:
    assert {member.value for member in enum_cls} == expected


def test_refusal_code_count_is_twenty_six() -> None:
    assert len(list(aa.RefusalCode)) == 26


# The REST status half of Appendix A.5's table, transcribed a second time
# by hand (independent of `REFUSAL_CODE_STATUS`) so a mistake made once in
# the module under test cannot also be the mistake that hides it here.
_EXPECTED_REFUSAL_STATUS: dict[str, int] = {
    "arc_source_admission_refused": 400,
    "arc_source_status_unavailable": 409,
    "arc_evidence_type_not_writable": 409,
    "arc_enrollment_challenge_required": 409,
    "arc_enrollment_verification_failed": 400,
    "arc_approval_challenge_expired": 409,
    "arc_approval_challenge_failed": 409,
    "arc_approval_challenge_superseded": 409,
    "arc_approval_already_completed": 409,
    "arc_approval_verification_failed": 400,
    "arc_approval_challenge_limit_reached": 429,
    "arc_proposal_state_conflict": 409,
    "arc_proposal_validation_failed": 422,
    "arc_provenance_invalid": 422,
    "arc_reach_confirmation_required": 409,
    "arc_envelope_invalid": 422,
    "arc_observation_insufficient": 409,
    "arc_observation_failed": 409,
    "arc_qualification_actor_invalid": 403,
    "arc_qualification_expired": 409,
    "arc_activation_predicate_failed": 409,
    "arc_operational_integrity_pending": 409,
    "arc_operational_integrity_failed": 409,
    "arc_drafter_model_disabled": 409,
    "arc_idempotency_conflict": 409,
    "arc_actor_not_caller_supplied": 400,
}


def test_refusal_code_status_matches_appendix() -> None:
    assert {code.value: status for code, status in aa.REFUSAL_CODE_STATUS.items()} == _EXPECTED_REFUSAL_STATUS


# ---------------------------------------------------------------------------
# Section 5.3's action-map: `AvailableAction` is checked against the closed
# static mapping this module freezes. No route exists yet, so this cannot
# be the non-vacuous registered-route parity check (that is `AAS-T21`'s);
# it can only confirm the frozen mapping itself is total and internally
# consistent.
# ---------------------------------------------------------------------------


def test_available_action_route_map_is_total() -> None:
    assert set(aa.AVAILABLE_ACTION_ROUTE_ACTIONS.keys()) == set(aa.AvailableAction)


def test_available_action_off_resource_exceptions_are_a_subset() -> None:
    assert aa.AVAILABLE_ACTION_OFF_RESOURCE_EXCEPTIONS <= set(aa.AVAILABLE_ACTION_ROUTE_ACTIONS.keys())
    assert aa.AVAILABLE_ACTION_OFF_RESOURCE_EXCEPTIONS == {
        aa.AvailableAction.REQUEST_APPROVAL,
        aa.AvailableAction.ACTIVATE,
    }


# ---------------------------------------------------------------------------
# Canonical examples: one positive and one refusal per REST route
# (Appendix A.1). The positive half validates against the named component;
# the refusal half validates against the shared REST-status mapping.
# ---------------------------------------------------------------------------


def _all_route_examples() -> list[tuple[str, dict[str, Any]]]:
    data = _load_json(_EXAMPLES_SNAPSHOT)
    return sorted(data["routes"].items())


@pytest.mark.parametrize(("route_key", "entry"), _all_route_examples())
def test_route_example_positive_validates_against_component(route_key: str, entry: dict[str, Any]) -> None:
    positive = entry["positive"]
    request_component = positive.get("request_component")
    if request_component is not None:
        model = aa.COMPONENTS[request_component]
        model.model_validate(positive["request"])
    response_component = positive.get("response_component")
    if response_component is None:
        # The one route with no JSON response component: GET .../body
        # returns raw bytes. A null component is only legitimate on that
        # one route; assert it here so a future edit cannot broaden the
        # exception unnoticed.
        assert route_key == "GET /v1/arc/sources/{source_evidence_id}/body"
        return
    model = aa.COMPONENTS[response_component]
    model.model_validate(positive["response"])


@pytest.mark.parametrize(("route_key", "entry"), _all_route_examples())
def test_route_example_refusal_matches_status_mapping(route_key: str, entry: dict[str, Any]) -> None:
    refusal = entry["refusal"]
    code, status = refusal["code"], refusal["status"]
    if code in _GENERIC_REFUSAL_STATUS:
        assert _GENERIC_REFUSAL_STATUS[code] == status
        return
    refusal_code = aa.RefusalCode(code)
    assert aa.REFUSAL_CODE_STATUS[refusal_code] == status


def test_every_route_has_exactly_one_positive_and_one_refusal_example() -> None:
    data = _load_json(_EXAMPLES_SNAPSHOT)
    for route_key, entry in data["routes"].items():
        assert "positive" in entry, route_key
        assert "refusal" in entry, route_key
        assert set(entry["refusal"]) == {"code", "status"}, route_key
