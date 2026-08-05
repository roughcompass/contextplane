"""The living-memory loop, walked end to end on one claim's life: staged, settled
by consolidation, proposed for promotion, accepted into the canonical graph,
served back with its citation, and reversed -- restoring exactly what the graph
held before.

Every arrow here has its own dedicated coverage elsewhere (staging in
`test_claim_assertion.py`-adjacent suites, consolidation's ADD/UPDATE/CONTESTED
decisions in the consolidation suite, the sweep's propose/auto-accept path in
`test_promotion_sweep.py`, accept/reject/reverse over HTTP in
`test_memory_promotion_surface.py`). What none of those walk is the whole chain
back to back on one piece of data, reading real state off the database at every
step rather than trusting that no exception means the step worked.

The reversal arrow needs something more interesting to restore than "the slot
goes back to empty", so this test carries a second claim: once the first claim's
promotion has written a canonical value, a second, later claim about the same
subject and predicate arrives from the same authority tier. Consolidation's
tie-break is recency, so it supersedes the first claim in staging -- but the
*canonical* row it goes on to replace, once promoted, is the row the first
claim's own promotion wrote. Reversing the second promotion has to bring that
exact row back, not merely clear the attribute, which is what
`PromotionService.reverse`'s own docstring ("restoring what the graph said
before") actually promises.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.memory.claim_authority import Evidence
from registry.service.memory.claim_ontology import seed_ontology
from registry.service.memory.claim_serving import ClaimQuery, ClaimServingService
from registry.service.memory.claim_writer import ClaimService
from registry.service.memory.consolidation import ConsolidationService
from registry.service.memory.promotion import PromotionService
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_producer_ctx as _ctx
from tests.helpers.seeding import seed_entity as _seed_entity

_NOW = datetime.datetime(2026, 8, 5, 9, 0, tzinfo=datetime.UTC)

# Producer is sufficient standing to author a claim and, in the same tenant, to
# review the proposal it becomes -- `PromotionService.REVIEW_ROLES` accepts
# either producer or admin, and this suite has no reason to exercise the
# admin-only edge.
_REVIEW_ROLES = frozenset({"producer"})


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
            {"tid": tid, "slug": f"loop-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'loop-actor', :sub, 'human', :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return aid


async def _claim_row(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT status, promotion_state, consolidated_at, confidence, "
                        "       superseded_by, is_contested "
                        "  FROM memory_claims WHERE claim_id = :cid"
                    ),
                    {"cid": claim_id},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


async def _proposal_row(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT proposal_id, state FROM memory_promotion_proposal WHERE claim_id = :cid"),
                    {"cid": claim_id},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


async def _journal_reversed_at(
    factory: async_sessionmaker[AsyncSession], promotion_id: uuid.UUID
) -> datetime.datetime | None:
    async with factory() as session:
        return (
            await session.execute(
                text("SELECT reversed_at FROM memory_promotion_journal WHERE promotion_id = :pid"),
                {"pid": promotion_id},
            )
        ).scalar_one()


async def _live_attribute(
    factory: async_sessionmaker[AsyncSession], entity_id: uuid.UUID, key: str
) -> tuple[uuid.UUID, object] | None:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT attr_id, value FROM attributes "
                    " WHERE entity_id = :eid AND key = :key AND t_invalidated_at IS NULL "
                    " ORDER BY t_valid_from DESC LIMIT 1"
                ),
                {"eid": entity_id, "key": key},
            )
        ).first()
    return (row[0], row[1]) if row is not None else None


@pytest.mark.asyncio
async def test_the_memory_loop_walks_from_assertion_to_reversal(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    ctx = _ctx(tid, aid)

    claims = ClaimService(factory, clock=FakeClock(_at(0)))
    consolidation = ConsolidationService(factory, clock=FakeClock(_at(0)))
    promotion = PromotionService(factory, claims=claims, clock=FakeClock(_at(0)))
    serving = ClaimServingService(factory, clock=FakeClock(_at(0)))

    # --- assert: ClaimService is the one path that can write memory_claims -----
    # A resolvable subject reference is what makes this land `staged` rather
    # than `unlinked` and queued for a curator.
    first = await claims.stage_claim(
        ctx,
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform-team",
        evidence=(Evidence(kind="session_event", ref="evt-1", excerpt="the platform team owns this capability"),),
        asserted_valid_from=_at(0),
    )
    row = await _claim_row(factory, first.claim_id)
    assert row["status"] == "staged"
    assert row["confidence"] is not None, "a linked claim is scored the moment it exists"
    assert row["consolidated_at"] is None

    # --- consolidate: nothing else claims this subject+predicate yet -----------
    # A plain ADD is a decision this test can name, not just an absence of error.
    outcome = await consolidation.consolidate(first.claim_id)
    assert outcome.decision == "add"
    row = await _claim_row(factory, first.claim_id)
    assert row["consolidated_at"] is not None

    # --- propose -----------------------------------------------------------
    proposal = await promotion.propose(first.claim_id)
    assert proposal is not None
    assert proposal.predicate == "owned_by_team"
    prow = await _proposal_row(factory, first.claim_id)
    assert prow["state"] == "open"
    row = await _claim_row(factory, first.claim_id)
    assert row["promotion_state"] == "proposed"

    # --- accept --------------------------------------------------------------
    promotion_id = await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_REVIEW_ROLES)
    row = await _claim_row(factory, first.claim_id)
    assert row["promotion_state"] == "promoted"
    prow = await _proposal_row(factory, first.claim_id)
    assert prow["state"] == "accepted"
    live = await _live_attribute(factory, subject, "owned_by_team")
    assert live is not None
    first_attr_id, first_value = live
    assert first_value == "platform-team"

    # --- visible in claim_serving with a citation -------------------------------
    # `ServedClaim` cannot be constructed without a citation -- this is the
    # module that owns the "no claim is served without its provenance" promise.
    served = await serving.get(ctx, first.claim_id)
    assert served is not None
    assert served.value == "platform-team"
    assert served.citations, "a served claim always carries at least one citation"
    assert served.citations[0].ref == "evt-1"

    found = await serving.query(ctx, ClaimQuery(subject_entity_id=subject, predicate="owned_by_team"))
    assert any(c.claim_id == first.claim_id for c in found)

    # --- a second claim, to give reversal something real to restore ------------
    # Same subject, same predicate, same authority tier (identical evidence
    # kind, same author) -- consolidation's tie-break between equally
    # authoritative claims is recency, so the later one supersedes the first
    # *in staging*. The canonical row it will go on to replace, once promoted,
    # is the one the first claim's own promotion wrote.
    claims_2 = ClaimService(factory, clock=FakeClock(_at(20)))
    consolidation_2 = ConsolidationService(factory, clock=FakeClock(_at(20)))
    promotion_2 = PromotionService(factory, claims=claims_2, clock=FakeClock(_at(20)))

    second = await claims_2.stage_claim(
        ctx,
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="growth-team",
        evidence=(Evidence(kind="session_event", ref="evt-2", excerpt="ownership moved to growth"),),
        asserted_valid_from=_at(20),
    )
    outcome_2 = await consolidation_2.consolidate(second.claim_id)
    assert outcome_2.decision == "update"
    assert outcome_2.superseded == (first.claim_id,)
    row = await _claim_row(factory, first.claim_id)
    assert row["status"] == "superseded"
    assert row["superseded_by"] == second.claim_id

    proposal_2 = await promotion_2.propose(second.claim_id)
    assert proposal_2 is not None
    promotion_id_2 = await promotion_2.accept(
        proposal_2.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_REVIEW_ROLES
    )
    live = await _live_attribute(factory, subject, "owned_by_team")
    assert live is not None
    second_attr_id, second_value = live
    assert second_value == "growth-team"
    assert second_attr_id != first_attr_id, "accept() must close the first row and write a new one, not mutate it"

    # --- reverse: restores the prior canonical state, not merely clears it -----
    await promotion_2.reverse(
        promotion_id_2,
        actor_tenant_id=tid,
        actor_id=aid,
        roles=_REVIEW_ROLES,
        reason="ownership claim retracted",
    )
    live = await _live_attribute(factory, subject, "owned_by_team")
    assert (
        live is not None
    ), "reversing the second promotion must restore the first promotion's row, not leave the slot empty"
    restored_attr_id, restored_value = live
    assert restored_attr_id == first_attr_id
    assert restored_value == "platform-team"
    row = await _claim_row(factory, second.claim_id)
    assert row["promotion_state"] == "reversed"

    # The first promotion is untouched by the second promotion's own reversal:
    # the first claim is still "promoted" (not "reversed"), and its own journal
    # row carries no reversal.
    row = await _claim_row(factory, first.claim_id)
    assert row["promotion_state"] == "promoted"
    assert await _journal_reversed_at(factory, promotion_id) is None
