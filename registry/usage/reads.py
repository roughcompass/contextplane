"""Reading usage back, in aggregate only.

There is no per-event read anywhere in this module and there is not going to be
one. The raw table exists to be aggregated and then expire; a row-level surface
would turn it into a per-actor activity log that outlives its own justification,
and every question this data is collected to answer is a question about a group.

**Two of these numbers do not add up across days, and that is the whole
difficulty here.** Every other field in a rollup is a sum, so a range is a
`SUM()`. Distinct actors and latency percentiles are not:

- Adding thirty days of `distinct_actors` counts a person who showed up daily
  thirty times. That figure is real and useful, but it is *actor-days*, not
  people, so it is returned under that name. The actual distinct count for a
  range can only come from the raw rows, so it is computed from them when the
  window is still inside retention and returned as null with a reason when it is
  not. Null rather than the sum: a number that is thirty times too large is worse
  than a gap, because a gap gets asked about.

- Averaging daily p95s is not the p95 of the range — it is a number with no
  definition at all. What can be said honestly from stored daily percentiles is
  the worst of them, so that is what is returned, under a name that says so.

The daily series exists partly for this reason: percentiles are exactly correct
at the grain they were computed, and a caller that wants latency over a range
should be reading the shape rather than one collapsed figure.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.usage.vocabularies import SURFACES

__all__ = [
    "MAX_RANGE_DAYS",
    "CapabilityUsage",
    "DailyPoint",
    "SurfaceSummary",
    "ToolUsage",
    "UsageSummary",
    "read_capability_rankings",
    "read_daily_series",
    "read_summary",
    "read_tool_rankings",
]

#: The widest window a single read will serve. Not a performance guess: the read
#: is served from a rollup indexed on `(tenant_id, day DESC)`, so cost is linear in
#: days. The cap exists so one request cannot ask for the entire history of a
#: deployment and discover the limit as a timeout.
MAX_RANGE_DAYS = 400

#: How many top rows a ranking returns by default and at most. A ranking is a
#: ranking; a caller wanting everything wants the series.
DEFAULT_RANKING_LIMIT = 20
MAX_RANKING_LIMIT = 200


class RangeTooWideError(ValueError):
    """The requested window exceeds `MAX_RANGE_DAYS`."""


class InvalidRangeError(ValueError):
    """The window ends before it starts."""


@dataclasses.dataclass(frozen=True)
class SurfaceSummary:
    surface: str
    calls: int
    ok_calls: int
    error_calls: int
    #: Sum of daily distinct actors. A person active on ten days counts ten times.
    #: Named for what it is, because `distinct_actors` summed over a range is the
    #: single most inviting wrong number in this whole subsystem.
    actor_days: int
    #: True distinct actors over the window, or None when the window reaches
    #: further back than the raw rows do.
    distinct_actors: int | None
    #: Why `distinct_actors` is None, when it is. Present so a caller renders an
    #: explanation rather than a zero.
    distinct_actors_unavailable_reason: str | None
    payload_bytes: int | None
    payload_tokens: int | None
    #: The largest daily p95 in the window. Deliberately not "the p95 over the
    #: window", which cannot be computed from daily percentiles at all.
    worst_daily_p95_ms: int | None


@dataclasses.dataclass(frozen=True)
class UsageSummary:
    start: datetime.date
    end: datetime.date
    days: int
    surfaces: tuple[SurfaceSummary, ...]


@dataclasses.dataclass(frozen=True)
class DailyPoint:
    day: datetime.date
    surface: str
    calls: int
    ok_calls: int
    error_calls: int
    distinct_actors: int
    p50_ms: int | None
    p95_ms: int | None
    p99_ms: int | None


@dataclasses.dataclass(frozen=True)
class ToolUsage:
    tool: str
    calls: int
    ok_calls: int
    error_calls: int
    actor_days: int
    worst_daily_p95_ms: int | None


@dataclasses.dataclass(frozen=True)
class CapabilityUsage:
    capability_id: uuid.UUID
    calls: int
    actor_days: int


def _validate(start: datetime.date, end: datetime.date) -> int:
    if end < start:
        msg = f"window ends before it starts: {start} to {end}"
        raise InvalidRangeError(msg)
    days = (end - start).days + 1
    if days > MAX_RANGE_DAYS:
        msg = f"window of {days} days exceeds the {MAX_RANGE_DAYS}-day maximum"
        raise RangeTooWideError(msg)
    return days


_SUMMARY = text(
    """
    SELECT
        surface,
        sum(calls),
        sum(ok_calls),
        sum(error_calls),
        -- Summed deliberately, and named `actor_days` all the way out to the
        -- response because of it.
        sum(distinct_actors),
        sum(payload_bytes),
        sum(payload_tokens),
        max(p95_ms)
    FROM usage_rollup_tenant_day
    WHERE tenant_id = :tenant AND day >= :start AND day <= :end
    GROUP BY surface
    ORDER BY surface
    """
)

# The true distinct count for a window, from the raw rows. Only correct while the
# whole window is still inside retention, which the caller checks before asking.
_DISTINCT_ACTORS = text(
    """
    SELECT surface, count(DISTINCT actor_id)
    FROM usage_events
    WHERE tenant_id = :tenant AND occurred_at >= :start AND occurred_at < :end
    GROUP BY surface
    """
)

_SERIES = text(
    """
    SELECT day, surface, calls, ok_calls, error_calls, distinct_actors, p50_ms, p95_ms, p99_ms
    FROM usage_rollup_tenant_day
    WHERE tenant_id = :tenant AND day >= :start AND day <= :end
    ORDER BY day, surface
    """
)

_TOOLS = text(
    """
    SELECT tool, sum(calls) AS calls, sum(ok_calls), sum(error_calls),
           sum(distinct_actors), max(p95_ms)
    FROM usage_rollup_tool_day
    WHERE tenant_id = :tenant AND day >= :start AND day <= :end
    GROUP BY tool
    ORDER BY calls DESC, tool
    LIMIT :limit
    """
)

_CAPABILITIES = text(
    """
    SELECT capability_id, sum(calls) AS calls, sum(distinct_actors)
    FROM usage_rollup_capability_day
    WHERE tenant_id = :tenant AND day >= :start AND day <= :end
    GROUP BY capability_id
    ORDER BY calls DESC, capability_id
    LIMIT :limit
    """
)


async def read_summary(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    start: datetime.date,
    end: datetime.date,
    retention_days: int,
    today: datetime.date,
) -> UsageSummary:
    """Totals per surface over an inclusive day range.

    `retention_days` and `today` are passed in rather than read here so the
    distinct-actor decision is testable without moving the clock, and so this stays
    a function over a session factory like every other read in this module.
    """
    days = _validate(start, end)
    params = {"tenant": tenant_id, "start": start, "end": end}

    # The raw rows only reach back `retention_days`, and the sweep runs hourly, so
    # a window whose first day is at or before the boundary is partially expired
    # already. Refusing at the boundary rather than one day inside it means the
    # answer does not depend on when in the hour the question was asked.
    boundary = today - datetime.timedelta(days=retention_days)
    raw_covers_window = start > boundary
    reason = (
        None
        if raw_covers_window
        else (
            f"the window starts on {start}, at or before the {retention_days}-day raw "
            f"retention boundary of {boundary}; a distinct count over it cannot be "
            "computed from rows that no longer exist"
        )
    )

    async with session_factory() as session:
        rows = (await session.execute(_SUMMARY, params)).all()

        distinct: dict[str, int] = {}
        if raw_covers_window and rows:
            distinct = {
                surface: count
                for surface, count in (
                    await session.execute(
                        _DISTINCT_ACTORS,
                        {
                            "tenant": tenant_id,
                            "start": datetime.datetime.combine(start, datetime.time.min, tzinfo=datetime.UTC),
                            # Exclusive upper bound one day past `end`, so the last
                            # day of an inclusive range is whole.
                            "end": datetime.datetime.combine(
                                end + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.UTC
                            ),
                        },
                    )
                ).all()
            }

    return UsageSummary(
        start=start,
        end=end,
        days=days,
        surfaces=tuple(
            SurfaceSummary(
                surface=surface,
                calls=int(calls),
                ok_calls=int(ok_calls),
                error_calls=int(error_calls),
                actor_days=int(actor_days),
                # `.get(surface, 0)` and not `.get(surface)`: when the window is
                # inside retention and the surface has rollup rows, an absent raw
                # group means the rows really were deleted for that surface, and
                # zero is the true count. Null is reserved for "cannot be known".
                distinct_actors=distinct.get(surface, 0) if raw_covers_window else None,
                distinct_actors_unavailable_reason=reason,
                payload_bytes=None if payload_bytes is None else int(payload_bytes),
                payload_tokens=None if payload_tokens is None else int(payload_tokens),
                worst_daily_p95_ms=None if worst_p95 is None else int(worst_p95),
            )
            for surface, calls, ok_calls, error_calls, actor_days, payload_bytes, payload_tokens, worst_p95 in rows
        ),
    )


async def read_daily_series(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    start: datetime.date,
    end: datetime.date,
    surface: str | None = None,
) -> tuple[DailyPoint, ...]:
    """One point per day per surface. Percentiles here are exact at this grain.

    Days with no traffic are absent rather than zero-filled. A caller plotting a
    line wants to know which is which, and inventing zeroes here would make an
    outage indistinguishable from a quiet Sunday.
    """
    _validate(start, end)
    if surface is not None and surface not in SURFACES:
        msg = f"unknown surface {surface!r}; expected one of {sorted(SURFACES)}"
        raise ValueError(msg)

    async with session_factory() as session:
        rows = (
            await session.execute(_SERIES, {"tenant": tenant_id, "start": start, "end": end})
        ).all()

    return tuple(
        DailyPoint(
            day=day,
            surface=row_surface,
            calls=int(calls),
            ok_calls=int(ok_calls),
            error_calls=int(error_calls),
            distinct_actors=int(distinct_actors),
            p50_ms=p50,
            p95_ms=p95,
            p99_ms=p99,
        )
        for day, row_surface, calls, ok_calls, error_calls, distinct_actors, p50, p95, p99 in rows
        if surface is None or row_surface == surface
    )


async def read_tool_rankings(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    start: datetime.date,
    end: datetime.date,
    limit: int = DEFAULT_RANKING_LIMIT,
) -> tuple[ToolUsage, ...]:
    """Which MCP tools were called, most first."""
    _validate(start, end)
    async with session_factory() as session:
        rows = (
            await session.execute(
                _TOOLS,
                {"tenant": tenant_id, "start": start, "end": end, "limit": min(limit, MAX_RANKING_LIMIT)},
            )
        ).all()

    return tuple(
        ToolUsage(
            tool=tool,
            calls=int(calls),
            ok_calls=int(ok_calls),
            error_calls=int(error_calls),
            actor_days=int(actor_days),
            worst_daily_p95_ms=None if worst_p95 is None else int(worst_p95),
        )
        for tool, calls, ok_calls, error_calls, actor_days, worst_p95 in rows
    )


async def read_capability_rankings(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    start: datetime.date,
    end: datetime.date,
    limit: int = DEFAULT_RANKING_LIMIT,
) -> tuple[CapabilityUsage, ...]:
    """Which capabilities this tenant's callers asked about, most first.

    Scoped to the calling tenant's own calls. A capability owned elsewhere appears
    here when this tenant's actors looked at it, which is this tenant's usage — and
    what another tenant's callers did with it is not in this table for this tenant.
    """
    _validate(start, end)
    async with session_factory() as session:
        rows = (
            await session.execute(
                _CAPABILITIES,
                {"tenant": tenant_id, "start": start, "end": end, "limit": min(limit, MAX_RANKING_LIMIT)},
            )
        ).all()

    return tuple(
        CapabilityUsage(capability_id=capability_id, calls=int(calls), actor_days=int(actor_days))
        for capability_id, calls, actor_days in rows
    )
