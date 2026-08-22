"""Owner-facing aggregates: how well the served context is holding up.

Named for the behaviour rather than added to the operator health router, which
answers "is anything broken right now" for a person watching a console. These
answer "is the context we serve any good", over a window, for an owner deciding
whether to invest in curation. Same gate, different question, different reader —
and folding them together would put team-scale quality figures on a surface whose
whole design assumes service-global plumbing counters.

**On the gate.** `admin`, which in this API means *tenant* administrator; there is
no service-operator identity in the REST surface, so the authorized-consumer set
resolves to the one identity that exists here. No cross-tenant aggregate exists
to be reached from here at all.

That gate used to rest on something stronger: the floors were enforced where the
aggregate was constructed, so an admin reading their own tenant's figures got the
same suppression everybody else did. that decision removed the floors, and the
authorization is now the whole of the protection rather than its outer layer.

**Every response carries what is needed to read it correctly.** The window, the
denominator and the classification are required fields, not annotations — a rate
served without its denominator is read as a count. `floors`, `suppressed`,
`partial` and `withheld` are gone from the response with the mechanism they
described.

**What this surface does not serve, which is now a property of these three
routes rather than a policy.** There is no per-actor path, no ranking, and no
ordering by value. That used to be a rule the whole module enforced —
"individual surveillance and team-performance evaluation are forbidden uses" —
and that decision rescinded it. What remains is that these routes do not offer those
shapes; nothing structural prevents a future one from doing so, and the
conformance gate that asserts today's absence is a pin on this surface, not a
guarantee about the system.
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


class CellOut(BaseModel):
    """One reported figure.

    `FloorsOut` and `suppressed` are gone with the floors they described
    `value` is no longer nullable: there is no state in which a cell
    exists without its figure, so a null here would have no meaning left to
    carry.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    value: int = Field(description="The measured figure for this cell.")


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
    cells: list[CellOut]
    total: int = Field(
        description=(
            "The total over every cell. It used to be recomputed over reported cells "
            "only, because serving the real population beside a withheld cell lets a "
            "reader recover the withheld figure by subtraction; with nothing withheld "
            "there is nothing to subtract toward."
        )
    )
    denominator: int = Field(
        description="The population the total is over, named for a reader who would otherwise infer a rate."
    )


def _window(days: int, now: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    """Resolve the requested window, clamped to the maximum span."""
    span = min(max(days, 1), MAX_WINDOW_DAYS)
    return now - datetime.timedelta(days=span), now


def _as_out(breakdown: learning_reads.Breakdown) -> BreakdownOut:
    """Project a breakdown onto the response model.

    The projection is still where the counts stop travelling. `Cell` keeps
    `actor_count` and `event_count` because a reader of the service layer may
    want to know how much a figure rests on; `CellOut` does not serve them, and
    widening it is a decision rather than a tidy-up.
    """
    return BreakdownOut(
        metric=breakdown.metric,
        cohort_key=breakdown.cohort_key,
        window_start=breakdown.window_start,
        window_end=breakdown.window_end,
        classification=AGGREGATE_CLASSIFICATION,
        cells=[CellOut(label=cell.label, value=cell.value) for cell in breakdown.cells],
        total=breakdown.total,
        denominator=breakdown.denominator,
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

    feedback = feedback_reads.FeedbackReadService(container.session_factory)
    learning = learning_reads.LearningReadService(container.session_factory)

    out: list[BreakdownOut] = []
    for metric in feedback_reads.FEEDBACK_METRICS:
        breakdown = await feedback.breakdown(ctx, metric, window_start=window_start, window_end=window_end)
        out.append(_as_out(breakdown))

    out.append(_as_out(await learning.claim_aging(ctx, now=now, window_start=window_start, window_end=window_end)))
    out.append(
        _as_out(await learning.contradiction_backlog(ctx, now=now, window_start=window_start, window_end=window_end))
    )
    out.append(_as_out(await learning.promotion_yield(ctx, window_start=window_start, window_end=window_end)))
    return out
