"""How a claim loses confidence as its assertion ages.

Decay is what stops an old, once-confident claim from looking permanently
authoritative. It is not a shelf life: the rate reflects how fast the thing being
described actually changes, so an interface claim about an actively-released
capability loses value far faster than a statement about who owns it.

**This is the one part of a score computed when it is read.** Everything depending
on other rows -- corroboration, disagreement -- is computed and stored when a claim
is written, because "why did this score as it did" is a question about the past and
the neighbourhood may have moved since. Age depends on nothing but the clock and
four values already on the row, so the stored score stays immutable between writes
and the effective value is derived.

A periodic job rewriting scores would be worse on exactly the ground it looks
better on. After it ran, the number that was actually served would be gone unless
the job also wrote a history row -- one row per claim per tick, containing no new
information. It would also be O(all claims) writes producing no facts, and it would
invalidate the stored audit record on every pass.

**The rate is keyed on the category and modified by the subject.** Category alone
cannot distinguish two subjects that change at different speeds; subject alone has
no behaviour for an entity nobody has watched, and would flatten the fast-versus-slow
distinction between an interface and an owner. Per predicate would be twenty-six
figures nobody could defend one at a time.
"""

from __future__ import annotations

import datetime

# The age at which an assertion has lost half its value above the floor, per
# category. Six numbers rather than twenty-six: the differences that are real are
# between categories.
CATEGORY_HALF_LIFE_DAYS: dict[str, float] = {
    # The fastest. Operations, timeouts, and size limits move with releases, and a
    # quarter is roughly one release train.
    "interface_contract": 90.0,
    # Environments and lifecycle state move with rollouts; availability targets and
    # recovery objectives are set once a year. This figure is a compromise between
    # the two and is the least defensible of the six.
    "operational_lifecycle": 120.0,
    # Dependency graphs move with refactors rather than releases.
    "dependency": 180.0,
    # Reorganisations happen roughly annually, so a team assignment holds for most
    # of a year. This is the slow category the requirement contrasts against an
    # interface claim.
    "ownership_stewardship": 270.0,
    # A decision that was taken does not become less true with age. What changes is
    # whether it still governs, and there is a predicate for saying so. Long enough
    # to be effectively no decay, finite so the mechanism needs no special case.
    "decision_rationale": 730.0,
    # A summary of what happened on a date is a historical record. Included so
    # every category has an entry, and excluded from decay by value type below.
    "session_summary": 3650.0,
}

# Below this, confidence stops falling. An assertion somebody made, citing evidence
# that still exists, never becomes less informative than no assertion at all --
# decaying to zero would claim it is indistinguishable from an invention. The floor
# sits inside the bucket whose published meaning is "do not act on this", so a
# fully-decayed claim stays visible and obviously stale rather than disappearing
# under a caller's default minimum.
DECAY_FLOOR = 0.10

# A subject changing every thirty days decays at its category's rate. Faster
# subjects decay faster and slower ones slower, bounded so no subject differs from
# its category by more than fourfold: how fast the subject moves is a modifier on
# the category, not a replacement for it.
SUBJECT_VOLATILITY_BASELINE_DAYS = 30.0
SUBJECT_VOLATILITY_MIN_FACTOR = 0.5
SUBJECT_VOLATILITY_MAX_FACTOR = 2.0

# Below this many recorded changes there is no volatility estimate, and the
# category rate applies unmodified. An entity nobody has watched change is not an
# entity that changes slowly; saying so would be inventing an observation.
MIN_CHANGE_OBSERVATIONS = 3

# How long a human confirmation holds decay off, capped by how fast the category
# moves. Confirming a fast-moving interface should not hold as long as confirming
# who owns something.
MAX_CONFIRMATION_HOLD_DAYS = 180.0

# Types that do not decay. A summary of what happened on a date does not become
# less true, and keying on the type rather than the predicate means a future prose
# predicate inherits the exemption.
NON_DECAYING_VALUE_TYPES = frozenset({"prose"})

_SECONDS_PER_DAY = 86400.0


def half_life_days(
    claim_category: str,
    *,
    subject_median_change_days: float | None = None,
    subject_change_observations: int = 0,
    tenant_multiplier: float = 1.0,
) -> float:
    """How long this claim takes to lose half its value above the floor.

    The category sets the rate and the subject modifies it. An entity with too
    little history gets no modifier rather than a guessed one: claiming a subject
    changes slowly because nobody has watched it would be inventing an observation.
    """
    base = CATEGORY_HALF_LIFE_DAYS.get(claim_category)
    if base is None:
        # An unrecognized category decays at the slowest rate rather than the
        # fastest. Guessing fast would silently retire claims under a category
        # somebody added without considering how quickly its subjects move.
        base = max(CATEGORY_HALF_LIFE_DAYS.values())

    factor = 1.0
    if (
        subject_median_change_days is not None
        and subject_change_observations >= MIN_CHANGE_OBSERVATIONS
        and subject_median_change_days > 0
    ):
        factor = min(
            SUBJECT_VOLATILITY_MAX_FACTOR,
            max(
                SUBJECT_VOLATILITY_MIN_FACTOR,
                subject_median_change_days / SUBJECT_VOLATILITY_BASELINE_DAYS,
            ),
        )
    return base * factor * max(0.01, tenant_multiplier)


def confirmation_hold_days(claim_category: str, *, configured: float | None = None) -> float:
    """How long a human confirmation holds decay off.

    Capped by the category's own half-life, so confirming a fast-moving interface
    does not hold as long as confirming a team assignment.
    """
    ceiling = CATEGORY_HALF_LIFE_DAYS.get(claim_category, MAX_CONFIRMATION_HOLD_DAYS)
    limit = configured if configured is not None else MAX_CONFIRMATION_HOLD_DAYS
    return min(limit, ceiling)


def effective_confidence(
    stored: float,
    *,
    scored_at: datetime.datetime,
    half_life: float,
    now: datetime.datetime,
    hold_until: datetime.datetime | None = None,
    value_type: str | None = None,
) -> float:
    """What a stored score is worth at `now`.

    Pure, and exactly reproducible for any past instant, which is what makes a
    value that moves without a write auditable rather than mysterious.

    Halving rather than a straight line, for two reasons. A straight line has a
    date on which the claim hits bottom, which reads as an expiry, and confidence
    is an estimate rather than a shelf life. And halving composes: ageing thirty
    days and then thirty more gives the same answer as ageing sixty, which is the
    property that lets this be derived at read time from a single origin.
    """
    if value_type in NON_DECAYING_VALUE_TYPES:
        return stored
    if half_life <= 0:
        return stored

    if hold_until is not None and now < hold_until:
        # A human looked at this recently. Nothing has aged yet.
        return stored

    # A confirmation resets the origin rather than resuming from where decay would
    # have been. Resuming would make the confirmation worthless the day its window
    # closed -- a claim confirmed after a long life would snap back to the floor,
    # and a person who spent time on it would rightly call that a bug.
    start = hold_until if hold_until is not None else scored_at
    # Clamped at zero so clock skew can never raise a score.
    age_days = max(0.0, (now - start).total_seconds() / _SECONDS_PER_DAY)
    return float(DECAY_FLOOR + (stored - DECAY_FLOOR) * (2.0 ** (-age_days / half_life)))


__all__ = [
    "CATEGORY_HALF_LIFE_DAYS",
    "DECAY_FLOOR",
    "MAX_CONFIRMATION_HOLD_DAYS",
    "MIN_CHANGE_OBSERVATIONS",
    "NON_DECAYING_VALUE_TYPES",
    "SUBJECT_VOLATILITY_BASELINE_DAYS",
    "SUBJECT_VOLATILITY_MAX_FACTOR",
    "SUBJECT_VOLATILITY_MIN_FACTOR",
    "confirmation_hold_days",
    "effective_confidence",
    "half_life_days",
]
