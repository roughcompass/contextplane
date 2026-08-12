"""Lifecycle reconnects return unresolved feedback and newer reviewed learning.

The lower-level bounds suite proves each query. This file proves the delivered
surface: run and stage references reach the same task, REST and MCP serialize
one response, a reconnect with no prior receipt has no temporal arms, and a
same-tenant actor outside the task audience is refused before those arms read.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import TenantPersona, bearer_headers
from tests.integration.test_receipt_resume_surfaces import _as, _mcp, surface  # noqa: F401

type _Lifecycle = dict[str, Any]


async def _seed_lifecycle(pg_url: str, state: dict[str, Any]) -> dict[str, Any]:
    now = state["harness"].app.state.clock.now()
    cutoff = now - datetime.timedelta(hours=1)
    run_ref = ("delivery", state["slug"], "run", f"run-{uuid.uuid4()}")
    stage_ref = ("delivery", state["slug"], "stage", f"stage-{uuid.uuid4()}")
    no_receipt_ref = ("delivery", state["slug"], "stage", f"stage-{uuid.uuid4()}")
    receipt_id = uuid.uuid4()
    feedback_ids = (uuid.uuid4(), uuid.uuid4())
    claim_ids = (uuid.uuid4(), uuid.uuid4())

    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            reference_ids: dict[tuple[str, str, str, str], uuid.UUID] = {}
            for reference in (run_ref, stage_ref, no_receipt_ref):
                reference_id = uuid.uuid4()
                reference_ids[reference] = reference_id
                await session.execute(
                    text(
                        "INSERT INTO context_external_references ("
                        " reference_id, tenant_id, source_system, source_namespace, kind, external_id,"
                        " classification, external_authority, collision_key"
                        ") VALUES ("
                        " :reference, :tenant, :system, :namespace, :kind, :external_id,"
                        " 'internal', 'delivery', :collision"
                        ")"
                    ),
                    {
                        "reference": reference_id,
                        "tenant": state["tenant_id"],
                        "system": reference[0],
                        "namespace": reference[1],
                        "kind": reference[2],
                        "external_id": reference[3],
                        "collision": "|".join(reference),
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO context_reference_bindings ("
                        " binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at"
                        ") VALUES (:binding, :tenant, :reference, 'intent_checkpoint', :checkpoint, :now)"
                    ),
                    {
                        "binding": uuid.uuid4(),
                        "tenant": state["tenant_id"],
                        "reference": reference_id,
                        "checkpoint": state["head_checkpoint_id"],
                        "now": now,
                    },
                )

            await session.execute(
                text(
                    "INSERT INTO context_receipts ("
                    " receipt_id, tenant_id, intent_id, state, cacheable, resolved_at, requested_by, request_digest"
                    ") VALUES ("
                    " :receipt, :tenant, :task, 'complete', TRUE, :cutoff, 'lifecycle-test', :digest"
                    ")"
                ),
                {
                    "receipt": receipt_id,
                    "tenant": state["tenant_id"],
                    "task": state["intent_id"],
                    "cutoff": cutoff,
                    "digest": f"sha256:{receipt_id.hex}",
                },
            )
            for reference in (run_ref, stage_ref):
                await session.execute(
                    text(
                        "INSERT INTO context_reference_bindings ("
                        " binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at"
                        ") VALUES (:binding, :tenant, :reference, 'context_item', :receipt, :now)"
                    ),
                    {
                        "binding": uuid.uuid4(),
                        "tenant": state["tenant_id"],
                        "reference": reference_ids[reference],
                        "receipt": receipt_id,
                        "now": now,
                    },
                )

            for index, feedback_id in enumerate(feedback_ids):
                item = f"lifecycle-item-{index}"
                await session.execute(
                    text(
                        "INSERT INTO context_receipt_items ("
                        " item_row_id, receipt_id, receipt_item_id, block, source, item_key"
                        ") VALUES (:row, :receipt, :item, 'canonical', 'lifecycle-test', :item)"
                    ),
                    {"row": uuid.uuid4(), "receipt": receipt_id, "item": item},
                )
                await session.execute(
                    text(
                        "INSERT INTO context_feedback ("
                        " feedback_id, tenant_id, kind, receipt_id, receipt_item_id, rating, learning_eligible,"
                        " note, reporter_id, reporter_type, idempotency_key, content_digest, created_at"
                        ") VALUES ("
                        " :feedback, :tenant, 'item_specific', :receipt, :item, 'incorrect', TRUE,"
                        " 'not for resume', :reporter, 'human', :key, :digest, :created"
                        ")"
                    ),
                    {
                        "feedback": feedback_id,
                        "tenant": state["tenant_id"],
                        "receipt": receipt_id,
                        "item": item,
                        "reporter": f"reporter-{feedback_id}",
                        "key": f"lifecycle-{feedback_id}",
                        "digest": feedback_id.hex,
                        "created": now - datetime.timedelta(minutes=5 + index),
                    },
                )

            for index, claim_id in enumerate(claim_ids):
                entity_id = uuid.uuid4()
                consolidated_at = now - datetime.timedelta(minutes=15 + index * 5)
                await session.execute(
                    text(
                        "INSERT INTO entities ("
                        " entity_id, tenant_id, entity_type, name, visibility, is_active, created_at"
                        ") VALUES ("
                        " :entity, :tenant, 'capability', :name, 'tenant-shared', TRUE, :created"
                        ")"
                    ),
                    {
                        "entity": entity_id,
                        "tenant": state["tenant_id"],
                        "name": f"lifecycle-{entity_id.hex}",
                        "created": consolidated_at,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_claims ("
                        " claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                        " subject_reference, predicate, value_type, claim_category, value_jsonb,"
                        " asserted_valid_from, status, visibility, source_authority, size_bytes, consolidated_at,"
                        " created_at, confidence, confidence_scored_at, confidence_inputs, scorer_version,"
                        " calibration_version, decay_half_life_days"
                        ") VALUES ("
                        " :claim, :tenant, :tenant, :actor, :entity, :subject, :predicate, 'prose',"
                        " 'operational_lifecycle', CAST(:value AS JSONB), :created, 'staged', 'private',"
                        " 'observer_extraction', 32, :created, :created, 0.800, :created, CAST(:inputs AS JSONB),"
                        " 'scorer.v1', 'calib.v1', 30"
                        ")"
                    ),
                    {
                        "claim": claim_id,
                        "tenant": state["tenant_id"],
                        "actor": uuid.UUID(state["owner_actor"]),
                        "entity": entity_id,
                        "subject": f"lifecycle:{entity_id}",
                        "predicate": f"lifecycle.learning.{index}",
                        "value": json.dumps(f"reviewed learning {index}"),
                        "created": consolidated_at,
                        "inputs": json.dumps({"lifecycle_test": True}),
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) "
                        "VALUES (:claim, 'connector_run', :ref)"
                    ),
                    {"claim": claim_id, "ref": f"lifecycle:{claim_id}"},
                )
    finally:
        await engine.dispose()

    return {
        "run_ref": run_ref,
        "stage_ref": stage_ref,
        "no_receipt_ref": no_receipt_ref,
        "receipt_id": receipt_id,
        "newest_feedback_id": feedback_ids[0],
        "newest_claim_id": claim_ids[0],
    }


@pytest_asyncio.fixture
async def lifecycle(surface: dict[str, Any], pg_container: str) -> AsyncIterator[_Lifecycle]:  # noqa: F811
    seeded = await _seed_lifecycle(pg_container, surface)
    yield {**surface, **seeded}


@pytest.mark.asyncio
async def test_rest_and_mcp_return_the_same_extended_lifecycle_resume(lifecycle: _Lifecycle) -> None:
    request_body = {
        "references": [list(lifecycle["run_ref"]), list(lifecycle["stage_ref"])],
        "feedback_bound": 1,
        "learning_bound": 1,
    }
    with _as(lifecycle, lifecycle["owner"]):
        rest = await lifecycle["client"].post(
            "/v1/context/resume",
            json=request_body,
            headers=bearer_headers(tenant_slug=lifecycle["slug"]),
        )
    mcp = await _mcp(lifecycle, lifecycle["owner"], "resume_context", request_body)

    assert rest.status_code == 200, rest.text
    body = rest.json()
    assert set(body) == set(mcp)
    for field in set(body) - {"learning"}:
        assert body[field] == mcp[field], f"the transports disagree about {field}"
    assert len(body["learning"]) == len(mcp["learning"]) == 1
    for field in set(body["learning"][0]) - {"as_of", "confidence"}:
        assert body["learning"][0][field] == mcp["learning"][0][field]
    # Claim confidence is decayed at read time, so two sequential transport
    # calls legitimately differ only by the milliseconds between them.
    rest_as_of = datetime.datetime.fromisoformat(body["learning"][0]["as_of"])
    mcp_as_of = datetime.datetime.fromisoformat(mcp["learning"][0]["as_of"])
    assert abs(mcp_as_of - rest_as_of) < datetime.timedelta(seconds=1)
    assert body["learning"][0]["confidence"] == pytest.approx(mcp["learning"][0]["confidence"], rel=1e-6)
    assert body["status"] == "resumed"
    assert body["receipts"][0]["receipt_id"] == str(lifecycle["receipt_id"])
    assert {
        (reference["source_system"], reference["source_namespace"], reference["kind"], reference["external_id"])
        for reference in body["references"]
    } == {lifecycle["run_ref"], lifecycle["stage_ref"]}
    assert body["feedback"][0]["feedback_id"] == str(lifecycle["newest_feedback_id"])
    assert body["feedback"][0]["consumed"] is False
    assert not {"note", "reporter_id", "reporter_type"} & set(body["feedback"][0])
    assert body["learning"][0]["claim_id"] == str(lifecycle["newest_claim_id"])
    assert body["learning"][0]["citations"]
    assert {"feedback", "learning"} <= set(body["truncated"])


@pytest.mark.asyncio
async def test_lifecycle_resume_without_a_last_receipt_has_no_temporal_arms(lifecycle: _Lifecycle) -> None:
    request_body = {"references": [list(lifecycle["no_receipt_ref"])]}
    with _as(lifecycle, lifecycle["owner"]):
        rest = await lifecycle["client"].post(
            "/v1/context/resume",
            json=request_body,
            headers=bearer_headers(tenant_slug=lifecycle["slug"]),
        )
    mcp = await _mcp(lifecycle, lifecycle["owner"], "resume_context", request_body)

    assert rest.status_code == 200, rest.text
    assert rest.json() == mcp
    assert rest.json()["feedback"] == []
    assert rest.json()["learning"] == []


@pytest.mark.asyncio
async def test_lifecycle_resume_refuses_a_same_tenant_nonparticipant(lifecycle: _Lifecycle) -> None:
    outsider = TenantPersona(slug=lifecycle["slug"], actor_id=uuid.uuid4(), roles=["producer", "consumer"])
    with _as(lifecycle, outsider):
        response = await lifecycle["client"].post(
            "/v1/context/resume",
            json={"references": [list(lifecycle["run_ref"])]},
            headers=bearer_headers(tenant_slug=lifecycle["slug"]),
        )

    assert response.status_code == httpx.codes.FORBIDDEN
    assert response.json()["errors"][0]["code"] == "forbidden"
    assert "task audience" in response.json()["errors"][0]["message"]
    assert "feedback" not in response.json()
    assert "learning" not in response.json()
