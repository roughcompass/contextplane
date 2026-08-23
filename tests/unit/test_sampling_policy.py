"""The review budget's arithmetic, and what it does not promise. E5-T2.

`minimum_sample` is the whole reason this magnitude is `derived` rather than
`validated` or `grandfathered`: it follows by arithmetic from two stated
inputs, so anybody with a calculator can check it. These tests are that
calculator, written independently of the implementation — the expected values
below come from the closed form, not from running the function and recording
what it said.
"""

from __future__ import annotations

import math

import pytest

from contextplane import ranking
from contextplane.exceptions import ValidationError
from contextplane.service.memory.sampling_policy import (
    _UNCONFIGURED_RISK,
    _UNCONFIGURED_SAMPLE,
    _UNCONFIGURED_TOLERANCE,
    SamplingPolicy,
    minimum_sample,
)


@pytest.mark.parametrize(
    ("tolerance", "risk", "expected"),
    [
        # The worked example the module docstring and the migration both cite.
        (0.05, 0.10, 45),
        (0.10, 0.10, 22),
        (0.02, 0.10, 114),
        # A tighter consumer's risk costs more samples at the same tolerance.
        (0.05, 0.01, 90),
    ],
)
def test_the_sample_is_the_smallest_zero_acceptance_plan(tolerance: float, risk: float, expected: int) -> None:
    """`(1 - p)**n <= beta`, solved for the smallest whole n.

    The expected values are computed from the closed form rather than copied
    from the implementation, so a change to the formula fails here instead of
    being ratified by a test that recorded whatever it produced.
    """
    assert minimum_sample(tolerance, risk) == expected
    assert (1 - tolerance) ** expected <= risk
    assert (1 - tolerance) ** (expected - 1) > risk, "one fewer draw must not have sufficed"


def test_a_tighter_tolerance_never_costs_fewer_samples() -> None:
    """Monotonicity, which is the property an operator reasons with: demanding
    a lower defect rate cannot make the budget cheaper."""
    samples = [minimum_sample(tolerance, 0.10) for tolerance in (0.20, 0.10, 0.05, 0.02, 0.01)]
    assert samples == sorted(samples), samples


@pytest.mark.parametrize("tolerance", [0.0, 1.0, -0.1, 1.5])
def test_a_tolerance_outside_the_open_interval_is_refused(tolerance: float) -> None:
    """Not clamped. A tolerance of 0 demands an infinite sample and 1 accepts
    anything; substituting a workable number for either would put a figure in
    the registry that no stated pair produces."""
    with pytest.raises(ValidationError, match="defect_tolerance"):
        minimum_sample(tolerance, 0.10)


@pytest.mark.parametrize("risk", [0.0, 1.0, -0.5])
def test_a_consumers_risk_outside_the_open_interval_is_refused(risk: float) -> None:
    with pytest.raises(ValidationError, match="consumers_risk"):
        minimum_sample(0.05, risk)


def test_a_policy_whose_stored_sample_no_longer_follows_reports_itself() -> None:
    """The check every load runs. A row whose three numbers disagree was edited
    in the database, and it would otherwise serve as though it were derived."""
    honest = SamplingPolicy(
        claim_category="dependency",
        defect_tolerance=0.05,
        consumers_risk=0.10,
        min_sample=45,
        reason="Dependency claims are cheap to verify and expensive to get wrong.",
    )
    assert honest.recomputes()

    edited = SamplingPolicy(
        claim_category="dependency",
        defect_tolerance=0.05,
        consumers_risk=0.10,
        min_sample=5,
        reason="Dependency claims are cheap to verify and expensive to get wrong.",
    )
    assert not edited.recomputes()


def test_the_derivation_matches_the_one_the_registry_records() -> None:
    """The registry entry states `n >= ln(beta) / ln(1 - p)`. If this module
    ever computed something else, the recorded derivation would be a
    description of code that no longer exists — which is exactly what `derived`
    status is supposed to make impossible."""
    for tolerance, risk in ((0.05, 0.10), (0.03, 0.05), (0.15, 0.20)):
        assert minimum_sample(tolerance, risk) == math.ceil(math.log(risk) / math.log(1 - tolerance))


def test_the_governed_floor_still_follows_from_its_recorded_derivation() -> None:
    """The registry holds 299 and records the pair it came from. If the two ever
    part company, the recorded derivation describes a number the entry no longer
    carries — and a derivation nobody can reproduce is the one thing `derived`
    status must not permit."""
    assert _UNCONFIGURED_SAMPLE == minimum_sample(_UNCONFIGURED_TOLERANCE, _UNCONFIGURED_RISK)
    assert _UNCONFIGURED_SAMPLE == 299


def test_the_floor_is_governed_as_derived_and_gates_on_it() -> None:
    """Not `grandfathered`. The whole reason E5-T1b existed is that this entry
    could not be recorded honestly under the two statuses the registry had, and
    `requires_validated` is set so the loader refuses to serve it on a status
    that says nobody checked."""
    model_id = "review-sampling-unconfigured-floor@1"
    assert ranking.validation_status(model_id) == "derived"
    assert ranking.requires_validated(model_id) is True
