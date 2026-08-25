"""What a prompt asserts, and what a persona is allowed to change.

E24-T7. Two rules carry the weight here, and each would be easy to remove
without any other test noticing:

- **`None` is not a permissive value.** A prompt asserting nothing and a prompt
  asserting that anything passes are different, and only the second reads as
  evidence later.
- **A persona parameterizes the five criteria and never extends them.** One that
  could add a criterion would be a rubric, and two rubrics produce two numbers
  nobody can put side by side — which is the split ADR 0024 refused, one level
  down.
"""

from __future__ import annotations

from typing import Any

import pytest

from contextplane.context.evaluation.envelope_judge import ENVELOPE_JUDGE_VERSION, AuthorizationFacts
from contextplane.context.evaluation.expectations import (
    PRESET_BALANCED,
    PRESET_COMPLIANCE,
    PRESET_NAMES,
    PRESET_RESEARCH,
    PRESETS,
    ExpectationsV1,
    preset,
)
from contextplane.exceptions import ValidationError
from contextplane.extraction.judge_prompt import JUDGE_RUBRIC_VERSION


def _of(**fields: Any) -> ExpectationsV1:
    return ExpectationsV1.of(fields)


# ---------------------------------------------------------------------------
# Absent is not permissive
# ---------------------------------------------------------------------------


def test_no_expectations_at_all_is_a_legal_state() -> None:
    assert ExpectationsV1.of(None) == ExpectationsV1()
    assert ExpectationsV1.of({}) == ExpectationsV1()


def test_an_unasserted_floor_is_none_rather_than_zero() -> None:
    """Zero asserts that anything passes; None asserts nothing."""
    assert _of().min_recall is None
    assert _of(min_recall=0.0).min_recall == 0.0


def test_an_unasserted_id_set_is_none_rather_than_empty() -> None:
    """An empty set says nothing is permitted; None says the prompt makes no claim."""
    assert _of().permitted_tenant_ids is None
    assert _of(permitted_tenant_ids=[]).permitted_tenant_ids == frozenset()


def test_an_unstated_ceiling_falls_back_to_the_scorers_own_default() -> None:
    """A prompt stating no ceiling asks for the standing one, not for no check."""
    assert _of().authorization_facts().max_classification == AuthorizationFacts().max_classification


def test_the_authorization_facts_carry_every_declared_dimension() -> None:
    facts = _of(
        max_classification="internal",
        permitted_instruction_scopes=["digest"],
        permitted_task_ids=["t1"],
        permitted_tenant_ids=["ten"],
        withdrawn_item_keys=["gone"],
    ).authorization_facts()
    assert facts.max_classification == "internal"
    assert facts.permitted_instruction_scopes == frozenset({"digest"})
    assert facts.permitted_task_ids == frozenset({"t1"})
    assert facts.permitted_tenant_ids == frozenset({"ten"})
    assert facts.withdrawn_item_keys == frozenset({"gone"})


# ---------------------------------------------------------------------------
# Validation refuses rather than repairs
# ---------------------------------------------------------------------------


def test_a_misspelled_field_is_refused_rather_than_dropped() -> None:
    """A dropped field leaves a prompt asserting nothing while reading as though it asserts something."""
    with pytest.raises(ValidationError, match="do not carry"):
        ExpectationsV1.of({"min_recal": 0.9})


def test_a_floor_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="0 to 1"):
        _of(min_precision=1.4)


def test_a_boolean_floor_is_refused() -> None:
    with pytest.raises(ValidationError, match="0 to 1"):
        _of(min_recall=True)


def test_a_classification_outside_the_vocabulary_is_refused() -> None:
    with pytest.raises(ValidationError, match="max_classification"):
        _of(max_classification="cosmic")


def test_an_instruction_scope_nothing_can_serve_is_refused() -> None:
    """A declaration that can never be violated is not a check."""
    with pytest.raises(ValidationError, match="never be violated"):
        _of(permitted_instruction_scopes=["everyone"])


def test_an_unknown_preset_name_is_refused() -> None:
    with pytest.raises(ValidationError, match="preset is one of"):
        _of(preset="whatever")


def test_a_key_listed_twice_is_one_requirement() -> None:
    """Counting it twice would move recall's denominator with no fact added to find."""
    assert _of(required_item_keys=["a", "b", "a"]).required_item_keys == ("a", "b")


def test_key_order_is_preserved() -> None:
    assert _of(required_item_keys=["b", "a"]).required_item_keys == ("b", "a")


def test_a_flag_that_is_not_a_boolean_is_refused() -> None:
    with pytest.raises(ValidationError, match="true or false"):
        _of(require_relevance="yes")


# ---------------------------------------------------------------------------
# Round-tripping
# ---------------------------------------------------------------------------


def test_absent_optionals_stay_absent_rather_than_becoming_null() -> None:
    """Two prompts differing only in how their absences are written would compare unequal."""
    stored = _of().stored()
    assert set(stored) == {"require_groundedness", "require_relevance"}


def test_stored_expectations_round_trip() -> None:
    original = _of(
        max_classification="internal",
        min_precision=0.5,
        min_recall=0.9,
        permitted_task_ids=["t1"],
        preset=PRESET_COMPLIANCE,
        relevant_item_keys=["r1"],
        require_relevance=False,
        required_item_keys=["a"],
    )
    assert ExpectationsV1.of(original.stored()) == original


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------


def test_every_seeded_persona_resolves() -> None:
    for name in PRESET_NAMES:
        assert preset(name).name == name


def test_an_unseeded_persona_is_refused_by_name() -> None:
    with pytest.raises(ValidationError, match="unknown preset"):
        preset("aggressive")


def test_a_persona_carries_the_rubric_versions_it_was_written_against() -> None:
    """A threshold on a criterion since redefined is a number describing something else."""
    for entry in PRESETS.values():
        assert entry.envelope_rubric_version == ENVELOPE_JUDGE_VERSION
        assert entry.judge_rubric_version == JUDGE_RUBRIC_VERSION


def test_a_persona_only_ever_parameterizes_the_declared_fields() -> None:
    """One that could add a criterion would be a rubric, not a persona."""
    baseline = set(ExpectationsV1().stored())
    for entry in PRESETS.values():
        assert set(entry.expectations.stored()) >= baseline
        assert ExpectationsV1.of(entry.expectations.stored()) == entry.expectations


def test_no_persona_relaxes_the_boundary_criterion() -> None:
    """Zero tolerance is not a knob a persona turns: the safety question is not how often it leaks."""
    fields = set(ExpectationsV1.__dataclass_fields__)
    assert not any("violation" in name or "tolerance" in name for name in fields)


def test_compliance_pins_the_ceiling_and_research_relaxes_relevance() -> None:
    compliance = PRESETS[PRESET_COMPLIANCE].expectations
    research = PRESETS[PRESET_RESEARCH].expectations
    assert compliance.max_classification == "internal"
    assert compliance.require_relevance is True
    assert research.min_precision is None
    assert research.require_relevance is False
    assert research.require_groundedness is True


def test_the_balanced_persona_asserts_no_numeric_floor() -> None:
    """A floor nobody chose is a threshold somebody will later read as evidence."""
    balanced = PRESETS[PRESET_BALANCED].expectations
    assert balanced.min_recall is None
    assert balanced.min_precision is None


def test_a_persona_records_its_own_name_so_a_reader_sees_where_it_started() -> None:
    for name, entry in PRESETS.items():
        assert entry.expectations.preset == name
