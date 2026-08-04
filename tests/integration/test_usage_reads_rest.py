"""The usage read endpoints, through the running app.

Three things can only be checked here rather than at the service layer: the role
gate, that one tenant's request cannot reach another tenant's numbers even when the
data exists, and that the surface stays aggregate-only — the last asserted against
the app's own route table, because the way a per-event endpoint arrives is somebody
adding one without noticing it was ruled out.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.usage.rollups import roll_up_day
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

type _Harness = tuple[EntitlementAuthHarness, AsyncClient]

_TODAY = datetime.datetime.now(tz=datetime.UTC).date()


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[_Harness]:
    async with EntitlementAuthHarness(pg_container) as app_harness:
        transport = ASGITransport(app=app_harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield app_harness, client


async def _materialise(harness: EntitlementAuthHarness, persona: TenantPersona) -> uuid.UUID:
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
            assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["tenant_id"])


async def _seed(pg_url: str, *, tenant_id: uuid.UUID, calls: int, surface: str = "rest") -> None:
    """Write raw usage rows for today and roll them up."""
    engine = create_async_engine(pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for _ in range(calls):
                await session.execute(
                    text(
                        "INSERT INTO usage_events (event_id, occurred_at, tenant_id, actor_id, surface,"
                        " operation, outcome, status_class, latency_ms)"
                        " VALUES (:e, now(), :t, :a, :s, :op, 'ok', '2xx', 12)"
                    ),
                    {
                        "e": uuid.uuid4(),
                        "t": tenant_id,
                        "a": uuid.uuid4(),
                        "s": surface,
                        "op": "get_capability" if surface == "mcp" else "/v1/capabilities",
                    },
                )
            await session.commit()
        await roll_up_day(factory, _TODAY)
    finally:
        await engine.dispose()


async def _get(harness: EntitlementAuthHarness, client: AsyncClient, persona: TenantPersona, path: str):
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        return await client.get(path, headers=bearer_headers(tenant_slug=persona.slug))


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["summary", "series", "tools", "capabilities"])
async def test_a_non_admin_cannot_read_usage(harness: _Harness, path: str) -> None:
    """Consumer and producer roles are not enough.

    This is tenant-admin, as everywhere in this API. The numbers are aggregate and
    name nobody, but reach and error rates are still the sort of thing a tenant
    decides who sees.
    """
    app_harness, client = harness
    persona = app_harness.add_persona(f"usage-consumer-{uuid.uuid4().hex[:6]}", roles=["consumer", "producer"])
    await _materialise(app_harness, persona)

    resp = await _get(app_harness, client, persona, f"/v1/admin/usage/{path}")

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_an_admin_reads_their_own_tenants_usage(pg_container: str, harness: _Harness) -> None:
    app_harness, client = harness
    persona = app_harness.add_persona(f"usage-admin-{uuid.uuid4().hex[:6]}", roles=["admin"])
    tenant_id = await _materialise(app_harness, persona)
    await _seed(pg_container, tenant_id=tenant_id, calls=3)

    resp = await _get(app_harness, client, persona, "/v1/admin/usage/summary")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    (surface,) = body["surfaces"]
    assert surface["surface"] == "rest"
    assert surface["calls"] == 3
    # Today is inside any permitted retention, so the true headcount is available.
    assert surface["distinct_actors"] == 3
    assert surface["actor_days"] == 3


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_admin_cannot_see_another_tenants_usage(pg_container: str, harness: _Harness) -> None:
    """Both tenants have data, so an unscoped query would return a wrong number
    rather than an empty one — the failure mode a fixture with only one tenant
    cannot detect."""
    app_harness, client = harness
    mine = app_harness.add_persona(f"usage-mine-{uuid.uuid4().hex[:6]}", roles=["admin"])
    theirs = app_harness.add_persona(f"usage-theirs-{uuid.uuid4().hex[:6]}", roles=["admin"])
    my_tenant = await _materialise(app_harness, mine)
    their_tenant = await _materialise(app_harness, theirs)

    await _seed(pg_container, tenant_id=my_tenant, calls=2)
    await _seed(pg_container, tenant_id=their_tenant, calls=7)

    resp = await _get(app_harness, client, mine, "/v1/admin/usage/summary")

    assert resp.status_code == 200, resp.text
    assert resp.json()["surfaces"][0]["calls"] == 2


@pytest.mark.asyncio
async def test_no_route_accepts_a_tenant_id(harness: _Harness) -> None:
    """Cross-tenant reads are impossible by construction rather than refused.

    A route that took a tenant id would need a check, and a check can be forgotten.
    None of these take one, so the tenant always comes from the verified context.
    """
    app_harness, _ = harness
    usage_routes = [r for r in app_harness.app.routes if getattr(r, "path", "").startswith("/v1/admin/usage")]

    assert usage_routes, "no usage routes registered — this test would pass vacuously"
    for route in usage_routes:
        assert "tenant" not in route.path, f"{route.path} accepts a tenant identifier"


# ---------------------------------------------------------------------------
# Aggregate-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_usage_surface_exposes_no_per_event_route(harness: _Harness) -> None:
    """The invariant that keeps the raw table from becoming an activity log.

    Asserted against the app's route table rather than by reading the router,
    because the way a per-event endpoint arrives is somebody adding one to a file
    where three aggregate endpoints already live.
    """
    app_harness, _ = harness
    paths = {getattr(r, "path", "") for r in app_harness.app.routes}
    usage_paths = {p for p in paths if p.startswith("/v1/admin/usage")}

    assert usage_paths == {
        "/v1/admin/usage/summary",
        "/v1/admin/usage/series",
        "/v1/admin/usage/tools",
        "/v1/admin/usage/capabilities",
    }, f"the usage surface changed shape: {sorted(usage_paths)}"

    # An event id in a path is the specific shape being ruled out.
    for path in usage_paths:
        assert "{" not in path, f"{path} takes a path parameter, which no aggregate read needs"


@pytest.mark.asyncio
async def test_no_usage_response_model_can_carry_an_actor_id(harness: _Harness) -> None:
    """A count of actors is the only actor-shaped field allowed out.

    Checked against the generated OpenAPI schema, so it covers whatever the routes
    actually declare rather than what the module happens to define.
    """
    app_harness, client = harness
    schema = app_harness.app.openapi()

    usage_ops = [
        op
        for path, item in schema["paths"].items()
        if path.startswith("/v1/admin/usage")
        for op in item.values()
    ]
    assert usage_ops, "no usage operations in the schema — this test would pass vacuously"

    refs: set[str] = set()
    for op in usage_ops:
        content = op.get("responses", {}).get("200", {}).get("content", {})
        ref = content.get("application/json", {}).get("schema", {}).get("$ref")
        if ref:
            refs.add(ref.rsplit("/", 1)[-1])

    schemas = schema["components"]["schemas"]

    def fields_of(name: str, seen: set[str]) -> list[tuple[str, dict]]:
        if name in seen:
            return []
        seen.add(name)
        out: list[tuple[str, dict]] = []
        for field, spec in schemas.get(name, {}).get("properties", {}).items():
            out.append((field, spec))
            nested = spec.get("items", {}).get("$ref") or spec.get("$ref")
            if nested:
                out += fields_of(nested.rsplit("/", 1)[-1], seen)
        return out

    fields: list[tuple[str, dict]] = []
    for ref in refs:
        fields += fields_of(ref, set())

    assert fields, "no fields resolved — this test would pass vacuously"

    def is_identifier(spec: dict) -> bool:
        """UUID-shaped, directly or through a nullable/array wrapper."""
        candidates = [spec, *spec.get("anyOf", []), spec.get("items", {})]
        return any(c.get("format") == "uuid" for c in candidates if isinstance(c, dict))

    # Asserted on the declared type rather than against a list of allowed names.
    # A name allowlist passes anything not yet thought of — `actor_ids`, say — and
    # what actually matters is that no actor reaches a caller as an identifier.
    # An actor-named field may be a count or an explanatory string; never an id.
    offenders = [f for f, spec in fields if "actor" in f and is_identifier(spec)]
    assert not offenders, f"usage responses must not identify actors: {sorted(offenders)}"

    # And the specific shapes, named, because they are the ones a reasonable person
    # would add while extending this and the type check alone would not explain why.
    banned = {"actor_id", "actor_ids", "actors"}
    present = {f for f, _ in fields} & banned
    assert not present, f"usage responses must aggregate actors, not list them: {sorted(present)}"


# ---------------------------------------------------------------------------
# Window handling at the edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_backwards_window_is_a_422_not_a_500(harness: _Harness) -> None:
    app_harness, client = harness
    persona = app_harness.add_persona(f"usage-range-{uuid.uuid4().hex[:6]}", roles=["admin"])
    await _materialise(app_harness, persona)

    resp = await _get(
        app_harness, client, persona, "/v1/admin/usage/summary?from=2026-05-20&to=2026-05-01"
    )

    assert resp.status_code == 422, resp.text
    assert "ends before it starts" in resp.text


@pytest.mark.asyncio
async def test_a_window_wider_than_the_cap_is_a_422(harness: _Harness) -> None:
    app_harness, client = harness
    persona = app_harness.add_persona(f"usage-wide-{uuid.uuid4().hex[:6]}", roles=["admin"])
    await _materialise(app_harness, persona)

    resp = await _get(app_harness, client, persona, "/v1/admin/usage/series?from=2020-01-01&to=2026-01-01")

    assert resp.status_code == 422, resp.text
    assert "maximum" in resp.text


@pytest.mark.asyncio
async def test_an_unknown_surface_filter_is_a_422(harness: _Harness) -> None:
    # An empty series for a typo'd filter would read as "no traffic on that
    # surface", which is a wrong answer rather than an error.
    app_harness, client = harness
    persona = app_harness.add_persona(f"usage-surface-{uuid.uuid4().hex[:6]}", roles=["admin"])
    await _materialise(app_harness, persona)

    resp = await _get(app_harness, client, persona, "/v1/admin/usage/series?surface=graphql")

    assert resp.status_code == 422, resp.text
    assert "unknown surface" in resp.text


@pytest.mark.asyncio
async def test_the_default_window_covers_the_last_thirty_days_including_today(
    pg_container: str, harness: _Harness
) -> None:
    """Today is included even though its rollup is still being recomputed.

    Anyone checking whether traffic they just generated arrived would otherwise see
    a surface that looked broken.
    """
    app_harness, client = harness
    persona = app_harness.add_persona(f"usage-default-{uuid.uuid4().hex[:6]}", roles=["admin"])
    tenant_id = await _materialise(app_harness, persona)
    await _seed(pg_container, tenant_id=tenant_id, calls=1, surface="mcp")

    resp = await _get(app_harness, client, persona, "/v1/admin/usage/summary")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["days"] == 30
    assert body["end"] == _TODAY.isoformat()
    assert body["surfaces"][0]["calls"] == 1


@pytest.mark.asyncio
async def test_the_tool_ranking_reaches_the_mcp_surface_only(pg_container: str, harness: _Harness) -> None:
    app_harness, client = harness
    persona = app_harness.add_persona(f"usage-tools-{uuid.uuid4().hex[:6]}", roles=["admin"])
    tenant_id = await _materialise(app_harness, persona)
    await _seed(pg_container, tenant_id=tenant_id, calls=2, surface="mcp")
    await _seed(pg_container, tenant_id=tenant_id, calls=5, surface="rest")

    resp = await _get(app_harness, client, persona, "/v1/admin/usage/tools")

    assert resp.status_code == 200, resp.text
    tools = resp.json()["tools"]
    assert [(t["tool"], t["calls"]) for t in tools] == [("get_capability", 2)]
