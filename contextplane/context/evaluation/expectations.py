"""What a prompt is checking, declared before it runs — and personas as presets.

E24-T7. `scenarios.py` states the mechanism and this adopts it without weakening
it:

    a scenario whose required facts were written after seeing what the system
    returned would be satisfied by whatever the system returned

The shipped prompt schema already carries `intent_note` — *"what this prompt is
checking"* — which is the prose form of the same idea. This is the structured
form, hung in the same place.

## A persona is a preset, not a second rubric

The user's framing: *"here is a best practice, but you may amend for a given
persona."* A compliance persona pins the classification ceiling and tolerates
zero boundary violations; a research persona relaxes precision and keeps recall.
Both are parameterizations of **the same five criteria**.

That distinction is load-bearing rather than tidy. A persona that could add a
criterion would be a rubric, and two rubrics produce two numbers nobody can put
side by side — which is exactly the split ADR 0024 refused at the level of
journeys, one level down.

## Every threshold is a floor, and `None` means "not asserted"

`min_recall = None` says the prompt makes no claim about recall, which is
different from `min_recall = 0.0` — the second asserts that any recall passes,
and something that always passes is a check somebody will later read as evidence.
The same distinction `AuthorizationFacts` already draws between `None` and an
empty set.

## Boundary violations have no threshold, deliberately

`SAFETY_TOLERANCE` is zero everywhere in this repository and it is not a knob a
persona may turn. *"Serving a single item the caller was not entitled to
disqualifies a configuration whatever its recall. Zero, not a rate: the safety
question is not how often the system leaks."* A research persona relaxing
precision is a resourcing decision; one relaxing the leak count would be deciding
not to look at some leaks.

## The presets are seeds, not a closed set

They ship editable and versioned with the rubric they parameterize. A deployment
whose evaluators need a fourth persona writes one; what it cannot do is write one
that scores something the rubric does not define.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Final

from contextplane.context.evaluation.envelope_judge import (
    ENVELOPE_JUDGE_VERSION,
    INSTRUCTION_SCOPES,
    AuthorizationFacts,
)
from contextplane.exceptions import ValidationError
from contextplane.extraction.judge_prompt import JUDGE_RUBRIC_VERSION, JUDGED_CRITERIA
from contextplane.sensitivity import TIERS, is_tier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

#: Every key an expectation may carry. Unknown keys are refused rather than
#: ignored: an evaluator who misspelled `min_recall` and had it dropped would
#: have a prompt that asserts nothing while reading as though it asserts
#: something -- which is worse than a prompt with no expectations at all.
_KNOWN: Final[frozenset[str]] = frozenset(
    {
        "max_classification",
        "min_precision",
        "min_recall",
        "permitted_instruction_scopes",
        "permitted_task_ids",
        "permitted_tenant_ids",
        "preset",
        "relevant_item_keys",
        "require_groundedness",
        "require_relevance",
        "required_item_keys",
        "withdrawn_item_keys",
    }
)

#: The seeded personas. Named rather than numbered, because an evaluator picks
#: one by what it is for.
PRESET_BALANCED: Final = "balanced"
PRESET_COMPLIANCE: Final = "compliance"
PRESET_RESEARCH: Final = "research"
PRESET_NAMES: Final[tuple[str, ...]] = (PRESET_BALANCED, PRESET_COMPLIANCE, PRESET_RESEARCH)


@dataclasses.dataclass(frozen=True)
class ExpectationsV1:
    """What one prompt asserts about a run, declared before the run happens.

    Every field is optional and `None` means *not asserted*, which is different
    from a permissive value. A prompt that asserts nothing is legal and reads as
    what it is; a prompt whose thresholds are all zero reads as passing checks
    nobody wrote.
    """

    #: Which persona this was seeded from, when it was. Recorded so a reader can
    #: see the shape somebody started from, and never re-read as the source of
    #: truth: the values below are what the run is judged against, because a
    #: preset edited afterwards must not change what a past prompt asserted.
    preset: str | None = None

    #: The keys the envelope must surface, and the keys that count as relevant.
    #: Both are written before the run, per `scenarios.py`.
    required_item_keys: tuple[str, ...] = ()
    relevant_item_keys: tuple[str, ...] = ()

    #: Floors on the two deterministic memory criteria.
    min_recall: float | None = None
    min_precision: float | None = None

    #: The authorization facts the boundary criterion is judged against. There is
    #: deliberately no tolerance beside them: zero is not a knob a persona turns.
    permitted_tenant_ids: frozenset[str] | None = None
    permitted_task_ids: frozenset[str] | None = None
    permitted_instruction_scopes: frozenset[str] | None = None
    max_classification: str | None = None
    withdrawn_item_keys: frozenset[str] = frozenset()

    #: Whether the two model-judged criteria must pass. `False` is a real value:
    #: a prompt exercising retrieval on a deployment with no judge configured
    #: still asserts the deterministic three.
    require_groundedness: bool = True
    require_relevance: bool = True

    @classmethod
    def of(cls, raw: Mapping[str, Any] | None) -> ExpectationsV1:
        """Build one from stored or submitted JSON, refusing anything unusable."""
        if not raw:
            return cls()
        unknown = sorted(set(raw) - _KNOWN)
        if unknown:
            raise ValidationError(f"expectations do not carry {unknown}; the fields are {sorted(_KNOWN)}")

        preset = raw.get("preset")
        if preset is not None and preset not in PRESET_NAMES:
            raise ValidationError(f"preset is one of {list(PRESET_NAMES)}, got {preset!r}")

        ceiling = raw.get("max_classification")
        if ceiling is not None and not is_tier(ceiling):
            raise ValidationError(f"max_classification is one of {list(TIERS)}, got {ceiling!r}")

        scopes = raw.get("permitted_instruction_scopes")
        if scopes is not None:
            unknown_scopes = sorted(set(str(s) for s in scopes) - set(INSTRUCTION_SCOPES))
            if unknown_scopes:
                raise ValidationError(
                    f"permitted_instruction_scopes are drawn from {list(INSTRUCTION_SCOPES)}, "
                    f"got {unknown_scopes}; a scope nothing can serve is a declaration that can "
                    "never be violated"
                )

        return cls(
            max_classification=str(ceiling) if ceiling is not None else None,
            min_precision=_fraction(raw.get("min_precision"), "min_precision"),
            min_recall=_fraction(raw.get("min_recall"), "min_recall"),
            permitted_instruction_scopes=_ids(scopes),
            permitted_task_ids=_ids(raw.get("permitted_task_ids")),
            permitted_tenant_ids=_ids(raw.get("permitted_tenant_ids")),
            preset=str(preset) if preset is not None else None,
            relevant_item_keys=_keys(raw.get("relevant_item_keys"), "relevant_item_keys"),
            require_groundedness=_flag(raw.get("require_groundedness"), "require_groundedness"),
            require_relevance=_flag(raw.get("require_relevance"), "require_relevance"),
            required_item_keys=_keys(raw.get("required_item_keys"), "required_item_keys"),
            withdrawn_item_keys=_ids(raw.get("withdrawn_item_keys")) or frozenset(),
        )

    def stored(self) -> dict[str, Any]:
        """The JSON form, with absent optionals absent rather than null.

        An omitted optional that came back as an explicit null would be a second
        spelling of the same expectation, and two prompts differing only in how
        their absences are written would compare unequal.
        """
        body: dict[str, Any] = {
            "require_groundedness": self.require_groundedness,
            "require_relevance": self.require_relevance,
        }
        if self.preset is not None:
            body["preset"] = self.preset
        if self.required_item_keys:
            body["required_item_keys"] = list(self.required_item_keys)
        if self.relevant_item_keys:
            body["relevant_item_keys"] = list(self.relevant_item_keys)
        if self.min_recall is not None:
            body["min_recall"] = self.min_recall
        if self.min_precision is not None:
            body["min_precision"] = self.min_precision
        if self.permitted_tenant_ids is not None:
            body["permitted_tenant_ids"] = sorted(self.permitted_tenant_ids)
        if self.permitted_task_ids is not None:
            body["permitted_task_ids"] = sorted(self.permitted_task_ids)
        if self.permitted_instruction_scopes is not None:
            body["permitted_instruction_scopes"] = sorted(self.permitted_instruction_scopes)
        if self.max_classification is not None:
            body["max_classification"] = self.max_classification
        if self.withdrawn_item_keys:
            body["withdrawn_item_keys"] = sorted(self.withdrawn_item_keys)
        return body

    def authorization_facts(self) -> AuthorizationFacts:
        """The declared facts the boundary criterion is judged against.

        `max_classification` falls back to the scorer's own default rather than
        to the most permissive tier: a prompt that states no ceiling is asking
        for the standing one, not opting out of the check.
        """
        default = AuthorizationFacts()
        return AuthorizationFacts(
            max_classification=self.max_classification or default.max_classification,
            permitted_instruction_scopes=self.permitted_instruction_scopes,
            permitted_task_ids=self.permitted_task_ids,
            permitted_tenant_ids=self.permitted_tenant_ids,
            withdrawn_item_keys=self.withdrawn_item_keys,
        )

    def asserts_nothing(self) -> bool:
        """Whether this prompt makes no checkable claim at all.

        A real and legal state -- an evaluator exploring has not yet decided what
        good looks like -- and one a surface says out loud rather than rendering
        as a row of passing checks.
        """
        return self == ExpectationsV1(
            require_groundedness=self.require_groundedness, require_relevance=self.require_relevance
        ) and not (self.require_groundedness or self.require_relevance)


@dataclasses.dataclass(frozen=True)
class Preset:
    """One named persona, and the rubric versions it parameterizes.

    The versions travel with it because a preset is a set of thresholds *over a
    rubric*, and a threshold on a criterion that has been redefined is a number
    describing something else. This is `calibration.py`'s key argument applied to
    a configuration rather than to a fit.
    """

    name: str
    description: str
    envelope_rubric_version: str
    judge_rubric_version: str
    expectations: ExpectationsV1


#: The seeded personas, over the same five criteria. Each is a parameterization,
#: never an extension: a persona that could add a criterion would be a rubric,
#: and two rubrics produce two numbers nobody can put side by side.
PRESETS: Final[dict[str, Preset]] = {
    PRESET_BALANCED: Preset(
        description=(
            "The standing default. Both judged criteria must pass and the standard classification "
            "ceiling applies; no numeric floor is asserted, because a floor nobody chose is a "
            "threshold somebody will later read as evidence."
        ),
        envelope_rubric_version=ENVELOPE_JUDGE_VERSION,
        expectations=ExpectationsV1(preset=PRESET_BALANCED),
        judge_rubric_version=JUDGE_RUBRIC_VERSION,
        name=PRESET_BALANCED,
    ),
    PRESET_COMPLIANCE: Preset(
        description=(
            "Pins the classification ceiling at `internal` and requires both judged criteria. "
            "Boundary violations are already zero-tolerance everywhere and this preset does not "
            "and cannot relax that — the safety question is not how often the system leaks."
        ),
        envelope_rubric_version=ENVELOPE_JUDGE_VERSION,
        expectations=ExpectationsV1(
            max_classification="internal",
            min_recall=1.0,
            preset=PRESET_COMPLIANCE,
            require_groundedness=True,
            require_relevance=True,
        ),
        judge_rubric_version=JUDGE_RUBRIC_VERSION,
        name=PRESET_COMPLIANCE,
    ),
    PRESET_RESEARCH: Preset(
        description=(
            "Keeps recall and asserts no precision floor: an exploratory run that serves widely is "
            "doing its job, and penalising it would push the evaluator toward narrow queries that "
            "measure nothing. Groundedness still applies — a wide search is not a licence to invent."
        ),
        envelope_rubric_version=ENVELOPE_JUDGE_VERSION,
        expectations=ExpectationsV1(
            min_recall=0.8,
            preset=PRESET_RESEARCH,
            require_groundedness=True,
            require_relevance=False,
        ),
        judge_rubric_version=JUDGE_RUBRIC_VERSION,
        name=PRESET_RESEARCH,
    ),
}


def preset(name: str) -> Preset:
    """One seeded persona by name, refusing one nobody seeded."""
    if name not in PRESETS:
        raise ValidationError(f"unknown preset {name!r}; the seeded personas are {list(PRESET_NAMES)}")
    return PRESETS[name]


def _fraction(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= float(value) <= 1.0:
        raise ValidationError(f"{field} is a number from 0 to 1 when given, got {value!r}")
    return float(value)


def _flag(value: object, field: str) -> bool:
    if value is None:
        return True
    if not isinstance(value, bool):
        raise ValidationError(f"{field} is true or false, got {value!r}")
    return value


def _keys(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValidationError(f"{field} is a list of item keys, got {type(value).__name__}")
    # Order-preserving de-duplication: a key listed twice is one requirement, and
    # counting it twice would move recall's denominator without anybody adding a
    # fact to find.
    return tuple(dict.fromkeys(str(entry) for entry in value))


def _ids(value: object) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple | set | frozenset):
        raise ValidationError(f"an id set is a list, got {type(value).__name__}")
    return frozenset(str(entry) for entry in value)


__all__ = [
    "JUDGED_CRITERIA",
    "PRESETS",
    "PRESET_BALANCED",
    "PRESET_COMPLIANCE",
    "PRESET_NAMES",
    "PRESET_RESEARCH",
    "ExpectationsV1",
    "Preset",
    "preset",
]
