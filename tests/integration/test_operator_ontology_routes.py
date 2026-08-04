"""The operator surface for organization-scope claim predicates.

These routes have no tenant. Authorization is an exact deployment identity, not
a role — every role in this system is tenant-scoped, so no role can serve as the
deployment trust root, and a tenant admin reaching a deployment-wide write is
precisely what the separate path exists to prevent.
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
async def admin(harness: EntitlementAuthHarness, client: AsyncClient):
    """A tenant admin — the highest role a tenant can grant."""
    p = harness.add_persona(f"onto-{uuid.uuid4().hex[:6]}", roles=["admin"])
    harness.configure_fetcher_for(p)
    with patch_validator_for_actor(p):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=p.slug))
        assert resp.status_code == 200, resp.text
    return p


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "value": f"pred_{uuid.uuid4().hex[:8]}",
        "value_type": "entity_ref",
        "claim_category": "dependency",
        "definition": "a predicate",
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio(loop_scope="module")
async def test_the_operator_routes_are_registered(harness: EntitlementAuthHarness) -> None:
    paths = {r.path for r in harness.app.routes if hasattr(r, "path")}
    assert "/v1/operator/claim-predicates" in paths
    assert "/v1/operator/claim-predicates/{value}/deprecate" in paths


@pytest.mark.asyncio(loop_scope="module")
async def test_the_operator_routes_are_not_under_tenant_admin(
    harness: EntitlementAuthHarness,
) -> None:
    """Kept off `/v1/admin` deliberately. Everything there is authorized by a
    role within a tenant; these are not, and putting them together invites the
    exact confusion that would let a tenant admin make a deployment-wide
    write."""
    admin_paths = [r.path for r in harness.app.routes if getattr(r, "path", "").startswith("/v1/admin")]
    assert not any("claim-predicate" in p for p in admin_paths)


@pytest.mark.asyncio(loop_scope="module")
async def test_a_tenant_admin_cannot_define_a_global_predicate(client: AsyncClient, admin) -> None:
    """The property that matters. A predicate defined here binds every tenant,
    so no tenant's own admin may create one."""
    with patch_validator_for_actor(admin):
        resp = await client.post(
            "/v1/operator/claim-predicates",
            json=_body(),
            headers=bearer_headers(tenant_slug=admin.slug),
        )
    assert resp.status_code == 403
    assert resp.json()["errors"][0]["code"] == "forbidden"


@pytest.mark.asyncio(loop_scope="module")
async def test_a_tenant_admin_cannot_deprecate_a_global_predicate(client: AsyncClient, admin) -> None:
    with patch_validator_for_actor(admin):
        resp = await client.post(
            "/v1/operator/claim-predicates/depends_on/deprecate",
            headers=bearer_headers(tenant_slug=admin.slug),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio(loop_scope="module")
async def test_a_tenant_admin_cannot_inventory_other_tenants_predicates(client: AsyncClient, admin) -> None:
    """The inventory names which tenants invented which terms. That is
    governance information, not something one tenant may read about another."""
    with patch_validator_for_actor(admin):
        resp = await client.get(
            "/v1/operator/claim-predicates/local-inventory",
            headers=bearer_headers(tenant_slug=admin.slug),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio(loop_scope="module")
async def test_the_operator_routes_require_authentication(client: AsyncClient) -> None:
    assert (await client.get("/v1/operator/claim-predicates")).status_code == 401
    assert (await client.post("/v1/operator/claim-predicates", json=_body())).status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_a_misspelled_field_is_rejected_rather_than_dropped(client: AsyncClient, admin) -> None:
    """A predicate created with a silently-dropped field would be missing the
    metadata everything else validates against."""
    with patch_validator_for_actor(admin):
        resp = await client.post(
            "/v1/operator/claim-predicates",
            json=_body(valuetype="entity_ref"),
            headers=bearer_headers(tenant_slug=admin.slug),
        )
    assert resp.status_code == 422
