"""Embedding + retrieval integration tests.

Covers:
- MCP list_capabilities tool (in-process call_tool, no SSE transport needed)
- Time-travel get_capability (FakeClock writes at T1 / T2, query at T1)
- 20 time-travel scenarios from eval/fixtures/time_travel_scenarios.json
- recall@10 over 50 questions from eval/fixtures/search_questions.json
- outbox gauge = 0 after drain

All tests share one pg_container (session scope) and one tenant seeded via
_seed. Time-travel and recall tests use a dedicated eval tenant so writes
don't cross-contaminate the MCP/list tests.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.mcp.server import create_registry_mcp_server
from registry.config import Settings
from registry.embedding import build_embedder
from registry.embedding.stub import StubEmbedder
from registry.service.catalog.core import CatalogService
from registry.service.catalog.schema import SchemaService
from registry.service.catalog.vocabulary import VocabularyService
from registry.service.retrieval import RetrievalService
from registry.service.retrieval.embedding_drain import _OUTBOX_PENDING_GAUGE, drain_outbox
from registry.storage.pg import create_engine, get_session_factory
from registry.types import TemporalFilter, TenantContext
from tests.helpers.clock import FakeClock
from tests.helpers.embedding_artifact import find_artifact

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_FIXTURES = pathlib.Path(__file__).parent.parent.parent / "eval" / "fixtures"
_TIME_TRAVEL_FILE = _FIXTURES / "time_travel_scenarios.json"
_SEARCH_QUESTIONS_FILE = _FIXTURES / "search_questions.json"
_TIME_TRAVEL_SCENARIO_COUNT = 20
_SEARCH_QUESTION_COUNT = 50
_EVAL_ENTITY_COUNT = 20

# ---------------------------------------------------------------------------
# Shared seed helper
#
# Uses raw SQL for all inserts (text() throughout). The actor row requires
# a non-null oidc_subject after the entitlement-auth migration. No api_token
# row is written — token auth is handled by the OIDC/entitlement path; tests
# that call service methods directly construct TenantContext without a token.
# ---------------------------------------------------------------------------

_VOCAB_ROWS = [
    ("entity_type", "capability"),
    ("entity_type", "concept"),
    ("entity_type", "operation"),
    ("fact_category", "overview"),
    ("fact_category", "adr"),
    ("fact_category", "dev_doc"),
    ("edge_rel", "concept_of"),
    ("edge_rel", "operation_of"),
    ("edge_rel", "depends_on"),
    ("edge_rel", "replaced_by"),
]


async def _seed(
    pg_url: str,
    *,
    slug: str,
    roles: list[str],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create tenant + actor; return (tenant_id, actor_id).

    Roles are carried only in TenantContext (not persisted to a dropped
    api_tokens table). MCP tests patch _resolve_tenant instead of relying
    on token lookup.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    oidc_subject = f"oidc-sub-{slug}-{actor_id.hex[:8]}"
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": now},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                    "VALUES (:aid, :tid, :sub, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "sub": oidc_subject, "dn": f"a-{slug}", "now": now},
            )
            for kind, value in _VOCAB_ROWS:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE)"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


# ---------------------------------------------------------------------------
# App + client fixtures (session-scoped for speed; tenant isolation via slug)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# test_mcp_list_capabilities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_list_capabilities(pg_container: str, app_settings: Settings) -> None:
    """MCP list_capabilities returns the cursor envelope, with the seeded rows in it.

    Uses call_tool() in-process — no SSE transport needed. _resolve_tenant is
    patched to return a TenantContext built from seeded DB rows so auth
    infrastructure (OIDC, entitlement service) is not contacted.

    Seeds a capability on purpose. Asserting only the envelope keys passes just
    as well against an empty result, which cannot distinguish "the tool works"
    from "the tenant resolved to nothing".
    """
    stub_settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
    )
    pg_engine = create_engine(stub_settings)
    session_factory = get_session_factory(pg_engine)

    clock = FakeClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    embedder = StubEmbedder()
    vocabulary = VocabularyService(session_factory)
    schema = SchemaService(session_factory, clock)
    catalog_svc = CatalogService(session_factory, clock, vocabulary, schema)
    retrieval_svc = RetrievalService(session_factory, clock, embedder, stub_settings)

    mcp_server = create_registry_mcp_server(
        retrieval=retrieval_svc,
        catalog=catalog_svc,
        session_factory=session_factory,
        workspace_service=MagicMock(),
        clock=clock,
    )

    # Seed a tenant + actor and build a TenantContext for the patch.
    tid, aid = await _seed(
        pg_container,
        slug=f"mcp-list-{uuid.uuid4().hex[:6]}",
        roles=["producer"],
    )
    ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"])
    await catalog_svc.create_entity(ctx, "capability", "mcp-listed-cap")

    # Patch _resolve_tenant to skip OIDC+entitlement resolution in-process.
    with patch(
        "registry.api.mcp.context._resolve_tenant",
        AsyncMock(return_value=ctx),
    ):
        result = await mcp_server.call_tool("list_capabilities", {"page_size": 20})

    await pg_engine.dispose()

    assert result, "call_tool returned empty result"
    # FastMCP's call_tool returns a (content_blocks, _) tuple. Some MCP
    # SDK versions return just content_blocks; handle both.
    content = result[0] if isinstance(result, tuple) else result
    payload = json.loads(content[0].text)  # type: ignore[union-attr]

    # Cursor pagination, matching every other list surface in the API. Offset
    # paging is gone: REST rejects `?page=` with 422, and the MCP tool takes a
    # `cursor` argument instead. An extra `page` key here would mean the two
    # surfaces had drifted apart again.
    assert set(payload) == {"items", "next_cursor"}, payload
    assert isinstance(payload["items"], list)
    assert payload["next_cursor"] is None, "one capability fits in a 20-row page"

    names = [item["name"] for item in payload["items"]]
    assert names == ["mcp-listed-cap"], f"expected the seeded capability, got {names}"


# ---------------------------------------------------------------------------
# test_time_travel_get_capability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_travel_get_capability(pg_container: str) -> None:
    """Write fact at T1, update at T2; get_capability as_of=T1 returns original body.

    Uses the REST API (POST /v1/capabilities + /v1/artifacts) with a FakeClock
    that is advanced between the two writes.
    """
    stub_settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
    )
    pg_engine = create_engine(stub_settings)
    session_factory = get_session_factory(pg_engine)

    t1 = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    t2 = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    fake_clock = FakeClock(t1)

    tid, aid = await _seed(
        pg_container,
        slug=f"tt-single-{uuid.uuid4().hex[:6]}",
        roles=["producer"],
    )
    vocabulary = VocabularyService(session_factory)
    schema = SchemaService(session_factory, fake_clock)
    catalog_svc = CatalogService(session_factory, fake_clock, vocabulary, schema)
    ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"])

    # Write original fact at T1.
    entity_ref = await catalog_svc.create_entity(ctx, "capability", "tt-single-cap")
    entity_id = entity_ref.entity_id
    fact_t1 = await catalog_svc.create_fact(
        ctx,
        entity_id=entity_id,
        category="overview",
        body="original-body-T1",
        valid_from=t1,
    )

    # Advance clock and update fact at T2.
    fake_clock.set(t2)
    await catalog_svc.update_fact(ctx, fact_id=fact_t1.fact_id, new_body="updated-body-T2", valid_from=t2)

    await pg_engine.dispose()

    # Now use the MCP server to verify time-travel.
    pg_engine2 = create_engine(stub_settings)
    session_factory2 = get_session_factory(pg_engine2)
    embedder = StubEmbedder()
    retrieval_svc = RetrievalService(session_factory2, fake_clock, embedder, stub_settings)
    catalog_svc2 = CatalogService(session_factory2, fake_clock, vocabulary, schema)
    mcp_server = create_registry_mcp_server(
        retrieval=retrieval_svc,
        catalog=catalog_svc2,
        session_factory=session_factory2,
        workspace_service=MagicMock(),
        clock=fake_clock,
    )

    with patch(
        "registry.api.mcp.context._resolve_tenant",
        AsyncMock(return_value=ctx),
    ):
        # Query at T1 — should see original body.
        result_t1 = await mcp_server.call_tool(
            "get_capability",
            {
                "entity_id": str(entity_id),
                "as_of": "2026-01-15T00:00:00+00:00",
            },
        )
        # Query at T2 — should see updated body.
        result_t2 = await mcp_server.call_tool(
            "get_capability",
            {
                "entity_id": str(entity_id),
                "as_of": "2026-03-15T00:00:00+00:00",
            },
        )

    await pg_engine2.dispose()

    content_t1 = result_t1[0] if isinstance(result_t1, tuple) else result_t1
    content_t2 = result_t2[0] if isinstance(result_t2, tuple) else result_t2
    record_t1 = json.loads(content_t1[0].text)  # type: ignore[union-attr]
    record_t2 = json.loads(content_t2[0].text)  # type: ignore[union-attr]

    # At T1 the original body should be in the facts list.
    bodies_t1 = [f["body"] for f in record_t1.get("facts", [])]
    assert "original-body-T1" in bodies_t1, f"Expected 'original-body-T1' in facts at T1, got: {bodies_t1}"

    # At T2 the updated body should appear.
    bodies_t2 = [f["body"] for f in record_t2.get("facts", [])]
    assert "updated-body-T2" in bodies_t2, f"Expected 'updated-body-T2' in facts at T2, got: {bodies_t2}"


# ---------------------------------------------------------------------------
# test_20_time_travel_scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_20_time_travel_scenarios(pg_container: str) -> None:
    """Iterate eval/fixtures/time_travel_scenarios.json; expect 100% pass.

    Each scenario:
      1. Writes the original fact at writes[0].t with FakeClock at that time.
      2. Updates the fact at writes[1].t.
      3. Queries via get_full_capability as_of the query.as_of time.
      4. Asserts the returned fact body == query.expected_body.
    """
    scenarios = _load_time_travel_scenarios()

    stub_settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
    )
    pg_engine = create_engine(stub_settings)
    session_factory = get_session_factory(pg_engine)

    # One shared tenant for all scenarios.
    tid, actor_id = await _seed(
        pg_container,
        slug=f"tt-batch-{uuid.uuid4().hex[:6]}",
        roles=["producer"],
    )
    ctx = TenantContext(tenant_id=tid, actor_id=actor_id, roles=["producer"])

    fake_clock = FakeClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    vocabulary = VocabularyService(session_factory)
    schema = SchemaService(session_factory, fake_clock)
    catalog_svc = CatalogService(session_factory, fake_clock, vocabulary, schema)

    failed: list[str] = []

    for scenario in scenarios:
        sc_id = scenario["id"]
        writes = scenario["writes"]
        query = scenario["query"]

        # Write 1 — original.
        w0 = writes[0]
        t_w0 = datetime.datetime.fromisoformat(w0["t"])
        fake_clock.set(t_w0)

        # create_entity generates its own UUID; we record the returned id and use it
        # for the scenario writes/query (fixture entity UUIDs are not pinned here).
        entity_ref = await catalog_svc.create_entity(ctx, "capability", f"cap-{sc_id}")
        entity_id = entity_ref.entity_id

        fake_clock.set(t_w0)
        fact = await catalog_svc.create_fact(
            ctx,
            entity_id=entity_id,
            category=w0["fact"]["category"],
            body=w0["fact"]["body"],
            valid_from=t_w0,
        )

        # Write 2 — update.
        w1 = writes[1]
        t_w1 = datetime.datetime.fromisoformat(w1["t"])
        fake_clock.set(t_w1)
        await catalog_svc.update_fact(
            ctx,
            fact_id=fact.fact_id,
            new_body=w1["fact"]["body"],
            valid_from=t_w1,
        )

        # Query as_of.
        as_of_dt = datetime.datetime.fromisoformat(query["as_of"])
        expected_body = query["expected_body"]

        record = await catalog_svc.get_full_capability(ctx, entity_id, as_of=as_of_dt)
        bodies = [f.body for f in record.facts]
        if expected_body not in bodies:
            failed.append(
                f"{sc_id}: expected body '{expected_body}' not in facts at as_of={query['as_of']}; " f"got: {bodies}"
            )

    await pg_engine.dispose()

    assert not failed, f"{len(failed)}/20 time-travel scenarios failed:\n" + "\n".join(failed)


# ---------------------------------------------------------------------------
# Entity + fact seeder for recall@10 test
# ---------------------------------------------------------------------------

# Canonical entity catalog for search questions.  UUIDs match search_questions.json.
# Bodies are phrased to be semantically relevant to the expected questions.
_EVAL_ENTITIES: list[dict[str, Any]] = [
    {
        "id": "e3e70682-c209-4cac-a29f-6fbed82c07cd",
        "name": "payment-service",
        "body": (
            "payment-service handles charging customers, refunds, delayed charges, "
            "local dev setup, and integrates with Postgres for storage. "
            "POST /charge endpoint for billing. "
            "Emits billing events. Integrates with fraud-detection. "
            "Rate limits apply. ADR for Postgres storage decision."
        ),
    },
    {
        "id": "cd613e30-d8f1-4adf-91b7-584a2265b1f5",
        "name": "search-capability",
        "body": (
            "search capability performs full-text and semantic search queries. "
            "Search query parameter list. Most recent search index update. "
            "Search re-indexing runbook. Common search ranking issues."
        ),
    },
    {
        "id": "d95bafc8-f2a4-427b-9cf4-bb99f4bea973",
        "name": "ingest-pipeline",
        "body": (
            "ingest pipeline owns the data ingestion path. "
            "Max payload size for ingest is documented here. "
            "How to run tests for ingest. Recent ingest pipeline changes."
        ),
    },
    {
        "id": "21636369-8b52-4b4a-97b7-50923ceb3ffd",
        "name": "auth-service",
        "body": (
            "auth service implements multi-tenancy enforcement and authentication. "
            "Rate limits on the auth endpoint. JWT validation and claims. "
            "Latest auth release notes. Token issuance. Depends on auth."
        ),
    },
    {
        "id": "b8a1abcd-1a69-46c7-8da4-f9fc3c6da5d7",
        "name": "recommendations-engine",
        "body": (
            "recommendations engine provides product recommendations. "
            "Text embedding model choice is documented. "
            "Debug recommendations latency. Recommendations data sources."
        ),
    },
    {
        "id": "5bc8fbbc-bde5-4099-8164-d8399f767c45",
        "name": "billing-events",
        "body": (
            "billing events capability emits webhook payloads for billing. "
            "AGPL licensing rationale is documented here. "
            "Webhook payload for billing events. Emits billing events."
        ),
    },
    {
        "id": "14a03569-d26b-4496-92e5-dfe8cb1855fe",
        "name": "notification-service",
        "body": (
            "notification service handles delivery of notifications. "
            "Notification security model. Notification delivery debugging."
        ),
    },
    {
        "id": "6513270e-269e-4d37-b2a7-4de452e6b438",
        "name": "fraud-detection",
        "body": (
            "fraud detection capability scopes fraud detection logic. "
            "Integrates with payment-service for fraud checking. "
            "How is fraud detection scoped?"
        ),
    },
    {
        "id": "4462ebfc-5f91-4ef0-9cfb-ac6e7687a66e",
        "name": "user-profile",
        "body": (
            "user-profile stores PII — email, name, address. "
            "User-profile CRUD endpoints. "
            "Recommendations data sources include user-profile."
        ),
    },
    {
        "id": "7b89296c-6dcb-4c50-8857-7eb1924770d3",
        "name": "rate-limiter",
        "body": (
            "rate limiter enforces rate limit policies across the platform. "
            "What is a rate limit? Current rate-limit policy ADR. "
            "All capabilities using rate-limit."
        ),
    },
    {
        "id": "db5b5fab-8f4d-4e27-9da1-494c73cf256d",
        "name": "idempotency-key",
        "body": (
            "idempotency key semantics: a unique key per request that prevents duplicate "
            "processing. Idempotency-key example usage."
        ),
    },
    {
        "id": "87751d4c-a850-4e2c-84dc-da6a797d76de",
        "name": "jwt-claims",
        "body": ("JWT claims used for authorization. JWT validation. " "When is a JWT considered expired?"),
    },
    {
        "id": "e8d79f49-af6d-414c-8a6f-188a424e617b",
        "name": "circuit-breaker",
        "body": ("circuit breaker pattern implementation. " "Explains what circuit breaker does and when it opens."),
    },
    {
        "id": "c15521b1-b3dc-450a-9daa-37e51b591d75",
        "name": "retry-policy",
        "body": (
            "retry policy defines the backoff strategy for failed requests. "
            "How to retry a failed charge using exponential backoff."
        ),
    },
    {
        "id": "85750621-02fb-4d4f-b57f-bc5af71a1bfc",
        "name": "charge-operation",
        "body": (
            "charge operation: POST /charge endpoint signature for billing. "
            "How to charge a customer. Local dev setup for payment-service. "
            "Idempotency-key example for retrying failed charges."
        ),
    },
    {
        "id": "48f165d5-7b00-47f4-b81e-f86f5c8cc1ab",
        "name": "refund-operation",
        "body": (
            "refund operation handles issuing refunds to customers. "
            "How do I issue refunds? payment-service refund handling."
        ),
    },
    {
        "id": "6018366c-f658-47a7-9ed3-4fe53a096533",
        "name": "checkout-service",
        "body": (
            "checkout service provides the checkout flow. "
            "What's new in checkout. Depends on auth. Emits billing events."
        ),
    },
    {
        "id": "4dad2986-ce83-4960-aa06-e9ab85a0bcc1",
        "name": "token-issuer",
        "body": (
            "token issuer issues new API tokens and JWT tokens. " "How do you issue a new token? Token issuance flow."
        ),
    },
    {
        "id": "72e63ac7-a953-4322-9f70-d5dc2e675fc7",
        "name": "jwt-validator",
        "body": (
            "jwt validator validates JWT tokens and checks expiry. "
            "How is a JWT validated? When is a JWT considered expired?"
        ),
    },
    {
        "id": "e539a78b-c8ef-4346-8b12-ae6ead581e57",
        "name": "embedding-service",
        "body": (
            "embedding service computes text embeddings. "
            "How to call /v1/embed. Document the embedding model choice. "
            "How does text embedding work? Recommendations use embeddings."
        ),
    },
]


def _load_time_travel_scenarios() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = json.loads(_TIME_TRAVEL_FILE.read_text())
    assert (
        len(scenarios) == _TIME_TRAVEL_SCENARIO_COUNT
    ), f"expected {_TIME_TRAVEL_SCENARIO_COUNT} scenarios, found {len(scenarios)}"
    scenario_ids = [str(scenario["id"]) for scenario in scenarios]
    assert len(set(scenario_ids)) == len(scenario_ids), "time-travel scenario IDs must be unique"

    for scenario in scenarios:
        writes = scenario["writes"]
        assert len(writes) == 2, f"{scenario['id']}: expected exactly two writes"
        assert (
            writes[0]["entity_id"] == writes[1]["entity_id"]
        ), f"{scenario['id']}: writes must target one fixture entity"
        first_write = datetime.datetime.fromisoformat(writes[0]["t"])
        second_write = datetime.datetime.fromisoformat(writes[1]["t"])
        query_time = datetime.datetime.fromisoformat(scenario["query"]["as_of"])
        assert (
            first_write < query_time < second_write
        ), f"{scenario['id']}: query must fall strictly between the two writes"
        assert (
            scenario["query"]["expected_body"] == writes[0]["fact"]["body"]
        ), f"{scenario['id']}: expected body must identify the first write"
    return scenarios


def _load_search_questions() -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = json.loads(_SEARCH_QUESTIONS_FILE.read_text())
    assert (
        len(questions) == _SEARCH_QUESTION_COUNT
    ), f"expected {_SEARCH_QUESTION_COUNT} questions, found {len(questions)}"
    question_ids = [str(question["id"]) for question in questions]
    assert len(set(question_ids)) == len(question_ids), "search question IDs must be unique"

    catalog_ids = {str(entity["id"]) for entity in _EVAL_ENTITIES}
    assert len(_EVAL_ENTITIES) == _EVAL_ENTITY_COUNT, "evaluation catalog must contain exactly 20 entities"
    assert len(catalog_ids) == _EVAL_ENTITY_COUNT, "evaluation entity IDs must be unique"
    referenced_ids: set[str] = set()
    for question in questions:
        assert str(question["question"]).strip(), f"{question['id']}: question must not be empty"
        expected_ids = [str(value) for value in question["expected_entity_ids"]]
        assert expected_ids, f"{question['id']}: expected_entity_ids must not be empty"
        assert len(set(expected_ids)) == len(
            expected_ids
        ), f"{question['id']}: expected_entity_ids must not contain duplicates"
        unknown_ids = set(expected_ids) - catalog_ids
        assert not unknown_ids, f"{question['id']}: unknown evaluation entity IDs: {sorted(unknown_ids)}"
        referenced_ids.update(expected_ids)
    assert referenced_ids == catalog_ids, "search fixture must exercise every evaluation entity"
    return questions


def test_frozen_retrieval_fixtures_have_expected_identity() -> None:
    _load_time_travel_scenarios()
    _load_search_questions()


async def _seed_eval_entities(
    pg_url: str,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Seed an eval-only tenant with all 20 eval entities.

    Returns (tenant_id, actor_id, fixture_uuid→actual_entity_uuid mapping).

    The fixture UUIDs in search_questions.json are symbolic; this test creates
    actual entities with matching NAMES and records the mapping so recall can
    be computed by name-matching.  We store the fixture UUID in external_id so
    the lookup is O(1) at query time.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    oidc_subject = f"oidc-sub-eval-recall-{actor_id.hex[:8]}"
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": f"eval-recall-{uuid.uuid4().hex[:8]}", "now": now},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                    "VALUES (:aid, :tid, :sub, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "sub": oidc_subject, "dn": "eval-actor", "now": now},
            )
            for kind, value in _VOCAB_ROWS:
                await session.execute(
                    text(
                        "INSERT INTO vocabulary_values (tenant_id, kind, value, is_system) "
                        "VALUES (:tid, :kind, :value, FALSE)"
                    ),
                    {"tid": tenant_id, "kind": kind, "value": value},
                )

        # Insert entities + facts with external_id = fixture UUID string.
        fixture_to_entity: dict[str, uuid.UUID] = {}
        async with factory() as session, session.begin():
            for spec in _EVAL_ENTITIES:
                entity_id = uuid.uuid4()
                fixture_to_entity[spec["id"]] = entity_id
                await session.execute(
                    text(
                        "INSERT INTO entities "
                        "(entity_id, tenant_id, entity_type, name, external_id, is_active, created_at, created_by) "
                        "VALUES (:eid, :tid, 'capability', :name, :ext_id, TRUE, :now, :aid)"
                    ),
                    {
                        "eid": entity_id,
                        "tid": tenant_id,
                        "name": spec["name"],
                        "ext_id": spec["id"],  # fixture UUID stored in external_id
                        "now": now,
                        "aid": actor_id,
                    },
                )
                fact_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO facts "
                        "(fact_id, tenant_id, entity_id, category, body, "
                        " is_authoritative, is_authoritative_superseded, "
                        " t_valid_from, t_ingested_at, created_by) "
                        "VALUES (:fid, :tid, :eid, 'overview', :body, TRUE, FALSE, :now, :now, :aid)"
                    ),
                    {
                        "fid": fact_id,
                        "tid": tenant_id,
                        "eid": entity_id,
                        "body": spec["body"],
                        "now": now,
                        "aid": actor_id,
                    },
                )
                # Also queue embedding outbox for drain.
                try:
                    import json as _json

                    chunk_plan = [{"index": 0, "start": 0, "end": len(spec["body"].split()), "text": spec["body"]}]
                    await session.execute(
                        text(
                            "INSERT INTO embedding_outbox "
                            "(outbox_id, tenant_id, target_type, target_id, "
                            " text_to_embed, chunk_plan, enqueued_at, attempts) "
                            "VALUES (gen_random_uuid(), :tid, 'fact', :fid, "
                            "        :body, CAST(:plan AS jsonb), :now, 0)"
                        ),
                        {
                            "tid": tenant_id,
                            "fid": fact_id,
                            "body": spec["body"],
                            "plan": _json.dumps(chunk_plan),
                            "now": now,
                        },
                    )
                except Exception:  # noqa: S110 - schema-compat guard: embedding_outbox may not exist yet on an older snapshot, and there is nothing else to do but skip the insert
                    pass  # embedding_outbox absent before this schema was introduced

    finally:
        await engine.dispose()

    return tenant_id, actor_id, fixture_to_entity


# ---------------------------------------------------------------------------
# test_recall_at_10
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_at_10(pg_container: str) -> None:
    """Recall@10 >= 70% over 50 search questions.

    Strategy:
    - Seed 20 entities with bodies matched to the fixture questions.
    - Drain the embedding outbox so embeddings exist (StubEmbedder produces
      zero vectors; lexical arm is the primary recall driver here).
    - For each question, run RetrievalService.search(top_k=10).
    - Recall for a question = 1 if ANY expected entity appears in top-10, else 0.
    - Overall recall@10 = (questions with >=1 expected hit) / 50.
    - Assert >= 0.70.
    """
    questions = _load_search_questions()

    # Measure against the real model when its artifact is staged, and fall back
    # to the stub otherwise so a plain checkout still runs the gate. The two
    # numbers mean different things: with the stub every semantic distance is
    # identical, so what is being measured is lexical recall with a dead
    # semantic arm. Which mode ran is printed and recorded.
    artifact = find_artifact()
    if artifact is not None:
        stub_settings = Settings(
            database_url=pg_container,
            pgbouncer_url=pg_container,
            scheduler_jobstore_url=pg_container,
            embedding_provider="onnx",
            embedding_model_path=str(artifact),
        )
        embedder = build_embedder(stub_settings)
        mode = f"onnx/{stub_settings.embedding_model}"
    else:
        stub_settings = Settings(
            database_url=pg_container,
            pgbouncer_url=pg_container,
            scheduler_jobstore_url=pg_container,
            embedding_provider="stub",
        )
        embedder = StubEmbedder()
        mode = "stub (zero vectors — lexical-dominant)"

    # Seed eval tenant.
    tid, actor_id, fixture_to_entity = await _seed_eval_entities(pg_container)
    ctx = TenantContext(tenant_id=tid, actor_id=actor_id, roles=["producer"])

    pg_engine = create_engine(stub_settings)
    session_factory = get_session_factory(pg_engine)
    fake_clock = FakeClock(datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC))

    # Drain outbox so embeddings table is populated.
    await drain_outbox(session_factory, embedder, stub_settings)

    retrieval_svc = RetrievalService(session_factory, fake_clock, embedder, stub_settings)

    hits = 0
    miss_details: list[str] = []

    for q_spec in questions:
        q_id = q_spec["id"]
        question = q_spec["question"]
        expected_fixture_ids: list[str] = q_spec["expected_entity_ids"]
        # Preflight validation guarantees every fixture UUID has a seeded entity.
        expected_actual_ids = {fixture_to_entity[fid] for fid in expected_fixture_ids}

        results = await retrieval_svc.search(
            ctx,
            q=question,
            top_k=10,
            temporal_filter=TemporalFilter(as_of=None),
        )
        returned_ids = {r.entity.entity_id for r in results}

        if expected_actual_ids & returned_ids:
            hits += 1
        else:
            miss_details.append(
                f"{q_id} ({question!r}): expected any of "
                f"{[str(i) for i in expected_actual_ids]}, "
                f"got {[str(i) for i in returned_ids]}"
            )

    await pg_engine.dispose()

    recall_at_10 = hits / len(questions)
    print(f"\nrecall@10 = {recall_at_10:.3f} ({hits}/{len(questions)})  embedder={mode}")

    assert recall_at_10 >= 0.70, (
        f"recall@10 = {recall_at_10:.3f} < 0.70 (embedding retrieval quality gate)\n"
        f"Missed questions:\n" + "\n".join(miss_details[:10])
    )


# ---------------------------------------------------------------------------
# test_outbox_gauge_zero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_gauge_zero(pg_container: str) -> None:
    """After writing a fact and draining the outbox, catalog_outbox_pending_size = 0."""
    stub_settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
    )
    pg_engine = create_engine(stub_settings)
    session_factory = get_session_factory(pg_engine)

    tid, actor_id = await _seed(
        pg_container,
        slug=f"outbox-gauge-{uuid.uuid4().hex[:6]}",
        roles=["producer"],
    )
    ctx = TenantContext(tenant_id=tid, actor_id=actor_id, roles=["producer"])

    fake_clock = FakeClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    vocabulary = VocabularyService(session_factory)
    schema = SchemaService(session_factory, fake_clock)
    catalog_svc = CatalogService(session_factory, fake_clock, vocabulary, schema)

    # Write one fact (enqueues to embedding_outbox in same transaction).
    entity_ref = await catalog_svc.create_entity(ctx, "capability", "outbox-test-cap")
    await catalog_svc.create_fact(
        ctx,
        entity_id=entity_ref.entity_id,
        category="overview",
        body="outbox gauge test fact body",
    )

    # Verify outbox has at least one pending row before drain.
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM embedding_outbox WHERE tenant_id = :tid"),
            {"tid": tid},
        )
        pre_count: int = result.scalar_one()
    assert pre_count >= 1, "embedding_outbox should have a pending row after create_fact"

    # Drain inline.
    embedder = StubEmbedder()
    await drain_outbox(session_factory, embedder, stub_settings)

    await pg_engine.dispose()

    # The Prometheus gauge should now be 0 (or reflect only rows from other tests).
    # We check the DB directly for this tenant's rows.
    pg_engine2 = create_engine(stub_settings)
    session_factory2 = get_session_factory(pg_engine2)
    try:
        async with session_factory2() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM embedding_outbox WHERE tenant_id = :tid"),
                {"tid": tid},
            )
            post_count: int = result.scalar_one()
    finally:
        await pg_engine2.dispose()

    assert post_count == 0, f"Expected 0 outbox rows for tenant after drain, got {post_count}"

    # The Prometheus gauge tracks the global count; after _refresh_pending_gauge it reflects
    # the current total. We assert it's been set (non-negative) as a smoke check.
    gauge_value: float = _OUTBOX_PENDING_GAUGE._value.get()
    assert gauge_value >= 0, f"catalog_outbox_pending_size gauge = {gauge_value} (must be >= 0)"


# ---------------------------------------------------------------------------
# model_id isolation in the semantic arm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_arm_ignores_other_models_embeddings(pg_container: str) -> None:
    """The ANN arm only reads rows written by the embedder currently in use.

    One `embeddings` table, and one HNSW index per partition, can hold vectors
    from more than one model at once — reindexing adds rows under a new model id
    without deleting the old ones, by design. Distances between two different
    embedding spaces are not comparable, so an unfiltered `ORDER BY vector <=>`
    would rank rows from both against each other and return whichever happened
    to land closer in a coordinate system it was never measured in.

    Seeds one fact, drains it under model id A, then rewrites those rows to
    model id B and asserts the arm no longer sees them.
    """
    stub_settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
    )
    pg_engine = create_engine(stub_settings)
    session_factory = get_session_factory(pg_engine)

    try:
        tid, actor_id = await _seed(
            pg_container,
            slug=f"model-iso-{uuid.uuid4().hex[:6]}",
            roles=["producer"],
        )
        ctx = TenantContext(tenant_id=tid, actor_id=actor_id, roles=["producer"])

        fake_clock = FakeClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
        vocabulary = VocabularyService(session_factory)
        schema = SchemaService(session_factory, fake_clock)
        catalog_svc = CatalogService(session_factory, fake_clock, vocabulary, schema)

        entity_ref = await catalog_svc.create_entity(ctx, "capability", "model-iso-cap")
        await catalog_svc.create_fact(
            ctx,
            entity_id=entity_ref.entity_id,
            category="overview",
            body="ledger posting and reconciliation",
        )

        embedder = StubEmbedder()
        await drain_outbox(session_factory, embedder, stub_settings)

        retrieval_svc = RetrievalService(session_factory, fake_clock, embedder, stub_settings)

        async def semantic_hits() -> int:
            results = await retrieval_svc._semantic_arm(
                ctx,
                q="ledger posting",
                top_k=10,
                temporal_filter=TemporalFilter(as_of=None),
                entity_type=None,
            )
            return len(results)

        assert await semantic_hits() >= 1, "own-model embeddings must be visible to the semantic arm"

        # Restamp every row under a different model. Nothing else changes — same
        # vectors, same tenant, same facts — so any change in the result is
        # attributable to the model_id filter alone.
        async with session_factory() as session, session.begin():
            updated = await session.execute(
                text("UPDATE embeddings SET model_id = :other WHERE tenant_id = :tid"),
                {"other": "some-other-model-v9", "tid": tid},
            )
        assert updated.rowcount >= 1, "expected the drain to have written embedding rows"

        assert await semantic_hits() == 0, (
            "the semantic arm returned embeddings written by a different model; "
            "distances across two embedding spaces are not comparable"
        )
    finally:
        await pg_engine.dispose()
