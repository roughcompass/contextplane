"""Unit tests for `registry/api/routers/_entity_crud.py` — the shared entity CRUD router factory.

`make_entity_router` builds identical POST/GET/PATCH/DELETE handlers for every
parent-anchored entity type (concepts via `concept_of`, operations via
`operation_of`, and any future type wired the same way) — a defect in the
factory is a defect in every router it produces at once. `concepts.py` and
`operations.py` each call the factory once and are already exercised
incidentally by `test_etag_if_match_rollout.py` (If-Match handling) and
`test_hateoas_links.py` (`_links` shape), but those suites only reach the
create/read/update happy path. This suite targets what they leave behind,
using `concepts.py`'s router — one concrete instantiation of the factory —
as the vehicle:

- POST /v1/concepts — an idempotency-key cache hit returns the stored
  response and never calls the service at all
- POST ...           — `parent_capability_id` supplied creates the
  `concept_of` edge; omitted, it does not
- POST ...           — a service-raised `CatalogError` subtype is mapped
  through `map_catalog_error`, not left to propagate raw
- GET  /v1/concepts/{id}     — same error-mapping guarantee on the read path
- GET  ... ?view=audit       — `tenant_id` is populated only under `audit`
- PATCH /v1/concepts/{id}    — same error-mapping guarantee on the update path
- DELETE /v1/concepts/{id}   — success -> 204; the handler's two `except`
  clauses are distinguished: `NotFoundError` takes its own dedicated 404
  branch, any other `CatalogError` falls through to `map_catalog_error`
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.middleware.idempotency import get_idempotency_context
from registry.api.middleware.tenant import get_tenant_context
from registry.exceptions import ConflictError, NotFoundError
from registry.types import TenantContext
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()
_PARENT_ID = uuid.uuid4()


def _ctx(roles: list[str] | None = None) -> TenantContext:
    return tenant_context(tenant_id=_TENANT, actor_id=_ACTOR, roles=roles or ["producer"])


def _entity_ref(entity_id: uuid.UUID = _ENTITY_ID) -> MagicMock:
    ref = MagicMock()
    ref.entity_id = entity_id
    ref.tenant_id = _TENANT
    ref.entity_type = "concept"
    ref.name = "my-concept"
    ref.external_id = None
    ref.is_active = True
    ref.created_at = _NOW
    return ref


def _record(entity_id: uuid.UUID = _ENTITY_ID) -> MagicMock:
    record = MagicMock()
    record.entity = _entity_ref(entity_id)
    record.lifecycle = "draft"
    record.attributes = {}
    record.facts = []
    record.edges_out = []
    record.edges_in = []
    return record


def _build_app(*, ctx: TenantContext | None = None) -> tuple[FastAPI, MagicMock]:
    """App wired with `concepts.py`'s router/mutation_router.

    `concepts.py` is one concrete call to `make_entity_router`
    (`entity_type="concept"`, `parent_edge_rel="concept_of"`) — exercising it
    here exercises the factory's own generated handlers directly.
    """
    from registry.api.routers.concepts import mutation_router, router

    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    catalog_svc = MagicMock()
    catalog_svc.create_entity = AsyncMock(return_value=_entity_ref())
    catalog_svc.create_edge = AsyncMock(return_value=None)
    catalog_svc.get_full_capability = AsyncMock(return_value=_record())
    catalog_svc.resolve_entity_handle = AsyncMock(return_value=_entity_ref())
    catalog_svc.update_entity = AsyncMock(return_value=None)
    catalog_svc.delete_entity = AsyncMock(return_value=None)
    app.state.catalog = catalog_svc

    effective_ctx = ctx or _ctx()

    async def _fake_ctx() -> TenantContext:
        return effective_ctx

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app, catalog_svc


# ---------------------------------------------------------------------------
# POST /v1/concepts — idempotency-key cache hit
# ---------------------------------------------------------------------------


class TestCreateIdempotencyHit:
    def test_cache_hit_returns_stored_response_without_calling_the_service(self) -> None:
        app, catalog_svc = _build_app()

        fake_idem = MagicMock()
        fake_idem.lookup = AsyncMock(return_value=(200, {"entity_id": str(_ENTITY_ID), "cached": True}))
        fake_idem.persist = AsyncMock(return_value=None)

        async def _fake_idem() -> object:
            return fake_idem

        app.dependency_overrides[get_idempotency_context] = _fake_idem

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/concepts",
            json={"name": "my-concept"},
            headers={"Idempotency-Key": "retry-1"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"entity_id": str(_ENTITY_ID), "cached": True}
        catalog_svc.create_entity.assert_not_awaited()
        fake_idem.persist.assert_not_awaited()


# ---------------------------------------------------------------------------
# POST /v1/concepts — optional parent_capability_id -> concept_of edge
# ---------------------------------------------------------------------------


class TestCreateParentEdge:
    def test_parent_capability_id_creates_the_parent_edge(self) -> None:
        app, catalog_svc = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/concepts",
            json={"name": "my-concept", "parent_capability_id": str(_PARENT_ID)},
        )
        assert resp.status_code == 201, resp.text
        catalog_svc.create_edge.assert_awaited_once()
        call = catalog_svc.create_edge.await_args
        assert call.kwargs["rel"] == "concept_of"
        assert call.kwargs["dst_entity_id"] == _PARENT_ID
        assert call.kwargs["src_entity_id"] == _ENTITY_ID

    def test_no_parent_capability_id_never_creates_an_edge(self) -> None:
        app, catalog_svc = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/v1/concepts", json={"name": "my-concept"})
        assert resp.status_code == 201, resp.text
        catalog_svc.create_edge.assert_not_awaited()


# ---------------------------------------------------------------------------
# POST /v1/concepts — CatalogError mapping
# ---------------------------------------------------------------------------


class TestCreateCatalogErrorMapping:
    def test_service_conflict_is_mapped_through_map_catalog_error(self) -> None:
        app, catalog_svc = _build_app()
        catalog_svc.create_entity = AsyncMock(side_effect=ConflictError("slug already exists"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/v1/concepts", json={"name": "my-concept"})
        assert resp.status_code == 409, resp.text
        assert "slug already exists" in resp.text


# ---------------------------------------------------------------------------
# GET /v1/concepts/{id} — CatalogError mapping + ?view=audit
# ---------------------------------------------------------------------------


class TestGetCatalogErrorMapping:
    def test_not_found_is_mapped_through_map_catalog_error(self) -> None:
        app, catalog_svc = _build_app()
        catalog_svc.resolve_entity_handle = AsyncMock(side_effect=NotFoundError(f"concept {_ENTITY_ID} not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/v1/concepts/{_ENTITY_ID}")
        assert resp.status_code == 404, resp.text
        assert f"concept {_ENTITY_ID} not found" in resp.text


class TestGetAuditView:
    def test_default_view_omits_tenant_id(self) -> None:
        app, _ = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/concepts/{_ENTITY_ID}")
        assert resp.status_code == 200, resp.text
        assert "tenant_id" not in resp.json()

    def test_audit_view_populates_tenant_id(self) -> None:
        app, _ = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/concepts/{_ENTITY_ID}", params={"view": "audit"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["tenant_id"] == str(_TENANT)


# ---------------------------------------------------------------------------
# PATCH /v1/concepts/{id} — CatalogError mapping
# ---------------------------------------------------------------------------


class TestPatchCatalogErrorMapping:
    def test_service_conflict_is_mapped_through_map_catalog_error(self) -> None:
        app, catalog_svc = _build_app()
        catalog_svc.update_entity = AsyncMock(side_effect=ConflictError("update conflicts with a pending change"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(f"/v1/concepts/{_ENTITY_ID}", json={"updates": {}})
        assert resp.status_code == 409, resp.text
        assert "update conflicts with a pending change" in resp.text


# ---------------------------------------------------------------------------
# DELETE /v1/concepts/{id} — success, and the two except clauses
# ---------------------------------------------------------------------------


class TestDelete:
    def test_success_returns_204_and_calls_delete_entity(self) -> None:
        app, catalog_svc = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.delete(f"/v1/concepts/{_ENTITY_ID}")
        assert resp.status_code == 204, resp.text
        catalog_svc.delete_entity.assert_awaited_once()

    def test_not_found_takes_the_dedicated_404_branch(self) -> None:
        app, catalog_svc = _build_app()
        catalog_svc.resolve_entity_handle = AsyncMock(side_effect=NotFoundError(f"concept {_ENTITY_ID} not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete(f"/v1/concepts/{_ENTITY_ID}")
        assert resp.status_code == 404, resp.text
        assert f"concept {_ENTITY_ID} not found" in resp.text
        catalog_svc.delete_entity.assert_not_awaited()

    def test_other_catalog_error_falls_through_to_map_catalog_error(self) -> None:
        """A `CatalogError` that is not a `NotFoundError` skips the dedicated
        404 branch above and is mapped by the general `except CatalogError`
        clause instead -- the two branches are reachable independently."""
        app, catalog_svc = _build_app()
        catalog_svc.delete_entity = AsyncMock(side_effect=ConflictError("cannot delete: has active dependents"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete(f"/v1/concepts/{_ENTITY_ID}")
        assert resp.status_code == 409, resp.text
        assert "cannot delete: has active dependents" in resp.text
