"""Operator surface: who is using this, through which surface, and for what.

Aggregates only. There is no endpoint here that returns a usage event, and adding
one would change what the underlying table is: raw rows exist to be aggregated and
then expire, and a row-level read surface would make them a per-actor activity log
that outlives the justification for collecting them. Every question this data was
gathered to answer is a question about a group.

**Nothing here names an actor.** The rankings and series carry counts, and the one
actor-shaped figure they carry is a count of distinct actors. So this surface is
tenant-scoped and role-gated without also being a way to look up what one named
colleague did last Tuesday.

**Two fields are named awkwardly on purpose.** `actor_days` is the sum of daily
distinct actors, which counts a daily visitor once per day; and
`worst_daily_p95_ms` is the largest daily p95, not the p95 of the range, which
cannot be computed from stored daily percentiles at all. Both could have been
called something shorter and wrong. A dashboard that renders `distinct_actors` for
a month and shows the sum of thirty days is not obviously broken to anyone looking
at it, which is exactly why the name has to carry the caveat.

**Tenant scoping is structural, not checked.** Every statement takes the tenant
from the request context, and no route accepts a tenant id, so there is no
cross-tenant read to authorize or refuse. A capability owned by another tenant
appears in the rankings only when this tenant's own callers asked about it, which
is this tenant's usage.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.routers._admin_common import _admin_required
from contextplane.types import TenantContext
from contextplane.usage import reads
from contextplane.usage.vocabularies import SURFACES

router = APIRouter(prefix="/v1/admin/usage")

#: Default window when the caller names neither end. Thirty days is the shortest
#: window in which a weekly rhythm is visible more than once.
_DEFAULT_WINDOW_DAYS = 30


class SurfaceSummaryOut(BaseModel):
    """One surface's (rest or mcp) figures for the requested window, nested inside UsageSummaryOut."""

    model_config = ConfigDict(extra="forbid")

    surface: Literal["rest", "mcp"]
    calls: int
    ok_calls: int
    error_calls: int
    actor_days: int = Field(
        description=(
            "Sum of each day's distinct actors. An actor active on ten days counts "
            "ten times. This is not a headcount — read distinct_actors for that."
        )
    )
    distinct_actors: int | None = Field(
        description=(
            "Actual distinct actors across the whole window, counted from raw rows. "
            "Null when the window reaches past the raw retention boundary, because "
            "it cannot be recovered from daily counts. Deliberately not the sum of "
            "actor_days, which for a month is up to thirty times too large."
        )
    )
    distinct_actors_unavailable_reason: str | None = Field(
        default=None,
        description="Why distinct_actors is null, so a caller can render the reason rather than a zero.",
    )
    payload_bytes: int | None
    payload_tokens: int | None
    worst_daily_p95_ms: int | None = Field(
        description=(
            "The largest single-day p95 in the window. Not the p95 of the window: "
            "an average of percentiles has no definition. For latency over time, "
            "read the daily series, where each percentile is exact at its own grain."
        )
    )


class UsageSummaryOut(BaseModel):
    """Response body for GET /v1/admin/usage/summary: one row per surface for the resolved window."""

    model_config = ConfigDict(extra="forbid")

    start: datetime.date
    end: datetime.date
    days: int
    surfaces: list[SurfaceSummaryOut]


class DailyPointOut(BaseModel):
    """One day's exact figures for one surface — unlike the summary, nothing here is an approximation."""

    model_config = ConfigDict(extra="forbid")

    day: datetime.date
    surface: Literal["rest", "mcp"]
    calls: int
    ok_calls: int
    error_calls: int
    distinct_actors: int = Field(
        description="Exact for this one day. Do not sum these across days — see actor_days on the summary."
    )
    p50_ms: int | None
    p95_ms: int | None
    p99_ms: int | None


class DailySeriesOut(BaseModel):
    """Response body for GET /v1/admin/usage/series: the day-by-day breakdown behind the summary's totals."""

    model_config = ConfigDict(extra="forbid")

    start: datetime.date
    end: datetime.date
    points: list[DailyPointOut] = Field(
        description=(
            "One point per day per surface. Days with no traffic are absent rather "
            "than zero, so a caller can tell an outage from a quiet weekend."
        )
    )


class ToolUsageOut(BaseModel):
    """One MCP tool's call volume and outcomes for the window, nested inside ToolRankingOut."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    calls: int
    ok_calls: int
    error_calls: int
    actor_days: int
    worst_daily_p95_ms: int | None


class ToolRankingOut(BaseModel):
    """Response body for GET /v1/admin/usage/tools: which MCP tools this tenant's agents actually call."""

    model_config = ConfigDict(extra="forbid")

    start: datetime.date
    end: datetime.date
    tools: list[ToolUsageOut]


class CapabilityUsageOut(BaseModel):
    """One capability's call volume for the window, nested inside CapabilityRankingOut."""

    model_config = ConfigDict(extra="forbid")

    capability_id: uuid.UUID
    calls: int
    actor_days: int


class CapabilityRankingOut(BaseModel):
    """Response body for GET /v1/admin/usage/capabilities: which capabilities this tenant's callers asked about."""

    model_config = ConfigDict(extra="forbid")

    start: datetime.date
    end: datetime.date
    capabilities: list[CapabilityUsageOut]


def _window(
    start: datetime.date | None,
    end: datetime.date | None,
) -> tuple[datetime.date, datetime.date]:
    """Resolve the requested window, defaulting to the last thirty whole days.

    Ends today rather than yesterday. Today's rollup is recomputed hourly, so it is
    present but incomplete — and a surface that hid it would look stale to anyone
    checking whether traffic they just generated arrived.
    """
    resolved_end = end if end is not None else datetime.datetime.now(tz=datetime.UTC).date()
    resolved_start = start if start is not None else resolved_end - datetime.timedelta(days=_DEFAULT_WINDOW_DAYS - 1)
    try:
        reads._validate(resolved_start, resolved_end)
    except reads.InvalidRangeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except reads.RangeTooWideError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return resolved_start, resolved_end


# ---------------------------------------------------------------------------
# Conversion, spelled out
# ---------------------------------------------------------------------------
#
# Field by field rather than a `**asdict()` splat. The splat reads as less code and
# types as `dict[str, object]`, which means a renamed field on either side becomes a
# runtime validation error on a live request instead of a type error here. These
# names carry caveats — `actor_days`, `worst_daily_p95_ms` — and quietly dropping
# one is exactly the failure the caveats exist to prevent.


def _surface_out(summary: reads.SurfaceSummary) -> SurfaceSummaryOut:
    return SurfaceSummaryOut(
        surface=summary.surface,  # type: ignore[arg-type]
        calls=summary.calls,
        ok_calls=summary.ok_calls,
        error_calls=summary.error_calls,
        actor_days=summary.actor_days,
        distinct_actors=summary.distinct_actors,
        distinct_actors_unavailable_reason=summary.distinct_actors_unavailable_reason,
        payload_bytes=summary.payload_bytes,
        payload_tokens=summary.payload_tokens,
        worst_daily_p95_ms=summary.worst_daily_p95_ms,
    )


def _point_out(point: reads.DailyPoint) -> DailyPointOut:
    return DailyPointOut(
        day=point.day,
        surface=point.surface,  # type: ignore[arg-type]
        calls=point.calls,
        ok_calls=point.ok_calls,
        error_calls=point.error_calls,
        distinct_actors=point.distinct_actors,
        p50_ms=point.p50_ms,
        p95_ms=point.p95_ms,
        p99_ms=point.p99_ms,
    )


def _tool_out(tool: reads.ToolUsage) -> ToolUsageOut:
    return ToolUsageOut(
        tool=tool.tool,
        calls=tool.calls,
        ok_calls=tool.ok_calls,
        error_calls=tool.error_calls,
        actor_days=tool.actor_days,
        worst_daily_p95_ms=tool.worst_daily_p95_ms,
    )


def _capability_out(capability: reads.CapabilityUsage) -> CapabilityUsageOut:
    return CapabilityUsageOut(
        capability_id=capability.capability_id,
        calls=capability.calls,
        actor_days=capability.actor_days,
    )


_StartQuery = Annotated[datetime.date | None, Query(alias="from", description="First day of the window, inclusive.")]
_EndQuery = Annotated[datetime.date | None, Query(alias="to", description="Last day of the window, inclusive.")]
_LimitQuery = Annotated[int, Query(ge=1, le=reads.MAX_RANKING_LIMIT)]


@router.get(
    "/summary",
    response_model=UsageSummaryOut,
    tags=["admin: operations"],
    summary="Call volume, outcomes, and reach per surface over a window",
)
async def get_usage_summary(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    start: _StartQuery = None,
    end: _EndQuery = None,
) -> UsageSummaryOut:
    """Aggregate figures only — this route never returns a row naming an actor.

    `from`/`to` default to the last 30 whole days ending today; today's own
    rollup is included even though it is only partially through the day, so a
    caller checking traffic they just generated does not see it missing.
    `distinct_actors` is null once the window reaches past the raw-retention
    boundary, because it cannot be reconstructed from the daily rollups alone.
    """
    resolved_start, resolved_end = _window(start, end)
    summary = await reads.read_summary(
        request.app.state.session_factory,
        tenant_id=ctx.tenant_id,
        start=resolved_start,
        end=resolved_end,
        retention_days=request.app.state.settings.usage_retention_days,
        today=datetime.datetime.now(tz=datetime.UTC).date(),
    )
    return UsageSummaryOut(
        start=summary.start,
        end=summary.end,
        days=summary.days,
        surfaces=[_surface_out(s) for s in summary.surfaces],
    )


@router.get(
    "/series",
    response_model=DailySeriesOut,
    tags=["admin: operations"],
    summary="Daily call volume, outcomes, and latency percentiles",
)
async def get_usage_series(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    start: _StartQuery = None,
    end: _EndQuery = None,
    surface: Annotated[str | None, Query(description=f"One of {sorted(SURFACES)}. Omit for all.")] = None,
) -> DailySeriesOut:
    """Day-by-day figures behind the summary's totals, one point per day per surface.

    Days with no recorded traffic are absent from `points` rather than present
    with zeros, so a caller can distinguish an outage from a quiet day. Every
    percentile here is exact for its own day — this is the series to read for
    latency over time, since the summary's `worst_daily_p95_ms` is only the
    largest of these, not a recomputed window-wide percentile.
    """
    resolved_start, resolved_end = _window(start, end)
    if surface is not None and surface not in SURFACES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown surface {surface!r}; expected one of {sorted(SURFACES)}",
        )
    points = await reads.read_daily_series(
        request.app.state.session_factory,
        tenant_id=ctx.tenant_id,
        start=resolved_start,
        end=resolved_end,
        surface=surface,
    )
    return DailySeriesOut(
        start=resolved_start,
        end=resolved_end,
        points=[_point_out(p) for p in points],
    )


@router.get(
    "/tools",
    response_model=ToolRankingOut,
    tags=["admin: operations"],
    summary="Which MCP tools agents actually call",
)
async def get_tool_rankings(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    start: _StartQuery = None,
    end: _EndQuery = None,
    limit: _LimitQuery = reads.DEFAULT_RANKING_LIMIT,
) -> ToolRankingOut:
    """Rank MCP tools by call volume over the window; `limit` bounds the returned list, not the underlying count."""
    resolved_start, resolved_end = _window(start, end)
    tools = await reads.read_tool_rankings(
        request.app.state.session_factory,
        tenant_id=ctx.tenant_id,
        start=resolved_start,
        end=resolved_end,
        limit=limit,
    )
    return ToolRankingOut(
        start=resolved_start,
        end=resolved_end,
        tools=[_tool_out(t) for t in tools],
    )


@router.get(
    "/capabilities",
    response_model=CapabilityRankingOut,
    tags=["admin: operations"],
    summary="Which capabilities this tenant's callers asked about",
)
async def get_capability_rankings(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    start: _StartQuery = None,
    end: _EndQuery = None,
    limit: _LimitQuery = reads.DEFAULT_RANKING_LIMIT,
) -> CapabilityRankingOut:
    """Rank capabilities by this tenant's own call volume.

    Never reflects other tenants' usage of the same capability.
    """
    resolved_start, resolved_end = _window(start, end)
    capabilities = await reads.read_capability_rankings(
        request.app.state.session_factory,
        tenant_id=ctx.tenant_id,
        start=resolved_start,
        end=resolved_end,
        limit=limit,
    )
    return CapabilityRankingOut(
        start=resolved_start,
        end=resolved_end,
        capabilities=[_capability_out(c) for c in capabilities],
    )
