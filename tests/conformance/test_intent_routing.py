"""The four agent write paths, pinned as a contract.

Write disjointness is the property that stops an agent turning a working note
into a canonical fact by sending it to a different URL. It cannot be checked by
reading one surface, because it is a statement about all of them at once: these
tests assert the whole route table, every ordered pair of routes, and the
agreement between each route's body shape and the schema the corresponding read
returns.

The crossing matrix is exhaustive on purpose. Twelve ordered pairs is small
enough to enumerate, and a pair that stops being refused is exactly the kind of
regression a spot check misses -- it looks like one route quietly growing a
field, not like a boundary being removed.
"""

from __future__ import annotations

import dataclasses
import itertools
from typing import Any

import pytest

from contextplane.context.intent import (
    AUTHORITY_CITED_EVIDENCE,
    AUTHORITY_ORIGINS,
    AUTHORITY_PARTICIPANT_GRANT,
    AUTHORITY_QUALIFIED_CONTROL,
    AUTHORITY_REQUESTER_ENTITLEMENT,
    AUTHORITY_WORKSPACE_ENTRY,
    INTENT_CANONICAL_REVIEW,
    INTENT_CHECKPOINT,
    INTENT_OBSERVATION,
    INTENT_REQUEST,
    ROUTES,
    TABLES_NO_INTENT_MAY_TARGET,
    UNREACHABLE_EFFECTS,
    WORKSPACE_BODY_FIELDS,
    WRITE_INTENTS,
    DisallowedWrite,
    WriteAuthority,
    route_agent_write,
)
from contextplane.context.schemas.reference import ObservedClaimContentV1, WorkspaceContentV1
from contextplane.workspaces.schemas.intent_memory import CLIENT_FIELDS as CHECKPOINT_CLIENT_FIELDS

# One valid body per route. Every crossing test reuses these, so a body that
# stops being valid for its own route fails loudly rather than making a
# crossing test pass for the wrong reason.
BODIES: dict[str, dict[str, Any]] = {
    INTENT_CHECKPOINT: {"goal": "finish the routing table", "next_action": "write the conformance suite"},
    INTENT_OBSERVATION: {
        "subject_ref": "entity:payments-api",
        "predicate": "owner_team",
        "value": "platform",
        "evidence_event_ids": ["evt-9f2"],
    },
    INTENT_REQUEST: {
        "subject_ref": "entity:payments-api",
        "request_kind": "ownership_change",
        "justification": "the listed owner left six months ago",
    },
    INTENT_CANONICAL_REVIEW: {
        "entity_id": "entity:payments-api",
        "review_kind": "promotion",
        "justification": "two independent observations agree",
    },
}

AUTHORITIES: dict[str, WriteAuthority] = {
    INTENT_CHECKPOINT: WriteAuthority(actor_id="agent-1", origin=AUTHORITY_PARTICIPANT_GRANT),
    INTENT_OBSERVATION: WriteAuthority(actor_id="agent-1", origin=AUTHORITY_CITED_EVIDENCE),
    INTENT_REQUEST: WriteAuthority(actor_id="agent-1", origin=AUTHORITY_REQUESTER_ENTITLEMENT),
    INTENT_CANONICAL_REVIEW: WriteAuthority(
        actor_id="agent-1", origin=AUTHORITY_QUALIFIED_CONTROL, control_id="control-promotion-guardrail"
    ),
}

PAIRS = [(a, b) for a, b in itertools.product(WRITE_INTENTS, repeat=2) if a != b]


# --- the route table itself ---------------------------------------------------


def test_an_agent_writes_in_exactly_four_ways() -> None:
    """A fifth path is a fifth thing an agent can do without anyone deciding it
    may. The count is pinned so growing it is a deliberate edit here."""
    assert tuple(ROUTES) == WRITE_INTENTS
    assert len(WRITE_INTENTS) == 4


@pytest.mark.parametrize("intent", WRITE_INTENTS)
def test_each_intent_names_one_effect_one_table_and_one_authority(intent: str) -> None:
    route = ROUTES[intent]
    assert route.intent == intent
    assert route.effect and route.target_table and route.authority


@pytest.mark.parametrize("attribute", ["effect", "target_table", "authority"])
def test_no_two_routes_share_an_effect_a_table_or_an_authority(attribute: str) -> None:
    """A shared anything means one route is reachable by the other's caller."""
    values = [getattr(route, attribute) for route in ROUTES.values()]
    assert len(set(values)) == len(values), f"routes share a {attribute}: {values}"


def test_no_intent_reaches_an_effect_reserved_for_somebody_else() -> None:
    """An agent stages; a curator promotes. An agent asks; an owner decides."""
    assert {route.effect for route in ROUTES.values()} & UNREACHABLE_EFFECTS == set()


def test_no_intent_targets_a_table_an_agent_write_may_not_write() -> None:
    assert {route.target_table for route in ROUTES.values()} & TABLES_NO_INTENT_MAY_TARGET == set()


def test_the_workspace_table_is_not_the_target_of_any_agent_write() -> None:
    """A note and a checkpoint landing in one table become indistinguishable
    rows, and every later reader inherits the ambiguity."""
    assert "workspace_entries" in TABLES_NO_INTENT_MAY_TARGET
    assert all(route.target_table != "workspace_entries" for route in ROUTES.values())


# --- agreement with the frozen read schemas -----------------------------------


def test_the_checkpoint_route_accepts_exactly_what_a_client_may_supply() -> None:
    """Pinned against the checkpoint schema's own client-field set, so the two
    cannot drift into a state where the route accepts a server-derived field."""
    assert ROUTES[INTENT_CHECKPOINT].body_fields == CHECKPOINT_CLIENT_FIELDS


def test_the_observation_route_carries_the_claim_content_minus_its_server_id() -> None:
    """A staged observation is the claim a reader will later see, without the
    identity the server assigns and with the subject it is about."""
    claim_fields = {field.name for field in dataclasses.fields(ObservedClaimContentV1)}
    assert ROUTES[INTENT_OBSERVATION].body_fields == (claim_fields - {"claim_id"}) | {"subject_ref"}


def test_no_route_accepts_a_workspace_content_field() -> None:
    """Overlap would make a body route by whichever surface received it rather
    than by what it is."""
    assert WORKSPACE_BODY_FIELDS == {field.name for field in dataclasses.fields(WorkspaceContentV1)}
    for intent, route in ROUTES.items():
        assert route.body_fields & WORKSPACE_BODY_FIELDS == set(), f"{intent} overlaps workspace content"


# --- every route accepts its own write ----------------------------------------


@pytest.mark.parametrize("intent", WRITE_INTENTS)
def test_a_well_formed_write_routes_to_its_own_effect_and_table(intent: str) -> None:
    routed = route_agent_write(intent, BODIES[intent], authority=AUTHORITIES[intent])
    assert (routed.intent, routed.effect, routed.target_table) == (
        intent,
        ROUTES[intent].effect,
        ROUTES[intent].target_table,
    )


@pytest.mark.parametrize("intent", WRITE_INTENTS)
def test_a_routed_write_does_not_follow_the_mapping_the_caller_passed_in(intent: str) -> None:
    """The caller keeps a reference to its own dict; a routed write that shared
    it would change after the decision that approved it."""
    body = dict(BODIES[intent])
    routed = route_agent_write(intent, body, authority=AUTHORITIES[intent])
    body["goal"] = "something else entirely"
    assert "goal" not in routed.body or routed.body["goal"] != "something else entirely"


# --- the paths cannot cross, in either direction ------------------------------


@pytest.mark.parametrize(("source", "destination"), PAIRS)
def test_a_body_from_one_route_is_refused_by_every_other(source: str, destination: str) -> None:
    """All twelve ordered pairs. A body that crosses would be stored as
    something nobody wrote."""
    with pytest.raises(DisallowedWrite):
        route_agent_write(destination, BODIES[source], authority=AUTHORITIES[destination])


@pytest.mark.parametrize(("source", "destination"), PAIRS)
def test_a_crossed_body_is_told_which_route_it_belongs_to(source: str, destination: str) -> None:
    """Whoever crossed the boundary meant one of the two routes and needs to
    know which; a bare 'invalid body' sends them looking at the wrong one."""
    with pytest.raises(DisallowedWrite) as refusal:
        route_agent_write(destination, BODIES[source], authority=AUTHORITIES[destination])
    assert "belongs to the" in str(refusal.value)


@pytest.mark.parametrize(("holder", "destination"), PAIRS)
def test_authority_for_one_route_does_not_satisfy_another(holder: str, destination: str) -> None:
    """An actor trusted to checkpoint a task was not thereby trusted to review
    a canonical promotion."""
    with pytest.raises(DisallowedWrite, match="does not carry across routes"):
        route_agent_write(destination, BODIES[destination], authority=AUTHORITIES[holder])


# --- a workspace entry is context, never authority ----------------------------


@pytest.mark.parametrize("intent", WRITE_INTENTS)
def test_a_workspace_body_is_refused_by_every_route_by_name(intent: str) -> None:
    workspace_body = {"entry_id": "we-1", "entry_kind": "note", "title": "idea", "body_md": "maybe we should"}
    with pytest.raises(DisallowedWrite, match="context, never authority"):
        route_agent_write(intent, workspace_body, authority=AUTHORITIES[intent])


@pytest.mark.parametrize("intent", WRITE_INTENTS)
def test_a_workspace_entry_authorizes_no_route(intent: str) -> None:
    """Including the canonical one: a canonical API that accepted a working note
    as authority would lend the registry's voice to a passing thought."""
    authority = WriteAuthority(actor_id="agent-1", origin=AUTHORITY_WORKSPACE_ENTRY)
    with pytest.raises(DisallowedWrite, match="authorizes nothing"):
        route_agent_write(intent, BODIES[intent], authority=authority)


def test_the_workspace_origin_is_nameable_so_provenance_need_not_be_hidden() -> None:
    """A caller must be able to say truthfully where a body came from and be
    refused, rather than having to omit the provenance to get through."""
    assert AUTHORITY_WORKSPACE_ENTRY in AUTHORITY_ORIGINS
    assert all(route.authority != AUTHORITY_WORKSPACE_ENTRY for route in ROUTES.values())


# --- the canonical route goes through a control that already qualified it -----


def test_a_canonical_review_naming_no_control_is_refused() -> None:
    authority = WriteAuthority(actor_id="agent-1", origin=AUTHORITY_QUALIFIED_CONTROL)
    with pytest.raises(DisallowedWrite, match="must name"):
        route_agent_write(INTENT_CANONICAL_REVIEW, BODIES[INTENT_CANONICAL_REVIEW], authority=authority)


@pytest.mark.parametrize("intent", [INTENT_CHECKPOINT, INTENT_OBSERVATION, INTENT_REQUEST])
def test_only_the_review_route_passes_through_a_control(intent: str) -> None:
    """A control id anywhere else is the caller saying it believes it is
    reviewing something."""
    authority = dataclasses.replace(AUTHORITIES[intent], control_id="control-promotion-guardrail")
    with pytest.raises(DisallowedWrite, match="passes through no control"):
        route_agent_write(intent, BODIES[intent], authority=authority)


# --- an observation is staged and cited ---------------------------------------


def test_an_observation_citing_nothing_is_refused() -> None:
    """Staging puts it where a curator may promote it, so an uncited one is an
    invention one approval away from being canonical."""
    body = dict(BODIES[INTENT_OBSERVATION]) | {"evidence_event_ids": []}
    with pytest.raises(DisallowedWrite, match="indistinguishable from an invention"):
        route_agent_write(INTENT_OBSERVATION, body, authority=AUTHORITIES[INTENT_OBSERVATION])


def test_an_observation_stages_rather_than_asserts() -> None:
    routed = route_agent_write(
        INTENT_OBSERVATION, BODIES[INTENT_OBSERVATION], authority=AUTHORITIES[INTENT_OBSERVATION]
    )
    assert routed.effect == "staged_observation"
    assert "asserted_claim_write" in UNREACHABLE_EFFECTS


# --- an unrecognised intent has no safe default -------------------------------


@pytest.mark.parametrize("intent", ["", "workspace", "canonical_mutation", "CHECKPOINT"])
def test_an_unknown_intent_is_refused_rather_than_defaulted(intent: str) -> None:
    """Every default is somebody's write."""
    with pytest.raises(DisallowedWrite, match="unknown write intent"):
        route_agent_write(intent, {}, authority=AUTHORITIES[INTENT_CHECKPOINT])
