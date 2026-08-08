"""In-process MCP tool latency against a representative claim corpus.

This is the CI-runnable boundary the SSE transport cannot provide: a call starts at
``FastMCP.call_tool`` and includes schema validation, the registry's tool wrapper,
the real claim service, JSON serialisation, and Postgres work. It deliberately excludes
SSE, network transit, and OIDC/entitlement I/O; those belong to operational transport
metrics rather than to this deterministic service-boundary gate.

The corpus is smaller than the one-million-claim product ceiling on purpose. Fifty
thousand claims make subject/predicate filtering and namespace-ranked retrieval do real
indexed work while keeping this release-pipeline test bounded. The ceiling itself is
covered by ``test_perf_claim_scale.py``.
"""

from __future__ import annotations

import datetime
import json
import statistics
import time
import uuid
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.api.mcp import context
from contextplane.api.mcp.server import create_contextplane_mcp_server
from contextplane.config import Settings
from contextplane.embedding.stub import StubEmbedder
from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.memory.claim_ontology import seed_ontology
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.retrieval.embedding_drain import drain_outbox
from contextplane.service.retrieval.embedding_index import enqueue_many, index_text
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock
from tests.perf.memory_fixtures import raw_connection, seed_scale_point

pytestmark = [pytest.mark.perf, pytest.mark.slow]

_NOW = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.UTC)

TOTAL_CLAIMS = 50_000
ENTITY_COUNT = 1_000
SEARCH_NAMESPACE_CLAIMS = 1_000

ASSERT_P95_BUDGET_MS = 250.0
QUERY_P95_BUDGET_MS = 120.0
SEARCH_P95_BUDGET_MS = 250.0

WARMUP = 5
SAMPLES = 40


def _p95(latencies_ms: list[float]) -> float:
    """Return the nearest-rank p95, which is an observation rather than interpolation."""
    ordered = sorted(latencies_ms)
    return ordered[max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))]


def _report(label: str, latencies_ms: list[float]) -> str:
    return (
        f"{label}: n={len(latencies_ms)} "
        f"mean={statistics.mean(latencies_ms):.1f}ms "
        f"median={statistics.median(latencies_ms):.1f}ms "
        f"p95={_p95(latencies_ms):.1f}ms "
        f"max={max(latencies_ms):.1f}ms"
    )


def _json_payload(result: Any) -> Any:
    """Decode the first text block across the two FastMCP return shapes we support."""
    content = result[0] if isinstance(result, tuple) else result
    assert content, "FastMCP returned no content blocks"
    text_payload = content[0].text
    assert isinstance(text_payload, str), "FastMCP returned a non-text tool result"
    return json.loads(text_payload)


async def _index_search_namespace(
    factory: async_sessionmaker[AsyncSession],
    *,
    embedder: StubEmbedder,
    settings: Settings,
    tenant_id: uuid.UUID,
) -> None:
    """Put the search slice through the shared embedding outbox and drain."""
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    text(
                        "SELECT claim_id, predicate, value_jsonb AS value "
                        "FROM memory_claims "
                        "WHERE owning_tenant_id = :tid AND namespace = 'perf/hot'"
                    ),
                    {"tid": tenant_id},
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == SEARCH_NAMESPACE_CLAIMS

    queued: list[dict[str, Any]] = []
    for row in rows:
        body = index_text(str(row["predicate"]), row["value"])
        queued.append(
            {
                "tenant_id": tenant_id,
                "target_type": "claim",
                "target_id": row["claim_id"],
                "text_to_embed": body,
                "chunk_plan": [{"index": 0, "text": body, "start": 0, "end": len(body.split())}],
            }
        )
    async with factory() as session, session.begin():
        await enqueue_many(session, rows=queued, now=_NOW)

    # One drain invocation handles one configured batch. Keep the loop bounded so a
    # broken drain cannot turn a performance test into a hang.
    for _ in range(10):
        async with factory() as session:
            pending = int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM embedding_outbox " "WHERE tenant_id = :tid AND target_type = 'claim'"
                        ),
                        {"tid": tenant_id},
                    )
                ).scalar_one()
            )
        if pending == 0:
            break
        await drain_outbox(factory, embedder, settings)

    async with factory() as session:
        indexed = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM embeddings "
                        "WHERE tenant_id = :tid AND target_type = 'claim' "
                        "AND model_id = :model"
                    ),
                    {"tid": tenant_id, "model": embedder.model_version},
                )
            ).scalar_one()
        )
    assert indexed == SEARCH_NAMESPACE_CLAIMS, f"indexed {indexed} of {SEARCH_NAMESPACE_CLAIMS} search claims"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mcp_scale_point(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """Build one real service graph over a guarded, representative scale point."""
    conn = await raw_connection(pg_container)
    try:
        seeded = await seed_scale_point(
            conn,
            total_claims=TOTAL_CLAIMS,
            entity_count=ENTITY_COUNT,
            namespace_claims=SEARCH_NAMESPACE_CLAIMS,
        )
    finally:
        await conn.close()

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FakeClock(_NOW)
    embedder = StubEmbedder(dim=384)
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
        outbox_batch_size=SEARCH_NAMESPACE_CLAIMS,
    )

    try:
        await seed_ontology(GlobalVocabularyService(factory, clock=clock))
        await _index_search_namespace(
            factory,
            embedder=embedder,
            settings=settings,
            tenant_id=seeded["tenant_id"],
        )

        services = SimpleNamespace(
            claims=ClaimService(factory, clock=clock),
            claim_serving=ClaimServingService(factory, clock=clock),
            embedder=embedder,
        )
        app = SimpleNamespace(state=SimpleNamespace(services=services))
        server = create_contextplane_mcp_server(
            retrieval=MagicMock(),
            catalog=MagicMock(),
            session_factory=factory,
            workspace_service=MagicMock(),
            clock=clock,
        )
        tenant = TenantContext(
            tenant_id=seeded["tenant_id"],
            actor_id=seeded["actor_id"],
            roles=["producer"],
            oidc_subject="mcp-perf",
        )
        yield {**seeded, "app": app, "server": server, "tenant": tenant}
    finally:
        await engine.dispose()


async def _measure(
    scale_point: dict[str, Any],
    *,
    tool: str,
    arguments: Callable[[int], dict[str, Any]],
    validate: Callable[[Any], None],
) -> list[float]:
    """Measure calls after warmup while replacing only external auth I/O."""
    app_token = context._request_app.set(scale_point["app"])
    resolver = AsyncMock(return_value=scale_point["tenant"])
    try:
        with patch("contextplane.api.mcp.context._resolve_tenant", resolver):
            for index in range(WARMUP):
                result = await scale_point["server"].call_tool(tool, arguments(index))
                validate(_json_payload(result))

            latencies: list[float] = []
            for index in range(SAMPLES):
                started = time.perf_counter()
                result = await scale_point["server"].call_tool(tool, arguments(WARMUP + index))
                latencies.append((time.perf_counter() - started) * 1000.0)
                # Decoding is deliberately outside the interval: the tool has already
                # serialised its response; this checks that a fast error/empty path
                # cannot satisfy the budget.
                validate(_json_payload(result))
    finally:
        context._request_app.reset(app_token)
    return latencies


@pytest.mark.asyncio(loop_scope="module")
async def test_assert_claim_p95_is_within_staging_write_budget(
    mcp_scale_point: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The staging-write boundary: a claim asserted over MCP must land as fast
    as one asserted over REST, because an agent that finds the memory surface
    slower than the catalog surface will route around it."""

    def validate(payload: Any) -> None:
        assert isinstance(payload, dict)
        assert payload["status"] == "staged"
        assert payload["subject_entity_id"] == str(mcp_scale_point["probe_entity"])

    latencies = await _measure(
        mcp_scale_point,
        tool="assert_claim",
        arguments=lambda index: {
            "subject_reference": str(mcp_scale_point["probe_entity"]),
            "predicate": "owned_by_team",
            "value": f"mcp-perf-{index}",
            "evidence": [{"kind": "session_event", "ref": f"mcp-perf-event-{index}"}],
        },
        validate=validate,
    )
    with capsys.disabled():
        print(f"\n  {_report('MCP assert_claim @ 50k claims', latencies)}")
    assert _p95(latencies) < ASSERT_P95_BUDGET_MS, _report("assert_claim", latencies)


@pytest.mark.asyncio(loop_scope="module")
async def test_query_claims_p95_is_within_structured_query_budget(
    mcp_scale_point: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The named-lookup path: subject plus predicate, served from an index.
    This is the cheapest read the claim surface offers, so it sets the floor
    the ranked-retrieval budget below is measured against."""

    def validate(payload: Any) -> None:
        assert isinstance(payload, list) and payload
        assert all(item["subject_entity_id"] == str(mcp_scale_point["probe_entity"]) for item in payload)
        assert all(item["predicate"] == "owned_by_team" for item in payload)

    latencies = await _measure(
        mcp_scale_point,
        tool="query_claims",
        arguments=lambda _index: {
            "subject_entity_id": str(mcp_scale_point["probe_entity"]),
            "predicate": "owned_by_team",
            "persona": "agent",
            "limit": 10,
        },
        validate=validate,
    )
    with capsys.disabled():
        print(f"\n  {_report('MCP query_claims @ 50k claims', latencies)}")
    assert _p95(latencies) <= QUERY_P95_BUDGET_MS, _report("query_claims", latencies)


@pytest.mark.asyncio(loop_scope="module")
async def test_search_claims_p95_is_within_semantic_retrieval_budget(
    mcp_scale_point: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Ranked retrieval: hybrid scoring over a candidate set, so its budget is
    necessarily looser than the named lookup above. Held separately because a
    regression here reads as "search got slow", not "claims got slow"."""

    def validate(payload: Any) -> None:
        assert isinstance(payload, list) and payload

    latencies = await _measure(
        mcp_scale_point,
        tool="search_claims",
        arguments=lambda index: {
            "q": f"owned by team {index}",
            "namespace_prefix": "perf/hot",
            "persona": "agent",
            "top_k": 10,
        },
        validate=validate,
    )
    with capsys.disabled():
        print(f"\n  {_report('MCP search_claims @ 1k indexed claims', latencies)}")
    assert _p95(latencies) <= SEARCH_P95_BUDGET_MS, _report("search_claims", latencies)
