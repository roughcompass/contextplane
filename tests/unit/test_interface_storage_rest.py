"""Unit tests for the interface storage REST endpoints.

Service interactions mocked via AsyncMock; no DB or network involved.

Coverage:
- PUT happy path → 200 + InterfaceSurfaceResponse.
- PUT non-producer role → 403 (require_roles gate).
- PUT malformed format → 422 (ValidationError surfaced from service).
- PUT non-owner / missing capability → 404.
- GET current-truth happy path.
- GET as_of malformed → 422.
- GET no interface yet → 200 with null canonical.
- GET ?view=default (default) → 200 without audit fields.
- GET ?view=audit  → 200 accepted (no-op for interface; no extra fields surfaced).
- GET ?view=bad    → 422.
- The PUT mutation route's alias action is "replace", not "update" (a full
  replacement, unlike every PATCH-backed "update" alias elsewhere in the API).
"""

from __future__ import annotations

import ast
import datetime
import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.routers.interface import mutation_router as interface_mutation_router
from registry.api.routers.interface import router as interface_router
from registry.exceptions import NotFoundError, ValidationError
from registry.service.catalog.interface_storage import InterfaceRecord
from registry.types import EntityRef, InterfaceSurface, TenantContext
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CAP = uuid.uuid4()


def _ctx(roles: list[str] | None = None) -> TenantContext:
    # _TENANT/_ACTOR are this module's own constants -- only the id source
    # diverges from the other producer-role contexts, not the build logic.
    return tenant_context(tenant_id=_TENANT, actor_id=_ACTOR, roles=roles)


def _build_app(
    *,
    put_return: InterfaceSurface | None = None,
    put_effect: Exception | None = None,
    get_return: InterfaceRecord | None = None,
    get_effect: Exception | None = None,
    ctx: TenantContext | None = None,
) -> FastAPI:
    import datetime

    app = FastAPI()
    app.include_router(interface_router)
    app.include_router(interface_mutation_router)

    svc = MagicMock()
    if put_effect is not None:
        svc.put_interface = AsyncMock(side_effect=put_effect)
    else:
        svc.put_interface = AsyncMock(return_value=put_return or InterfaceSurface(operations=[], events=[], fields=[]))
    if get_effect is not None:
        svc.get_interface = AsyncMock(side_effect=get_effect)
    else:
        svc.get_interface = AsyncMock(
            return_value=get_return
            or InterfaceRecord(
                capability_id=_CAP,
                interface_canonical=None,
                interface_source=None,
                interface_format=None,
                as_of=None,
            )
        )
    app.state.interface_storage = svc

    # Catalog mock: resolve_entity_handle echoes the UUID from the path param.
    catalog_mock = MagicMock()

    async def _resolve(ctx_arg: TenantContext, handle: str, **_kw: object) -> EntityRef:
        return EntityRef(
            entity_id=uuid.UUID(handle),
            tenant_id=ctx_arg.tenant_id,
            entity_type="capability",
            name="cap",
            external_id=None,
            is_active=True,
            created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        )

    catalog_mock.resolve_entity_handle = _resolve
    app.state.catalog = catalog_mock

    from registry.api.middleware.tenant import get_tenant_context

    effective = ctx if ctx is not None else _ctx()

    async def _fake_ctx() -> TenantContext:
        return effective

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------


class TestPutInterface:
    def test_happy_path_returns_canonical_surface(self) -> None:
        canonical = InterfaceSurface(
            operations=[],
            events=[],
            fields=[{"name": "id", "type": "string", "required": True}],
        )
        app = _build_app(put_return=canonical)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.put(
            f"/v1/capabilities/{_CAP}/interface",
            json={
                "interface_source": "type X = { id: string; }",
                "interface_format": "typescript",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["fields"] == [{"name": "id", "type": "string", "required": True}]

    def test_non_producer_role_returns_403(self) -> None:
        app = _build_app(ctx=_ctx(roles=["consumer"]))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put(
            f"/v1/capabilities/{_CAP}/interface",
            json={"interface_source": {}, "interface_format": "json_schema"},
        )
        assert resp.status_code == 403

    def test_malformed_format_returns_422(self) -> None:
        app = _build_app(put_effect=ValidationError("bad format"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put(
            f"/v1/capabilities/{_CAP}/interface",
            json={"interface_source": {}, "interface_format": "graphql"},
        )
        assert resp.status_code == 422

    def test_not_found_returns_404(self) -> None:
        app = _build_app(put_effect=NotFoundError(f"{_CAP} not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.put(
            f"/v1/capabilities/{_CAP}/interface",
            json={"interface_source": {}, "interface_format": "json_schema"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


class TestGetInterface:
    def test_no_interface_returns_200_with_nulls(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP}/interface")
        assert resp.status_code == 200
        body = resp.json()
        assert body["interface_canonical"] is None
        assert body["interface_source"] is None
        assert body["interface_format"] is None
        assert body["as_of"] is None

    def test_current_truth_returns_canonical(self) -> None:
        canonical = InterfaceSurface(
            operations=[{"name": "ping", "method": "GET", "path": "/ping", "params": [], "returns": "object"}],
            events=[],
            fields=[],
        )
        record = InterfaceRecord(
            capability_id=_CAP,
            interface_canonical=canonical,
            interface_source={"format": "openapi", "raw": {"openapi": "3.0.0"}},
            interface_format="openapi",
            as_of=None,
        )
        app = _build_app(get_return=record)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP}/interface")
        assert resp.status_code == 200
        body = resp.json()
        assert body["interface_format"] == "openapi"
        assert body["interface_canonical"]["operations"][0]["name"] == "ping"

    def test_malformed_as_of_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/v1/capabilities/{_CAP}/interface",
            params={"as_of": "not-a-date"},
        )
        assert resp.status_code == 422

    def test_naive_as_of_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/v1/capabilities/{_CAP}/interface",
            params={"as_of": "2026-01-01T00:00:00"},  # no timezone
        )
        assert resp.status_code == 422

    def test_view_audit_accepted_as_no_op(self) -> None:
        """?view=audit is a no-op for the interface endpoint (composed record,
        no individual bitemporal rows exposed). Must return 200 without error."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/v1/capabilities/{_CAP}/interface",
            params={"view": "audit"},
        )
        assert resp.status_code == 200

    def test_invalid_view_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            f"/v1/capabilities/{_CAP}/interface",
            params={"view": "raw"},
        )
        assert resp.status_code == 422

    def test_default_view_omits_audit_fields(self) -> None:
        """Default shape must not contain valid_from / ingested_at keys."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/capabilities/{_CAP}/interface")
        assert resp.status_code == 200
        body = resp.json()
        assert "valid_from" not in body
        assert "ingested_at" not in body
        assert "t_valid_from" not in body


# ---------------------------------------------------------------------------
# Alias-action pin — PUT is "replace", not "update"
# ---------------------------------------------------------------------------


def test_put_interface_mutation_route_uses_replace_action() -> None:
    """PUT replaces the whole interface surface in one write, unlike every
    other "update" alias in this API, which backs a PATCH partial update.

    Parses the source rather than probing the built route: the module's
    default mode ("rest") registers no POST-tunneled alias at all, so there
    is nothing to introspect on the live router without the reload machinery
    ``tests/integration/test_http_methods_mode.py`` uses. Reading the
    ``add_mutation_route(..., action=...)`` call site directly pins the
    actual wiring decision instead of re-deriving it.
    """
    from registry.api.routers import interface as interface_module

    source = inspect.getsource(interface_module)
    tree = ast.parse(source)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_mutation_route"
    ]
    assert len(calls) == 1, f"expected exactly one add_mutation_route call, found {len(calls)}"

    actions = {kw.value.value for kw in calls[0].keywords if kw.arg == "action" and isinstance(kw.value, ast.Constant)}
    assert actions == {"replace"}, f"interface PUT mutation route action drifted to {actions}; expected 'replace'"
