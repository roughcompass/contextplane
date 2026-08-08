"""What a confidence number means, and how one is arrived at.

Confidence is the estimated probability that a claim is correct as asserted over
its effective interval, on a `[0,1]` scale with published bucket semantics. It is
not a model's opinion of its own output: a self-reported score is an input, and
only once somebody has checked what that provider's numbers turn out to be worth.

**Confidence is not authority.** Authority says where a claim came from and decides
which claim supersedes which. Confidence says how likely it is to be right.
Independent sources agreeing raises the second and never the first -- a claim does
not come from somewhere else because two people said it.

**Computed when a claim is written or rescored, and stored with its inputs.** A
reader asking why a claim scored as it did needs the answer that was true at the
time; the neighbourhood may have moved since. Age is the one exception, because it
depends on nothing but the clock and values already on the row -- see
`confidence_decay`.

**The scoring function is pure.** Everything it needs is passed in. That is what
makes `recompute(stored_inputs) == stored_confidence` a testable property over
every row, which is the only real definition of auditable here.
"""

from __future__ import annotations

import dataclasses
import math
from collections import defaultdict
from collections.abc import Sequence

from contextplane.service.governance.authority import (
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OBSERVER_INFERENCE,
    AUTHORITY_OWNER_EXTRACTION,
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_OWNER_INFERENCE,
    SOURCE_AUTHORITY_RANK,
)

# Identifies the function that produced a score. Bumped whenever the arithmetic
# changes, because without it a scoring change makes every historical score
# unreproducible and turns a calibration set into a mixture of numbers from
# different functions.
SCORER_VERSION = "confidence-v1"

# --- the published scale ----------------------------------------------------
#
# Part of the interface contract, and not configurable per tenant. A consumer
# filtering at 0.8 is asserting something specific, and it has to mean the same
# thing in every tenant or the filter means nothing.
#
# Five buckets, none narrower than the accuracy tolerance a calibration check can
# verify. A narrower bucket would claim a resolution no evaluation could confirm --
# a caller choosing between 0.80 and 0.85 would be choosing between two numbers
# provably meaning the same thing. Five is also the number of evidence states this
# pipeline can distinguish: nothing, one weak observation, one good observation,
# independent agreement, and a human who looked.
BUCKET_UNRELIABLE = "unreliable"
BUCKET_WEAK = "weak"
BUCKET_MODERATE = "moderate"
BUCKET_STRONG = "strong"
BUCKET_CONFIRMED = "confirmed"

#: Lower bound of each bucket, strongest first. Ranges are lower-closed and
#: upper-open, so a value sitting exactly on a boundary belongs to the bucket
#: above it and a caller thresholding on the boundary knows which side they get.
BUCKET_LOWER_BOUNDS: tuple[tuple[str, float], ...] = (
    (BUCKET_CONFIRMED, 0.85),
    (BUCKET_STRONG, 0.70),
    (BUCKET_MODERATE, 0.45),
    (BUCKET_WEAK, 0.20),
    (BUCKET_UNRELIABLE, 0.00),
)

#: What each bucket licenses a consumer to do. Published because a number without
#: a stated meaning is one every caller interprets differently.
BUCKET_SEMANTICS: dict[str, str] = {
    BUCKET_CONFIRMED: (
        "A human with standing put their name to this claim, within the confirmation "
        "window. The only bucket to treat as you would treat the canonical graph."
    ),
    BUCKET_STRONG: (
        "Independently corroborated, or a reproducible extraction by the owner. Act "
        "without re-verifying, but do not make an irreversible change on it alone."
    ),
    BUCKET_MODERATE: (
        "The resting state of an uncorroborated machine extraction from a source with "
        "standing. Act on it where being wrong is cheap and reversible."
    ),
    BUCKET_WEAK: (
        "A single unconfirmed observation from a source without standing, or an "
        "assertion contested or decayed past usefulness. A lead to verify, not a fact."
    ),
    BUCKET_UNRELIABLE: (
        "Do not act on this. Present only because suppressing it would hide that an " "assertion exists at all."
    ),
}

# Nothing here is certain. A scale on which something reaches 1.0 has nowhere left
# to express "and this one was checked twice".
MAX_CONFIDENCE = 0.98

# Below this, age stops lowering a score. An assertion somebody made, citing
# evidence that still exists, never becomes less informative than no assertion at
# all -- decaying to zero would claim it is indistinguishable from an invention.
DECAY_FLOOR = 0.10

# --- base score by authority tier -------------------------------------------
#
# Where a claim starts, before anything corroborates or contradicts it.
#
# Derived rather than chosen: standing multiplied by reproducibility, with owner at
# 1.00 and observer at 0.52, against human 0.80, reproducible parse 0.62, and model
# inference 0.45. The observer factor is not free -- anything above 0.5625 would
# place a non-owner's first-hand statement above an owner's model inference and
# invert the authority ladder. The products are written out because this table is
# what a tenant configures, what a test asserts, and what an audit record names;
# recomputing it at import would let those three drift.
#
# The gap between an owner's inference and an observer's first-hand statement is
# deliberately narrow. Those two really are close in how often they turn out right.
# The ladder says the owner wins; this says it wins narrowly.
#
# The strongest value sits in the second-highest bucket, not the highest. The top
# bucket is reserved for a human who reviewed this particular claim, which is a
# different event from an owner asserting something in the first person.
BASE_CONFIDENCE_BY_AUTHORITY: dict[str, float] = {
    AUTHORITY_OWNER_HUMAN: 0.80,
    AUTHORITY_OWNER_EXTRACTION: 0.62,
    AUTHORITY_OWNER_INFERENCE: 0.45,
    AUTHORITY_OBSERVER_HUMAN: 0.42,
    AUTHORITY_OBSERVER_EXTRACTION: 0.32,
    AUTHORITY_OBSERVER_INFERENCE: 0.23,
    # No entry for an unresolved subject. Such a claim is excluded from scoring
    # altogether and its confidence is null rather than low: a number there would
    # assert a determination nobody made, and nothing would mark it stale once
    # curation links the claim to an entity.
}

# What a human confirmation is worth. Below the ceiling, because a human can be
# wrong, and far enough inside the top bucket that a single disagreement drops it
# out -- a confirmed claim that is contested is not confirmed-and-uncontested, and
# the bucket should say so.
CONFIRMED_CONFIDENCE = 0.92

# What a detected disagreement costs, as a share of the distance above the floor.
# Deliberately modest: the contested mark is what carries the consequence -- such a
# claim cannot be promoted and always needs review -- and a large score penalty on
# top would be counting the same fact twice. Review eligibility deliberately does
# not read confidence at all, because certainty about a consequential change is a
# reason to look at it rather than a reason to skip it.
CONTRADICTION_PENALTY = 0.25

# --- corroboration ----------------------------------------------------------
#
# How much one independent source agreeing is worth, by the authority of that
# source. Strictly decreasing across the authority ladder, and that is a constraint
# rather than an observation: the ladder is the only ordering over these values,
# and a second table ordered differently would eventually have a rule built on the
# wrong one. Monotonicity is asserted by test.
#
# This raises confidence and nothing else. It never enters the authority column,
# never affects which claim supersedes which, and never reorders the ladder.
CORROBORATION_WEIGHT_BY_RANK: dict[int, float] = {
    0: 1.00,  # owner, first-person human
    1: 0.85,  # owner, reproducible parse
    2: 0.60,  # owner, model inference
    3: 0.50,  # observer, first-person human
    4: 0.40,  # observer, reproducible parse
    5: 0.25,  # observer, model inference
    6: 0.00,  # subject unresolved -- not scored at all
}

# The share of the distance to certainty that corroboration alone may close.
# Applied to what remains above the claim's own base, so a claim that already
# scores highly gains less: there is less left to learn.
CORROBORATION_HEADROOM_FRACTION = 0.60

# Sets how fast the curve flattens. At 2.0 the second independent source is worth
# roughly two thirds of the first and the fifth roughly a tenth, so volume cannot
# substitute for quality.
CORROBORATION_SCALE = 2.0

# One person observing the same thing in many sessions is still one person.
# Separate sessions are separate occasions and do corroborate, but a single actor's
# contribution stops here -- otherwise an agent re-observing a fact every session
# would ratchet to the ceiling off a single source, which is the repetition the
# independence rule exists to exclude.
MAX_CLASSES_PER_ACTOR = 2


def bucket_for(confidence: float) -> str:
    """Which published bucket a score falls in."""
    for name, lower in BUCKET_LOWER_BOUNDS:
        if confidence >= lower:
            return name
    return BUCKET_UNRELIABLE


@dataclasses.dataclass(frozen=True)
class ConfidencePolicy:
    """A tenant's weighting. Defaults are the shipped values.

    An absent policy row resolves to this, so a deployment that configures nothing
    scores normally. A tenant may move the weights; it may not reorder the ladder,
    change a bucket boundary, or decide which authority tier a claim receives.
    """

    base_by_authority: dict[str, float] = dataclasses.field(default_factory=lambda: dict(BASE_CONFIDENCE_BY_AUTHORITY))
    corroboration_headroom: float = CORROBORATION_HEADROOM_FRACTION
    corroboration_scale: float = CORROBORATION_SCALE
    contradiction_penalty: float = CONTRADICTION_PENALTY
    confirmed_confidence: float = CONFIRMED_CONFIDENCE
    confirmation_hold_days: int = 180
    decay_multiplier: float = 1.0

    def __post_init__(self) -> None:
        # Deliberately a bare `ValueError`, not this codebase's
        # `ValidationError`, and left that way on purpose: every production
        # construction of `ConfidencePolicy` uses the all-defaults form
        # (`ConfidencePolicy()`, which always satisfies both checks below --
        # see `claim_writer.py`, `claim_curator_actions.py`,
        # `consolidation.py`, `confirmation.py`). No request boundary in this
        # deployment ever builds one from caller-supplied weights, so there is
        # no router or MCP tool catch site whose exception type this could
        # fall out of step with -- unlike the request-facing raises this task
        # did rebase (`claim_serving.py`, `contest.py::resolve`), this is a
        # config-shape invariant with no live consumer to protect.
        ranks = [SOURCE_AUTHORITY_RANK[tier] for tier in self.base_by_authority if tier in SOURCE_AUTHORITY_RANK]
        ordered = [
            self.base_by_authority[tier]
            for tier in sorted(self.base_by_authority, key=lambda t: SOURCE_AUTHORITY_RANK[t])
        ]
        if ordered != sorted(ordered, reverse=True) or len(set(ordered)) != len(ordered):
            msg = (
                "base confidences must strictly decrease across the authority ladder; a "
                "configuration where a non-owner outranks an owner contradicts the rule that "
                f"only owners assert authoritative facts (got {self.base_by_authority!r})"
            )
            raise ValueError(msg)
        if not ranks:
            msg = "a policy must assign a base confidence to at least one authority tier"
            raise ValueError(msg)


@dataclasses.dataclass(frozen=True)
class EvidenceClass:
    """One piece of evidence, reduced to what corroboration needs.

    `key` identifies the independence class: several turns of one conversation, or
    several runs of one connector over one source, share a key. `group` is the
    capping unit -- an actor, or a connector source -- so one person across many
    sessions cannot substitute repetition for independence.
    """

    key: str
    group: str
    authority_rank: int


@dataclasses.dataclass(frozen=True)
class ConfidenceInputs:
    """Everything the score was computed from, as stored beside it.

    Recorded rather than re-derived so that "why did this claim score as it did"
    can be answered from the row. The neighbourhood may have changed since, and a
    reader asking about the past should not get an answer about the present.
    """

    authority: str
    base: float
    corroborating_classes: int
    corroborating_mass: float
    is_contested: bool
    is_confirmed: bool
    provider_confidence: float | None
    provider_applied: bool
    scorer_version: str = SCORER_VERSION

    def as_json(self) -> dict[str, object]:
        """Serialize to dict for storage in claim.confidence_inputs_jsonb."""
        return {
            "authority": self.authority,
            "base": self.base,
            "corroborating_classes": self.corroborating_classes,
            "corroborating_mass": round(self.corroborating_mass, 6),
            "is_contested": self.is_contested,
            "is_confirmed": self.is_confirmed,
            "provider_confidence": self.provider_confidence,
            # Stated positively rather than by omission: an absent field reads as
            # "fine", and the whole point is that nothing has checked what the
            # provider's number predicts.
            "provider_applied": self.provider_applied,
            "scorer_version": self.scorer_version,
        }


@dataclasses.dataclass(frozen=True)
class ScoredConfidence:
    """The final confidence score, bucket label, and the inputs that produced it."""

    value: float
    bucket: str
    inputs: ConfidenceInputs


def corroborating_mass(
    classes: Sequence[EvidenceClass],
    *,
    max_per_group: int = MAX_CLASSES_PER_ACTOR,
) -> tuple[float, int]:
    """Weighted mass of independent agreement, and how many classes it came from.

    Deduplicates by independence key first: several pieces of evidence tracing to
    one conversation, or several runs of one connector over one source, are one
    source however many rows they occupy. Then caps each group, so a single actor
    across many sessions cannot substitute repetition for independence.
    """
    strongest_by_key: dict[str, tuple[str, int]] = {}
    for item in classes:
        current = strongest_by_key.get(item.key)
        if current is None or item.authority_rank < current[1]:
            strongest_by_key[item.key] = (item.group, item.authority_rank)

    per_group: dict[str, list[int]] = defaultdict(list)
    for group, rank in strongest_by_key.values():
        per_group[group].append(rank)

    mass = 0.0
    counted = 0
    for ranks in per_group.values():
        # Strongest first, so a cap discards the weakest evidence rather than
        # whichever happened to be stored last.
        for rank in sorted(ranks)[:max_per_group]:
            mass += CORROBORATION_WEIGHT_BY_RANK.get(rank, 0.0)
            counted += 1
    return mass, counted


def score(
    *,
    authority: str,
    corroborators: Sequence[EvidenceClass] = (),
    is_contested: bool = False,
    is_confirmed: bool = False,
    provider_confidence: float | None = None,
    provider_mapping: object | None = None,
    policy: ConfidencePolicy | None = None,
) -> ScoredConfidence | None:
    """The score for one claim, or None when the claim is not scored at all.

    Order of operations is fixed and matters:

    1. base from the authority tier -- None and stop if the subject did not resolve
    2. the provider's own number, which contributes nothing until calibrated
    3. corroboration
    4. human confirmation, which replaces steps 1 to 3
    5. the disagreement penalty, applied *after* confirmation, because a later
       disagreement can still contest a confirmed claim
    6. clamp into the servable range

    The clamp is what guarantees the stored value never sits below the floor age
    decays toward, which is what makes ageing monotone and lets a
    minimum-confidence query prefilter on an index.
    """
    active = policy or ConfidencePolicy()

    base = active.base_by_authority.get(authority)
    if base is None:
        # No subject means no owner to compare against, so nothing to score. A low
        # number here would assert a determination nobody made.
        return None

    mass, class_count = corroborating_mass(corroborators)
    provider_applied = False

    if provider_mapping is not None and provider_confidence is not None:
        # Reserved for a calibrated mapping. Nothing supplies one yet, and using
        # an uncalibrated self-report would launder an unexamined number into an
        # authoritative-looking signal.
        provider_applied = True

    value = base + (1.0 - base) * active.corroboration_headroom * (1.0 - math.exp(-mass / active.corroboration_scale))

    if is_confirmed:
        # A human who reviewed this claim replaces the machine estimate rather
        # than adjusting it. Averaging would make confirmation worth less the
        # weaker the original source was, which is backwards.
        value = active.confirmed_confidence

    if is_contested:
        # Applied last so it can still contest a confirmed claim, and
        # multiplicatively on the headroom above the floor so repeated
        # disagreements approach the floor without ever crossing it.
        value = DECAY_FLOOR + (value - DECAY_FLOOR) * (1.0 - active.contradiction_penalty)

    value = min(MAX_CONFIDENCE, max(DECAY_FLOOR, value))

    return ScoredConfidence(
        value=round(value, 3),
        bucket=bucket_for(value),
        inputs=ConfidenceInputs(
            authority=authority,
            base=base,
            corroborating_classes=class_count,
            corroborating_mass=mass,
            is_contested=is_contested,
            is_confirmed=is_confirmed,
            provider_confidence=provider_confidence,
            provider_applied=provider_applied,
        ),
    )


def recompute(inputs: ConfidenceInputs, *, policy: ConfidencePolicy | None = None) -> float:
    """Re-derive a stored score from its stored inputs.

    The definition of auditable that matters: a reader can take the record beside
    a claim and arrive at the same number. Shipped as a test over every row.
    """
    active = policy or ConfidencePolicy()
    value = inputs.base + (1.0 - inputs.base) * active.corroboration_headroom * (
        1.0 - math.exp(-inputs.corroborating_mass / active.corroboration_scale)
    )
    if inputs.is_confirmed:
        value = active.confirmed_confidence
    if inputs.is_contested:
        value = DECAY_FLOOR + (value - DECAY_FLOOR) * (1.0 - active.contradiction_penalty)
    return round(min(MAX_CONFIDENCE, max(DECAY_FLOOR, value)), 3)


__all__ = [
    "BASE_CONFIDENCE_BY_AUTHORITY",
    "BUCKET_CONFIRMED",
    "BUCKET_LOWER_BOUNDS",
    "BUCKET_MODERATE",
    "BUCKET_SEMANTICS",
    "BUCKET_STRONG",
    "BUCKET_UNRELIABLE",
    "BUCKET_WEAK",
    "CONFIRMED_CONFIDENCE",
    "CONTRADICTION_PENALTY",
    "CORROBORATION_HEADROOM_FRACTION",
    "CORROBORATION_SCALE",
    "CORROBORATION_WEIGHT_BY_RANK",
    "DECAY_FLOOR",
    "MAX_CLASSES_PER_ACTOR",
    "MAX_CONFIDENCE",
    "SCORER_VERSION",
    "ConfidenceInputs",
    "ConfidencePolicy",
    "EvidenceClass",
    "ScoredConfidence",
    "bucket_for",
    "corroborating_mass",
    "recompute",
    "score",
]
