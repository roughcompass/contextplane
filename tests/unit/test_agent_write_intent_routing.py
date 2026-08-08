"""Each refusal the intent router makes, one test per rule.

The conformance suite pins the route table as a contract. This one exercises
the decisions the router makes on a request: what it refuses, and whether the
refusal says enough for the caller to fix it. Both matter -- a boundary that
holds but cannot explain itself gets worked around instead of understood.
"""

from __future__ import annotations

import dataclasses

import pytest

from contextplane.context.intent import (
    AUTHORITY_CITED_EVIDENCE,
    AUTHORITY_PARTICIPANT_GRANT,
    AUTHORITY_QUALIFIED_CONTROL,
    AUTHORITY_REQUESTER_ENTITLEMENT,
    AUTHORITY_WORKSPACE_ENTRY,
    EFFECT_CHECKPOINT_APPEND,
    INTENT_CANONICAL_REVIEW,
    INTENT_CHECKPOINT,
    INTENT_OBSERVATION,
    INTENT_REQUEST,
    ROUTES,
    WRITE_INTENTS,
    DisallowedWrite,
    WriteAuthority,
    WriteRoute,
    assert_routes_disjoint,
    effect_of,
    route_agent_write,
    target_table_of,
)

CHECKPOINT_AUTHORITY = WriteAuthority(actor_id="agent-1", origin=AUTHORITY_PARTICIPANT_GRANT)
OBSERVATION_AUTHORITY = WriteAuthority(actor_id="agent-1", origin=AUTHORITY_CITED_EVIDENCE)
REQUEST_AUTHORITY = WriteAuthority(actor_id="agent-1", origin=AUTHORITY_REQUESTER_ENTITLEMENT)
REVIEW_AUTHORITY = WriteAuthority(actor_id="agent-1", origin=AUTHORITY_QUALIFIED_CONTROL, control_id="ctrl-1")

CHECKPOINT_BODY = {"goal": "route the write"}
OBSERVATION_BODY = {"subject_ref": "entity:api", "predicate": "owner_team", "evidence_event_ids": ["evt-1"]}
REQUEST_BODY = {"subject_ref": "entity:api", "request_kind": "ownership_change", "justification": "owner left"}
REVIEW_BODY = {"entity_id": "entity:api", "review_kind": "promotion", "justification": "two sources agree"}


# --- the authority a caller stands on -----------------------------------------


def test_an_unattributed_write_is_refused() -> None:
    """A write nobody is named for cannot be reviewed after the fact."""
    with pytest.raises(DisallowedWrite, match="needs an actor"):
        WriteAuthority(actor_id="   ", origin=AUTHORITY_PARTICIPANT_GRANT)


def test_an_unknown_authority_origin_is_refused() -> None:
    with pytest.raises(DisallowedWrite, match="unknown authority origin"):
        WriteAuthority(actor_id="agent-1", origin="vibes")  # type: ignore[arg-type]


def test_a_blank_control_id_is_refused_rather_than_read_as_reviewed() -> None:
    """An empty string passes an 'is it set' check and names no control."""
    with pytest.raises(DisallowedWrite, match="absent or names a control"):
        WriteAuthority(actor_id="agent-1", origin=AUTHORITY_QUALIFIED_CONTROL, control_id="  ")


# --- routing a well-formed write ----------------------------------------------


def test_a_checkpoint_routes_to_the_checkpoint_table_under_the_caller_s_actor() -> None:
    routed = route_agent_write(INTENT_CHECKPOINT, CHECKPOINT_BODY, authority=CHECKPOINT_AUTHORITY)
    assert routed.effect == EFFECT_CHECKPOINT_APPEND
    assert routed.target_table == "task_checkpoints"
    assert routed.actor_id == "agent-1"


def test_the_routed_body_is_a_copy_the_caller_cannot_reach() -> None:
    body = dict(CHECKPOINT_BODY)
    routed = route_agent_write(INTENT_CHECKPOINT, body, authority=CHECKPOINT_AUTHORITY)
    body["goal"] = "rewritten after approval"
    assert routed.body["goal"] == "route the write"


# --- what the router refuses --------------------------------------------------


def test_an_unknown_intent_has_no_safe_default() -> None:
    with pytest.raises(DisallowedWrite, match="unknown write intent"):
        route_agent_write("promote", CHECKPOINT_BODY, authority=CHECKPOINT_AUTHORITY)


def test_a_non_string_body_key_is_refused() -> None:
    with pytest.raises(DisallowedWrite, match="keyed by field name"):
        route_agent_write(INTENT_CHECKPOINT, {1: "goal"}, authority=CHECKPOINT_AUTHORITY)  # type: ignore[dict-item]


def test_a_field_from_another_route_names_that_route() -> None:
    body = dict(CHECKPOINT_BODY) | {"predicate": "owner_team"}
    with pytest.raises(DisallowedWrite, match="belongs to the observation route"):
        route_agent_write(INTENT_CHECKPOINT, body, authority=CHECKPOINT_AUTHORITY)


def test_a_field_no_route_defines_is_refused_rather_than_dropped() -> None:
    """A dropped field is indistinguishable from one the server acted on."""
    body = dict(CHECKPOINT_BODY) | {"mood": "optimistic"}
    with pytest.raises(DisallowedWrite, match="unknown field 'mood'"):
        route_agent_write(INTENT_CHECKPOINT, body, authority=CHECKPOINT_AUTHORITY)


def test_a_missing_required_field_is_named() -> None:
    with pytest.raises(DisallowedWrite, match=r"missing required field\(s\) \['goal'\]"):
        route_agent_write(INTENT_CHECKPOINT, {"next_action": "later"}, authority=CHECKPOINT_AUTHORITY)


def test_a_blank_required_string_is_refused() -> None:
    with pytest.raises(DisallowedWrite, match="needs a goal"):
        route_agent_write(INTENT_CHECKPOINT, {"goal": "   "}, authority=CHECKPOINT_AUTHORITY)


def test_an_empty_required_list_that_is_not_evidence_is_refused() -> None:
    body = dict(REQUEST_BODY) | {"request_kind": []}
    with pytest.raises(DisallowedWrite, match="needs a non-empty request_kind"):
        route_agent_write(INTENT_REQUEST, body, authority=REQUEST_AUTHORITY)


# --- an observation stages a claim, so its citation is load-bearing -----------


def test_an_observation_citing_nothing_is_refused() -> None:
    body = dict(OBSERVATION_BODY) | {"evidence_event_ids": []}
    with pytest.raises(DisallowedWrite, match="indistinguishable from an invention"):
        route_agent_write(INTENT_OBSERVATION, body, authority=OBSERVATION_AUTHORITY)


def test_a_bare_string_citation_is_refused_rather_than_read_per_character() -> None:
    body = dict(OBSERVATION_BODY) | {"evidence_event_ids": "evt-1"}
    with pytest.raises(DisallowedWrite, match="one character at a time"):
        route_agent_write(INTENT_OBSERVATION, body, authority=OBSERVATION_AUTHORITY)


def test_a_blank_cited_event_cites_nothing() -> None:
    body = dict(OBSERVATION_BODY) | {"evidence_event_ids": ["evt-1", "  "]}
    with pytest.raises(DisallowedWrite, match="cites nothing"):
        route_agent_write(INTENT_OBSERVATION, body, authority=OBSERVATION_AUTHORITY)


# --- authority does not carry across routes -----------------------------------


def test_a_participant_grant_does_not_authorize_a_canonical_review() -> None:
    with pytest.raises(DisallowedWrite, match="does not carry across routes"):
        route_agent_write(INTENT_CANONICAL_REVIEW, REVIEW_BODY, authority=CHECKPOINT_AUTHORITY)


def test_the_refusal_names_the_route_the_presented_authority_does_satisfy() -> None:
    with pytest.raises(DisallowedWrite, match="satisfies the checkpoint route"):
        route_agent_write(INTENT_OBSERVATION, OBSERVATION_BODY, authority=CHECKPOINT_AUTHORITY)


def test_a_workspace_entry_authorizes_nothing() -> None:
    authority = WriteAuthority(actor_id="agent-1", origin=AUTHORITY_WORKSPACE_ENTRY)
    with pytest.raises(DisallowedWrite, match="authorizes nothing"):
        route_agent_write(INTENT_CHECKPOINT, CHECKPOINT_BODY, authority=authority)


def test_a_workspace_body_is_refused_by_name_not_as_an_unknown_field() -> None:
    """By name, because refusal-by-mismatch stops holding the moment either
    shape grows a field."""
    with pytest.raises(DisallowedWrite, match="context, never authority"):
        route_agent_write(INTENT_CHECKPOINT, {"body_md": "half an idea"}, authority=CHECKPOINT_AUTHORITY)


def test_a_review_must_name_the_control_it_passed_through() -> None:
    authority = WriteAuthority(actor_id="agent-1", origin=AUTHORITY_QUALIFIED_CONTROL)
    with pytest.raises(DisallowedWrite, match="must name"):
        route_agent_write(INTENT_CANONICAL_REVIEW, REVIEW_BODY, authority=authority)


def test_a_control_id_on_a_non_review_route_is_refused() -> None:
    authority = dataclasses.replace(OBSERVATION_AUTHORITY, control_id="ctrl-1")
    with pytest.raises(DisallowedWrite, match="passes through no control"):
        route_agent_write(INTENT_OBSERVATION, OBSERVATION_BODY, authority=authority)


def test_a_review_with_its_control_is_accepted() -> None:
    routed = route_agent_write(INTENT_CANONICAL_REVIEW, REVIEW_BODY, authority=REVIEW_AUTHORITY)
    assert routed.target_table == "memory_promotion_proposal"


# --- the route table refuses to be crossed at import --------------------------


def _table(**replacements: WriteRoute) -> dict[str, WriteRoute]:
    """The shipped table with one route swapped, in declared order."""
    return {intent: replacements.get(intent, ROUTES[intent]) for intent in WRITE_INTENTS}


def test_the_shipped_route_table_is_disjoint() -> None:
    assert_routes_disjoint(ROUTES)


def test_a_route_table_missing_a_path_is_refused() -> None:
    partial = {intent: ROUTES[intent] for intent in WRITE_INTENTS[:3]}
    with pytest.raises(DisallowedWrite, match="exactly"):
        assert_routes_disjoint(partial)


def test_a_route_filed_under_the_wrong_intent_is_refused() -> None:
    crossed = _table(**{INTENT_REQUEST: ROUTES[INTENT_CHECKPOINT]})
    with pytest.raises(DisallowedWrite, match="declares itself"):
        assert_routes_disjoint(crossed)


def test_a_route_claiming_an_effect_reserved_for_somebody_else_is_refused() -> None:
    crossed = _table(
        **{INTENT_OBSERVATION: dataclasses.replace(ROUTES[INTENT_OBSERVATION], effect="asserted_claim_write")}
    )
    with pytest.raises(DisallowedWrite, match="no agent write may produce"):
        assert_routes_disjoint(crossed)


def test_a_route_targeting_canonical_rows_is_refused() -> None:
    crossed = _table(
        **{INTENT_CANONICAL_REVIEW: dataclasses.replace(ROUTES[INTENT_CANONICAL_REVIEW], target_table="entities")}
    )
    with pytest.raises(DisallowedWrite, match="may write directly"):
        assert_routes_disjoint(crossed)


def test_a_route_accepting_a_workspace_entry_as_authority_is_refused() -> None:
    crossed = _table(
        **{INTENT_REQUEST: dataclasses.replace(ROUTES[INTENT_REQUEST], authority=AUTHORITY_WORKSPACE_ENTRY)}
    )
    with pytest.raises(DisallowedWrite, match="how a thought becomes a record"):
        assert_routes_disjoint(crossed)


def test_a_route_accepting_a_workspace_field_is_refused() -> None:
    crossed = _table(
        **{
            INTENT_CHECKPOINT: dataclasses.replace(
                ROUTES[INTENT_CHECKPOINT], body_fields=ROUTES[INTENT_CHECKPOINT].body_fields | {"body_md"}
            )
        }
    )
    with pytest.raises(DisallowedWrite, match="routes by whichever surface received it"):
        assert_routes_disjoint(crossed)


def test_a_route_requiring_a_field_it_does_not_accept_is_refused() -> None:
    crossed = _table(**{INTENT_REQUEST: dataclasses.replace(ROUTES[INTENT_REQUEST], required_fields=("nonesuch",))})
    with pytest.raises(DisallowedWrite, match="it does not accept"):
        assert_routes_disjoint(crossed)


@pytest.mark.parametrize(
    ("attribute", "value", "match"),
    [
        ("effect", "task_checkpoint_append", "share the effect"),
        ("target_table", "task_checkpoints", "share the target table"),
        ("authority", AUTHORITY_PARTICIPANT_GRANT, "share the authority"),
    ],
)
def test_two_routes_sharing_a_dimension_are_refused(attribute: str, value: str, match: str) -> None:
    """A shared effect, table or authority means one route is reachable by the
    other's caller."""
    crossed = _table(**{INTENT_REQUEST: dataclasses.replace(ROUTES[INTENT_REQUEST], **{attribute: value})})
    with pytest.raises(DisallowedWrite, match=match):
        assert_routes_disjoint(crossed)


# --- the two lookups a surface uses -------------------------------------------


@pytest.mark.parametrize("intent", WRITE_INTENTS)
def test_effect_and_table_lookups_agree_with_the_route_table(intent: str) -> None:
    assert effect_of(intent) == ROUTES[intent].effect
    assert target_table_of(intent) == ROUTES[intent].target_table


@pytest.mark.parametrize("lookup", [effect_of, target_table_of])
def test_a_lookup_on_an_unknown_intent_raises(lookup: object) -> None:
    with pytest.raises(DisallowedWrite, match="unknown write intent"):
        lookup("promote")  # type: ignore[operator]
