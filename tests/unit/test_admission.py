"""How admission decides, without a scanner fixture or a database.

The conformance suite proves the floor holds against every prohibited class on
every pilot field type -- that is the contract. This file covers the decisions
around it: how the floor is built, what happens at the edges of the vocabulary,
and the invariants the decision object enforces on its own construction.

Split this way because the two answer different questions. Conformance asks
"does a card number get in"; this asks "is the thing that refuses it built so it
cannot quietly stop refusing" -- a floor assembled from the shipped detectors
keeps working when a detector is added, and one written out by hand does not.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.context.admission import (
    PILOT_FIELD_TYPES,
    PROHIBITED_CLASSES,
    TRIGGER_PII_BLOCKED,
    AdmissionDecision,
    NotAPilotField,
    RefusalRecord,
    admit,
    blocking_field_policies,
)
from contextplane.security.pii_patterns import BUILT_IN_PATTERNS

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SSN = "patient ssn 123-45-6789 on file"


def _admit(text: str, field_type: str = "claim_value") -> AdmissionDecision:
    return admit(text, field_type=field_type, tenant_id=_TENANT, now=_NOW)


# --- The vocabulary is derived, not restated ----------------------------------


def test_the_prohibited_classes_are_the_shipped_detectors() -> None:
    """Read off the registry rather than listed. A hand-written list is a second
    source of truth, and the two disagree the first time a detector is added --
    in the direction that silently admits the new class."""
    assert PROHIBITED_CLASSES == {pattern.name for pattern in BUILT_IN_PATTERNS}


def test_the_floor_covers_the_whole_cross_product() -> None:
    """Built rather than written out, so a new detector or a new field type
    extends the floor by construction."""
    policies = blocking_field_policies()

    assert len(policies) == len(PILOT_FIELD_TYPES) * len(PROHIBITED_CLASSES)
    assert set(policies.values()) == {"block"}


def test_the_floor_keys_match_the_scanner_lookup_shape() -> None:
    """`field_type:class`. A key in any other shape is a policy the scanner
    never consults, which looks like a floor and is not."""
    for key in blocking_field_policies():
        field_type, _, pii_class = key.partition(":")
        assert field_type in PILOT_FIELD_TYPES
        assert pii_class in PROHIBITED_CLASSES


def test_the_pilot_field_types_are_the_five_the_inventory_names() -> None:
    assert PILOT_FIELD_TYPES == {
        "memory_session_event.body",
        "artifact.body",
        "claim_value",
        "workspace_entry.body",
        "workspace_entry.references",
    }


# --- Edges of the decision ----------------------------------------------------


def test_empty_content_is_admitted() -> None:
    """Nothing to refuse. Worth pinning because a scanner that errored on empty
    input would turn every no-op write into a refusal."""
    assert _admit("").admitted


def test_content_with_no_prohibited_class_is_admitted() -> None:
    assert _admit("the deploy finished and the queue drained").admitted


def test_a_refusal_reports_every_class_once_in_detection_order() -> None:
    decision = _admit(f"{_SSN} then again {_SSN}")

    assert decision.classes == ("ssn",)


def test_an_unknown_field_type_raises_rather_than_admitting() -> None:
    """Silence on an unrecognised field is how admission gets switched off for a
    surface by a typo."""
    with pytest.raises(NotAPilotField):
        _admit(_SSN, "workspace_entry.bodies")


def test_the_error_names_the_field_types_that_do_have_a_floor() -> None:
    """A refusal a caller cannot act on costs a round trip through the source."""
    with pytest.raises(NotAPilotField) as error:
        _admit(_SSN, "not_a_field")

    for field_type in PILOT_FIELD_TYPES:
        assert field_type in str(error.value)


@pytest.mark.parametrize("field_type", sorted(PILOT_FIELD_TYPES))
def test_the_refusal_names_the_field_it_was_refused_for(field_type: str) -> None:
    """`target_type` is the field, so an auditor reading the row knows which
    surface obligation was being met."""
    assert _admit(_SSN, field_type).refusals[0].target_type == field_type


# --- The decision object defends itself ---------------------------------------


def test_an_admitted_decision_carries_no_refusals() -> None:
    assert AdmissionDecision(admitted=True).refusals == ()


def test_an_admitted_decision_with_refusals_is_refused_at_construction() -> None:
    """The shape that gets misread: a caller acts on the boolean and never sees
    the reasons."""
    refusal = _admit(_SSN).refusals[0]

    with pytest.raises(ValueError, match="cannot carry refusals"):
        AdmissionDecision(admitted=True, refusals=(refusal,))


def test_a_refusal_with_no_reason_is_refused_at_construction() -> None:
    """An unexplained refusal is indistinguishable from a bug, and gets
    retried."""
    with pytest.raises(ValueError, match="must say why"):
        AdmissionDecision(admitted=False)


def test_the_decision_is_frozen() -> None:
    """A decision a caller can edit is a decision that can be turned into an
    admission after the fact."""
    decision = _admit(_SSN)

    with pytest.raises(AttributeError):
        decision.admitted = True  # type: ignore[misc]


def test_a_refusal_record_is_frozen() -> None:
    refusal = _admit(_SSN).refusals[0]

    with pytest.raises(AttributeError):
        refusal.detail = "something else"  # type: ignore[misc]


# --- The audit payload --------------------------------------------------------


def test_the_audit_payload_is_explicit_about_what_it_carries() -> None:
    """Built field by field rather than from `asdict`, so a field added to the
    record has to be considered before it reaches a durable row -- which is the
    moment to notice that it should not."""
    payload = _admit(_SSN).refusals[0].as_audit_payload()

    assert set(payload) == {"trigger", "pii_class", "pii_category", "detail", "strategy_id", "field_type"}


def test_the_audit_payload_carries_no_identifiers_that_belong_in_columns() -> None:
    """Tenant, actor and target are columns on the audit row. Duplicating them
    into the payload would let the two disagree."""
    payload = _admit(_SSN).refusals[0].as_audit_payload()

    assert not ({"tenant_id", "actor_id", "target_id", "occurred_at"} & set(payload))


def test_the_trigger_is_the_generic_code_and_the_class_is_separate() -> None:
    refusal = _admit(_SSN).refusals[0]

    assert refusal.trigger == TRIGGER_PII_BLOCKED
    assert refusal.pii_class == "ssn"
    assert refusal.pii_class != refusal.trigger


def test_the_record_has_no_field_that_locates_the_value() -> None:
    """The scanner reports an offset and a length; the record drops both. An
    offset is not the value, but it points at one inside stored text."""
    fields = set(RefusalRecord.__dataclass_fields__)

    assert not (fields & {"offset", "length", "match_offset", "match_length", "excerpt", "value"})
