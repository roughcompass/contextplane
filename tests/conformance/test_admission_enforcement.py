"""Admission refuses prohibited content, on a deployment configured with nothing.

The property under test is not "the scanner detects things" -- it always did.
It is that detection now has a consequence that does not depend on somebody
having inserted a policy row. Before admission, a fresh deployment detected a
card number, logged it, and stored it, because the escalation to blocking was
configuration and the default was advisory.

So every test here runs against admission's own floor with no tenant policy, no
pattern overrides and no field-policy rows. If any of them start passing only
because a fixture configured something, the thing being proved has been lost.

Two rules about the refusal record get their own tests because both are easy to
violate while looking correct: the record must never carry the offending value
or a pointer to it, and a prohibited class must never become a trigger.
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

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")

#: One specimen per prohibited class. Values are syntactically valid and
#: entirely fabricated -- a real credential in a test file is the thing this
#: module exists to keep out of storage.
_SPECIMENS: dict[str, str] = {
    "ssn": "patient ssn 123-45-6789 on file",
    "credit_card": "card 4111 1111 1111 1111 charged",
    "email": "reach me at someone@example.com",
    "phone": "call +1 415 555 0132 after six",
    "aws_access_key": "key AKIAIOSFODNN7EXAMPLE rotated",
    "aws_secret_key": "secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY here",
    "jwt_token": "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
}


def _admit(text: str, field_type: str) -> AdmissionDecision:
    return admit(text, field_type=field_type, tenant_id=_TENANT, now=_NOW)


# --- The floor ----------------------------------------------------------------


def test_every_prohibited_class_has_a_specimen() -> None:
    """Guards the table below. A class added to the detectors without a
    specimen here would silently stop being tested, and the suite would keep
    reporting full coverage of a set that had grown."""
    assert set(_SPECIMENS) == PROHIBITED_CLASSES


@pytest.mark.parametrize("pii_class", sorted(_SPECIMENS))
@pytest.mark.parametrize("field_type", sorted(PILOT_FIELD_TYPES))
def test_each_prohibited_class_is_refused_on_each_pilot_field(pii_class: str, field_type: str) -> None:
    """Thirty-five combinations, none of which needs a policy row.

    Parametrised over both axes rather than spot-checked because the failure
    this prevents is per-pair: a floor that covers six classes on five fields
    and misses the seventh reads as complete from any single example.
    """
    decision = _admit(_SPECIMENS[pii_class], field_type)

    assert not decision.admitted, f"{pii_class} was admitted into {field_type}"
    assert pii_class in decision.classes


def test_clean_content_is_admitted() -> None:
    """The half that makes the refusals meaningful: a floor that refused
    everything would pass every test above."""
    for field_type in sorted(PILOT_FIELD_TYPES):
        assert _admit("the deploy finished and the queue drained", field_type).admitted


def test_the_floor_needs_no_configuration() -> None:
    """Stated directly, because this is the correction admission exists to make.

    The generated policy map blocks every class on every pilot field, so the
    outcome does not depend on a `pii_field_policies` row existing.
    """
    policies = blocking_field_policies()

    assert len(policies) == len(PILOT_FIELD_TYPES) * len(PROHIBITED_CLASSES)
    assert set(policies.values()) == {"block"}
    for field_type in PILOT_FIELD_TYPES:
        for pii_class in PROHIBITED_CLASSES:
            assert policies[f"{field_type}:{pii_class}"] == "block"


def test_content_carrying_two_classes_is_refused_for_both() -> None:
    """A caller told about one problem fixes it and is refused again. Reporting
    every reason at once is the difference between one round trip and several."""
    decision = _admit(f"{_SPECIMENS['ssn']} and {_SPECIMENS['aws_secret_key']}", "claim_value")

    assert not decision.admitted
    assert {"ssn", "aws_secret_key"} <= set(decision.classes)


def test_a_class_is_reported_once_however_often_it_appears() -> None:
    """Three card numbers are one problem, not three."""
    decision = _admit(" ".join([_SPECIMENS["credit_card"]] * 3), "artifact.body")

    assert decision.classes.count("credit_card") == 1


def test_a_field_with_no_floor_is_refused_rather_than_admitted() -> None:
    """A mistyped field name must not be how admission gets switched off for a
    surface. Silence on an unknown field is the failure that looks like
    success."""
    with pytest.raises(NotAPilotField, match="no admission floor"):
        _admit(_SPECIMENS["ssn"], "memory_session_event.bdoy")


# --- What a refusal records ---------------------------------------------------


def test_a_refusal_never_reproduces_the_offending_value() -> None:
    """The record is written to a durable, widely-read place. Copying the
    prohibited value into it puts the value in the one row guaranteed to be
    retained."""
    for pii_class, specimen in _SPECIMENS.items():
        decision = _admit(specimen, "memory_session_event.body")
        for refusal in decision.refusals:
            rendered = f"{refusal.detail} {refusal.as_audit_payload()}"
            for token in specimen.split():
                if len(token) > 8 and any(character.isdigit() for character in token):
                    assert token not in rendered, f"{pii_class} refusal leaked {token!r}"


def test_a_refusal_carries_no_pointer_to_the_value_either() -> None:
    """An offset is not the value, but it locates one inside stored text. The
    scanner reports both; the record deliberately drops them."""
    refusal = _admit(_SPECIMENS["ssn"], "claim_value").refusals[0]
    fields = {field.name for field in RefusalRecord.__dataclass_fields__.values()}

    assert not ({"offset", "length", "match_offset", "match_length", "excerpt"} & fields)
    assert "offset" not in refusal.as_audit_payload()


def test_a_prohibited_class_is_never_a_trigger() -> None:
    """The trigger vocabulary is a metric label with a closed set. If a class
    could be a trigger, the set would grow by one per detector."""
    for specimen in _SPECIMENS.values():
        for refusal in _admit(specimen, "workspace_entry.body").refusals:
            assert refusal.trigger == TRIGGER_PII_BLOCKED
            assert refusal.trigger not in PROHIBITED_CLASSES


def test_a_refusal_carries_the_scope_an_auditor_needs() -> None:
    actor = uuid.uuid4()
    target = uuid.uuid4()

    decision = admit(
        _SPECIMENS["ssn"],
        field_type="workspace_entry.references",
        tenant_id=_TENANT,
        now=_NOW,
        actor_id=actor,
        target_id=target,
        strategy_id="strategy-7",
    )
    refusal = decision.refusals[0]

    assert (refusal.tenant_id, refusal.actor_id, refusal.target_id) == (_TENANT, actor, target)
    assert refusal.target_type == "workspace_entry.references"
    assert refusal.occurred_at == _NOW
    assert refusal.strategy_id == "strategy-7"


def test_attribution_is_optional_and_says_so() -> None:
    """`strategy_id` is set only where a namespace is present, so a record
    without one is ordinary rather than broken -- and an auditor reading a null
    should not conclude the writer failed."""
    refusal = _admit(_SPECIMENS["email"], "artifact.body").refusals[0]

    assert refusal.strategy_id is None
    assert refusal.actor_id is None


# --- The decision itself ------------------------------------------------------


def test_an_admitted_decision_cannot_carry_refusals() -> None:
    """The shape that gets misread. A decision that says "admitted" while
    carrying reasons will be acted on by its boolean."""
    with pytest.raises(ValueError, match="cannot carry refusals"):
        AdmissionDecision(
            admitted=True,
            refusals=(_admit(_SPECIMENS["ssn"], "claim_value").refusals[0],),
        )


def test_a_refusal_must_say_why() -> None:
    """An unexplained refusal is indistinguishable from a bug, and gets
    retried."""
    with pytest.raises(ValueError, match="must say why"):
        AdmissionDecision(admitted=False)
