"""A lot inspected fewer times than its plan requires cannot be accepted.

E5-T2b. E5-T2 derived `min_sample` from a stated defect tolerance and consumer's
risk, and wrote down what the arithmetic assumes. E5-T4 built
`inspected_dispositions` -- *"the number acceptance sampling is entitled to
use"* -- and wrote down why automated disposals are excluded from it. **Nothing
compared them.** A tenant could set a budget, review a tenth of it, and nothing
anywhere said so.

Filed against E5-T2 rather than defined by E12-T3, on that entry's own
instruction: *"a halt defined by its consumer is the second regime this task
refuses."*

Pure, so the halt is testable without a database and cannot come to depend on
how a caller happened to obtain either number.
"""

from __future__ import annotations

import pytest

from contextplane.service.memory.sampling_policy import (
    SampleTooSmall,
    SamplingPolicy,
    acceptance_state,
    minimum_sample,
    require_minimum_sample,
)


def _policy(*, min_sample: int, category: str = "ownership_stewardship") -> SamplingPolicy:
    return SamplingPolicy(
        claim_category=category,
        consumers_risk=0.10,
        defect_tolerance=0.05,
        min_sample=min_sample,
        reason="the tenant's stated tolerance for this category",
    )


def test_a_lot_at_its_floor_is_accepted() -> None:
    """Exactly the floor is enough. The plan's arithmetic is `n >=`, and a halt
    that demanded one more would be a second, stricter plan nobody derived."""
    state = acceptance_state(_policy(min_sample=45), inspected=45)

    assert state.met
    assert state.shortfall == 0
    require_minimum_sample(state)


def test_a_lot_short_of_its_floor_is_refused() -> None:
    """The whole of this task in one assertion. Before it, this never happened."""
    state = acceptance_state(_policy(min_sample=45), inspected=44)

    assert not state.met
    with pytest.raises(SampleTooSmall, match="44 of a required 45"):
        require_minimum_sample(state)


def test_the_refusal_says_how_far_short_it_is() -> None:
    """ "Not yet" and "not nearly" are different operational situations, and a
    caller told only that it may not proceed cannot tell an operator which."""
    state = acceptance_state(_policy(min_sample=45), inspected=3)

    assert state.shortfall == 42
    with pytest.raises(SampleTooSmall, match="42 more must be inspected by a person"):
        require_minimum_sample(state)


def test_the_refusal_says_what_a_short_sample_costs() -> None:
    """Proceeding on a short sample does not weaken the guarantee -- it removes
    it, while leaving a number that still looks like one. The message says so,
    because the reader of this error is deciding whether to override it."""
    with pytest.raises(SampleTooSmall, match="says nothing about a lot inspected fewer times"):
        require_minimum_sample(acceptance_state(_policy(min_sample=45), inspected=0))


def test_a_lot_beyond_its_floor_is_accepted_and_reports_no_shortfall() -> None:
    state = acceptance_state(_policy(min_sample=45), inspected=200)

    assert state.met
    assert state.shortfall == 0


def test_the_floor_the_halt_reads_is_the_one_the_policy_derived() -> None:
    """The halt compares against the *derived* floor, not a number a caller
    passed alongside it. A policy whose stored sample no longer follows from its
    inputs is caught on load by `recomputes`; this asserts the halt is reading
    the same field that check protects."""
    policy = _policy(min_sample=minimum_sample(0.05, 0.10))

    assert policy.recomputes()
    assert acceptance_state(policy, inspected=0).min_sample == policy.min_sample


def test_the_halt_carries_the_category_it_refused_for() -> None:
    """A tenant has one policy per category, so a refusal that did not name the
    category would send an operator to review the wrong queue."""
    state = acceptance_state(_policy(category="operational_runbook", min_sample=45), inspected=1)

    assert state.claim_category == "operational_runbook"
    with pytest.raises(SampleTooSmall, match="operational_runbook"):
        require_minimum_sample(state)


@pytest.mark.parametrize("inspected", [-1, -100])
def test_a_negative_count_still_refuses_rather_than_wrapping(inspected: int) -> None:
    """No caller should produce one, and if one does the answer is still "not
    enough" rather than a shortfall computed to be larger than the floor."""
    state = acceptance_state(_policy(min_sample=45), inspected=inspected)

    assert not state.met
    assert state.shortfall >= 45
