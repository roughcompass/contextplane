"""Latency budgets for the surfaces this layer publishes.

The two operations that run in a loop: reporting an observation, and reporting
feedback about a served answer. Both sit on somebody else's critical path -- a
CI job posting a workflow conclusion, an agent reporting that an item was wrong
-- and an operation slow enough to be worth skipping is an operation that stops
being reported, which quietly empties the evidence base the rest of this layer
reasons over.

The floored aggregate read has no budget here yet. It is an operator-initiated
read rather than a loop, and it cannot currently be measured through the app at
all: the claim-aging query names columns its table does not have, so the call
raises before any latency is attributable to it. A budget written against a
call that cannot complete would be a number nobody could fail.

**The budgets are ceilings, not targets.** Measured on a developer machine
ingest and feedback run in single-digit milliseconds; the ceilings sit an order
of magnitude above that, which leaves room for a slower shared runner while
still failing on a regression that changes the *shape* of an operation -- a
per-reference query that used to be one insert, or a scan that grows with the
ledger. A budget sixty times the measurement would report green the whole way
down.

p95 is nearest-rank over real requests through the app, not a mean: an average
hides exactly the tail a caller waiting on a synchronous write experiences.
"""

from __future__ import annotations

import datetime
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

pytestmark = [pytest.mark.perf, pytest.mark.slow]

_NOW = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_SOURCE_SYSTEM = "github-actions"

#: Samples per operation. Small enough to keep the suite quick, large enough
#: that a nearest-rank p95 is not simply the maximum.
_SAMPLES = 20

#: How many observations the ledger already holds when the measurement starts.
#: A per-row cost that only appears once a tenant has history would hide inside
#: a measurement taken against an empty table.
_LEDGER_DEPTH = 50

INGEST_P95_BUDGET_MS = 200.0
FEEDBACK_P95_BUDGET_MS = 200.0


def _p95(latencies_ms: list[float]) -> float:
    """Nearest-rank p95 -- an observation that really happened, not an interpolation."""
    ordered = sorted(latencies_ms)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def _report(label: str, latencies_ms: list[float]) -> str:
    return (
        f"{label}: p95={_p95(latencies_ms):.1f}ms "
        f"min={min(latencies_ms):.1f}ms max={max(latencies_ms):.1f}ms n={len(latencies_ms)}"
    )


async def _measure(operation: Any, samples: int = _SAMPLES) -> list[float]:
    """Time `samples` calls, discarding the first as warm-up.

    The first request through the app pays for connection setup and whatever
    the ORM compiles once; charging that to the budget would measure the
    harness rather than the operation.
    """
    latencies: list[float] = []
    await operation(0)
    for index in range(1, samples + 1):
        started = time.perf_counter()
        await operation(index)
        latencies.append((time.perf_counter() - started) * 1000)
    return latencies


@pytest_asyncio.fixture(loop_scope="module")
async def scale_point(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """One tenant, a declared source, a receipt to bind to, and a ledger with history."""
    slug = f"perf-fl-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        caller = harness.add_persona(slug, roles=["admin"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(caller)
            with patch_validator_for_actor(caller):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert whoami.status_code == 200, whoami.text
                tenant_id = uuid.UUID(whoami.json()["tenant_id"])
                actor_id = whoami.json()["actor_id"]

            source_id = uuid.uuid4()
            receipt_id = uuid.uuid4()
            receipt_item_id = f"workspace:perf:{uuid.uuid4().hex[:12]}"
            engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with factory() as session, session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO sync_sources "
                            "  (source_id, tenant_id, source_type, display_name, config, "
                            "   is_active, created_at, created_by) "
                            "VALUES (:sid, :tid, 'manual', 'perf-source', '{}'::jsonb, TRUE, :now, :actor)"
                        ),
                        {"sid": source_id, "tid": tenant_id, "now": _NOW, "actor": actor_id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO context_receipts "
                            "  (receipt_id, tenant_id, state, cacheable, resolved_at, requested_by) "
                            "VALUES (:r, :t, 'complete', FALSE, :now, :actor)"
                        ),
                        {"r": receipt_id, "t": tenant_id, "now": _NOW, "actor": str(actor_id)},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO context_receipt_items "
                            "  (item_row_id, receipt_id, receipt_item_id, block, source, item_key, "
                            "   trust, trust_source, classification) "
                            "VALUES (:row, :receipt, :iid, 'workspace', 'workspace', :key, "
                            "        'reported', 'workspace', 'internal')"
                        ),
                        {
                            "row": uuid.uuid4(),
                            "receipt": receipt_id,
                            "iid": receipt_item_id,
                            "key": "perf-entry",
                        },
                    )
            finally:
                await engine.dispose()

            with patch_validator_for_actor(caller):
                declared = await client.post(
                    "/v1/admin/memory-sources",
                    headers=bearer_headers(tenant_slug=slug),
                    json={
                        "source_id": str(source_id),
                        "authority_tier": "observer_extraction",
                        # High enough that the ceiling never trips mid-measurement:
                        # a 429 would be timed as a refusal rather than a write.
                        "ingest_ceiling": 100000,
                        "window_seconds": 3600,
                    },
                )
                assert declared.status_code == 201, declared.text

            point = {
                "client": client,
                "harness": harness,
                "caller": caller,
                "slug": slug,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "source_id": source_id,
                "receipt_id": receipt_id,
                "receipt_item_id": receipt_item_id,
            }

            # History, so a per-row cost has somewhere to show up.
            with patch_validator_for_actor(caller):
                for index in range(_LEDGER_DEPTH):
                    seeded = await client.post(
                        "/v1/signals",
                        headers=bearer_headers(tenant_slug=slug),
                        json=_signal_body(point, key=f"seed-{index}"),
                    )
                    assert seeded.status_code == 201, seeded.text

            yield point


def _signal_body(point: dict[str, Any], *, key: str) -> dict[str, Any]:
    return {
        "source_id": str(point["source_id"]),
        "source_system": _SOURCE_SYSTEM,
        "source_event_id": f"github:workflow_run:{key}",
        "producer_id": f"connector:{_SOURCE_SYSTEM}",
        "producer_type": "external",
        "idempotency_key": f"delivery-{key}",
        "classification": "internal",
        "schema_version": "external_signal.v1",
        "event_time": _NOW.isoformat(),
        "observed_time": _NOW.isoformat(),
        "references": [
            {
                "source_system": "github",
                "source_namespace": "acme/app",
                "kind": "pull_request",
                "external_id": f"pr-{key}",
                "classification": "internal",
                "external_authority": "github",
            }
        ],
        "payload": {"conclusion": "failure"},
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_reporting_an_observation_stays_within_budget(scale_point: dict[str, Any]) -> None:
    """Ingest is on a connector's critical path, and a connector that times out
    drops the observation rather than retrying forever."""
    client: httpx.AsyncClient = scale_point["client"]
    scale_point["harness"].configure_fetcher_for(scale_point["caller"])

    with patch_validator_for_actor(scale_point["caller"]):

        async def operation(index: int) -> None:
            response = await client.post(
                "/v1/signals",
                headers=bearer_headers(tenant_slug=scale_point["slug"]),
                json=_signal_body(scale_point, key=f"measure-{uuid.uuid4().hex[:10]}"),
            )
            assert response.status_code == 201, response.text

        latencies = await _measure(operation)

    assert _p95(latencies) < INGEST_P95_BUDGET_MS, _report("ingest", latencies)


@pytest.mark.asyncio(loop_scope="module")
async def test_reporting_feedback_stays_within_budget(scale_point: dict[str, Any]) -> None:
    """Feedback is reported by whoever just received an answer, synchronously,
    while they are still looking at it."""
    client: httpx.AsyncClient = scale_point["client"]
    scale_point["harness"].configure_fetcher_for(scale_point["caller"])

    with patch_validator_for_actor(scale_point["caller"]):

        async def operation(index: int) -> None:
            response = await client.post(
                "/v1/context/feedback",
                headers=bearer_headers(tenant_slug=scale_point["slug"]),
                json={
                    "kind": "item_specific",
                    "rating": "relevant",
                    "reporter_id": str(scale_point["actor_id"]),
                    "reporter_type": "human",
                    "idempotency_key": f"fb-{uuid.uuid4().hex[:12]}",
                    "receipt_id": str(scale_point["receipt_id"]),
                    "receipt_item_id": scale_point["receipt_item_id"],
                },
            )
            assert response.status_code in (200, 201), response.text

        latencies = await _measure(operation)

    assert _p95(latencies) < FEEDBACK_P95_BUDGET_MS, _report("feedback", latencies)
