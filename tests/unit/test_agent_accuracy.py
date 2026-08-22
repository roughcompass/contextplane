"""The accuracy arithmetic, without a database.

Everything here is about the split between what a verdict counts toward and what
it divides into, which is the only place this module can be subtly wrong in a
way that still looks like a number.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.exceptions import ValidationError
from contextplane.service.memory.agent_accuracy import (
    BREAKDOWN_CATEGORY,
    BREAKDOWN_OVERALL,
    Accuracy,
    AccuracyGroup,
    AgentAccuracyService,
)

_START = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
_END = datetime.datetime(2026, 8, 22, tzinfo=datetime.UTC)


def _group(label: str, *, correct: int, incorrect: int, undecidable: int = 0) -> AccuracyGroup:
    return AccuracyGroup(label=label, n_correct=correct, n_incorrect=incorrect, n_undecidable=undecidable)


def test_an_undecidable_verdict_counts_as_review_effort_and_not_as_an_answer() -> None:
    """The split this module exists to get right.

    Nine correct, one incorrect, ninety undecidable is a 90% accuracy rate over
    a small sample and a hundred reviews. Folding the undecidables into the
    denominator would report 9%; dropping them entirely would hide that ninety
    reviews happened and reached nothing, which is the more actionable fact of
    the two.
    """
    group = _group("overall", correct=9, incorrect=1, undecidable=90)

    assert group.rate == pytest.approx(0.9)
    assert group.n_decided == 10
    assert group.n_adjudicated == 100


def test_a_window_that_decided_nothing_has_no_rate_rather_than_a_rate_of_zero() -> None:
    """Zero is a specific claim, and the wrong one.

    An agent whose every verdict was undecidable has an *unknown* accuracy.
    Reporting 0.0 says it was wrong every time, which is the reading a caller
    would act on.
    """
    group = _group("overall", correct=0, incorrect=0, undecidable=7)

    assert group.rate is None
    assert group.n_adjudicated == 7


def test_being_wrong_every_time_is_a_rate_of_zero_and_not_an_absent_one() -> None:
    """The other side of the same distinction, so `None` cannot creep in as a
    synonym for bad."""
    group = _group("overall", correct=0, incorrect=4)

    assert group.rate == 0.0
    assert group.n_decided == 4


def test_the_header_is_summed_from_the_groups_rather_than_queried_separately() -> None:
    """Two statements over one window would eventually differ by a filter
    somebody added to one of them. Summing makes disagreement unrepresentable."""
    accuracy = Accuracy(
        author_actor_id=uuid.uuid4(),
        window_start=_START,
        window_end=_END,
        breakdown=BREAKDOWN_CATEGORY,
        groups=(
            _group("ownership_stewardship", correct=3, incorrect=1, undecidable=1),
            _group("operational_lifecycle", correct=1, incorrect=5),
        ),
    )

    overall = accuracy.overall
    assert overall.n_correct == 4
    assert overall.n_incorrect == 6
    assert overall.n_undecidable == 1
    assert overall.n_decided == 10
    assert overall.rate == pytest.approx(0.4)
    assert overall.label == BREAKDOWN_OVERALL


def test_a_header_over_no_groups_reports_nothing_rather_than_perfection() -> None:
    """An empty sum is zero correct out of zero decided, which must read as
    "unknown" and not as a clean sheet."""
    accuracy = Accuracy(
        author_actor_id=uuid.uuid4(),
        window_start=_START,
        window_end=_END,
        breakdown=BREAKDOWN_OVERALL,
        groups=(),
    )

    assert accuracy.overall.rate is None
    assert accuracy.overall.n_adjudicated == 0


@pytest.mark.asyncio
async def test_an_unknown_breakdown_is_refused_before_the_database_is_touched() -> None:
    """The breakdown names a column this module interpolates into SQL, so the
    closed set is a safety property rather than tidiness."""
    service = AgentAccuracyService(object())  # type: ignore[arg-type]  # never reached

    with pytest.raises(ValidationError, match="unknown breakdown"):
        await service.accuracy_for(
            object(),  # type: ignore[arg-type]  # never reached
            author_actor_id=uuid.uuid4(),
            window_start=_START,
            window_end=_END,
            breakdown="author_actor_id",
        )


@pytest.mark.asyncio
async def test_a_backwards_window_is_refused() -> None:
    service = AgentAccuracyService(object())  # type: ignore[arg-type]  # never reached

    with pytest.raises(ValidationError, match="window_end must be after"):
        await service.accuracy_for(
            object(),  # type: ignore[arg-type]  # never reached
            author_actor_id=uuid.uuid4(),
            window_start=_END,
            window_end=_START,
        )
