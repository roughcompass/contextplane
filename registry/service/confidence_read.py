"""Serving a confidence score: the stored value, aged to now.

The stored number is immutable between writes. What a caller sees is that number
adjusted for how long the assertion has been sitting there, computed from four
values already on the row plus the clock. That keeps attribution complete --
anyone can reproduce the served value at any past instant -- while a periodic
rewrite job would destroy exactly that, because after it ran the number actually
served would be gone.

**A minimum-confidence filter pushes into SQL, and the prefilter is sound.** Ageing
can only lower a score, because the stored value never sits below the floor it
decays toward. So a query can narrow on the indexed stored column first and then
apply the exact adjustment: no claim stored below a threshold can age up through
it. Without that guarantee the filter would need a sequential scan.
"""

from __future__ import annotations

import dataclasses
import datetime
import statistics
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.service.confidence import bucket_for
from registry.service.confidence_decay import (
    MIN_CHANGE_OBSERVATIONS,
    effective_confidence,
)

# How far back to look when judging how fast a subject changes. A year covers an
# annual reorganisation and several release trains, and stops a long-dead entity's
# ancient churn from making it look volatile forever.
VOLATILITY_WINDOW_DAYS = 365

# Enough history to compute a median without one outlier deciding it.
_MAX_CHANGE_SAMPLES = 200


@dataclasses.dataclass(frozen=True)
class ServedConfidence:
    """What a caller sees, and enough to explain it."""

    stored: float
    effective: float
    bucket: str
    scored_at: datetime.datetime
    half_life_days: float
    age_days: float
    is_held: bool


def serve(
    *,
    stored: float,
    scored_at: datetime.datetime,
    half_life_days: float,
    now: datetime.datetime,
    hold_until: datetime.datetime | None = None,
    value_type: str | None = None,
) -> ServedConfidence:
    """The stored score as of `now`, with the arithmetic that produced it."""
    effective = effective_confidence(
        stored,
        scored_at=scored_at,
        half_life=half_life_days,
        now=now,
        hold_until=hold_until,
        value_type=value_type,
    )
    origin = hold_until if hold_until is not None else scored_at
    return ServedConfidence(
        stored=stored,
        effective=round(effective, 3),
        bucket=bucket_for(effective),
        scored_at=scored_at,
        half_life_days=half_life_days,
        age_days=max(0.0, (now - origin).total_seconds() / 86400.0),
        is_held=hold_until is not None and now < hold_until,
    )


async def subject_change_profile(
    session: AsyncSession,
    *,
    entity_id: uuid.UUID,
    now: datetime.datetime,
    window_days: int = VOLATILITY_WINDOW_DAYS,
) -> tuple[float | None, int]:
    """How often this entity has actually changed: median gap in days, and how
    many gaps that median came from.

    Read from the canonical graph's own bi-temporal history rather than collected
    separately -- "how fast does this subject change" is a question the registry
    already has the answer to. Attributes, facts and edges together, because a
    capability whose dependencies churn weekly is a fast-moving subject even if
    its attributes never move.

    Returns `(None, 0)` when there is too little history. An entity nobody has
    watched change is not an entity that changes slowly, and saying so would be
    inventing an observation.
    """
    rows = (
        await session.execute(
            text(
                "SELECT t_valid_from FROM ("
                "  SELECT t_valid_from FROM attributes "
                "   WHERE entity_id = :eid "
                "     AND t_valid_from >= CAST(:since AS TIMESTAMPTZ) "
                "  UNION ALL "
                "  SELECT t_valid_from FROM facts "
                "   WHERE entity_id = :eid "
                "     AND t_valid_from >= CAST(:since AS TIMESTAMPTZ) "
                "  UNION ALL "
                "  SELECT t_valid_from FROM edges "
                "   WHERE src_entity_id = :eid "
                "     AND t_valid_from >= CAST(:since AS TIMESTAMPTZ) "
                ") AS changes "
                "ORDER BY t_valid_from "
                "LIMIT :lim"
            ),
            {
                "eid": entity_id,
                "since": now - datetime.timedelta(days=window_days),
                "lim": _MAX_CHANGE_SAMPLES,
            },
        )
    ).all()

    instants = [r.t_valid_from for r in rows]
    if len(instants) < MIN_CHANGE_OBSERVATIONS:
        return None, 0

    gaps = [(instants[i + 1] - instants[i]).total_seconds() / 86400.0 for i in range(len(instants) - 1)]
    # Median rather than mean: one bulk import on a single day would otherwise
    # make a stable entity look highly volatile.
    positive = [g for g in gaps if g > 0]
    if not positive:
        # Everything recorded at the same instant is one change however many rows
        # it touched, so there is no interval to measure.
        return None, 0
    return statistics.median(positive), len(positive)


__all__ = [
    "VOLATILITY_WINDOW_DAYS",
    "ServedConfidence",
    "serve",
    "subject_change_profile",
]
