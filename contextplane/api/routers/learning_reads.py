"""Owner-facing aggregates: how well the served context is holding up.

Named for the behaviour rather than added to the operator health router, which
answers "is anything broken right now" for a person watching a console. These
answer "is the context we serve any good", over a window, for an owner deciding
whether to invest in curation. Same gate, different question, different reader —
and folding them together would put team-scale quality figures on a surface whose
whole design assumes service-global plumbing counters.

**On the gate.** `admin`, which in this API means *tenant* administrator; there is
no service-operator identity in the REST surface, so the authorized-consumer set
resolves to the one identity that exists here. That is safe in a way it would not
be for a raw read: the floors are enforced where the aggregate is constructed, so
an admin reading their own tenant's figures gets the same suppression everybody
else does. No cross-tenant aggregate exists to be reached from here at all.

**Every response carries what is needed to read it correctly.** The window, the
denominator, the classification, and the floors in force are required fields, not
annotations. A rate served without its denominator is read as a count; a partial
total served without its label is read as the truth; and a suppressed cell served
without the floors looks like a gap in the data rather than a rule being applied.

**What this surface deliberately cannot serve.** There is no per-actor path, no
ranking, no ordering by value, and no cohort finer than the tenant. Those are not
missing features — individual surveillance and team-performance evaluation are
forbidden uses, so the absence is the design, and a conformance gate asserts it
rather than trusting this docstring.
"""

from __future__ import annotations

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.container import Services, services
from contextplane.api.routers._admin_common import _admin_required
from contextplane.service.memory import learning_reads
from contextplane.signals import reads as feedback_reads
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/learning", tags=["learning-reads"])

#: What these aggregates are classified as. Served on every response because the
#: figure and its handling rules travel together or they do not travel at all.
AGGREGATE_CLASSIFICATION = "internal"

#: The longest window a caller may ask for, in days. Bounded because an unbounded
#: window makes the floors easier to clear by accumulating years of reports, which
#: is the opposite of what the floors are for.
MAX_WINDOW_DAYS = 400

#: The default window when a caller names none.
DEFAULT_WINDOW_DAYS = 30

#: Every metric this surface serves, from the two modules that compute them. Built
#: from their own closed tuples so a metric cannot be advertised here that nothing
#: computes, nor computed there and silently unreachable.
SERVED_METRICS: tuple[str, ...] = feedback_reads.FEEDBACK_METRICS + learning_reads.LEARNING_METRICS


class FloorsOut(BaseModel):
    """The thresholds in force, served so a suppressed cell is legible."""

    model_config = ConfigDict(extra="forbid")

    min_actors: int = Field(description="Minimum distinct contributors before a cell may carry a value.")
    min_events: int = Field(description="Minimum events before a cell may carry a value.")


class CellOut(BaseModel):
    """One reported figure, or the record that one was withheld.

    The counts behind a suppressed cell are deliberately absent from this model.
    Serving them would defeat the suppression: an actor count of two is the
    disclosure the floor exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    value: int | None = Field(
        description=(
            "Null exactly when the cell was suppressed for falling below a floor. "
            "Not zero: a withheld cell is not an empty one, and reporting it as zero "
            "would understate every total computed from this breakdown."
        )
    )
    suppressed: bool


class BreakdownOut(BaseModel):
    """One metric over one window, with everything needed to read it."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    cohort_key: str = Field(
        description=(
            "The population the figures cover. Always the tenant: no finer cohort "
            "exists, because a per-team breakdown would be a team-performance view."
        )
    )
    window_start: datetime.datetime
    window_end: datetime.datetime
    classification: str
    floors: FloorsOut
    cells: list[CellOut]
    total: int | None = Field(
        description=(
            "Recomputed over reported cells only, never the true population. With a "
            "cell withheld, serving the real total would let a reader recover the "
            "withheld figure by subtraction."
        )
    )
    denominator: int | None = Field(
        description="The population the total is over, named for a reader who would otherwise infer a rate."
    )
    partial: bool = Field(description="True when at least one cell was suppressed, so the total is over a subset.")
    withheld: bool = Field(
        description=(
            "True when the whole breakdown was withheld because its remainder could "
            "not be combined into a bucket clearing the floors. Distinct from every "
            "cell being suppressed: not even the shape of the distribution is served."
        )
    )


def _window(days: int, now: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    """Resolve the requested window, clamped to the maximum span."""
    span = min(max(days, 1), MAX_WINDOW_DAYS)
    return now - datetime.timedelta(days=span), now


def _as_out(breakdown: learning_reads.Breakdown, floors: learning_reads.Floors) -> BreakdownOut:
    """Project a floored breakdown onto the response model.

    The projection is where the counts stop travelling: `Cell` keeps them so a
    recompute can re-test the floors, and `CellOut` has nowhere to put them.
    """
    return BreakdownOut(
        metric=breakdown.metric,
        cohort_key=breakdown.cohort_key,
        window_start=breakdown.window_start,
        window_end=breakdown.window_end,
        classification=AGGREGATE_CLASSIFICATION,
        floors=FloorsOut(min_actors=floors.min_actors, min_events=floors.min_events),
        cells=[CellOut(label=cell.label, value=cell.value, suppressed=cell.suppressed) for cell in breakdown.cells],
        total=breakdown.total,
        denominator=breakdown.denominator,
        partial=breakdown.partial,
        withheld=breakdown.withheld,
    )


@router.get(
    "/metrics",
    response_model=list[str],
    summary="Which aggregate metrics this deployment serves",
)
async def list_metrics(
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> list[str]:
    """The closed metric set, so a client discovers it rather than guessing."""
    return list(SERVED_METRICS)


@router.get(
    "/aggregates",
    response_model=list[BreakdownOut],
    summary="Feedback and learning aggregates for this tenant",
)
async def read_aggregates(
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    container: Annotated[Services, Depends(services)],
    window_days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = DEFAULT_WINDOW_DAYS,
) -> list[BreakdownOut]:
    """Every served metric over one window, each already floored.

    All metrics in one response rather than a metric-per-path: a caller that had to
    name a metric would be able to ask for exactly the one whose cells are thin, and
    repeat that across windows until a suppressed cell was bracketed. Serving the
    whole set over one window makes that probing no cheaper than reading everything.
    """
    now = container.clock.now()
    window_start, window_end = _window(window_days, now)

    floors = learning_reads.Floors()
    feedback = feedback_reads.FeedbackReadService(container.session_factory, floors=floors)
    learning = learning_reads.LearningReadService(container.session_factory, floors=floors)

    out: list[BreakdownOut] = []
    for metric in feedback_reads.FEEDBACK_METRICS:
        breakdown = await feedback.breakdown(ctx, metric, window_start=window_start, window_end=window_end)
        out.append(_as_out(breakdown, floors))

    out.append(
        _as_out(
            await learning.claim_aging(ctx, now=now, window_start=window_start, window_end=window_end),
            floors,
        )
    )
    out.append(
        _as_out(
            await learning.contradiction_backlog(ctx, now=now, window_start=window_start, window_end=window_end),
            floors,
        )
    )
    out.append(
        _as_out(
            await learning.promotion_yield(ctx, window_start=window_start, window_end=window_end),
            floors,
        )
    )
    return out
