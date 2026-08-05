"""SourceIngestService is constructed by the app's own wiring, not only by a
test that constructs it directly.

`source_ingest.py` was already thoroughly integration-tested
(`tests/integration/test_capability_requests.py`) but never built by
`create_app`, and never called from the connector run loop. This file covers
the first half of that gap -- the second half (the runner actually routing
claim-shaped facts through it) is `test_ingest_runner_claim_bridge.py`.

Covers:
- `Services` (the typed container) declares a `source_ingest` field.
- A real `create_app()`, with its lifespan run, wires a working
  `SourceIngestService` onto `app.state.services.source_ingest` -- the same
  instance every router and every background job would read.
- That wired instance is functional end-to-end: `governance.declare` +
  `source_ingest.ingest` against a real Postgres stage a claim through the
  exact collaborators wiring constructed, not a directly-constructed
  `SourceIngestService` standing in for them.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from registry.main import create_app
from registry.service.memory.claim_authority import Evidence
from registry.service.memory.source_ingest import Candidate, SourceIngestService
from registry.types import TenantContext
from registry.wiring.container import Services
from tests.helpers.auth_harness import default_settings
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fixtures / seed helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def wired_app(pg_container: str) -> AsyncIterator[FastAPI]:
    """A real `create_app()`, lifespan run, no HTTP traffic -- just the
    container every router and background job reads."""
    settings = default_settings(pg_container)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def ontology(pg_container: str) -> None:
    from registry.service.catalog.global_vocabulary import GlobalVocabularyService
    from registry.service.memory.claim_ontology import seed_ontology

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))
    finally:
        await engine.dispose()


async def _seed_tenant(pg_url: str) -> uuid.UUID:
    tid = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tid, "slug": f"sisw-{tid.hex[:8]}", "now": _NOW},
            )
    finally:
        await engine.dispose()
    return tid


async def _seed_actor(pg_url: str, tid: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                    "                    actor_kind, created_at) "
                    "VALUES (:aid, :tid, 'a', :sub, 'sync_worker', :now)"
                ),
                {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
            )
    finally:
        await engine.dispose()
    return aid


async def _seed_entity(pg_url: str, tid: uuid.UUID) -> uuid.UUID:
    eid = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                    "                      is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :name, 'public', TRUE, :now)"
                ),
                {"eid": eid, "tid": tid, "name": f"cap-{eid.hex[:8]}", "now": _NOW},
            )
    finally:
        await engine.dispose()
    return eid


async def _seed_source(pg_url: str, tid: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name, "
                    "                          config, is_active, created_at) "
                    "VALUES (:sid, :tid, 'openapi', 'src', '{}'::jsonb, TRUE, :now)"
                ),
                {"sid": sid, "tid": tid, "now": _NOW},
            )
    finally:
        await engine.dispose()
    return sid


def _ctx(tid: uuid.UUID, aid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["sync_worker"], oidc_subject="s")


# ---------------------------------------------------------------------------
# Container field
# ---------------------------------------------------------------------------


def test_services_container_declares_source_ingest_field() -> None:
    """A typo or a dropped field here is a construction error at startup, not
    a `None` three call frames deep in a request handler -- see the
    container's own module docstring for why that matters."""
    assert "source_ingest" in Services.__dataclass_fields__


# ---------------------------------------------------------------------------
# Construction + wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_ingest_is_constructed_by_create_app(wired_app: FastAPI) -> None:
    services: Services = wired_app.state.services
    assert isinstance(services.source_ingest, SourceIngestService)


@pytest.mark.asyncio
async def test_wired_source_ingest_stages_a_claim_through_governance_and_claims(
    wired_app: FastAPI,
    pg_container: str,
    ontology: None,
) -> None:
    """The instance `create_app` wired -- not a directly-constructed stand-in --
    actually admits and stages a claim, proving `claims` and `governance` are
    the real collaborators, wired together correctly through the container."""
    services: Services = wired_app.state.services

    tenant_id = await _seed_tenant(pg_container)
    actor_id = await _seed_actor(pg_container, tenant_id)
    subject_id = await _seed_entity(pg_container, tenant_id)
    source_id = await _seed_source(pg_container, tenant_id)

    ctx = _ctx(tenant_id, actor_id)
    await services.source_governance.declare(ctx, source_id=source_id, authority_tier="observer_extraction")

    candidate = Candidate(
        subject_reference=str(subject_id),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="document_revision", ref="wiring-proof@r1", excerpt="Owner: platform"),),
    )
    result = await services.source_ingest.ingest(ctx, source_id=source_id, candidates=(candidate,))
    assert result.admitted
    assert result.written == 1

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT status, predicate FROM memory_claims WHERE subject_entity_id = :sid"),
                    {"sid": subject_id},
                )
            ).one()
    finally:
        await engine.dispose()
    assert row.status == "staged"
    assert row.predicate == "owned_by_team"
