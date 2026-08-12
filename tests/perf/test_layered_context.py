"""Latency budgets for the surfaces this layer publishes.

Four operations an agent does in a loop: resolve context, append a checkpoint,
resume from an external reference, and read a receipt. If any of them is slow
enough to be worth avoiding, the reliability work around them stops mattering --
a caller that skips the receipt read because it costs 400ms has an unaudited
answer, and one that skips resume starts work that already exists.

**The budgets are ceilings, not targets, and they are deliberately not generous.**
Measured on a developer machine the four operations run at roughly 12ms, 7ms,
7ms and 3ms; the ceilings are an order of magnitude above that, which leaves
room for a slower, noisier shared runner while still failing on a regression
that changes the *shape* of an operation -- an N+1 that appears once a task
grows a chain, or a per-request scan that used to be per-arm. A budget sixty
times the measurement, which is where these started, would have caught nothing
short of a catastrophe and reported green the whole way down.

p95 is nearest-rank over real requests through the app, not a mean: an average
hides exactly the tail an agent waiting on a synchronous call experiences.
"""

from __future__ import annotations

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

_REF = ("github", "acme/app", "pull_request", "9001")

#: How many checkpoints the task carries. Enough that a per-checkpoint cost
#: would show up in the budgets below rather than hiding inside a single-row
#: measurement.
_CHAIN_DEPTH = 25

#: Samples per operation. Small enough to keep the suite quick, large enough
#: that a nearest-rank p95 is not simply the maximum.
_SAMPLES = 20

RESOLVE_P95_BUDGET_MS = 150.0
APPEND_P95_BUDGET_MS = 150.0
RESUME_P95_BUDGET_MS = 150.0
RECEIPT_LOOKUP_P95_BUDGET_MS = 100.0


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
    """One tenant with a task carrying a real chain and a bound reference."""
    slug = f"perf-lc-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        caller = harness.add_persona(slug, roles=["producer", "consumer"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(caller)
            with patch_validator_for_actor(caller):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert whoami.status_code == 200, whoami.text
                tenant_id = uuid.UUID(whoami.json()["tenant_id"])
                actor_id = whoami.json()["actor_id"]

            intent_id, reference_id = uuid.uuid4(), uuid.uuid4()
            engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with factory() as session, session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO intent_participant_grants "
                            "(tenant_id, intent_id, actor_id, role, granted_by, granted_at, expires_at, "
                            " resolver_version) "
                            "VALUES (:t, :task, :actor, 'owner', 'bootstrap', now() - interval '1 hour', "
                            "        NULL, 'explicit/v1')"
                        ),
                        {"t": tenant_id, "task": intent_id, "actor": str(actor_id)},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO context_external_references "
                            "(reference_id, tenant_id, source_system, source_namespace, kind, external_id, "
                            " classification, external_authority, collision_key) "
                            "VALUES (:rid, :t, :sys, :ns, :kind, :eid, 'internal', 'github', :ckey)"
                        ),
                        {
                            "rid": reference_id,
                            "t": tenant_id,
                            "sys": _REF[0],
                            "ns": _REF[1],
                            "kind": _REF[2],
                            "eid": _REF[3],
                            "ckey": "|".join(_REF),
                        },
                    )

                with patch_validator_for_actor(caller):
                    for index in range(_CHAIN_DEPTH):
                        appended = await client.post(
                            f"/v1/intents/{intent_id}/checkpoints",
                            headers={**bearer_headers(tenant_slug=slug), "Idempotency-Key": f"seed-{index}"},
                            json={"goal": f"step {index}", "next_action": "carry on"},
                        )
                        assert appended.status_code in (200, 201), appended.text
                        async with factory() as session, session.begin():
                            await session.execute(
                                text(
                                    "INSERT INTO context_reference_bindings "
                                    "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                                    "VALUES (:bid, :t, :rid, 'intent_checkpoint', :cid, now())"
                                ),
                                {
                                    "bid": uuid.uuid4(),
                                    "t": tenant_id,
                                    "rid": reference_id,
                                    "cid": uuid.UUID(appended.json()["checkpoint_id"]),
                                },
                            )
            finally:
                await engine.dispose()

            yield {
                "client": client,
                "harness": harness,
                "caller": caller,
                "slug": slug,
                "tenant_id": tenant_id,
                "intent_id": intent_id,
            }


def _headers(scale_point: dict[str, Any]) -> dict[str, str]:
    return bearer_headers(tenant_slug=scale_point["slug"])


@pytest.mark.asyncio(loop_scope="module")
async def test_context_resolve_p95_is_within_budget(scale_point: dict[str, Any]) -> None:
    """Four arms, assembled, labelled and receipted, in one synchronous call."""
    client = scale_point["client"]

    async def _resolve(index: int) -> None:
        with patch_validator_for_actor(scale_point["caller"]):
            resp = await client.post(
                "/v1/context/resolve", headers=_headers(scale_point), json={"query": f"deployment {index}"}
            )
        assert resp.status_code == 200, resp.text

    latencies = await _measure(_resolve)

    assert _p95(latencies) < RESOLVE_P95_BUDGET_MS, _report("context.resolve", latencies)


@pytest.mark.asyncio(loop_scope="module")
async def test_checkpoint_append_p95_does_not_grow_with_the_chain(scale_point: dict[str, Any]) -> None:
    """Appending takes a task lock and reads the head. Both are O(1) by design;
    a budget met against a 25-deep chain is what says so."""
    client = scale_point["client"]

    async def _append(index: int) -> None:
        with patch_validator_for_actor(scale_point["caller"]):
            resp = await client.post(
                f"/v1/tasks/{scale_point['intent_id']}/checkpoints",
                headers={**_headers(scale_point), "Idempotency-Key": f"perf-{index}"},
                json={"goal": f"measured step {index}", "next_action": "carry on"},
            )
        assert resp.status_code in (200, 201), resp.text

    latencies = await _measure(_append)

    assert _p95(latencies) < APPEND_P95_BUDGET_MS, _report("checkpoint.append", latencies)


@pytest.mark.asyncio(loop_scope="module")
async def test_bounded_resume_p95_is_within_budget(scale_point: dict[str, Any]) -> None:
    """Resume is bounded, so its cost must not track the chain it resumes from.
    Measured against a task with far more checkpoints than the bound returns."""
    client = scale_point["client"]

    async def _resume(_index: int) -> None:
        with patch_validator_for_actor(scale_point["caller"]):
            resp = await client.post(
                "/v1/context/resume",
                headers=_headers(scale_point),
                json={"references": [list(_REF)]},
            )
        assert resp.status_code == 200, resp.text

    latencies = await _measure(_resume)

    assert _p95(latencies) < RESUME_P95_BUDGET_MS, _report("context.resume", latencies)


@pytest.mark.asyncio(loop_scope="module")
async def test_receipt_lookup_by_reference_p95_is_within_budget(scale_point: dict[str, Any]) -> None:
    """The read an auditor makes, and the one an agent makes to check its own
    last answer. Cheap enough that nobody skips it."""
    client = scale_point["client"]

    async def _lookup(_index: int) -> None:
        with patch_validator_for_actor(scale_point["caller"]):
            resp = await client.get(
                "/v1/receipts/by-reference",
                params={
                    "source_system": _REF[0],
                    "source_namespace": _REF[1],
                    "kind": _REF[2],
                    "external_id": _REF[3],
                },
                headers=_headers(scale_point),
            )
        assert resp.status_code == 200, resp.text

    latencies = await _measure(_lookup)

    assert _p95(latencies) < RECEIPT_LOOKUP_P95_BUDGET_MS, _report("receipts.by_reference", latencies)
