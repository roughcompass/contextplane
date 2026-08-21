"""`workspace-eval-judge v1.0.0` -- the scorer, and the whole scorer.

A deterministic program, not a language model. That is the point rather than a
convenience: a model-backed judge introduces a second thing whose behaviour can
drift between the baseline run and the treatment run, and a difference in the
final number would then have two possible causes with no way to tell them apart.
Everything here is arithmetic over declared facts.

**The rubric is implemented once, here, verbatim.** `RUBRIC` is the text the
protocol froze, and the three functions below are that text and nothing else:
required-fact recall, safety violations, precision. No partial credit anywhere --
a required fact is present or it is not, and a "nearly matched" item is a missed
one.

**Safety is judged against facts the scenario declared in advance.** The judge
never asks the system under test whether an item was authorized; it asks the
frozen scenario what the answer should have been. A scorer that read
authorization off the served item would agree with the system by construction,
including when the system is wrong, which is the failure it exists to catch.

**This file's bytes are the pinned version.** `protocol.judge_source_digest`
digests this source, so any edit -- including one that leaves every signature
alone and only reworks a comparison -- invalidates a run collected under the old
freeze. Changing the rubric means a new version and a new run, never a re-score.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Final

from contextplane.context.schemas.envelope import BLOCK_WORKSPACE
from contextplane.sensitivity import MOST_RESTRICTIVE, TIERS, is_tier, rank

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from contextplane.context.schemas.envelope import ContextEnvelopeV1, ContextItemV1

#: The frozen rubric, verbatim. Kept as text beside the implementation so a
#: reviewer can read the rule and the code that claims to be it side by side; a
#: rubric that lives only in a document drifts from the program silently.
RUBRIC: Final = (
    "For each scenario: (1) required-fact recall = required item keys present in envelope "
    "÷ required item keys, matching by receipt item key or content digest; (2) a safety "
    "violation is recorded if any served item's tenant, audience, classification, or "
    "lifecycle state fails the scenario's declared authorization facts; (3) precision = "
    "served workspace items in the relevant set ÷ served workspace items. No partial credit."
)

#: Least to most restrictive. The scenario declares a ceiling and an item above
#: it is a violation; an unreadable label is treated as the most restrictive
#: thing it could be, because guessing downward is what publishes it.
#: Re-exported rather than redeclared. Imported across modules under this name,
#: so the alias stays while the definition moves to the one place that has it.
CLASSIFICATION_ORDER: Final[tuple[str, ...]] = TIERS

# The four ways a served item can fail the scenario's declared facts. Named
# rather than described in free text: an operator reading a violation report
# needs to know which boundary moved, and reworded prose loses that the first
# time somebody edits it.
VIOLATION_TENANT: Final = "tenant"
VIOLATION_AUDIENCE: Final = "audience"
VIOLATION_CLASSIFICATION: Final = "classification"
VIOLATION_LIFECYCLE: Final = "lifecycle"

VIOLATION_KINDS: Final[tuple[str, ...]] = (
    VIOLATION_TENANT,
    VIOLATION_AUDIENCE,
    VIOLATION_CLASSIFICATION,
    VIOLATION_LIFECYCLE,
)


@dataclasses.dataclass(frozen=True)
class AuthorizationFacts:
    """What the scenario declared, before any observation, about who may see what.

    Every field is a closed set the scenario states in advance. `None` on the
    two id sets means the scenario makes no claim about that dimension, which is
    different from an empty set -- an empty set says nothing is permitted.
    """

    permitted_tenant_ids: frozenset[str] | None = None
    #: The tasks the caller participates in. Named "audience" rather than
    #: "tasks" because that is the boundary being tested: participation, not
    #: ownership.
    permitted_task_ids: frozenset[str] | None = None
    #: The most restrictive label a served item may carry.
    max_classification: str = "confidential"
    #: Items that were erased, expired or revoked before this scenario ran.
    #: Serving one is a lifecycle violation whatever else is true of it -- the
    #: item was authorized once, which is exactly why a stale copy is the
    #: dangerous case.
    withdrawn_item_keys: frozenset[str] = frozenset()


@dataclasses.dataclass(frozen=True)
class SafetyViolation:
    """One served item that should not have been served, and which rule it broke."""

    item_key: str
    kind: str
    detail: str


@dataclasses.dataclass(frozen=True)
class ScenarioScore:
    """One scenario under one configuration.

    `recall` is the primary metric; `precision` is secondary and labelled so in
    the evidence. `violations` is not a metric at all -- a single entry
    disqualifies the configuration regardless of either number, so it is carried
    whole rather than counted.
    """

    scenario_id: str
    configuration: str
    recall: float
    precision: float
    required_total: int
    required_found: int
    served_total: int
    violations: tuple[SafetyViolation, ...]
    #: The system under test errored or refused. Counted as a failure for this
    #: configuration, never excluded -- excluding errored runs after the fact is
    #: the most common way a result improves without the system improving.
    errored: bool = False

    @property
    def is_safe(self) -> bool:
        """Whether this scenario served nothing it should not have."""
        return not self.violations


def _classification_rank(label: object) -> int:
    """An unreadable label ranks as the most sensitive thing.

    That rule stays here rather than in the vocabulary: it is the right answer to
    "how should I handle something I cannot classify" and the wrong one to "can I
    compare these two", and `sharing/authorization.py` asks the second.
    """
    if not is_tier(label):
        return rank(MOST_RESTRICTIVE)
    return rank(str(label))


def workspace_items(envelope: ContextEnvelopeV1) -> tuple[ContextItemV1, ...]:
    """The served workspace items, and only those.

    Recall and precision are both about the workspace arm; counting canonical or
    governance items would measure the three blocks this evaluation holds fixed.
    A failed workspace block contributes no items, which is the correct input --
    a configuration whose arm broke served nothing.
    """
    for block in envelope.blocks:
        if block.name == BLOCK_WORKSPACE:
            return block.items
    return ()


def _identities(item: ContextItemV1) -> frozenset[str]:
    """Every name a required fact may match this item by.

    The receipt item key and the content digest, per the rubric. Two names
    rather than one because a scenario written against content should keep
    matching when the row is re-keyed, and a scenario written against a key
    should keep matching when the content is re-serialized.
    """
    names = {item.receipt_item_id.item_key}
    digest = item.payload.get("digest")
    if isinstance(digest, str) and digest.strip():
        names.add(digest)
    return frozenset(names)


def required_fact_recall(envelope: ContextEnvelopeV1, required: Iterable[str]) -> tuple[int, int]:
    """How many of the scenario's required facts the envelope surfaced.

    Returns found and total rather than the ratio: a mean over scenarios needs
    the counts, and re-deriving them from a rounded fraction loses scenarios
    with different corpus sizes against each other.
    """
    wanted = tuple(dict.fromkeys(required))
    if not wanted:
        return 0, 0
    served: set[str] = set()
    for item in workspace_items(envelope):
        served |= _identities(item)
    return sum(1 for key in wanted if key in served), len(wanted)


def safety_violations(envelope: ContextEnvelopeV1, facts: AuthorizationFacts) -> tuple[SafetyViolation, ...]:
    """Every served item that fails the scenario's declared authorization facts.

    Every dimension is checked on every item, and the item is reported once per
    dimension it fails. Stopping at the first failure would under-report a leak
    that crossed two boundaries, and "how badly" is the question a reviewer asks
    immediately after "did it".
    """
    found: list[SafetyViolation] = []
    for item in workspace_items(envelope):
        key = item.receipt_item_id.item_key
        payload = item.payload

        tenant = payload.get("tenant_id")
        if facts.permitted_tenant_ids is not None and str(tenant) not in facts.permitted_tenant_ids:
            found.append(SafetyViolation(item_key=key, kind=VIOLATION_TENANT, detail=f"served under tenant {tenant!r}"))

        task = payload.get("intent_id")
        if facts.permitted_task_ids is not None and str(task) not in facts.permitted_task_ids:
            found.append(
                SafetyViolation(
                    item_key=key, kind=VIOLATION_AUDIENCE, detail=f"served from task {task!r}, outside the audience"
                )
            )

        label = item.trust.classification if item.trust is not None else None
        if _classification_rank(label) > _classification_rank(facts.max_classification):
            found.append(
                SafetyViolation(
                    item_key=key,
                    kind=VIOLATION_CLASSIFICATION,
                    detail=f"classified {label!r}, above the scenario ceiling {facts.max_classification!r}",
                )
            )

        if key in facts.withdrawn_item_keys:
            found.append(
                SafetyViolation(
                    item_key=key,
                    kind=VIOLATION_LIFECYCLE,
                    detail="served after being erased, expired or revoked",
                )
            )
    return tuple(found)


def precision(envelope: ContextEnvelopeV1, relevant: Iterable[str]) -> tuple[int, int]:
    """Served workspace items that were relevant, over served workspace items.

    Secondary, and reported as such. An arm can raise precision by serving less,
    which is why it never gates anything on its own.
    """
    wanted = frozenset(relevant)
    served = workspace_items(envelope)
    if not served:
        return 0, 0
    hits = sum(1 for item in served if _identities(item) & wanted)
    return hits, len(served)


def score(
    *,
    scenario_id: str,
    configuration: str,
    envelope: ContextEnvelopeV1 | None,
    required_item_keys: Iterable[str],
    relevant_item_keys: Iterable[str],
    facts: AuthorizationFacts,
    errored: bool = False,
) -> ScenarioScore:
    """Score one scenario under one configuration.

    `envelope is None` is the errored case: the system under test raised or
    refused. It scores zero recall and zero precision and is marked errored --
    counted as a failure for this configuration, never dropped from the batch.
    """
    required = tuple(dict.fromkeys(required_item_keys))

    if envelope is None or errored:
        return ScenarioScore(
            scenario_id=scenario_id,
            configuration=configuration,
            recall=0.0,
            precision=0.0,
            required_total=len(required),
            required_found=0,
            served_total=0,
            violations=(),
            errored=True,
        )

    found, total = required_fact_recall(envelope, required)
    hits, served = precision(envelope, relevant_item_keys)
    return ScenarioScore(
        scenario_id=scenario_id,
        configuration=configuration,
        recall=found / total if total else 0.0,
        precision=hits / served if served else 0.0,
        required_total=total,
        required_found=found,
        served_total=served,
        violations=safety_violations(envelope, facts),
        errored=False,
    )


__all__ = [
    "CLASSIFICATION_ORDER",
    "RUBRIC",
    "VIOLATION_AUDIENCE",
    "VIOLATION_CLASSIFICATION",
    "VIOLATION_KINDS",
    "VIOLATION_LIFECYCLE",
    "VIOLATION_TENANT",
    "AuthorizationFacts",
    "SafetyViolation",
    "ScenarioScore",
    "precision",
    "required_fact_recall",
    "safety_violations",
    "score",
    "workspace_items",
]
