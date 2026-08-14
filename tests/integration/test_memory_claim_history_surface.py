"""Integration tests for the claim-history REST surface: `GET .../history`
(`chain_for`) and `GET .../believed` (`believed_at`), over a live FastAPI
app + Postgres.

`ClaimHistoryService.chain_for`/`believed_at` take no tenant context by
design -- they read `memory_claims` rows by id or by subject alone. These
tests exercise the router's own tenant-enforcement wrap around them:

- GET /v1/memory/claims/{id}/history       → the supersession chain, oldest
  first, including a human confirmation's own entry
- GET .../history on a claim in another tenant's private subject, and on a
  claim id that never existed → the identical 404 (status and body), so a
  claim id is never a cross-tenant existence oracle
- GET .../history on a claim about a PUBLIC subject whose chain later
  narrowed to a private claim → the narrower entry is filtered out of the
  response, not treated as a reason to refuse the whole chain
- GET /v1/memory/claims/believed?as_of=    → the belief set at a past
  instant, picking the claim that was current then, not now
- GET .../believed on a private subject in another tenant, and on a
  subject id that never existed → the identical 404
- GET .../believed with a malformed as_of  → 422, before the service (and
  its `memory_claims` read) is ever touched
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

from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.memory.claim_authority import Evidence, StagedClaim
from contextplane.service.memory.claim_ontology import seed_ontology
from contextplane.types import TenantContext
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


async def _seed_entity(pg_url: str, tenant_id: uuid.UUID, *, visibility: str = "public") -> uuid.UUID:
    entity_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, "
                    "                      visibility, is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :name, :vis, TRUE, :now)"
                ),
                {
                    "eid": entity_id,
                    "tid": tenant_id,
                    "name": f"cap-{entity_id.hex[:8]}",
                    "vis": visibility,
                    "now": _NOW,
                },
            )
    finally:
        await engine.dispose()
    return entity_id


async def _materialise_persona(harness: EntitlementAuthHarness, persona: TenantPersona) -> tuple[uuid.UUID, uuid.UUID]:
    """JIT-materialise *persona*'s tenant + actor row via `/v1/whoami`.

    Returns `(tenant_id, actor_id)`. The actor row this creates gets the
    schema default `actor_kind='human'`, which is what `:confirm` needs.
    """
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
            assert resp.status_code == 200, resp.text
    body = resp.json()
    return uuid.UUID(body["tenant_id"]), uuid.UUID(body["actor_id"])


async def _stage_and_consolidate(
    harness: EntitlementAuthHarness,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    subject: uuid.UUID,
    value: object,
    predicate: str = "owned_by_team",
    visibility: str | None = None,
) -> uuid.UUID:
    """Stage a claim and reconcile it against its neighbourhood, against the
    running app's own services -- not the REST surface, which has no
    `stage`/`consolidate` route (extraction and the sweep are the production
    callers)."""
    services = harness.app.state.services
    claim: StagedClaim = await services.claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=_EV,
        visibility=visibility,
    )
    await services.consolidation.consolidate(claim.claim_id)
    return claim.claim_id


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    await _seed_ontology(pg_container)
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


def _client(harness: EntitlementAuthHarness) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /v1/memory/claims/{id}/history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_history_shows_the_chain_across_a_confirmation_in_order(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"hist-confirm-{uuid.uuid4().hex[:8]}")
    tenant_id, actor_id = await _materialise_persona(harness, persona)
    subject = await _seed_entity(pg_container, tenant_id)

    original = await _stage_and_consolidate(
        harness, tenant_id=tenant_id, actor_id=actor_id, subject=subject, value="platform"
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            confirm_resp = await client.post(
                f"/v1/memory/claims/{original}:confirm",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert confirm_resp.status_code == 200, confirm_resp.text
            confirmed_claim_id = confirm_resp.json()["claim_id"]

            resp = await client.get(
                f"/v1/memory/claims/{original}/history",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["claim_id"] for item in items] == [str(original), confirmed_claim_id]
    assert items[0]["superseded_by"] == confirmed_claim_id
    assert items[0]["superseded_reason"] == "human_confirmed"
    assert items[0]["was_current"] is False
    assert items[1]["was_current"] is True
    assert items[1]["source_authority"] == "owner_human"


@pytest.mark.asyncio(loop_scope="module")
async def test_history_on_a_foreign_tenants_private_claim_is_the_same_404_as_missing(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """A claim id is never a cross-tenant existence oracle -- a private
    claim in someone else's tenant and a claim id nobody ever staged answer
    identically."""
    owner = harness.add_persona(f"hist-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, owner_actor = await _materialise_persona(harness, owner)
    stranger = harness.add_persona(f"hist-stranger-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, stranger)

    subject = await _seed_entity(pg_container, owner_tenant, visibility="private")
    claim_id = await _stage_and_consolidate(
        harness, tenant_id=owner_tenant, actor_id=owner_actor, subject=subject, value="platform"
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(stranger):
            foreign_resp = await client.get(
                f"/v1/memory/claims/{claim_id}/history",
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
            missing_resp = await client.get(
                f"/v1/memory/claims/{uuid.uuid4()}/history",
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
    assert foreign_resp.status_code == 404, foreign_resp.text
    assert missing_resp.status_code == 404, missing_resp.text
    assert foreign_resp.json() == missing_resp.json()


@pytest.mark.asyncio(loop_scope="module")
async def test_history_filters_a_chain_entry_narrower_than_the_caller_may_see(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """The subject is public, so a different tenant can see it and the
    first claim about it (also public) -- but a later claim that narrowed
    its own visibility to private must not be served to that tenant, even
    though it appears later in the same chain the caller can otherwise
    read."""
    owner = harness.add_persona(f"hist-narrow-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, owner_actor = await _materialise_persona(harness, owner)
    reader = harness.add_persona(f"hist-narrow-reader-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, reader)

    subject = await _seed_entity(pg_container, owner_tenant, visibility="public")
    first = await _stage_and_consolidate(
        harness, tenant_id=owner_tenant, actor_id=owner_actor, subject=subject, value="platform"
    )
    second = await _stage_and_consolidate(
        harness,
        tenant_id=owner_tenant,
        actor_id=owner_actor,
        subject=subject,
        value="billing",
        visibility="private",
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(owner):
            owner_resp = await client.get(
                f"/v1/memory/claims/{first}/history",
                headers=bearer_headers(tenant_slug=owner.slug),
            )
        with patch_validator_for_actor(reader):
            reader_resp = await client.get(
                f"/v1/memory/claims/{first}/history",
                headers=bearer_headers(tenant_slug=reader.slug),
            )

    assert owner_resp.status_code == 200, owner_resp.text
    owner_ids = [item["claim_id"] for item in owner_resp.json()["items"]]
    assert owner_ids == [str(first), str(second)], "the owning tenant sees its own full chain"

    assert reader_resp.status_code == 200, reader_resp.text
    reader_ids = [item["claim_id"] for item in reader_resp.json()["items"]]
    assert reader_ids == [str(first)], "the narrower entry is filtered, not a reason to refuse the chain"


@pytest.mark.asyncio(loop_scope="module")
async def test_history_on_a_missing_claim_is_404(harness: EntitlementAuthHarness) -> None:
    persona = harness.add_persona(f"hist-404-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, persona)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.get(
                f"/v1/memory/claims/{uuid.uuid4()}/history",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# GET /v1/memory/claims/believed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_believed_picks_the_claim_that_was_current_at_the_given_instant(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"believed-{uuid.uuid4().hex[:8]}")
    tenant_id, actor_id = await _materialise_persona(harness, persona)
    subject = await _seed_entity(pg_container, tenant_id)

    before = datetime.datetime.now(datetime.UTC)
    first = await _stage_and_consolidate(
        harness, tenant_id=tenant_id, actor_id=actor_id, subject=subject, value="platform"
    )
    between = datetime.datetime.now(datetime.UTC)
    second = await _stage_and_consolidate(
        harness, tenant_id=tenant_id, actor_id=actor_id, subject=subject, value="billing"
    )
    after = datetime.datetime.now(datetime.UTC)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            before_resp = await client.get(
                "/v1/memory/claims/believed",
                params={"subject_entity_id": str(subject), "as_of": before.isoformat()},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            between_resp = await client.get(
                "/v1/memory/claims/believed",
                params={"subject_entity_id": str(subject), "as_of": between.isoformat()},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            after_resp = await client.get(
                "/v1/memory/claims/believed",
                params={"subject_entity_id": str(subject), "as_of": after.isoformat()},
                headers=bearer_headers(tenant_slug=persona.slug),
            )

    assert before_resp.status_code == 200, before_resp.text
    assert before_resp.json()["items"] == [], "nobody had asserted anything yet"

    assert between_resp.status_code == 200, between_resp.text
    between_items = between_resp.json()["items"]
    assert [item["claim_id"] for item in between_items] == [str(first)]
    assert between_items[0]["value"] == "platform"

    assert after_resp.status_code == 200, after_resp.text
    after_items = after_resp.json()["items"]
    assert [item["claim_id"] for item in after_items] == [str(second)]
    assert after_items[0]["value"] == "billing"


@pytest.mark.asyncio(loop_scope="module")
async def test_believed_on_a_foreign_tenants_private_subject_is_the_same_404_as_missing(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    owner = harness.add_persona(f"believed-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, _owner_actor = await _materialise_persona(harness, owner)
    stranger = harness.add_persona(f"believed-stranger-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, stranger)

    subject = await _seed_entity(pg_container, owner_tenant, visibility="private")

    async with _client(harness) as client:
        with patch_validator_for_actor(stranger):
            foreign_resp = await client.get(
                "/v1/memory/claims/believed",
                params={"subject_entity_id": str(subject), "as_of": _NOW.isoformat()},
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
            missing_resp = await client.get(
                "/v1/memory/claims/believed",
                params={"subject_entity_id": str(uuid.uuid4()), "as_of": _NOW.isoformat()},
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
    assert foreign_resp.status_code == 404, foreign_resp.text
    assert missing_resp.status_code == 404, missing_resp.text
    assert foreign_resp.json() == missing_resp.json()


@pytest.mark.asyncio(loop_scope="module")
async def test_believed_with_a_malformed_as_of_is_422(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"believed-badasof-{uuid.uuid4().hex[:8]}")
    tenant_id, _actor_id = await _materialise_persona(harness, persona)
    subject = await _seed_entity(pg_container, tenant_id)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.get(
                "/v1/memory/claims/believed",
                params={"subject_entity_id": str(subject), "as_of": "not-a-datetime"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 422, resp.text
