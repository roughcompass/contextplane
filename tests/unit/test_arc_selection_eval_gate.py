"""The ARC context-selection eval gate: fixture-driven measurement of select().

Three of the four metrics ARC selection is evaluated against live here,
because none of them need a database. `select()` in
`registry.arc.service.selection` is a pure function over `SelectionInput` --
a manifest plus a snapshot of candidate directives, rules, exceptions, and
obligations -- so a fixture file plus this loader is the whole apparatus
needed to measure it. The fourth metric, stale-receipt denial, depends on
revocation state that only a live database models; it lives in
`tests/integration/test_arc_stale_receipt_denial.py`.

What each metric measures, in its own terms:

- **mandatory-inclusion recall** -- of the (directive, revision) pairs a case
  says must be in the mandatory set, what fraction actually are. This is the
  metric a governed agent cares about most directly: an obligation that
  silently fails to appear is worse than one that is visibly blocked, because
  nobody is told to go fix anything.
- **prohibited-inclusion rate** -- of the directive ids a case says must never
  appear (a directive owned by a different tenant, or one whose window has
  closed), how many show up anyway, in either the mandatory or the optional
  set. This is a tenant-isolation and time-window invariant, so the threshold
  is exact: any leak at all is a failure, not a rate to be minimised.
- **precedence-conflict detection** -- of the conflicting directive pairs a
  case says selection should find, what fraction it actually reports in
  `result.conflicts`. Selection only ever records conflicts among the
  *mandatory* set (see `select()`); an optional-vs-optional conflict degrades
  the resolution but is not itself a reported pair, so cases built to exercise
  that path list no expected conflicts.

Each fixture case also gets a full strict-equality check -- status, both
directive sets, the must-not-appear list, both reason tuples, and the
conflict pairs -- which is what makes a failure attributable to one case_id
rather than showing up only as a shifted aggregate number.
"""

from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from registry.arc.service.selection import (
    ApprovedException,
    ConflictFinding,
    MandatoryObligation,
    ScopedDirective,
    SelectionInput,
    SelectionResult,
    select,
)
from registry.arc.types import (
    ActionClass,
    ApplicabilityRule,
    AuthorityScope,
    ConflictSubjectKey,
    Directive,
    DirectiveType,
    NormalizedConstraint,
    ResolutionStatus,
    TaskKind,
    TaskManifest,
)

_FIXTURE_PATH = Path(__file__).parent.parent.parent / "eval" / "fixtures" / "arc_selection_cases.json"

# Exact thresholds, not approximations. select() is a deterministic pure
# function (see test_arc_determinism.py's NF1.1 sweep), so a fixture case
# that cannot reproduce these exactly means the fixture is wrong or the
# engine regressed -- there is no principled "close enough" for either.
_MANDATORY_INCLUSION_RECALL_THRESHOLD = 1.0
_PROHIBITED_INCLUSION_RATE_THRESHOLD = 0.0
_PRECEDENCE_CONFLICT_DETECTION_THRESHOLD = 1.0


# ---------------------------------------------------------------------------
# Fixture loading: JSON -> the exact dataclasses select() reads.
# ---------------------------------------------------------------------------


def _uid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


def _dt(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def _maybe_dt(value: str | None) -> datetime.datetime | None:
    return None if value is None else _dt(value)


def _str_set(values: list[str] | None) -> frozenset[str]:
    return frozenset(values or ())


def _uuid_set(values: list[str] | None) -> frozenset[uuid.UUID]:
    return frozenset(_uid(v) for v in values or ())


def _tenant_ref(value: str | None, *, case_tenant_id: uuid.UUID) -> uuid.UUID | None:
    """'self' resolves to the case's own tenant; anything else is a literal UUID.

    The sentinel exists so a fixture author writing a tenant-scoped rule that
    targets the requester does not have to repeat the tenant_id verbatim --
    the one case that needs a genuinely different tenant (arc-sel-002) simply
    writes that tenant's literal UUID instead.
    """
    if value is None:
        return None
    if value == "self":
        return case_tenant_id
    return _uid(value)


def _load_conflict_subject(raw: dict[str, Any] | None) -> ConflictSubjectKey | None:
    if raw is None:
        return None
    return ConflictSubjectKey(
        schema_version=raw["schema_version"],
        namespace=raw["namespace"],
        subject_selector=raw["subject_selector"],
        operation=raw["operation"],
        action_class=raw["action_class"],
        target_selector=raw["target_selector"],
    )


def _load_constraint(raw: dict[str, Any] | None) -> NormalizedConstraint | None:
    if raw is None:
        return None
    return NormalizedConstraint.parse(raw["modality"], raw["operator"], raw.get("value"))


def _load_directive(raw: dict[str, Any]) -> Directive:
    return Directive(
        directive_id=_uid(raw["directive_id"]),
        revision_id=_uid(raw["revision_id"]),
        directive_type=DirectiveType(raw["directive_type"]),
        source_anchor=raw["source_anchor"],
        conflict_subject=_load_conflict_subject(raw.get("conflict_subject")),
        constraint=_load_constraint(raw.get("constraint")),
        delegable_exception=raw.get("delegable_exception", False),
    )


def _load_rule(raw: dict[str, Any], *, revision_id: uuid.UUID, case_tenant_id: uuid.UUID) -> ApplicabilityRule:
    # revision_id is not repeated in the JSON: a rule always belongs to the
    # same revision as the directive it was loaded beside, so the loader
    # derives it rather than giving a fixture author a second place to keep
    # the two in sync.
    return ApplicabilityRule(
        rule_id=_uid(raw["rule_id"]),
        revision_id=revision_id,
        scope=AuthorityScope(raw["scope"]),
        is_mandatory=raw.get("is_mandatory", True),
        target_tenant_id=_tenant_ref(raw.get("target_tenant_id"), case_tenant_id=case_tenant_id),
        capability_ids=_uuid_set(raw.get("capability_ids")),
        capability_labels=_str_set(raw.get("capability_labels")),
        domain_ids=_str_set(raw.get("domain_ids")),
        task_kinds=frozenset(TaskKind(v) for v in raw.get("task_kinds", [])),
        action_classes=frozenset(ActionClass(v) for v in raw.get("action_classes", [])),
        environments=_str_set(raw.get("environments")),
        data_sensitivity_tiers=_str_set(raw.get("data_sensitivity_tiers")),
        effective_from=_maybe_dt(raw.get("effective_from")),
        effective_until=_maybe_dt(raw.get("effective_until")),
    )


def _load_manifest(raw: dict[str, Any]) -> TaskManifest:
    return TaskManifest(
        session_id=raw.get("session_id", "s1"),
        task_kind=TaskKind(raw["task_kind"]),
        requested_action_classes=frozenset(ActionClass(v) for v in raw.get("requested_action_classes", [])),
        capability_ids=_uuid_set(raw.get("capability_ids")),
        domain_ids=_str_set(raw.get("domain_ids")),
        environment=raw.get("environment"),
        data_sensitivity=raw.get("data_sensitivity"),
    )


def _load_exception(raw: dict[str, Any], *, case_tenant_id: uuid.UUID) -> ApprovedException:
    lower_scope_tenant_id = _tenant_ref(raw["lower_scope_tenant_id"], case_tenant_id=case_tenant_id)
    assert lower_scope_tenant_id is not None, "an exception must name a lower-scope tenant"
    return ApprovedException(
        exception_id=_uid(raw["exception_id"]),
        higher_scope_directive_id=_uid(raw["higher_scope_directive_id"]),
        higher_scope_revision_id=_uid(raw["higher_scope_revision_id"]),
        lower_scope_tenant_id=lower_scope_tenant_id,
        replacement_constraint=_load_constraint(raw["replacement_constraint"]),  # type: ignore[arg-type]
        effective_from=_maybe_dt(raw.get("effective_from")),
        effective_until=_maybe_dt(raw.get("effective_until")),
        revoked_at=_maybe_dt(raw.get("revoked_at")),
    )


def _load_obligation(raw: dict[str, Any]) -> MandatoryObligation:
    return MandatoryObligation(
        obligation_id=_uid(raw["obligation_id"]),
        directive_id=_uid(raw["directive_id"]),
        obligation_state=raw["obligation_state"],
        applicability_digest=raw.get("applicability_digest", "0" * 64),
    )


class FixtureCase:
    """One fixture case: the SelectionInput it builds, plus its expectation.

    A thin wrapper rather than a bare tuple so pytest's parametrize ids and
    the metric functions below can both read `case_id` and `description`
    without unpacking positionally.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self.case_id: str = raw["case_id"]
        self.description: str = raw["description"]
        tenant_id = _uid(raw["tenant_id"])
        as_of = _dt(raw["as_of"])

        candidates: list[tuple[Directive, ApplicabilityRule, datetime.datetime]] = []
        for entry in raw.get("candidates", []):
            directive = _load_directive(entry["directive"])
            rule = _load_rule(entry["rule"], revision_id=directive.revision_id, case_tenant_id=tenant_id)
            candidates.append((directive, rule, _dt(entry["revision_effective_from"])))

        self.inputs = SelectionInput(
            manifest=_load_manifest(raw["manifest"]),
            tenant_id=tenant_id,
            as_of=as_of,
            candidates=tuple(candidates),
            exceptions=tuple(_load_exception(e, case_tenant_id=tenant_id) for e in raw.get("exceptions", [])),
            obligations=tuple(_load_obligation(o) for o in raw.get("obligations", [])),
        )
        self.expected: dict[str, Any] = raw["expected"]

    # -- expectation accessors, each returning the shape its metric compares --

    @property
    def expected_status(self) -> ResolutionStatus:
        return ResolutionStatus(self.expected["status"])

    @property
    def expected_mandatory(self) -> frozenset[tuple[str, str]]:
        return _pairs(self.expected["mandatory"])

    @property
    def expected_optional(self) -> frozenset[tuple[str, str]]:
        return _pairs(self.expected["optional"])

    @property
    def expected_must_not_appear(self) -> frozenset[str]:
        return frozenset(str(_uid(v)) for v in self.expected["must_not_appear_directive_ids"])

    @property
    def expected_blocked_reasons(self) -> frozenset[str]:
        return frozenset(self.expected["blocked_reasons"])

    @property
    def expected_degraded_reasons(self) -> frozenset[str]:
        return frozenset(self.expected["degraded_reasons"])

    @property
    def expected_conflicts(self) -> frozenset[frozenset[str]]:
        return frozenset(frozenset({str(_uid(a)), str(_uid(b))}) for a, b in self.expected["conflicts"])

    @property
    def expected_applied_exception_ids(self) -> frozenset[str] | None:
        """None means the case makes no claim; an empty set is a real claim of 'none applied'."""
        raw_ids = self.expected.get("applied_exception_ids")
        return None if raw_ids is None else frozenset(str(_uid(v)) for v in raw_ids)


def _pairs(raw_list: list[dict[str, str]]) -> frozenset[tuple[str, str]]:
    return frozenset((str(_uid(item["directive_id"])), str(_uid(item["revision_id"]))) for item in raw_list)


def _actual_pairs(scoped: tuple[ScopedDirective, ...]) -> frozenset[tuple[str, str]]:
    return frozenset((str(s.directive.directive_id), str(s.directive.revision_id)) for s in scoped)


def _actual_ids(scoped: tuple[ScopedDirective, ...]) -> frozenset[str]:
    return frozenset(str(s.directive.directive_id) for s in scoped)


def _actual_conflicts(conflicts: tuple[ConflictFinding, ...]) -> frozenset[frozenset[str]]:
    return frozenset(frozenset({str(c.left.directive_id), str(c.right.directive_id)}) for c in conflicts)


def _load_cases() -> list[FixtureCase]:
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return [FixtureCase(c) for c in raw["cases"]]


_CASES = _load_cases()
_RESULTS: dict[str, SelectionResult] = {case.case_id: select(case.inputs) for case in _CASES}
_BY_ID: dict[str, FixtureCase] = {case.case_id: case for case in _CASES}


# ---------------------------------------------------------------------------
# Fixture-set sanity: guards against a vacuous corpus.
#
# Mirrors test_arc_determinism.py's coverage check. A fixture set that always
# reads READY, or never exercises a real conflict, would let every metric
# below report a perfect score while proving nothing.
# ---------------------------------------------------------------------------


def test_the_fixture_set_meets_the_minimum_case_count() -> None:
    assert len(_CASES) >= 15, f"only {len(_CASES)} cases; the eval fixture requires at least 15"


def test_the_fixture_set_covers_every_resolution_status() -> None:
    statuses = {result.status for result in _RESULTS.values()}
    assert statuses == set(ResolutionStatus), f"missing statuses: {set(ResolutionStatus) - statuses}"


def test_the_fixture_set_exercises_a_real_conflict_and_a_real_exception() -> None:
    assert any(result.conflicts for result in _RESULTS.values()), "no case produced a conflict"
    assert any(result.applied_exception_ids for result in _RESULTS.values()), "no case applied an exception"
    assert any(c.expected_must_not_appear for c in _CASES), "no case checks a must-not-appear directive"


# ---------------------------------------------------------------------------
# Per-case strict equality: the attribution layer.
#
# Parametrized on case_id so a failure names the exact case in pytest's own
# output, with no aggregation to obscure which governance situation broke.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", [c.case_id for c in _CASES], ids=[c.case_id for c in _CASES])
def test_case_matches_its_full_expectation(case_id: str) -> None:
    case = _BY_ID[case_id]
    result = _RESULTS[case_id]
    label = f"[{case_id}] {case.description}"

    assert (
        result.status is case.expected_status
    ), f"{label}: status was {result.status}, expected {case.expected_status}"
    assert (
        _actual_pairs(result.mandatory) == case.expected_mandatory
    ), f"{label}: mandatory set was {_actual_pairs(result.mandatory)}, expected {case.expected_mandatory}"
    assert (
        _actual_pairs(result.optional) == case.expected_optional
    ), f"{label}: optional set was {_actual_pairs(result.optional)}, expected {case.expected_optional}"
    present = _actual_ids(result.mandatory) | _actual_ids(result.optional)
    leaked = present & case.expected_must_not_appear
    assert not leaked, f"{label}: directive(s) {leaked} must not appear anywhere in the result but did"
    assert (
        set(result.blocked_reasons) == case.expected_blocked_reasons
    ), f"{label}: blocked_reasons was {set(result.blocked_reasons)}, expected {case.expected_blocked_reasons}"
    assert (
        set(result.degraded_reasons) == case.expected_degraded_reasons
    ), f"{label}: degraded_reasons was {set(result.degraded_reasons)}, expected {case.expected_degraded_reasons}"
    assert (
        _actual_conflicts(result.conflicts) == case.expected_conflicts
    ), f"{label}: conflicts was {_actual_conflicts(result.conflicts)}, expected {case.expected_conflicts}"
    if case.expected_applied_exception_ids is not None:
        actual_exceptions = frozenset(str(x) for x in result.applied_exception_ids)
        assert (
            actual_exceptions == case.expected_applied_exception_ids
        ), f"{label}: applied_exception_ids was {actual_exceptions}, expected {case.expected_applied_exception_ids}"


# ---------------------------------------------------------------------------
# The four -- three, here -- named metrics.
# ---------------------------------------------------------------------------


def test_mandatory_inclusion_recall_is_1_0() -> None:
    """Of the (directive, revision) pairs that should be mandatory, what fraction are.

    Micro-averaged across every case: every expected pair counts once,
    regardless of which case it came from, so one case with many expected
    directives cannot be diluted by many cases with few.
    """
    found = 0
    total = 0
    missed_cases: list[str] = []
    for case in _CASES:
        expected = case.expected_mandatory
        if not expected:
            continue
        actual = _actual_pairs(_RESULTS[case.case_id].mandatory)
        hit = len(expected & actual)
        found += hit
        total += len(expected)
        if hit < len(expected):
            missed_cases.append(case.case_id)

    recall = found / total if total else 1.0
    assert recall == _MANDATORY_INCLUSION_RECALL_THRESHOLD, (
        f"mandatory-inclusion recall = {recall:.3f} ({found}/{total}); "
        f"cases with a missing mandatory directive: {missed_cases}"
    )


def test_prohibited_inclusion_rate_is_0_0() -> None:
    """Of the directive ids that must never appear, how many showed up anyway."""
    violations = 0
    checked = 0
    offending_cases: list[str] = []
    for case in _CASES:
        prohibited = case.expected_must_not_appear
        if not prohibited:
            continue
        result = _RESULTS[case.case_id]
        present = _actual_ids(result.mandatory) | _actual_ids(result.optional)
        leaked = prohibited & present
        violations += len(leaked)
        checked += len(prohibited)
        if leaked:
            offending_cases.append(case.case_id)

    rate = violations / checked if checked else 0.0
    assert rate == _PROHIBITED_INCLUSION_RATE_THRESHOLD, (
        f"prohibited-inclusion rate = {rate:.3f} ({violations}/{checked}); "
        f"cases with a leaked directive: {offending_cases}"
    )


def test_precedence_conflict_detection_is_1_0() -> None:
    """Of the conflicting pairs that should be detected, what fraction are.

    Only mandatory-side conflicts are counted, because select() only ever
    records mandatory-side pairs in `result.conflicts` -- an optional-only
    conflict degrades instead (see arc-sel-003), and cases built around that
    path correctly list no expected conflicts here.
    """
    found = 0
    total = 0
    missed_cases: list[str] = []
    for case in _CASES:
        expected = case.expected_conflicts
        if not expected:
            continue
        actual = _actual_conflicts(_RESULTS[case.case_id].conflicts)
        hit = len(expected & actual)
        found += hit
        total += len(expected)
        if hit < len(expected):
            missed_cases.append(case.case_id)

    recall = found / total if total else 1.0
    assert recall == _PRECEDENCE_CONFLICT_DETECTION_THRESHOLD, (
        f"precedence-conflict detection = {recall:.3f} ({found}/{total}); "
        f"cases with a missed conflict: {missed_cases}"
    )
