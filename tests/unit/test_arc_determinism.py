"""Same inputs, byte-identical result — over generated inputs, not examples.

An example-based test proves determinism for the cases someone thought of. The
guarantee covers all of them, so this generates candidate sets across the whole shape
space and asserts the property holds for each.

Generation is seeded and the seed is a constant, so a failure is reproducible.
`random` is used to *build inputs*, never inside the engine — `test_arc_selection`
asserts separately that the engine imports no such module.
"""

from __future__ import annotations

import datetime
import random
import uuid

import pytest

from registry.arc.schemas.canonical import canonicalize_bundle_content
from registry.arc.service.selection import (
    ApprovedException,
    MandatoryObligation,
    SelectionInput,
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
    TaskKind,
    TaskManifest,
)

SEED = 20260730
CASES = 120

_TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
_AS_OF = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
_VALUES = ("approved", "pending", "rejected")
_OPERATIONS = ("release", "merge", "export")


def _rng(case: int) -> random.Random:
    return random.Random(SEED + case)


def _subject(rng: random.Random) -> ConflictSubjectKey:
    return ConflictSubjectKey(
        schema_version="arc_conflict_v1",
        namespace=rng.choice(("deploy", "data")),
        subject_selector=rng.choice(("service:a", "service:b")),
        operation=rng.choice(_OPERATIONS),
        action_class=rng.choice([a.value for a in ActionClass]),
        target_selector=rng.choice(("env:prod", "env:stage")),
    )


def _candidate(rng: random.Random) -> tuple[Directive, ApplicabilityRule, datetime.datetime]:
    revision_id = uuid.UUID(int=rng.getrandbits(128), version=4)
    directive_id = uuid.UUID(int=rng.getrandbits(128), version=4)
    citation_only = rng.random() < 0.2

    if citation_only:
        directive = Directive(
            directive_id=directive_id,
            revision_id=revision_id,
            directive_type=DirectiveType.CITATION_ONLY,
            source_anchor="a#1",
        )
    else:
        operator = rng.choice(("equals", "in_set", "not_in_set", "present"))
        value = None if operator == "present" else rng.choice(_VALUES)
        directive = Directive(
            directive_id=directive_id,
            revision_id=revision_id,
            directive_type=rng.choice((DirectiveType.REQUIRE, DirectiveType.PROHIBIT, DirectiveType.ESCALATE)),
            source_anchor="a#1",
            conflict_subject=_subject(rng),
            constraint=NormalizedConstraint.parse(rng.choice(("require", "prohibit")), operator, value),
            delegable_exception=rng.random() < 0.3,
        )

    scope = rng.choice(list(AuthorityScope))
    rule = ApplicabilityRule(
        rule_id=uuid.UUID(int=rng.getrandbits(128), version=4),
        revision_id=revision_id,
        scope=scope,
        is_mandatory=rng.random() < 0.7,
        target_tenant_id=_TENANT if scope is AuthorityScope.TENANT else None,
        capability_labels=frozenset({"x"}) if scope is AuthorityScope.CAPABILITY else frozenset(),
        task_kinds=frozenset(rng.sample(list(TaskKind), rng.randint(0, 2))),
        action_classes=frozenset(rng.sample(list(ActionClass), rng.randint(0, 2))),
    )
    # Effective time drawn from a small set so ties are common — ties are where a
    # weak sort key would let input order leak into the result.
    effective = _AS_OF - datetime.timedelta(days=rng.choice((0, 0, 0, 30, 60)))
    return (directive, rule, effective)


def _case(case: int) -> SelectionInput:
    rng = _rng(case)
    candidates = tuple(_candidate(rng) for _ in range(rng.randint(0, 8)))

    exceptions: list[ApprovedException] = []
    for directive, _rule, _eff in candidates:
        if directive.delegable_exception and rng.random() < 0.4:
            exceptions.append(
                ApprovedException(
                    exception_id=uuid.UUID(int=rng.getrandbits(128), version=4),
                    higher_scope_directive_id=directive.directive_id,
                    higher_scope_revision_id=directive.revision_id,
                    lower_scope_tenant_id=_TENANT,
                    replacement_constraint=NormalizedConstraint.parse("require", "equals", rng.choice(_VALUES)),
                )
            )

    obligations = tuple(
        MandatoryObligation(
            obligation_id=uuid.UUID(int=rng.getrandbits(128), version=4),
            directive_id=uuid.UUID(int=rng.getrandbits(128), version=4),
            obligation_state=rng.choice(("satisfied", "missing_revoked")),
            applicability_digest="d" * 64,
        )
        for _ in range(rng.randint(0, 2))
    )

    return SelectionInput(
        manifest=TaskManifest(
            session_id="s1",
            task_kind=rng.choice(list(TaskKind)),
            requested_action_classes=frozenset(rng.sample(list(ActionClass), rng.randint(0, 3))),
            capability_ids=frozenset(),
            domain_ids=frozenset(),
            environment=rng.choice((None, "production", "staging")),
            data_sensitivity=rng.choice((None, "confidential")),
        ),
        tenant_id=_TENANT,
        as_of=_AS_OF,
        candidates=candidates,
        exceptions=tuple(exceptions),
        obligations=obligations,
    )


def _fingerprint(result: object) -> bytes:
    """Canonical bytes of the decision, so "identical" means byte-identical."""
    assert hasattr(result, "status")
    return canonicalize_bundle_content(
        {
            "status": str(result.status),  # type: ignore[attr-defined]
            "mandatory": [
                str(s.directive.directive_id)
                for s in result.mandatory  # type: ignore[attr-defined]
            ],
            "optional": [
                str(s.directive.directive_id)
                for s in result.optional  # type: ignore[attr-defined]
            ],
            "blocked": list(result.blocked_reasons),  # type: ignore[attr-defined]
            "degraded": list(result.degraded_reasons),  # type: ignore[attr-defined]
            "exceptions": [str(e) for e in result.applied_exception_ids],  # type: ignore[attr-defined]
        }
    )


@pytest.mark.parametrize("case", range(CASES))
def test_repeated_evaluation_is_byte_identical(case: int) -> None:
    inputs = _case(case)
    assert _fingerprint(select(inputs)) == _fingerprint(select(inputs))


@pytest.mark.parametrize("case", range(CASES))
def test_candidate_order_does_not_change_the_result(case: int) -> None:
    """The property that matters most.

    Candidates arrive from a query whose row order is not guaranteed, so if
    ordering leaked into the outcome the same manifest could resolve differently
    on two runs against identical data.
    """
    inputs = _case(case)
    shuffled = list(inputs.candidates)
    _rng(case).shuffle(shuffled)
    reordered = SelectionInput(
        manifest=inputs.manifest,
        tenant_id=inputs.tenant_id,
        as_of=inputs.as_of,
        candidates=tuple(shuffled),
        exceptions=inputs.exceptions,
        obligations=inputs.obligations,
    )
    assert _fingerprint(select(inputs)) == _fingerprint(select(reordered))


@pytest.mark.parametrize("case", range(CASES))
def test_exception_and_obligation_order_does_not_change_the_result(case: int) -> None:
    inputs = _case(case)
    rng = _rng(case)
    exceptions = list(inputs.exceptions)
    obligations = list(inputs.obligations)
    rng.shuffle(exceptions)
    rng.shuffle(obligations)
    reordered = SelectionInput(
        manifest=inputs.manifest,
        tenant_id=inputs.tenant_id,
        as_of=inputs.as_of,
        candidates=inputs.candidates,
        exceptions=tuple(exceptions),
        obligations=tuple(obligations),
    )
    assert _fingerprint(select(inputs)) == _fingerprint(select(reordered))


def test_the_generator_actually_covers_the_shape_space() -> None:
    """Guards against a vacuous sweep.

    If generation drifted to producing only empty candidate sets, every
    determinism assertion above would pass while proving nothing.
    """
    statuses, scopes, sizes = set(), set(), set()
    saw_conflict = saw_exception = saw_missing = False
    for case in range(CASES):
        inputs = _case(case)
        result = select(inputs)
        statuses.add(result.status)
        sizes.add(len(inputs.candidates))
        scopes.update(rule.scope for _d, rule, _e in inputs.candidates)
        saw_conflict = saw_conflict or bool(result.conflicts)
        saw_exception = saw_exception or bool(result.applied_exception_ids)
        saw_missing = saw_missing or any(o.is_missing for o in inputs.obligations)

    assert len(statuses) >= 2, f"only saw {statuses}"
    assert len(scopes) == len(AuthorityScope), f"scopes not covered: {scopes}"
    assert max(sizes) >= 5, "never generated a large candidate set"
    assert 0 in sizes, "never generated an empty candidate set"
    assert saw_conflict, "no case produced a conflict"
    assert saw_exception, "no case applied an exception"
    assert saw_missing, "no case had a missing obligation"
