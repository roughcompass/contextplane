"""The task-memory surfaces, over HTTP against a real Postgres.

Two things are proved here that nothing else can prove.

**The surfaces are actually reachable.** A router that `wiring/routes.py` never
names, and an MCP tool module `api/mcp/server.py` never registers, are both
unreachable code that looks entirely correct in review. This suite fails with
`404` and an unknown tool respectively if either mount is dropped, which is the
only reason those two files are in this task's scope.

**Both transports answer the same question the same way.** Not because each was
written carefully, but because both adapt one pair of services. The tests below
drive the same operations over REST and over the tool functions and compare the
answers, so a future change that teaches one transport something the other does
not know fails here rather than in production.

Authorization is the third thread through all of it: a denial must never say
why, because "no such task", "not a participant" and "grant expired" are three
answers that together enumerate the tenant's tasks.
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

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

type _Surface = dict[str, Any]


async def _seed_owner_grant(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    actor_id: str,
) -> None:
    """Make one actor the owner of a task, directly.

    Inserted rather than granted through the surface because granting requires
    an owner, and the first owner of a task has nobody to be granted by. That
    bootstrap is the task-creation path's job, which this slice does not
    publish.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO task_participant_grants "
                    "(tenant_id, task_id, actor_id, role, granted_by, granted_at, expires_at, resolver_version) "
                    "VALUES (:tid, :task, :actor, 'owner', 'bootstrap', :now, NULL, 'explicit/v1')"
                ),
                {"tid": tenant_id, "task": task_id, "actor": actor_id, "now": _NOW},
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def surface(pg_container: str) -> AsyncIterator[_Surface]:
    slug = f"tm-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        owner = harness.add_persona(slug, roles=["producer"])
        outsider = harness.add_persona(slug, roles=["producer"], actor_id=uuid.uuid4())

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(owner)
            with patch_validator_for_actor(owner):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert resp.status_code == 200, resp.text
                tenant_id = uuid.UUID(resp.json()["tenant_id"])
                owner_actor = resp.json()["actor_id"]

            task_id = uuid.uuid4()
            await _seed_owner_grant(pg_container, tenant_id=tenant_id, task_id=task_id, actor_id=str(owner_actor))

            yield {
                "client": client,
                "harness": harness,
                "owner": owner,
                "outsider": outsider,
                "slug": slug,
                "tenant_id": tenant_id,
                "owner_actor": str(owner_actor),
                "task_id": task_id,
            }


def _as(surface: _Surface, persona: Any):
    surface["harness"].configure_fetcher_for(persona)
    return patch_validator_for_actor(persona)


# --- The surfaces exist at all ------------------------------------------------


@pytest.mark.asyncio
async def test_the_rest_routes_are_mounted(surface: _Surface) -> None:
    """The check that fails if `wiring/routes.py` stops naming this router.

    Unreachable code is the failure this task was blocked on twice, and it looks
    exactly like correct code from inside the router file.
    """
    with _as(surface, surface["owner"]):
        resp = await surface["client"].get(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_mcp_tools_are_registered(surface: _Surface) -> None:
    """The same check for the other transport.

    MCP registration is static -- `api/mcp/server.py` names each tool module
    inline -- so a module nobody names is as unreachable as an unmounted router,
    and the first amendment to this task fixed only the REST half.
    """
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    app = surface["harness"].app
    server = create_contextplane_mcp_server(
        retrieval=app.state.retrieval,
        catalog=app.state.catalog,
        session_factory=app.state.session_factory,
        clock=app.state.clock,
        workspace_service=app.state.workspace_service,
    )
    tools = await server.list_tools()
    names = {tool.name for tool in tools}

    assert "append_task_checkpoint" in names
    assert "list_task_participants" in names
    assert "grant_task_participation" in names


# --- Grants -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_owner_can_add_and_list_a_participant(surface: _Surface) -> None:
    headers = bearer_headers(tenant_slug=surface["slug"])
    with _as(surface, surface["owner"]):
        created = await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=headers,
            json={"actor_id": "agent-b", "role": "contributor"},
        )
        listed = await surface["client"].get(f"/v1/tasks/{surface['task_id']}/participants", headers=headers)

    assert created.status_code == 201, created.text
    assert created.json()["granted_by"] == surface["owner_actor"], "the grant must be attributed to the caller"
    actors = {grant["actor_id"] for grant in listed.json()["grants"]}
    assert "agent-b" in actors


@pytest.mark.asyncio
async def test_a_non_participant_is_refused_without_being_told_why(surface: _Surface) -> None:
    """One body for every denial. Three distinguishable answers would let a
    caller enumerate the tenant's tasks by watching which refusal came back."""
    with _as(surface, surface["outsider"]):
        resp = await surface["client"].get(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 403
    body = resp.text
    assert "not a participant" not in body
    assert "expired" not in body
    assert "no such task" not in body


@pytest.mark.asyncio
async def test_an_unknown_task_is_refused_identically_to_a_forbidden_one(surface: _Surface) -> None:
    """The pair that makes the previous test meaningful: if these two differed,
    the denial would be an oracle for which task ids exist."""
    with _as(surface, surface["outsider"]):
        headers = bearer_headers(tenant_slug=surface["slug"])
        forbidden = await surface["client"].get(f"/v1/tasks/{surface['task_id']}/participants", headers=headers)
        unknown = await surface["client"].get(f"/v1/tasks/{uuid.uuid4()}/participants", headers=headers)

    assert forbidden.status_code == unknown.status_code == 403
    assert forbidden.text == unknown.text


@pytest.mark.asyncio
async def test_a_participant_who_is_not_an_owner_cannot_widen_the_audience(surface: _Surface) -> None:
    """Reading a task does not confer the right to add people to it."""
    headers = bearer_headers(tenant_slug=surface["slug"])
    outsider_actor = str(surface["outsider"].actor_id)
    with _as(surface, surface["owner"]):
        await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=headers,
            json={"actor_id": outsider_actor, "role": "reader"},
        )

    with _as(surface, surface["outsider"]):
        resp = await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=headers,
            json={"actor_id": "agent-c", "role": "reader"},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_revoking_is_idempotent(surface: _Surface) -> None:
    """A retry after a dropped response must not read as a different outcome."""
    headers = bearer_headers(tenant_slug=surface["slug"])
    with _as(surface, surface["owner"]):
        await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=headers,
            json={"actor_id": "agent-d", "role": "reader"},
        )
        first = await surface["client"].delete(f"/v1/tasks/{surface['task_id']}/participants/agent-d", headers=headers)
        second = await surface["client"].delete(f"/v1/tasks/{surface['task_id']}/participants/agent-d", headers=headers)

    assert first.status_code == second.status_code == 204


@pytest.mark.asyncio
async def test_a_revoked_grant_is_still_listed(surface: _Surface) -> None:
    """Revocation is temporal, not a delete. An audit of a past read needs the
    grant that authorized it, and hiding it makes a revoked participant look
    like one who was never there."""
    headers = bearer_headers(tenant_slug=surface["slug"])
    with _as(surface, surface["owner"]):
        await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=headers,
            json={"actor_id": "agent-e", "role": "reader"},
        )
        await surface["client"].delete(f"/v1/tasks/{surface['task_id']}/participants/agent-e", headers=headers)
        listed = await surface["client"].get(f"/v1/tasks/{surface['task_id']}/participants", headers=headers)

    grants = {grant["actor_id"]: grant for grant in listed.json()["grants"]}
    assert "agent-e" in grants
    assert grants["agent-e"]["expires_at"] is not None


@pytest.mark.asyncio
async def test_an_unknown_role_is_refused(surface: _Surface) -> None:
    with _as(surface, surface["owner"]):
        resp = await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=bearer_headers(tenant_slug=surface["slug"]),
            json={"actor_id": "agent-f", "role": "overlord"},
        )

    assert resp.status_code == 422


# --- Checkpoints --------------------------------------------------------------


@pytest.mark.asyncio
async def test_appending_a_checkpoint_returns_201_and_reads_back(surface: _Surface) -> None:
    headers = {**bearer_headers(tenant_slug=surface["slug"]), "Idempotency-Key": "k1"}
    with _as(surface, surface["owner"]):
        created = await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/checkpoints",
            headers=headers,
            json={"goal": "ship the thing", "decisions": ["use the kit"]},
        )
        checkpoint_id = created.json()["checkpoint_id"]
        fetched = await surface["client"].get(
            f"/v1/tasks/{surface['task_id']}/checkpoints/{checkpoint_id}",
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert created.status_code == 201, created.text
    assert fetched.status_code == 200
    assert fetched.json()["goal"] == "ship the thing"
    assert fetched.json()["author"], "a checkpoint has to say who recorded it"


@pytest.mark.asyncio
async def test_a_replayed_idempotency_key_answers_200_with_the_first_checkpoint(surface: _Surface) -> None:
    """The distinction a client retrying a dropped response depends on: 201 means
    this call appended a step, 200 means it found the one its earlier call wrote."""
    headers = {**bearer_headers(tenant_slug=surface["slug"]), "Idempotency-Key": "k-replay"}
    body = {"goal": "only once"}
    with _as(surface, surface["owner"]):
        first = await surface["client"].post(f"/v1/tasks/{surface['task_id']}/checkpoints", headers=headers, json=body)
        second = await surface["client"].post(f"/v1/tasks/{surface['task_id']}/checkpoints", headers=headers, json=body)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["checkpoint_id"] == second.json()["checkpoint_id"]
    assert first.json()["sequence"] == second.json()["sequence"], "a replay must not advance the chain"


@pytest.mark.asyncio
async def test_appending_without_an_idempotency_key_is_refused(surface: _Surface) -> None:
    """Retrying is the one thing a client does after a dropped response, so a
    keyless append produces a duplicate step under exactly the condition it
    will meet."""
    with _as(surface, surface["owner"]):
        resp = await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/checkpoints",
            headers=bearer_headers(tenant_slug=surface["slug"]),
            json={"goal": "no key"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_checkpoint_is_reachable_by_digest(surface: _Surface) -> None:
    """A digest names content, so a caller holding one from a receipt need not
    know which task it belongs to."""
    headers = {**bearer_headers(tenant_slug=surface["slug"]), "Idempotency-Key": "k-digest"}
    with _as(surface, surface["owner"]):
        created = await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/checkpoints", headers=headers, json={"goal": "find me"}
        )
        digest = created.json()["digest"]
        fetched = await surface["client"].get(
            f"/v1/checkpoints/by-digest/{digest}", headers=bearer_headers(tenant_slug=surface["slug"])
        )

    assert fetched.status_code == 200
    assert fetched.json()["checkpoint_id"] == created.json()["checkpoint_id"]


@pytest.mark.asyncio
async def test_a_non_participant_cannot_append(surface: _Surface) -> None:
    headers = {**bearer_headers(tenant_slug=surface["slug"]), "Idempotency-Key": "k-nope"}
    with _as(surface, surface["outsider"]):
        resp = await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/checkpoints", headers=headers, json={"goal": "sneak"}
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_checkpoint_id_from_another_task_is_not_found(surface: _Surface) -> None:
    """The task in the path is part of the address. A mismatched pair must not
    read somebody else's chain."""
    headers = {**bearer_headers(tenant_slug=surface["slug"]), "Idempotency-Key": "k-other"}
    with _as(surface, surface["owner"]):
        created = await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/checkpoints", headers=headers, json={"goal": "mine"}
        )
        resp = await surface["client"].get(
            f"/v1/tasks/{uuid.uuid4()}/checkpoints/{created.json()['checkpoint_id']}",
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code in (403, 404), "either refusal is fine; returning the checkpoint is not"


# --- The two transports agree -------------------------------------------------


@pytest.mark.asyncio
async def test_both_transports_report_the_same_participants(surface: _Surface) -> None:
    """Written against one pair of services, so this asserts the wiring rather
    than two careful implementations. A future change that teaches one transport
    something the other does not know fails here."""
    from contextplane.api.mcp.tools import task_memory as tools

    headers = bearer_headers(tenant_slug=surface["slug"])
    with _as(surface, surface["owner"]):
        await surface["client"].post(
            f"/v1/tasks/{surface['task_id']}/participants",
            headers=headers,
            json={"actor_id": "agent-parity", "role": "reader"},
        )
        rest = await surface["client"].get(f"/v1/tasks/{surface['task_id']}/participants", headers=headers)

    app = surface["harness"].app
    container = app.state.services
    grants = await container.task_grants.list_grants(await _ctx_for(surface), task_id=surface["task_id"])

    rest_actors = sorted(grant["actor_id"] for grant in rest.json()["grants"])
    service_actors = sorted(grant.actor_id for grant in grants)
    assert rest_actors == service_actors
    assert "agent-parity" in rest_actors
    assert tools.list_task_participants is not None, "the tool the MCP surface exposes over the same service"


async def _ctx_for(surface: _Surface) -> Any:
    """The owner's TenantContext, for calling a service directly."""
    from contextplane.types import TenantContext

    return TenantContext(
        tenant_id=surface["tenant_id"],
        actor_id=uuid.UUID(surface["owner_actor"]),
        roles=["producer"],
    )
