"""Adversarial selectivity: can a caller shed an obligation by changing what it says about itself?

E3-T5 asks for a benchmark of "adversarial selectivity" and notes that the
phrase needs a definition before it can be measured. This file is that
definition, and it is deliberately narrow:

    **A caller escapes when a rule that applies to an honest manifest stops
    applying to a manifest the same caller could equally well have sent.**

Not a relevance number. The threat is the one E1's audit already found once:
`rule_applies` matches the manifest's caller-supplied dimensions against a
rule's selectors, and every matcher answers `False` for a value it does not
recognise -- so an unrecognised value does not fail loudly, it *sheds the rule*.
A host declaring `data_sensitivity="ultra-secret"`, or omitting the field, used
to escape every rule that named a tier. `_declared_sensitivity` closed that by
reading the unknown as `MOST_RESTRICTIVE` before matching.

E3-T5's job is to find the next one of those. It found two.

**What this measures, mechanically.** For each dimension `rule_applies` reads,
the fixture pairs a rule selector naming a value with an honest manifest value
that matches it. One minimal mandatory directive is built per probe, so an
outcome is attributable to that dimension rather than to interacting rules.
`select()` runs against the honest manifest -- which must produce the directive,
or the probe measured nothing -- and then against each adversarial variant. A
variant *escapes* when the directive disappears.

**Two kinds of escape, and conflating them would be the mistake.**

*Evasion by unrecognised value* is a defect in this engine. `environment` and
`domain_ids` are open-vocabulary strings matched by bare set membership, so
`environment=None`, `environment="prod-ish"` and `domain_ids=[]` each shed every
rule scoped to them. That is the E1 hole, twice, unfixed.

*Misdeclaration* is not. `entity_ids`, `requested_action_classes` and
`intent_kind` are closed or referential: a caller naming a different entity or
declaring `merge` when it intends to `deploy` has lied about its intent, and no
fail-closed read of a well-formed value can detect that. The defence is
attestation -- the manifest is signed and hashed into the claims digest -- not
selection. They are probed anyway, because a benchmark that only covers the
dimensions somebody already suspected is a benchmark that confirms suspicions.

**Why E1's fix does not transfer to the two live holes.** Reading an unknown
sensitivity tier as the most restrictive one works because sensitivity is an
*ordered scale with a maximum*. Environments and domains have no ordering; there
is no "most restrictive environment". So the fix has to be a different shape --
apply every rule scoped to the dimension when the manifest is silent, or refuse
the request -- and choosing between those is a design decision with an
availability cost, not a bug fix. **This file does not make that decision.** It
reports, and it ratchets, which is the discipline the extraction ground truth
established: report before you gate, because the first measurement is as likely
to be about the harness as about the system.

**What is gated, therefore.** Two things that cannot regress:

1. `data_sensitivity` must not escape by omission or by an unknown value. That
   is a shipped property with a name, and it is also this benchmark's
   anti-vacuity control -- if the one dimension known to be closed reports an
   escape, the harness is measuring something other than what it claims.
2. The set of escaping (dimension, variant) pairs must match the fixture's
   recorded expectation exactly. Equality, not a count: a new escape fails, and
   so does a fixed one, until somebody edits the fixture deliberately. A count
   alone would let one hole close while another opened.
"""

from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from contextplane.arc.service.selection import (
    ApplicabilityRule,
    SelectionInput,
    select,
)
from contextplane.arc.types import (
    ActionClass,
    AuthorityScope,
    ConflictSubjectKey,
    Directive,
    DirectiveType,
    IntentKind,
    IntentManifest,
    NormalizedConstraint,
)

_FIXTURE_PATH = Path(__file__).parent.parent.parent / "eval" / "fixtures" / "adversarial_selectivity.json"

#: Every dimension `rule_applies` reads from the manifest. Pinned so a new
#: dimension added to the matcher fails here rather than going unprobed -- an
#: unprobed dimension is exactly where the next E1 hole would live.
_DIMENSIONS: frozenset[str] = frozenset(
    {
        "data_sensitivity",
        "environment",
        "domain_ids",
        "entity_ids",
        "requested_action_classes",
        "intent_kind",
    }
)

_TENANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
_DIRECTIVE_ID = uuid.UUID("0d000000-0000-4000-8000-000000000001")
_REVISION_ID = uuid.UUID("0d000000-0000-4000-8000-000000000002")
_RULE_ID = uuid.UUID("0d000000-0000-4000-8000-000000000003")
_AS_OF = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)


def _fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _probe_rule(selector: dict[str, list[str]]) -> ApplicabilityRule:
    """One GLOBAL mandatory rule constrained on exactly the probed dimension.

    Global scope so no tenant predicate participates, and every selector this
    probe is not about left empty -- empty means "no constraint", so the rule
    matches on the probed dimension alone. That is what makes an escape
    attributable.
    """
    return ApplicabilityRule(
        rule_id=_RULE_ID,
        revision_id=_REVISION_ID,
        scope=AuthorityScope.GLOBAL,
        is_mandatory=True,
        target_tenant_id=None,
        entity_ids=frozenset(uuid.UUID(v) for v in selector.get("entity_ids", ())),
        domain_ids=frozenset(selector.get("domain_ids", ())),
        intent_kinds=frozenset(IntentKind(v) for v in selector.get("intent_kinds", ())),
        action_classes=frozenset(ActionClass(v) for v in selector.get("action_classes", ())),
        environments=frozenset(selector.get("environments", ())),
        data_sensitivity_tiers=frozenset(selector.get("data_sensitivity_tiers", ())),
        effective_from=None,
        effective_until=None,
    )


def _manifest(dimension: str, value: Any) -> IntentManifest:
    """The honest manifest with exactly one dimension replaced.

    Every other dimension is left at a value the probe rule does not constrain,
    so the only thing that can change the outcome is the substitution.
    """
    fields: dict[str, Any] = {
        "session_id": "adv-1",
        "intent_kind": IntentKind.CODE_CHANGE,
        "requested_action_classes": frozenset({ActionClass.DEPLOY}),
        "entity_ids": frozenset(),
        "domain_ids": frozenset(),
        "environment": None,
        "data_sensitivity": None,
    }
    if dimension == "intent_kind":
        fields["intent_kind"] = IntentKind(value)
    elif dimension == "requested_action_classes":
        fields["requested_action_classes"] = frozenset(ActionClass(v) for v in value)
    elif dimension == "entity_ids":
        fields["entity_ids"] = frozenset(uuid.UUID(v) for v in value)
    elif dimension == "domain_ids":
        fields["domain_ids"] = frozenset(value)
    else:
        fields[dimension] = value
    return IntentManifest(**fields)


def _probe_directive() -> Directive:
    """One `require` directive, carrying the comparable shape its type demands.

    `Directive.__post_init__` refuses an action-protecting type without both a
    conflict subject and a constraint -- without them it is citation_only and
    cannot protect an action. The values are arbitrary and identical across
    every probe: only one directive is ever in play, so nothing here can
    conflict with anything, and holding them fixed keeps the probes differing in
    exactly one thing.
    """
    return Directive(
        directive_id=_DIRECTIVE_ID,
        revision_id=_REVISION_ID,
        directive_type=DirectiveType.REQUIRE,
        source_anchor="policy#adversarial-probe",
        conflict_subject=ConflictSubjectKey(
            schema_version="arc_conflict_v1",
            namespace="deploy",
            subject_selector="svc:probe",
            operation="release",
            action_class="deploy",
            target_selector="env:probe",
        ),
        constraint=NormalizedConstraint.parse("require", "equals", "approved"),
        delegable_exception=False,
    )


def _directive_selected(dimension: str, value: Any, selector: dict[str, list[str]]) -> bool:
    """Whether the probe's mandatory directive survives into the result."""
    directive = _probe_directive()
    result = select(
        SelectionInput(
            manifest=_manifest(dimension, value),
            tenant_id=_TENANT,
            as_of=_AS_OF,
            candidates=((directive, _probe_rule(selector), _AS_OF),),
        )
    )
    return any(scoped.directive.directive_id == _DIRECTIVE_ID for scoped in result.mandatory)


def _measure() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Run every probe. Returns (escapes, expected), each a set of (dimension, variant)."""
    escapes: set[tuple[str, str]] = set()
    expected: set[tuple[str, str]] = set()
    for probe in _fixture()["dimensions"]:
        dimension, selector = probe["dimension"], probe["rule_selector"]
        assert _directive_selected(dimension, probe["honest"], selector), (
            f"{dimension}: the honest manifest did not select the probe directive, so every "
            "variant below would 'escape' a rule that never applied. The probe is broken, not the engine."
        )
        for variant, value in probe["variants"].items():
            if not _directive_selected(dimension, value, selector):
                escapes.add((dimension, variant))
        expected |= {(dimension, v) for v in probe["expected_escapes"]}
    return escapes, expected


# ---------------------------------------------------------------------------
# The fixture's own contract, checked without running selection
# ---------------------------------------------------------------------------


def test_the_fixture_probes_every_dimension_selection_matches_on() -> None:
    """A dimension nobody probed is where the next hole lives.

    `rule_applies` reads six manifest dimensions. If a seventh is added and this
    file is not extended, the benchmark keeps reporting the same escape set
    while a new evasion sits beside it, unmeasured and looking measured.
    """
    doc = _fixture()
    assert doc["frozen"] is True, "an unfrozen corpus makes the ratchet below a number about something else"
    probed = {p["dimension"] for p in doc["dimensions"]}
    assert probed == _DIMENSIONS, (
        f"probed dimensions do not match what selection reads; "
        f"unprobed: {sorted(_DIMENSIONS - probed)}, unknown: {sorted(probed - _DIMENSIONS)}"
    )
    for probe in doc["dimensions"]:
        assert probe["variants"], f"{probe['dimension']}: a probe with no variants tests nothing"
        unknown = set(probe["expected_escapes"]) - set(probe["variants"])
        assert not unknown, f"{probe['dimension']}: expects escapes from variants it does not define: {sorted(unknown)}"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["omitted", "unknown_value", "empty_string"])
def test_an_unrecognised_sensitivity_does_not_shed_the_rule(variant: str) -> None:
    """The one dimension E1 closed, held closed — and the harness's calibration.

    `_declared_sensitivity` reads an unknown or absent tier as `MOST_RESTRICTIVE`
    before matching, so none of these three sheds a rule naming `restricted`.
    Parametrised rather than folded into the ratchet because this property has a
    name and a reason, and a regression here should say which input broke it.

    It is also the anti-vacuity control. If the dimension known to be closed
    reports an escape, the benchmark is measuring something other than what it
    claims and every other number in it is suspect.
    """
    probe = next(p for p in _fixture()["dimensions"] if p["dimension"] == "data_sensitivity")
    assert _directive_selected("data_sensitivity", probe["variants"][variant], probe["rule_selector"]), (
        f"a manifest with data_sensitivity {variant} escaped a rule naming 'restricted'; "
        "`_declared_sensitivity` no longer reads the unknown as most-restrictive"
    )


def test_the_escaping_dimensions_are_exactly_the_ones_on_record() -> None:
    """The ratchet: equality against the fixture, not a count.

    A count would let one hole close while another opened and report no change.
    Equality means a new escape fails, and a *fixed* escape fails too — until
    somebody edits the fixture, which is the moment the fix gets recorded rather
    than absorbed.

    Two of these are live defects and the rest are misdeclaration, which
    selection cannot detect and attestation is supposed to. The module docstring
    says which is which and why E1's fix does not transfer; this test only holds
    the line.
    """
    escapes, expected = _measure()
    assert escapes == expected, (
        "adversarial selectivity changed.\n"
        f"  newly escaping: {sorted(escapes - expected)}\n"
        f"  no longer escaping: {sorted(expected - escapes)}\n"
        "If a hole was closed, update eval/fixtures/adversarial_selectivity.json and record it "
        "in eval/EVAL.md. If one opened, it is a caller-facing evasion and not a test failure to silence."
    )


def test_the_report(capsys: pytest.CaptureFixture[str]) -> None:
    """The measurement, printed. `make eval` runs this with -s.

    Separate from the gate above because the number and the threshold are
    different things — the same split `test_multi_session_recall.py` makes, and
    for the reason that task learned: a figure filed beside the assertion that
    constrains it eventually gets tuned to the assertion.
    """
    escapes, _ = _measure()
    doc = _fixture()
    total = sum(len(p["variants"]) for p in doc["dimensions"])
    with capsys.disabled():
        print(f"\nadversarial selectivity: {len(escapes)}/{total} probe variants shed their rule")
        for probe in doc["dimensions"]:
            hit = sorted(v for d, v in escapes if d == probe["dimension"])
            print(f"  {probe['dimension']:26} {len(hit)}/{len(probe['variants'])}  {', '.join(hit) or '—'}")
        print("  Two are open-vocabulary evasions (environment, domain_ids); the rest are")
        print("  misdeclaration, which selection cannot detect and attestation must.")
        print("  Record this in eval/EVAL.md; this test produces the figure and does not judge it.")

    assert total > 0, "no probe variants ran, so this reported nothing"
