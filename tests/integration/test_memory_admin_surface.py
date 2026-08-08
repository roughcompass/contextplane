"""Integration tests for the memory-curation admin surface: promotion
policy, the autopromote allowlist, and source governance -- against a real
Postgres, driven over HTTP through the entitlement-auth harness.

Covers:
- GET/PUT /v1/admin/memory-promotion-policy: unconfigured defaults, a
  round trip through PUT, a validation refusal (422), non-admin role (403).
- GET /v1/admin/memory-autopromote-allowlist + POST .../:allow / :revoke:
  full lifecycle (empty -> allowed -> revoked), non-admin role (403).
- GET/POST /v1/admin/memory-sources + PATCH .../{id} + POST
  .../{id}:reset-breaker: declare -> list -> partial-update -> reset-breaker
  lifecycle, declaring an unknown source (404), a foreign tenant's source
  (403), non-admin role (403).
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

from contextplane.service.memory.source_governance import SourceGovernanceService
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

type _AdminClients = dict[str, Any]


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_sync_source(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    created_by: uuid.UUID,
) -> None:
    """Insert a bare `sync_sources` row directly -- `SourceGovernanceService.declare`
    checks this table for ownership, but nothing about admin-surface governance
    exercises the connector/vocab machinery `admin_sync.py`'s own create route
    validates, so a raw insert is the faithful minimum fixture."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO sync_sources "
                    "  (source_id, tenant_id, source_type, display_name, config, "
                    "   is_active, created_at, created_by) "
                    "VALUES (:sid, :tid, 'manual', 'admin-surface-test-source', '{}'::jsonb, "
                    "        TRUE, :now, :actor)"
                ),
                {"sid": source_id, "tid": tenant_id, "now": _NOW, "actor": created_by},
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_clients(pg_container: str) -> AsyncIterator[_AdminClients]:
    """One tenant, two personas (`admin` and a non-admin `producer`), one
    shared ASGI client authenticated as whichever persona a test switches to
    via `patch_validator_for_actor`."""
    slug = f"memadmin-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        admin_persona = harness.add_persona(slug, roles=["admin"])
        producer_persona = harness.add_persona(slug, roles=["producer"], actor_id=uuid.uuid4())

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(admin_persona)
            with patch_validator_for_actor(admin_persona):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert resp.status_code == 200, resp.text
                tenant_id = uuid.UUID(resp.json()["tenant_id"])
                actor_id = uuid.UUID(resp.json()["actor_id"])

            yield {
                "client": client,
                "harness": harness,
                "admin": admin_persona,
                "producer": producer_persona,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "pg_url": pg_container,
                "slug": slug,
            }


def _as_admin(clients: _AdminClients) -> Any:
    clients["harness"].configure_fetcher_for(clients["admin"])
    return patch_validator_for_actor(clients["admin"])


def _as_producer(clients: _AdminClients) -> Any:
    clients["harness"].configure_fetcher_for(clients["producer"])
    return patch_validator_for_actor(clients["producer"])


# ---------------------------------------------------------------------------
# Promotion policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promotion_policy_get_returns_cautious_defaults(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]

    with _as_admin(admin_clients):
        resp = await client.get("/v1/admin/memory-promotion-policy", headers=bearer_headers(tenant_slug=slug))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"confidence_floor": 0.0, "blast_radius_threshold": 5, "always_review": []}


@pytest.mark.asyncio
async def test_promotion_policy_put_round_trips(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]

    with _as_admin(admin_clients):
        put_resp = await client.put(
            "/v1/admin/memory-promotion-policy",
            json={
                "confidence_floor": 0.65,
                "blast_radius_threshold": 12,
                "always_review": ["lifecycle_state", "deprecated_after"],
            },
            headers=bearer_headers(tenant_slug=slug),
        )
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json() == {
            "confidence_floor": 0.65,
            "blast_radius_threshold": 12,
            "always_review": ["deprecated_after", "lifecycle_state"],
        }

        get_resp = await client.get("/v1/admin/memory-promotion-policy", headers=bearer_headers(tenant_slug=slug))
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json() == put_resp.json()


@pytest.mark.asyncio
async def test_promotion_policy_put_out_of_range_confidence_floor_returns_422(
    admin_clients: _AdminClients,
) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]

    with _as_admin(admin_clients):
        resp = await client.put(
            "/v1/admin/memory-promotion-policy",
            json={"confidence_floor": 1.5, "blast_radius_threshold": 5, "always_review": []},
            headers=bearer_headers(tenant_slug=slug),
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_promotion_policy_non_admin_returns_403(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]

    with _as_producer(admin_clients):
        get_resp = await client.get("/v1/admin/memory-promotion-policy", headers=bearer_headers(tenant_slug=slug))
        put_resp = await client.put(
            "/v1/admin/memory-promotion-policy",
            json={"confidence_floor": 0.1, "blast_radius_threshold": 5, "always_review": []},
            headers=bearer_headers(tenant_slug=slug),
        )
    assert get_resp.status_code == 403
    assert put_resp.status_code == 403


# ---------------------------------------------------------------------------
# Autopromote allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autopromote_allowlist_allow_revoke_lifecycle(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]

    with _as_admin(admin_clients):
        empty = await client.get("/v1/admin/memory-autopromote-allowlist", headers=bearer_headers(tenant_slug=slug))
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"predicates": []}

        allowed = await client.post(
            "/v1/admin/memory-autopromote-allowlist:allow",
            json={"predicate": "owned_by_team"},
            headers=bearer_headers(tenant_slug=slug),
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json() == {"predicates": ["owned_by_team"]}

        listed = await client.get("/v1/admin/memory-autopromote-allowlist", headers=bearer_headers(tenant_slug=slug))
        assert listed.json() == {"predicates": ["owned_by_team"]}

        revoked = await client.post(
            "/v1/admin/memory-autopromote-allowlist:revoke",
            json={"predicate": "owned_by_team"},
            headers=bearer_headers(tenant_slug=slug),
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json() == {"predicates": []}

        final = await client.get("/v1/admin/memory-autopromote-allowlist", headers=bearer_headers(tenant_slug=slug))
        assert final.json() == {"predicates": []}


@pytest.mark.asyncio
async def test_autopromote_allowlist_non_admin_returns_403(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]

    with _as_producer(admin_clients):
        get_resp = await client.get("/v1/admin/memory-autopromote-allowlist", headers=bearer_headers(tenant_slug=slug))
        allow_resp = await client.post(
            "/v1/admin/memory-autopromote-allowlist:allow",
            json={"predicate": "owned_by_team"},
            headers=bearer_headers(tenant_slug=slug),
        )
    assert get_resp.status_code == 403
    assert allow_resp.status_code == 403


# ---------------------------------------------------------------------------
# Source governance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_sources_declare_list_patch_lifecycle(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]
    tenant_id: uuid.UUID = admin_clients["tenant_id"]
    actor_id: uuid.UUID = admin_clients["actor_id"]
    pg_url: str = admin_clients["pg_url"]

    source_id = uuid.uuid4()
    await _seed_sync_source(pg_url, tenant_id=tenant_id, source_id=source_id, created_by=actor_id)

    with _as_admin(admin_clients):
        declared = await client.post(
            "/v1/admin/memory-sources",
            json={"source_id": str(source_id), "authority_tier": "owner_human", "ingest_ceiling": 100},
            headers=bearer_headers(tenant_slug=slug),
        )
        assert declared.status_code == 201, declared.text
        body = declared.json()
        assert body["source_id"] == str(source_id)
        assert body["authority_tier"] == "owner_human"
        assert body["ingest_ceiling"] == 100
        assert body["window_seconds"] == 3600
        assert body["breach_count"] == 0
        assert body["breaker_open_until"] is None
        # Off by default: declaring a source never auto-opts it into
        # provisioning entities for subjects that don't resolve.
        assert body["may_provision_entities"] is False

        listed = await client.get("/v1/admin/memory-sources", headers=bearer_headers(tenant_slug=slug))
        assert listed.status_code == 200, listed.text
        assert [s["source_id"] for s in listed.json()] == [str(source_id)]
        assert listed.json()[0]["may_provision_entities"] is False

        patched = await client.patch(
            f"/v1/admin/memory-sources/{source_id}",
            json={"ingest_ceiling": 250},
            headers=bearer_headers(tenant_slug=slug),
        )
        assert patched.status_code == 200, patched.text
        # authority_tier and window_seconds keep their existing values --
        # a PATCH naming only `ingest_ceiling` must not reset either, and the
        # provisioning flag is preserved the same way.
        assert patched.json()["authority_tier"] == "owner_human"
        assert patched.json()["window_seconds"] == 3600
        assert patched.json()["ingest_ceiling"] == 250
        assert patched.json()["may_provision_entities"] is False

        # PATCH opts the source into provisioning explicitly.
        opted_in = await client.patch(
            f"/v1/admin/memory-sources/{source_id}",
            json={"may_provision_entities": True},
            headers=bearer_headers(tenant_slug=slug),
        )
        assert opted_in.status_code == 200, opted_in.text
        assert opted_in.json()["may_provision_entities"] is True
        # A subsequent PATCH naming only an unrelated field must not flip it
        # back off -- the same merge-preserved contract as every other field.
        assert opted_in.json()["ingest_ceiling"] == 250

        unrelated_patch = await client.patch(
            f"/v1/admin/memory-sources/{source_id}",
            json={"ingest_ceiling": 300},
            headers=bearer_headers(tenant_slug=slug),
        )
        assert unrelated_patch.status_code == 200, unrelated_patch.text
        assert unrelated_patch.json()["may_provision_entities"] is True
        assert unrelated_patch.json()["ingest_ceiling"] == 300


@pytest.mark.asyncio
async def test_memory_sources_declare_may_provision_entities_true(admin_clients: _AdminClients) -> None:
    """The declare body's own `may_provision_entities` field, not only the PATCH's."""
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]
    tenant_id: uuid.UUID = admin_clients["tenant_id"]
    actor_id: uuid.UUID = admin_clients["actor_id"]
    pg_url: str = admin_clients["pg_url"]

    source_id = uuid.uuid4()
    await _seed_sync_source(pg_url, tenant_id=tenant_id, source_id=source_id, created_by=actor_id)

    with _as_admin(admin_clients):
        declared = await client.post(
            "/v1/admin/memory-sources",
            json={
                "source_id": str(source_id),
                "authority_tier": "owner_human",
                "may_provision_entities": True,
            },
            headers=bearer_headers(tenant_slug=slug),
        )
        assert declared.status_code == 201, declared.text
        assert declared.json()["may_provision_entities"] is True


@pytest.mark.asyncio
async def test_memory_sources_reset_breaker_clears_tripped_state(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]
    tenant_id: uuid.UUID = admin_clients["tenant_id"]
    actor_id: uuid.UUID = admin_clients["actor_id"]
    pg_url: str = admin_clients["pg_url"]

    source_id = uuid.uuid4()
    await _seed_sync_source(pg_url, tenant_id=tenant_id, source_id=source_id, created_by=actor_id)

    with _as_admin(admin_clients):
        declared = await client.post(
            "/v1/admin/memory-sources",
            json={"source_id": str(source_id), "authority_tier": "owner_human", "ingest_ceiling": 1},
            headers=bearer_headers(tenant_slug=slug),
        )
        assert declared.status_code == 201, declared.text

    # Trip the breaker directly through the service -- `admit()` is the
    # ingest-time gate connectors call, not a route this task's surface
    # exposes, so the fixture reaches it the same way `test_sync_ingest.py`
    # exercises connector-only behavior that has no HTTP route of its own.
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        governance = SourceGovernanceService(factory, clock=FakeClock(_NOW))
        first = await governance.admit(source_id, count=1)
        assert first.permitted, first.reason
        second = await governance.admit(source_id, count=1)
        assert not second.permitted
    finally:
        await engine.dispose()

    with _as_admin(admin_clients):
        tripped = await client.get("/v1/admin/memory-sources", headers=bearer_headers(tenant_slug=slug))
        assert tripped.status_code == 200, tripped.text
        assert tripped.json()[0]["breaker_open_until"] is not None
        assert tripped.json()[0]["breach_count"] == 1

        reset = await client.post(
            f"/v1/admin/memory-sources/{source_id}:reset-breaker",
            headers=bearer_headers(tenant_slug=slug),
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["breaker_open_until"] is None


@pytest.mark.asyncio
async def test_memory_sources_declare_unknown_source_returns_404(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]

    with _as_admin(admin_clients):
        resp = await client.post(
            "/v1/admin/memory-sources",
            json={"source_id": str(uuid.uuid4()), "authority_tier": "owner_human"},
            headers=bearer_headers(tenant_slug=slug),
        )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_memory_sources_declare_foreign_tenant_source_returns_403(pg_container: str) -> None:
    """A source declared by tenant A cannot be governed by tenant B, even
    though the row already exists -- `declare`'s ownership check runs
    against `sync_sources`, not the caller's own tenant."""
    slug_a = f"memadmin-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"memadmin-b-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        persona_a = harness.add_persona(slug_a, roles=["admin"])
        persona_b = harness.add_persona(slug_b, roles=["admin"])

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(persona_a)
            with patch_validator_for_actor(persona_a):
                whoami_a = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug_a))
                assert whoami_a.status_code == 200, whoami_a.text
                tenant_a = uuid.UUID(whoami_a.json()["tenant_id"])
                actor_a = uuid.UUID(whoami_a.json()["actor_id"])

            source_id = uuid.uuid4()
            await _seed_sync_source(pg_container, tenant_id=tenant_a, source_id=source_id, created_by=actor_a)

            harness.configure_fetcher_for(persona_b)
            with patch_validator_for_actor(persona_b):
                resp = await client.post(
                    "/v1/admin/memory-sources",
                    json={"source_id": str(source_id), "authority_tier": "owner_human"},
                    headers=bearer_headers(tenant_slug=slug_b),
                )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_memory_sources_non_admin_returns_403(admin_clients: _AdminClients) -> None:
    client: httpx.AsyncClient = admin_clients["client"]
    slug = admin_clients["slug"]

    with _as_producer(admin_clients):
        list_resp = await client.get("/v1/admin/memory-sources", headers=bearer_headers(tenant_slug=slug))
        declare_resp = await client.post(
            "/v1/admin/memory-sources",
            json={"source_id": str(uuid.uuid4()), "authority_tier": "owner_human"},
            headers=bearer_headers(tenant_slug=slug),
        )
    assert list_resp.status_code == 403
    assert declare_resp.status_code == 403
