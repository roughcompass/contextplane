"""Multi-session recall: is what one session learned retrievable in the next?

E8 names this as one of four things its harness still owes and no task covered
it. It is not blocked on anything, and the reason is worth stating because it
also decides what the measurement can honestly claim.

**Extracted claims are the cross-session carrier, not session events.** Session
events are deliberately not an embedding target -- nothing reads them
semantically, replay is by `seq`, and adding a target speculatively would mean
embedding every conversational turn in every tenant against a benefit nobody has
stated. Claims already are a target. And `ClaimServingService.retrieve` is scoped by tenant and actor, never
by session. So a claim derived in session A is *reachable* from session B by
construction -- there is no filter to defeat.

What is therefore measured here is not reachability but **retrieval**: given a
question asked in a later session, does the claim the earlier session produced
actually come back, against the competition of every other scenario's claims in
the same tenant? That is the question an agent's two-call loop depends on, and
the one nothing answered.

**Report first, threshold later**, following the extraction ground truth's
discipline and for the reason that task learned the hard way: its first
measurement was a fixture defect reading as a model result, and a threshold set
beside that number would have been set against the defect.

**Two things the first run found, both about the harness rather than the model,
and both recorded because the next reader will otherwise rediscover them.**
Staging a claim is not enough to make it retrievable: `project_claim` refuses to
queue anything until `consolidated_at` is set, and the drain is what turns a
queued row into a vector. A fixture that staged and then queried measured an
empty index and reported 0/12, which is a harness failure wearing the shape of a
recall result -- caught only by the anti-vacuity assertion at the bottom. So
**an agent's cross-session recall depends on consolidation having run**, which is
a real property of the system and not a test detail.

**The embedder decides which regime the number describes**, so the report says
which one it ran in. With the ONNX artifact present this is a semantic
measurement; without it the stub returns zero vectors and the lexical arm
dominates. Both are real numbers about different systems, and a figure filed
without the mode would be neither. Same split `test_retrieval_embedding.py`
makes for `recall@10`.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.config import Settings
from contextplane.embedding import build_embedder
from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.memory.claim_ontology import seed_ontology
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.memory.claim_writer import ClaimService, Evidence
from contextplane.service.memory.consolidation import ConsolidationService
from contextplane.service.retrieval.embedding_drain import drain_outbox
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 22, 12, 0, 0, tzinfo=datetime.UTC)
_FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "eval" / "fixtures" / "multi_session_recall.json"

#: How deep the later session looks. Ten because that is what every other recall
#: figure in this harness uses, and a metric comparable with its siblings is
#: worth more than one tuned to look good.
TOP_K = 10


def _load() -> dict[str, object]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def test_the_fixture_holds_what_it_says_it_holds() -> None:
    """The contract, checked without a database.

    A recall figure over a fixture that quietly shrank is a number about a
    different corpus wearing the same name. This runs always; the measurement
    below needs a database and does not.
    """
    doc = _load()
    scenarios = doc["scenarios"]
    assert isinstance(scenarios, list)
    assert len(scenarios) == doc["scenario_count"] == 12

    seen: set[str] = set()
    for scenario in scenarios:
        assert scenario["scenario_id"] not in seen, "duplicate scenario id"
        seen.add(scenario["scenario_id"])
        # The two sessions must differ, or the scenario measures same-session
        # recall and the file's whole premise with it.
        assert scenario["earlier_session_id"] != scenario["later_session_id"]
        assert scenario["claims"], "a scenario with no claims can never be recalled"
        for claim in scenario["claims"]:
            assert claim["value_kind"] in {"entity", "string"}
        assert scenario["query"].strip()


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"msr-{tid.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'agent', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return tid, aid


async def _seed_entity(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, name: str) -> uuid.UUID:
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, "
                "                      visibility, is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, 'tenant-shared', TRUE, :now)"
            ),
            {"eid": eid, "tid": tid, "name": name, "now": _NOW},
        )
    return eid


@pytest.mark.asyncio
async def test_multi_session_recall(factory: async_sessionmaker[AsyncSession], pg_container: str) -> None:
    """Recall@10 for a question asked one session after the answer was learned.

    Every scenario's claims live in the same tenant, so each query competes with
    all the others. A per-scenario tenant would make this trivially perfect and
    measure nothing an agent experiences.
    """
    doc = _load()
    scenarios = doc["scenarios"]
    assert isinstance(scenarios, list)

    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))
    tid, aid = await _seed_tenant(factory)
    ctx = TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="agent")
    claims = ClaimService(factory, clock=FakeClock(_NOW))
    consolidation = ConsolidationService(factory, clock=FakeClock(_NOW))

    # One entity per name across the whole fixture, not one per mention. Entity
    # names are unique per tenant, and reusing them is also the truer shape: the
    # same service appears in several scenarios, which is what gives the corpus
    # a graph rather than twelve disjoint pairs.
    entities: dict[str, uuid.UUID] = {}

    async def entity(name: str) -> uuid.UUID:
        if name not in entities:
            entities[name] = await _seed_entity(factory, tid, name)
        return entities[name]

    # Stage every scenario's claims first, so each later query is answered
    # against the whole corpus rather than against its own scenario alone.
    staged: dict[str, list[uuid.UUID]] = {}
    for scenario in scenarios:
        ids: list[uuid.UUID] = []
        for claim in scenario["claims"]:
            subject = await entity(claim["subject"])
            # The ontology types each predicate: `depends_on` and its siblings
            # take an entity reference, `owned_by_team` and its siblings take a
            # literal. Sending the wrong one is refused, which is how the first
            # draft of this fixture -- written against an invented `uses_tool` --
            # found out it had invented one.
            value = str(await entity(claim["value"])) if claim["value_kind"] == "entity" else claim["value"]
            written = await claims.stage_claim(
                ctx,
                subject_reference=str(subject),
                predicate=claim["predicate"],
                value=value,
                evidence=(
                    Evidence(
                        kind="session_event",
                        # The earlier session is what makes this cross-session:
                        # the evidence names an event that happened there.
                        ref=f"{scenario['earlier_session_id']}/evt-{uuid.uuid4().hex[:8]}",
                        excerpt=claim["excerpt"],
                    ),
                ),
            )
            # Consolidate, or the claim is never indexed and this measures an
            # empty index. `project_claim` requires `consolidated_at` to be set
            # before it will queue anything, so an agent's cross-session recall
            # depends on consolidation having run -- which is a real property of
            # the system and the first thing this fixture found.
            await consolidation.consolidate(written.claim_id)
            ids.append(written.claim_id)
        staged[scenario["scenario_id"]] = ids

    settings = Settings(
        database_url=pg_container,
        pgbouncer_url=pg_container,
        scheduler_jobstore_url=pg_container,
        embedding_provider="stub",
    )
    embedder = build_embedder(settings)
    # Consolidation queues; the drain is what writes vectors. Without this the
    # index is empty however well the claims were staged.
    await drain_outbox(factory, embedder, settings)
    serving = ClaimServingService(factory, clock=FakeClock(_NOW))

    hits = 0
    misses: list[str] = []
    for scenario in scenarios:
        served = await serving.retrieve(ctx, query=scenario["query"], embedder=embedder, top_k=TOP_K)
        returned = {c.claim_id for c in served}
        wanted = set(staged[scenario["scenario_id"]])
        if wanted & returned:
            hits += 1
        else:
            misses.append(f"{scenario['scenario_id']}: {scenario['query']!r}")

    total = len(scenarios)
    recall = hits / total
    print(f"\nmulti-session recall@{TOP_K}: {hits}/{total} = {recall:.3f}")
    print(f"  embedder: {settings.embedding_provider} (zero vectors — lexical-dominant)")
    for miss in misses:
        print(f"  miss  {miss}")
    print("  Record this figure in eval/EVAL.md; this test produces it and does not judge it.")

    # Anti-vacuity, not a quality threshold. A run that retrieved nothing at all
    # is a broken harness rather than a bad recall number, and the two would be
    # indistinguishable from a bare report. What "good" is has not been decided
    # and is not decided here.
    assert hits > 0, (
        "no scenario recalled anything, which is a harness failure rather than a measurement: "
        "claims were staged and the retrieve returned none of them for any query"
    )
