"""Turning raw usage into aggregates that outlive it.

Three grains, one pass each, all idempotent: a day can be rolled up repeatedly and
the result is the same, which matters because the schedule will occasionally run
twice over the same window and because a backfill has to be safe.

**Every statement counts distinct actors and stores no actor.** That is the single
property the whole retention design rests on — an aggregate with no actor
identifier is not personal data, so it needs no boundary and no erasure, and a
right-to-be-forgotten request cannot change a number someone has already quoted.

**Percentiles are computed here and stored, not computed on read.** The raw rows
they summarise will be deleted, so a read-time percentile would silently start
returning less as history aged out — the shape of bug that looks like a change in
the service rather than a change in the data.
"""

from __future__ import annotations

import dataclasses
import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["RollupResult", "roll_up_day"]


@dataclasses.dataclass(frozen=True)
class RollupResult:
    day: datetime.date
    tenant_day_rows: int
    capability_day_rows: int
    tool_day_rows: int


# `ON CONFLICT DO UPDATE` rather than delete-then-insert: re-running a day must not
# leave a window where the aggregate is missing, because the read API serves from
# these tables and a reader mid-rerun would get a hole rather than stale data.
_TENANT_DAY = text(
    """
    INSERT INTO usage_rollup_tenant_day (
        tenant_id, day, surface, calls, ok_calls, error_calls, distinct_actors,
        p50_ms, p95_ms, p99_ms, payload_bytes, payload_tokens, computed_at
    )
    SELECT
        tenant_id,
        CAST(:day AS date),
        surface,
        count(*),
        count(*) FILTER (WHERE outcome = 'ok'),
        count(*) FILTER (WHERE outcome = 'error'),
        -- The actor dimension, reduced to a number and then forgotten.
        count(DISTINCT actor_id),
        percentile_disc(0.50) WITHIN GROUP (ORDER BY latency_ms),
        percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms),
        percentile_disc(0.99) WITHIN GROUP (ORDER BY latency_ms),
        sum(payload_bytes),
        sum(payload_tokens),
        now()
    FROM usage_events
    WHERE occurred_at >= :start AND occurred_at < :end
    GROUP BY tenant_id, surface
    ON CONFLICT (tenant_id, day, surface) DO UPDATE SET
        calls = EXCLUDED.calls,
        ok_calls = EXCLUDED.ok_calls,
        error_calls = EXCLUDED.error_calls,
        distinct_actors = EXCLUDED.distinct_actors,
        p50_ms = EXCLUDED.p50_ms,
        p95_ms = EXCLUDED.p95_ms,
        p99_ms = EXCLUDED.p99_ms,
        payload_bytes = EXCLUDED.payload_bytes,
        payload_tokens = EXCLUDED.payload_tokens,
        computed_at = now()
    """
)

# `unnest` because one call can concern several entities — a blast-radius query
# touches many — and each should count once for each capability it involved.
_CAPABILITY_DAY = text(
    """
    INSERT INTO usage_rollup_capability_day (
        tenant_id, day, capability_id, calls, distinct_actors, computed_at
    )
    SELECT
        tenant_id,
        CAST(:day AS date),
        capability_id,
        count(*),
        count(DISTINCT actor_id),
        now()
    FROM usage_events, unnest(subject_entity_ids) AS capability_id
    WHERE occurred_at >= :start AND occurred_at < :end
    GROUP BY tenant_id, capability_id
    ON CONFLICT (tenant_id, day, capability_id) DO UPDATE SET
        calls = EXCLUDED.calls,
        distinct_actors = EXCLUDED.distinct_actors,
        computed_at = now()
    """
)

_TOOL_DAY = text(
    """
    INSERT INTO usage_rollup_tool_day (
        tenant_id, day, tool, calls, ok_calls, error_calls, distinct_actors,
        p50_ms, p95_ms, p99_ms, computed_at
    )
    SELECT
        tenant_id,
        CAST(:day AS date),
        operation,
        count(*),
        count(*) FILTER (WHERE outcome = 'ok'),
        count(*) FILTER (WHERE outcome = 'error'),
        count(DISTINCT actor_id),
        percentile_disc(0.50) WITHIN GROUP (ORDER BY latency_ms),
        percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms),
        percentile_disc(0.99) WITHIN GROUP (ORDER BY latency_ms),
        now()
    FROM usage_events
    WHERE occurred_at >= :start AND occurred_at < :end
      AND surface = 'mcp'
    GROUP BY tenant_id, operation
    ON CONFLICT (tenant_id, day, tool) DO UPDATE SET
        calls = EXCLUDED.calls,
        ok_calls = EXCLUDED.ok_calls,
        error_calls = EXCLUDED.error_calls,
        distinct_actors = EXCLUDED.distinct_actors,
        p50_ms = EXCLUDED.p50_ms,
        p95_ms = EXCLUDED.p95_ms,
        p99_ms = EXCLUDED.p99_ms,
        computed_at = now()
    """
)


async def roll_up_day(
    session_factory: async_sessionmaker[AsyncSession],
    day: datetime.date,
) -> RollupResult:
    """Aggregate one whole UTC day into all three grains.

    A whole day, not a rolling window: the grain has to be stable for a
    month-on-month comparison to mean anything, and a partial day rolled up early
    then rolled up again later is exactly what the upsert makes safe.

    All three statements share one transaction. A day that is half rolled up is
    worse than one not rolled up at all, because the read API cannot tell the
    difference and would serve a tool breakdown that disagrees with its own total.
    """
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(days=1)
    params = {"day": day, "start": start, "end": end}

    async with session_factory() as session, session.begin():
        tenant_rows = (await session.execute(_TENANT_DAY, params)).rowcount  # type: ignore[attr-defined]
        capability_rows = (await session.execute(_CAPABILITY_DAY, params)).rowcount  # type: ignore[attr-defined]
        tool_rows = (await session.execute(_TOOL_DAY, params)).rowcount  # type: ignore[attr-defined]

    return RollupResult(
        day=day,
        tenant_day_rows=int(tenant_rows or 0),
        capability_day_rows=int(capability_rows or 0),
        tool_day_rows=int(tool_rows or 0),
    )
