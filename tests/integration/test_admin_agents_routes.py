"""The admin agent surface, over HTTP.

Two things this proves that the service tests cannot.

**The routes are mounted.** A router `wiring/routes.py` never includes is
unreachable code that looks entirely correct in review — the same failure the
task-memory surfaces were blocked on twice.

**The asymmetry between the two surfaces is real.** An agent's MCP tools take no
actor identifier, so asking about a colleague is unsayable; these routes take one
in the path and are gated on the admin role instead. That gate is the whole of
the protection now that no floor makes a per-actor figure unconstructible, so it
is asserted rather than assumed.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import EntitlementAuthHarness, bearer_headers, patch_validator_for_actor

_SEEDED = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_JUDGED = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_START = "2026-08-01T00:00:00+00:00"
_END = "2026-08-22T00:00:00+00:00"

type _Surface = dict[str, Any]


async def _seed_judged_claim(pg_url: str, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, verdict: str) -> None:
    """One claim by `agent_id`, with one verdict on it."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cid, eid = uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                    "VALUES (:e, :t, 'capability', :n, 'tenant-shared', TRUE, :now)"
                ),
                {"e": eid, "t": tenant_id, "n": f"cap-{eid.hex[:8]}", "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claims ("
                    "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                    "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                    "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                    "  consolidated_at, created_at, confidence, confidence_scored_at, confidence_inputs,"
                    "  scorer_version, calibration_version, decay_half_life_days"
                    ") VALUES ("
                    "  :cid, :t, :t, :a, :e, 'ref', 'owned_by_team', 'prose',"
                    "  'ownership_stewardship', CAST('\"platform\"' AS JSONB), :now, 'staged', 'private',"
                    "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST('{}' AS JSONB),"
                    "  'scorer.v1', 'calib.v1', 30)"
                ),
                {"cid": cid, "t": tenant_id, "a": agent_id, "e": eid, "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claim_adjudication ("
                    "  tenant_id, claim_id, adjudicated_by, verdict, observed_confidence,"
                    "  observed_bucket, calibration_version, source_authority, adjudicated_at"
                    ") VALUES (:t, :cid, :a, :v, 0.700, '0.7', 'calib.v1', 'observer_extraction', :at)"
                ),
                {"t": tenant_id, "cid": cid, "a": agent_id, "v": verdict, "at": _JUDGED},
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def surface(pg_container: str) -> AsyncIterator[_Surface]:
    slug = f"ag-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        admin = harness.add_persona(slug, roles=["admin"])
        consumer = harness.add_persona(slug, roles=["consumer"], actor_id=uuid.uuid4())

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(admin)
            with patch_validator_for_actor(admin):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert resp.status_code == 200, resp.text
                tenant_id = uuid.UUID(resp.json()["tenant_id"])
                agent_id = uuid.UUID(resp.json()["actor_id"])

            yield {
                "client": client,
                "harness": harness,
                "admin": admin,
                "consumer": consumer,
                "slug": slug,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "pg_url": pg_container,
            }


def _as(surface: _Surface, persona: Any) -> Any:
    surface["harness"].configure_fetcher_for(persona)
    return patch_validator_for_actor(persona)


@pytest.mark.asyncio
async def test_the_accuracy_route_is_mounted_and_reports(surface: _Surface) -> None:
    """Fails with 404 if `wiring/routes.py` stops including this router."""
    await _seed_judged_claim(
        surface["pg_url"], tenant_id=surface["tenant_id"], agent_id=surface["agent_id"], verdict="correct"
    )
    await _seed_judged_claim(
        surface["pg_url"], tenant_id=surface["tenant_id"], agent_id=surface["agent_id"], verdict="incorrect"
    )

    with _as(surface, surface["admin"]):
        resp = await surface["client"].get(
            f"/v1/agents/{surface['agent_id']}/accuracy",
            params={"window_start": _START, "window_end": _END},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overall"]["n_adjudicated"] == 2
    assert body["overall"]["rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_the_autonomy_and_failure_pattern_routes_are_mounted(surface: _Surface) -> None:
    await _seed_judged_claim(
        surface["pg_url"], tenant_id=surface["tenant_id"], agent_id=surface["agent_id"], verdict="incorrect"
    )
    params = {"window_start": _START, "window_end": _END}
    headers = bearer_headers(tenant_slug=surface["slug"])

    with _as(surface, surface["admin"]):
        autonomy = await surface["client"].get(
            f"/v1/agents/{surface['agent_id']}/autonomy", params=params, headers=headers
        )
        patterns = await surface["client"].get(
            f"/v1/agents/{surface['agent_id']}/failure-patterns", params=params, headers=headers
        )

    assert autonomy.status_code == 200, autonomy.text
    assert autonomy.json()["n_sessions"] == 0
    assert autonomy.json()["autonomy_rate"] is None, "no sessions is an unknown rate, not a perfect one"

    assert patterns.status_code == 200, patterns.text
    assert patterns.json()["groups"][0]["predicate"] == "owned_by_team"
    assert patterns.json()["report_id"], "the report is stored, because an instruction has to cite one"


@pytest.mark.asyncio
async def test_a_consumer_cannot_read_another_actors_figures(surface: _Surface) -> None:
    """The admin gate is the whole of the protection here.

    Nothing structural stops a per-actor figure being constructed any more, so
    this route is the boundary. A non-admin reaching it would be reading a
    colleague's performance record.
    """
    with _as(surface, surface["consumer"]):
        resp = await surface["client"].get(
            f"/v1/agents/{surface['agent_id']}/accuracy",
            params={"window_start": _START, "window_end": _END},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_the_instruction_lifecycle_runs_end_to_end(surface: _Surface) -> None:
    """Propose, activate, roll back — over HTTP, in the order an operator uses."""
    await _seed_judged_claim(
        surface["pg_url"], tenant_id=surface["tenant_id"], agent_id=surface["agent_id"], verdict="incorrect"
    )
    headers = bearer_headers(tenant_slug=surface["slug"])
    agent = surface["agent_id"]

    with _as(surface, surface["admin"]):
        report = await surface["client"].get(
            f"/v1/agents/{agent}/failure-patterns",
            params={"window_start": _START, "window_end": _END},
            headers=headers,
        )
        report_id = report.json()["report_id"]

        first = await surface["client"].post(
            f"/v1/agents/{agent}/instructions",
            headers=headers,
            json={"version": 1, "content": "name the team explicitly", "motivated_by_report_id": report_id},
        )
        second = await surface["client"].post(
            f"/v1/agents/{agent}/instructions",
            headers=headers,
            json={"version": 2, "content": "and cite the source", "motivated_by_report_id": report_id},
        )
        assert first.status_code in (200, 201), first.text
        assert second.status_code in (200, 201), second.text

        listed = await surface["client"].get(f"/v1/agents/{agent}/instructions", headers=headers)
        assert [i["version"] for i in listed.json()] == [2, 1]
        assert all(i["status"] != "active" for i in listed.json()), "a proposal is not in force"

        activated = await surface["client"].post(
            f"/v1/agents/{agent}/instructions/{first.json()['instruction_id']}:activate", headers=headers
        )
        assert activated.status_code in (200, 201), activated.text
        assert activated.json()["version"] == 1

        rolled = await surface["client"].post(f"/v1/agents/{agent}/instructions:rollback", headers=headers)

    assert rolled.status_code in (200, 201), rolled.text
    assert (
        rolled.json()["restored_instruction_id"] is None
    ), "there was no predecessor, so rollback reports that rather than demoting the incumbent into a gap"


@pytest.mark.asyncio
async def test_proposing_without_a_real_report_is_refused_with_the_id(surface: _Surface) -> None:
    """Before an opaque foreign-key violation would surface."""
    missing = uuid.uuid4()
    with _as(surface, surface["admin"]):
        resp = await surface["client"].post(
            f"/v1/agents/{surface['agent_id']}/instructions",
            headers=bearer_headers(tenant_slug=surface["slug"]),
            json={"version": 1, "content": "x", "motivated_by_report_id": str(missing)},
        )

    assert resp.status_code == 404, resp.text
    assert str(missing) in resp.text
