"""Unit tests for `contextplane.service.memory.confidence_read`.

Three of the four modules D1's confidence track names already have dedicated
unit suites (`test_confidence.py`, `test_confidence_decay.py`,
`test_calibration.py`); `confidence_read.py` is the fourth and the one this
file exists to close.

**Premise check against the code, not against this suite's own plan.**
`confidence_read.py` has exactly two public functions and no error paths of
its own: `serve()` is a pure function over already-fetched values (there is
no "missing confidence record" branch to hit -- a caller who has no stored
confidence never calls `serve()` at all, it is not a lookup) and no
"malformed stored input" validation (bad `stored`/`half_life_days` values
flow straight into `confidence_decay.effective_confidence`'s own arithmetic,
which is that module's tested surface, not this one's). What *is* real and
was genuinely uncovered before this file: `subject_change_profile`'s own
branches (too-few-observations, all-same-instant, and the real median-gap
case) had zero unit coverage -- unit-scope `--cov` showed its entire body as
the only missing lines in the module, all incidentally reached only via
`ClaimService`'s own suite importing the module, never via a call into
`subject_change_profile` itself.

Coverage:
- `serve`: zero-age returns the stored value rounded to three places;
  decay lowers the served value as age grows; the served bucket comes from
  the *effective* (decayed) value, not the stored one, across a boundary
  that would land in a different bucket than the stored value's own; a
  future `hold_until` reports `is_held` and, because ageing from a
  future origin clamps at zero, always reports `age_days == 0.0`; a past
  (elapsed) `hold_until` is not held but still anchors `age_days` at the
  hold point rather than the original `scored_at`; `value_type` is forwarded
  to `effective_confidence` (a non-decaying type stays at the stored value
  even after significant age, where the same age with no type decays).
- `subject_change_profile`: too few observations and all-same-instant both
  report `(None, 0)` rather than inventing a volatility number; the real
  case computes the median of the *positive* gaps and returns how many fed
  it; the query is built with the entity id, the `now - window_days` cutoff
  (default and a caller-supplied override), and the fixed sample cap.
"""

from __future__ import annotations

import dataclasses
import datetime
import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.service.memory.confidence import (
    BUCKET_CONFIRMED,
    BUCKET_MODERATE,
    bucket_for,
)
from contextplane.service.memory.confidence_decay import MIN_CHANGE_OBSERVATIONS
from contextplane.service.memory.confidence_read import (
    _MAX_CHANGE_SAMPLES,
    VOLATILITY_WINDOW_DAYS,
    ServedConfidence,
    serve,
    subject_change_profile,
)

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# serve()
# ---------------------------------------------------------------------------


def test_serve_at_zero_age_returns_the_stored_value_rounded_to_three_places() -> None:
    served = serve(stored=0.123456, scored_at=_NOW, half_life_days=30.0, now=_NOW)

    assert served.effective == 0.123
    assert served.stored == 0.123456
    assert served.age_days == 0.0
    assert served.is_held is False


def test_serve_lowers_the_effective_value_as_the_claim_ages_by_exactly_one_half_life() -> None:
    scored_at = _NOW - datetime.timedelta(days=30)
    served = serve(stored=0.90, scored_at=scored_at, half_life_days=30.0, now=_NOW)

    # DECAY_FLOOR=0.10, one half-life elapsed: 0.10 + (0.90-0.10)*0.5 = 0.50
    assert served.effective == 0.50
    assert served.age_days == pytest.approx(30.0)


def test_serve_derives_the_bucket_from_the_decayed_value_not_the_stored_one() -> None:
    stored = 0.90
    scored_at = _NOW - datetime.timedelta(days=5)
    served = serve(stored=stored, scored_at=scored_at, half_life_days=10.0, now=_NOW)

    assert bucket_for(stored) == BUCKET_CONFIRMED
    assert served.effective < stored
    assert served.bucket == BUCKET_MODERATE
    assert served.bucket == bucket_for(served.effective)


def test_serve_with_a_future_hold_until_is_held_and_reports_zero_age() -> None:
    """Ageing from a future origin clamps at zero -- a held claim always
    reports `age_days == 0.0`, never a negative number."""
    scored_at = _NOW - datetime.timedelta(days=400)
    hold_until = _NOW + datetime.timedelta(days=1)
    served = serve(stored=0.9, scored_at=scored_at, half_life_days=10.0, now=_NOW, hold_until=hold_until)

    assert served.is_held is True
    assert served.age_days == 0.0
    assert served.effective == 0.9  # `effective_confidence` returns `stored` unchanged while held


def test_serve_with_an_elapsed_hold_until_is_not_held_but_still_anchors_age_on_it() -> None:
    """Once a hold has elapsed the claim decays again, but the age is
    measured from the hold point, not from the original `scored_at` --
    otherwise a confirmation would be worthless the day its window closed."""
    scored_at = _NOW - datetime.timedelta(days=1000)
    hold_until = _NOW - datetime.timedelta(days=10)
    served = serve(stored=0.9, scored_at=scored_at, half_life_days=30.0, now=_NOW, hold_until=hold_until)

    assert served.is_held is False
    assert served.age_days == pytest.approx(10.0)


def test_serve_forwards_value_type_so_a_non_decaying_type_never_ages_down() -> None:
    scored_at = _NOW - datetime.timedelta(days=365)

    decaying = serve(stored=0.9, scored_at=scored_at, half_life_days=30.0, now=_NOW, value_type=None)
    non_decaying = serve(stored=0.9, scored_at=scored_at, half_life_days=30.0, now=_NOW, value_type="prose")

    assert decaying.effective < 0.9
    assert non_decaying.effective == 0.9


def test_serve_returns_a_frozen_served_confidence() -> None:
    served = serve(stored=0.5, scored_at=_NOW, half_life_days=30.0, now=_NOW)

    assert isinstance(served, ServedConfidence)
    with pytest.raises(dataclasses.FrozenInstanceError):
        served.effective = 0.99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# subject_change_profile()
# ---------------------------------------------------------------------------


def _rows(instants: list[datetime.datetime]) -> MagicMock:
    result = MagicMock()
    result.all = MagicMock(return_value=[types.SimpleNamespace(t_valid_from=t) for t in instants])
    return result


def _session(rows_result: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=rows_result)
    return session


@pytest.mark.asyncio
async def test_subject_change_profile_reports_no_volatility_below_the_minimum_observation_count() -> None:
    assert MIN_CHANGE_OBSERVATIONS == 3
    instants = [_NOW - datetime.timedelta(days=d) for d in (10, 5)]  # only 2, one short of the minimum
    session = _session(_rows(instants))

    median, count = await subject_change_profile(session, entity_id=uuid.uuid4(), now=_NOW)

    assert (median, count) == (None, 0)


@pytest.mark.asyncio
async def test_subject_change_profile_reports_no_volatility_when_every_change_lands_at_one_instant() -> None:
    same_instant = _NOW - datetime.timedelta(days=1)
    session = _session(_rows([same_instant, same_instant, same_instant]))

    median, count = await subject_change_profile(session, entity_id=uuid.uuid4(), now=_NOW)

    assert (median, count) == (None, 0)


@pytest.mark.asyncio
async def test_subject_change_profile_returns_the_median_of_the_positive_gaps_and_how_many_fed_it() -> None:
    base = _NOW - datetime.timedelta(days=10)
    instants = [
        base,
        base + datetime.timedelta(days=1),
        base + datetime.timedelta(days=3),
        base + datetime.timedelta(days=6),
    ]
    session = _session(_rows(instants))

    median, count = await subject_change_profile(session, entity_id=uuid.uuid4(), now=_NOW)

    # gaps: 1, 2, 3 -> median 2.0, all three positive
    assert median == pytest.approx(2.0)
    assert count == 3


@pytest.mark.asyncio
async def test_subject_change_profile_binds_entity_window_and_the_fixed_sample_cap() -> None:
    entity_id = uuid.uuid4()
    session = _session(_rows([]))

    await subject_change_profile(session, entity_id=entity_id, now=_NOW)

    args, _kwargs = session.execute.call_args
    params = args[1]
    assert params["eid"] == entity_id
    assert params["since"] == _NOW - datetime.timedelta(days=VOLATILITY_WINDOW_DAYS)
    assert params["lim"] == _MAX_CHANGE_SAMPLES


@pytest.mark.asyncio
async def test_subject_change_profile_honours_a_caller_supplied_window() -> None:
    entity_id = uuid.uuid4()
    session = _session(_rows([]))

    await subject_change_profile(session, entity_id=entity_id, now=_NOW, window_days=30)

    args, _ = session.execute.call_args
    params = args[1]
    assert params["since"] == _NOW - datetime.timedelta(days=30)
