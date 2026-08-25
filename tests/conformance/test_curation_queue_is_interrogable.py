"""The queue's ordering and each disposition's cost reach the caller.

E5-T6a. Both halves of what a reviewer needs to act on this queue were already
computed by the service and dropped before the wire:

- `QueueItem` has carried `escalated`, `dependant_count` and `sampling_priority`
  since the ordering landed — with a comment saying *"a rank a reviewer cannot
  interrogate is a rank they learn to ignore"* — and `_to_queue_item_response`
  did not copy them across.
- `DISPOSITIONS` records what each of the six commits to across five dimensions,
  and no endpoint served any of it.

So a client could render the order and not the reason for it, and offer six
buttons whose consequences it could only know by restating this module's
contents. These pin both directions: what the service computes reaches the
response, and what the response offers matches what the service accepts.
"""

from __future__ import annotations

import dataclasses

from contextplane.api.routers import memory_curation as router_module
from contextplane.api.schemas import memory_curation as schemas
from contextplane.service.memory import curation_cases, curation_ranking
from contextplane.service.memory.curation_queue import QueueItem

#: The ordering's inputs, named here so the two ends can be checked against each
#: other rather than against a list somebody keeps in their head.
_RANK_TERMS = ("escalated", "dependant_count", "sampling_priority")


def test_every_term_the_ordering_uses_reaches_the_caller() -> None:
    """The queue is ranked, and a rank whose inputs are invisible is an order a
    reviewer has to take on trust.

    Checked against `QueueItem` rather than a literal list, so a fourth term
    added to the ordering fails here instead of being silently unexplained.
    """
    served = set(schemas.QueueItemResponse.model_fields)
    for term in _RANK_TERMS:
        assert term in served, f"the ordering reads {term} and the response does not carry it"

    computed = {field.name for field in dataclasses.fields(QueueItem)}
    assert set(_RANK_TERMS) <= computed

    # Nothing the service computes for a queue item may be dropped on the way
    # out. Stated as a whole-object rule rather than as three names, because
    # this task exists precisely because three fields were computed and dropped
    # and nothing noticed for as long as the ordering had shipped.
    assert not computed - served, (
        f"the service computes {sorted(computed - served)} for a queue item and the response "
        "drops them. Serve them, or delete them from QueueItem — a field computed on every row "
        "and visible to nobody is the shape this task was filed to remove."
    )
    # `available_actions` is a property rather than a field, derived from the
    # rest, so it is on the response and not in `dataclasses.fields`.
    assert served - computed == {"available_actions"}


def test_the_sort_columns_and_the_served_terms_describe_the_same_order() -> None:
    """The three served terms are the three the SQL sorts on, and nothing else.

    `_RANK_ORDER` also names `created_at` and `claim_id`, which are the tiebreak
    and are already on the response under their own names. A term appearing in
    the ORDER BY and nowhere in the response would be an ordering input no
    reviewer could see.
    """
    order = curation_ranking._RANK_ORDER
    for column in ("escalation_rank", "neg_dependants", "neg_sampling"):
        assert column in order
    assert "confidence" not in order, (
        "confidence is back in the ordering. It was left out to break the feedback loop "
        "where a claim nobody reviews decays and sinks because it sank."
    )


def test_confidence_is_served_but_is_not_a_rank_term() -> None:
    """A reviewer sees the confidence and it does not move the row.

    Worth pinning because the response carries both, and a reader who sees a
    number beside a position assumes the first produced the second.
    """
    assert "confidence" in schemas.QueueItemResponse.model_fields
    assert "confidence" not in _RANK_TERMS


def test_every_disposition_a_person_may_record_has_its_consequences_served() -> None:
    """Six buttons, six different approval authorities and rollback stories.

    A client that restated them would be a second copy of a governance rule,
    diverging from this one silently the first time a policy changed — which is
    what the design guide means by not inventing client-only governance gates.
    """
    recordable = set(curation_cases.OPERATOR_DISPOSITIONS)
    literal = router_module.RecordDispositionRequest.model_fields["disposition"].annotation
    offered = set(getattr(literal, "__args__", ()))
    assert offered == recordable, (
        "the disposition the endpoint accepts and the vocabulary a person may record have diverged; "
        f"recordable={sorted(recordable)} offered={sorted(offered)}"
    )


def test_both_transports_offer_the_same_dispositions() -> None:
    """The gap a withdrawn change exposed, closed.

    The HTTP route states its set as a hand-written `Literal`; the MCP tool took
    a bare `str` and let `record_disposition` validate, which validates against
    the *whole* vocabulary. While every disposition was operator-recordable that
    was invisible. The moment one was meant to be service-only, the agent-facing
    surface accepted it and this test — inspecting only the HTTP model — reported
    the surfaces closed.

    So it checks the MCP tool too, and both against one name.
    """
    import inspect

    from contextplane.api.mcp.tools import memory_curation as mcp_module

    recordable = set(curation_cases.OPERATOR_DISPOSITIONS)
    literal = router_module.RecordDispositionRequest.model_fields["disposition"].annotation
    assert set(getattr(literal, "__args__", ())) == recordable

    source = inspect.getsource(mcp_module.record_case_disposition)
    assert "OPERATOR_DISPOSITIONS" in source, (
        "the MCP tool must refuse a disposition a person may not record; without it "
        "the agent-facing surface accepts every value the service knows"
    )


def test_the_two_disposition_groups_are_the_whole_vocabulary() -> None:
    """So a disposition cannot be added to neither and reach no surface at all,
    or added to both and be offered where it should not be."""
    operator = set(curation_cases.OPERATOR_DISPOSITIONS)
    policy_only = curation_cases.POLICY_ONLY_DISPOSITIONS

    assert operator | policy_only == set(curation_cases.DISPOSITIONS)
    assert operator.isdisjoint(policy_only)


def test_the_served_policy_carries_every_dimension_the_service_records() -> None:
    """All five, and the target kind.

    `DispositionPolicy`'s own docstring is that these are properties of the
    *target* and that the three proposal targets disagree on all of them —
    "collapsing them into one 'propose' disposition with a free-text note would
    make all three look equally consequential in the queue, and the one that
    reaches every agent is not". A response that dropped one would do the same
    thing one dimension at a time.
    """
    recorded = {field.name for field in dataclasses.fields(curation_cases.DispositionPolicy)}
    served = set(schemas.DispositionPolicyResponse.model_fields)
    missing = recorded - served
    assert missing <= {"audit_action"}, f"the consequence response drops {sorted(missing)}"
    assert "audit_action" not in served, (
        "audit_action is the vocabulary term the write emits, not a consequence a reviewer "
        "weighs; serving it would put an internal identifier in front of a decision"
    )


def test_the_consequence_surface_is_ordered_as_the_vocabulary_declares_it() -> None:
    """The first three settle a disagreement on the curator's own authority; the
    last three ask an approver outside curation for a write. That grouping is a
    property of the vocabulary, and a client sorting alphabetically loses it."""
    order = list(curation_cases.DISPOSITIONS)
    assert order[:3] == [
        curation_cases.DISPOSITION_CONFIRM,
        curation_cases.DISPOSITION_REJECT,
        curation_cases.DISPOSITION_SUPERSEDE,
    ]
    for name in order[:3]:
        assert curation_cases.DISPOSITIONS[name].approval_authority == "curation_owner"
        assert curation_cases.DISPOSITIONS[name].target_kind is None
    for name in order[3:]:
        assert curation_cases.DISPOSITIONS[name].approval_authority != "curation_owner"
        assert curation_cases.DISPOSITIONS[name].target_kind is not None


def test_the_consequence_surface_needs_no_tenant_and_discloses_none() -> None:
    """It is the vocabulary the service accepts, identical for every caller.

    Gating it would only mean a reviewer sees a set of buttons whose
    consequences they cannot read — and the write itself is authorized where it
    always was.
    """
    handler = router_module.list_disposition_policies
    assert handler.__code__.co_argcount == 0, (
        "the consequence surface now takes an argument. It answers the same for everybody "
        "and reads no tenant data; a parameter here is a tenant filter waiting to be added."
    )
