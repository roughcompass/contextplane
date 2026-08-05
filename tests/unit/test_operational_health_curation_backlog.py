"""The loop's curation step, on the operator console.

Two readings close a gap the vision names directly: a curator queue that
backs up, and a promotion proposal nobody has reviewed, are both silent
failures today -- nothing pages on them, and the only way to notice is to
open the curation surface and count. This is the console-level view of both,
answered the same way every other cluster-scope reading here is: counted
from the table at read time, so it is correct regardless of which replica
answers.

The response shape is deliberately unchanged. Both readings are new entries
in the existing `queues` list, not a new top-level field -- a client reading
this payload today does not need to change to keep working tomorrow.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from registry.api.auth.context import get_tenant_context
from registry.api.routers import admin_operational_health
from registry.service.platform.operational_health import (
    _QUEUE_COUNTS,
    OperationalHealth,
    collect_operational_health,
)
from registry.types import TenantContext

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


def _session_factory(
    *,
    counts: list[int] | None = None,
    oldest_open_proposal_at: datetime.datetime | None = None,
    fail_after: int | None = None,
) -> MagicMock:
    """One mocked value per `_QUEUE_COUNTS` entry (`scalar_one`), then the
    oldest-open-proposal age query (`scalar_one_or_none`).

    `fail_after` makes the call at that position (1-indexed) raise instead of
    returning a value -- used to pin that an unreadable query renders as
    `None` at exactly the reading it belongs to, not to every reading.
    """
    values = list(counts if counts is not None else [0] * len(_QUEUE_COUNTS))
    session = AsyncMock()
    calls = 0

    async def execute(*_a: object, **_kw: object) -> MagicMock:
        nonlocal calls
        calls += 1
        if fail_after is not None and calls == fail_after:
            raise RuntimeError("relation does not exist")
        result = MagicMock()
        if calls <= len(values):
            result.scalar_one = MagicMock(return_value=values[calls - 1])
        else:
            result.scalar_one_or_none = MagicMock(return_value=oldest_open_proposal_at)
        return result

    session.execute = execute
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


async def _collect(**kwargs: object) -> OperationalHealth:
    return await collect_operational_health(_session_factory(**kwargs), now=_NOW)  # type: ignore[arg-type]


# --- curation queue backlog -----------------------------------------------


@pytest.mark.asyncio
async def test_curation_queue_backlog_is_a_cluster_scoped_gauge() -> None:
    counts = [0] * len(_QUEUE_COUNTS)
    counts[-1] = 17
    health = await _collect(counts=counts)

    reading = next(r for r in health.queues if r.key == "curation_queue_backlog")
    assert reading.value == 17.0
    assert reading.scope == "cluster"
    assert reading.kind == "gauge"
    assert reading.instance is None


@pytest.mark.asyncio
async def test_curation_queue_backlog_reports_null_when_the_table_is_unreadable() -> None:
    """Unreadable, not empty. The same distinction every other queue reading
    here makes, pinned for this one specifically since it is the newest."""
    health = await _collect(fail_after=len(_QUEUE_COUNTS))
    reading = next(r for r in health.queues if r.key == "curation_queue_backlog")
    assert reading.value is None


# --- oldest open proposal age ----------------------------------------------


@pytest.mark.asyncio
async def test_oldest_open_proposal_age_is_now_minus_its_created_at() -> None:
    created_at = _NOW - datetime.timedelta(hours=3)
    health = await _collect(oldest_open_proposal_at=created_at)

    reading = next(r for r in health.queues if r.key == "oldest_open_proposal_age_seconds")
    assert reading.value == pytest.approx(3 * 3600)
    assert reading.scope == "cluster"
    assert reading.kind == "gauge"
    assert reading.instance is None


@pytest.mark.asyncio
async def test_no_open_proposals_reads_as_a_healthy_zero_not_unreadable() -> None:
    """An empty review queue is not stale. Reporting it as unreadable would be
    the same word this page uses for a broken query, and an operator would
    have no way to tell the two apart."""
    health = await _collect(oldest_open_proposal_at=None)
    reading = next(r for r in health.queues if r.key == "oldest_open_proposal_age_seconds")
    assert reading.value == 0.0


@pytest.mark.asyncio
async def test_an_unreadable_oldest_proposal_query_reports_null() -> None:
    health = await _collect(fail_after=len(_QUEUE_COUNTS) + 1)
    reading = next(r for r in health.queues if r.key == "oldest_open_proposal_age_seconds")
    assert reading.value is None


# --- the response shape does not change ------------------------------------


@pytest.mark.asyncio
async def test_both_new_readings_land_in_queues_not_a_new_top_level_field() -> None:
    health = await _collect()
    keys = {r.key for r in health.queues}
    assert {"curation_queue_backlog", "oldest_open_proposal_age_seconds"} <= keys
    assert not any(r.key.startswith("curation") or "proposal" in r.key for r in health.data_quality)


# --- over the wire ----------------------------------------------------------


def _app(*, counts: list[int] | None = None, oldest_open_proposal_at: datetime.datetime | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_operational_health.router)
    app.state.session_factory = _session_factory(counts=counts, oldest_open_proposal_at=oldest_open_proposal_at)
    ctx = TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=frozenset({"admin"}))
    app.dependency_overrides[get_tenant_context] = lambda: ctx
    return app


@pytest.mark.asyncio
async def test_the_admin_endpoint_serves_both_new_readings() -> None:
    created_at = _NOW - datetime.timedelta(minutes=90)
    app = _app(oldest_open_proposal_at=created_at)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/v1/admin/operational-health")
    assert response.status_code == 200

    by_key = {r["key"]: r for r in response.json()["queues"]}
    assert "curation_queue_backlog" in by_key
    assert "oldest_open_proposal_age_seconds" in by_key
    for reading in (by_key["curation_queue_backlog"], by_key["oldest_open_proposal_age_seconds"]):
        assert reading["scope"] == "cluster"
        assert reading["kind"] == "gauge"
        assert reading["instance"] is None
