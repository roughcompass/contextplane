"""The bound, and the promotions it is meant to stop.

The arithmetic is pinned against the figures the plan quotes, so a later change
to the interval maths shows up as a disagreement with the reasoning that
justified the rule rather than as a silently different threshold.
"""

from __future__ import annotations

import pytest

from contextplane.service.memory.promotion_bound import (
    clears_incumbent,
    wilson_lower_bound,
)


class TestWilsonLowerBound:
    def test_nineteen_of_twenty_lands_near_the_figure_the_plan_quotes(self) -> None:
        """The number the whole rule was argued from: 0.95 observed, ~0.75 floor."""
        assert wilson_lower_bound(19, 20) == pytest.approx(0.75, abs=0.02)

    def test_a_perfect_small_suite_still_admits_real_doubt(self) -> None:
        """Twenty of twenty is not proof of 1.0; the bound says how much doubt."""
        bound = wilson_lower_bound(20, 20)
        assert 0.80 < bound < 1.0

    def test_the_same_rate_measured_on_more_cases_bounds_higher(self) -> None:
        """The property that makes a larger replay suite worth investing in."""
        small = wilson_lower_bound(19, 20)
        large = wilson_lower_bound(190, 200)
        assert large > small

    def test_no_trials_is_no_evidence_rather_than_weak_evidence(self) -> None:
        assert wilson_lower_bound(0, 0) == 0.0

    def test_total_failure_bounds_at_zero(self) -> None:
        assert wilson_lower_bound(0, 20) == 0.0

    def test_the_bound_never_leaves_the_unit_interval(self) -> None:
        for trials in (1, 5, 20, 200):
            for successes in range(trials + 1):
                assert 0.0 <= wilson_lower_bound(successes, trials) <= 1.0

    def test_impossible_counts_are_refused_rather_than_clamped(self) -> None:
        """Clamping would turn a caller's bug into a plausible-looking number."""
        with pytest.raises(ValueError, match="successes must lie"):
            wilson_lower_bound(21, 20)


class TestClearsIncumbent:
    def test_the_case_the_rule_exists_for_does_not_promote(self) -> None:
        """0.95 over twenty cases against a 0.89 incumbent: the plan's example.

        On a point estimate this promotes and looks like a six-point gain. The
        bound says twenty cases cannot tell those two apart.
        """
        verdict = clears_incumbent(
            candidate_successes=19,
            candidate_trials=20,
            incumbent_successes=89,
            incumbent_trials=100,
        )
        assert verdict.promote is False
        assert verdict.candidate_rate > verdict.incumbent_rate
        assert "does not clear" in verdict.reason

    def test_the_same_candidate_rate_on_a_larger_suite_does_promote(self) -> None:
        """What buying a bigger replay suite actually buys."""
        verdict = clears_incumbent(
            candidate_successes=190,
            candidate_trials=200,
            incumbent_successes=89,
            incumbent_trials=100,
        )
        assert verdict.promote is True
        assert verdict.candidate_lower_bound > verdict.incumbent_rate

    def test_an_unmeasured_candidate_never_promotes(self) -> None:
        verdict = clears_incumbent(
            candidate_successes=0,
            candidate_trials=0,
            incumbent_successes=89,
            incumbent_trials=100,
        )
        assert verdict.promote is False
        assert "unmeasured" in verdict.reason

    def test_a_first_version_promotes_against_no_incumbent(self) -> None:
        """With nothing in place, any measured success beats the zero baseline."""
        verdict = clears_incumbent(
            candidate_successes=19,
            candidate_trials=20,
            incumbent_successes=0,
            incumbent_trials=0,
        )
        assert verdict.promote is True

    def test_a_candidate_measured_on_fewer_cases_cannot_win_by_being_uncertain(self) -> None:
        """Why the comparison is a bound against a rate, not bound against bound.

        Bound-against-bound would let a candidate beat a well-measured incumbent
        by having been measured on almost nothing, because a small suite gives
        the incumbent a lower floor to clear.
        """
        verdict = clears_incumbent(
            candidate_successes=3,
            candidate_trials=3,
            incumbent_successes=950,
            incumbent_trials=1000,
        )
        assert verdict.promote is False

    def test_the_reason_names_the_numbers_a_reviewer_would_ask_for(self) -> None:
        verdict = clears_incumbent(
            candidate_successes=19,
            candidate_trials=20,
            incumbent_successes=89,
            incumbent_trials=100,
        )
        assert "20 cases" in verdict.reason
        assert f"{verdict.candidate_lower_bound:.3f}" in verdict.reason
