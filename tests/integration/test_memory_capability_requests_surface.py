"""Integration tests for the capability-requests REST surface: raise, list,
fetch, history, transition, and link-to-promotion, over a live FastAPI app +
Postgres.

`CapabilityRequestService` is fully built and integration-tested already
(`tests/integration/test_capability_requests.py`) against direct service
calls; these tests exercise the same lifecycle over HTTP, plus the one thing
only the router can get wrong: `raise_request`'s own subject lookup is a
bare existence check, not a visibility filter, so the route wraps it in the
visibility chokepoint before calling the service.

- POST /v1/memory/capability-requests               → 201, routed to the
  subject's owning tenant regardless of who asked
- GET  /v1/memory/capability-requests?role=owner    → the owner's queue
- GET  /v1/memory/capability-requests?role=requester → the requester's
  outbound history
- GET  /v1/memory/capability-requests/{id}          → either party's view
- GET  /v1/memory/capability-requests/{id}/history   → transitions in order
- PATCH /v1/memory/capability-requests/{id}          → acknowledge, accept,
  decline (with reason)
- POST /v1/memory/capability-requests/{id}:link-promotion → closes the loop
- NAMED test (the oracle-parity test this task exists to pin): raising a
  request against a subject the caller cannot see returns the identical
  error -- status and body -- as raising one against a subject that does not
  exist at all. Without the router's own chokepoint wrap, an invisible
  subject and a missing one would be told apart by whether the request
  landed, which is a cross-tenant existence oracle.
- Cursor round-trip on the owner queue: every page advances, no duplicates,
  the union matches everything raised.
- Illegal transition (raised → accepted, skipping acknowledgement) → 409
- A decline with no reason → 422
- A non-owner tenant attempting to transition a request → 403
- `:link-promotion` before a request is accepted → 409 ("cannot point at a
  change")
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

    Returns `(tenant_id, actor_id)`.
    """
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
            assert resp.status_code == 200, resp.text
    body = resp.json()
    return uuid.UUID(body["tenant_id"]), uuid.UUID(body["actor_id"])


async def _seed_accepted_promotion(
    harness: EntitlementAuthHarness,
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    subject: uuid.UUID,
) -> uuid.UUID:
    """A promotion journal row, so `:link-promotion` has a real target.

    Staging goes through the write path (a staged claim has invariants a
    hand-rolled row would skip); the proposal and journal rows are inserted
    directly, matching `tests/integration/test_capability_requests.py`'s own
    `_seed_promotion` -- driving the whole promotion path here would make
    these tests fail for reasons that have nothing to do with requests.
    """
    services = harness.app.state.services
    claim = await services.claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    proposal_id, promotion_id = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_promotion_proposal (proposal_id, claim_id, owner_tenant_id, "
                    "  author_tenant_id, subject_entity_id, predicate, target_kind, target_key, "
                    "  mapping_version, proposed_value, valid_from, state, decided_by, "
                    "  decided_at, created_at) "
                    "VALUES (:pid, :cid, :tid, :tid, :sid, 'owned_by_team', 'attribute', "
                    "        'owned_by_team', 1, '\"platform\"'::jsonb, :now, 'accepted', "
                    "        :aid, :now, :now)"
                ),
                {
                    "pid": proposal_id,
                    "cid": claim.claim_id,
                    "tid": tenant_id,
                    "sid": subject,
                    "aid": actor_id,
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_promotion_journal (promotion_id, proposal_id, claim_id, "
                    "  tenant_id, target_kind, created_row_id, promoted_at, promoted_by) "
                    "VALUES (:prid, :pid, :cid, :tid, 'attribute', :row, :now, :aid)"
                ),
                {
                    "prid": promotion_id,
                    "pid": proposal_id,
                    "cid": claim.claim_id,
                    "tid": tenant_id,
                    "row": uuid.uuid4(),
                    "now": _NOW,
                    "aid": actor_id,
                },
            )
    finally:
        await engine.dispose()
    return promotion_id


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    await _seed_ontology(pg_container)
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


def _client(harness: EntitlementAuthHarness) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test")


# ---------------------------------------------------------------------------
# Full lifecycle over HTTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_a_request_lifecycle_over_http(harness: EntitlementAuthHarness, pg_container: str) -> None:
    owner = harness.add_persona(f"req-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, owner_actor = await _materialise_persona(harness, owner)
    consumer = harness.add_persona(f"req-consumer-{uuid.uuid4().hex[:8]}")
    consumer_tenant, _consumer_actor = await _materialise_persona(harness, consumer)

    subject = await _seed_entity(pg_container, owner_tenant, visibility="public")

    async with _client(harness) as client:
        with patch_validator_for_actor(consumer):
            raise_resp = await client.post(
                "/v1/memory/capability-requests",
                json={
                    "subject_entity_id": str(subject),
                    "request_category": "interface_change",
                    "title": "needs an idempotency key",
                    "body": "retries double-charge without one",
                },
                headers=bearer_headers(tenant_slug=consumer.slug),
            )
        assert raise_resp.status_code == 201, raise_resp.text
        created = raise_resp.json()
        request_id = created["request_id"]
        assert created["owner_tenant_id"] == str(owner_tenant)
        assert created["requester_tenant_id"] == str(consumer_tenant)
        assert created["status"] == "raised"

        # The owner's queue shows it.
        with patch_validator_for_actor(owner):
            owner_queue_resp = await client.get(
                "/v1/memory/capability-requests", headers=bearer_headers(tenant_slug=owner.slug)
            )
        assert owner_queue_resp.status_code == 200, owner_queue_resp.text
        assert request_id in [i["request_id"] for i in owner_queue_resp.json()["items"]]

        # The requester's outbound history shows it too.
        with patch_validator_for_actor(consumer):
            consumer_history_resp = await client.get(
                "/v1/memory/capability-requests?role=requester",
                headers=bearer_headers(tenant_slug=consumer.slug),
            )
        assert consumer_history_resp.status_code == 200, consumer_history_resp.text
        assert request_id in [i["request_id"] for i in consumer_history_resp.json()["items"]]

        # Either party can fetch it directly.
        with patch_validator_for_actor(consumer):
            get_resp = await client.get(
                f"/v1/memory/capability-requests/{request_id}",
                headers=bearer_headers(tenant_slug=consumer.slug),
            )
        assert get_resp.status_code == 200, get_resp.text

        # Owner acknowledges, then accepts.
        with patch_validator_for_actor(owner):
            ack_resp = await client.patch(
                f"/v1/memory/capability-requests/{request_id}",
                json={"to_status": "acknowledged"},
                headers=bearer_headers(tenant_slug=owner.slug),
            )
            assert ack_resp.status_code == 200, ack_resp.text
            assert ack_resp.json()["status"] == "acknowledged"

            accept_resp = await client.patch(
                f"/v1/memory/capability-requests/{request_id}",
                json={"to_status": "accepted"},
                headers=bearer_headers(tenant_slug=owner.slug),
            )
            assert accept_resp.status_code == 200, accept_resp.text
            assert accept_resp.json()["status"] == "accepted"

        # History shows both transitions, oldest first.
        with patch_validator_for_actor(consumer):
            history_resp = await client.get(
                f"/v1/memory/capability-requests/{request_id}/history",
                headers=bearer_headers(tenant_slug=consumer.slug),
            )
        assert history_resp.status_code == 200, history_resp.text
        transitions = history_resp.json()["items"]
        assert [(t["from_status"], t["to_status"]) for t in transitions] == [
            ("raised", "acknowledged"),
            ("acknowledged", "accepted"),
        ]

        # The owner links it to the change it produced.
        promotion_id = await _seed_accepted_promotion(
            harness, pg_container, tenant_id=owner_tenant, actor_id=owner_actor, subject=subject
        )
        with patch_validator_for_actor(owner):
            link_resp = await client.post(
                f"/v1/memory/capability-requests/{request_id}:link-promotion",
                json={"promotion_id": str(promotion_id)},
                headers=bearer_headers(tenant_slug=owner.slug),
            )
        assert link_resp.status_code == 200, link_resp.text
        assert link_resp.json() == {"status": "linked"}

        # The requester sees the loop closed.
        with patch_validator_for_actor(consumer):
            final_resp = await client.get(
                f"/v1/memory/capability-requests/{request_id}",
                headers=bearer_headers(tenant_slug=consumer.slug),
            )
        assert final_resp.status_code == 200, final_resp.text
        assert final_resp.json()["resulting_promotion_id"] == str(promotion_id)


# ---------------------------------------------------------------------------
# NAMED test: invisible subject == missing subject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_raising_against_an_invisible_subject_is_the_same_error_as_missing(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    """The oracle-parity test this task exists to pin: without the router's
    own chokepoint wrap, `raise_request`'s bare existence check would let a
    caller tell a private entity apart from one that does not exist just by
    whether their request landed. Both must answer identically -- same
    status, same body."""
    owner = harness.add_persona(f"req-oracle-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, _owner_actor = await _materialise_persona(harness, owner)
    stranger = harness.add_persona(f"req-oracle-stranger-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, stranger)

    private_subject = await _seed_entity(pg_container, owner_tenant, visibility="private")
    missing_subject = uuid.uuid4()

    async with _client(harness) as client:
        with patch_validator_for_actor(stranger):
            invisible_resp = await client.post(
                "/v1/memory/capability-requests",
                json={
                    "subject_entity_id": str(private_subject),
                    "request_category": "defect",
                    "title": "t",
                    "body": "b",
                },
                headers=bearer_headers(tenant_slug=stranger.slug),
            )
            missing_resp = await client.post(
                "/v1/memory/capability-requests",
                json={
                    "subject_entity_id": str(missing_subject),
                    "request_category": "defect",
                    "title": "t",
                    "body": "b",
                },
                headers=bearer_headers(tenant_slug=stranger.slug),
            )

    assert invisible_resp.status_code == 404, invisible_resp.text
    assert missing_resp.status_code == 404, missing_resp.text
    assert invisible_resp.json() == missing_resp.json()


# ---------------------------------------------------------------------------
# Cursor round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_owner_queue_pages_through_without_duplicates(harness: EntitlementAuthHarness, pg_container: str) -> None:
    owner = harness.add_persona(f"req-cursor-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, _owner_actor = await _materialise_persona(harness, owner)
    consumer = harness.add_persona(f"req-cursor-consumer-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, consumer)

    subject = await _seed_entity(pg_container, owner_tenant, visibility="public")

    raised_ids: list[str] = []
    async with _client(harness) as client:
        with patch_validator_for_actor(consumer):
            for i in range(3):
                resp = await client.post(
                    "/v1/memory/capability-requests",
                    json={
                        "subject_entity_id": str(subject),
                        "request_category": "operational",
                        "title": f"request {i}",
                        "body": "body",
                    },
                    headers=bearer_headers(tenant_slug=consumer.slug),
                )
                assert resp.status_code == 201, resp.text
                raised_ids.append(resp.json()["request_id"])

        page_size = 2
        seen: list[str] = []
        pages: list[int] = []
        cursor: str | None = None
        with patch_validator_for_actor(owner):
            for _ in range(5):  # bounded loop; 3 items at page_size=2 needs 2 pages
                url = f"/v1/memory/capability-requests?page_size={page_size}"
                if cursor is not None:
                    url += f"&cursor={cursor}"
                resp = await client.get(url, headers=bearer_headers(tenant_slug=owner.slug))
                assert resp.status_code == 200, resp.text
                body = resp.json()
                pages.append(len(body["items"]))
                seen.extend(item["request_id"] for item in body["items"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break

    assert sorted(seen) == sorted(raised_ids)
    assert len(seen) == len(set(seen)), "no duplicate rows across pages"
    # `page_size` bounds each page; ignoring it entirely and returning all
    # three rows on page one with a null cursor would still pass the two
    # assertions above, so pagination itself is pinned directly here.
    assert len(pages) >= 2, f"expected at least two pages at page_size={page_size}, got {pages}"
    assert all(n <= page_size for n in pages), f"a page exceeded page_size={page_size}: {pages}"


# ---------------------------------------------------------------------------
# Lifecycle refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_skipping_straight_to_accepted_is_409(harness: EntitlementAuthHarness, pg_container: str) -> None:
    owner = harness.add_persona(f"req-skip-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, _owner_actor = await _materialise_persona(harness, owner)
    subject = await _seed_entity(pg_container, owner_tenant, visibility="public")

    async with _client(harness) as client:
        with patch_validator_for_actor(owner):
            raise_resp = await client.post(
                "/v1/memory/capability-requests",
                json={
                    "subject_entity_id": str(subject),
                    "request_category": "interface_change",
                    "title": "add a cursor",
                    "body": "offset paging is unstable",
                },
                headers=bearer_headers(tenant_slug=owner.slug),
            )
            assert raise_resp.status_code == 201, raise_resp.text
            request_id = raise_resp.json()["request_id"]

            resp = await client.patch(
                f"/v1/memory/capability-requests/{request_id}",
                json={"to_status": "accepted"},
                headers=bearer_headers(tenant_slug=owner.slug),
            )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio(loop_scope="module")
async def test_declining_without_a_reason_is_422(harness: EntitlementAuthHarness, pg_container: str) -> None:
    owner = harness.add_persona(f"req-noreason-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, _owner_actor = await _materialise_persona(harness, owner)
    subject = await _seed_entity(pg_container, owner_tenant, visibility="public")

    async with _client(harness) as client:
        with patch_validator_for_actor(owner):
            raise_resp = await client.post(
                "/v1/memory/capability-requests",
                json={
                    "subject_entity_id": str(subject),
                    "request_category": "defect",
                    "title": "timeouts under load",
                    "body": "p99 exceeds the documented budget",
                },
                headers=bearer_headers(tenant_slug=owner.slug),
            )
            assert raise_resp.status_code == 201, raise_resp.text
            request_id = raise_resp.json()["request_id"]

            resp = await client.patch(
                f"/v1/memory/capability-requests/{request_id}",
                json={"to_status": "declined"},
                headers=bearer_headers(tenant_slug=owner.slug),
            )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio(loop_scope="module")
async def test_a_non_owner_tenant_cannot_transition_a_request(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    owner = harness.add_persona(f"req-nonowner-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, _owner_actor = await _materialise_persona(harness, owner)
    consumer = harness.add_persona(f"req-nonowner-consumer-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, consumer)
    subject = await _seed_entity(pg_container, owner_tenant, visibility="public")

    async with _client(harness) as client:
        with patch_validator_for_actor(consumer):
            raise_resp = await client.post(
                "/v1/memory/capability-requests",
                json={
                    "subject_entity_id": str(subject),
                    "request_category": "interface_change",
                    "title": "needs a batch variant",
                    "body": "one call per row does not scale",
                },
                headers=bearer_headers(tenant_slug=consumer.slug),
            )
            assert raise_resp.status_code == 201, raise_resp.text
            request_id = raise_resp.json()["request_id"]

            # The requester tries to decide their own request.
            resp = await client.patch(
                f"/v1/memory/capability-requests/{request_id}",
                json={"to_status": "accepted"},
                headers=bearer_headers(tenant_slug=consumer.slug),
            )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Link to promotion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_a_request_not_yet_accepted_cannot_point_at_a_change(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    owner = harness.add_persona(f"req-linkbefore-owner-{uuid.uuid4().hex[:8]}")
    owner_tenant, owner_actor = await _materialise_persona(harness, owner)
    subject = await _seed_entity(pg_container, owner_tenant, visibility="public")

    async with _client(harness) as client:
        with patch_validator_for_actor(owner):
            raise_resp = await client.post(
                "/v1/memory/capability-requests",
                json={
                    "subject_entity_id": str(subject),
                    "request_category": "defect",
                    "title": "fix the 500",
                    "body": "empty body returns 500 not 400",
                },
                headers=bearer_headers(tenant_slug=owner.slug),
            )
            assert raise_resp.status_code == 201, raise_resp.text
            request_id = raise_resp.json()["request_id"]

            promotion_id = await _seed_accepted_promotion(
                harness, pg_container, tenant_id=owner_tenant, actor_id=owner_actor, subject=subject
            )

            # Still `raised` -- neither accepted nor resolved.
            resp = await client.post(
                f"/v1/memory/capability-requests/{request_id}:link-promotion",
                json={"promotion_id": str(promotion_id)},
                headers=bearer_headers(tenant_slug=owner.slug),
            )
    assert resp.status_code == 409, resp.text
