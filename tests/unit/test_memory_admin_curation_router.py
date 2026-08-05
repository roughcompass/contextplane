"""Unit tests for the memory-curation admin router.

Service interactions are mocked on ``app.state.services`` (the typed
container); the bare module functions ``load_policy``/``set_policy`` are
monkeypatched on the router module directly, the same way
``test_memory_curation_router.py`` patches ``stage_claim_defended`` --
neither is a service method reachable off the container.

Coverage:
- GET  /v1/admin/memory-promotion-policy          → 200 + policy view
- GET  ... non-admin role                          → 403
- PUT  /v1/admin/memory-promotion-policy           → 200 + updated policy
  view (registered under both PUT and the POST `:replace` alias, since
  ``tests/conftest.py`` forces ``REGISTRY_HTTP_METHODS_MODE=both``)
- PUT  ... out-of-range confidence_floor / negative threshold
                                                    → 422 (view-model bounds,
  never reaches ``set_policy``)
- PUT  ... service-level ValidationError / PermissionError
                                                    → 422 / 403
- PUT  ... non-admin role                          → 403
- GET  /v1/admin/memory-autopromote-allowlist       → 200 + sorted predicates
- GET  ... non-admin role                          → 403
- POST .../:allow                                   → 200 + updated list;
  ``GuardrailService.allow`` awaited with the right args
- POST .../:revoke                                  → 200 + updated list
- POST .../:allow ... empty predicate / extra field  → 422
- POST .../:allow / :revoke ... non-admin role       → 403
- GET  /v1/admin/memory-sources                     → 200 + declared sources
- POST /v1/admin/memory-sources                      → 201 + declared policy
- POST ... not found / bad tier / wrong tenant        → 404 / 422 / 403
- PATCH /v1/admin/memory-sources/{id}                → 200 + merged policy
  (omitted fields keep the current value, not ``declare``'s own defaults)
- PATCH ... unknown source / foreign-tenant source     → 404 (identical
  either way -- the router's own tenant check runs before ``declare``)
- PATCH ... service-level ValidationError              → 422
- PATCH ... non-admin role                             → 403
- POST .../{id}:reset-breaker                          → 200 + reloaded
  policy (breaker cleared)
- POST ... reset_breaker raises PermissionError          → 403
- POST ... non-admin role                                → 403
- GET  /v1/admin/memory-calibration                     → 200 + one row per
  triple, in whatever order ``active_mappings`` returned
- GET  ... non-admin role                                → 403
- POST /v1/admin/memory-calibration:refit                → 200 + the outcome
  ``refit_one`` returned; called with the body's triple and the ctx actor as
  ``fitted_by``
- POST ... below the evaluation floor                    → 200, `activated:
  false`, the `uncalibrated` version (the gate refuses quietly, not with an
  error status -- there is nothing wrong with the request)
- POST ... missing field / extra field                   → 422
- POST ... non-admin role                                → 403
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import registry.api.routers.admin_memory_curation as admin_memory_curation_module
from registry.api.routers.admin_memory_curation import mutation_router, router
from registry.exceptions import NotFoundError, ValidationError
from registry.service.memory.calibration import UNCALIBRATED, MappingStatus
from registry.service.memory.promotion_eligibility import PromotionPolicy
from registry.service.memory.source_governance import SourcePolicy
from registry.workers.calibration_refit import RefitOutcome
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()
_SOURCE_ID = uuid.uuid4()
_PROVIDER = "anthropic"
_MODEL = "claude-haiku-4-5-20251001"
_STRATEGY = "observation"


def _policy(**overrides: object) -> PromotionPolicy:
    defaults: dict[str, object] = dict(
        confidence_floor=0.0,
        blast_radius_threshold=5,
        always_review=frozenset(),
    )
    defaults.update(overrides)
    return PromotionPolicy(**defaults)  # type: ignore[arg-type]


def _mapping_status(**overrides: object) -> MappingStatus:
    defaults: dict[str, object] = dict(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        version=f"{_PROVIDER}:{_MODEL}:{_STRATEGY}:2026-01-01:200",
        status="active",
        n_adjudicated=200,
        measured_error=0.05,
        fitted_at=_NOW,
    )
    defaults.update(overrides)
    return MappingStatus(**defaults)  # type: ignore[arg-type]


def _refit_outcome(**overrides: object) -> RefitOutcome:
    defaults: dict[str, object] = dict(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        version=f"{_PROVIDER}:{_MODEL}:{_STRATEGY}:2026-01-01:200",
        activated=True,
        n_adjudicated=200,
    )
    defaults.update(overrides)
    return RefitOutcome(**defaults)  # type: ignore[arg-type]


def _source_policy(**overrides: object) -> SourcePolicy:
    defaults: dict[str, object] = dict(
        source_id=_SOURCE_ID,
        tenant_id=_TENANT,
        authority_tier="derived",
        ingest_ceiling=1000,
        window_seconds=3600,
        breaker_open_until=None,
        breach_count=0,
    )
    defaults.update(overrides)
    return SourcePolicy(**defaults)  # type: ignore[arg-type]


def _build_app(
    *,
    monkeypatch: pytest.MonkeyPatch,
    load_policy_return: PromotionPolicy | None = None,
    set_policy_return: PromotionPolicy | None = None,
    set_policy_effect: Exception | None = None,
    allowlist_return: frozenset[str] = frozenset(),
    policies_for_tenant_return: tuple[SourcePolicy, ...] = (),
    declare_return: SourcePolicy | None = None,
    declare_effect: Exception | None = None,
    policy_for_return: SourcePolicy | None = None,
    policy_for_side_effect: list[SourcePolicy | None] | None = None,
    reset_breaker_effect: Exception | None = None,
    active_mappings_return: tuple[MappingStatus, ...] = (),
    refit_one_return: RefitOutcome | None = None,
    refit_one_effect: Exception | None = None,
    ctx: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(mutation_router)

    async def _fake_load_policy(session: object, tenant_id: uuid.UUID) -> PromotionPolicy:
        return load_policy_return or _policy()

    if set_policy_effect is not None:

        async def _fake_set_policy(*args: object, **kwargs: object) -> PromotionPolicy:
            raise set_policy_effect

    else:

        async def _fake_set_policy(*args: object, **kwargs: object) -> PromotionPolicy:
            return set_policy_return or _policy()

    monkeypatch.setattr(admin_memory_curation_module, "load_policy", _fake_load_policy)
    monkeypatch.setattr(admin_memory_curation_module, "set_policy", _fake_set_policy)

    if refit_one_effect is not None:
        fake_refit_one = AsyncMock(side_effect=refit_one_effect)
    else:
        fake_refit_one = AsyncMock(return_value=refit_one_return or _refit_outcome())
    monkeypatch.setattr(admin_memory_curation_module, "refit_one", fake_refit_one)
    # Exposed for tests to assert on call args -- `refit_one` is a bare
    # function reached off the router module, not a service method off the
    # typed container, so there is nowhere else to hang the mock a caller can
    # introspect after the request completes.
    app.state.refit_one_mock = fake_refit_one

    promotion_guardrails = MagicMock()
    promotion_guardrails.allowlist_for = AsyncMock(return_value=allowlist_return)
    promotion_guardrails.allow = AsyncMock(return_value=None)
    promotion_guardrails.revoke = AsyncMock(return_value=None)

    source_governance = MagicMock()
    source_governance.policies_for_tenant = AsyncMock(return_value=policies_for_tenant_return)
    if declare_effect is not None:
        source_governance.declare = AsyncMock(side_effect=declare_effect)
    else:
        source_governance.declare = AsyncMock(return_value=declare_return or _source_policy())
    if policy_for_side_effect is not None:
        source_governance.policy_for = AsyncMock(side_effect=policy_for_side_effect)
    else:
        source_governance.policy_for = AsyncMock(return_value=policy_for_return)
    if reset_breaker_effect is not None:
        source_governance.reset_breaker = AsyncMock(side_effect=reset_breaker_effect)
    else:
        source_governance.reset_breaker = AsyncMock(return_value=None)

    calibration = MagicMock()
    calibration.active_mappings = AsyncMock(return_value=active_mappings_return)

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.begin = MagicMock(return_value=begin_cm)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session)

    clock = MagicMock()
    clock.now = MagicMock(return_value=_NOW)

    app.state.services = MagicMock(
        promotion_guardrails=promotion_guardrails,
        source_governance=source_governance,
        calibration=calibration,
        session_factory=session_factory,
        clock=clock,
    )

    from registry.api.middleware.tenant import get_tenant_context

    effective_ctx = ctx if ctx is not None else tenant_context(tenant_id=_TENANT, actor_id=_ACTOR, roles=["admin"])

    async def _fake_ctx() -> object:
        return effective_ctx

    # Override the identity dependency `_admin_required` itself wraps, not
    # `_admin_required`, so its own role check actually runs against the
    # fake context -- overriding `_admin_required` directly would bypass the
    # very check the non-admin tests exist to exercise.
    app.dependency_overrides[get_tenant_context] = _fake_ctx
    return app


def _non_admin_ctx() -> object:
    return tenant_context(tenant_id=_TENANT, actor_id=_ACTOR, roles=["producer"])


# ---------------------------------------------------------------------------
# promotion policy
# ---------------------------------------------------------------------------


class TestPromotionPolicy:
    def test_get_returns_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, load_policy_return=_policy(confidence_floor=0.4))
        resp = TestClient(app).get("/v1/admin/memory-promotion-policy")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["confidence_floor"] == 0.4
        assert body["blast_radius_threshold"] == 5
        assert body["always_review"] == []

    def test_get_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).get("/v1/admin/memory-promotion-policy")
        assert resp.status_code == 403

    def test_put_returns_updated_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(
            monkeypatch=monkeypatch,
            set_policy_return=_policy(confidence_floor=0.6, always_review=frozenset({"lifecycle_state"})),
        )
        resp = TestClient(app).put(
            "/v1/admin/memory-promotion-policy",
            json={"confidence_floor": 0.6, "blast_radius_threshold": 5, "always_review": ["lifecycle_state"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["confidence_floor"] == 0.6
        assert resp.json()["always_review"] == ["lifecycle_state"]

    def test_put_via_post_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The POST `:replace` tunnel alias reaches the same handler."""
        app = _build_app(monkeypatch=monkeypatch, set_policy_return=_policy(confidence_floor=0.2))
        resp = TestClient(app).post(
            "/v1/admin/memory-promotion-policy:replace",
            json={"confidence_floor": 0.2, "blast_radius_threshold": 5, "always_review": []},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["confidence_floor"] == 0.2

    def test_put_out_of_range_confidence_floor_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch)
        resp = TestClient(app).put(
            "/v1/admin/memory-promotion-policy",
            json={"confidence_floor": 1.5, "blast_radius_threshold": 5, "always_review": []},
        )
        assert resp.status_code == 422

    def test_put_negative_threshold_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch)
        resp = TestClient(app).put(
            "/v1/admin/memory-promotion-policy",
            json={"confidence_floor": 0.0, "blast_radius_threshold": -1, "always_review": []},
        )
        assert resp.status_code == 422

    def test_put_service_validation_error_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(
            monkeypatch=monkeypatch,
            set_policy_effect=ValidationError("confidence_floor must be between 0 and 1"),
        )
        resp = TestClient(app).put(
            "/v1/admin/memory-promotion-policy",
            json={"confidence_floor": 0.5, "blast_radius_threshold": 5, "always_review": []},
        )
        assert resp.status_code == 422

    def test_put_service_permission_error_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(
            monkeypatch=monkeypatch,
            set_policy_effect=PermissionError("configuring the promotion policy requires the admin role"),
        )
        resp = TestClient(app).put(
            "/v1/admin/memory-promotion-policy",
            json={"confidence_floor": 0.5, "blast_radius_threshold": 5, "always_review": []},
        )
        assert resp.status_code == 403

    def test_put_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).put(
            "/v1/admin/memory-promotion-policy",
            json={"confidence_floor": 0.5, "blast_radius_threshold": 5, "always_review": []},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# autopromote allowlist
# ---------------------------------------------------------------------------


class TestAutopromoteAllowlist:
    def test_get_returns_sorted_predicates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, allowlist_return=frozenset({"z_pred", "a_pred"}))
        resp = TestClient(app).get("/v1/admin/memory-autopromote-allowlist")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"predicates": ["a_pred", "z_pred"]}

    def test_get_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).get("/v1/admin/memory-autopromote-allowlist")
        assert resp.status_code == 403

    def test_allow_calls_service_and_returns_updated_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, allowlist_return=frozenset({"owned_by_team"}))
        resp = TestClient(app).post("/v1/admin/memory-autopromote-allowlist:allow", json={"predicate": "owned_by_team"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"predicates": ["owned_by_team"]}
        services = app.state.services
        call = services.promotion_guardrails.allow.await_args
        assert call is not None
        assert call.args == (_TENANT, "owned_by_team")
        assert call.kwargs["actor_id"] == _ACTOR

    def test_allow_empty_predicate_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch)
        resp = TestClient(app).post("/v1/admin/memory-autopromote-allowlist:allow", json={"predicate": ""})
        assert resp.status_code == 422
        app.state.services.promotion_guardrails.allow.assert_not_awaited()

    def test_allow_extra_field_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch)
        resp = TestClient(app).post(
            "/v1/admin/memory-autopromote-allowlist:allow",
            json={"predicate": "owned_by_team", "extra": "nope"},
        )
        assert resp.status_code == 422

    def test_allow_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).post("/v1/admin/memory-autopromote-allowlist:allow", json={"predicate": "owned_by_team"})
        assert resp.status_code == 403
        app.state.services.promotion_guardrails.allow.assert_not_awaited()

    def test_revoke_calls_service_and_returns_updated_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, allowlist_return=frozenset())
        resp = TestClient(app).post(
            "/v1/admin/memory-autopromote-allowlist:revoke", json={"predicate": "owned_by_team"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"predicates": []}
        call = app.state.services.promotion_guardrails.revoke.await_args
        assert call is not None
        assert call.args == (_TENANT, "owned_by_team")

    def test_revoke_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).post(
            "/v1/admin/memory-autopromote-allowlist:revoke", json={"predicate": "owned_by_team"}
        )
        assert resp.status_code == 403
        app.state.services.promotion_guardrails.revoke.assert_not_awaited()


# ---------------------------------------------------------------------------
# source governance
# ---------------------------------------------------------------------------


class TestMemorySources:
    def test_list_returns_declared_sources(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, policies_for_tenant_return=(_source_policy(),))
        resp = TestClient(app).get("/v1/admin/memory-sources")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["source_id"] == str(_SOURCE_ID)
        assert body[0]["authority_tier"] == "derived"

    def test_list_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).get("/v1/admin/memory-sources")
        assert resp.status_code == 403

    def test_declare_returns_201(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, declare_return=_source_policy(authority_tier="owner_human"))
        resp = TestClient(app).post(
            "/v1/admin/memory-sources",
            json={"source_id": str(_SOURCE_ID), "authority_tier": "owner_human"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["authority_tier"] == "owner_human"
        call = app.state.services.source_governance.declare.await_args
        assert call.kwargs["source_id"] == _SOURCE_ID
        assert call.kwargs["authority_tier"] == "owner_human"
        assert call.kwargs["ingest_ceiling"] == 1000
        assert call.kwargs["window_seconds"] == 3600

    def test_declare_not_found_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, declare_effect=NotFoundError("no such source"))
        resp = TestClient(app).post(
            "/v1/admin/memory-sources",
            json={"source_id": str(_SOURCE_ID), "authority_tier": "owner_human"},
        )
        assert resp.status_code == 404

    def test_declare_bad_tier_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, declare_effect=ValidationError("authority_tier must be one of ..."))
        resp = TestClient(app).post(
            "/v1/admin/memory-sources",
            json={"source_id": str(_SOURCE_ID), "authority_tier": "bogus"},
        )
        assert resp.status_code == 422

    def test_declare_wrong_tenant_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(
            monkeypatch=monkeypatch, declare_effect=PermissionError("only the owning tenant may govern a source")
        )
        resp = TestClient(app).post(
            "/v1/admin/memory-sources",
            json={"source_id": str(_SOURCE_ID), "authority_tier": "owner_human"},
        )
        assert resp.status_code == 403

    def test_declare_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).post(
            "/v1/admin/memory-sources",
            json={"source_id": str(_SOURCE_ID), "authority_tier": "owner_human"},
        )
        assert resp.status_code == 403
        app.state.services.source_governance.declare.assert_not_awaited()

    def test_patch_merges_omitted_fields_from_current(self, monkeypatch: pytest.MonkeyPatch) -> None:
        current = _source_policy(authority_tier="derived", ingest_ceiling=1000, window_seconds=3600)
        app = _build_app(
            monkeypatch=monkeypatch,
            policy_for_return=current,
            declare_return=_source_policy(ingest_ceiling=5000),
        )
        resp = TestClient(app).patch(
            f"/v1/admin/memory-sources/{_SOURCE_ID}",
            json={"ingest_ceiling": 5000},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ingest_ceiling"] == 5000
        call = app.state.services.source_governance.declare.await_args
        assert call.kwargs["source_id"] == _SOURCE_ID
        assert call.kwargs["authority_tier"] == "derived"
        assert call.kwargs["ingest_ceiling"] == 5000
        assert call.kwargs["window_seconds"] == 3600

    def test_patch_via_post_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, policy_for_return=_source_policy())
        resp = TestClient(app).post(
            f"/v1/admin/memory-sources/{_SOURCE_ID}:update",
            json={"window_seconds": 7200},
        )
        assert resp.status_code == 200, resp.text

    def test_patch_missing_source_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, policy_for_return=None)
        resp = TestClient(app).patch(f"/v1/admin/memory-sources/{_SOURCE_ID}", json={"ingest_ceiling": 10})
        assert resp.status_code == 404
        app.state.services.source_governance.declare.assert_not_awaited()

    def test_patch_foreign_tenant_source_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Identical to the missing-source case -- the router never learns a
        different tenant governs this id."""
        foreign = _source_policy(tenant_id=uuid.uuid4())
        app = _build_app(monkeypatch=monkeypatch, policy_for_return=foreign)
        resp = TestClient(app).patch(f"/v1/admin/memory-sources/{_SOURCE_ID}", json={"ingest_ceiling": 10})
        assert resp.status_code == 404
        app.state.services.source_governance.declare.assert_not_awaited()

    def test_patch_service_validation_error_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(
            monkeypatch=monkeypatch,
            policy_for_return=_source_policy(),
            declare_effect=ValidationError("ingest_ceiling and window_seconds must be positive"),
        )
        resp = TestClient(app).patch(f"/v1/admin/memory-sources/{_SOURCE_ID}", json={"ingest_ceiling": 1})
        assert resp.status_code == 422

    def test_patch_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).patch(f"/v1/admin/memory-sources/{_SOURCE_ID}", json={"ingest_ceiling": 10})
        assert resp.status_code == 403

    def test_reset_breaker_returns_reloaded_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(
            monkeypatch=monkeypatch,
            policy_for_return=_source_policy(breaker_open_until=None, breach_count=1),
        )
        resp = TestClient(app).post(f"/v1/admin/memory-sources/{_SOURCE_ID}:reset-breaker")
        assert resp.status_code == 200, resp.text
        assert resp.json()["breaker_open_until"] is None
        app.state.services.source_governance.reset_breaker.assert_awaited_once()
        app.state.services.source_governance.policy_for.assert_awaited_once()

    def test_reset_breaker_permission_error_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, reset_breaker_effect=PermissionError("no such source in this tenant"))
        resp = TestClient(app).post(f"/v1/admin/memory-sources/{_SOURCE_ID}:reset-breaker")
        assert resp.status_code == 403
        app.state.services.source_governance.policy_for.assert_not_awaited()

    def test_reset_breaker_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).post(f"/v1/admin/memory-sources/{_SOURCE_ID}:reset-breaker")
        assert resp.status_code == 403
        app.state.services.source_governance.reset_breaker.assert_not_awaited()


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


class TestMemoryCalibration:
    def test_get_returns_one_row_per_triple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(
            monkeypatch=monkeypatch,
            active_mappings_return=(_mapping_status(), _mapping_status(strategy_id="preference", status="failed")),
        )
        resp = TestClient(app).get("/v1/admin/memory-calibration")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 2
        assert body[0]["strategy_id"] == _STRATEGY
        assert body[0]["status"] == "active"
        assert body[1]["strategy_id"] == "preference"
        assert body[1]["status"] == "failed"

    def test_get_empty_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing fitted yet is the deployment's own honest starting state,
        not a fault -- an empty list, not a 404."""
        app = _build_app(monkeypatch=monkeypatch, active_mappings_return=())
        resp = TestClient(app).get("/v1/admin/memory-calibration")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_get_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).get("/v1/admin/memory-calibration")
        assert resp.status_code == 403

    def test_refit_returns_the_outcome_and_calls_the_shared_sequence_with_the_named_triple(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _build_app(
            monkeypatch=monkeypatch,
            refit_one_return=_refit_outcome(strategy_id="preference", version="v-9", activated=True, n_adjudicated=250),
        )
        resp = TestClient(app).post(
            "/v1/admin/memory-calibration:refit",
            json={"provider_id": _PROVIDER, "model_id": _MODEL, "strategy_id": "preference"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["strategy_id"] == "preference"
        assert body["version"] == "v-9"
        assert body["activated"] is True
        assert body["n_adjudicated"] == 250

        call = app.state.refit_one_mock.await_args
        assert call is not None
        # `services.calibration` is the first positional argument -- the same
        # instance the GET route reads from, not a second construction.
        assert call.args[0] is app.state.services.calibration
        assert call.kwargs["provider_id"] == _PROVIDER
        assert call.kwargs["model_id"] == _MODEL
        assert call.kwargs["strategy_id"] == "preference"
        assert call.kwargs["fitted_by"] == _ACTOR

    def test_refit_below_the_evaluation_floor_returns_200_uncalibrated_not_activated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate refuses quietly: a triple with too little evidence is not a
        malformed request, so the response is 200 with `activated: false` and
        the `uncalibrated` sentinel, not an error status."""
        app = _build_app(
            monkeypatch=monkeypatch,
            refit_one_return=_refit_outcome(version=UNCALIBRATED, activated=False, n_adjudicated=5),
        )
        resp = TestClient(app).post(
            "/v1/admin/memory-calibration:refit",
            json={"provider_id": _PROVIDER, "model_id": _MODEL, "strategy_id": _STRATEGY},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == UNCALIBRATED
        assert body["activated"] is False

    def test_refit_missing_field_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch)
        resp = TestClient(app).post(
            "/v1/admin/memory-calibration:refit",
            json={"provider_id": _PROVIDER, "model_id": _MODEL},
        )
        assert resp.status_code == 422
        app.state.refit_one_mock.assert_not_awaited()

    def test_refit_empty_strategy_id_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch)
        resp = TestClient(app).post(
            "/v1/admin/memory-calibration:refit",
            json={"provider_id": _PROVIDER, "model_id": _MODEL, "strategy_id": ""},
        )
        assert resp.status_code == 422
        app.state.refit_one_mock.assert_not_awaited()

    def test_refit_extra_field_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch)
        resp = TestClient(app).post(
            "/v1/admin/memory-calibration:refit",
            json={"provider_id": _PROVIDER, "model_id": _MODEL, "strategy_id": _STRATEGY, "extra": "nope"},
        )
        assert resp.status_code == 422

    def test_refit_non_admin_returns_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch=monkeypatch, ctx=_non_admin_ctx())
        resp = TestClient(app).post(
            "/v1/admin/memory-calibration:refit",
            json={"provider_id": _PROVIDER, "model_id": _MODEL, "strategy_id": _STRATEGY},
        )
        assert resp.status_code == 403
        app.state.refit_one_mock.assert_not_awaited()
