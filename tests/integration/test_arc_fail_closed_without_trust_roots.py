"""Regression pin: ARC activation refuses closed with no trust root configured.

Nothing in this deployment registers an approval verifier, writes
`artifact_activation` evidence, or ever calls the verifier's own `.verify()`.
Activation's response to that gap is a deliberate design choice, not an
oversight: it refuses outright rather than falling through to checks that
would be satisfied by exactly the capability they exist to constrain (a
direct database write could set the lifecycle column itself). Falling open
here would let a deployment accumulate activated revisions while every
downstream reader -- selection, receipts -- trusts that "active" means
"approved".

This test drives the real, wired application (`create_app`, the same
`ArtifactService` instance the admin router calls) rather than a
service constructed by hand for the test, so it fails if a future wiring
change enables verification -- or makes it settings-driven -- before the
verifier-registration and evidence-writer surface actually exists to back
it up. That is the regression this pin exists to catch: nothing here
should regress the fail-closed default silently.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


@pytest_asyncio.fixture
async def client(harness: EntitlementAuthHarness) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def persona(harness: EntitlementAuthHarness, client: AsyncClient) -> tuple[TenantPersona, uuid.UUID]:
    """An admin persona, plus the tenant id the JIT path materialized for it.

    `activate` authorizes a tenant-scoped write against `admin` in the
    caller's own tenant, so the persona needs nothing more than that role --
    unlike a deployment-wide write, which no tenant role can ever satisfy.
    """
    p = harness.add_persona(f"arc-fail-closed-{uuid.uuid4().hex[:8]}", roles=["admin"])
    harness.configure_fetcher_for(p)
    with patch_validator_for_actor(p):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=p.slug))
        assert resp.status_code == 200, resp.text
        tenant_id = uuid.UUID(resp.json()["tenant_id"])
    return p, tenant_id


async def _seed_draft_revision(factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID) -> uuid.UUID:
    """One tenant-scoped artifact with one draft revision, no evidence attached.

    No directive or applicability rule is seeded. The refusal this test pins
    happens before either would be read -- activation checks the review date
    and then the verification gate before it ever loads a directive -- so
    seeding them would be scaffolding no assertion here touches.
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    artifact_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_artifacts (artifact_id, tenant_id, slug, kind, created_at) "
                "VALUES (:aid, :tid, :slug, 'policy', :now)"
            ),
            {"aid": artifact_id, "tid": tenant_id, "slug": f"a-{artifact_id.hex[:8]}", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO arc_revisions ("
                "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
                "  source_revision_locator, content_digest, lifecycle_state, effective_from,"
                "  review_expires_at, detail_audience, freshness_basis, content_classification,"
                "  content_retention_until, content_storage_mode, source_body_plaintext, created_at"
                ") VALUES ("
                "  :rid, :aid, :tid, 'test-system', :locator, :rlocator, :digest, 'draft', :efrom,"
                "  :review, 'all_matched_actors', 'revision_pinned_only', 'internal',"
                "  :retention, 'none', 'body', :now)"
            ),
            {
                "rid": revision_id,
                "aid": artifact_id,
                "tid": tenant_id,
                "locator": f"loc://{revision_id.hex[:8]}",
                "rlocator": f"loc://{revision_id.hex[:8]}@1",
                "digest": revision_id.hex + revision_id.hex,
                "efrom": now - datetime.timedelta(days=1),
                "review": now + datetime.timedelta(days=365),
                "retention": now + datetime.timedelta(days=730),
                "now": now,
            },
        )
    return revision_id


@pytest.mark.asyncio
async def test_activation_refuses_closed_with_no_trust_root_configured(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    persona: tuple[TenantPersona, uuid.UUID],
) -> None:
    p, tenant_id = persona
    revision_id = await _seed_draft_revision(factory, tenant_id=tenant_id)

    with patch_validator_for_actor(p):
        resp = await client.post(
            f"/v1/arc/admin/revisions/{revision_id}/activate",
            json={},
            headers=bearer_headers(tenant_slug=p.slug),
        )

    # Refuses rather than proceeding open: a 409 naming the lifecycle
    # conflict, not a 200 that put the revision into force and not a 500
    # that would hide the refusal as a crash.
    assert resp.status_code == 409, resp.text
    error = resp.json()["errors"][0]
    assert error["code"] == "lifecycle_conflict"
    assert "verification is not configured" in error["message"]

    # The state the "closed" half of fail-closed actually refers to: the
    # revision must still be exactly what it was before the request, not
    # merely that the response looked like a refusal.
    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT lifecycle_state FROM arc_revisions WHERE revision_id = :rid"),
                {"rid": revision_id},
            )
        ).scalar_one()
    assert state == "draft"
