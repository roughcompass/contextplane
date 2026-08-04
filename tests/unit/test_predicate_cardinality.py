"""How many values a predicate may hold, and why guessing it is not an option.

Cardinality decides whether two differing values are a disagreement or two
facts. Getting it wrong for a set-valued predicate is not a noise problem: a
detected disagreement marks both claims contested, a contested claim cannot be
promoted and always needs review, and no reviewer can resolve it because both
values are true and neither supersedes the other.

The tests that matter most are the two disproofs — that neither the value type
nor the category determines cardinality. Those are what justify carrying a third
declared property instead of deriving it, and if either stopped holding, the
column would be redundant.
"""

from __future__ import annotations

from collections import defaultdict

from registry.service.catalog.global_vocabulary import (
    CARDINALITY_MULTI,
    CARDINALITY_SINGLE,
    VALUE_CARDINALITIES,
)
from registry.service.memory.claim_ontology import ONTOLOGY

_BY_NAME = {seed.value: seed for seed in ONTOLOGY}


def test_every_shipped_predicate_declares_a_cardinality() -> None:
    """No default. A predicate whose cardinality was never considered is one
    whose claims may become permanently unpromotable."""
    for seed in ONTOLOGY:
        assert seed.value_cardinality in VALUE_CARDINALITIES, seed.value


def test_value_type_does_not_determine_cardinality() -> None:
    """The first disproof, and the reason this is a declared property.

    If a type implied a cardinality, the column would be redundant and should be
    deleted. It does not: two entity-reference predicates and two string
    predicates disagree.
    """
    by_type: dict[str, set[str]] = defaultdict(set)
    for seed in ONTOLOGY:
        by_type[seed.value_type].add(seed.value_cardinality)

    ambiguous = {t: c for t, c in by_type.items() if len(c) > 1}
    assert ambiguous, "no value type carries both cardinalities; the column may be derivable"
    # The specific pairs the reasoning rests on.
    assert _BY_NAME["steward_entity"].value_cardinality == CARDINALITY_MULTI
    assert _BY_NAME["interface_version"].value_cardinality == CARDINALITY_SINGLE
    assert _BY_NAME["exposes_operation"].value_cardinality == CARDINALITY_MULTI
    assert _BY_NAME["owned_by_team"].value_cardinality == CARDINALITY_SINGLE


def test_claim_category_does_not_determine_cardinality() -> None:
    """The second disproof. `interface_contract` holds both."""
    by_category: dict[str, set[str]] = defaultdict(set)
    for seed in ONTOLOGY:
        by_category[seed.claim_category].add(seed.value_cardinality)

    ambiguous = {c: v for c, v in by_category.items() if len(v) > 1}
    assert ambiguous, "no category carries both cardinalities; the column may be derivable"
    assert _BY_NAME["exposes_operation"].claim_category == "interface_contract"
    assert _BY_NAME["request_timeout_seconds"].claim_category == "interface_contract"
    assert _BY_NAME["exposes_operation"].value_cardinality != _BY_NAME["request_timeout_seconds"].value_cardinality


def test_every_relation_naming_a_third_entity_is_set_valued() -> None:
    """A dependency names something else, so a second value is a second
    dependency. Treating these as single-valued would flag every normal
    multi-dependency capability."""
    for name in ("depends_on", "composes", "provides_to", "conflicts_with", "steward_entity"):
        assert _BY_NAME[name].value_cardinality == CARDINALITY_MULTI, name


def test_a_capability_may_be_deployed_in_several_environments() -> None:
    """The clearest case. Staging and production hold at once."""
    assert _BY_NAME["deployment_environment"].value_cardinality == CARDINALITY_MULTI


def test_decision_properties_are_set_valued_because_decisions_accumulate() -> None:
    """The supersession predicate exists precisely because a subject collects
    decision records. If it did not, there would be nothing to supersede — so a
    subject with three records has three timestamps, and none of them conflict."""
    assert _BY_NAME["supersedes_decision"].value_cardinality == CARDINALITY_MULTI
    for name in ("decision_record_url", "decided_at", "decision_status"):
        assert _BY_NAME[name].value_cardinality == CARDINALITY_MULTI, name


def test_deprecation_is_single_valued_although_decision_timestamps_are_not() -> None:
    """The asymmetry is deliberate and worth pinning. Deprecation is a property
    of the capability — one instant — while a decision timestamp is a property of
    a decision the triple cannot name."""
    assert _BY_NAME["deprecated_after"].value_cardinality == CARDINALITY_SINGLE
    assert _BY_NAME["decided_at"].value_cardinality == CARDINALITY_MULTI


def test_accountability_is_singular_but_escalation_is_a_ladder() -> None:
    """One team answers for a capability; escalation runs through several
    people. The pair is why cardinality cannot follow the category."""
    assert _BY_NAME["owned_by_team"].value_cardinality == CARDINALITY_SINGLE
    assert _BY_NAME["on_call_rotation"].value_cardinality == CARDINALITY_SINGLE
    assert _BY_NAME["escalation_contact"].value_cardinality == CARDINALITY_MULTI


def test_the_under_specified_version_predicate_is_set_valued() -> None:
    """Its value constrains some dependency and the triple cannot say which, so
    two values may describe two different dependencies. Comparing them would
    compare unrelated things."""
    assert _BY_NAME["depends_on_version"].value_cardinality == CARDINALITY_MULTI


def test_thresholds_and_targets_are_single_valued() -> None:
    """Each answers one question, so two answers means one is wrong."""
    for name in (
        "request_timeout_seconds",
        "max_request_bytes",
        "recovery_time_objective_seconds",
        "target_availability",
        "is_publicly_callable",
        "lifecycle_state",
        "runbook_url",
        "interface_specification_url",
    ):
        assert _BY_NAME[name].value_cardinality == CARDINALITY_SINGLE, name


def test_the_split_is_neither_all_single_nor_all_multi() -> None:
    """A degenerate split would mean the property was never really applied."""
    counts = defaultdict(int)
    for seed in ONTOLOGY:
        counts[seed.value_cardinality] += 1
    assert counts[CARDINALITY_SINGLE] >= 5
    assert counts[CARDINALITY_MULTI] >= 5


def test_every_single_valued_predicate_has_a_strictly_validated_type() -> None:
    """Why an unreadable value cannot reach the comparison, and what would break it.

    The comparison returns "cannot tell" for a value it cannot parse, and that
    branch is deliberately unreachable from stored data: every type a
    single-valued predicate can declare is parsed at the write path, so a stored
    value is always readable. That is why removing the check does not currently
    fail anything — it is defence in depth, not live logic.

    This test is what keeps it that way. Adding a single-valued predicate whose
    type is only shape-checked would make unreadable values storable, and from
    then on two of them would compare as "cannot tell" forever with nothing
    surfacing it.
    """
    # Types the write path parses rather than merely shape-checks: numbers are
    # real numbers, timestamps are instants, URLs are absolute, version ranges
    # expand, booleans are booleans.
    strictly_validated = {
        "boolean",
        "integer",
        "bytes",
        "duration_seconds",
        "decimal",
        "timestamp_utc",
        "url",
        "version_predicate",
        # Free text and vocabulary members are always readable as text, so the
        # comparison can always fold them.
        "string",
        "enum",
    }

    loose = [
        seed.value
        for seed in ONTOLOGY
        if seed.value_cardinality == CARDINALITY_SINGLE and seed.value_type not in strictly_validated
    ]
    assert not loose, (
        f"single-valued predicates with a loosely-validated type: {loose}. "
        "An unparseable stored value would compare as undecidable forever."
    )


def test_prose_is_never_single_valued() -> None:
    """A paragraph cannot be compared, so a single-valued prose predicate would
    declare a disagreement that nothing could ever detect."""
    for seed in ONTOLOGY:
        if seed.value_type == "prose":
            assert seed.value_cardinality == CARDINALITY_MULTI, seed.value
