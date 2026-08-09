"""Feedback and signal aggregates: how well served context is holding up.

These answer the questions an owner has about the system — is context going stale,
is it being reused, do handoffs succeed — and they answer them over people's
reports, which is what makes the floors load-bearing rather than defensive. The
floors, the suppression rule and the partial-total rule are imported from the
learning-read module rather than restated here: two definitions that agree today is
not the same as one rule enforced uniformly, and this is exactly the pair that would
drift.

**Diagnostic observations are excluded structurally, not filtered late.** A
diagnostic cites neither a receipt nor an item and is never learning-eligible: it is
a report *about the system's plumbing*, not a verdict on served context. Counting it
as a quality signal would let a burst of plumbing reports read as a collapse in
context quality. Every statement here excludes the kind in its own WHERE clause, so
there is no aggregate whose exclusion depends on the caller remembering to ask.

**No cell is per-actor and no cohort is finer than the tenant.** The reporter id is
read only to count *distinct* reporters, which is what the actor floor is tested
against; it never reaches a value, a label, or a response. That is the difference
between measuring whether enough independent people reported something and
publishing who they were.
"""

from __future__ import annotations

import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.memory.learning_reads import (
    Breakdown,
    Cell,
    Floors,
    build_breakdown,
)
from contextplane.signals.feedback import KIND_DIAGNOSTIC
from contextplane.types import TenantContext

METRIC_CONTEXT_QUALITY = "context_quality"
METRIC_REUSE = "reuse"
METRIC_HANDOFF_SUCCESS = "handoff_success"
METRIC_ADEQUACY = "adequacy"

#: Every metric this module serves, closed so the router cannot advertise one
#: nothing computes and the conformance gate can assert the whole set.
FEEDBACK_METRICS: tuple[str, ...] = (
    METRIC_CONTEXT_QUALITY,
    METRIC_REUSE,
    METRIC_HANDOFF_SUCCESS,
    METRIC_ADEQUACY,
)

#: Which ratings each metric is computed over. Explicit per metric rather than
#: "everything else": a rating added to the vocabulary must be assigned to a metric
#: deliberately, and one assigned to none is visibly unreported rather than silently
#: folded into whichever aggregate happened to use a negation.
_RATINGS_FOR: dict[str, tuple[str, ...]] = {
    METRIC_CONTEXT_QUALITY: ("missing", "stale", "incorrect", "contradicted", "unsafe"),
    METRIC_REUSE: ("selected", "ignored"),
    METRIC_HANDOFF_SUCCESS: ("succeeded", "failed", "rolled_back"),
    METRIC_ADEQUACY: ("relevant", "irrelevant", "needs_human_review"),
}

# One statement for all four metrics: they differ only in which ratings they group
# over. `reporter_id` appears once, inside count(DISTINCT ...), which is the only
# use of it this module has.
_RATING_BREAKDOWN_SQL = """
SELECT
    rating AS label,
    count(*) AS event_count,
    count(DISTINCT reporter_id) AS actor_count
FROM context_feedback
WHERE tenant_id = :tenant
  AND kind <> :diagnostic_kind
  AND rating = ANY(:ratings)
  AND created_at >= :window_start
  AND created_at < :window_end
GROUP BY rating
"""


class FeedbackReadService:
    """Aggregate feedback reads for one tenant, floored at construction.

    Constructed per request from the session factory and the floors, like the
    learning-read service it shares its rules with. No method returns an unfloored
    figure, so there is no ordering of calls that produces one.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        floors: Floors | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._floors = floors or Floors()

    @property
    def floors(self) -> Floors:
        """The floors in force, so a caller can serve them beside the figures."""
        return self._floors

    async def breakdown(
        self,
        ctx: TenantContext,
        metric: str,
        *,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Breakdown:
        """One metric's cells, already suppressed, combined and partial-totalled."""
        ratings = _RATINGS_FOR[metric]
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(_RATING_BREAKDOWN_SQL),
                    {
                        "tenant": ctx.tenant_id,
                        "diagnostic_kind": KIND_DIAGNOSTIC,
                        "ratings": list(ratings),
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).all()

        cells = [
            Cell.measured(
                str(row.label),
                actor_count=int(row.actor_count),
                event_count=int(row.event_count),
                value=int(row.event_count),
                floors=self._floors,
            )
            for row in rows
        ]
        return build_breakdown(
            metric,
            window_start=window_start,
            window_end=window_end,
            cells=cells,
            floors=self._floors,
        )


__all__ = [
    "FEEDBACK_METRICS",
    "METRIC_ADEQUACY",
    "METRIC_CONTEXT_QUALITY",
    "METRIC_HANDOFF_SUCCESS",
    "METRIC_REUSE",
    "FeedbackReadService",
]
