"""Ageing a score, and the properties that let it happen at read time.

The whole design rests on two of these: that halving composes, so an effective
value can be derived from a single stored origin, and that ageing only ever lowers
a score, so a minimum-confidence query can prefilter on the stored column and then
apply the exact adjustment.

The rest are judgements about rates. The tests hold the fast-versus-slow ordering
rather than the specific numbers, because the ordering is what the requirement
actually asks for and the numbers will be argued about.
"""

from __future__ import annotations

import datetime

import pytest

from registry.service.memory.claim_ontology import ONTOLOGY
from registry.service.memory.confidence_decay import (
    CATEGORY_HALF_LIFE_DAYS,
    DECAY_FLOOR,
    HISTORICAL_CATEGORIES,
    MIN_CHANGE_OBSERVATIONS,
    SUBJECT_VOLATILITY_MAX_FACTOR,
    SUBJECT_VOLATILITY_MIN_FACTOR,
    confirmation_hold_days,
    effective_confidence,
    half_life_days,
)

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


def _aged(stored: float, days: float, half_life: float, **kw: object) -> float:
    return effective_confidence(
        stored,
        scored_at=_NOW,
        half_life=half_life,
        now=_NOW + datetime.timedelta(days=days),
        **kw,  # type: ignore[arg-type]
    )


# --- the two load-bearing properties -----------------------------------------


def test_halving_composes() -> None:
    """Ageing thirty days then thirty more equals ageing sixty. This is the
    property that lets an effective value be derived at read time from one stored
    origin instead of rewritten by a job."""
    once = _aged(0.8, 60, 90)
    stepwise = effective_confidence(
        _aged(0.8, 30, 90),
        scored_at=_NOW + datetime.timedelta(days=30),
        half_life=90,
        now=_NOW + datetime.timedelta(days=60),
    )
    assert once == pytest.approx(stepwise, abs=1e-9)


def test_ageing_only_ever_lowers_a_score() -> None:
    """What makes a stored value a sound upper bound: a minimum-confidence query
    can narrow on the indexed column first, because no claim stored below a
    threshold can age up through it."""
    stored = 0.8
    previous = stored
    for days in (0, 1, 10, 100, 1000, 10_000):
        current = _aged(stored, days, 90)
        assert current <= previous + 1e-12
        previous = current


def test_clock_skew_cannot_raise_a_score() -> None:
    """A negative age would otherwise multiply rather than divide."""
    backwards = effective_confidence(0.8, scored_at=_NOW, half_life=90, now=_NOW - datetime.timedelta(days=30))
    assert backwards == 0.8


# --- the floor ----------------------------------------------------------------


def test_decay_stops_at_the_floor() -> None:
    """An assertion somebody made, citing evidence that still exists, never becomes
    less informative than no assertion at all. Decaying to zero would claim it is
    indistinguishable from an invention."""
    ancient = _aged(0.95, 100_000, 90)
    assert ancient >= DECAY_FLOOR
    assert ancient == pytest.approx(DECAY_FLOOR, abs=1e-6)


def test_a_score_already_at_the_floor_does_not_move() -> None:
    assert _aged(DECAY_FLOOR, 5000, 90) == pytest.approx(DECAY_FLOOR)


def test_one_half_life_removes_half_the_distance_above_the_floor() -> None:
    """The definition, asserted so a change to the curve shape is visible."""
    stored = 0.90
    after = _aged(stored, 90, 90)
    assert after == pytest.approx(DECAY_FLOOR + (stored - DECAY_FLOOR) / 2, abs=1e-6)


# --- category rates -----------------------------------------------------------


def test_an_interface_claim_decays_faster_than_an_ownership_claim() -> None:
    """The requirement's own example. Operations and timeouts move with releases;
    a team assignment holds for most of a year."""
    assert CATEGORY_HALF_LIFE_DAYS["interface_contract"] < CATEGORY_HALF_LIFE_DAYS["ownership_stewardship"]


def test_a_decision_barely_decays() -> None:
    """A decision that was taken does not become less true with age. What changes
    is whether it still governs, and there is a predicate for saying so."""
    # The slowest of the categories that describe *current state*. The historical
    # ones are slower still and are excluded by name rather than by listing them
    # here, so adding one does not silently change what this test asserts.
    assert CATEGORY_HALF_LIFE_DAYS["decision_rationale"] == max(
        CATEGORY_HALF_LIFE_DAYS[c] for c in CATEGORY_HALF_LIFE_DAYS if c not in HISTORICAL_CATEGORIES
    )


def test_every_shipped_category_has_a_rate() -> None:
    """A category with no rate would fall to the default and decay at a speed
    nobody chose for it."""
    for seed in ONTOLOGY:
        assert seed.claim_category in CATEGORY_HALF_LIFE_DAYS, seed.claim_category


def test_an_unknown_category_decays_at_the_slowest_rate() -> None:
    """Guessing fast would silently retire claims under a category somebody added
    without considering how quickly its subjects move."""
    unknown = half_life_days("something_new")
    assert unknown == max(CATEGORY_HALF_LIFE_DAYS.values())


def test_two_categories_with_different_rates_decay_differently() -> None:
    """Half of the fourth exit criterion, at the category level."""
    stored = 0.8
    fast = _aged(stored, 90, CATEGORY_HALF_LIFE_DAYS["interface_contract"])
    slow = _aged(stored, 90, CATEGORY_HALF_LIFE_DAYS["ownership_stewardship"])
    assert fast < slow


# --- subject volatility --------------------------------------------------------


def test_a_fast_changing_subject_decays_faster_than_a_slow_one() -> None:
    """The other half of the fourth exit criterion, and the part a category-only
    rate cannot express: two subjects, one category."""
    volatile = half_life_days("interface_contract", subject_median_change_days=5.0, subject_change_observations=10)
    stable = half_life_days("interface_contract", subject_median_change_days=200.0, subject_change_observations=10)
    assert volatile < stable


def test_the_subject_modifier_is_bounded_in_both_directions() -> None:
    """The category is the primary signal and the subject is a modifier. Unbounded,
    one churning entity would decay to nothing in days and one dormant entity would
    never decay at all."""
    base = CATEGORY_HALF_LIFE_DAYS["interface_contract"]
    extreme_fast = half_life_days("interface_contract", subject_median_change_days=0.01, subject_change_observations=99)
    extreme_slow = half_life_days(
        "interface_contract", subject_median_change_days=10_000.0, subject_change_observations=99
    )
    assert extreme_fast == pytest.approx(base * SUBJECT_VOLATILITY_MIN_FACTOR)
    assert extreme_slow == pytest.approx(base * SUBJECT_VOLATILITY_MAX_FACTOR)


def test_an_unwatched_subject_gets_no_modifier_rather_than_a_guess() -> None:
    """An entity nobody has watched change is not an entity that changes slowly.
    Claiming otherwise would be inventing an observation."""
    unmodified = half_life_days("interface_contract")
    assert unmodified == CATEGORY_HALF_LIFE_DAYS["interface_contract"]


def test_too_little_history_is_treated_as_no_history() -> None:
    """One or two observed changes is not a rate."""
    thin = half_life_days(
        "interface_contract",
        subject_median_change_days=1.0,
        subject_change_observations=MIN_CHANGE_OBSERVATIONS - 1,
    )
    assert thin == CATEGORY_HALF_LIFE_DAYS["interface_contract"]


def test_a_tenant_multiplier_scales_the_rate() -> None:
    """A tenant knows how fast its own capabilities move. It does not get to decide
    that ownership changes faster than an interface, which is why this multiplies
    rather than replaces."""
    base = half_life_days("dependency")
    doubled = half_life_days("dependency", tenant_multiplier=2.0)
    assert doubled == pytest.approx(base * 2)


def test_a_tenant_multiplier_cannot_produce_a_zero_half_life() -> None:
    """A zero half-life would divide by zero and, before that, mean every claim is
    worthless the instant it is written."""
    assert half_life_days("dependency", tenant_multiplier=0.0) > 0


# --- prose does not decay ------------------------------------------------------


def test_prose_does_not_decay() -> None:
    """A summary of what happened on a date does not become less true. Keyed on the
    value type so a future prose predicate inherits the exemption."""
    assert _aged(0.5, 10_000, 90, value_type="prose") == 0.5


# --- confirmation holds --------------------------------------------------------


def test_a_confirmation_holds_decay_off_entirely() -> None:
    held = effective_confidence(
        0.92,
        scored_at=_NOW,
        half_life=90,
        now=_NOW + datetime.timedelta(days=30),
        hold_until=_NOW + datetime.timedelta(days=90),
    )
    assert held == 0.92


def test_decay_resumes_from_the_confirmation_not_from_the_original_assertion() -> None:
    """Resuming from where decay would have been would make a confirmation
    worthless the moment its hold expired -- a long-lived claim confirmed today
    would snap back toward the floor on that day."""
    hold_until = _NOW + datetime.timedelta(days=90)
    just_after = effective_confidence(
        0.92,
        scored_at=_NOW,
        half_life=90,
        now=hold_until + datetime.timedelta(days=1),
        hold_until=hold_until,
    )
    # One day past a 90-day half-life removes only a sliver, not 91 days' worth.
    assert just_after > 0.90


def test_a_confirmation_hold_is_capped_by_how_fast_the_category_moves() -> None:
    """Confirming a fast-moving interface should not hold as long as confirming who
    owns something."""
    interface = confirmation_hold_days("interface_contract")
    ownership = confirmation_hold_days("ownership_stewardship")
    assert interface < ownership
    assert interface == CATEGORY_HALF_LIFE_DAYS["interface_contract"]


def test_a_configured_hold_cannot_exceed_the_category_ceiling() -> None:
    """A tenant asking for a two-year hold on an interface claim is asking for a
    stale claim to look fresh."""
    generous = confirmation_hold_days("interface_contract", configured=730.0)
    assert generous == CATEGORY_HALF_LIFE_DAYS["interface_contract"]


def test_a_shorter_configured_hold_is_respected() -> None:
    assert confirmation_hold_days("ownership_stewardship", configured=30.0) == 30.0


# --- worked curves ------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "half_life", "expected"),
    [
        (0, 90, 0.705),
        (45, 90, 0.528),
        (90, 90, 0.403),
        (180, 90, 0.251),
        (90, 270, 0.581),
        (270, 270, 0.403),
    ],
)
def test_the_curve_matches_its_stated_shape(days: int, half_life: int, expected: float) -> None:
    """Pinned so a change to the arithmetic is visible rather than plausible."""
    assert _aged(0.705, days, half_life) == pytest.approx(expected, abs=0.002)
