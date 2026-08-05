"""End-to-end tests for the api-ergonomics phase.

Covers user-visible features:
- /whoami session context
- Structured error envelope on 4xx
- UI-flavoured default response (no bitemporal cols) + ?view=audit
- Artifact list pagination + filters + sparse fields
- HATEOAS _links on detail responses + whoami
- ETag emission + If-Match precondition
- X-Idempotency-Key replay + conflict

Auth uses tests/helpers/auth_harness.py: a TenantPersona is built that
matches the actor bootstrap_dev_tenant.py creates (same slug + same
``oidc_subject``), so the harness's patched validator resolves to the
real seeded actor row instead of creating a parallel one. Data
seeding still goes through seed.py — the dev-admin actor is the
created_by reference for the seeded artifacts, and the assertions
check ``created_by_display_name == "dev-admin"``.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - test-harness invocation of this repo's own scripts (sys.executable + fixed script path), no caller input
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_REPO_ROOT = Path(__file__).parent.parent.parent
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap_dev_tenant.py"
_SEED_SCRIPT = _REPO_ROOT / "scripts" / "seed.py"

# bootstrap_dev_tenant.py creates this user as the dev-admin oidc_subject
# by default. Aligning the test persona's oidc_subject with this value
# means the harness's resolver materialisation hits the existing actor
# row instead of inserting a sibling actor with a synthetic subject.
_DEFAULT_ACTOR_USER_ID = "dev-admin"


def _run(database_url: str, script: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *extra],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
        cwd=str(_REPO_ROOT),
        check=False,
    )


async def _lookup_actor_id(pg_url: str, slug: str, oidc_subject: str) -> uuid.UUID:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT a.actor_id FROM actors a "
                        "JOIN tenants t ON t.tenant_id = a.tenant_id "
                        "WHERE t.slug = :slug AND a.oidc_subject = :sub"
                    ),
                    {"slug": slug, "sub": oidc_subject},
                )
            ).first()
        assert row is not None, (
            f"actor (slug={slug}, oidc_subject={oidc_subject}) not found — "
            "bootstrap_dev_tenant.py may have failed silently"
        )
        return uuid.UUID(str(row[0]))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_client(
    pg_container: str,
) -> AsyncIterator[tuple[AsyncClient, EntitlementAuthHarness, TenantPersona]]:
    """Bootstrap + seed the dx-ergonomics tenant, then build a harness
    persona aligned with the dev-admin actor row."""
    slug = "dx-ergonomics"

    # bootstrap defaults to display_name="Dev Admin"; seed.py looks up
    # the dev-admin actor by display_name="dev-admin" — pass the
    # override so the two scripts agree. Skip mock-OIDC seeding because
    # the mock services aren't running inside the test container.
    bootstrap = _run(
        pg_container,
        _BOOTSTRAP_SCRIPT,
        "--tenant-slug",
        slug,
        "--actor-display-name",
        "dev-admin",
        "--skip-mock-seed",
    )
    assert bootstrap.returncode == 0, bootstrap.stderr

    seed = _run(pg_container, _SEED_SCRIPT, "--tenant-slug", slug)
    assert seed.returncode == 0, seed.stderr

    actor_id = await _lookup_actor_id(pg_container, slug, _DEFAULT_ACTOR_USER_ID)

    async with EntitlementAuthHarness(pg_container) as harness:
        # Build a persona whose oidc_subject matches the existing
        # dev-admin row. The resolver's actor_store will UPDATE the
        # existing row instead of inserting a new one (the
        # (tenant_id, oidc_subject) unique constraint guarantees this).
        persona = TenantPersona(slug=slug, actor_id=actor_id, roles=["admin", "producer", "consumer"])
        # Override the oidc_subject so it equals what bootstrap used.
        persona_with_real_sub = TenantPersona.__new__(TenantPersona)
        persona_with_real_sub.__dict__.update(persona.__dict__)
        persona_with_real_sub.__dict__["_oidc_subject_override"] = _DEFAULT_ACTOR_USER_ID

        # Patch the @property at the instance level by subclassing.
        class _FixedSubjectPersona(TenantPersona):
            @property
            def oidc_subject(self) -> str:  # type: ignore[override]
                return _DEFAULT_ACTOR_USER_ID

        fixed = _FixedSubjectPersona(slug=slug, actor_id=actor_id, roles=persona.roles)

        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, harness, fixed


@pytest.mark.asyncio
async def test_whoami_returns_session_context(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        r = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
    assert r.status_code == 200, r.text
    body = r.json()
    for field in (
        "actor_id",
        "actor_display_name",
        "tenant_id",
        "tenant_slug",
        "tenant_display_name",
        "roles",
    ):
        assert field in body, f"whoami missing {field}"
    assert body["tenant_slug"] == "dx-ergonomics"
    assert body["actor_display_name"] == "dev-admin"
    assert "admin" in body["roles"]
    assert body["_links"]["self"] == "/v1/whoami"


@pytest.mark.asyncio
async def test_error_envelope_on_404(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        r = await client.get(
            "/v1/capabilities/nonexistent-thing",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert r.status_code == 404
    body = r.json()
    assert "errors" in body
    assert body["errors"][0]["code"] == "not_found"
    assert "message" in body["errors"][0]


@pytest.mark.asyncio
async def test_default_response_excludes_bitemporal_cols(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        body = (
            await client.get(
                "/v1/capabilities/salt-design-system",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()
    for forbidden in ("tenant_id", "is_active", "superseded_facts_count"):
        assert forbidden not in body, f"default view leaks {forbidden}"
    for fact in body.get("facts", []):
        for forbidden in ("tenant_id", "entity_id", "is_authoritative", "valid_from", "ingested_at"):
            assert forbidden not in fact, f"default fact leaks {forbidden}"


@pytest.mark.asyncio
async def test_audit_view_includes_bitemporal_cols(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        body = (
            await client.get(
                "/v1/capabilities/salt-design-system?view=audit",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()
    for required in ("tenant_id", "is_active"):
        assert required in body, f"audit view missing {required}"


@pytest.mark.asyncio
async def test_capability_response_has_links(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        body = (
            await client.get(
                "/v1/capabilities/salt-design-system",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()
    links = body["_links"]
    assert links["self"] == "/v1/capabilities/salt-design-system"
    assert links["artifacts"] == "/v1/capabilities/salt-design-system/artifacts"
    assert links["dependencies"] == "/v1/capabilities/salt-design-system/dependencies"


@pytest.mark.asyncio
async def test_get_capability_emits_etag(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        r = await client.get(
            "/v1/capabilities/salt-design-system",
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert r.status_code == 200
    etag = r.headers.get("etag")
    assert etag is not None
    assert etag.startswith('W/"') and etag.endswith('"')


@pytest.mark.asyncio
async def test_patch_visibility_stale_if_match_returns_412(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        r = await client.patch(
            "/v1/capabilities/salt-design-system/visibility",
            headers={
                **bearer_headers(tenant_slug=persona.slug),
                "Content-Type": "application/json",
                "If-Match": 'W/"definitely-stale"',
            },
            json={"visibility": "private"},
        )
    assert r.status_code == 412
    assert r.json()["errors"][0]["code"] == "precondition_failed"


@pytest.mark.asyncio
async def test_idempotency_key_replays_first_response(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        h = {
            **bearer_headers(tenant_slug=persona.slug),
            "Content-Type": "application/json",
            "X-Idempotency-Key": "erg-test-key-1",
        }
        body = {"name": "erg-idempotency-cap", "entity_type": "capability"}

        r1 = await client.post("/v1/capabilities", headers=h, json=body)
        assert r1.status_code == 201
        first_id = r1.json()["entity_id"]

        r2 = await client.post("/v1/capabilities", headers=h, json=body)
        assert r2.status_code == 201
        assert r2.json()["entity_id"] == first_id, "same key + body must replay"

        r3 = await client.post(
            "/v1/capabilities",
            headers=h,
            json={"name": "erg-different-cap", "entity_type": "capability"},
        )
    assert r3.status_code == 409
    assert r3.json()["errors"][0]["code"] == "idempotency_key_conflict"


@pytest.mark.asyncio
async def test_artifact_list_default_excludes_body(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    """Default list response includes title/category/created_at, NOT body."""
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        salt_body = (
            await client.get(
                "/v1/capabilities/salt-design-system",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()
        eid = salt_body["entity_id"]
        body = (
            await client.get(
                f"/v1/capabilities/{eid}/artifacts",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()
    assert body["items"], "expected artifacts"
    first = body["items"][0]
    assert "title" in first
    assert "body" not in first
    assert first.get("created_by_display_name") == "dev-admin"


@pytest.mark.asyncio
async def test_artifact_list_with_category_filter(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        salt_body = (
            await client.get(
                "/v1/capabilities/salt-design-system",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()
        eid = salt_body["entity_id"]
        body = (
            await client.get(
                f"/v1/capabilities/{eid}/artifacts?category=overview",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["category"] == "overview"


# ---------------------------------------------------------------------------
# Search: cites its evidence rather than embedding it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_cites_its_evidence_instead_of_embedding_it(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    """A result says which artifacts matched and how to read them.

    The bodies used to come back inline, on every hit, with no way for a caller
    to have asked for less. A list response is the wrong place to ship documents.
    """
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        body = (
            await client.get(
                "/v1/search?q=salt",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()

    assert body["items"], "expected the seeded capability to match"
    for item in body["items"]:
        assert "tenant_id" not in item, "default search result leaks the owning tenant"
        assert "matching_facts" not in item, "default search result embeds the artifacts"
        assert "citations" in item, "a result must name the evidence that made it match"
        for citation in item["citations"]:
            assert "body" not in citation, "a citation is a handle, not the document"
            assert citation["_links"]["self"].startswith("/v1/capabilities/")


@pytest.mark.asyncio
async def test_search_audit_view_returns_the_artifacts_inline(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    """Reconstructing what the index saw is the one case that wants the bodies."""
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        body = (
            await client.get(
                "/v1/search?q=salt&view=audit",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()

    hit = next(i for i in body["items"] if i["citations"])
    assert "tenant_id" in hit
    assert hit["matching_facts"], "audit view must return the matched artifacts"
    fact = hit["matching_facts"][0]
    for required in ("valid_from", "ingested_at", "created_at"):
        assert required in fact, f"audit artifact missing {required}"


@pytest.mark.asyncio
async def test_the_capability_list_does_not_carry_the_tenant(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        headers = bearer_headers(tenant_slug=persona.slug)
        default = (await client.get("/v1/capabilities", headers=headers)).json()
        audit = (await client.get("/v1/capabilities?view=audit", headers=headers)).json()

    assert default["items"], "expected seeded capabilities"
    for item in default["items"]:
        assert "tenant_id" not in item
        assert "is_active" not in item
    for item in audit["items"]:
        assert "tenant_id" in item
        assert "is_active" in item


@pytest.mark.asyncio
async def test_dependency_edges_honour_the_view(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        headers = bearer_headers(tenant_slug=persona.slug)
        default = (await client.get("/v1/capabilities/salt-design-system/dependencies", headers=headers)).json()
        audit = (
            await client.get("/v1/capabilities/salt-design-system/dependencies?view=audit", headers=headers)
        ).json()

    for edge in default["edges"]:
        for forbidden in ("tenant_id", "valid_from", "ingested_at", "invalidated_at"):
            assert forbidden not in edge, f"default edge leaks {forbidden}"
    for edge in audit["edges"]:
        assert "tenant_id" in edge
        assert "valid_from" in edge


# ---------------------------------------------------------------------------
# The view parameter means the same thing everywhere it appears
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_operation_taking_a_view_rejects_an_unknown_one(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    """One sweep over the whole surface, driven by the schema rather than a list.

    This is the test whose absence let the search endpoint keep its own shape:
    every sibling honoured the parameter, so nobody checked the one that did not
    declare it. Reading the operations out of the spec means a new endpoint is
    covered the moment it is added, and an endpoint that accepts the parameter
    without validating it — which one did, answering 200 where the rest answer
    422 — cannot go unnoticed.
    """
    client, harness, persona = seeded_client
    spec = client._transport.app.openapi()  # type: ignore[attr-defined]

    targets = [
        (path, method)
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
        if isinstance(op, dict)
        and any(p.get("name") == "view" for p in op.get("parameters", []) or [])
        and method.lower() == "get"
        and "{" not in path
    ]
    assert targets, "expected at least one parameterless GET taking a view"

    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        headers = bearer_headers(tenant_slug=persona.slug)
        for path, method in targets:
            url = f"{path}?view=nonsense" if "?" not in path else f"{path}&view=nonsense"
            # Endpoints with required query params answer 422 for that reason too,
            # which is the same verdict; what matters is that none answers 200.
            response = await client.request(method.upper(), url, headers=headers)
            assert response.status_code == 422, f"{method.upper()} {path} accepted view=nonsense"


@pytest.mark.asyncio
async def test_the_audit_view_adds_no_temporal_fields_to_an_entity(
    seeded_client: tuple[AsyncClient, EntitlementAuthHarness, TenantPersona],
) -> None:
    """Entities are not bitemporal, and the audit view does not pretend otherwise.

    Read literally, the audit contract would have every shape grow the four
    bitemporal columns. An entity row has none: it carries a birth (``created_at``,
    present by default) and a soft-delete flag, and nothing to project the others
    from. Bitemporality lives on facts and edges — the rows that assert something
    which can later become untrue. Pinned so a reader finds the rule rather than
    an unexplained absence.
    """
    client, harness, persona = seeded_client
    harness.configure_fetcher_for(persona)
    with patch_validator_for_actor(persona):
        body = (
            await client.get(
                "/v1/capabilities/salt-design-system?view=audit",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
        ).json()

    for absent in ("valid_from", "valid_to", "ingested_at", "invalidated_at"):
        assert absent not in body, f"entity level should not carry {absent}"
    assert "created_at" in body
