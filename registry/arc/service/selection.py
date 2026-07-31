"""Conflict detection over normalized constraints.

Two directives conflict when they share a `conflict_subject_key` and their
normalized constraints admit no common assignment. Both halves matter: same
subject alone is not a conflict — two directives may add compatible controls over
the same thing — and incompatible constraints over *different* subjects are simply
unrelated.

The implementation is a set intersection over an explicit model of what each
constraint permits, not a table of operator pairs. With four operators and two
modalities there are 64 ordered combinations, and a pairwise table is exactly
where the one nobody thought about hides. Reducing each constraint to "the set of
values this permits" collapses all of them into one comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

from registry.arc.types import (
    ConflictSubjectKey,
    ConstraintOperator,
    Directive,
    Modality,
    NormalizedConstraint,
)


@dataclass(frozen=True)
class _Permitted:
    """What a constraint allows, as either a finite set or a complement of one.

    `is_complement=False` — exactly these values are allowed.
    `is_complement=True`  — everything except these values is allowed.
    """

    values: frozenset[str]
    is_complement: bool

    def intersects(self, other: _Permitted) -> bool:
        """Whether some assignment satisfies both."""
        if not self.is_complement and not other.is_complement:
            # Two finite sets: they must share a member.
            return bool(self.values & other.values)
        if self.is_complement and other.is_complement:
            # Two complements of finite sets over an unbounded universe always
            # leave something out of both.
            return True
        finite, complement = (self.values, other.values) if not self.is_complement else (other.values, self.values)
        # A finite set intersects a complement unless every member is excluded.
        return bool(finite - complement)


def _permitted(constraint: NormalizedConstraint) -> _Permitted:
    """Reduce a constraint to the set of values it permits.

    This is the step that removes the operator table. Once both sides are
    expressed as permitted-value sets, `require`/`prohibit` and all four operators
    compare the same way.
    """
    op = constraint.operator
    prohibit = constraint.modality is Modality.PROHIBIT

    if op is ConstraintOperator.PRESENT:
        # `require present` permits any value; `prohibit present` permits none,
        # which is the empty finite set and therefore intersects nothing.
        return _Permitted(frozenset(), is_complement=not prohibit)

    if op is ConstraintOperator.EQUALS or op is ConstraintOperator.IN_SET:
        # require: only these. prohibit: anything but these.
        return _Permitted(constraint.values, is_complement=prohibit)

    # NOT_IN_SET inverts the sense, so a prohibition of "not in S" permits S.
    return _Permitted(constraint.values, is_complement=not prohibit)


def constraints_are_compatible(left: NormalizedConstraint, right: NormalizedConstraint) -> bool:
    """Whether some assignment satisfies both constraints."""
    return _permitted(left).intersects(_permitted(right))


def directives_conflict(left: Directive, right: Directive) -> bool:
    """Whether two directives conflict.

    A `citation_only` directive never conflicts: it carries no constraint, so
    there is nothing for another directive to be incompatible with. Directives
    with different subjects are additive — ARC makes no attempt to infer semantic
    contradiction from prose, because guessing there would either block valid work
    or, worse, silently permit an action two directives forbid.
    """
    if left.constraint is None or right.constraint is None:
        return False
    if left.conflict_subject is None or right.conflict_subject is None:
        return False
    if left.conflict_subject != right.conflict_subject:
        return False
    if left.directive_id == right.directive_id:
        # Same stable identity: a successor revision replaces its predecessor
        # rather than conflicting with it.
        return False
    return not constraints_are_compatible(left.constraint, right.constraint)


@dataclass(frozen=True)
class ConflictFinding:
    """One incompatible pair, with both sides cited.

    Both revisions are named because `blocked_conflict` must let an operator see
    what disagreed. Picking a winner by revision timestamp would resolve the
    block invisibly and is exactly what ARC does not do.
    """

    subject: ConflictSubjectKey
    left: Directive
    right: Directive

    def revision_pair(self) -> tuple[str, str]:
        return (str(self.left.revision_id), str(self.right.revision_id))


def find_conflicts(directives: list[Directive]) -> list[ConflictFinding]:
    """Every conflicting pair, in a deterministic order.

    Grouped by subject first so the comparison is quadratic only within a subject
    rather than across the whole selection. Output is sorted by subject and then
    by directive id, because a nondeterministic reason list would make two
    identical resolutions produce different receipts.
    """
    by_subject: dict[ConflictSubjectKey, list[Directive]] = {}
    for directive in directives:
        if directive.conflict_subject is None or directive.constraint is None:
            continue
        by_subject.setdefault(directive.conflict_subject, []).append(directive)

    findings: list[ConflictFinding] = []
    for subject in sorted(by_subject):
        group = sorted(by_subject[subject], key=lambda d: str(d.directive_id))
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                if directives_conflict(left, right):
                    findings.append(ConflictFinding(subject=subject, left=left, right=right))
    return findings
