"""The session-memory REST surface.

Adapter-level: that the routes exist, refuse what they should refuse before
reaching a service, and translate typed errors into the statuses the interface
promises.

The most important assertion here is a *negative* one — no route accepts an
actor identifier. Sessions carry no visibility setting and no sharing mode, so
the actor on the credential is the only thing scoping them; a parameter naming
an actor would be an authorization bypass with nothing downstream to catch it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)


# Module-scoped: one app for the file rather than one per test.
#
# Each `EntitlementAuthHarness` builds a full FastAPI app with its own engine
# and connection pool. This bucket's own conftest notes that engines accumulate
# across a long run and the suite tips into "too many clients already" -- which
# it duly did when this file first added ten more app builds. Nothing here
# mutates app state, so one instance serves every test.
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client(harness: EntitlementAuthHarness) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def persona(harness: EntitlementAuthHarness, client: AsyncClient):
    p = harness.add_persona(f"mem-{uuid.uuid4().hex[:6]}", roles=["consumer"])
    harness.configure_fetcher_for(p)
    with patch_validator_for_actor(p):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=p.slug))
        assert resp.status_code == 200, resp.text
    return p


def _event(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"kind": "user_message", "body": "hello"}
    body.update(overrides)
    return body


# --- the routes exist and require a credential --------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_the_memory_routes_are_registered(harness: EntitlementAuthHarness) -> None:
    paths = {r.path for r in harness.app.routes if hasattr(r, "path")}
    assert "/v1/memory/sessions" in paths
    assert "/v1/memory/sessions/{session_id}/events" in paths
    assert "/v1/memory/sessions/{session_id}/events/{event_id}" in paths


@pytest.mark.asyncio(loop_scope="module")
async def test_every_memory_route_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/memory/sessions")).status_code == 401
    assert (await client.post("/v1/memory/sessions/S/events", json=_event())).status_code == 401
    assert (await client.get("/v1/memory/sessions/S/events")).status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_no_route_accepts_an_actor_identifier(harness: EntitlementAuthHarness) -> None:
    """The omission is the control.

    Sessions have no visibility setting and no sharing mode, so the actor on
    the credential is the only thing scoping them. A route accepting an
    `actor_id` — as a path, query, or body field — would let a caller read
    somebody else's conversation with nothing downstream to catch it.
    """
    memory_routes = [r for r in harness.app.routes if getattr(r, "path", "").startswith("/v1/memory")]
    assert memory_routes, "no memory routes registered"

    for route in memory_routes:
        for param in getattr(route, "dependant", None).query_params if getattr(route, "dependant", None) else []:
            assert "actor" not in param.name, f"{route.path} accepts {param.name}"
        for param in getattr(route, "dependant", None).path_params if getattr(route, "dependant", None) else []:
            assert "actor" not in param.name, f"{route.path} accepts {param.name}"
        assert "actor" not in route.path


# --- round trip -----------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_an_event_can_be_recorded_and_replayed(client: AsyncClient, persona) -> None:
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        created = await client.post("/v1/memory/sessions/S1/events", json=_event(body="first turn"), headers=headers)
        assert created.status_code == 201, created.text

        replay = await client.get("/v1/memory/sessions/S1/events", headers=headers)
        one = await client.get(f"/v1/memory/sessions/S1/events/{created.json()['event_id']}", headers=headers)
        sessions = await client.get("/v1/memory/sessions", headers=headers)

    assert [e["body"] for e in replay.json()] == ["first turn"]
    assert one.json()["seq"] == 1
    assert [s["session_id"] for s in sessions.json()] == ["S1"]
    assert sessions.json()[0]["event_count"] == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_a_deleted_event_leaves_replay_over_http(client: AsyncClient, persona) -> None:
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        created = await client.post("/v1/memory/sessions/S2/events", json=_event(body="drop me"), headers=headers)
        event_id = created.json()["event_id"]
        deleted = await client.delete(f"/v1/memory/sessions/S2/events/{event_id}", headers=headers)
        replay = await client.get("/v1/memory/sessions/S2/events", headers=headers)
        refetch = await client.get(f"/v1/memory/sessions/S2/events/{event_id}", headers=headers)

    assert deleted.status_code == 204
    assert replay.json() == []
    assert refetch.status_code == 404


# --- refusals --------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_an_unknown_kind_is_rejected_at_the_boundary(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/memory/sessions/S/events",
            json=_event(kind="thinking"),
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="module")
async def test_a_misspelled_field_is_rejected_rather_than_dropped(client: AsyncClient, persona) -> None:
    """A caller who misspells `metadata` and has it silently dropped believes
    it attached a filter key that was never stored, and will not find those
    events again."""
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/memory/sessions/S/events",
            json=_event(metadatas={"task": "T-1"}),
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="module")
async def test_an_unknown_event_is_not_found(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.get(
            f"/v1/memory/sessions/S/events/{uuid.uuid4()}",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio(loop_scope="module")
async def test_an_oversized_page_is_rejected(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/memory/sessions/S/events?limit=99999",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio(loop_scope="module")
async def test_an_unknown_order_is_rejected(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/memory/sessions/S/events?order=sideways",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


# --- claim retrieval ----------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_the_claim_routes_are_registered(harness: EntitlementAuthHarness) -> None:
    paths = {r.path for r in harness.app.routes if hasattr(r, "path")}
    assert "/v1/memory/claims" in paths
    assert "/v1/memory/claims/{claim_id}" in paths


@pytest.mark.asyncio(loop_scope="module")
async def test_the_claim_routes_require_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/memory/claims")).status_code == 401
    assert (await client.get("/v1/memory/claims/00000000-0000-0000-0000-000000000001")).status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_an_unknown_persona_is_refused_rather_than_defaulted(client: AsyncClient, persona) -> None:
    """A typo'd persona must not fall back to a default depth.

    Silently defaulting would serve an L1 responder an architect's view, or the
    reverse, with nothing in the response saying which projection was applied.
    """
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/memory/claims?persona=l2", headers=headers)
    assert resp.status_code == 422, resp.text
    assert "persona" in resp.text


@pytest.mark.asyncio(loop_scope="module")
async def test_a_claim_limit_beyond_the_maximum_is_refused(client: AsyncClient, persona) -> None:
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/memory/claims?limit=101", headers=headers)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio(loop_scope="module")
async def test_an_absent_claim_is_not_found_rather_than_forbidden(client: AsyncClient, persona) -> None:
    """The same rule the session routes use. Distinguishing "not yours" from "not
    there" tells the caller a claim exists, and the subject of a claim is often the
    part they were not entitled to learn."""
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/memory/claims/00000000-0000-0000-0000-000000000001", headers=headers)
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio(loop_scope="module")
async def test_the_claims_route_returns_an_empty_list_when_nothing_matches(client: AsyncClient, persona) -> None:
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/memory/claims", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# --- semantic search over claims -------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_the_search_route_is_registered_before_the_id_route(
    harness: EntitlementAuthHarness,
) -> None:
    """Declaration order is load-bearing, so it is asserted rather than assumed.

    FastAPI matches in declaration order. With `/claims/{claim_id}` first, a request for
    `/claims/search` binds "search" to a UUID path parameter and fails validation -- it
    does not fall through to the next route. My first version had them the other way round
    and a docstring confidently explaining why that was fine.
    """
    paths = [r.path for r in harness.app.routes if hasattr(r, "path")]
    assert "/v1/memory/claims/search" in paths
    assert paths.index("/v1/memory/claims/search") < paths.index("/v1/memory/claims/{claim_id}")


@pytest.mark.asyncio(loop_scope="module")
async def test_the_search_route_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/memory/claims/search?q=anything")).status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_search_reaches_the_service_rather_than_the_id_route(client: AsyncClient, persona) -> None:
    """The proof that ordering works: a 200 with an empty list, not a 422 about a UUID.

    A 422 here would mean `search` was parsed as a claim id, which is exactly the failure
    the ordering prevents.
    """
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/memory/claims/search?q=who%20owns%20auth", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio(loop_scope="module")
async def test_search_refuses_an_unknown_persona(client: AsyncClient, persona) -> None:
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/memory/claims/search?q=x&persona=l2", headers=headers)
    assert resp.status_code == 422, resp.text
    assert "persona" in resp.text


@pytest.mark.asyncio(loop_scope="module")
async def test_search_refuses_a_top_k_beyond_the_maximum(client: AsyncClient, persona) -> None:
    headers = bearer_headers(tenant_slug=persona.slug)
    with patch_validator_for_actor(persona):
        resp = await client.get("/v1/memory/claims/search?q=x&top_k=101", headers=headers)
    assert resp.status_code == 422, resp.text
