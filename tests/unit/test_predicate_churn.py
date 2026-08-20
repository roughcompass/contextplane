"""The churn fit, and the two ways it is stopped from being believed too early.

Most of these are about refusal. The arithmetic is one line and the reason this
module exists at all is that the line is easy to compute and easy to believe for
the wrong reason -- so the tests that matter are the ones proving a number is not
produced, and not selected, until it has earned both.
"""

from __future__ import annotations

import math

import pytest

from contextplane.service.memory.predicate_churn import (
    DEFAULT_WINDOW_DAYS,
    MIN_OBSERVED_SUPERSESSIONS,
    STATUS_ACTIVE,
    STATUS_FITTED,
    STATUS_REJECTED,
    ChurnFitRefused,
    fit,
    half_life_from_rate,
)


class TestHalfLifeArithmetic:
    def test_half_the_population_turning_over_gives_the_window_as_the_half_life(self) -> None:
        """The definitional case, and the one a reader can check without a
        calculator: if half of them went in a year, the half-life is a year."""
        assert half_life_from_rate(rate=0.5, window_days=365) == pytest.approx(365.0)

    def test_a_faster_rate_gives_a_shorter_half_life(self) -> None:
        fast = half_life_from_rate(rate=0.8, window_days=365)
        slow = half_life_from_rate(rate=0.2, window_days=365)
        assert fast < 365.0 < slow

    def test_the_fit_inverts_the_decay_curve_it_feeds(self) -> None:
        """The property that makes the number usable: a claim decayed under this
        half-life for one window retains exactly the surviving fraction."""
        rate = 0.3
        half_life = half_life_from_rate(rate=rate, window_days=365)
        surviving = 0.5 ** (365 / half_life)
        assert surviving == pytest.approx(1.0 - rate)

    def test_nothing_superseded_is_refused_rather_than_called_slow(self) -> None:
        """A statement about the window, not about the predicate. Returning
        infinity here would record 'never changes' from having not looked long
        enough."""
        with pytest.raises(ChurnFitRefused, match="no half-life"):
            half_life_from_rate(rate=0.0, window_days=365)

    def test_everything_superseded_is_refused_rather_than_called_instant(self) -> None:
        with pytest.raises(ChurnFitRefused, match="no half-life"):
            half_life_from_rate(rate=1.0, window_days=365)

    def test_a_window_of_no_days_measures_nothing(self) -> None:
        with pytest.raises(ChurnFitRefused, match="measures nothing"):
            half_life_from_rate(rate=0.5, window_days=0)


class TestTheObservationFloor:
    def test_a_predicate_below_the_floor_carries_no_rate(self) -> None:
        """Three supersessions is not a measurement. Such a predicate falls back
        to its category, which is what the shipped model does with nothing
        better."""
        with pytest.raises(ChurnFitRefused, match="below the floor"):
            fit(predicate="owned_by_team", sampled_claims=400, observed_supersessions=3)

    def test_a_predicate_at_the_floor_is_fitted(self) -> None:
        """The control. A floor that rejected its own boundary would be a floor
        one higher than the one written down."""
        fitted = fit(predicate="owned_by_team", sampled_claims=400, observed_supersessions=MIN_OBSERVED_SUPERSESSIONS)
        assert fitted.observed_supersessions == MIN_OBSERVED_SUPERSESSIONS
        assert fitted.half_life_days > 0

    def test_an_empty_window_is_refused(self) -> None:
        with pytest.raises(ChurnFitRefused, match="nothing was measured"):
            fit(predicate="owned_by_team", sampled_claims=0, observed_supersessions=0)

    def test_more_supersessions_than_claims_is_refused_rather_than_clamped(self) -> None:
        """Clamping would turn a counting bug into a rate of 1.0, which the
        arithmetic above then refuses for a different and misleading reason."""
        with pytest.raises(ChurnFitRefused, match="impossible"):
            fit(predicate="owned_by_team", sampled_claims=30, observed_supersessions=40)


class TestFittedValues:
    def test_a_fast_predicate_fits_a_shorter_half_life_than_a_slow_one(self) -> None:
        """The whole claim of ADR 0003 in one assertion: predicates within one
        category genuinely differ, so measuring them separately says something the
        category figure cannot."""
        fast = fit(predicate="interface_version", sampled_claims=200, observed_supersessions=120)
        slow = fit(predicate="max_request_bytes", sampled_claims=200, observed_supersessions=25)
        assert fast.half_life_days < slow.half_life_days

    def test_the_fit_records_the_sample_it_came_from(self) -> None:
        """So a reader sees how much evidence stood behind the number without a
        join, the same reason the calibration version carries its count."""
        fitted = fit(predicate="deployment_environment", sampled_claims=250, observed_supersessions=100)
        assert fitted.sampled_claims == 250
        assert fitted.observed_supersessions == 100
        assert fitted.observation_window_days == DEFAULT_WINDOW_DAYS
        assert fitted.supersession_rate == pytest.approx(0.4)

    def test_the_stored_half_life_reproduces_the_measured_rate(self) -> None:
        """Rounded to two places for storage, so this asserts the rounding did not
        move the number somewhere a reader could not re-derive."""
        fitted = fit(predicate="lifecycle_state", sampled_claims=300, observed_supersessions=90)
        surviving = 0.5 ** (fitted.observation_window_days / fitted.half_life_days)
        assert surviving == pytest.approx(1.0 - fitted.supersession_rate, abs=1e-4)

    def test_the_window_covers_every_category_the_shipped_model_expects_to_move(self) -> None:
        """A window shorter than the thing being measured cannot see it.

        Four of the shipped categories describe current state and are expected to
        change; the slowest of them is ownership at 270 days, so a one-year window
        contains it. The other three are set at 730 days or more with comments
        saying so outright — a decision that was taken, an incident that happened,
        a summary of a conversation. Those are out of reach of this window on
        purpose: a predicate in one of them will fall below the observation floor
        and keep its authored figure, which is the right answer for a claim whose
        truth does not expire.
        """
        from contextplane.service.memory.confidence_decay import CATEGORY_HALF_LIFE_DAYS

        moving = ("interface_contract", "operational_lifecycle", "dependency", "ownership_stewardship")
        assert DEFAULT_WINDOW_DAYS >= max(CATEGORY_HALF_LIFE_DAYS[name] for name in moving)

        static = set(CATEGORY_HALF_LIFE_DAYS) - set(moving)
        assert all(
            CATEGORY_HALF_LIFE_DAYS[name] >= 2 * DEFAULT_WINDOW_DAYS for name in static
        ), "a category outside the moving set but inside the window would be silently unmeasured"


class TestTheStatusVocabulary:
    def test_a_fit_starts_unselectable(self) -> None:
        """The ADR's rule in the one place a reader will look for it. `store`
        takes no argument that could make a fit active."""
        import inspect

        from contextplane.service.memory.predicate_churn import PredicateChurnService

        signature = inspect.signature(PredicateChurnService.store)
        assert "status" not in signature.parameters
        assert STATUS_FITTED == "fitted"

    def test_a_rejected_fit_is_a_recorded_answer_and_not_an_absence(self) -> None:
        """`rejected` and `fitted` are different states on purpose: one says
        somebody looked and concluded these were corrections, the other says
        nobody has looked. Collapsing them loses the finding."""
        assert STATUS_REJECTED != STATUS_FITTED
        assert STATUS_ACTIVE not in (STATUS_FITTED, STATUS_REJECTED)


def test_the_arithmetic_is_the_stated_formula() -> None:
    """Pinned against the closed form rather than against fixtures, so a change
    to the implementation is compared with the model it claims to implement."""
    for rate in (0.1, 0.25, 0.5, 0.75, 0.9):
        for window in (30, 90, 365):
            expected = window * math.log(2) / -math.log(1.0 - rate)
            assert half_life_from_rate(rate=rate, window_days=window) == pytest.approx(expected)
