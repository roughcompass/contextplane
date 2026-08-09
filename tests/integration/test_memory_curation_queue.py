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

from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.memory.claim_authority import Evidence
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


# ---------------------------------------------------------------------------
# Contradiction groups and curation cases, against a real database
#
# Three things only Postgres can answer here: that grouping reads real contest
# rows written by real detection, that axis idempotency holds against a
# concurrent-looking second call, and that the CHECK constraints refuse the
# half-recorded decisions the service layer promises never to write.
# ---------------------------------------------------------------------------


async def _whoami(harness: EntitlementAuthHarness, persona: TenantPersona) -> tuple[uuid.UUID, uuid.UUID]:
    """The tenant *and* actor a persona resolves to.

    The actor matters for dispositions: `owner_id` is compared against the
    caller's own actor, so a test that routes a case to itself has to know which
    actor the credential resolves to rather than seeding an unrelated one.
    """
    harness.configure_fetcher_for(persona)
    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return uuid.UUID(body["tenant_id"]), uuid.UUID(body["actor_id"])


async def _stage_conflicting_pair(
    harness: EntitlementAuthHarness, pg_url: str, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Two claims that really disagree, detected by the real write path."""
    subject = await _seed_entity(pg_url, tenant_id)
    claims = harness.app.state.services.claims
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])
    first = await claims.stage_claim(
        ctx, subject_reference=str(subject), predicate="owned_by_team", value="platform", evidence=_EV
    )
    second = await claims.stage_claim(
        ctx, subject_reference=str(subject), predicate="owned_by_team", value="billing", evidence=_EV
    )
    assert second.is_contested, "detection did not record the disagreement this test depends on"
    return subject, first.claim_id


@pytest.mark.asyncio
async def test_a_real_contradiction_surfaces_as_one_group_with_both_members(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Two pairwise rows would be two questions; the axis is one."""
    persona = harness.add_persona(f"cgrp-{uuid.uuid4().hex[:8]}")
    tenant_id, actor_id = await _whoami(harness, persona)
    subject, _ = await _stage_conflicting_pair(harness, pg_container, tenant_id, actor_id)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/memory/contradiction-groups", headers=bearer_headers(tenant_slug=persona.slug))

    assert resp.status_code == 200, resp.text
    groups = resp.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["subject_entity_id"] == str(subject)
    assert groups[0]["predicate"] == "owned_by_team"
    assert groups[0]["member_count"] == 2
    assert len(groups[0]["contest_ids"]) == 1


@pytest.mark.asyncio
async def test_another_tenants_contradiction_is_invisible(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """Both sides of a pair are tenant-scoped, so a neighbouring tenant's
    disagreement contributes no group and leaks no claim id."""
    owner = harness.add_persona(f"cgrp-own-{uuid.uuid4().hex[:8]}")
    other = harness.add_persona(f"cgrp-oth-{uuid.uuid4().hex[:8]}")
    owner_tenant, owner_actor = await _whoami(harness, owner)
    await _stage_conflicting_pair(harness, pg_container, owner_tenant, owner_actor)
    await _whoami(harness, other)

    harness.configure_fetcher_for(other)
    async with _client(harness) as client:
        with patch_validator_for_actor(other):
            resp = await client.get("/v1/memory/contradiction-groups", headers=bearer_headers(tenant_slug=other.slug))

    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == []


@pytest.mark.asyncio
async def test_opening_a_case_twice_on_one_axis_yields_one_case(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Re-detection is the normal path. A second row would let two owners decide
    one disagreement differently."""
    persona = harness.add_persona(f"ccase-idem-{uuid.uuid4().hex[:8]}")
    await _whoami(harness, persona)
    body = {"subject_reference": "svc:payments", "predicate": "owned_by_team"}

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            first = await client.post(
                "/v1/memory/curation-cases", json=body, headers=bearer_headers(tenant_slug=persona.slug)
            )
            second = await client.post(
                "/v1/memory/curation-cases", json=body, headers=bearer_headers(tenant_slug=persona.slug)
            )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["case_id"] == second.json()["case_id"]


@pytest.mark.asyncio
async def test_the_case_lifecycle_records_the_authority_its_disposition_commits_to(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Open, route, decide -- and the stored row names the approver and the
    evidence that approver needs, written at disposition time."""
    persona = harness.add_persona(f"ccase-life-{uuid.uuid4().hex[:8]}")
    _tenant_id, actor_id = await _whoami(harness, persona)
    headers = bearer_headers(tenant_slug=persona.slug)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            opened = await client.post(
                "/v1/memory/curation-cases",
                json={"subject_reference": "svc:payments", "predicate": "owned_by_team"},
                headers=headers,
            )
            assert opened.status_code == 201, opened.text
            case_id = opened.json()["case_id"]
            assert opened.json()["approval_authority"] is None

            routed = await client.post(
                f"/v1/memory/curation-cases/{case_id}:route",
                json={"owner_id": str(actor_id)},
                headers=headers,
            )
            assert routed.status_code == 200, routed.text
            assert routed.json()["status"] == "routed"

            decided = await client.post(
                f"/v1/memory/curation-cases/{case_id}:disposition",
                json={"disposition": "propose_arc"},
                headers=headers,
            )
            assert decided.status_code == 200, decided.text

            fetched = await client.get(f"/v1/memory/curation-cases/{case_id}", headers=headers)

    body = decided.json()
    assert body["status"] == "resolved"
    assert body["disposition"] == "propose_arc"
    assert body["approval_authority"] == "arc_approver"
    assert body["target_kind"] == "arc_artifact"
    assert body["resolved_at"] is not None
    # The read-back proves it was persisted, not just returned.
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == body


@pytest.mark.asyncio
async def test_a_disposition_from_someone_other_than_the_owner_is_refused(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """The check that makes routing mean anything. 403, not 404: the case is
    readable, and the refusal is about authority to settle it."""
    persona = harness.add_persona(f"ccase-auth-{uuid.uuid4().hex[:8]}")
    await _whoami(harness, persona)
    headers = bearer_headers(tenant_slug=persona.slug)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            opened = await client.post(
                "/v1/memory/curation-cases",
                json={"subject_reference": "svc:billing", "predicate": "owned_by_team"},
                headers=headers,
            )
            case_id = opened.json()["case_id"]
            await client.post(
                f"/v1/memory/curation-cases/{case_id}:route",
                json={"owner_id": "somebody-else-entirely"},
                headers=headers,
            )
            refused = await client.post(
                f"/v1/memory/curation-cases/{case_id}:disposition",
                json={"disposition": "confirm"},
                headers=headers,
            )
            still_open = await client.get(f"/v1/memory/curation-cases/{case_id}", headers=headers)

    assert refused.status_code == 403, refused.text
    assert still_open.json()["status"] == "routed", "a refused disposition must not resolve the case"
    assert still_open.json()["disposition"] is None


@pytest.mark.asyncio
async def test_another_tenants_case_answers_as_missing(harness: EntitlementAuthHarness, pg_container: str) -> None:
    owner = harness.add_persona(f"ccase-own-{uuid.uuid4().hex[:8]}")
    other = harness.add_persona(f"ccase-oth-{uuid.uuid4().hex[:8]}")
    await _whoami(harness, owner)
    async with _client(harness) as client:
        with patch_validator_for_actor(owner):
            opened = await client.post(
                "/v1/memory/curation-cases",
                json={"subject_reference": "svc:payments", "predicate": "owned_by_team"},
                headers=bearer_headers(tenant_slug=owner.slug),
            )
    case_id = opened.json()["case_id"]

    await _whoami(harness, other)
    harness.configure_fetcher_for(other)
    async with _client(harness) as client:
        with patch_validator_for_actor(other):
            resp = await client.get(
                f"/v1/memory/curation-cases/{case_id}", headers=bearer_headers(tenant_slug=other.slug)
            )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_the_database_refuses_a_resolved_case_with_no_disposition(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """The service never writes this row, and the schema is the reason it cannot
    be written by anything else either -- a case that left the queue without
    saying what was decided is unreviewable afterwards."""
    persona = harness.add_persona(f"ccase-ck-{uuid.uuid4().hex[:8]}")
    tenant_id, _actor_id = await _whoami(harness, persona)

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        with pytest.raises(Exception, match="ck_case_resolved_has_a_disposition"):
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO curation_cases "
                        "  (case_id, tenant_id, subject_reference, predicate, status, created_at) "
                        "VALUES (:cid, :tid, 'svc:payments', 'owned_by_team', 'resolved', :now)"
                    ),
                    {"cid": uuid.uuid4(), "tid": tenant_id, "now": _NOW},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_database_refuses_a_routed_case_with_no_owner(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """Routed means routed to somebody. A contradiction that reaches nobody is a
    contradiction that stays."""
    persona = harness.add_persona(f"ccase-ck2-{uuid.uuid4().hex[:8]}")
    tenant_id, _actor_id = await _whoami(harness, persona)

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        with pytest.raises(Exception, match="ck_case_routed_has_an_owner"):
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO curation_cases "
                        "  (case_id, tenant_id, subject_reference, predicate, status, created_at) "
                        "VALUES (:cid, :tid, 'svc:payments', 'owned_by_team', 'routed', :now)"
                    ),
                    {"cid": uuid.uuid4(), "tid": tenant_id, "now": _NOW},
                )
    finally:
        await engine.dispose()
