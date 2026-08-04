"""Fitting a provider's self-reports, and refusing to publish a fit that misses.

The most important behaviour here is the cold start. This deployment has zero judged
outcomes, so the honest state is "no mapping", not "an identity mapping" — identity
would assert that a model reporting 0.9 is right nine times in ten, which nobody has
checked. Several tests exist only to hold that line.

The second is that a fit missing the accuracy target is stored and never selected.
A mapping worse than the bound is worse than no mapping, because it carries a version
string that reads as calibrated.
"""

from __future__ import annotations

import pytest

from registry.service.calibration import (
    CALIBRATION_BIN_COUNT,
    MAX_CALIBRATION_ERROR,
    MIN_ADJUDICATED_FOR_MAPPING,
    PRIOR_STRENGTH,
    STATUS_ACTIVE,
    STATUS_FAILED,
    UNCALIBRATED,
    Adjudication,
    calibration_error,
    fit,
    mapping_version,
)


def _well_calibrated(n: int = 400) -> list[Adjudication]:
    """A provider whose numbers mean what they say: a claim reported at 0.8 is
    correct four times in five."""
    out: list[Adjudication] = []
    for i in range(n):
        raw = (i % 10) / 10 + 0.05
        # Deterministic rather than random, so a failure is reproducible.
        out.append(Adjudication(provider_confidence=raw, was_correct=(i % 100) < raw * 100))
    return out


def _overconfident_provider(n: int = 400) -> list[Adjudication]:
    """A provider that reports 0.95 and is right one time in ten.

    Note what this is *not*: a miscalibrated mapping. A fit over this data learns
    that this provider's 0.95 means 0.1, and is then self-consistent -- correcting
    a provider's scale is the mapping's entire job. Testing the fit against its own
    training data can only ever measure whether the arithmetic is consistent.
    """
    return [Adjudication(provider_confidence=0.95, was_correct=(i % 10) == 0) for i in range(n)]


def _deliberately_wrong_bins() -> tuple[float, ...]:
    """A mapping asserting near-certainty for every raw score.

    This is what the requirement means by a miscalibrated mapping: not a provider
    whose scale is skewed, but a *mapping* whose predictions do not match observed
    outcomes. Such a mapping is worse than none at all, because it carries a version
    string that reads as calibrated.
    """
    return tuple(0.95 for _ in range(CALIBRATION_BIN_COUNT))


# --- the cold start -----------------------------------------------------------


def test_the_uncalibrated_token_cannot_be_mistaken_for_a_version() -> None:
    """Every real version is colon-delimited, so a claim carrying this token can
    never resolve to a mapping by accident."""
    assert ":" not in UNCALIBRATED
    real = mapping_version(provider_id="anthropic", model_id="m", strategy_id="s", fit_date="2026-11-04", n=243)
    assert real != UNCALIBRATED
    assert ":" in real


def test_no_observations_is_not_a_passing_fit() -> None:
    """An empty set must never be publishable. Reporting a perfect error on no
    evidence is exactly how an unexamined mapping becomes active."""
    empty = fit([])
    assert empty.n_adjudicated == 0
    assert not empty.meets_target
    assert empty.status == STATUS_FAILED


def test_the_version_names_everything_that_would_invalidate_the_fit() -> None:
    """Provider and model are in the key so that changing either matches no row and
    scoring reverts to uncalibrated with nobody having to remember to act. That is
    what makes recalibration a mechanism rather than a procedure."""
    first = mapping_version(provider_id="anthropic", model_id="haiku-4-5", strategy_id="obs", fit_date="d", n=250)
    swapped_model = mapping_version(
        provider_id="anthropic", model_id="sonnet-5", strategy_id="obs", fit_date="d", n=250
    )
    swapped_strategy = mapping_version(
        provider_id="anthropic", model_id="haiku-4-5", strategy_id="pref", fit_date="d", n=250
    )
    assert len({first, swapped_model, swapped_strategy}) == 3


def test_the_version_records_how_much_evidence_stood_behind_it() -> None:
    """So a claim's record shows the sample size without a join."""
    assert "250" in mapping_version(provider_id="p", model_id="m", strategy_id="s", fit_date="d", n=250)


# --- fitting ------------------------------------------------------------------


def test_a_well_calibrated_provider_meets_the_target() -> None:
    fitted = fit(_well_calibrated())
    assert fitted.measured_error <= MAX_CALIBRATION_ERROR
    assert fitted.status == STATUS_ACTIVE


def test_a_deliberately_miscalibrated_mapping_fails_the_check() -> None:
    """The sixth exit criterion. A mapping predicting near-certainty against
    outcomes that are mostly wrong must be measured as failing."""
    observations = _overconfident_provider()
    error = calibration_error(_deliberately_wrong_bins(), observations)
    assert error > MAX_CALIBRATION_ERROR


def test_a_fit_corrects_a_skewed_provider_rather_than_failing_on_it() -> None:
    """Correcting a provider's scale is the mapping's whole purpose, so a fit over
    consistent data is self-consistent however skewed the raw numbers were. Worth
    asserting because the opposite reading -- that a skewed provider should fail the
    check -- is the intuitive one and it is wrong.
    """
    fitted = fit(_overconfident_provider())
    assert fitted.meets_target
    # It learned what 0.95 is actually worth from this provider.
    assert fitted.apply(0.95) < 0.2


def test_a_fit_that_misses_the_target_is_marked_failed_not_active() -> None:
    """A mapping worse than the bound is never selected for scoring."""
    from dataclasses import replace

    honest = fit(_well_calibrated())
    hopeless = replace(honest, measured_error=0.40)
    assert hopeless.status == STATUS_FAILED
    assert not hopeless.meets_target


def test_a_fit_produces_one_value_per_bin() -> None:
    fitted = fit(_well_calibrated())
    assert len(fitted.bins) == CALIBRATION_BIN_COUNT
    assert all(0.0 <= b <= 1.0 for b in fitted.bins)


def test_a_thin_bin_is_pulled_toward_the_pooled_rate() -> None:
    """What stops four observations from setting a number. The smoothing is why a
    bin with almost no evidence does not swing the mapping."""
    mostly_wrong = [Adjudication(provider_confidence=0.05, was_correct=False) for _ in range(200)]
    one_lucky_bin = [Adjudication(provider_confidence=0.95, was_correct=True) for _ in range(2)]
    fitted = fit(mostly_wrong + one_lucky_bin)

    # Two correct observations in the top bin must not make it read as certain.
    assert fitted.bins[9] < 0.5


def test_a_bin_with_enough_evidence_is_trusted() -> None:
    """The other side of smoothing: with plenty of observations the bin's own rate
    dominates, or the prior would permanently flatten every real signal."""
    plenty = [Adjudication(provider_confidence=0.95, was_correct=True) for _ in range(int(PRIOR_STRENGTH) * 20)]
    fitted = fit(plenty)
    assert fitted.bins[9] > 0.9


def test_the_error_is_weighted_by_how_many_outcomes_landed_in_each_bin() -> None:
    """A bin holding two outcomes must not dominate one holding two hundred."""
    bins = tuple(0.5 for _ in range(CALIBRATION_BIN_COUNT))
    lopsided = [Adjudication(provider_confidence=0.05, was_correct=False) for _ in range(200)]
    lopsided += [Adjudication(provider_confidence=0.95, was_correct=True) for _ in range(2)]
    error = calibration_error(bins, lopsided)
    # Dominated by the large bin, whose true rate is 0.0 against a predicted 0.5.
    assert error == pytest.approx(0.5, abs=0.02)


def test_the_error_of_an_empty_set_is_the_worst_possible() -> None:
    """So nothing can be published on no evidence."""
    assert calibration_error(tuple(0.5 for _ in range(CALIBRATION_BIN_COUNT)), []) == 1.0


def test_applying_a_mapping_returns_its_bin_value() -> None:
    """The audit record is the sentence: of the judged claims whose raw score landed
    here, this fraction were right."""
    fitted = fit(_well_calibrated())
    assert fitted.apply(0.05) == fitted.bins[0]
    assert fitted.apply(0.95) == fitted.bins[9]


def test_a_raw_score_outside_the_range_is_clamped_rather_than_raising() -> None:
    """A provider reporting something out of range is a provider bug, and a scoring
    path that raised on it would take extraction down with it."""
    fitted = fit(_well_calibrated())
    assert fitted.apply(-1.0) == fitted.bins[0]
    assert fitted.apply(2.0) == fitted.bins[9]


def test_the_minimum_sample_matches_the_evaluation_set_the_target_is_defined_over() -> None:
    """A fit on less could not be checked against the target even in principle."""
    assert MIN_ADJUDICATED_FOR_MAPPING == 200


def test_the_bin_count_is_coarse_enough_to_hold_real_evidence() -> None:
    """At the minimum sample, ten bins average twenty observations each — already
    thin. Finer bins would fit noise."""
    assert MIN_ADJUDICATED_FOR_MAPPING / CALIBRATION_BIN_COUNT >= PRIOR_STRENGTH
