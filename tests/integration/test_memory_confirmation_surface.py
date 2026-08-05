"""Integration tests for the confirmation REST surface: `:confirm` and
`:adjudicate` over a live FastAPI app + Postgres.

`ConfirmationService.confirm`/`adjudicate` already exist and are already
integration-tested against direct calls (`tests/integration/test_claim_confirmation.py`).
This file exercises the same behavior through HTTP, so it asserts on
observable effects (the row a confirmation supersedes, the adjudication row
a verdict writes), not just the response status:

- POST /v1/memory/claims/{id}:confirm       → raises the score to the
  confirmed bucket, marks the original claim superseded, and audits both
  the confirmation and the supersession as separate rows
- POST ...:confirm on an unlinked claim     → 409 (link it first)
- POST ...:confirm as a non-human actor    → 403 (the human tier is not
  reachable by asserting a role -- it comes from the actor's own kind)
- POST ...:confirm on a missing claim      → 404
- POST /v1/memory/claims/{id}:adjudicate    → records the verdict, the row
  a calibration fit would later read, and an audit row carrying the verdict
  and whether a note was left (not the note text itself)
- POST ...:adjudicate on a missing claim   → 404
- POST ...:adjudicate with an unknown verdict / out-of-range confidence
  → 422 from the view model, before the service (and the adjudication
  table) is ever touched
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

from registry.audit import actions
from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.memory.claim_authority import Evidence
from registry.service.memory.claim_ontology import seed_ontology
from registry.service.memory.confidence import BUCKET_CONFIRMED, bucket_for
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


async def _seed_actor(pg_url: str, tenant_id: uuid.UUID, *, kind: str = "sync_worker") -> uuid.UUID:
    actor_id = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                    "                    actor_kind, created_at) "
                    "VALUES (:aid, :tid, 'seed', :sub, :kind, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "sub": f"seed-{actor_id.hex[:8]}", "kind": kind, "now": _NOW},
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


async def _claim_row(pg_url: str, claim_id: uuid.UUID) -> dict[str, object]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT confidence, source_authority, confidence_hold_until, "
                        "       confirms_claim_id, superseded_by, is_contested "
                        "FROM memory_claims WHERE claim_id = :cid"
                    ),
                    {"cid": claim_id},
                )
            ).one()
        return dict(row._mapping)
    finally:
        await engine.dispose()


async def _adjudication_rows(pg_url: str, claim_id: uuid.UUID) -> list[dict[str, object]]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT verdict, observed_confidence, observed_bucket, "
                        "       calibration_version, note "
                        "FROM memory_claim_adjudication WHERE claim_id = :cid"
                    ),
                    {"cid": claim_id},
                )
            ).all()
        return [dict(r._mapping) for r in rows]
    finally:
        await engine.dispose()


async def _audit_rows(pg_url: str, claim_id: uuid.UUID) -> list[dict[str, object]]:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (
                await session.execute(
                    text("SELECT action, actor_id, after_jsonb FROM audit_log " "WHERE target_id = :cid ORDER BY ts"),
                    {"cid": claim_id},
                )
            ).all()
        return [dict(r._mapping) for r in rows]
    finally:
        await engine.dispose()


async def _set_actor_kind(pg_url: str, actor_id: uuid.UUID, kind: str) -> None:
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE actors SET actor_kind = :kind WHERE actor_id = :aid"),
                {"kind": kind, "aid": actor_id},
            )
    finally:
        await engine.dispose()


async def _materialise_persona(harness: EntitlementAuthHarness, persona: TenantPersona) -> tuple[uuid.UUID, uuid.UUID]:
    """JIT-materialise *persona*'s tenant + actor row via `/v1/whoami`.

    Returns `(tenant_id, actor_id)`. The actor row this creates gets the
    schema default `actor_kind='human'` -- exactly the identity `:confirm`
    needs to succeed as a curator, and the one the 403 test flips away
    from afterwards.
    """
    harness.configure_fetcher_for(persona)
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch_validator_for_actor(persona):
            resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=persona.slug))
            assert resp.status_code == 200, resp.text
    body = resp.json()
    return uuid.UUID(body["tenant_id"]), uuid.UUID(body["actor_id"])


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    await _seed_ontology(pg_container)
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


def _client(harness: EntitlementAuthHarness) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=harness.app), base_url="http://test")


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_raises_the_score_and_marks_the_original_superseded(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"cnf-{uuid.uuid4().hex[:8]}")
    tenant_id, _human_actor_id = await _materialise_persona(harness, persona)
    machine_actor_id = await _seed_actor(pg_container, tenant_id, kind="sync_worker")
    subject = await _seed_entity(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    original = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=machine_actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{original.claim_id}:confirm",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirms_claim_id"] == str(original.claim_id)
    assert body["source_authority"] == "owner_human"
    assert body["bucket"] == BUCKET_CONFIRMED

    new_claim_id = uuid.UUID(body["claim_id"])
    new_row = await _claim_row(pg_container, new_claim_id)
    assert bucket_for(float(new_row["confidence"])) == BUCKET_CONFIRMED  # type: ignore[arg-type]
    assert new_row["confidence_hold_until"] is not None

    original_row = await _claim_row(pg_container, original.claim_id)
    assert original_row["superseded_by"] == new_claim_id

    confirmed_audit = await _audit_rows(pg_container, new_claim_id)
    assert [r["action"] for r in confirmed_audit] == [actions.CLAIM_CONFIRMED]
    assert confirmed_audit[0]["after_jsonb"]["confirms_claim_id"] == str(original.claim_id)
    assert confirmed_audit[0]["after_jsonb"]["source_authority"] == "owner_human"

    superseded_audit = await _audit_rows(pg_container, original.claim_id)
    assert [r["action"] for r in superseded_audit] == [actions.CLAIM_SUPERSEDED]
    assert superseded_audit[0]["after_jsonb"] == {"superseded_by": str(new_claim_id)}


@pytest.mark.asyncio
async def test_confirm_on_an_unlinked_claim_is_409(harness: EntitlementAuthHarness, pg_container: str) -> None:
    persona = harness.add_persona(f"cnf-unlinked-{uuid.uuid4().hex[:8]}")
    tenant_id, _actor_id = await _materialise_persona(harness, persona)
    machine_actor_id = await _seed_actor(pg_container, tenant_id, kind="sync_worker")

    claims = harness.app.state.services.claims
    unlinked = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=machine_actor_id, roles=["producer"]),
        subject_reference="github:acme/mystery",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{unlinked.claim_id}:confirm",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 409, resp.text
    assert await _audit_rows(pg_container, unlinked.claim_id) == []


@pytest.mark.asyncio
async def test_confirm_by_a_non_human_actor_is_403(harness: EntitlementAuthHarness, pg_container: str) -> None:
    """The human tier is not a role a caller can assert -- it comes from the
    authenticated actor's own `actor_kind`. Flipping the caller's own JIT
    actor row to a service kind (as if a worker somehow held that
    credential) must refuse, not silently mint human-tier authority."""
    persona = harness.add_persona(f"cnf-nonhuman-{uuid.uuid4().hex[:8]}")
    tenant_id, actor_id = await _materialise_persona(harness, persona)
    await _set_actor_kind(pg_container, actor_id, "sync_worker")
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
                f"/v1/memory/claims/{staged.claim_id}:confirm",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 403, resp.text
    assert await _audit_rows(pg_container, staged.claim_id) == []


@pytest.mark.asyncio
async def test_confirm_on_a_missing_claim_is_404(harness: EntitlementAuthHarness) -> None:
    persona = harness.add_persona(f"cnf-404-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, persona)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{uuid.uuid4()}:confirm",
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# adjudicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adjudicate_records_the_verdict_and_feeds_calibration_observations(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"adj-{uuid.uuid4().hex[:8]}")
    tenant_id, actor_id = await _materialise_persona(harness, persona)
    subject = await _seed_entity(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    claim = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{claim.claim_id}:adjudicate",
                json={"verdict": "correct", "observed_confidence": 0.42, "note": "matches what I found"},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "recorded"}

    rows = await _adjudication_rows(pg_container, claim.claim_id)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "correct"
    assert float(rows[0]["observed_confidence"]) == pytest.approx(0.42)
    assert rows[0]["observed_bucket"] == bucket_for(0.42)
    assert rows[0]["note"] == "matches what I found"

    audit_rows = await _audit_rows(pg_container, claim.claim_id)
    assert [r["action"] for r in audit_rows] == [actions.CLAIM_ADJUDICATED]
    payload = audit_rows[0]["after_jsonb"]
    assert payload["verdict"] == "correct"
    assert payload["observed_confidence"] == pytest.approx(0.42)
    assert payload["note_present"] is True
    assert "matches what I found" not in str(payload)


@pytest.mark.asyncio
async def test_adjudicate_on_a_missing_claim_is_404(harness: EntitlementAuthHarness) -> None:
    persona = harness.add_persona(f"adj-404-{uuid.uuid4().hex[:8]}")
    await _materialise_persona(harness, persona)

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{uuid.uuid4()}:adjudicate",
                json={"verdict": "correct", "observed_confidence": 0.5},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_adjudicate_with_an_unknown_verdict_is_422_and_writes_nothing(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"adj-badverdict-{uuid.uuid4().hex[:8]}")
    tenant_id, actor_id = await _materialise_persona(harness, persona)
    subject = await _seed_entity(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    claim = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{claim.claim_id}:adjudicate",
                json={"verdict": "probably", "observed_confidence": 0.5},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 422, resp.text
    assert await _adjudication_rows(pg_container, claim.claim_id) == []


@pytest.mark.asyncio
async def test_adjudicate_with_an_out_of_range_confidence_is_422_and_writes_nothing(
    harness: EntitlementAuthHarness, pg_container: str
) -> None:
    persona = harness.add_persona(f"adj-badconf-{uuid.uuid4().hex[:8]}")
    tenant_id, actor_id = await _materialise_persona(harness, persona)
    subject = await _seed_entity(pg_container, tenant_id)

    claims = harness.app.state.services.claims
    claim = await claims.stage_claim(
        TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"]),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    async with _client(harness) as client:
        with patch_validator_for_actor(persona):
            resp = await client.post(
                f"/v1/memory/claims/{claim.claim_id}:adjudicate",
                json={"verdict": "correct", "observed_confidence": 1.5},
                headers=bearer_headers(tenant_slug=persona.slug),
            )
    assert resp.status_code == 422, resp.text
    assert await _adjudication_rows(pg_container, claim.claim_id) == []
