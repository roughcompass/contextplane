"""The connector run loop routes claim-shaped facts through source governance
and the claim store -- the second half of the wiring gap `source_ingest.py`
was quarantined for (the first half, construction, is
`test_source_ingest_wiring.py`).

Drives `registry.ingest.runner._execute_sync` directly against a real
Postgres, with only the network boundary stubbed (`discover`/`fetch`/
`validate` on the real connector classes) -- `parse()` runs for real, so the
claim-shaped/non-claim-shaped split and the predicate/value it produces are
exactly what the scheduled job would produce, not a hand-built stand-in.

Covers:
- A resolvable subject: the claim lands `staged` with the mapped predicate,
  the connector's own `source_url` as its value, and `connector_run`
  evidence naming the sync run.
- An unresolvable subject with `may_provision_entities` unset (the default):
  the claim lands `unlinked`; no entity is created.
- An unresolvable subject with `may_provision_entities` set: a new entity is
  provisioned and the claim lands `staged` against it.
- A ceiling breach on the second of two artifacts: the first artifact's claim
  is admitted and staged, the second is refused and the breaker trips -- no
  claim row for the refused artifact.
- The facts-table write and the claim-path write are independent: a claim
  path refusal for one artifact's claim-shaped fact does not block that same
  artifact's non-claim-shaped fact from reaching the facts table.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.config import Settings
from registry.ingest.connector import DiscoveredArtifact, ParsedFact
from registry.ingest.connectors.markdown_adr_rfc import MarkdownADRRFCConnector
from registry.ingest.connectors.openapi import OpenAPIConnector
from registry.ingest.runner import _execute_sync
from registry.service.catalog.core import CatalogService
from registry.service.catalog.schema import SchemaService
from registry.service.catalog.vocabulary import VocabularyService
from registry.service.memory.claims import ClaimService
from registry.service.memory.source_governance import SourceGovernanceService
from registry.service.memory.source_ingest import SourceIngestService
from registry.storage.models import Entity, SyncRun, SyncSource
from registry.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fixtures / seed helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ontology(factory: async_sessionmaker[AsyncSession]) -> None:
    from registry.service.catalog.global_vocabulary import GlobalVocabularyService
    from registry.service.memory.claim_ontology import seed_ontology

    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"bridge-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'sync', :sub, 'sync_worker', :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return aid


async def _seed_source(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    *,
    source_type: str,
) -> SyncSource:
    sid = uuid.uuid4()
    async with factory() as session, session.begin():
        row = SyncSource(
            source_id=sid,
            tenant_id=tid,
            source_type=source_type,
            display_name=f"test-{source_type}",
            config={"owner": "acme", "repo": "test", "ref": "main"},
            credentials_ref=None,
            schedule=None,
            is_active=True,
            created_at=_NOW,
        )
        session.add(row)
    # Re-fetched in its own session so the returned ORM object is the one
    # `_execute_sync` reads plain columns off of -- matching the runner's own
    # `run_sync_job` (which loads the row via `session.get` before dispatch).
    async with factory() as session:
        source_row = await session.get(SyncSource, sid)
        assert source_row is not None
        return source_row


async def _seed_sync_run(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, source_id: uuid.UUID) -> uuid.UUID:
    """A `sync_runs` row for the run `_execute_sync` writes evidence against.

    `run_sync_job` normally opens this before calling `_execute_sync`; calling
    `_execute_sync` directly (as every test here does, to drive the runner
    without an HTTP-triggered scheduler) means the fixture has to open it
    instead. Needed for two reasons: `_finish_run`'s own status update, and
    `connector_run` evidence's authority derivation, which joins this table to
    `sync_sources.source_type` to decide whether a claim earns the extraction
    tier.
    """
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            SyncRun(
                sync_run_id=run_id,
                tenant_id=tid,
                source_id=source_id,
                status="running",
                trigger="manual",
                started_at=_NOW,
            )
        )
    return run_id


async def _seed_entity(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, entity_id: uuid.UUID) -> None:
    async with factory() as session, session.begin():
        session.add(
            Entity(
                entity_id=entity_id,
                tenant_id=tid,
                entity_type="capability",
                name=f"cap-{entity_id.hex[:8]}",
                external_id=None,
                is_active=True,
                created_at=_NOW,
            )
        )


def _ctx(tid: uuid.UUID, aid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["sync_worker"], oidc_subject="s")


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:x@localhost/test",
        pgbouncer_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_jobstore_url="postgresql+asyncpg://x:x@localhost/test",
        scheduler_use_memory_jobstore=True,
        embedding_provider="stub",
        connector_run_timeout_s=30,
    )


def _services(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[CatalogService, SourceGovernanceService, SourceIngestService]:
    clock = FakeClock(_NOW)
    vocabulary = VocabularyService(factory)
    schema = SchemaService(factory, clock)
    catalog = CatalogService(factory, clock, vocabulary, schema)
    claims = ClaimService(factory, clock=clock)
    governance = SourceGovernanceService(factory, clock=clock)
    source_ingest = SourceIngestService(claims=claims, governance=governance, catalog=catalog)
    return catalog, governance, source_ingest


async def _claim_row(factory: async_sessionmaker[AsyncSession], *, predicate: str, author_tenant_id: uuid.UUID) -> Any:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT status, predicate, subject_entity_id, value_jsonb AS value "
                    "  FROM memory_claims WHERE predicate = :pred AND author_tenant_id = :tid"
                ),
                {"pred": predicate, "tid": author_tenant_id},
            )
        ).one()


# ---------------------------------------------------------------------------
# Resolvable subject -> staged, correct predicate/value/evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolvable_subject_lands_staged_with_mapped_predicate(
    factory: async_sessionmaker[AsyncSession],
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source = await _seed_source(factory, tid, source_type="openapi")
    run_id = await _seed_sync_run(factory, tid, source.source_id)

    artifact = DiscoveredArtifact(
        artifact_id="petstore.openapi.yaml",
        source_url="https://raw.githubusercontent.com/acme/test/main/petstore.openapi.yaml",
        artifact_type="openapi",
    )
    raw = b"openapi: '3.0.3'\ninfo:\n  title: Petstore\n  version: '1.0.0'\npaths: {}\n"
    # Real parse() to learn the deterministic entity_id the connector derives,
    # so the fixture can pre-seed exactly that entity -- the resolvable case.
    facts = OpenAPIConnector().parse(artifact, raw)
    assert facts[0].category == "api_doc"
    await _seed_entity(factory, tid, facts[0].entity_id)

    catalog, governance, source_ingest = _services(factory)
    await governance.declare(_ctx(tid, aid), source_id=source.source_id, authority_tier="observer_extraction")

    with (
        patch.object(OpenAPIConnector, "validate", new=AsyncMock(return_value=None)),
        patch.object(OpenAPIConnector, "discover", new=AsyncMock(return_value=[artifact])),
        patch.object(OpenAPIConnector, "fetch", new=AsyncMock(return_value=raw)),
    ):
        await _execute_sync(
            source=source,
            sync_run_id=run_id,
            ctx=_ctx(tid, aid),
            session_factory=factory,
            catalog=catalog,
            settings=_settings(),
            source_ingest=source_ingest,
        )

    row = await _claim_row(factory, predicate="interface_specification_url", author_tenant_id=tid)
    assert row.status == "staged"
    assert row.subject_entity_id == facts[0].entity_id
    assert row.value == artifact.source_url

    async with factory() as session:
        evidence = (
            await session.execute(
                text(
                    "SELECT evidence_kind, evidence_ref FROM memory_claim_provenance p "
                    "  JOIN memory_claims c ON c.claim_id = p.claim_id "
                    " WHERE c.author_tenant_id = :tid"
                ),
                {"tid": tid},
            )
        ).one()
    assert evidence.evidence_kind == "connector_run"
    assert evidence.evidence_ref == str(run_id)


# ---------------------------------------------------------------------------
# Unresolvable subject: provisioning off (default) vs on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_subject_lands_unlinked_when_provisioning_unset(
    factory: async_sessionmaker[AsyncSession],
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source = await _seed_source(factory, tid, source_type="markdown_adr_rfc")
    run_id = await _seed_sync_run(factory, tid, source.source_id)

    artifact = DiscoveredArtifact(
        artifact_id="docs/adr/0001-postgres.md",
        source_url="https://raw.githubusercontent.com/acme/test/main/docs/adr/0001-postgres.md",
        artifact_type="markdown_adr_rfc",
    )
    raw = b"# Use Postgres\n\nDecision body.\n"
    # No entity seeded for this artifact's deterministic entity_id -- the
    # unresolvable case.

    catalog, governance, source_ingest = _services(factory)
    await governance.declare(_ctx(tid, aid), source_id=source.source_id, authority_tier="observer_extraction")

    with (
        patch.object(MarkdownADRRFCConnector, "validate", new=AsyncMock(return_value=None)),
        patch.object(MarkdownADRRFCConnector, "discover", new=AsyncMock(return_value=[artifact])),
        patch.object(MarkdownADRRFCConnector, "fetch", new=AsyncMock(return_value=raw)),
    ):
        await _execute_sync(
            source=source,
            sync_run_id=run_id,
            ctx=_ctx(tid, aid),
            session_factory=factory,
            catalog=catalog,
            settings=_settings(),
            source_ingest=source_ingest,
        )

    row = await _claim_row(factory, predicate="decision_record_url", author_tenant_id=tid)
    assert row.status == "unlinked"
    assert row.subject_entity_id is None

    async with factory() as session:
        entity_count = (
            await session.execute(text("SELECT count(*) FROM entities WHERE tenant_id = :tid"), {"tid": tid})
        ).scalar_one()
    assert entity_count == 0, "no entity should be provisioned when may_provision_entities is unset"


@pytest.mark.asyncio
async def test_unresolvable_subject_is_provisioned_and_linked_when_flag_set(
    factory: async_sessionmaker[AsyncSession],
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source = await _seed_source(factory, tid, source_type="markdown_adr_rfc")
    run_id = await _seed_sync_run(factory, tid, source.source_id)

    artifact = DiscoveredArtifact(
        artifact_id="docs/adr/0002-redis.md",
        source_url="https://raw.githubusercontent.com/acme/test/main/docs/adr/0002-redis.md",
        artifact_type="markdown_adr_rfc",
    )
    raw = b"# Use Redis\n\nDecision body.\n"

    catalog, governance, source_ingest = _services(factory)
    await governance.declare(
        _ctx(tid, aid),
        source_id=source.source_id,
        authority_tier="observer_extraction",
        may_provision_entities=True,
    )

    with (
        patch.object(MarkdownADRRFCConnector, "validate", new=AsyncMock(return_value=None)),
        patch.object(MarkdownADRRFCConnector, "discover", new=AsyncMock(return_value=[artifact])),
        patch.object(MarkdownADRRFCConnector, "fetch", new=AsyncMock(return_value=raw)),
    ):
        await _execute_sync(
            source=source,
            sync_run_id=run_id,
            ctx=_ctx(tid, aid),
            session_factory=factory,
            catalog=catalog,
            settings=_settings(),
            source_ingest=source_ingest,
        )

    row = await _claim_row(factory, predicate="decision_record_url", author_tenant_id=tid)
    assert row.status == "staged"
    assert row.subject_entity_id is not None

    async with factory() as session:
        provisioned = await session.get(Entity, row.subject_entity_id)
    assert provisioned is not None
    assert provisioned.tenant_id == tid
    assert provisioned.entity_type == "capability"


# ---------------------------------------------------------------------------
# Ceiling breach -> breaker; the refused artifact writes no claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ceiling_breach_on_second_artifact_trips_breaker_and_refuses(
    factory: async_sessionmaker[AsyncSession],
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source = await _seed_source(factory, tid, source_type="markdown_adr_rfc")
    run_id = await _seed_sync_run(factory, tid, source.source_id)

    artifact_a = DiscoveredArtifact(
        artifact_id="docs/adr/0003-a.md",
        source_url="https://raw.githubusercontent.com/acme/test/main/docs/adr/0003-a.md",
        artifact_type="markdown_adr_rfc",
    )
    artifact_b = DiscoveredArtifact(
        artifact_id="docs/adr/0004-b.md",
        source_url="https://raw.githubusercontent.com/acme/test/main/docs/adr/0004-b.md",
        artifact_type="markdown_adr_rfc",
    )
    raw_by_id = {
        artifact_a.artifact_id: b"# Decision A\n\nBody A.\n",
        artifact_b.artifact_id: b"# Decision B\n\nBody B.\n",
    }

    catalog, governance, source_ingest = _services(factory)
    # One claim per artifact's batch; a ceiling of 1 admits the first
    # artifact's single candidate and refuses the second's.
    await governance.declare(
        _ctx(tid, aid),
        source_id=source.source_id,
        authority_tier="observer_extraction",
        ingest_ceiling=1,
        may_provision_entities=True,
    )

    async def _fake_fetch(artifact: DiscoveredArtifact, _source: SyncSource) -> bytes:
        return raw_by_id[artifact.artifact_id]

    with (
        patch.object(MarkdownADRRFCConnector, "validate", new=AsyncMock(return_value=None)),
        patch.object(MarkdownADRRFCConnector, "discover", new=AsyncMock(return_value=[artifact_a, artifact_b])),
        patch.object(MarkdownADRRFCConnector, "fetch", new=AsyncMock(side_effect=_fake_fetch)),
    ):
        await _execute_sync(
            source=source,
            sync_run_id=run_id,
            ctx=_ctx(tid, aid),
            session_factory=factory,
            catalog=catalog,
            settings=_settings(),
            source_ingest=source_ingest,
        )

    async with factory() as session:
        staged_count = (
            await session.execute(
                text("SELECT count(*) FROM memory_claims WHERE author_tenant_id = :tid"),
                {"tid": tid},
            )
        ).scalar_one()
        policy_row = (
            await session.execute(
                text("SELECT breaker_open_until, breach_count FROM memory_source_governance WHERE source_id = :sid"),
                {"sid": source.source_id},
            )
        ).one()
    assert staged_count == 1, "only the first artifact's batch was admitted"
    assert policy_row.breaker_open_until is not None
    assert policy_row.breach_count == 1


# ---------------------------------------------------------------------------
# Independence: a claim-path refusal does not block the facts-table write
# ---------------------------------------------------------------------------


class _MixedFactsConnector:
    """A synthetic connector whose one artifact produces one claim-shaped fact
    (`api_doc`) and one non-claim-shaped fact (`dev_doc`) -- proving the
    runner's split treats the two subsets as independent writes, not that any
    real shipped connector emits this exact mix (none does)."""

    def __init__(self, claim_shaped_entity_id: uuid.UUID, fact_entity_id: uuid.UUID, artifact: DiscoveredArtifact):
        self._claim_entity_id = claim_shaped_entity_id
        self._fact_entity_id = fact_entity_id
        self._artifact = artifact

    async def validate(self, credentials_ref: str | None) -> None:
        return None

    async def discover(self, source: SyncSource) -> list[DiscoveredArtifact]:
        return [self._artifact]

    async def fetch(self, artifact: DiscoveredArtifact, source: SyncSource) -> bytes:
        return b"irrelevant"

    def parse(self, artifact: DiscoveredArtifact, raw: bytes) -> list[ParsedFact]:
        return [
            ParsedFact(
                entity_id=self._claim_entity_id,
                category="api_doc",
                body="spec body",
                valid_from=None,
                source_url=self._artifact.source_url,
                commit_sha=None,
            ),
            ParsedFact(
                entity_id=self._fact_entity_id,
                category="dev_doc",
                body="doc body",
                valid_from=None,
                source_url=self._artifact.source_url,
                commit_sha=None,
            ),
        ]


@pytest.mark.asyncio
async def test_claim_path_refusal_does_not_block_facts_table_write(
    factory: async_sessionmaker[AsyncSession],
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    source = await _seed_source(factory, tid, source_type="openapi")
    run_id = await _seed_sync_run(factory, tid, source.source_id)

    claim_entity_id = uuid.uuid4()  # never resolves; irrelevant here since admission itself refuses.
    fact_entity_id = uuid.uuid4()
    await _seed_entity(factory, tid, fact_entity_id)  # facts-table write needs this to exist.

    artifact = DiscoveredArtifact(
        artifact_id="mixed-artifact",
        source_url="https://example.com/mixed-artifact",
        artifact_type="openapi",
    )
    connector = _MixedFactsConnector(claim_entity_id, fact_entity_id, artifact)

    catalog, governance, source_ingest = _services(factory)
    await governance.declare(_ctx(tid, aid), source_id=source.source_id, authority_tier="observer_extraction")
    # Pre-consume the entire window so this run's one claim-shaped candidate
    # breaches immediately -- the facts-table write must still land.
    exhausted = await governance.admit(source.source_id, count=1000)
    assert exhausted.permitted

    with patch("registry.ingest.runner.get_connector", return_value=lambda: connector):
        await _execute_sync(
            source=source,
            sync_run_id=run_id,
            ctx=_ctx(tid, aid),
            session_factory=factory,
            catalog=catalog,
            settings=_settings(),
            source_ingest=source_ingest,
        )

    async with factory() as session:
        fact_row = (
            await session.execute(
                text("SELECT category FROM facts WHERE entity_id = :eid"),
                {"eid": fact_entity_id},
            )
        ).one()
        claim_count = (
            await session.execute(
                text("SELECT count(*) FROM memory_claims WHERE author_tenant_id = :tid"),
                {"tid": tid},
            )
        ).scalar_one()
    assert fact_row.category == "dev_doc"
    assert claim_count == 0, "the refused claim path must not have written anything"
