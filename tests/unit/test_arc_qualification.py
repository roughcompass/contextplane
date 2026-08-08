"""Unit tests for `contextplane/arc/service/qualification.py`: no database.

`_requires_observation`/`_sufficiency` are pure and checked against the
full closed classification vocabulary. `close_due_cohort` is exercised
against a scripted `unittest.mock.AsyncMock` standing in for `queries/
observation.py` -- proving the closing-boundary decision logic itself
(never earlier than the correct boundary, exactly at either boundary,
idempotent once closed) without a real transaction. The non-vacuous proof
that this boundary is enforced against a real clock, through the real
scheduler, one second before and exactly at the deadline, is `tests/
integration/test_arc_observation.py`'s job -- matching the same precedent
`tests/integration/test_arc_source_status.py` already set for
`SourceStatusService`.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from contextplane.arc.schemas.authoring_profile_shapes import RISK_CLASSIFICATIONS
from contextplane.arc.service.qualification import (
    MAX_LIVE_WINDOW,
    _requires_observation,
    _sufficiency,
    close_due_cohort,
)
from contextplane.arc.service.queries.observation import CohortRow, ResultCounters

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# _requires_observation / _sufficiency: the ADR 041 Sec.1/Sec.5 tables.
# ---------------------------------------------------------------------------


def test_risk_classifications_vocabulary_has_exactly_ten_members() -> None:
    """Sanity check that the fixture below stays aligned with the closed
    vocabulary this module classifies against -- not a restatement of the
    vocabulary-parity conformance test."""
    assert len(RISK_CLASSIFICATIONS) == 10


@pytest.mark.parametrize(
    "classification,required",
    [
        ("global_mandatory", True),
        ("global_non_mandatory", True),
        ("tenant_mandatory", True),
        ("domain_mandatory", True),
        ("capability_mandatory", True),
        ("task_mandatory", True),
        ("tenant_non_mandatory", False),
        ("domain_non_mandatory", False),
        ("capability_non_mandatory", False),
        ("task_non_mandatory", False),
    ],
)
def test_requires_observation_matches_adr_041_sec_1_for_every_classification(
    classification: str, required: bool
) -> None:
    """Required when any rule is global OR any rule is mandatory -- every
    one of the ten closed literals, not a representative sample."""
    assert classification in RISK_CLASSIFICATIONS
    assert _requires_observation(classification) is required


def test_sufficiency_is_24h_100_for_non_global_mandatory() -> None:
    window, count = _sufficiency("tenant_mandatory")
    assert window == datetime.timedelta(hours=24)
    assert count == 100


def test_sufficiency_is_72h_1000_for_global_non_mandatory() -> None:
    window, count = _sufficiency("global_non_mandatory")
    assert window == datetime.timedelta(hours=72)
    assert count == 1000


def test_sufficiency_is_72h_1000_for_global_mandatory() -> None:
    window, count = _sufficiency("global_mandatory")
    assert window == datetime.timedelta(hours=72)
    assert count == 1000


# ---------------------------------------------------------------------------
# close_due_cohort: the closing-boundary decision, against a scripted fake.
# ---------------------------------------------------------------------------


def _cohort(
    *,
    risk_classification: str = "tenant_mandatory",
    window_started_at: datetime.datetime = _NOW,
    window_deadline: datetime.datetime | None = None,
    closed_at: datetime.datetime | None = None,
) -> CohortRow:
    return CohortRow(
        cohort_id=uuid.uuid4(),
        proposal_id=uuid.uuid4(),
        proposal_version=1,
        candidate_revision_id=uuid.uuid4(),
        risk_classification=risk_classification,
        scope_predicate_digest="a" * 64,
        tenant_membership_digest="b" * 64,
        eligibility_predicate_digest="c" * 64,
        frozen_at=window_started_at,
        window_started_at=window_started_at,
        window_deadline=window_deadline or (window_started_at + datetime.timedelta(hours=24)),
        window_ended_at=None,
        closed_at=closed_at,
    )


def _counters(*, eligible: int, observed: int) -> ResultCounters:
    return ResultCounters(
        eligible_count=eligible,
        observed_count=observed,
        unexplained_count=0,
        out_of_envelope_count=0,
        counters_by_delta_code={},
    )


@pytest.mark.asyncio
async def test_close_due_cohort_stays_open_before_the_deadline_even_if_already_sufficient() -> None:
    """100% coverage of the *declared* window matters -- reaching the
    minimum count early must not close the window before its planned
    deadline, or the remainder of the window goes unobserved while still
    being claimed as "coverage complete"."""
    cohort = _cohort(window_deadline=_NOW + datetime.timedelta(hours=24))
    one_second_before_deadline = cohort.window_deadline - datetime.timedelta(seconds=1)
    with patch("contextplane.arc.service.qualification.obs_queries") as mocked:
        mocked.load_aggregate_counters = AsyncMock(return_value=_counters(eligible=100, observed=100))
        result = await close_due_cohort(AsyncMock(), cohort, now=one_second_before_deadline)
        mocked.close_cohort.assert_not_called()
    assert result.closed_at is None


@pytest.mark.asyncio
async def test_close_due_cohort_closes_exactly_at_the_deadline_when_sufficient() -> None:
    cohort = _cohort(window_deadline=_NOW + datetime.timedelta(hours=24))
    with patch("contextplane.arc.service.qualification.obs_queries") as mocked:
        mocked.load_aggregate_counters = AsyncMock(return_value=_counters(eligible=100, observed=100))
        mocked.close_cohort = AsyncMock(return_value=True)
        mocked.load_cohort = AsyncMock(
            return_value=_cohort(window_deadline=cohort.window_deadline, closed_at=cohort.window_deadline)
        )
        result = await close_due_cohort(AsyncMock(), cohort, now=cohort.window_deadline)
        mocked.close_cohort.assert_awaited_once()
        _, kwargs = mocked.close_cohort.call_args
        assert kwargs["window_ended_at"] == cohort.window_deadline
        assert kwargs["closed_at"] == cohort.window_deadline
    assert result.closed_at is not None


@pytest.mark.asyncio
async def test_close_due_cohort_stays_open_past_the_deadline_when_insufficient() -> None:
    """Insufficient at the planned deadline does not close the cohort --
    the window extends implicitly toward the seven-day cap, per ADR 041
    Sec.5's own fallback ordering."""
    cohort = _cohort(window_deadline=_NOW + datetime.timedelta(hours=24))
    just_past_deadline = cohort.window_deadline + datetime.timedelta(hours=1)
    with patch("contextplane.arc.service.qualification.obs_queries") as mocked:
        mocked.load_aggregate_counters = AsyncMock(return_value=_counters(eligible=100, observed=1))
        result = await close_due_cohort(AsyncMock(), cohort, now=just_past_deadline)
        mocked.close_cohort.assert_not_called()
    assert result.closed_at is None


@pytest.mark.asyncio
async def test_close_due_cohort_stays_open_one_second_before_the_seven_day_cap() -> None:
    cohort = _cohort(window_started_at=_NOW, window_deadline=_NOW + datetime.timedelta(hours=24))
    one_second_before_cap = _NOW + MAX_LIVE_WINDOW - datetime.timedelta(seconds=1)
    with patch("contextplane.arc.service.qualification.obs_queries") as mocked:
        mocked.load_aggregate_counters = AsyncMock(return_value=_counters(eligible=100, observed=1))
        result = await close_due_cohort(AsyncMock(), cohort, now=one_second_before_cap)
        mocked.close_cohort.assert_not_called()
    assert result.closed_at is None


@pytest.mark.asyncio
async def test_close_due_cohort_closes_exactly_at_the_seven_day_cap_when_still_insufficient() -> None:
    cohort = _cohort(window_started_at=_NOW, window_deadline=_NOW + datetime.timedelta(hours=24))
    exactly_at_cap = _NOW + MAX_LIVE_WINDOW
    with patch("contextplane.arc.service.qualification.obs_queries") as mocked:
        mocked.load_aggregate_counters = AsyncMock(return_value=_counters(eligible=100, observed=1))
        mocked.close_cohort = AsyncMock(return_value=True)
        mocked.load_cohort = AsyncMock(return_value=_cohort(closed_at=exactly_at_cap))
        result = await close_due_cohort(AsyncMock(), cohort, now=exactly_at_cap)
        mocked.close_cohort.assert_awaited_once()
        _, kwargs = mocked.close_cohort.call_args
        assert kwargs["window_ended_at"] == exactly_at_cap
    assert result.closed_at is not None


@pytest.mark.asyncio
async def test_close_due_cohort_is_a_no_op_once_already_closed() -> None:
    cohort = _cohort(closed_at=_NOW)
    with patch("contextplane.arc.service.qualification.obs_queries") as mocked:
        result = await close_due_cohort(AsyncMock(), cohort, now=_NOW + datetime.timedelta(days=30))
        mocked.load_aggregate_counters.assert_not_called()
        mocked.close_cohort.assert_not_called()
    assert result is cohort
