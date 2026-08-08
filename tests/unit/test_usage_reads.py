"""Unit tests for `contextplane/usage/reads.py` — the aggregate-only usage read surface.

Unit-scope coverage was 73% before this file, but that number was flattering:
`_validate`'s two error paths are exercised incidentally through
`test_error_contract_http.py`'s router-level test, while every read
function's *own* body -- the actual row mapping, the cross-tenant ownership
query, and the module's own headline difficulty (distinct-actor and
worst-daily-p95 semantics) -- had no dedicated coverage at all.

Coverage:
- read_owned_capability_usage — row mapping; limit clamped to
  `MAX_RANKING_LIMIT`; no usage rows means an absent capability, not one
  with zeros (the module's own stated design, not an oversight)
- read_summary                — inside-retention path computes true
  `distinct_actors` from a second query; outside-retention path returns
  `None` with a reason and skips that query entirely; the boundary itself
  (`start == boundary`) lands on the outside-retention side; `actor_days`
  is the raw un-deduplicated sum, never conflated with `distinct_actors`
- read_daily_series            — row mapping; unknown `surface` raises
  `ValueError`; the `surface` filter narrows rows fetched for every
  surface down to the one requested
- read_tool_rankings / read_capability_rankings — row mapping; limit
  clamped to `MAX_RANKING_LIMIT`
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.usage import reads
from contextplane.usage.vocabularies import SURFACE_MCP, SURFACE_REST

_TENANT = uuid.uuid4()
_START = datetime.date(2026, 1, 1)
_END = datetime.date(2026, 1, 7)


class _FakeResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


def _session_factory(*row_batches: list[tuple[object, ...]]) -> tuple[MagicMock, AsyncMock]:
    """A `session_factory` whose `session.execute` returns each of
    *row_batches* in turn, one per call, wrapped as a fake `Result`."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(rows=batch) for batch in row_batches])

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=session_cm), session


# ---------------------------------------------------------------------------
# read_owned_capability_usage — the one cross-tenant query in this module
# ---------------------------------------------------------------------------


class TestReadOwnedCapabilityUsage:
    async def test_maps_rows_to_owned_capability_usage(self) -> None:
        cap_id = uuid.uuid4()
        factory, _ = _session_factory([(cap_id, "PaymentAPI", 42, 40, 2, 7, 1024)])

        result = await reads.read_owned_capability_usage(factory, owner_tenant_id=_TENANT, start=_START, end=_END)

        assert len(result) == 1
        row = result[0]
        assert row.capability_id == cap_id
        assert row.name == "PaymentAPI"
        assert row.calls == 42
        assert row.ok_calls == 40
        assert row.error_calls == 2
        assert row.actor_days == 7
        assert row.payload_bytes == 1024

    async def test_null_payload_bytes_stays_none_not_zero(self) -> None:
        cap_id = uuid.uuid4()
        factory, _ = _session_factory([(cap_id, "PaymentAPI", 1, 1, 0, 1, None)])

        result = await reads.read_owned_capability_usage(factory, owner_tenant_id=_TENANT, start=_START, end=_END)

        assert result[0].payload_bytes is None

    async def test_no_recorded_usage_returns_no_rows_not_zeroed_rows(self) -> None:
        """An owned capability nobody has ever called is absent, per the
        module's own documented design -- not present with zero counts."""
        factory, _ = _session_factory([])

        result = await reads.read_owned_capability_usage(factory, owner_tenant_id=_TENANT, start=_START, end=_END)

        assert result == ()

    async def test_limit_is_clamped_to_the_ranking_ceiling(self) -> None:
        factory, session = _session_factory([])

        await reads.read_owned_capability_usage(
            factory,
            owner_tenant_id=_TENANT,
            start=_START,
            end=_END,
            limit=reads.MAX_RANKING_LIMIT + 500,
        )

        params = session.execute.await_args.args[1]
        assert params["limit"] == reads.MAX_RANKING_LIMIT


# ---------------------------------------------------------------------------
# read_summary — actor_days vs. distinct_actors, and the retention boundary
# ---------------------------------------------------------------------------


class TestReadSummary:
    async def test_inside_retention_window_computes_true_distinct_actors(self) -> None:
        # start is well after the 30-day boundary, so the raw rows still cover it.
        factory, session = _session_factory(
            [("rest", 100, 90, 10, 50, 2048, 4096, 120)],
            [("rest", 12)],
        )

        summary = await reads.read_summary(
            factory,
            tenant_id=_TENANT,
            start=datetime.date(2026, 1, 20),
            end=datetime.date(2026, 1, 25),
            retention_days=30,
            today=datetime.date(2026, 1, 31),
        )

        assert session.execute.await_count == 2
        surface = summary.surfaces[0]
        assert surface.distinct_actors == 12
        assert surface.distinct_actors_unavailable_reason is None
        # actor_days is the raw summed column -- a different number from the
        # true distinct count, and both are present at once.
        assert surface.actor_days == 50
        assert surface.actor_days != surface.distinct_actors

    async def test_outside_retention_window_returns_none_with_a_reason_and_skips_the_query(self) -> None:
        factory, session = _session_factory([("rest", 100, 90, 10, 50, None, None, None)])

        summary = await reads.read_summary(
            factory,
            tenant_id=_TENANT,
            start=datetime.date(2025, 1, 1),
            end=datetime.date(2025, 1, 7),
            retention_days=30,
            today=datetime.date(2026, 1, 31),
        )

        # Only the rollup query ran -- the raw-row distinct-actor query was
        # never issued, because the window is outside where raw rows exist.
        assert session.execute.await_count == 1
        surface = summary.surfaces[0]
        assert surface.distinct_actors is None
        assert surface.distinct_actors_unavailable_reason is not None
        assert "30-day raw" in surface.distinct_actors_unavailable_reason

    async def test_window_starting_exactly_on_the_boundary_is_outside_retention(self) -> None:
        """`start > boundary` is strict: a window whose first day lands
        exactly on the boundary is treated as partially expired, not as
        still fully covered by the raw rows."""
        today = datetime.date(2026, 1, 31)
        boundary = today - datetime.timedelta(days=30)
        factory, session = _session_factory([("rest", 10, 10, 0, 5, None, None, None)])

        summary = await reads.read_summary(
            factory,
            tenant_id=_TENANT,
            start=boundary,
            end=boundary + datetime.timedelta(days=1),
            retention_days=30,
            today=today,
        )

        assert session.execute.await_count == 1
        assert summary.surfaces[0].distinct_actors is None

    async def test_no_rollup_rows_skips_the_distinct_actor_query_even_inside_retention(self) -> None:
        factory, session = _session_factory([])

        summary = await reads.read_summary(
            factory,
            tenant_id=_TENANT,
            start=datetime.date(2026, 1, 20),
            end=datetime.date(2026, 1, 25),
            retention_days=30,
            today=datetime.date(2026, 1, 31),
        )

        assert session.execute.await_count == 1
        assert summary.surfaces == ()


# ---------------------------------------------------------------------------
# read_daily_series — no zero-filling, and the surface filter
# ---------------------------------------------------------------------------


class TestReadDailySeries:
    async def test_maps_rows_without_inventing_missing_days(self) -> None:
        day = datetime.date(2026, 1, 3)
        factory, _ = _session_factory([(day, SURFACE_REST, 10, 9, 1, 4, 50, 90, 120)])

        result = await reads.read_daily_series(factory, tenant_id=_TENANT, start=_START, end=_END)

        assert len(result) == 1
        assert result[0].day == day
        assert result[0].surface == SURFACE_REST
        assert result[0].p95_ms == 90

    async def test_unknown_surface_raises_value_error(self) -> None:
        factory, _ = _session_factory([])

        with pytest.raises(ValueError, match="unknown surface"):
            await reads.read_daily_series(factory, tenant_id=_TENANT, start=_START, end=_END, surface="carrier-pigeon")

    async def test_surface_filter_narrows_rows_fetched_for_every_surface(self) -> None:
        day = datetime.date(2026, 1, 3)
        factory, _ = _session_factory(
            [
                (day, SURFACE_REST, 10, 9, 1, 4, 50, 90, 120),
                (day, SURFACE_MCP, 3, 3, 0, 2, 20, 40, 60),
            ]
        )

        result = await reads.read_daily_series(factory, tenant_id=_TENANT, start=_START, end=_END, surface=SURFACE_MCP)

        assert len(result) == 1
        assert result[0].surface == SURFACE_MCP


# ---------------------------------------------------------------------------
# read_tool_rankings / read_capability_rankings — mapping + limit clamp
# ---------------------------------------------------------------------------


class TestReadToolRankings:
    async def test_maps_rows(self) -> None:
        factory, _ = _session_factory([("query_claims", 500, 490, 10, 80, 45)])

        result = await reads.read_tool_rankings(factory, tenant_id=_TENANT, start=_START, end=_END)

        assert len(result) == 1
        assert result[0].tool == "query_claims"
        assert result[0].calls == 500
        assert result[0].worst_daily_p95_ms == 45

    async def test_limit_is_clamped_to_the_ranking_ceiling(self) -> None:
        factory, session = _session_factory([])

        await reads.read_tool_rankings(
            factory, tenant_id=_TENANT, start=_START, end=_END, limit=reads.MAX_RANKING_LIMIT + 500
        )

        params = session.execute.await_args.args[1]
        assert params["limit"] == reads.MAX_RANKING_LIMIT


class TestReadCapabilityRankings:
    async def test_maps_rows(self) -> None:
        cap_id = uuid.uuid4()
        factory, _ = _session_factory([(cap_id, 30, 6)])

        result = await reads.read_capability_rankings(factory, tenant_id=_TENANT, start=_START, end=_END)

        assert len(result) == 1
        assert result[0].capability_id == cap_id
        assert result[0].calls == 30
        assert result[0].actor_days == 6

    async def test_limit_is_clamped_to_the_ranking_ceiling(self) -> None:
        factory, session = _session_factory([])

        await reads.read_capability_rankings(
            factory, tenant_id=_TENANT, start=_START, end=_END, limit=reads.MAX_RANKING_LIMIT + 500
        )

        params = session.execute.await_args.args[1]
        assert params["limit"] == reads.MAX_RANKING_LIMIT
