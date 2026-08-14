"""Integration tests for the promotion-review REST surface: proposals, their
accept/reject verdicts, and reversal.

Verifies the full HTTP surface against a live FastAPI app + Postgres:

- GET   /v1/memory/promotion-proposals            → tenant-scoped, keyset-paged
- GET   ... cursor round-trip across a real query  → every page advances, no dupes
- GET   /v1/memory/promotion-proposals/{id}        → one proposal's full view
- GET   ... owned by a foreign tenant               → 404 (same shape as missing)
- PATCH /v1/memory/promotion-proposals/{id}        → accept writes the canonical
  row (verified by reading `attributes` directly, not by trusting the echoed
  response body)
- PATCH ... state=rejected                          → the claim stays staged and
  serving; nothing is written to the canonical graph
- PATCH ... a non-owner tenant                      → 403 (the service's own gate)
- PATCH ... an already-decided proposal             → 409
- POST  /v1/memory/promotions/{id}:reverse         → the prior canonical value
  comes back, proven by a direct read, not by trusting the 200

`propose()` itself has no REST route in this task (the sweep worker is the
only production caller) -- proposals are built here by calling
`ClaimService.stage_claim` / `ConsolidationService.consolidate` /
`PromotionService.propose` directly against the harness's app, exactly as
`tests/integration/test_promotion.py` does. Everything downstream of a built
proposal -- list, get, accept, reject, reverse -- goes over HTTP.
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
from contextplane.service.memory.claim_authority import Evidence
from contextplane.service.memory.claim_ontology import seed_ontology
from contextplane.service.memory.promotion import Proposal
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


async def _attributes(pg_url: str, entity_id: uuid.UUID, key: str) -> list[dict[str, object]]:
    """The canonical read path this task's accept/reverse tests verify
    against -- the same shape `tests/integration/test_promotion.py` uses,
    read directly rather than trusting an HTTP response body."""
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT attr_id, value, t_valid_from, t_valid_to, t_invalidated_at "
                            "  FROM attributes WHERE entity_id = :eid AND key = :key "
                            " ORDER BY t_valid_from"
                        ),
                        {"eid": entity_id, "key": key},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]
    finally:
        await engine.dispose()


async def _live_value(pg_url: str, entity_id: uuid.UUID, key: str) -> object:
    """What the graph says right now. `_attributes` already orders by
    `t_valid_from`, so the last live row in that order is the current one --
    no re-sort needed."""
    rows = await _attributes(pg_url, entity_id, key)
    live = [r for r in rows if r["t_invalidated_at"] is None]
    return live[-1]["value"] if live else None


async def _claim_row(pg_url: str, claim_id: uuid.UUID) -> dict[str, object]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            result = (
                await session.execute(
                    text("SELECT status, promotion_state FROM memory_claims WHERE claim_id = :cid"),
                    {"cid": claim_id},
                )
            ).one()
        return {"status": result.status, "promotion_state": result.promotion_state}
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


async def _stage_and_propose(
    harness: EntitlementAuthHarness,
    pg_url: str,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    subject: uuid.UUID,
    *,
    predicate: str = "owned_by_team",
    value: object = "platform",
) -> Proposal:
    """A proposal, built the only way one can be today: stage, consolidate,
    propose, all against the running app's own services -- not the REST
    surface, which has no `propose` route (the sweep worker is the one
    production caller)."""
    services = harness.app.state.services
    claim = await services.claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=_EV,
    )
    await services.consolidation.consolidate(claim.claim_id)
    proposal: Proposal | None = await services.promotion.propose(claim.claim_id)
    assert proposal is not None, "the staged claim was not eligible for promotion"
    return proposal


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    await _seed_ontology(pg_container)
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


def _client(harness: EntitlementAuthHarness) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /v1/memory/promotion-proposals -- list + cursor round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_list_pages_through_open_proposals_without_duplicates(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"prop-list-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)

    proposal_ids: list[str] = []
    for i in range(3):
        subject = await _seed_entity(pg_container, tenant_id)
        proposal = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value=f"team-{i}")
        proposal_ids.append(str(proposal.proposal_id))

    page_size = 2
    seen: list[str] = []
    pages: list[int] = []
    cursor: str | None = None
    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            for _ in range(5):  # bounded loop; 3 items at page_size=2 needs 2 pages
                url = f"/v1/memory/promotion-proposals?page_size={page_size}"
                if cursor is not None:
                    url += f"&cursor={cursor}"
                resp = await client.get(url, headers=bearer_headers(tenant_slug=persona.slug))
                assert resp.status_code == 200, resp.text
                body = resp.json()
                pages.append(len(body["items"]))
                seen.extend(item["proposal_id"] for item in body["items"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break

    assert sorted(seen) == sorted(proposal_ids)
    assert len(seen) == len(set(seen))
    # `page_size` is a page cap, not a hint: a route that ignored it and
    # returned all three rows on page one with a null cursor would satisfy
    # both assertions above too, so pagination itself has to be pinned
    # directly -- at least two pages, and none larger than `page_size`.
    assert len(pages) >= 2, f"expected at least two pages at page_size={page_size}, got {pages}"
    assert all(n <= page_size for n in pages), f"a page exceeded page_size={page_size}: {pages}"


# ---------------------------------------------------------------------------
# GET /v1/memory/promotion-proposals/{id} -- fetch one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_get_returns_the_full_proposal_view(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"prop-get-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    proposal = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value="platform")

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.get(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proposal_id"] == str(proposal.proposal_id)
    assert body["subject_entity_id"] == str(subject)
    assert body["predicate"] == "owned_by_team"
    assert body["proposed_value"] == "platform"
    assert body["target_kind"] == "attribute"
    assert body["state"] == "open"


@pytest.mark.asyncio(loop_scope="module")
async def test_get_on_a_foreign_tenants_proposal_is_the_same_404_as_missing(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    owner = harness.add_persona(f"prop-owner-{uuid.uuid4().hex[:8]}")
    stranger = harness.add_persona(f"prop-stranger-{uuid.uuid4().hex[:8]}")
    owner_tenant_id = await _materialise_persona(harness, owner)
    await _materialise_persona(harness, stranger)
    actor_id = await _seed_actor(pg_container, owner_tenant_id)
    subject = await _seed_entity(pg_container, owner_tenant_id)

    proposal = await _stage_and_propose(harness, pg_container, owner_tenant_id, actor_id, subject)

    async with _client(harness) as client:
        with patch_validator_for_actor(stranger):
            missing_resp = await client.get(
                f"/v1/memory/promotion-proposals/{uuid.uuid4()}",
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
            foreign_resp = await client.get(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
    assert missing_resp.status_code == 404
    assert foreign_resp.status_code == 404
    assert foreign_resp.json() == missing_resp.json()


# ---------------------------------------------------------------------------
# PATCH /v1/memory/promotion-proposals/{id} -- accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_accept_writes_the_canonical_row(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"prop-accept-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    proposal = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value="platform")

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.patch(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                json={"state": "accepted"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proposal"]["state"] == "accepted"
    assert body["promotion_id"] is not None

    # The claim/attribute read path -- not the echoed response body -- is
    # what proves the canonical write actually landed.
    rows = await _attributes(pg_container, subject, "owned_by_team")
    assert len(rows) == 1
    assert rows[0]["value"] == "platform"
    assert rows[0]["t_invalidated_at"] is None
    assert await _live_value(pg_container, subject, "owned_by_team") == "platform"


@pytest.mark.asyncio(loop_scope="module")
async def test_accept_with_an_amended_value_promotes_the_amendment(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"prop-amend-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    proposal = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value="platform")

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.patch(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                json={"state": "accepted", "amended_value": "billing"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["state"] == "amended"
    assert await _live_value(pg_container, subject, "owned_by_team") == "billing"


# ---------------------------------------------------------------------------
# PATCH /v1/memory/promotion-proposals/{id} -- reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_reject_with_reason_leaves_the_claim_staged(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"prop-reject-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    proposal = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value="platform")

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.patch(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                json={"state": "rejected", "reason": "incorrect"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proposal"]["state"] == "rejected"
    assert body["promotion_id"] is None

    # Rejected, but still staged and serving -- a promotion refusal is a
    # verdict on one proposed write, not on the claim itself.
    claim_row = await _claim_row(pg_container, proposal.claim_id)
    assert claim_row["status"] == "staged"
    assert claim_row["promotion_state"] == "rejected"
    assert await _live_value(pg_container, subject, "owned_by_team") is None


# ---------------------------------------------------------------------------
# POST /v1/memory/promotions/{id}:reverse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_reverse_restores_the_prior_canonical_value(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"prop-reverse-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    first = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value="platform")
    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            first_resp = await client.patch(
                f"/v1/memory/promotion-proposals/{first.proposal_id}",
                json={"state": "accepted"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert first_resp.status_code == 200, first_resp.text
    assert await _live_value(pg_container, subject, "owned_by_team") == "platform"

    second = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value="billing")
    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            second_resp = await client.patch(
                f"/v1/memory/promotion-proposals/{second.proposal_id}",
                json={"state": "accepted"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert second_resp.status_code == 200, second_resp.text
    second_promotion_id = second_resp.json()["promotion_id"]
    assert await _live_value(pg_container, subject, "owned_by_team") == "billing"

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            reverse_resp = await client.post(
                f"/v1/memory/promotions/{second_promotion_id}:reverse",
                json={"reason": "the second value was wrong"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert reverse_resp.status_code == 200, reverse_resp.text
    assert reverse_resp.json() == {"status": "reversed"}

    # The prior state, not merely the prior value -- the earlier row is
    # reopened rather than a fresh one written with the old value.
    assert await _live_value(pg_container, subject, "owned_by_team") == "platform"
    rows = await _attributes(pg_container, subject, "owned_by_team")
    assert len(rows) == 2
    live = [r for r in rows if r["t_invalidated_at"] is None]
    assert len(live) == 1
    assert live[0]["value"] == "platform"


@pytest.mark.asyncio(loop_scope="module")
async def test_reversing_an_already_reversed_promotion_is_409(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"prop-reverse-twice-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    proposal = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value="platform")
    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            accept_resp = await client.patch(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                json={"state": "accepted"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            promotion_id = accept_resp.json()["promotion_id"]
            first = await client.post(
                f"/v1/memory/promotions/{promotion_id}:reverse",
                json={"reason": "wrong"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert first.status_code == 200, first.text
            second = await client.post(
                f"/v1/memory/promotions/{promotion_id}:reverse",
                json={"reason": "again"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Authority: non-owner, double-decide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_review_from_a_foreign_tenant_is_403(harness: EntitlementAuthHarness, pg_container: str) -> None:
    owner = harness.add_persona(f"prop-review-owner-{uuid.uuid4().hex[:8]}")
    stranger = harness.add_persona(f"prop-review-stranger-{uuid.uuid4().hex[:8]}")
    owner_tenant_id = await _materialise_persona(harness, owner)
    await _materialise_persona(harness, stranger)
    actor_id = await _seed_actor(pg_container, owner_tenant_id)
    subject = await _seed_entity(pg_container, owner_tenant_id)

    proposal = await _stage_and_propose(harness, pg_container, owner_tenant_id, actor_id, subject)

    async with _client(harness) as client:
        with patch_validator_for_actor(stranger):
            resp = await client.patch(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                json={"state": "accepted"},
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
    assert resp.status_code == 403

    # Refused, and nothing was written.
    assert await _live_value(pg_container, subject, "owned_by_team") is None


@pytest.mark.asyncio(loop_scope="module")
async def test_deciding_an_already_decided_proposal_is_409(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"prop-double-{uuid.uuid4().hex[:8]}")
    tenant_id = await _materialise_persona(harness, persona)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject = await _seed_entity(pg_container, tenant_id)

    proposal = await _stage_and_propose(harness, pg_container, tenant_id, actor_id, subject, value="platform")

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            first = await client.patch(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                json={"state": "accepted"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
            assert first.status_code == 200, first.text

            second = await client.patch(
                f"/v1/memory/promotion-proposals/{proposal.proposal_id}",
                json={"state": "rejected", "reason": "incorrect"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert second.status_code == 409
