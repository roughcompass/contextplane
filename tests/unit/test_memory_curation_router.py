"""Unit tests for the memory curation router.

Service interactions are mocked on ``app.state.services`` (the typed
container, not a bare ``app.state.<name>`` attribute); no DB or network is
involved.

Coverage:
- GET  /v1/memory/curation-queue                → 200 + item list, next_cursor
- GET  ... ?counts=true                          → 200 + per-reason tally
- GET  ... ?cursor=<malformed>                   → 422 invalid_cursor
- GET  ... ?page_size=0 / page_size=too-large     → 422 (Query validation)
- POST /v1/memory/claims/{id}:link               → 200 + linked claim view
- POST ... not found / conflict / bad ref / role  → 404 / 409 / 422 / 403
- POST ... extra field in body                    → 422 (extra="forbid")
- POST /v1/memory/claims/{id}:discard            → 200 + {"status": "discarded"}
- POST ... not found / conflict / role             → 404 / 409 / 403
- Both :link/:discard routes are plain POSTs, reachable regardless of
  REGISTRY_HTTP_METHODS_MODE -- there is no alternate verb to switch between.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.routers.memory_curation import router
from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.service.memory.claims import StagedClaim
from registry.service.memory.curation_queue import QueueItem
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CLAIM_ID = uuid.uuid4()
_SUBJECT_ID = uuid.uuid4()


def _queue_item(
    *,
    reason: str = "unlinked",
    claim_id: uuid.UUID | None = None,
    subject_entity_id: uuid.UUID | None = None,
    proposal_id: uuid.UUID | None = None,
) -> QueueItem:
    return QueueItem(
        claim_id=claim_id or _CLAIM_ID,
        reason=reason,
        subject_reference="github:acme/mystery",
        subject_entity_id=subject_entity_id,
        predicate="owned_by_team",
        value="platform",
        confidence=None,
        created_at=_NOW,
        human_backed=False,
        proposal_id=proposal_id,
    )


def _staged_claim(**overrides: object) -> StagedClaim:
    defaults: dict[str, object] = dict(
        claim_id=_CLAIM_ID,
        subject_entity_id=_SUBJECT_ID,
        predicate="owned_by_team",
        value="platform",
        status="staged",
        visibility="tenant-shared",
        owning_tenant_id=_TENANT,
        source_authority="owner_human",
        is_contested=False,
    )
    defaults.update(overrides)
    return StagedClaim(**defaults)  # type: ignore[arg-type]


def _build_app(
    *,
    items_return: tuple[QueueItem, ...] = (),
    counts_return: dict[str, int] | None = None,
    link_return: StagedClaim | None = None,
    link_effect: Exception | None = None,
    discard_effect: Exception | None = None,
    ctx: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    queue = MagicMock()
    queue.items_for = AsyncMock(return_value=items_return)
    queue.counts_for = AsyncMock(return_value=counts_return or {})

    claims = MagicMock()
    if link_effect is not None:
        claims.link_subject = AsyncMock(side_effect=link_effect)
    else:
        claims.link_subject = AsyncMock(return_value=link_return or _staged_claim())
    if discard_effect is not None:
        claims.discard = AsyncMock(side_effect=discard_effect)
    else:
        claims.discard = AsyncMock(return_value=None)

    app.state.services = MagicMock(curation_queue=queue, claims=claims)

    from registry.api.middleware.tenant import get_tenant_context

    effective_ctx = ctx if ctx is not None else tenant_context(tenant_id=_TENANT, actor_id=_ACTOR, roles=["producer"])

    async def _fake_ctx() -> object:
        return effective_ctx

    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


# ---------------------------------------------------------------------------
# GET /v1/memory/curation-queue
# ---------------------------------------------------------------------------


class TestGetCurationQueue:
    def test_returns_200_and_items(self) -> None:
        app = _build_app(items_return=(_queue_item(),))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/v1/memory/curation-queue")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["claim_id"] == str(_CLAIM_ID)
        assert item["reason"] == "unlinked"
        assert item["available_actions"] == ["link", "discard"]
        assert body["next_cursor"] is None

    def test_empty_queue_returns_200_and_empty_list(self) -> None:
        app = _build_app(items_return=())
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/v1/memory/curation-queue")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "next_cursor": None}

    def test_more_than_a_page_sets_next_cursor(self) -> None:
        # page_size + 1 rows returned by the service signals another page.
        items = tuple(_queue_item(claim_id=uuid.uuid4()) for _ in range(3))
        app = _build_app(items_return=items)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/v1/memory/curation-queue?page_size=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

    def test_scopes_by_the_caller_tenant(self) -> None:
        app = _build_app(items_return=())
        client = TestClient(app, raise_server_exceptions=True)
        client.get("/v1/memory/curation-queue")
        call = app.state.services.curation_queue.items_for.await_args
        assert call.args[0] == _TENANT

    def test_counts_true_returns_tally_not_items(self) -> None:
        app = _build_app(counts_return={"unlinked": 3, "contested": 1})
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/v1/memory/curation-queue?counts=true")
        assert resp.status_code == 200
        assert resp.json() == {"counts": {"unlinked": 3, "contested": 1}}
        app.state.services.curation_queue.counts_for.assert_awaited_once()
        app.state.services.curation_queue.items_for.assert_not_awaited()

    def test_malformed_cursor_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/memory/curation-queue?cursor=not-valid-base64!!!")
        assert resp.status_code == 422

    def test_page_size_zero_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/memory/curation-queue?page_size=0")
        assert resp.status_code == 422

    def test_page_size_over_max_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/memory/curation-queue?page_size=100000")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /v1/memory/claims/{id}:link
# ---------------------------------------------------------------------------


class TestLinkClaimSubject:
    def test_returns_200_and_linked_claim(self) -> None:
        app = _build_app(link_return=_staged_claim())
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            f"/v1/memory/claims/{_CLAIM_ID}:link",
            json={"subject_reference": "github:acme/widget"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["claim_id"] == str(_CLAIM_ID)
        assert body["status"] == "staged"
        assert body["subject_entity_id"] == str(_SUBJECT_ID)
        call = app.state.services.claims.link_subject.await_args
        assert call.kwargs["claim_id"] == _CLAIM_ID
        assert call.kwargs["subject_reference"] == "github:acme/widget"

    def test_not_found_returns_404(self) -> None:
        app = _build_app(link_effect=NotFoundError("claim not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:link", json={"subject_reference": "x"})
        assert resp.status_code == 404

    def test_conflict_returns_409(self) -> None:
        app = _build_app(link_effect=ConflictError("already staged"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:link", json={"subject_reference": "x"})
        assert resp.status_code == 409

    def test_unresolvable_reference_returns_422(self) -> None:
        app = _build_app(link_effect=ValidationError("still does not resolve"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:link", json={"subject_reference": "nowhere"})
        assert resp.status_code == 422

    def test_non_curator_role_returns_403(self) -> None:
        app = _build_app(link_effect=PermissionError("requires producer or admin"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:link", json={"subject_reference": "x"})
        assert resp.status_code == 403

    def test_empty_subject_reference_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:link", json={"subject_reference": ""})
        assert resp.status_code == 422

    def test_unknown_field_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/v1/memory/claims/{_CLAIM_ID}:link",
            json={"subject_reference": "x", "unexpected": "field"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /v1/memory/claims/{id}:discard
# ---------------------------------------------------------------------------


class TestDiscardClaim:
    def test_returns_200_and_discarded_status(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            f"/v1/memory/claims/{_CLAIM_ID}:discard",
            json={"reason": "wrong team, corrected verbally"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "discarded"}
        call = app.state.services.claims.discard.await_args
        assert call.kwargs["claim_id"] == _CLAIM_ID
        assert call.kwargs["reason"] == "wrong team, corrected verbally"

    def test_not_found_returns_404(self) -> None:
        app = _build_app(discard_effect=NotFoundError("claim not found"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:discard", json={"reason": "x"})
        assert resp.status_code == 404

    def test_already_rejected_returns_409(self) -> None:
        app = _build_app(discard_effect=ConflictError("nothing to discard"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:discard", json={"reason": "x"})
        assert resp.status_code == 409

    def test_non_curator_role_returns_403(self) -> None:
        app = _build_app(discard_effect=PermissionError("requires producer or admin"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:discard", json={"reason": "x"})
        assert resp.status_code == 403

    def test_empty_reason_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/claims/{_CLAIM_ID}:discard", json={"reason": ""})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Registration is unconditional -- no HttpMethodRouter mode dependency
# ---------------------------------------------------------------------------


class TestRouteRegistrationIsModeIndependent:
    def test_only_the_literal_action_paths_are_registered(self) -> None:
        """No `:link:link` / `:discard:discard` double-suffixed alias exists --
        these routes never go through HttpMethodRouter's mutation-mode switch,
        so there is nothing for REGISTRY_HTTP_METHODS_MODE to add or remove."""
        paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
        assert "/v1/memory/claims/{claim_id}:link" in paths
        assert "/v1/memory/claims/{claim_id}:discard" in paths
        assert not any(p.endswith(":link:link") or p.endswith(":discard:discard") for p in paths)
