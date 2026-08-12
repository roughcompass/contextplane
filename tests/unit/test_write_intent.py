"""The generic profile write contract, one test per rule it holds.

Two properties carry the security weight and are tested from both directions:
a write with no intent is refused rather than defaulted, and no field a caller
can send lets it assert canonical authority. The second is the reason the
routing decision takes a server-resolved authority object instead of reading
the body -- so the tests build an ordinary caller and try, repeatedly, to reach
the canonical route with it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from contextplane.api.schemas.profile_writes import (
    AssertionProvenanceInputV1,
    ProfileWriteIdentityV1,
    ProfileWriteRequestV1,
    TemporalStateV1,
)
from contextplane.entities.write_intent import (
    AUTHORITY_OBSERVED_EVIDENCE,
    AUTHORITY_REQUESTER_ENTITLEMENT,
    AUTHORITY_VERIFIED_APPROVAL,
    EFFECT_CANONICAL_ASSERTION_WRITE,
    EFFECT_OWNER_REVIEW_ENTRY,
    EFFECT_STAGED_CLAIM,
    INTENT_AUTHORIZED_APPROVAL,
    INTENT_OBSERVATION,
    INTENT_REQUEST,
    PROFILE_WRITE_INTENTS,
    RESERVED_AUTHORITY_FIELDS,
    ROUTES,
    ProfileWriteAuthority,
    ProfileWriteRoute,
    RefusedProfileWrite,
    assert_routes_disjoint,
    effect_of,
    refuse_caller_asserted_authority,
    route_profile_write,
)

OBSERVER = ProfileWriteAuthority(actor_id="agent-1", origin=AUTHORITY_OBSERVED_EVIDENCE)
REQUESTER = ProfileWriteAuthority(actor_id="agent-1", origin=AUTHORITY_REQUESTER_ENTITLEMENT)
APPROVER = ProfileWriteAuthority(actor_id="owner-1", origin=AUTHORITY_VERIFIED_APPROVAL, approval_reference="apr-1")

NOW = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.UTC)


def _provenance(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_system": "github",
        "source_namespace": "acme",
        "external_record_id": "repo/42",
        "observed_time": NOW,
    }
    body.update(overrides)
    return body


def _request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "intent": INTENT_OBSERVATION,
        "subject_kind": "entity",
        "subject_type": "service",
        "identity": {"handle": "acme:service/checkout"},
        "target_revision": {"profile_revision": "core-3"},
        "temporal": {"valid_from": NOW},
        "idempotency_key": "idem-1",
        "provenance": _provenance(),
        "properties": {"tier": "gold"},
    }
    body.update(overrides)
    return body


# --- the vocabulary is closed and has no default ------------------------------


def test_the_vocabulary_is_exactly_three_intents() -> None:
    """A fourth intent is a fourth thing a caller may do without anyone deciding it may."""
    assert PROFILE_WRITE_INTENTS == (INTENT_OBSERVATION, INTENT_REQUEST, INTENT_AUTHORIZED_APPROVAL)
    assert tuple(ROUTES) == PROFILE_WRITE_INTENTS


def test_a_missing_intent_is_refused_by_the_router() -> None:
    with pytest.raises(RefusedProfileWrite, match="there is no default"):
        route_profile_write(None, authority=OBSERVER)


def test_a_blank_intent_is_refused_rather_than_read_as_absent() -> None:
    """A whitespace intent passes an `is it set` check and names no route."""
    with pytest.raises(RefusedProfileWrite, match="states its intent"):
        route_profile_write("   ", authority=OBSERVER)


def test_an_unknown_intent_has_no_safe_interpretation() -> None:
    with pytest.raises(RefusedProfileWrite, match="unknown write intent"):
        route_profile_write("upsert", authority=OBSERVER)


def test_a_missing_intent_is_refused_by_the_envelope() -> None:
    """The body is refused outright, so nothing downstream infers what it meant."""
    body = _request()
    del body["intent"]
    with pytest.raises(ValidationError) as caught:
        ProfileWriteRequestV1(**body)
    assert any(error["loc"] == ("intent",) and error["type"] == "missing" for error in caught.value.errors())


def test_the_envelope_refuses_an_intent_outside_the_vocabulary() -> None:
    with pytest.raises(ValidationError):
        ProfileWriteRequestV1(**_request(intent="upsert"))


# --- each intent reaches exactly one effect -----------------------------------


def test_each_intent_routes_to_its_own_effect() -> None:
    assert effect_of(INTENT_OBSERVATION) == EFFECT_STAGED_CLAIM
    assert effect_of(INTENT_REQUEST) == EFFECT_OWNER_REVIEW_ENTRY
    assert effect_of(INTENT_AUTHORIZED_APPROVAL) == EFFECT_CANONICAL_ASSERTION_WRITE


def test_an_observation_stages_a_claim_rather_than_asserting_one() -> None:
    routed = route_profile_write(INTENT_OBSERVATION, authority=OBSERVER)
    assert routed.effect == EFFECT_STAGED_CLAIM
    assert routed.actor_id == "agent-1"
    assert routed.approval_reference is None


def test_a_request_enters_the_owner_queue() -> None:
    routed = route_profile_write(INTENT_REQUEST, authority=REQUESTER)
    assert routed.effect == EFFECT_OWNER_REVIEW_ENTRY


def test_a_verified_approval_reaches_canonical_validation() -> None:
    routed = route_profile_write(INTENT_AUTHORIZED_APPROVAL, authority=APPROVER, approval_reference="apr-1")
    assert routed.effect == EFFECT_CANONICAL_ASSERTION_WRITE
    assert routed.approval_reference == "apr-1"


def test_two_routes_never_share_an_effect_or_an_authority() -> None:
    """Checked at import on the real table; re-checked here so the rule is testable."""
    crossed = dict(ROUTES)
    crossed[INTENT_REQUEST] = dataclasses.replace(crossed[INTENT_REQUEST], effect=EFFECT_STAGED_CLAIM)
    with pytest.raises(RefusedProfileWrite, match="share the effect"):
        assert_routes_disjoint(crossed)


def test_a_route_table_that_grew_a_member_is_refused() -> None:
    grown: dict[str, ProfileWriteRoute] = dict(ROUTES)
    grown["bulk_sync"] = ProfileWriteRoute(
        intent=INTENT_OBSERVATION, effect=EFFECT_STAGED_CLAIM, authority=AUTHORITY_OBSERVED_EVIDENCE
    )
    with pytest.raises(RefusedProfileWrite, match="a fourth route"):
        assert_routes_disjoint(grown)


def test_a_route_filed_under_the_wrong_key_is_refused() -> None:
    """The key and the route's own intent are two chances to say the same thing."""
    mislabelled = dict(ROUTES)
    mislabelled[INTENT_REQUEST] = dataclasses.replace(mislabelled[INTENT_REQUEST], intent=INTENT_OBSERVATION)
    with pytest.raises(RefusedProfileWrite, match="declares itself"):
        assert_routes_disjoint(mislabelled)


def test_a_route_that_takes_an_approval_without_requiring_one_is_refused() -> None:
    """Carrying a reference and requiring verified approval are the same fact.

    Split apart, a route could accept an approval id on an authority that never
    verified one -- which is the caller-asserted approval this contract exists
    to make unrepresentable.
    """
    lax = dict(ROUTES)
    lax[INTENT_OBSERVATION] = dataclasses.replace(lax[INTENT_OBSERVATION], carries_approval_reference=True)
    with pytest.raises(RefusedProfileWrite, match="disagrees with itself about approval"):
        assert_routes_disjoint(lax)


def test_the_effect_of_an_unknown_intent_is_not_guessed() -> None:
    with pytest.raises(RefusedProfileWrite, match="unknown write intent"):
        effect_of("upsert")


# --- naming a route does not open it ------------------------------------------


def test_an_ordinary_agent_cannot_select_the_canonical_route() -> None:
    """The property the whole split exists for: the body asks, the server decides."""
    with pytest.raises(RefusedProfileWrite, match="needs 'verified_approval' authority"):
        route_profile_write(INTENT_AUTHORIZED_APPROVAL, authority=OBSERVER, approval_reference="apr-1")


def test_an_entitled_requester_still_cannot_select_the_canonical_route() -> None:
    """Authority for the review queue is not authority to write past it."""
    with pytest.raises(RefusedProfileWrite, match="does not open it"):
        route_profile_write(INTENT_AUTHORIZED_APPROVAL, authority=REQUESTER, approval_reference="apr-1")


def test_an_approver_taking_the_observation_route_is_refused() -> None:
    """Refused in both directions: a resolved approval is not a licence to stage."""
    with pytest.raises(RefusedProfileWrite, match="needs 'observed_evidence' authority"):
        route_profile_write(INTENT_OBSERVATION, authority=APPROVER)


def test_the_approval_route_needs_a_reference_to_re_resolve() -> None:
    with pytest.raises(RefusedProfileWrite, match="names the approval it passed"):
        route_profile_write(INTENT_AUTHORIZED_APPROVAL, authority=APPROVER)


def test_a_reference_to_somebody_elses_approval_is_refused() -> None:
    """The service re-resolves the reference rather than trusting the body's copy."""
    with pytest.raises(RefusedProfileWrite, match="not the approval verified for this caller"):
        route_profile_write(INTENT_AUTHORIZED_APPROVAL, authority=APPROVER, approval_reference="apr-999")


def test_an_approval_reference_on_a_weaker_route_is_refused() -> None:
    with pytest.raises(RefusedProfileWrite, match="passes through no approval"):
        route_profile_write(INTENT_OBSERVATION, authority=OBSERVER, approval_reference="apr-1")


# --- the authority object cannot be built out of a request --------------------


def test_an_unattributed_write_is_refused() -> None:
    with pytest.raises(RefusedProfileWrite, match="needs an actor"):
        ProfileWriteAuthority(actor_id="  ", origin=AUTHORITY_OBSERVED_EVIDENCE)


def test_an_approval_authority_with_no_reference_is_refused() -> None:
    """One that verified nothing would still satisfy the origin check downstream."""
    with pytest.raises(RefusedProfileWrite, match="names the approval it verified"):
        ProfileWriteAuthority(actor_id="owner-1", origin=AUTHORITY_VERIFIED_APPROVAL)


def test_a_blank_approval_reference_is_refused() -> None:
    with pytest.raises(RefusedProfileWrite, match="names the approval it verified"):
        ProfileWriteAuthority(actor_id="owner-1", origin=AUTHORITY_VERIFIED_APPROVAL, approval_reference="   ")


def test_a_non_approval_authority_carrying_an_approval_is_refused() -> None:
    with pytest.raises(RefusedProfileWrite, match="carries no approval"):
        ProfileWriteAuthority(actor_id="agent-1", origin=AUTHORITY_OBSERVED_EVIDENCE, approval_reference="apr-1")


def test_an_unknown_authority_origin_is_refused() -> None:
    with pytest.raises(RefusedProfileWrite, match="unknown authority origin"):
        ProfileWriteAuthority(actor_id="agent-1", origin="self_asserted")  # type: ignore[arg-type]


# --- no caller-supplied field asserts canonical authority ---------------------


@pytest.mark.parametrize("field", sorted(RESERVED_AUTHORITY_FIELDS))
def test_no_reserved_authority_field_may_be_supplied_by_a_caller(field: str) -> None:
    """Every one of them, not a sample: the set is the contract."""
    with pytest.raises(RefusedProfileWrite, match="the platform derives rather than accepts"):
        refuse_caller_asserted_authority({field: "anything"})


def test_the_envelope_refuses_an_asserted_trust_class() -> None:
    with pytest.raises(ValidationError, match="derives rather than accepts"):
        ProfileWriteRequestV1(**_request(authority="canonical_owner"))


def test_the_envelope_refuses_an_asserted_validation_result() -> None:
    with pytest.raises(ValidationError, match="derives rather than accepts"):
        ProfileWriteRequestV1(**_request(validation_result="valid"))


def test_provenance_refuses_an_asserted_trust_class() -> None:
    """The nested object gets the same screen; the outer one cannot cover it."""
    with pytest.raises(ValidationError, match="derives rather than accepts"):
        ProfileWriteRequestV1(**_request(provenance=_provenance(authority="canonical_owner")))


def test_provenance_refuses_a_caller_supplied_ingest_time() -> None:
    with pytest.raises(ValidationError, match="derives rather than accepts"):
        ProfileWriteRequestV1(**_request(provenance=_provenance(ingested_at=NOW)))


def test_a_reserved_name_inside_typed_properties_is_a_property_not_an_assertion() -> None:
    """A profile may define a property called `authority`; in the bag it is a value.

    The screen is on the envelope and its provenance, not on the typed property
    bag -- otherwise the contract would quietly forbid a legal profile.
    """
    request = ProfileWriteRequestV1(**_request(properties={"authority": "finance-team"}))
    assert request.properties == {"authority": "finance-team"}


def test_an_unknown_envelope_field_is_refused_rather_than_dropped() -> None:
    with pytest.raises(ValidationError):
        ProfileWriteRequestV1(**_request(force_canonical=True))


# --- the envelope's own shape rules -------------------------------------------


def test_the_approval_intent_requires_a_reference_in_the_body() -> None:
    with pytest.raises(ValidationError, match="names the approval it passed"):
        ProfileWriteRequestV1(**_request(intent=INTENT_AUTHORIZED_APPROVAL))


def test_a_weaker_intent_carrying_an_approval_reference_is_refused() -> None:
    with pytest.raises(ValidationError, match="asserts a review that did not happen"):
        ProfileWriteRequestV1(**_request(approval_reference="apr-1"))


def test_an_approval_body_round_trips_with_its_reference() -> None:
    request = ProfileWriteRequestV1(**_request(intent=INTENT_AUTHORIZED_APPROVAL, approval_reference="apr-1"))
    assert request.intent == INTENT_AUTHORIZED_APPROVAL
    assert request.approval_reference == "apr-1"


def test_a_write_names_its_subject() -> None:
    with pytest.raises(ValidationError, match="names its subject"):
        ProfileWriteIdentityV1()


def test_a_subject_may_be_named_by_id_and_handle_together() -> None:
    identity = ProfileWriteIdentityV1(subject_id=uuid.uuid4(), handle="acme:service/checkout")
    assert identity.handle == "acme:service/checkout"


def test_an_unqualified_handle_is_refused() -> None:
    """Names repeat across types, so a handle without its type resolves to more than one thing."""
    with pytest.raises(ValidationError):
        ProfileWriteIdentityV1(handle="checkout")


def test_a_validity_interval_ends_after_it_starts() -> None:
    with pytest.raises(ValidationError, match="ends after it starts"):
        TemporalStateV1(valid_from=NOW, valid_to=NOW - dt.timedelta(days=1))


def test_an_empty_validity_interval_is_refused() -> None:
    """Equal endpoints assert a fact that was never true and read back as one."""
    with pytest.raises(ValidationError, match="ends after it starts"):
        TemporalStateV1(valid_from=NOW, valid_to=NOW)


def test_provenance_is_required_for_every_write() -> None:
    body = _request()
    del body["provenance"]
    with pytest.raises(ValidationError):
        ProfileWriteRequestV1(**body)


def test_a_blank_source_system_does_not_pass_as_provenance() -> None:
    with pytest.raises(ValidationError):
        AssertionProvenanceInputV1(**_provenance(source_system="   "))


def test_confidence_accompanies_a_derivation_method() -> None:
    with pytest.raises(ValidationError, match="accompanies a derivation method"):
        AssertionProvenanceInputV1(**_provenance(confidence=0.9))


def test_a_derived_value_may_carry_confidence() -> None:
    provenance = AssertionProvenanceInputV1(**_provenance(derivation_method="owner-inference", confidence=0.9))
    assert provenance.confidence == 0.9
