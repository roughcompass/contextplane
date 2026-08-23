"""The quarantine surface, over HTTP against a real Postgres. E4-T8.

Contract under test
----------------------------------------------------
The one thing this file exists to prove is **reachability**. E4-T2, E4-T3 and
E4-T4 built apply, preview, revert and receipt withholding, and
`grep -rn QuarantineService contextplane/` found the service in its own module
and nowhere else — no route, no tool, no entry in `wiring/`. Everything below
was already tested at the service level; none of it could be invoked.

So these tests drive the routes, not the service, and a route that is registered
but unwired fails here with a 404 or an `AttributeError` on the container rather
than passing on a service the test constructed for itself.

Three properties beyond reachability:

**Preview withholds nothing.** It is a separate route rather than a `dry_run`
flag precisely so a caller cannot withhold content by getting a boolean wrong,
and this asserts the claim still serves afterwards.

**A predicate matching nothing is refused.** A quarantine that withheld nothing
reads later as one that was tried and worked.

**The collaborators are actually wired.** A `QuarantineService` built without
them still applies and reverts — so a test that only checked apply/revert would
pass on a deployment whose receipts keep serving. This asserts the receipt the
claim was quoted in stops serving, which only happens if the root passed the
withholder through.

Uses a real Postgres container via the session-scoped ``pg_container`` fixture.
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)
from tests.helpers.builders import make_persona_new_client as _make_persona

_SEEDED = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_RUN = "run-42"


class _World:
    """An admin, a claim under a known connector run, and a receipt quoting it."""

    def __init__(self, client: AsyncClient, persona: object, tenant_id: uuid.UUID, pg_url: str) -> None:
        self.client = client
        self.persona = persona
        self.tenant_id = tenant_id
        self.pg_url = pg_url

    def headers(self) -> dict[str, str]:
        return bearer_headers(tenant_slug=self.persona.slug)  # type: ignore[attr-defined]

    async def claim(self, *, run: str = _RUN) -> uuid.UUID:
        claim_id, entity_id = uuid.uuid4(), uuid.uuid4()
        engine = create_async_engine(self.pg_url, connect_args={"prepared_statement_cache_size": 0})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session, session.begin():
                actor = (
                    await session.execute(
                        text("SELECT actor_id FROM actors WHERE tenant_id = :t LIMIT 1"),
                        {"t": self.tenant_id},
                    )
                ).scalar_one()
                await session.execute(
                    text(
                        "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                        " is_active, created_at) "
                        "VALUES (:e, :t, 'capability', :n, 'tenant-shared', TRUE, :now)"
                    ),
                    {"e": entity_id, "n": f"cap-{entity_id.hex[:8]}", "now": _SEEDED, "t": self.tenant_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_claims ("
                        "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                        "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                        "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                        "  consolidated_at, created_at, confidence, confidence_scored_at, confidence_inputs,"
                        "  scorer_version, calibration_version, decay_half_life_days, namespace, strategy_id"
                        ") VALUES ("
                        "  :cid, :t, :t, :a, :e, 'ref', 'owned_by_team', 'prose',"
                        "  'ownership_stewardship', CAST('\"platform\"' AS JSONB), :now, 'staged', 'private',"
                        "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST('{}' AS JSONB),"
                        "  'scorer.v1', 'calib.v1', 30, 'team/a', 'extract.v1')"
                    ),
                    {"a": actor, "cid": claim_id, "e": entity_id, "now": _SEEDED, "t": self.tenant_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) "
                        "VALUES (:cid, 'connector_run', :run)"
                    ),
                    {"cid": claim_id, "run": run},
                )
        finally:
            await engine.dispose()
        return claim_id

    async def receipt_quoting(self, claim_id: uuid.UUID) -> uuid.UUID:
        """A hydrated receipt whose `observed_claims` item names this claim.

        The shape `observed_claims_arm` writes: `item_key=str(claim.claim_id)`.
        """
        receipt_id = uuid.uuid4()
        engine = create_async_engine(self.pg_url, connect_args={"prepared_statement_cache_size": 0})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO context_receipts ("
                        "  receipt_id, tenant_id, state, cacheable, hydration_state, item_count,"
                        "  exclusion_count, resolved_at, requested_by"
                        ") VALUES (:r, :t, 'complete', TRUE, 'complete', 1, 0, :now, 'agent')"
                    ),
                    {"now": _SEEDED, "r": receipt_id, "t": self.tenant_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO context_receipt_items ("
                        "  item_row_id, receipt_id, receipt_item_id, block, source, item_key,"
                        "  trust, trust_source, assertion_kind, authority, freshness,"
                        "  mutability, attribution, classification"
                        ") VALUES (:i, :r, :rid, 'observed_claims', 'claims', :key,"
                        "  'observed', 'memory_claim', 'observed', 'observer_extraction', :now,"
                        "  'mutable', 'agent', 'internal')"
                    ),
                    {
                        "i": uuid.uuid4(),
                        "key": str(claim_id),
                        "now": _SEEDED,
                        "r": receipt_id,
                        "rid": f"item-{receipt_id.hex[:8]}",
                    },
                )
        finally:
            await engine.dispose()
        return receipt_id

    async def quarantined_at(self, claim_id: uuid.UUID) -> datetime.datetime | None:
        engine = create_async_engine(self.pg_url, connect_args={"prepared_statement_cache_size": 0})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                row = await session.execute(
                    text("SELECT quarantined_at FROM memory_claims WHERE claim_id = :c"),
                    {"c": claim_id},
                )
                return row.scalar_one()
        finally:
            await engine.dispose()


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[_World]:
    slug = f"quar-{secrets.token_hex(4)}"
    async with EntitlementAuthHarness(pg_container) as harness:
        persona = await _make_persona(harness, pg_container, slug=slug, roles=["admin"])
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
            factory = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with factory() as session:
                    tenant_id = (
                        await session.execute(text("SELECT tenant_id FROM tenants WHERE slug = :s"), {"s": slug})
                    ).scalar_one()
            finally:
                await engine.dispose()
            yield _World(client, persona, tenant_id, pg_container)


@pytest.mark.asyncio
async def test_the_quarantine_surface_is_reachable_at_all(world: _World) -> None:
    """The whole point of this task. Before it, the service existed and no
    request could reach it."""
    claim_id = await world.claim()

    with patch_validator_for_actor(world.persona):
        applied = await world.client.post(
            "/v1/admin/claim-quarantines",
            headers=world.headers(),
            json={"selector": "connector_run", "value": _RUN, "reason": "bad connector run"},
        )

    assert applied.status_code == 201, applied.text
    body = applied.json()
    assert body["matched"] == [str(claim_id)]
    assert body["matched_count"] == 1
    assert await world.quarantined_at(claim_id) is not None


@pytest.mark.asyncio
async def test_a_preview_reports_the_match_set_and_withholds_nothing(world: _World) -> None:
    """A separate route rather than a `dry_run` flag, so a caller cannot
    withhold content by getting a boolean wrong."""
    claim_id = await world.claim()

    with patch_validator_for_actor(world.persona):
        previewed = await world.client.post(
            "/v1/admin/claim-quarantines:preview",
            headers=world.headers(),
            json={"selector": "connector_run", "value": _RUN},
        )

    assert previewed.status_code == 200, previewed.text
    body = previewed.json()
    assert body["matched"] == [str(claim_id)]
    assert await world.quarantined_at(claim_id) is None, "a preview must withhold nothing"


@pytest.mark.asyncio
async def test_the_preview_says_whether_its_downstream_answer_is_complete(world: _World) -> None:
    """`truncated` is what tells a capped downstream set from the answer, and
    an untraversed subject from a subject with no dependants."""
    await world.claim()

    with patch_validator_for_actor(world.persona):
        previewed = await world.client.post(
            "/v1/admin/claim-quarantines:preview",
            headers=world.headers(),
            json={"selector": "connector_run", "value": _RUN},
        )

    body = previewed.json()
    assert body["seeds_total"] == 1
    assert "truncated" in body
    assert body["downstream"] == []


@pytest.mark.asyncio
async def test_a_predicate_matching_nothing_is_refused_rather_than_recorded(world: _World) -> None:
    """A quarantine that withheld nothing reads later as one that was tried and
    worked, and an incident review would take it as containment."""
    with patch_validator_for_actor(world.persona):
        applied = await world.client.post(
            "/v1/admin/claim-quarantines",
            headers=world.headers(),
            json={"selector": "connector_run", "value": "run-nothing", "reason": "nothing to hold"},
        )

    assert applied.status_code == 409, applied.text


@pytest.mark.asyncio
async def test_an_unknown_selector_is_refused_by_the_closed_vocabulary(world: _World) -> None:
    with patch_validator_for_actor(world.persona):
        applied = await world.client.post(
            "/v1/admin/claim-quarantines",
            headers=world.headers(),
            json={"selector": "confidence", "value": "0.5", "reason": "not a provenance statement"},
        )

    assert applied.status_code == 422, applied.text


@pytest.mark.asyncio
async def test_reverting_puts_the_claims_back_and_refuses_a_second_revert(world: _World) -> None:
    """A second revert is a 409 rather than a 200 with zero: "already reverted"
    and "nothing left to restore" are different facts about the incident."""
    claim_id = await world.claim()

    with patch_validator_for_actor(world.persona):
        applied = await world.client.post(
            "/v1/admin/claim-quarantines",
            headers=world.headers(),
            json={"selector": "connector_run", "value": _RUN, "reason": "bad connector run"},
        )
        quarantine_id = applied.json()["quarantine_id"]
        first = await world.client.post(f"/v1/admin/claim-quarantines/{quarantine_id}:revert", headers=world.headers())
        second = await world.client.post(f"/v1/admin/claim-quarantines/{quarantine_id}:revert", headers=world.headers())

    assert first.status_code == 200, first.text
    assert first.json()["restored_count"] == 1
    assert await world.quarantined_at(claim_id) is None
    assert second.status_code == 409, second.text


@pytest.mark.asyncio
async def test_applying_through_the_route_also_withholds_the_receipts_that_quoted_it(
    world: _World,
) -> None:
    """The test that distinguishes a wired quarantine from a half-configured one.

    A `QuarantineService` built without its collaborators still applies and
    reverts, so a test that checked only `quarantined_at` would pass on a
    deployment whose receipts keep serving the withheld content. This passes
    only if the composition root actually threaded the withholder through.
    """
    claim_id = await world.claim()
    receipt_id = await world.receipt_quoting(claim_id)

    with patch_validator_for_actor(world.persona):
        before = await world.client.get(f"/v1/receipts/{receipt_id}/exclusions", headers=world.headers())
        await world.client.post(
            "/v1/admin/claim-quarantines",
            headers=world.headers(),
            json={"selector": "connector_run", "value": _RUN, "reason": "bad connector run"},
        )
        after = await world.client.get(f"/v1/receipts/{receipt_id}/exclusions", headers=world.headers())

    assert before.status_code == 200, before.text
    assert after.status_code == 409, after.text
    assert after.json()["errors"][0]["code"] == "receipt_withheld"
