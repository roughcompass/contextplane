"""The sweep that turns consolidated claims into promotion proposals -- and, where
a tenant's own guardrails permit it, straight into the canonical graph.

Consolidation decides a claim is settled; nothing else in the platform calls
`PromotionService.propose`. These tests exercise the wire this worker is: that a
consolidated claim is found and proposed, that the default posture leaves it open
for a human reviewer, that an allowlisted tenant's eligible claim is auto-accepted
by a system-curator identity distinct from whoever staged it, and that the sharp
edges named in the design review -- high-impact, non-owner authorship -- still
route to review even when the predicate is allowlisted.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.memory.claim_authority import Evidence
from registry.service.memory.claim_ontology import seed_ontology
from registry.service.memory.claim_writer import ClaimService
from registry.service.memory.consolidation import ConsolidationService
from registry.service.memory.promotion import PromotionService
from registry.service.memory.promotion_guardrails import GuardrailService
from registry.workers.promotion_sweep import PromotionSweepWorker
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_producer_ctx as _ctx
from tests.helpers.seeding import seed_entity as _seed_entity

_NOW = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.UTC)


def _at(minutes: int) -> datetime.datetime:
    return _NOW + datetime.timedelta(minutes=minutes)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def ontology(factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_ontology(GlobalVocabularyService(factory, clock=FakeClock(_NOW)))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"psweep-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, 'human', :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return aid


async def _stage(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    subject: uuid.UUID,
    *,
    predicate: str = "owned_by_team",
    value: object = "platform",
    at: int = 0,
) -> uuid.UUID:
    """Stage a claim and consolidate it, which is what makes it a sweep candidate."""
    clock = FakeClock(_at(at))
    claim = await ClaimService(factory, clock=clock).stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    await ConsolidationService(factory, clock=clock).consolidate(claim.claim_id)
    return claim.claim_id


def _sweep(
    factory: async_sessionmaker[AsyncSession], *, at: int = 0, batch_size: int = 100_000
) -> PromotionSweepWorker:
    # The huge default batch is deliberate: the suite's shared container carries
    # other tests' staged claims, some permanently un-proposable, and the walk is
    # oldest-first -- with a production-sized batch this test's own claim can fall
    # outside the walk entirely and never be considered.
    clock = FakeClock(_at(at))
    claims = ClaimService(factory, clock=clock)
    promotion = PromotionService(factory, claims=claims, clock=clock)
    guardrails = GuardrailService(factory, clock=clock)
    return PromotionSweepWorker(factory, promotion, guardrails, clock=clock, batch_size=batch_size)


async def _promotion_state(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> str | None:
    async with factory() as session:
        return (
            await session.execute(
                text("SELECT promotion_state FROM memory_claims WHERE claim_id = :cid"), {"cid": claim_id}
            )
        ).scalar_one()


async def _proposal_state(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> str:
    async with factory() as session:
        return str(
            (
                await session.execute(
                    text("SELECT state FROM memory_promotion_proposal WHERE claim_id = :cid"), {"cid": claim_id}
                )
            ).scalar_one()
        )


async def _proposal_high_impact_reasons(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> list[str]:
    async with factory() as session:
        reasons = (
            await session.execute(
                text("SELECT high_impact_reasons FROM memory_promotion_proposal WHERE claim_id = :cid"),
                {"cid": claim_id},
            )
        ).scalar_one()
    return list(reasons or [])


async def _live_value(factory: async_sessionmaker[AsyncSession], entity_id: uuid.UUID, key: str) -> object:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT value FROM attributes "
                    " WHERE entity_id = :eid AND key = :key AND t_invalidated_at IS NULL "
                    " ORDER BY t_valid_from DESC LIMIT 1"
                ),
                {"eid": entity_id, "key": key},
            )
        ).first()
    return row[0] if row is not None else None


async def _journal_row(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT promoted_by FROM memory_promotion_journal WHERE claim_id = :cid"), {"cid": claim_id}
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


async def _actor_kind(factory: async_sessionmaker[AsyncSession], actor_id: uuid.UUID) -> str:
    async with factory() as session:
        return str(
            (
                await session.execute(text("SELECT actor_kind FROM actors WHERE actor_id = :aid"), {"aid": actor_id})
            ).scalar_one()
        )


async def _wrapper_audit_row(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT actor_id, after_jsonb FROM audit_log "
                        " WHERE target_id = :cid AND action = 'claim.auto_promoted'"
                    ),
                    {"cid": claim_id},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None, "expected the sweep's own wrapper audit row"
    return dict(row)


# --- proposing, and the default posture ----------------------------------------


@pytest.mark.asyncio
async def test_a_consolidated_claim_is_proposed_and_left_open_by_default(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """No allowlist entry: eligible, uncontested, owner-originated -- and it still
    waits, because nobody opted the predicate in."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    claim_id = await _stage(factory, tid, aid, subject, at=0)

    report = await _sweep(factory, at=10).run_once()

    # Counters are relative: the sweep's walk is global, and the suite's shared
    # container carries other tests' staged claims -- some of which fail propose
    # forever and so re-enter every walk. The claim's own rows are the proof.
    assert report.considered >= 1
    assert report.awaiting_review >= 1
    assert await _promotion_state(factory, claim_id) == "proposed"
    assert await _proposal_state(factory, claim_id) == "open"
    assert await _live_value(factory, subject, "owned_by_team") is None


@pytest.mark.asyncio
async def test_a_proposed_claim_is_not_considered_again(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """`propose` sets `promotion_state='proposed'` in the same transaction as the
    insert, which is what keeps the candidate query from finding the same claim
    twice."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    claim_id = await _stage(factory, tid, aid, subject, at=0)

    await _sweep(factory, at=10).run_once()
    assert await _promotion_state(factory, claim_id) == "proposed"
    await _sweep(factory, at=20).run_once()

    # One proposal row, not two: the second walk must not have re-proposed it.
    async with factory() as session:
        proposals = (
            await session.execute(
                text("SELECT COUNT(*) FROM memory_promotion_proposal WHERE claim_id = :cid"), {"cid": claim_id}
            )
        ).scalar_one()
    assert proposals == 1


# --- allowlisted auto-promotion --------------------------------------------------


@pytest.mark.asyncio
async def test_an_allowlisted_eligible_claim_is_auto_promoted_by_the_system_curator(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    await GuardrailService(factory, clock=FakeClock(_NOW)).allow(tid, "owned_by_team", actor_id=aid)
    claim_id = await _stage(factory, tid, aid, subject, predicate="owned_by_team", value="platform", at=0)

    report = await _sweep(factory, at=10).run_once()

    assert report.auto_promoted >= 1
    assert await _promotion_state(factory, claim_id) == "promoted"
    assert await _proposal_state(factory, claim_id) == "accepted"
    assert await _live_value(factory, subject, "owned_by_team") == "platform"

    # The journal's promoted_by is the system-curator actor, never the human actor
    # who staged the claim -- an auto-promotion must be traceable to a distinct,
    # non-human identity.
    journal = await _journal_row(factory, claim_id)
    system_actor_id = journal["promoted_by"]
    assert system_actor_id != aid
    assert await _actor_kind(factory, system_actor_id) == "system_curator"  # type: ignore[arg-type]

    # The sweep's own wrapper audit row, alongside accept()'s own CLAIM_PROMOTED row.
    wrapper = await _wrapper_audit_row(factory, claim_id)
    assert wrapper["actor_id"] == system_actor_id
    payload = json.loads(wrapper["after_jsonb"]) if isinstance(wrapper["after_jsonb"], str) else wrapper["after_jsonb"]
    assert payload["auto_promoted"] is True
    assert payload["system_actor_id"] == str(system_actor_id)
    assert payload["guardrail_decision"]["permitted"] is True


@pytest.mark.asyncio
async def test_the_system_curator_actor_is_reused_across_claims_for_the_same_tenant(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))
    await guardrails.allow(tid, "owned_by_team", actor_id=aid)
    await guardrails.allow(tid, "runbook_url", actor_id=aid)

    first_subject = await _seed_entity(factory, tid)
    second_subject = await _seed_entity(factory, tid)
    first_claim = await _stage(factory, tid, aid, first_subject, predicate="owned_by_team", value="platform", at=0)
    second_claim = await _stage(
        factory, tid, aid, second_subject, predicate="runbook_url", value="https://runbooks/x", at=1
    )

    await _sweep(factory, at=10).run_once()

    first_journal = await _journal_row(factory, first_claim)
    second_journal = await _journal_row(factory, second_claim)
    assert first_journal["promoted_by"] == second_journal["promoted_by"]


@pytest.mark.asyncio
async def test_a_high_impact_claim_is_never_auto_promoted_even_when_allowlisted(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """The condition no allowlist entry can switch off, exercised end to end
    through the real impact assessment rather than a mocked decision.

    `awaiting_review >= 1`, `promotion_state == "proposed"`, and no live value are
    also exactly what a never-registered allowlist entry would produce -- an
    allow() that silently no-oped would leave the same three facts behind. The two
    assertions below close that gap: the proposal row must actually carry a
    high-impact reason (proving the real impact assessment ran and found one, not
    just that review never triggered) and the allowlist row must actually exist
    (proving the guard that stopped promotion was the high-impact check, not an
    allowlist that never registered in the first place).
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))
    await guardrails.allow(tid, "lifecycle_state", actor_id=aid)
    claim_id = await _stage(factory, tid, aid, subject, predicate="lifecycle_state", value="deprecated", at=0)

    report = await _sweep(factory, at=10).run_once()

    assert report.awaiting_review >= 1
    assert await _promotion_state(factory, claim_id) == "proposed"
    assert await _live_value(factory, subject, "lifecycle_state") is None

    # The impact assessment actually ran and actually found a reason -- this is
    # not review-by-default with an empty reasons list.
    assert await _proposal_high_impact_reasons(factory, claim_id), "expected a recorded high-impact reason"

    # The allowlist entry actually registered -- a no-op allow() would produce the
    # identical observable state above, so this is the check that rules it out.
    assert "lifecycle_state" in await guardrails.allowlist_for(tid)


# --- failure isolation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_claim_does_not_stop_the_rest(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    other_subject = await _seed_entity(factory, tid)
    failing_subject = await _seed_entity(factory, tid)
    other_claim = await _stage(factory, tid, aid, other_subject, predicate="owned_by_team", value="platform", at=0)
    failing_claim = await _stage(
        factory, tid, aid, failing_subject, predicate="runbook_url", value="https://runbooks/x", at=1
    )

    clock = FakeClock(_at(10))
    claims = ClaimService(factory, clock=clock)

    class _FailsOnce(PromotionService):
        def __init__(self) -> None:
            super().__init__(factory, claims=claims, clock=clock)
            self.calls = 0

        async def propose(self, claim_id: uuid.UUID):  # type: ignore[no-untyped-def]
            self.calls += 1
            if claim_id == failing_claim:
                msg = "this neighbourhood is pathological"
                raise RuntimeError(msg)
            return await super().propose(claim_id)

    promotion = _FailsOnce()
    # The huge batch is deliberate, same as `_sweep`'s default: the suite's shared
    # container carries other tests' staged claims, and the walk is oldest-first --
    # with the worker's own default batch size this test's two claims can fall
    # outside the walk entirely and never be considered.
    worker = PromotionSweepWorker(
        factory, promotion, GuardrailService(factory, clock=clock), clock=clock, batch_size=100_000
    )
    await worker.run_once()

    # `report.failed` and `promotion.calls` are global counters over the shared
    # container's full walk, so a bare `>= 1` / `>= 2` is satisfiable by other
    # tests' claims alone and proves nothing about this test's own two. The
    # per-claim assertions below are the real proof: the failing claim never
    # got promoted and the other claim was still proposed despite it.
    assert await _promotion_state(factory, failing_claim) is None
    assert await _proposal_state(factory, other_claim) == "open"
