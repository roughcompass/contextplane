"""The twenty-entity evaluation catalog and its seeder, shared by the eval gates.

Two gates measure retrieval against this corpus -- recall@10 in
`test_retrieval_embedding.py` and receipt-joined precision in
`test_retrieval_relevance.py` -- and a second copy of twenty entity bodies is a
second corpus the moment somebody edits one of them. The fixture UUIDs in
`eval/fixtures/search_questions.json` name these entities, so a divergence would
not fail loudly; it would measure two different things and report both under one
heading.

Moved here unchanged. The bodies, the ids, the vocabulary rows and the seeding
SQL are byte-identical to what `test_retrieval_embedding.py` held, because the
recall figure recorded in `eval/EVAL.md` was measured against exactly these.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "eval" / "fixtures"
SEARCH_QUESTIONS_FILE = FIXTURES / "search_questions.json"
SEARCH_QUESTION_COUNT = 50
EVAL_ENTITY_COUNT = 20


VOCAB_ROWS = [
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


# Canonical entity catalog for search questions.  UUIDs match search_questions.json.
# Bodies are phrased to be semantically relevant to the expected questions.
EVAL_ENTITIES: list[dict[str, Any]] = [
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


def load_search_questions() -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = json.loads(SEARCH_QUESTIONS_FILE.read_text())
    assert (
        len(questions) == SEARCH_QUESTION_COUNT
    ), f"expected {SEARCH_QUESTION_COUNT} questions, found {len(questions)}"
    question_ids = [str(question["id"]) for question in questions]
    assert len(set(question_ids)) == len(question_ids), "search question IDs must be unique"

    catalog_ids = {str(entity["id"]) for entity in EVAL_ENTITIES}
    assert len(EVAL_ENTITIES) == EVAL_ENTITY_COUNT, "evaluation catalog must contain exactly 20 entities"
    assert len(catalog_ids) == EVAL_ENTITY_COUNT, "evaluation entity IDs must be unique"
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


async def seed_eval_entities(
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
            for kind, value in VOCAB_ROWS:
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
            for spec in EVAL_ENTITIES:
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
