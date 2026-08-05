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
- GET  /v1/memory/promotion-proposals             → 200 + list, next_cursor
- GET  ... ?cursor=<malformed>                     → 422 invalid_cursor
- GET  /v1/memory/promotion-proposals/{id}        → 200 + proposal view
- GET  ... missing / owned by another tenant       → 404 (same shape either way)
- PATCH /v1/memory/promotion-proposals/{id}       → 200 + {proposal, promotion_id}
- PATCH ... state=accepted (+ amended_value)       → PromotionService.accept
- PATCH ... state=rejected (+ reason)              → PromotionService.reject
- PATCH ... not found / conflict / role / bad state → 404 / 409 / 403 / 422
- PATCH ... amended_value with state=rejected       → 422 (cross-field guard)
- PATCH ... reason with state=accepted              → 422 (cross-field guard)
- The PATCH route is registered under both surfaces (PATCH + POST
  `:update` alias) because ``tests/conftest.py`` forces
  ``REGISTRY_HTTP_METHODS_MODE=both`` for the whole unit-test session --
  unlike :link/:discard, this route has a real alternate verb to switch.
- POST /v1/memory/promotions/{id}:reverse          → 200 + {"status": "reversed"}
- POST ... not found / conflict / role              → 404 / 409 / 403
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from registry.api.routers.memory_curation import mutation_router, router
from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.service.memory.claims import StagedClaim
from registry.service.memory.curation_queue import QueueItem
from registry.service.memory.promotion import Proposal
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_CLAIM_ID = uuid.uuid4()
_SUBJECT_ID = uuid.uuid4()
_PROPOSAL_ID = uuid.uuid4()
_PROMOTION_ID = uuid.uuid4()


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


def _proposal(**overrides: object) -> Proposal:
    defaults: dict[str, object] = dict(
        proposal_id=_PROPOSAL_ID,
        claim_id=_CLAIM_ID,
        owner_tenant_id=_TENANT,
        author_tenant_id=_TENANT,
        subject_entity_id=_SUBJECT_ID,
        predicate="owned_by_team",
        target_kind="attribute",
        target_key="owned_by_team",
        current_value=None,
        proposed_value="platform",
        valid_from=_NOW,
        valid_to=None,
        high_impact_reasons=(),
        state="open",
        created_at=_NOW,
    )
    defaults.update(overrides)
    return Proposal(**defaults)  # type: ignore[arg-type]


def _build_app(
    *,
    items_return: tuple[QueueItem, ...] = (),
    counts_return: dict[str, int] | None = None,
    link_return: StagedClaim | None = None,
    link_effect: Exception | None = None,
    discard_effect: Exception | None = None,
    proposals_return: tuple[Proposal, ...] = (),
    get_proposal_return: Proposal | None = None,
    accept_return: uuid.UUID | None = None,
    accept_effect: Exception | None = None,
    reject_effect: Exception | None = None,
    reverse_effect: Exception | None = None,
    ctx: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

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

    promotion = MagicMock()
    promotion.proposals_for = AsyncMock(return_value=proposals_return)
    promotion.get_proposal = AsyncMock(return_value=get_proposal_return)
    if accept_effect is not None:
        promotion.accept = AsyncMock(side_effect=accept_effect)
    else:
        promotion.accept = AsyncMock(return_value=accept_return or _PROMOTION_ID)
    if reject_effect is not None:
        promotion.reject = AsyncMock(side_effect=reject_effect)
    else:
        promotion.reject = AsyncMock(return_value=None)
    if reverse_effect is not None:
        promotion.reverse = AsyncMock(side_effect=reverse_effect)
    else:
        promotion.reverse = AsyncMock(return_value=None)

    app.state.services = MagicMock(curation_queue=queue, claims=claims, promotion=promotion)

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
# GET /v1/memory/promotion-proposals
# ---------------------------------------------------------------------------


class TestListPromotionProposals:
    def test_returns_200_and_items(self) -> None:
        app = _build_app(proposals_return=(_proposal(),))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/v1/memory/promotion-proposals")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["proposal_id"] == str(_PROPOSAL_ID)
        assert item["state"] == "open"
        assert item["high_impact"] is False
        assert body["next_cursor"] is None

    def test_defaults_to_open_state(self) -> None:
        app = _build_app(proposals_return=())
        client = TestClient(app, raise_server_exceptions=True)
        client.get("/v1/memory/promotion-proposals")
        call = app.state.services.promotion.proposals_for.await_args
        assert call.kwargs["state"] == "open"
        assert call.args[0] == _TENANT

    def test_bad_state_value_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/memory/promotion-proposals?state=not-a-real-state")
        assert resp.status_code == 422

    def test_more_than_a_page_sets_next_cursor(self) -> None:
        items = tuple(_proposal(proposal_id=uuid.uuid4()) for _ in range(3))
        app = _build_app(proposals_return=items)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/v1/memory/promotion-proposals?page_size=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

    def test_malformed_cursor_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/v1/memory/promotion-proposals?cursor=not-valid-base64!!!")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/memory/promotion-proposals/{id}
# ---------------------------------------------------------------------------


class TestGetPromotionProposal:
    def test_returns_200_and_proposal(self) -> None:
        app = _build_app(get_proposal_return=_proposal())
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get(f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["proposal_id"] == str(_PROPOSAL_ID)
        assert body["claim_id"] == str(_CLAIM_ID)
        assert body["proposed_value"] == "platform"

    def test_missing_returns_404(self) -> None:
        app = _build_app(get_proposal_return=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}")
        assert resp.status_code == 404

    def test_foreign_tenant_returns_the_same_404(self) -> None:
        """Owned-by-someone-else and absent produce an identical error --
        a proposal id is not a cross-tenant existence oracle."""
        stranger_tenant = uuid.uuid4()
        app = _build_app(get_proposal_return=_proposal(owner_tenant_id=stranger_tenant))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /v1/memory/promotion-proposals/{id}
# ---------------------------------------------------------------------------


class TestReviewPromotionProposal:
    def test_accept_returns_200_and_promotion_id(self) -> None:
        app = _build_app(
            accept_return=_PROMOTION_ID,
            get_proposal_return=_proposal(state="accepted"),
        )
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "accepted"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["promotion_id"] == str(_PROMOTION_ID)
        assert body["proposal"]["state"] == "accepted"
        call = app.state.services.promotion.accept.await_args
        assert call.args[0] == _PROPOSAL_ID
        assert call.kwargs["actor_tenant_id"] == _TENANT
        assert call.kwargs["actor_id"] == _ACTOR
        assert call.kwargs["roles"] == frozenset({"producer"})
        assert "amended_value" not in call.kwargs

    def test_accept_with_amended_value_passes_it_through(self) -> None:
        app = _build_app(
            accept_return=_PROMOTION_ID,
            get_proposal_return=_proposal(state="amended"),
        )
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "accepted", "amended_value": "corrected-team"},
        )
        assert resp.status_code == 200, resp.text
        call = app.state.services.promotion.accept.await_args
        assert call.kwargs["amended_value"] == "corrected-team"

    def test_accept_with_amended_value_null_is_distinct_from_omitted(self) -> None:
        """An explicit `null` amendment must reach the service as
        `amended_value=None`, not be treated as though the field were never
        sent -- the two mean different things (promote the claim's own
        value vs. promote a null)."""
        app = _build_app(
            accept_return=_PROMOTION_ID,
            get_proposal_return=_proposal(state="amended"),
        )
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "accepted", "amended_value": None},
        )
        assert resp.status_code == 200, resp.text
        call = app.state.services.promotion.accept.await_args
        assert "amended_value" in call.kwargs
        assert call.kwargs["amended_value"] is None

    def test_reject_returns_200_with_no_promotion_id(self) -> None:
        app = _build_app(get_proposal_return=_proposal(state="rejected"))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "rejected", "reason": "incorrect"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["promotion_id"] is None
        assert body["proposal"]["state"] == "rejected"
        call = app.state.services.promotion.reject.await_args
        assert call.kwargs["reason"] == "incorrect"

    def test_reject_without_reason_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "rejected"},
        )
        assert resp.status_code == 422

    def test_amended_value_with_rejected_state_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "rejected", "reason": "incorrect", "amended_value": "x"},
        )
        assert resp.status_code == 422

    def test_reason_with_accepted_state_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "accepted", "reason": "incorrect"},
        )
        assert resp.status_code == 422

    def test_bad_state_value_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "amended"},
        )
        assert resp.status_code == 422

    def test_not_found_returns_404(self) -> None:
        app = _build_app(accept_effect=NotFoundError("no such proposal"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "accepted"},
        )
        assert resp.status_code == 404

    def test_already_decided_returns_409(self) -> None:
        app = _build_app(accept_effect=ConflictError("proposal is already accepted"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "accepted"},
        )
        assert resp.status_code == 409

    def test_non_owner_returns_403(self) -> None:
        app = _build_app(accept_effect=PermissionError("only the owning tenant may act"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "accepted"},
        )
        assert resp.status_code == 403

    def test_unknown_field_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}",
            json={"state": "accepted", "unexpected": "field"},
        )
        assert resp.status_code == 422

    def test_post_tunnel_alias_reaches_the_same_handler(self) -> None:
        """`REGISTRY_HTTP_METHODS_MODE=both` (forced by tests/conftest.py for
        the whole unit-test session) registers the POST `:update` alias
        alongside the PATCH verb route -- both must reach the same review
        logic."""
        app = _build_app(
            accept_return=_PROMOTION_ID,
            get_proposal_return=_proposal(state="accepted"),
        )
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            f"/v1/memory/promotion-proposals/{_PROPOSAL_ID}:update",
            json={"state": "accepted"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["promotion_id"] == str(_PROMOTION_ID)


# ---------------------------------------------------------------------------
# POST /v1/memory/promotions/{id}:reverse
# ---------------------------------------------------------------------------


class TestReversePromotion:
    def test_returns_200_and_reversed_status(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            f"/v1/memory/promotions/{_PROMOTION_ID}:reverse",
            json={"reason": "the underlying source corrected itself"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "reversed"}
        call = app.state.services.promotion.reverse.await_args
        assert call.args[0] == _PROMOTION_ID
        assert call.kwargs["reason"] == "the underlying source corrected itself"
        assert call.kwargs["actor_tenant_id"] == _TENANT
        assert call.kwargs["roles"] == frozenset({"producer"})

    def test_not_found_returns_404(self) -> None:
        app = _build_app(reverse_effect=NotFoundError("no such promotion"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/promotions/{_PROMOTION_ID}:reverse", json={"reason": "x"})
        assert resp.status_code == 404

    def test_already_reversed_returns_409(self) -> None:
        app = _build_app(reverse_effect=ConflictError("promotion was already reversed"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/promotions/{_PROMOTION_ID}:reverse", json={"reason": "x"})
        assert resp.status_code == 409

    def test_non_owner_returns_403(self) -> None:
        app = _build_app(reverse_effect=PermissionError("only the owning tenant may reverse"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/promotions/{_PROMOTION_ID}:reverse", json={"reason": "x"})
        assert resp.status_code == 403

    def test_empty_reason_returns_422(self) -> None:
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/v1/memory/promotions/{_PROMOTION_ID}:reverse", json={"reason": ""})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRouteRegistrationIsModeIndependent:
    def test_only_the_literal_action_paths_are_registered(self) -> None:
        """No `:link:link` / `:discard:discard` double-suffixed alias exists --
        these routes never go through HttpMethodRouter's mutation-mode switch,
        so there is nothing for REGISTRY_HTTP_METHODS_MODE to add or remove."""
        paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
        assert "/v1/memory/claims/{claim_id}:link" in paths
        assert "/v1/memory/claims/{claim_id}:discard" in paths
        assert "/v1/memory/promotions/{promotion_id}:reverse" in paths
        assert not any(p.endswith(":link:link") or p.endswith(":discard:discard") for p in paths)

    def test_promotion_proposal_review_registers_both_surfaces(self) -> None:
        """Unlike :link/:discard/:reverse, this route has a genuine
        alternate verb -- both the PATCH route and its POST `:update` alias
        must exist on `mutation_router` (mode is forced to `both` for the
        whole unit-test session by ``tests/conftest.py``)."""
        by_path: dict[str, set[str]] = {}
        for r in mutation_router.routes:
            by_path.setdefault(r.path, set()).update(r.methods)  # type: ignore[attr-defined]
        assert by_path.get("/v1/memory/promotion-proposals/{proposal_id}") == {"PATCH"}
        assert by_path.get("/v1/memory/promotion-proposals/{proposal_id}:update") == {"POST"}
