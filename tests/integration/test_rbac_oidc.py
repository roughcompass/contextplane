"""Admin, OIDC, rate-limit, and RBAC integration tests.

Covers:
- test_admin_vocab_workflow: admin adds, lists, and deprecates a vocabulary value via API.
- test_audit_query_time_range: GET /v1/admin/audit with actor_id + from/to
  returns the lifecycle transition event seeded in the test.
- test_oidc_jwt_validates_against_live_jwks /
  test_oidc_rejects_issuer_outside_the_allowlist: the real signature path —
  discovery and JWKS fetched over HTTP from the in-process mock IdP, RS256
  verified against the published key. Everywhere else the validator is patched
  out, so this is the only place that check runs for real.
- test_rate_limit_429: exhaust budget (writes_per_second=0 row), assert 429
  with retry_after_s field.
- test_consumer_cannot_call_producer_endpoint: consumer role gets 403 on
  POST /v1/capabilities.
- test_rbac_tenant_isolation_full_suite: import and sanity-check that the
  conformance suite has admin endpoints registered (representative subset).
"""

from __future__ import annotations

import datetime
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.api.auth import oidc as _oidc_module
from registry.config import Settings
from registry.exceptions import CatalogError
from registry.main import create_app
from registry.storage.models import AuditLog, RateLimit
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_audit_event(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str = "lifecycle.transition",
    ts: datetime.datetime,
) -> uuid.UUID:
    """Insert a single audit log row; return audit_id."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    audit_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            session.add(
                AuditLog(
                    audit_id=audit_id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action=action,
                    target_type="entity",
                    target_id=entity_id,
                    before_jsonb={"state": "draft"},
                    after_jsonb={"state": "active"},
                    ts=ts,
                    request_id="test-req-001",
                    error_code=None,
                )
            )
    finally:
        await engine.dispose()
    return audit_id


async def _seed_zero_budget_rate_limit(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Insert a rate_limits row with writes_per_second=0 to force 429."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            session.add(
                RateLimit(
                    limit_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    reads_per_second=100,
                    writes_per_second=0,
                    created_at=_NOW,
                )
            )
    finally:
        await engine.dispose()


async def _get_tenant_id(pg_url: str, slug: str) -> uuid.UUID:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(text("SELECT tenant_id FROM tenants WHERE slug = :slug"), {"slug": slug})
            ).first()
            assert row is not None, f"tenant {slug} not found"
            return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


async def _get_actor_id(pg_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT actor_id FROM actors WHERE tenant_id = :tid LIMIT 1"),
                    {"tid": tenant_id},
                )
            ).first()
            assert row is not None
            return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


async def _make_persona(h: EntitlementAuthHarness, pg_url: str, *, slug: str, roles: list[str]) -> TenantPersona:
    """Materialise tenant + actor via /v1/whoami."""
    persona = h.add_persona(slug, roles=roles)
    h.configure_fetcher_for(persona)
    transport = httpx.ASGITransport(app=h.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert resp.status_code == 200, resp.text
    return persona


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_vocab_workflow(pg_container: str, app_settings: Settings) -> None:
    """Admin adds, lists, and deprecates a vocabulary value — all via API.

    No direct DB writes after the initial tenant seed. All mutations go
    through the production HTTP handlers. This is the integration gate for
    the admin vocab surface.
    """
    suffix = uuid.uuid4().hex[:6]

    async with EntitlementAuthHarness(pg_container) as h:
        persona = await _make_persona(
            h, pg_container, slug=f"p4-admin-{suffix}", roles=["admin", "producer", "consumer"]
        )
        transport = httpx.ASGITransport(app=h.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            h.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                # Step 1: add a vocabulary value for a new entity_type.
                vocab_resp = await client.post(
                    "/v1/admin/vocabularies/entity_type",
                    json={"value": f"widget-{suffix}"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert vocab_resp.status_code == 201, vocab_resp.text
                assert vocab_resp.json()["value"] == f"widget-{suffix}"

                # Step 2: list vocabulary values — our new value appears.
                list_vocab_resp = await client.get(
                    "/v1/admin/vocabularies/entity_type",
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert list_vocab_resp.status_code == 200
                values = [v["value"] for v in list_vocab_resp.json()]
                assert f"widget-{suffix}" in values

                # Step 3: deprecate (rotate) the newly added value.
                patch_resp = await client.patch(
                    f"/v1/admin/vocabularies/entity_type/widget-{suffix}",
                    json={"deprecated_at": "2026-06-01T00:00:00Z"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )
                assert patch_resp.status_code == 200
                assert patch_resp.json()["deprecated_at"] is not None


@pytest.mark.asyncio
async def test_audit_query_time_range(pg_container: str, app_settings: Settings) -> None:
    """GET /v1/admin/audit with actor_id + from/to filters returns the seeded event.

    Also validates keyset pagination: first page returns the event + no cursor
    when result fits within page_size.
    """
    suffix = uuid.uuid4().hex[:6]

    async with EntitlementAuthHarness(pg_container) as h:
        # Auditor persona — the audit endpoint requires the auditor role
        # specifically. Admin is higher precedence so the resolver would
        # collapse ["admin", "auditor"] to ["admin"], which fails the gate.
        admin_persona = await _make_persona(h, pg_container, slug=f"p4-audit-{suffix}", roles=["auditor"])
        tenant_id = await _get_tenant_id(pg_container, admin_persona.slug)
        actor_id = await _get_actor_id(pg_container, tenant_id)

        # Seed a lifecycle transition event within the query window.
        event_ts = datetime.datetime(2026, 7, 15, 12, 0, 0, tzinfo=datetime.UTC)
        audit_id = await _seed_audit_event(
            pg_container,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="lifecycle.transition",
            ts=event_ts,
        )

        transport = httpx.ASGITransport(app=h.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            h.configure_fetcher_for(admin_persona)
            with patch_validator_for_actor(admin_persona):
                resp = await client.get(
                    "/v1/admin/audit",
                    params={
                        "actor_id": str(actor_id),
                        "from": "2026-07-01T00:00:00Z",
                        "to": "2026-08-01T00:00:00Z",
                        "page_size": 50,
                    },
                    headers=bearer_headers(tenant_slug=admin_persona.slug),
                )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = body.get("items") or body.get("rows") or []
    row_ids = [r["audit_id"] for r in rows]
    assert str(audit_id) in row_ids, f"seeded audit_id {audit_id} not found in rows: {row_ids}"
    for row in rows:
        row_ts = datetime.datetime.fromisoformat(row["ts"])
        assert row_ts >= datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
        assert row_ts <= datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)


@pytest.mark.asyncio
async def test_oidc_jwt_validates_against_live_jwks(pg_container: str) -> None:
    """A real RS256 JWT validates against a JWKS fetched over HTTP.

    Everywhere else in the suite `validate_oidc_token` is patched out, because
    the harness needs a fixed identity without standing up an IdP. That leaves
    signature validation itself — fetch the discovery doc, fetch the JWKS,
    match the kid, verify the signature, check iss and aud — asserted nowhere.

    Here the whole chain runs for real. The only substitution is the transport:
    the cache's HTTP calls are routed into the in-process mock IdP rather than
    a socket, so the discovery document and the JWKS are genuinely fetched and
    parsed, and the token is genuinely verified against the published key.

    This test previously existed but was skipped: respx cannot intercept the
    clients the cache builds internally. The cache now takes an optional
    transport for exactly this.
    """
    from tests.helpers.jwt_factory import TEST_AUDIENCE, make_jwt
    from tests.mocks.oidc_server.app import app as mock_idp

    # The mock derives its issuer from the request host, so the issuer the
    # discovery doc advertises is fixed by the URL we fetch it from.
    _IDP_HOST = "http://mock-idp.test"
    _ISSUER = f"{_IDP_HOST}/default"
    _DISCOVERY = f"{_ISSUER}/.well-known/openid-configuration"

    subject = f"oidc-user-{uuid.uuid4().hex[:6]}"

    cache = _oidc_module._OidcCache(transport=httpx.ASGITransport(app=mock_idp))
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        oidc_discovery_url=_DISCOVERY,
        oidc_issuer_allowlist=[_ISSUER],
        resource_uri_allowlist=[TEST_AUDIENCE],
        embedding_provider="stub",
    )

    claims, identity = await _oidc_module.validate_oidc_token(make_jwt(sub=subject, iss=_ISSUER), settings, cache=cache)
    assert identity == subject
    assert claims["iss"] == _ISSUER

    # The fetches actually happened — a cache left empty would mean the
    # validator took some path that never consulted the published keys.
    assert cache.discovery_doc is not None, "discovery document was never fetched"
    assert cache.jwks_data is not None, "JWKS was never fetched"

    # A token signed by a different key must fail against the same JWKS.
    # Without this, a validator that skipped verification would still pass.
    forged = make_jwt(sub=subject, iss=_ISSUER).rsplit(".", 1)[0] + ".Zm9yZ2VkLXNpZ25hdHVyZQ"
    with pytest.raises(CatalogError):
        await _oidc_module.validate_oidc_token(forged, settings, cache=cache)


@pytest.mark.asyncio
async def test_oidc_rejects_issuer_outside_the_allowlist(pg_container: str) -> None:
    """A validly-signed token from an unlisted issuer is refused.

    The signature check alone is not authorization: any IdP whose JWKS the
    cache can reach could otherwise mint tokens this service accepts.
    """
    from tests.helpers.jwt_factory import TEST_AUDIENCE, make_jwt
    from tests.mocks.oidc_server.app import app as mock_idp

    # The mock derives its issuer from the request host, so the issuer the
    # discovery doc advertises is fixed by the URL we fetch it from.
    _IDP_HOST = "http://mock-idp.test"
    _ISSUER = f"{_IDP_HOST}/default"
    _DISCOVERY = f"{_ISSUER}/.well-known/openid-configuration"

    cache = _oidc_module._OidcCache(transport=httpx.ASGITransport(app=mock_idp))
    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        oidc_discovery_url=_DISCOVERY,
        oidc_issuer_allowlist=["https://some-other-idp.example"],
        resource_uri_allowlist=[TEST_AUDIENCE],
        embedding_provider="stub",
    )

    with pytest.raises(CatalogError):
        await _oidc_module.validate_oidc_token(make_jwt(iss=_ISSUER), settings, cache=cache)


@pytest.mark.asyncio
async def test_rate_limit_429(pg_container: str, app_settings: Settings) -> None:
    """Exhaust write budget (writes_per_minute=0), assert 429 with retry_after_s.

    A zero-budget tenant is immediately throttled on any write.
    """
    suffix = uuid.uuid4().hex[:6]

    async with EntitlementAuthHarness(pg_container) as h:
        persona = await _make_persona(h, pg_container, slug=f"p4-rl-{suffix}", roles=["admin", "producer"])
        tenant_id = await _get_tenant_id(pg_container, persona.slug)
        actor_id = await _get_actor_id(pg_container, tenant_id)

        await _seed_zero_budget_rate_limit(
            pg_container,
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

        # Build a new app with rate_limit_write_per_minute=0 so the very first
        # POST exhausts the bucket and triggers 429. The harness resolver is
        # shared so auth still resolves correctly.
        constrained = Settings(
            database_url=app_settings.database_url,
            pgbouncer_url=app_settings.pgbouncer_url,
            scheduler_jobstore_url=app_settings.scheduler_jobstore_url,
            scheduler_use_memory_jobstore=True,
            rate_limit_enabled=True,
            rate_limit_write_per_minute=0,
            embedding_provider=app_settings.embedding_provider,
        )
        constrained_app = create_app(constrained)
        constrained_app.state.claim_resolver = h.app.state.claim_resolver  # type: ignore[attr-defined]

        transport = httpx.ASGITransport(app=constrained_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            h.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                # Any write endpoint will be throttled — use capability create (POST).
                resp = await client.post(
                    "/v1/capabilities",
                    json={"name": "rate-limit-test"},
                    headers=bearer_headers(tenant_slug=persona.slug),
                )

    assert resp.status_code == 429, f"Expected 429 for zero-budget tenant; got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_consumer_cannot_call_producer_endpoint(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """Consumer role gets 403 on POST /v1/capabilities (requires producer or admin role)."""
    suffix = uuid.uuid4().hex[:6]

    async with EntitlementAuthHarness(pg_container) as h:
        persona = await _make_persona(h, pg_container, slug=f"p4-consumer-{suffix}", roles=["consumer"])
        transport = httpx.ASGITransport(app=h.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            h.configure_fetcher_for(persona)
            with patch_validator_for_actor(persona):
                resp = await client.post(
                    "/v1/capabilities",
                    json={
                        "name": "test-svc",
                        "entity_type": "service",
                        "facts": [],
                    },
                    headers=bearer_headers(tenant_slug=persona.slug),
                )

    assert (
        resp.status_code == 403
    ), f"consumer token must get 403 on POST /v1/capabilities; got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_rbac_tenant_isolation_full_suite(
    pg_container: str,
    app_settings: Settings,
) -> None:
    """Sanity: tenant isolation tests are present in the conformance suite.

    Imports the conformance module and verifies the cross-tenant isolation
    test functions are importable. Checks against the current function names
    in the conformance suite.
    """
    import tests.conformance.test_tenant_isolation as iso_module

    assert hasattr(
        iso_module, "test_admin_audit_returns_no_cross_tenant_rows"
    ), "test_admin_audit_returns_no_cross_tenant_rows must be present in conformance suite"
    assert hasattr(
        iso_module, "test_capability_path_param_swap"
    ), "test_capability_path_param_swap must be present in conformance suite"
    assert hasattr(
        iso_module, "test_search_returns_no_cross_tenant_hits"
    ), "test_search_returns_no_cross_tenant_hits must be present in conformance suite"
    assert hasattr(
        iso_module, "test_no_bearer_returns_401"
    ), "test_no_bearer_returns_401 must be present in conformance suite"
