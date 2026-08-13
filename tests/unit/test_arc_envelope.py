"""Unit tests for `contextplane/arc/service/envelope.py`:
`ExpectedImpactEnvelopeService.validate` -- the ADR 041 §4 predicate-key
allowlist, empty-set rejection, item non-overlap (the named overlap
matrix), count-range validation, and canonical-digest computation.

Every rejection this service can produce is asserted to surface as
`EnvelopeInvalid` (`arc_envelope_invalid`), never the pure profile
validator's own `arc_proposal_validation_failed` -- see this module's own
docstring for why that distinction is the point of this file existing
rather than callers using `authoring_profiles.validate_expected_impact_
envelope_v1` directly.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from contextplane.arc.service.envelope import EnvelopeInvalid, ExpectedImpactEnvelopeService

_PROPOSAL_ID = uuid.uuid4()
_PROPOSAL_VERSION = 1
_ISSUER = "https://idp.example.test"
_OPERATOR = "operator"

# The six approved selector dimensions, exactly -- ADR 041 §4's closed set.
_PREDICATE_FIELDS = (
    "intent_kind",
    "requested_action_classes",
    "environment",
    "data_sensitivity_tier",
    "capability_ids",
    "domain_ids",
)


def _predicate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": "arc_observation_class_predicate_v2",
        "intent_kind": None,
        "requested_action_classes": None,
        "environment": None,
        "data_sensitivity_tier": None,
        "capability_ids": None,
        "domain_ids": None,
    }
    base.update(overrides)
    return base


def _item(item_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item_id": item_id,
        "delta_code": "newly_selected",
        "class_predicate": _predicate(),
        "minimum_count": 0,
        "maximum_count": None,
        "rationale_code": "expected_low_traffic",
    }
    base.update(overrides)
    return base


def _envelope(*items: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": "arc_expected_impact_envelope_v2",
        "envelope_id": str(uuid.uuid4()),
        "proposal_id": str(_PROPOSAL_ID),
        "proposal_version": _PROPOSAL_VERSION,
        "items": list(items),
        "author_issuer": _ISSUER,
        "author_subject": _OPERATOR,
        "created_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _validate(envelope: dict[str, Any]) -> Any:
    return ExpectedImpactEnvelopeService().validate(
        envelope, proposal_id=_PROPOSAL_ID, proposal_version=_PROPOSAL_VERSION
    )


# ---------------------------------------------------------------------------
# Happy path: exactly the six-member predicate field set, canonical digest.
# ---------------------------------------------------------------------------


def test_a_minimal_valid_envelope_is_accepted_and_digested() -> None:
    result = _validate(_envelope(_item("item-1")))
    assert len(result.envelope_digest) == 64
    assert all(c in "0123456789abcdef" for c in result.envelope_digest)


def test_the_predicate_schema_declares_exactly_the_six_approved_fields() -> None:
    predicate = _predicate()
    assert set(predicate) - {"profile"} == set(_PREDICATE_FIELDS)


def test_digest_is_deterministic_for_the_same_logical_envelope() -> None:
    envelope = _envelope(_item("item-1"))
    first = _validate(dict(envelope))
    second = _validate(dict(envelope))
    assert first.envelope_digest == second.envelope_digest


# ---------------------------------------------------------------------------
# Forbidden predicate keys: rejection, not silent ignoring. Every named key
# plus an arbitrary unknown one, each independently.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_key",
    ["tenant_id", "repository_identity", "session_id", "intent_summary", "an_entirely_unrecognized_key"],
)
def test_forbidden_predicate_keys_are_rejected_not_ignored(forbidden_key: str) -> None:
    predicate = _predicate()
    predicate[forbidden_key] = "anything"
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1", class_predicate=predicate)))


def test_forbidden_keys_are_rejected_even_when_every_approved_field_is_also_present() -> None:
    """A caller cannot smuggle a forbidden key in alongside a fully valid
    predicate and have it silently dropped -- the whole object is refused."""
    predicate = _predicate(intent_kind=["a"], domain_ids=["b"])
    predicate["session_id"] = "s-123"
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1", class_predicate=predicate)))


# ---------------------------------------------------------------------------
# Empty sets: null is unconstrained, but an empty array is invalid.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", _PREDICATE_FIELDS)
def test_an_empty_set_is_rejected_for_every_predicate_field(field: str) -> None:
    predicate = _predicate(**{field: []})
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1", class_predicate=predicate)))


def test_null_predicate_fields_remain_accepted_as_unconstrained() -> None:
    """The reverse of the empty-set case: every field defaulted to `None`
    is exactly what "unconstrained" means, and must not be rejected."""
    result = _validate(_envelope(_item("item-1", class_predicate=_predicate())))
    assert len(result.envelope_digest) == 64


def test_a_populated_set_is_accepted_and_canonicalized_in_sorted_order() -> None:
    """A set-valued array's canonical form is sorted by its own canonical
    bytes, regardless of the order the request submitted it in -- the
    persisted `class_predicate` reflects that sorted order, not request
    order."""
    predicate = _predicate(intent_kind=["research", "coding"], capability_ids=[str(uuid.uuid4())])
    result = _validate(_envelope(_item("item-1", class_predicate=predicate)))
    assert result.envelope["items"][0]["class_predicate"]["intent_kind"] == ["coding", "research"]


def test_a_duplicate_set_entry_is_rejected_rather_than_silently_deduplicated() -> None:
    """A set-valued array's canonicalizer refuses a literal duplicate
    entry outright (`ProfileValidationFailed`, translated here to
    `EnvelopeInvalid`) rather than silently collapsing it to one -- a
    caller that submitted the same selector twice gets told, not quietly
    corrected."""
    predicate = _predicate(intent_kind=["research", "coding", "research"])
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1", class_predicate=predicate)))


# ---------------------------------------------------------------------------
# The overlap matrix: two items with the same delta code, varied one
# predicate field at a time -- proves overlap detection is exact-match,
# not partial, and that a differing delta code never triggers a false
# overlap. Named per Appendix B.2: "test_arc_envelope.py (named overlap
# matrix)".
# ---------------------------------------------------------------------------


def test_identical_predicate_and_delta_code_overlaps() -> None:
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1"), _item("item-2")))


def test_same_delta_code_different_delta_code_pairing_does_not_overlap() -> None:
    """Two items that share a delta code with different predicates must be
    accepted -- overlap is keyed on (delta_code, predicate), not delta_code
    alone."""
    a = _item("item-1", class_predicate=_predicate(intent_kind=["research"]))
    b = _item("item-2", class_predicate=_predicate(intent_kind=["coding"]))
    result = _validate(_envelope(a, b))
    assert {item["item_id"] for item in result.envelope["items"]} == {"item-1", "item-2"}


def test_identical_predicate_different_delta_code_does_not_overlap() -> None:
    """The other axis of the same key: an identical predicate is fine
    across two different delta codes, since nothing says those two
    deltas are ambiguous with each other."""
    a = _item("item-1", delta_code="newly_selected")
    b = _item("item-2", delta_code="no_longer_selected")
    result = _validate(_envelope(a, b))
    assert [item["delta_code"] for item in result.envelope["items"]] == ["newly_selected", "no_longer_selected"]


@pytest.mark.parametrize(
    "field",
    _PREDICATE_FIELDS,
)
def test_matrix_varying_one_predicate_field_at_a_time_between_two_same_delta_items(field: str) -> None:
    """For each of the six predicate fields independently: two items with
    the same delta code and identical predicates except for one differing
    field must be accepted (no overlap), and the same two items with that
    one field made equal again must be rejected (overlap) -- proving
    overlap comparison is exact on the full predicate object, not merely
    "shares a delta code"."""
    value_by_field: dict[str, tuple[list[str], list[str]]] = {
        "intent_kind": (["research"], ["coding"]),
        "requested_action_classes": (["read"], ["write"]),
        "environment": (["staging"], ["production"]),
        "data_sensitivity_tier": (["low"], ["high"]),
        "capability_ids": ([str(uuid.uuid4())], [str(uuid.uuid4())]),
        "domain_ids": (["docs"], ["billing"]),
    }
    first_value, second_value = value_by_field[field]

    differing_a = _item("item-1", class_predicate=_predicate(**{field: first_value}))
    differing_b = _item("item-2", class_predicate=_predicate(**{field: second_value}))
    _validate(_envelope(differing_a, differing_b))  # differing on `field` alone: no overlap

    same_a = _item("item-1", class_predicate=_predicate(**{field: first_value}))
    same_b = _item("item-2", class_predicate=_predicate(**{field: first_value}))
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(same_a, same_b))  # identical on every field including `field`: overlap


def test_three_items_two_of_which_overlap_still_refuses() -> None:
    a = _item("item-1", class_predicate=_predicate(intent_kind=["research"]))
    b = _item("item-2", class_predicate=_predicate(intent_kind=["coding"]))
    c = _item("item-3", class_predicate=_predicate(intent_kind=["research"]))
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(a, b, c))


# ---------------------------------------------------------------------------
# minimum_count / maximum_count range validation.
# ---------------------------------------------------------------------------


def test_negative_minimum_count_is_rejected() -> None:
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1", minimum_count=-1)))


def test_maximum_below_minimum_is_rejected() -> None:
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1", minimum_count=5, maximum_count=2)))


def test_null_maximum_count_is_accepted_as_unbounded() -> None:
    result = _validate(_envelope(_item("item-1", minimum_count=0, maximum_count=None)))
    assert result.envelope["items"][0]["maximum_count"] is None


def test_maximum_equal_to_minimum_is_accepted() -> None:
    result = _validate(_envelope(_item("item-1", minimum_count=3, maximum_count=3)))
    assert result.envelope["items"][0]["minimum_count"] == result.envelope["items"][0]["maximum_count"] == 3


# ---------------------------------------------------------------------------
# Unknown delta code, profile confusion, and target-identity mismatch.
# ---------------------------------------------------------------------------


def test_unknown_delta_code_is_rejected() -> None:
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1", delta_code="not_a_real_delta_code")))


def test_wrong_profile_literal_is_rejected() -> None:
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1"), profile="arc_observation_class_predicate_v2"))


def test_envelope_naming_a_different_proposal_is_rejected() -> None:
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1"), proposal_id=str(uuid.uuid4())))


def test_envelope_naming_a_different_proposal_version_is_rejected() -> None:
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope(_item("item-1"), proposal_version=_PROPOSAL_VERSION + 1))


def test_zero_items_is_rejected() -> None:
    with pytest.raises(EnvelopeInvalid):
        _validate(_envelope())
