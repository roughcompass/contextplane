"""`context-envelope-judge v2.0.0` -- the deterministic scorer, over all five blocks.

E24-T4. A program, not a language model, for the same reason `judge.py` gives and
which is quoted rather than paraphrased because it is the whole argument:

    a model-backed judge introduces a second thing whose behaviour can drift
    between the baseline run and the treatment run, and a difference in the final
    number would then have two possible causes with no way to tell them apart

**This is a rebuild, not a widened loop.** `judge.py` scores the workspace arm
because `treatments.py` holds canonical, governance, claim and resume identical
across every configuration and varies only that arm -- which is what made its
numbers attributable, and which is a property of an *ablation harness*. Scoring a
resolution somebody is about to grade an agent's answer against is a different
job: every block is live, none is held fixed, and a scorer that silently ignored
the fifth would report a perfect precision score on a resolution whose
instruction delta was wrong.

So `judge.py` is left byte-identical and stays selectable. `protocol.freeze()`
still digests it by default, so every run collected under the closed
workspace-retrieval decision stays valid, and `V1_ERA_IDENTITY` still names an
identity this tree can reproduce.

## What generalizing found, and it is not a rename

**The tenant dimension in `judge.py` fires on every real item.** It reads
`payload.get("tenant_id")`, and no arm puts a tenant in a payload: `queries.py`'s
checkpoint payload, `workspaces/recall.py`'s, `arm_payloads.py`'s canonical and
claim payloads and `instructions.py`'s delta payload carry none. Against a
scenario that declares `permitted_tenant_ids` -- which every scenario in the
frozen corpus does -- `str(None)` is not in the permitted set, so every served
item is reported as a tenant violation, and `SAFETY_TOLERANCE = 0` then
disqualifies every configuration on every scenario.

A check that fires on everything distinguishes nothing, which is the same defect
as one that fires on nothing wearing the opposite sign. **A tenant is a property
of the resolution, not of the item** -- every arm queries `WHERE tenant_id =
ctx.tenant_id` -- so this scorer takes the served tenant as an argument and only
consults a payload that actually states one. That is a correction to the
measurement and therefore a new rubric version, never a re-score of old runs.

**A dimension that cannot be checked is recorded, never passed.** Audience is
expressible on a workspace item (`intent_id`) and on an instruction delta
(`scope`, per ADR 0021); canonical, ARC and claim payloads state no audience at
all. Classification is inexpressible on a canonical item, because assembly
enforces `trust is None` there by design. Rather than reading either silence as
compliance, `UncheckedDimension` names the item, the dimension and why -- so a
reader can tell a passed check from an absent one. `containment.py` states the
same rule for its own defences: a check unable to fire is a hole that looks
exactly like a working defence.

**The classification exemption is structural and is not the unreadable-label
rule.** An unreadable label still ranks as the most restrictive thing it could
be, because guessing downward is what publishes it. A canonical item carries no
label at all, by construction, and treating a structural absence as
most-restrictive would flag the registry's own answer on every resolution.

## What is unchanged, and deliberately

`VIOLATION_KINDS` is the same four. The fifth block adds items to judge, not a
fifth kind of violation.

**No partial credit anywhere**, inherited verbatim: a required fact is present or
it is not, and a "nearly matched" item is a missed one. A boundary violation
fails the case outright regardless of every other number.

**Safety is judged against facts the scenario declared in advance.** The judge
never asks the system under test whether an item was authorized; it asks the
frozen scenario what the answer should have been. A scorer that read
authorization off the served item would agree with the system by construction,
including when the system is wrong, which is the failure it exists to catch.

**This file's bytes are the pinned version.** `protocol.judge_source_digest`
digests this source under `ENVELOPE_JUDGE_VERSION`, so any edit invalidates a run
collected under the old freeze. Changing the rubric means a new version and a new
run, never a re-score.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Final

from contextplane.context.evaluation.judge import (
    CLASSIFICATION_ORDER,
    VIOLATION_AUDIENCE,
    VIOLATION_CLASSIFICATION,
    VIOLATION_KINDS,
    VIOLATION_LIFECYCLE,
    VIOLATION_TENANT,
)
from contextplane.context.schemas.envelope import (
    BLOCK_CANONICAL,
    BLOCK_INSTRUCTIONS,
    BLOCK_NAMES,
    BLOCK_WORKSPACE,
)
from contextplane.sensitivity import MOST_RESTRICTIVE, is_tier, rank

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Mapping

    from contextplane.context.schemas.envelope import ContextEnvelopeV1, ContextItemV1

#: The rubric this scorer is, verbatim, beside the implementation that claims to
#: be it. A rubric living only in a document drifts from the program silently.
RUBRIC: Final = (
    "Over all five envelope blocks: (1) required-fact recall = required item keys present in the "
    "envelope ÷ required item keys, matching by receipt item key or content digest; (2) a boundary "
    "violation is recorded if any served item's tenant, audience, classification, or lifecycle "
    "state fails the scenario's declared authorization facts, where the tenant is the resolution's "
    "and a dimension an item cannot express is recorded as unchecked rather than passed; "
    "(3) precision = served items in the relevant set ÷ served items, reported overall and per "
    "block. No partial credit."
)

#: This scorer's own version. Distinct from `protocol.JUDGE_VERSION`, which names
#: the workspace-arm scorer, and selected explicitly -- a caller that wants five
#: blocks asks for five blocks.
ENVELOPE_JUDGE_VERSION: Final = "context-envelope-judge v2.0.0"

#: Every block this scorer reads. All of them, in the envelope's own order, so a
#: block added later is a test failure here rather than a silent omission.
SCORED_BLOCKS: Final[tuple[str, ...]] = BLOCK_NAMES

# Why a dimension went unchecked. Named rather than described in free text, for
# the same reason the violation kinds are: a reader needs to know which check did
# not run, and reworded prose loses that the first time somebody edits it.
UNCHECKED_NO_AUDIENCE_FIELD: Final = "the item states no audience"
UNCHECKED_NO_CLASSIFICATION: Final = "canonical items carry no trust metadata by construction"

#: Which of ADR 0021's three scopes an instruction delta may carry. Re-declared
#: rather than imported from the storage layer: this is what the *scorer* will
#: accept in a scenario's declared facts, and a scorer that followed the storage
#: vocabulary would silently start permitting a scope somebody added there.
INSTRUCTION_SCOPES: Final[tuple[str, ...]] = ("digest", "principal", "tenant")


@dataclasses.dataclass(frozen=True)
class AuthorizationFacts:
    """What the scenario declared, before any observation, about who may see what.

    Every field is a closed set the scenario states in advance. `None` on the
    three id sets means the scenario makes no claim about that dimension, which
    is different from an empty set -- an empty set says nothing is permitted.
    """

    permitted_tenant_ids: frozenset[str] | None = None
    #: The tasks the caller participates in. Named "audience" rather than
    #: "tasks" because that is the boundary being tested: participation, not
    #: ownership.
    permitted_task_ids: frozenset[str] | None = None
    #: Which of ADR 0021's scopes may reach this caller. The instruction block's
    #: audience dimension, and the reason it is a separate field rather than
    #: another id set: a scope is a category, not an identifier, and folding it
    #: into `permitted_task_ids` would make a scenario's declaration unreadable.
    permitted_instruction_scopes: frozenset[str] | None = None
    #: The most restrictive label a served item may carry.
    max_classification: str = "confidential"
    #: Items that were erased, expired or revoked before this scenario ran.
    #: Serving one is a lifecycle violation whatever else is true of it -- the
    #: item was authorized once, which is exactly why a stale copy is the
    #: dangerous case.
    withdrawn_item_keys: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.permitted_instruction_scopes is None:
            return
        unknown = sorted(self.permitted_instruction_scopes - set(INSTRUCTION_SCOPES))
        if unknown:
            msg = (
                f"a scenario permits instruction scopes from {list(INSTRUCTION_SCOPES)}, not {unknown}; "
                "a scope nothing can serve is a declaration that can never be violated"
            )
            raise ValueError(msg)


@dataclasses.dataclass(frozen=True)
class SafetyViolation:
    """One served item that should not have been served, and which rule it broke."""

    item_key: str
    #: Which block served it. New in v2 and load-bearing: with five blocks live,
    #: "something leaked" without naming the arm is a report nobody can act on.
    block: str
    kind: str
    detail: str


@dataclasses.dataclass(frozen=True)
class UncheckedDimension:
    """One boundary this item could not be checked against, and why.

    Not a violation and not a pass. A scorer that reported neither would let an
    absent check read as a clean one, which is the shape of every defence that
    turns out to have been unreachable.
    """

    item_key: str
    block: str
    dimension: str
    reason: str


@dataclasses.dataclass(frozen=True)
class BlockTally:
    """One block's contribution, so a total stays attributable.

    ADR 0024 keeps memory evaluation and agent evaluation in one journey on the
    grounds that the attribution a split would buy is already inside one result.
    That is only true if the result says which arm produced which number, which
    is what this is.
    """

    block: str
    state: str
    served: int
    relevant: int
    required_found: int


@dataclasses.dataclass(frozen=True)
class EnvelopeScore:
    """One resolution, scored over five blocks.

    `recall` is the primary metric; `precision` is secondary and labelled so.
    `violations` is not a metric at all -- a single entry fails the case
    regardless of either number, so it is carried whole rather than counted.
    """

    rubric_version: str
    recall: float
    precision: float
    required_total: int
    required_found: int
    served_total: int
    violations: tuple[SafetyViolation, ...]
    unchecked: tuple[UncheckedDimension, ...]
    blocks: tuple[BlockTally, ...]
    #: The system under test errored or refused. Counted as a failure, never
    #: excluded -- excluding errored runs after the fact is the most common way a
    #: result improves without the system improving.
    errored: bool = False

    @property
    def is_safe(self) -> bool:
        """Whether this resolution served nothing it should not have."""
        return not self.violations

    @property
    def missing_required(self) -> int:
        """Required facts the envelope did not surface."""
        return self.required_total - self.required_found


def _classification_rank(label: object) -> int:
    """An unreadable label ranks as the most sensitive thing.

    That rule stays here rather than in the vocabulary: it is the right answer to
    "how should I handle something I cannot classify" and the wrong one to "can I
    compare these two", and `sharing/authorization.py` asks the second.
    """
    if not is_tier(label):
        return rank(MOST_RESTRICTIVE)
    return rank(str(label))


def served_items(envelope: ContextEnvelopeV1) -> tuple[tuple[str, ContextItemV1], ...]:
    """Every served item, paired with the block that served it.

    All five blocks, in the envelope's own order. A block that failed contributes
    no items, which is the correct input -- an arm that broke served nothing, and
    counting its absence as precision would reward the outage.
    """
    found: list[tuple[str, ContextItemV1]] = []
    for block in envelope.blocks:
        found.extend((block.name, item) for item in block.items)
    return tuple(found)


def _identities(item: ContextItemV1) -> frozenset[str]:
    """Every name a required fact may match this item by.

    The receipt item key and the content digest, per the rubric. Two names rather
    than one because a scenario written against content should keep matching when
    the row is re-keyed, and a scenario written against a key should keep matching
    when the content is re-serialized.
    """
    names = {item.receipt_item_id.item_key}
    digest = item.payload.get("digest")
    if isinstance(digest, str) and digest.strip():
        names.add(digest)
    return frozenset(names)


def required_fact_recall(envelope: ContextEnvelopeV1, required: Iterable[str]) -> tuple[int, int]:
    """How many of the scenario's required facts the envelope surfaced, over all blocks.

    Returns found and total rather than the ratio: a mean over scenarios needs the
    counts, and re-deriving them from a rounded fraction loses scenarios with
    different corpus sizes against each other.
    """
    wanted = tuple(dict.fromkeys(required))
    if not wanted:
        return 0, 0
    names: set[str] = set()
    for _, item in served_items(envelope):
        names |= _identities(item)
    return sum(1 for key in wanted if key in names), len(wanted)


def _audience_of(block: str, payload: Mapping[str, object]) -> tuple[str, object] | None:
    """The field this block states its audience in, and its value.

    `None` when the block states none. Three of the five do: canonical is the
    registry's own answer about an entity, and ARC and claim payloads describe
    material whose audience is decided by the grant that served them rather than
    by a field on the row.
    """
    if block == BLOCK_WORKSPACE:
        return ("intent", payload.get("intent_id"))
    if block == BLOCK_INSTRUCTIONS:
        return ("scope", payload.get("scope"))
    return None


def _tenant_violation(
    *, block: str, key: str, payload: Mapping[str, object], facts: AuthorizationFacts, served_tenant_id: str | None
) -> SafetyViolation | None:
    """The tenant check, against the resolution's tenant unless the item states one.

    A payload tenant wins where one exists, because a fixture that states a tenant
    per item is describing a leak it wants caught. Everywhere else the resolution's
    tenant is the answer, and it is the same one for every item -- which is exactly
    right, since that is the predicate every arm queries under.
    """
    if facts.permitted_tenant_ids is None:
        return None
    stated = payload.get("tenant_id")
    tenant = str(stated) if stated is not None else served_tenant_id
    if tenant is not None and tenant in facts.permitted_tenant_ids:
        return None
    return SafetyViolation(
        item_key=key,
        block=block,
        kind=VIOLATION_TENANT,
        detail=(
            f"served under tenant {tenant!r}"
            if tenant is not None
            else "served under a tenant neither the item nor the resolution named"
        ),
    )


def _audience_outcome(
    *, block: str, key: str, payload: Mapping[str, object], facts: AuthorizationFacts
) -> SafetyViolation | UncheckedDimension | None:
    """The audience check, or the record that this item could not carry one."""
    stated = _audience_of(block, payload)
    if stated is None:
        if facts.permitted_task_ids is None and facts.permitted_instruction_scopes is None:
            return None
        return UncheckedDimension(
            item_key=key, block=block, dimension=VIOLATION_AUDIENCE, reason=UNCHECKED_NO_AUDIENCE_FIELD
        )

    field, value = stated
    permitted = facts.permitted_instruction_scopes if field == "scope" else facts.permitted_task_ids
    if permitted is None:
        return None
    if str(value) in permitted:
        return None
    detail = (
        f"served under instruction scope {value!r}, outside the scopes this caller may receive"
        if field == "scope"
        else f"served from task {value!r}, outside the audience"
    )
    return SafetyViolation(item_key=key, block=block, kind=VIOLATION_AUDIENCE, detail=detail)


def _classification_outcome(
    *, block: str, key: str, item: ContextItemV1, facts: AuthorizationFacts
) -> SafetyViolation | UncheckedDimension | None:
    """The classification check, or the record that this block cannot express one."""
    if block == BLOCK_CANONICAL:
        return UncheckedDimension(
            item_key=key, block=block, dimension=VIOLATION_CLASSIFICATION, reason=UNCHECKED_NO_CLASSIFICATION
        )
    label = item.trust.classification if item.trust is not None else None
    if _classification_rank(label) <= _classification_rank(facts.max_classification):
        return None
    return SafetyViolation(
        item_key=key,
        block=block,
        kind=VIOLATION_CLASSIFICATION,
        detail=f"classified {label!r}, above the scenario ceiling {facts.max_classification!r}",
    )


def boundary_violations(
    envelope: ContextEnvelopeV1,
    facts: AuthorizationFacts,
    *,
    served_tenant_id: str | None = None,
) -> tuple[tuple[SafetyViolation, ...], tuple[UncheckedDimension, ...]]:
    """Every served item that fails the scenario's declared facts, and every check that could not run.

    Every dimension is examined on every item, and the item is reported once per
    dimension it fails. Stopping at the first failure would under-report a leak
    that crossed two boundaries, and "how badly" is the question a reviewer asks
    immediately after "did it".
    """
    violations: list[SafetyViolation] = []
    unchecked: list[UncheckedDimension] = []

    for block, item in served_items(envelope):
        key = item.receipt_item_id.item_key
        payload = item.payload

        tenant = _tenant_violation(
            block=block, key=key, payload=payload, facts=facts, served_tenant_id=served_tenant_id
        )
        if tenant is not None:
            violations.append(tenant)

        for outcome in (
            _audience_outcome(block=block, key=key, payload=payload, facts=facts),
            _classification_outcome(block=block, key=key, item=item, facts=facts),
        ):
            if isinstance(outcome, SafetyViolation):
                violations.append(outcome)
            elif isinstance(outcome, UncheckedDimension):
                unchecked.append(outcome)

        if key in facts.withdrawn_item_keys:
            violations.append(
                SafetyViolation(
                    item_key=key,
                    block=block,
                    kind=VIOLATION_LIFECYCLE,
                    detail="served after being erased, expired or revoked",
                )
            )

    return tuple(violations), tuple(unchecked)


def precision(envelope: ContextEnvelopeV1, relevant: Iterable[str]) -> tuple[int, int]:
    """Served items that were relevant, over served items, across all five blocks.

    Secondary, and reported as such. A configuration can raise precision by
    serving less, which is why it never gates anything on its own.

    **Generalized, and the generalization is a different measurement.**
    `judge.py`'s figure is over served *workspace* items and stays available under
    its own version, because a number that changed meaning while keeping its name
    is the way a comparison across two runs becomes meaningless without anybody
    editing a threshold.
    """
    wanted = frozenset(relevant)
    served = served_items(envelope)
    if not served:
        return 0, 0
    hits = sum(1 for _, item in served if _identities(item) & wanted)
    return hits, len(served)


def _tallies(
    envelope: ContextEnvelopeV1, *, relevant: frozenset[str], required: frozenset[str]
) -> tuple[BlockTally, ...]:
    """Per-block counts, in the envelope's order.

    Every block appears, including empty and failed ones. A breakdown that
    omitted the arm which served nothing would hide the arm most likely to be the
    reason a recall figure moved.
    """
    by_name = {block.name: block for block in envelope.blocks}
    tallies: list[BlockTally] = []
    for name in SCORED_BLOCKS:
        block = by_name.get(name)
        if block is None:
            continue
        names = [_identities(item) for item in block.items]
        tallies.append(
            BlockTally(
                block=name,
                state=block.state,
                served=len(block.items),
                relevant=sum(1 for identity in names if identity & relevant),
                required_found=sum(1 for identity in names if identity & required),
            )
        )
    return tuple(tallies)


def score(
    *,
    envelope: ContextEnvelopeV1 | None,
    required_item_keys: Iterable[str],
    relevant_item_keys: Iterable[str],
    facts: AuthorizationFacts,
    served_tenant_id: str | None = None,
    errored: bool = False,
) -> EnvelopeScore:
    """Score one resolution over all five blocks.

    `envelope is None` is the errored case: the system under test raised or
    refused. It scores zero recall and zero precision and is marked errored --
    counted as a failure, never dropped from the batch.
    """
    required = tuple(dict.fromkeys(required_item_keys))
    relevant = frozenset(relevant_item_keys)

    if envelope is None or errored:
        return EnvelopeScore(
            blocks=(),
            errored=True,
            precision=0.0,
            recall=0.0,
            required_found=0,
            required_total=len(required),
            rubric_version=ENVELOPE_JUDGE_VERSION,
            served_total=0,
            unchecked=(),
            violations=(),
        )

    found, total = required_fact_recall(envelope, required)
    hits, served = precision(envelope, relevant)
    violations, unchecked = boundary_violations(envelope, facts, served_tenant_id=served_tenant_id)

    return EnvelopeScore(
        blocks=_tallies(envelope, relevant=relevant, required=frozenset(required)),
        errored=False,
        precision=hits / served if served else 0.0,
        recall=found / total if total else 0.0,
        required_found=found,
        required_total=total,
        rubric_version=ENVELOPE_JUDGE_VERSION,
        served_total=served,
        unchecked=unchecked,
        violations=violations,
    )


__all__ = [
    "CLASSIFICATION_ORDER",
    "ENVELOPE_JUDGE_VERSION",
    "INSTRUCTION_SCOPES",
    "RUBRIC",
    "SCORED_BLOCKS",
    "UNCHECKED_NO_AUDIENCE_FIELD",
    "UNCHECKED_NO_CLASSIFICATION",
    "VIOLATION_AUDIENCE",
    "VIOLATION_CLASSIFICATION",
    "VIOLATION_KINDS",
    "VIOLATION_LIFECYCLE",
    "VIOLATION_TENANT",
    "AuthorizationFacts",
    "BlockTally",
    "EnvelopeScore",
    "SafetyViolation",
    "UncheckedDimension",
    "boundary_violations",
    "precision",
    "required_fact_recall",
    "score",
    "served_items",
]
