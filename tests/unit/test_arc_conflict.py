"""Conflict detection: the full operator/modality truth table.

Exhaustive rather than representative. Four operators times two modalities is 64
ordered combinations, and the value of reducing constraints to permitted-value
sets is precisely that no combination gets special-cased — so the test walks all
of them and checks the answer against an independent brute-force model.
"""

from __future__ import annotations

import itertools
import uuid

import pytest

from registry.arc.service.selection import (
    ConflictFinding,
    constraints_are_compatible,
    directives_conflict,
    find_conflicts,
)
from registry.arc.types import (
    ConflictSubjectKey,
    ConstraintOperator,
    Directive,
    DirectiveType,
    Modality,
    NormalizedConstraint,
)

# A small concrete universe for the brute-force oracle. Members are drawn from
# and outside the constraint sets so complements are exercised too.
_UNIVERSE = ("approved", "pending", "rejected", "other")


def _subject(**over: str) -> ConflictSubjectKey:
    fields = {
        "schema_version": "arc_conflict_v1",
        "namespace": "deploy",
        "subject_selector": "service:payments",
        "operation": "release",
        "action_class": "deploy",
        "target_selector": "env:production",
    }
    fields.update(over)
    return ConflictSubjectKey(**fields)  # type: ignore[arg-type]


def _c(modality: str, operator: str, value: str | None) -> NormalizedConstraint:
    return NormalizedConstraint.parse(modality, operator, value)


def _satisfies(constraint: NormalizedConstraint, candidate: str) -> bool:
    """Independent oracle: does `candidate` satisfy this constraint?

    Written from the plain meaning of each operator, not from the implementation,
    so agreement between the two is evidence rather than a tautology.
    """
    op = constraint.operator
    if op is ConstraintOperator.PRESENT:
        holds = True
    elif op in (ConstraintOperator.EQUALS, ConstraintOperator.IN_SET):
        holds = candidate in constraint.values
    else:  # NOT_IN_SET
        holds = candidate not in constraint.values
    return holds if constraint.modality is Modality.REQUIRE else not holds


def _brute_force_compatible(a: NormalizedConstraint, b: NormalizedConstraint) -> bool:
    return any(_satisfies(a, v) and _satisfies(b, v) for v in _UNIVERSE)


def _all_constraints() -> list[NormalizedConstraint]:
    out = []
    for modality in ("require", "prohibit"):
        out.append(_c(modality, "present", None))
        out.append(_c(modality, "equals", "approved"))
        out.append(_c(modality, "in_set", "approved,pending"))
        out.append(_c(modality, "not_in_set", "rejected"))
    return out


def test_the_full_truth_table_agrees_with_an_independent_oracle() -> None:
    """All ordered operator/modality pairs, checked against brute force.

    A disagreement here is a real defect: it means some pair of directives is
    judged compatible when no assignment satisfies both, or judged conflicting
    when one does.
    """
    disagreements = []
    for a, b in itertools.product(_all_constraints(), repeat=2):
        got = constraints_are_compatible(a, b)
        expected = _brute_force_compatible(a, b)
        if got != expected:
            disagreements.append(
                f"{a.modality}/{a.operator}{sorted(a.values)} vs "
                f"{b.modality}/{b.operator}{sorted(b.values)}: got {got}, oracle {expected}"
            )
    assert not disagreements, "\n".join(disagreements)


def test_the_table_actually_covers_every_operator_and_modality() -> None:
    """Guards against a vacuous pass if the constraint list were trimmed."""
    constraints = _all_constraints()
    assert {c.operator for c in constraints} == set(ConstraintOperator)
    assert {c.modality for c in constraints} == set(Modality)
    assert len(constraints) == len(ConstraintOperator) * len(Modality)


def test_compatibility_is_symmetric() -> None:
    """Conflict is a property of a pair, not of an evaluation order."""
    for a, b in itertools.product(_all_constraints(), repeat=2):
        assert constraints_are_compatible(a, b) == constraints_are_compatible(b, a)


# --- the specific cases the design calls out -------------------------------


def test_require_and_prohibit_of_the_same_value_conflict() -> None:
    assert not constraints_are_compatible(_c("require", "equals", "approved"), _c("prohibit", "equals", "approved"))


def test_two_unequal_required_values_conflict() -> None:
    assert not constraints_are_compatible(_c("require", "equals", "approved"), _c("require", "equals", "pending"))


def test_disjoint_required_sets_conflict() -> None:
    assert not constraints_are_compatible(_c("require", "in_set", "approved"), _c("require", "in_set", "pending"))


def test_overlapping_required_sets_are_compatible() -> None:
    """Narrowing is additive, not contradictory."""
    assert constraints_are_compatible(
        _c("require", "in_set", "approved,pending"), _c("require", "in_set", "pending,other")
    )


def test_prohibit_present_conflicts_with_any_requirement() -> None:
    """ "must not be set" and "must be set" cannot both hold."""
    assert not constraints_are_compatible(_c("prohibit", "present", None), _c("require", "equals", "approved"))


def test_two_prohibitions_are_compatible_when_something_remains() -> None:
    assert constraints_are_compatible(_c("prohibit", "equals", "approved"), _c("prohibit", "equals", "pending"))


# --- directive-level conflict ----------------------------------------------


def _directive(did: str, constraint: NormalizedConstraint, subject: ConflictSubjectKey | None = None) -> Directive:
    return Directive(
        directive_id=uuid.UUID(did),
        revision_id=uuid.uuid4(),
        directive_type=DirectiveType.REQUIRE,
        source_anchor="a#1",
        conflict_subject=subject or _subject(),
        constraint=constraint,
    )


_ID_A = "aaaaaaaa-0000-4000-8000-000000000001"
_ID_B = "bbbbbbbb-0000-4000-8000-000000000002"


def test_incompatible_directives_on_the_same_subject_conflict() -> None:
    a = _directive(_ID_A, _c("require", "equals", "approved"))
    b = _directive(_ID_B, _c("prohibit", "equals", "approved"))
    assert directives_conflict(a, b) is True


def test_incompatible_directives_on_different_subjects_do_not_conflict() -> None:
    """Different subjects are additive; ARC does not infer contradiction from prose."""
    a = _directive(_ID_A, _c("require", "equals", "approved"))
    b = _directive(_ID_B, _c("prohibit", "equals", "approved"), _subject(operation="rollback"))
    assert directives_conflict(a, b) is False


def test_a_successor_revision_of_the_same_identity_does_not_conflict() -> None:
    """Same stable id means replacement, not disagreement."""
    a = _directive(_ID_A, _c("require", "equals", "approved"))
    b = _directive(_ID_A, _c("require", "equals", "pending"))
    assert directives_conflict(a, b) is False


def test_citation_only_never_conflicts() -> None:
    """It carries no constraint, so there is nothing to be incompatible with."""
    citation = Directive(
        directive_id=uuid.UUID(_ID_A),
        revision_id=uuid.uuid4(),
        directive_type=DirectiveType.CITATION_ONLY,
        source_anchor="a#1",
    )
    enforceable = _directive(_ID_B, _c("require", "equals", "approved"))
    assert directives_conflict(citation, enforceable) is False
    assert directives_conflict(enforceable, citation) is False


# --- find_conflicts --------------------------------------------------------


def test_find_conflicts_reports_both_revisions() -> None:
    """blocked_conflict must let an operator see what disagreed."""
    a = _directive(_ID_A, _c("require", "equals", "approved"))
    b = _directive(_ID_B, _c("prohibit", "equals", "approved"))
    findings = find_conflicts([a, b])
    assert len(findings) == 1
    assert isinstance(findings[0], ConflictFinding)
    assert set(findings[0].revision_pair()) == {str(a.revision_id), str(b.revision_id)}


def test_find_conflicts_is_deterministic_under_input_reordering() -> None:
    """A nondeterministic reason list would make identical resolutions differ."""
    a = _directive(_ID_A, _c("require", "equals", "approved"))
    b = _directive(_ID_B, _c("prohibit", "equals", "approved"))
    forward = [(f.left.directive_id, f.right.directive_id) for f in find_conflicts([a, b])]
    reverse = [(f.left.directive_id, f.right.directive_id) for f in find_conflicts([b, a])]
    assert forward == reverse


def test_compatible_directives_produce_no_findings() -> None:
    a = _directive(_ID_A, _c("require", "in_set", "approved,pending"))
    b = _directive(_ID_B, _c("require", "in_set", "pending"))
    assert find_conflicts([a, b]) == []


def test_no_findings_for_an_empty_selection() -> None:
    assert find_conflicts([]) == []
