"""Integration tests for the memory curation REST surface: the curation
queue and its two curator verdicts, link and discard.

Verifies the full HTTP surface against a live FastAPI app + Postgres:

- GET  /v1/memory/curation-queue                 → tenant-scoped items + counts
- GET  ... keyset pagination across a real query  → next_cursor advances, no dupes
- POST /v1/memory/claims/{id}:link                → unlinked claim gets a subject
- POST /v1/memory/claims/{id}:discard             → a staged claim closes
- POST /v1/memory/claims/{id}:discard             → a never-resolvable unlinked
  claim closes too -- the migration this task ships legalizes exactly this
  row shape (`rejected`, subject and confidence both still NULL). Before it,
  the database itself refused the write.
- Cross-tenant discard is refused (404-shaped by way of 403 -- the service's
  own tenancy check, not a route-level re-check).
- Linking to a claim that does not exist is 404.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.memory.claim_authority import Evidence
from registry.service.memory.claim_ontology import seed_ontology
from registry.types import TenantContext
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.UTC)
_EV = (Evidence(kind="session_event", ref="evt-1", excerpt="it depends on billing"),)


async def _seed_ontology(pg_url: str) -> None:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))
    finally:
        await engine.dispose()


async def _seed_actor(pg_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    actor_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:aid, :tid, 'seed', :sub, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "sub": f"seed-{actor_id.hex[:8]}", "now": _NOW},
            )
    finally:
        await engine.dispose()
    return actor_id


async def _seed_entity(pg_url: str, tenant_id: uuid.UUID) -> uuid.UUID:
    entity_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, "
                    "                      visibility, is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :name, 'tenant-shared', TRUE, :now)"
                ),
                {"eid": entity_id, "tid": tenant_id, "name": f"cap-{entity_id.hex[:8]}", "now": _NOW},
            )
    finally:
        await engine.dispose()
    return entity_id


async def _row(pg_url: str, claim_id: uuid.UUID) -> dict[str, object]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            result = (
                await session.execute(
                    text("SELECT status, subject_entity_id, confidence FROM memory_claims WHERE claim_id = :cid"),
                    {"cid": claim_id},
                )
            ).one()
        return {"status": result.status, "subject_entity_id": result.subject_entity_id, "confidence": result.confidence}
    finally:
        await engine.dispose()


async def _materialise_persona(harness: EntitlementAuthHarness, persona: TenantPersona) -> uuid.UUID:
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
            assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["tenant_id"])


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    await _seed_ontology(pg_container)
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


def _client(harness: EntitlementAuthHarness) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test")


# ---------------------------------------------------------------------------
# Queue listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_lists_an_unlinked_claim_and_the_counts_agree(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"curq-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    unlinked = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/memory/curation-queue", headers=bearer_headers(tenant_slug=persona.slug))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert [i["claim_id"] for i in body["items"]] == [str(unlinked.claim_id)]
            assert body["items"][0]["reason"] == "unlinked"
            assert body["items"][0]["available_actions"] == ["link", "discard"]
            assert body["next_cursor"] is None

            counts_resp = await client.get(
                "/v1/memory/curation-queue?counts=true", headers=bearer_headers(tenant_slug=persona.slug)
            )
            assert counts_resp.status_code == 200
            assert counts_resp.json() == {"counts": {"unlinked": 1}}


@pytest.mark.asyncio
async def test_queue_pagination_advances_the_cursor_without_duplicates(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"curq-page-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])
    staged_ids = []
    for i in range(3):
        c = await claims.stage_claim(
            ctx,
            subject_reference=f"github:acme/mystery-{i}",
            predicate="owned_by_team",
            value="platform",
            evidence=_EV,
        )
        staged_ids.append(str(c.claim_id))

    page_size = 2
    seen: list[str] = []
    pages: list[int] = []
    cursor: str | None = None
    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            for _ in range(5):  # bounded loop; 3 items at page_size=2 needs 2 pages
                url = f"/v1/memory/curation-queue?page_size={page_size}"
                if cursor is not None:
                    url += f"&cursor={cursor}"
                resp = await client.get(url, headers=bearer_headers(tenant_slug=persona.slug))
                assert resp.status_code == 200, resp.text
                body = resp.json()
                pages.append(len(body["items"]))
                seen.extend(i["claim_id"] for i in body["items"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break

    assert sorted(seen) == sorted(staged_ids)
    assert len(seen) == len(set(seen))
    # `page_size` bounds each page; ignoring it entirely and returning all
    # three rows on page one with a null cursor would still pass the two
    # assertions above, so pagination itself is pinned directly here.
    assert len(pages) >= 2, f"expected at least two pages at page_size={page_size}, got {pages}"
    assert all(n <= page_size for n in pages), f"a page exceeded page_size={page_size}: {pages}"


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_gives_an_unlinked_claim_a_subject(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"link-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    unlinked = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{unlinked.claim_id}:link",
                json={"subject_reference": str(subject)},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "staged"
            assert body["subject_entity_id"] == str(subject)

            # Now staged and no longer flagged for any queue reason.
            queue_resp = await client.get("/v1/memory/curation-queue", headers=bearer_headers(tenant_slug=persona.slug))
            assert queue_resp.json()["items"] == []

    row = await _row(pg_container, unlinked.claim_id)
    assert row["status"] == "staged"
    assert row["subject_entity_id"] == subject


@pytest.mark.asyncio
async def test_link_on_a_missing_claim_is_404(harness: EntitlementAuthHarness) -> None:
    persona = harness.add_persona(f"link-404-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, persona)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{uuid.uuid4()}:link",
                json={"subject_reference": "whatever"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# discard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_closes_a_staged_claim(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"disc-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    staged = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{staged.claim_id}:discard",
                json={"reason": "wrong team, corrected verbally"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "discarded"}

    row = await _row(pg_container, staged.claim_id)
    assert row["status"] == "rejected"


@pytest.mark.asyncio
async def test_discard_closes_a_never_resolvable_unlinked_claim_end_to_end(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """The migration this task ships legalizes exactly this row shape:
    `rejected`, subject and confidence both still NULL. Before it, the CHECK
    constraints made the write impossible and `discard` refused with a
    conflict; a reference that will never resolve had no way out of the
    queue."""
    persona = harness.add_persona(f"disc-unlinked-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    unlinked = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference="github:acme/never-will-resolve",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{unlinked.claim_id}:discard",
                json={"reason": "dead reference, will never resolve"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "discarded"}

    row = await _row(pg_container, unlinked.claim_id)
    assert row["status"] == "rejected"
    assert row["subject_entity_id"] is None
    assert row["confidence"] is None


@pytest.mark.asyncio
async def test_discard_from_a_foreign_tenant_is_refused(harness: EntitlementAuthHarness, pg_container: str) -> None:
    owner = harness.add_persona(f"disc-owner-{uuid.uuid4().hex[:8]}")
    stranger = harness.add_persona(f"disc-stranger-{uuid.uuid4().hex[:8]}")
    owner_tenant_id = await _materialise_persona(harness, owner)
    await _materialise_persona(harness, stranger)
    actor_id = await _seed_actor(pg_container, owner_tenant_id)
    subject = await _seed_entity(pg_container, owner_tenant_id)

    claims = harness.app.state.services.claims
    staged = await claims.stage_claim(
        TenantContext(tenant_id=owner_tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(stranger):
            resp = await client.post(
                f"/v1/memory/claims/{staged.claim_id}:discard",
                json={"reason": "not mine to discard"},
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
    assert resp.status_code == 403

    row = await _row(pg_container, staged.claim_id)
    assert row["status"] == "staged"
