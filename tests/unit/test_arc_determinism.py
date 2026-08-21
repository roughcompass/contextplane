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

from contextplane.arc.schemas.canonical import canonicalize_bundle_content
from contextplane.arc.service.selection import (
    ApprovedException,
    MandatoryObligation,
    SelectionInput,
    select,
)
from contextplane.arc.types import (
    ActionClass,
    ApplicabilityRule,
    AuthorityScope,
    ConflictSubjectKey,
    Directive,
    DirectiveType,
    IntentKind,
    IntentManifest,
    NormalizedConstraint,
)

SEED = 20260730
CASES = 120

_TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
_AS_OF = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)

#: Small shared pools so a narrow-scoped rule and the manifest can actually
#: overlap. The manifest used to declare no entities and no domains at all,
#: which was harmless while entity and domain rules carried no selector -- both
#: matched everything on those dimensions. Now that each scope must name its
#: own selector, an empty manifest would mean two of the five scopes never
#: match, and the sweep would be quietly testing three.
_DOMAINS = ("payments", "logistics")
_ENTITIES = (
    uuid.UUID("cccccccc-0000-4000-8000-000000000001"),
    uuid.UUID("cccccccc-0000-4000-8000-000000000002"),
)
_VALUES = ("approved", "pending", "rejected")


def _rng(case: int) -> random.Random:
    return random.Random(SEED + case)


#: Three fixed conflict subjects, chosen from rather than composed.
#:
#: Two directives conflict only when they share a subject, so how often this
#: sweep exercises conflict resolution at all is decided here. Composing the
#: subject from five independent draws spanned 120 combinations, and across 120
#: cases that produced conflicts in exactly **one** -- the `saw_conflict`
#: coverage assertion was passing by a margin of one, so any change to the
#: generator's random stream could silently turn conflict coverage off. It did:
#: adding the selectors the scope guards now require shifted the stream and the
#: single colliding pair disappeared, which is how this was found.
#:
#: A three-element pool makes collisions structural. Nothing here is testing the
#: breadth of the subject vocabulary; `test_arc_selection` owns that.
_SUBJECTS = tuple(
    ConflictSubjectKey(
        schema_version="arc_conflict_v1",
        namespace=namespace,
        subject_selector="service:a",
        operation=operation,
        action_class=action_class,
        target_selector="env:prod",
    )
    for namespace, operation, action_class in (
        ("deploy", "release", "deploy"),
        ("deploy", "merge", "merge"),
        ("data", "export", "data_export"),
    )
)


def _subject(rng: random.Random) -> ConflictSubjectKey:
    return rng.choice(_SUBJECTS)


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
    # Every scope below `global` must now name what it is scoped to, so the
    # selector a scope requires is supplied rather than drawn: the generator's
    # job is to vary shapes the model admits, and a rule the constructor refuses
    # tests the constructor, not determinism. Intent scope needs *both* an
    # intent kind and an action class, so those two draw from 1 rather than 0.
    #
    # The draws stay in their original order, and this matters more than it
    # looks: reordering them changes what every later `rng` call returns, so the
    # whole 120-case corpus becomes a different corpus. A first attempt hoisted
    # them above `getrandbits`, and the coverage guard below caught it by
    # noticing no case produced a conflict any more.
    rule_id = uuid.UUID(int=rng.getrandbits(128), version=4)
    is_mandatory = rng.random() < 0.7
    floor = 1 if scope is AuthorityScope.INTENT else 0
    rule = ApplicabilityRule(
        rule_id=rule_id,
        revision_id=revision_id,
        scope=scope,
        is_mandatory=is_mandatory,
        target_tenant_id=_TENANT if scope is AuthorityScope.TENANT else None,
        entity_ids=frozenset({rng.choice(_ENTITIES)}) if scope is AuthorityScope.ENTITY else frozenset(),
        domain_ids=frozenset({rng.choice(_DOMAINS)}) if scope is AuthorityScope.DOMAIN else frozenset(),
        intent_kinds=frozenset(rng.sample(list(IntentKind), rng.randint(floor, 2))),
        action_classes=frozenset(rng.sample(list(ActionClass), rng.randint(floor, 2))),
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
        manifest=IntentManifest(
            session_id="s1",
            intent_kind=rng.choice(list(IntentKind)),
            requested_action_classes=frozenset(rng.sample(list(ActionClass), rng.randint(0, 3))),
            entity_ids=frozenset(_ENTITIES),
            domain_ids=frozenset(_DOMAINS),
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
    conflicts = exceptions = missing = 0
    for case in range(CASES):
        inputs = _case(case)
        result = select(inputs)
        statuses.add(result.status)
        sizes.add(len(inputs.candidates))
        scopes.update(rule.scope for _d, rule, _e in inputs.candidates)
        conflicts += bool(result.conflicts)
        exceptions += bool(result.applied_exception_ids)
        missing += any(o.is_missing for o in inputs.obligations)

    assert len(statuses) >= 2, f"only saw {statuses}"
    assert len(scopes) == len(AuthorityScope), f"scopes not covered: {scopes}"
    assert max(sizes) >= 5, "never generated a large candidate set"
    assert 0 in sizes, "never generated an empty candidate set"
    # Counted with a floor rather than asserted as "at least one". These three
    # were booleans, and `conflicts` was true for exactly one case in 120 -- so
    # the guard was one unlucky shuffle away from passing while covering
    # nothing, and an unrelated change to the random stream duly turned it off.
    # A floor of three is still comfortably below what the generator produces
    # and states the coverage the sweep actually depends on.
    assert conflicts >= 3, f"only {conflicts} case(s) produced a conflict"
    assert exceptions >= 3, f"only {exceptions} case(s) applied an exception"
    assert missing >= 3, f"only {missing} case(s) had a missing obligation"
