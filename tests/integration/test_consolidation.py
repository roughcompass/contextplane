"""Reconciling claims: duplicates collapse, conflicts resolve by authority then recency.

The rule these tests exist to hold is that **authority beats recency**. Newest-wins is
the behaviour the design rejects, and it is also the behaviour any naive implementation
falls into, because the newest claim is the one being consolidated. So several tests
here stage the weaker claim *second* and assert it loses anyway.

Idempotence is the other load-bearing property. A sweep that re-decided every pass
would write an audit row per pass, and the log would record how often the sweep ran
rather than what it decided.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.audit import actions
from registry.service.claim_ontology import seed_ontology
from registry.service.claims import ClaimService, Evidence
from registry.service.confirmation import ConfirmationService
from registry.service.consolidation import (
    DECISION_ADD,
    DECISION_CONTESTED,
    DECISION_NOOP,
    DECISION_PROPOSAL,
    DECISION_UPDATE,
    REASON_CLUSTER_COLLAPSED,
    REASON_LOST_CONFLICT,
    ConsolidationService,
)
from registry.service.global_vocabulary import GlobalVocabularyService
from registry.types import FakeClock, TenantContext

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


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


@pytest.fixture
def claims(factory: async_sessionmaker[AsyncSession]) -> ClaimService:
    return ClaimService(factory, clock=FakeClock(_NOW))


@pytest.fixture
def consolidation(factory: async_sessionmaker[AsyncSession]) -> ConsolidationService:
    return ConsolidationService(factory, clock=FakeClock(_NOW))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"cons-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, *, kind: str = "human") -> uuid.UUID:
    aid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :kind, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "kind": kind, "now": _NOW},
        )
    return aid


async def _seed_entity(
    factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, *, visibility: str = "public"
) -> uuid.UUID:
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                "                      is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, :vis, TRUE, :now)"
            ),
            {"eid": eid, "tid": tid, "name": f"cap-{eid.hex[:8]}", "vis": visibility, "now": _NOW},
        )
    return eid


async def _seed_sync_run(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    source_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name, "
                "                          config, is_active, created_at) "
                "VALUES (:sid, :tid, 'openapi', 'src', '{}'::jsonb, TRUE, :now)"
            ),
            {"sid": source_id, "tid": tid, "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO sync_runs (sync_run_id, tenant_id, source_id, status, trigger, "
                "                       started_at) "
                "VALUES (:rid, :tid, :sid, 'done', 'manual', :now)"
            ),
            {"rid": run_id, "tid": tid, "sid": source_id, "now": _NOW},
        )
    return run_id


def _ctx(tid: uuid.UUID, aid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s")


def _at(offset_minutes: int) -> datetime.datetime:
    """A distinct instant. A frozen clock gives every claim the same `created_at`,
    which makes recency untestable -- and recency is half the resolution rule."""
    return _NOW + datetime.timedelta(minutes=offset_minutes)


async def _arrive(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    subject: uuid.UUID,
    *,
    at: int,
    predicate: str = "owned_by_team",
    value: object = "platform",
    evidence: tuple[Evidence, ...] = (Evidence(kind="session_event", ref="e1"),),
    **kw: object,
):
    """Stage a claim and reconcile it, as a sweep would on arrival.

    Consolidating on arrival matters: staging several claims and only then
    reconciling them lets whichever is processed first supersede the others, which is
    correct behaviour but tests something different from what a live pipeline does.
    """
    clock = FakeClock(_at(at))
    service = ClaimService(factory, clock=clock)
    claim = await service.stage_claim(
        TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s"),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=evidence,
        **kw,  # type: ignore[arg-type]
    )
    outcome = await ConsolidationService(factory, clock=clock).consolidate(claim.claim_id)
    return claim.claim_id, outcome


async def _row(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, t_invalidated_at, superseded_by, superseded_reason, "
                    "       consolidated_at, source_authority, is_contested "
                    "FROM lmm_claims WHERE claim_id = :cid"
                ),
                {"cid": claim_id},
            )
        ).one()
    return dict(row._mapping)


async def _audit_actions(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> list[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT action FROM audit_log WHERE target_id = :cid ORDER BY ts"),
                {"cid": claim_id},
            )
        ).all()
    return [r.action for r in rows]


async def _stage(
    claims: ClaimService,
    tid: uuid.UUID,
    aid: uuid.UUID,
    subject: uuid.UUID,
    *,
    predicate: str = "owned_by_team",
    value: object = "platform",
    evidence: tuple[Evidence, ...] = (Evidence(kind="session_event", ref="e1"),),
    **kw: object,
) -> uuid.UUID:
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=evidence,
        **kw,  # type: ignore[arg-type]
    )
    return claim.claim_id


# --- ADD ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_with_no_neighbourhood_is_added(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    claim, outcome = await _arrive(factory, tid, aid, subject, at=0)

    assert outcome.decision == DECISION_ADD
    assert (await _row(factory, claim))["status"] == "staged"


@pytest.mark.asyncio
async def test_a_second_value_of_a_set_valued_predicate_is_added_not_a_conflict(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """A capability is deployed in staging and production. Treating the second as a
    rival would make it lose a conflict it was never in."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    first, _ = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=0,
        predicate="deployment_environment",
        value="staging",
    )
    second, outcome = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=10,
        predicate="deployment_environment",
        value="production",
    )

    assert outcome.decision == DECISION_ADD
    assert (await _row(factory, first))["status"] == "staged"
    assert (await _row(factory, second))["status"] == "staged"


# --- NO-OP: exit criterion 1 --------------------------------------------------


@pytest.mark.asyncio
async def test_an_equivalent_claim_is_a_noop_and_leaves_one_survivor(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """The first exit criterion. Two claims saying the same thing must not become two
    rows -- otherwise every session that mentions a fact adds one."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    first, _ = await _arrive(factory, tid, aid, subject, at=0, value="platform")
    second, outcome = await _arrive(factory, tid, aid, subject, at=10, value="platform")

    assert outcome.decision == DECISION_NOOP
    assert (await _row(factory, first))["status"] == "staged", "the earlier claim survives"
    later = await _row(factory, second)
    assert later["status"] == "superseded"
    assert later["superseded_reason"] == REASON_CLUSTER_COLLAPSED
    assert later["superseded_by"] == first


@pytest.mark.asyncio
async def test_equivalence_under_folding_is_still_a_noop(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """ "Platform" and "platform" are one team, so the second phrasing adds nothing."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    await _arrive(factory, tid, aid, subject, at=0, value="Platform")
    _, outcome = await _arrive(factory, tid, aid, subject, at=10, value=" platform ")
    assert outcome.decision == DECISION_NOOP


@pytest.mark.asyncio
async def test_a_collapse_merges_provenance_onto_the_survivor(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """The point of collapsing rather than dropping: the survivor gains the knowledge
    that another source said the same thing, which is what raises corroboration."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    first, _ = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=0,
        evidence=(Evidence(kind="session_event", ref="first"),),
    )
    await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=10,
        evidence=(Evidence(kind="session_event", ref="second"),),
    )

    async with factory() as session:
        refs = {
            r.evidence_ref
            for r in (
                await session.execute(
                    text("SELECT evidence_ref FROM lmm_claim_provenance WHERE claim_id = :cid"),
                    {"cid": first},
                )
            ).all()
        }
    assert refs == {"first", "second"}


@pytest.mark.asyncio
async def test_a_collapse_is_recorded_with_how_it_was_matched(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """A reviewer checking a questionable collapse wants to know whether a typed
    comparison decided it or a similarity score did."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    first, _ = await _arrive(factory, tid, aid, subject, at=0)
    second, _ = await _arrive(factory, tid, aid, subject, at=10)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT matched_by, similarity FROM lmm_claim_cluster "
                    "WHERE survivor_claim_id = :s AND collapsed_claim_id = :c"
                ),
                {"s": first, "c": second},
            )
        ).one()
    assert row.matched_by == "exact_value"
    assert float(row.similarity) == 1.0


# --- authority beats recency: exit criterion 2 --------------------------------


@pytest.mark.asyncio
async def test_a_newer_weaker_claim_does_not_supersede_an_older_stronger_one(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """The second exit criterion, and the rule the whole design rests on. Newest-wins
    means a model's guess overwrites a published contract because it was observed this
    morning."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    run = await _seed_sync_run(factory, tid)

    strong, _ = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=0,
        value="platform",
        evidence=(Evidence(kind="connector_run", ref=str(run)),),
    )
    weak, outcome = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=10,
        value="billing",
        evidence=(Evidence(kind="session_event", ref="e9"),),
    )

    assert outcome.decision == DECISION_CONTESTED
    assert (await _row(factory, strong))["status"] == "staged", "the stronger claim survives"
    assert (await _row(factory, weak))["status"] == "staged", "the weaker one is not removed"
    assert "never displaces a stronger one" in outcome.reason


@pytest.mark.asyncio
async def test_a_stronger_claim_supersedes_an_older_weaker_one(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """The third exit criterion. The old claim is closed and still retrievable."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    run = await _seed_sync_run(factory, tid)

    weak, _ = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=0,
        value="billing",
        evidence=(Evidence(kind="session_event", ref="e9"),),
    )
    strong, outcome = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=10,
        value="platform",
        evidence=(Evidence(kind="connector_run", ref=str(run)),),
    )

    assert outcome.decision == DECISION_UPDATE
    closed = await _row(factory, weak)
    assert closed["status"] == "superseded"
    assert closed["t_invalidated_at"] is not None
    assert closed["superseded_by"] == strong
    assert closed["superseded_reason"] == REASON_LOST_CONFLICT
    assert (await _row(factory, strong))["status"] == "staged"


@pytest.mark.asyncio
async def test_recency_decides_among_equals(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """Authority first, recency second -- and second still means it decides when the
    first is a tie. Otherwise two sources of equal standing would deadlock forever."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    older, _ = await _arrive(factory, tid, aid, subject, at=0, value="billing")
    newer, outcome = await _arrive(factory, tid, aid, subject, at=10, value="platform")

    assert outcome.decision == DECISION_UPDATE
    assert (await _row(factory, older))["status"] == "superseded"


@pytest.mark.asyncio
async def test_a_superseded_claim_stays_retrievable(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """Nothing is deleted. The previous belief keeps its own score and provenance,
    which is what makes a mistaken supersession recoverable."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    older, _ = await _arrive(factory, tid, aid, subject, at=0, value="billing")
    await _arrive(factory, tid, aid, subject, at=10, value="platform")

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT value_jsonb, confidence, confidence_inputs " "FROM lmm_claims WHERE claim_id = :cid"),
                {"cid": older},
            )
        ).one()
    assert row.value_jsonb == "billing"
    assert row.confidence is not None
    assert row.confidence_inputs is not None


# --- cross-tenant: exit criterion 4 -------------------------------------------


@pytest.mark.asyncio
async def test_a_non_owner_conflict_is_routed_rather_than_resolved(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """The fourth exit criterion. A conflict about somebody else's capability is the
    owner's to settle. Gated on the tenant columns rather than on authority rank,
    because "different tenant" and "lower rank" have different consequences and one
    ordinal cannot say both."""
    owner_tid = await _seed_tenant(factory)
    owner_aid = await _seed_actor(factory, owner_tid)
    observer_tid = await _seed_tenant(factory)
    observer_aid = await _seed_actor(factory, observer_tid)
    subject = await _seed_entity(factory, owner_tid)

    owned, _ = await _arrive(factory, owner_tid, owner_aid, subject, at=0, value="platform")
    outside, outcome = await _arrive(factory, observer_tid, observer_aid, subject, at=10, value="billing")

    assert outcome.decision == DECISION_PROPOSAL
    assert (await _row(factory, owned))["status"] == "staged"
    assert (await _row(factory, outside))["status"] == "staged"
    assert actions.CLAIM_PROPOSAL_ROUTED in await _audit_actions(factory, outside)


@pytest.mark.asyncio
async def test_a_non_owner_claim_never_supersedes_even_at_a_human_tier(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """Standing dominates derivation. A human on a consuming team is a real source and
    still does not get to overwrite the owner."""
    owner_tid = await _seed_tenant(factory)
    owner_aid = await _seed_actor(factory, owner_tid)
    observer_tid = await _seed_tenant(factory)
    observer_human = await _seed_actor(factory, observer_tid, kind="human")
    subject = await _seed_entity(factory, owner_tid)

    owned, _ = await _arrive(
        factory,
        owner_tid,
        owner_aid,
        subject,
        at=0,
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    outside, outcome = await _arrive(
        factory,
        observer_tid,
        observer_human,
        subject,
        at=10,
        value="billing",
        evidence=(Evidence(kind="curator", ref=str(observer_human)),),
    )

    assert outcome.decision == DECISION_PROPOSAL
    assert (await _row(factory, owned))["status"] == "staged"


# --- confirmed claims: exit criterion 5 ---------------------------------------


@pytest.mark.asyncio
async def test_a_machine_claim_contests_a_confirmed_claim_without_superseding_it(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """The fifth exit criterion, through consolidation rather than through the
    authority comparison alone. No machine tier equals a human one, so the rank
    comparison already refuses -- this asserts the refusal survives the full path."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    machine = await _seed_actor(factory, tid, kind="sync_worker")
    subject = await _seed_entity(factory, tid)

    original, _ = await _arrive(factory, tid, human, subject, at=0, value="platform")
    confirmations = ConfirmationService(factory, claims, clock=FakeClock(_at(5)))
    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original)

    _, outcome = await _arrive(
        factory,
        tid,
        machine,
        subject,
        at=10,
        value="billing",
        evidence=(Evidence(kind="session_event", ref="e9"),),
    )

    assert outcome.decision == DECISION_CONTESTED
    assert (await _row(factory, confirmed.claim_id))["status"] == "staged"


# --- audit: exit criterion 7 --------------------------------------------------


@pytest.mark.asyncio
async def test_every_decision_writes_exactly_one_audit_row(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """Including the decision to do nothing. A sweep that recorded only its changes
    would be indistinguishable from a sweep that never ran."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    claim, _ = await _arrive(factory, tid, aid, subject, at=0)

    assert await _audit_actions(factory, claim) == [actions.CLAIM_CONSOLIDATED_ADD]


@pytest.mark.asyncio
async def test_each_decision_uses_its_own_action(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """A single "consolidated" action with the outcome in a payload would make "show
    me every supersession" a text search."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    added, _ = await _arrive(factory, tid, aid, subject, at=0, value="billing")
    updated, _ = await _arrive(factory, tid, aid, subject, at=10, value="platform")
    duplicate, _ = await _arrive(factory, tid, aid, subject, at=20, value="platform")

    assert await _audit_actions(factory, added) == [actions.CLAIM_CONSOLIDATED_ADD]
    assert await _audit_actions(factory, updated) == [actions.CLAIM_CONSOLIDATED_UPDATE]
    assert await _audit_actions(factory, duplicate) == [actions.CLAIM_CONSOLIDATED_NOOP]


# --- idempotence: NF5.2 -------------------------------------------------------


@pytest.mark.asyncio
async def test_re_running_over_an_unchanged_neighbourhood_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """Otherwise the audit log records how often the sweep ran rather than what it
    decided."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    claim, first = await _arrive(factory, tid, aid, subject, at=0)

    second = await consolidation.consolidate(claim)
    third = await consolidation.consolidate(claim)

    assert first.decision == DECISION_ADD
    assert second.already_settled and third.already_settled
    assert await _audit_actions(factory, claim) == [actions.CLAIM_CONSOLIDATED_ADD]


@pytest.mark.asyncio
async def test_re_running_does_not_drift_confidence(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    claim, _ = await _arrive(factory, tid, aid, subject, at=0)

    async with factory() as session:
        before = (
            await session.execute(text("SELECT confidence FROM lmm_claims WHERE claim_id = :cid"), {"cid": claim})
        ).scalar_one()
    for _ in range(3):
        await consolidation.consolidate(claim)
    async with factory() as session:
        after = (
            await session.execute(text("SELECT confidence FROM lmm_claims WHERE claim_id = :cid"), {"cid": claim})
        ).scalar_one()

    assert float(before) == float(after)


@pytest.mark.asyncio
async def test_a_newer_neighbour_makes_the_claim_reconsidered(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """Idempotence must not become one-shot. A claim arriving later genuinely changes
    the answer, and treating consolidation as done-once would leave a conflict
    undetected whenever the conflicting claim showed up second."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    run = await _seed_sync_run(factory, tid)
    # The first claim is the stronger one, so the later arrival contests rather than
    # supersedes -- which is the case where the earlier claim genuinely needs
    # reconsidering, because its own conflict state changed without it being touched.
    first, first_outcome = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=0,
        value="platform",
        evidence=(Evidence(kind="connector_run", ref=str(run)),),
    )

    assert first_outcome.decision == DECISION_ADD
    assert (await consolidation.consolidate(first)).already_settled

    await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=60,
        value="billing",
        evidence=(Evidence(kind="session_event", ref="e9"),),
    )
    later = ConsolidationService(factory, clock=FakeClock(_at(90)))
    reconsidered = await later.consolidate(first)

    assert not reconsidered.already_settled
    assert reconsidered.decision in {DECISION_CONTESTED, DECISION_UPDATE}


@pytest.mark.asyncio
async def test_consolidating_a_superseded_claim_does_nothing(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """A closed claim is not reconsidered. Otherwise a sweep would relitigate history
    every pass, and a claim could be closed twice in favour of different survivors."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    older, _ = await _arrive(factory, tid, aid, subject, at=0, value="billing")
    await _arrive(factory, tid, aid, subject, at=10, value="platform")

    before = await _row(factory, older)
    outcome = await consolidation.consolidate(older)
    after = await _row(factory, older)

    assert outcome.already_settled
    assert before == after


# --- the neighbourhood --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_closed_claim_is_excluded_from_the_neighbourhood(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """What makes a repeated sweep find nothing to do rather than reconsidering
    closed history."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    await _arrive(factory, tid, aid, subject, at=0, value="billing")
    second, _ = await _arrive(factory, tid, aid, subject, at=10, value="platform")

    third, outcome = await _arrive(factory, tid, aid, subject, at=20, value="platform")

    # It matches the live claim, not the closed one.
    assert outcome.decision == DECISION_NOOP
    assert outcome.collapsed == (second,)


@pytest.mark.asyncio
async def test_claims_about_different_subjects_are_not_neighbours(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    first_subject = await _seed_entity(factory, tid)
    second_subject = await _seed_entity(factory, tid)
    await _arrive(factory, tid, aid, first_subject, at=0, value="platform")
    _, outcome = await _arrive(factory, tid, aid, second_subject, at=10, value="billing")
    assert outcome.decision == DECISION_ADD


@pytest.mark.asyncio
async def test_an_unlinked_claim_is_not_consolidated(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """No subject means no neighbourhood, and such a claim is excluded from every
    other path too."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/unknown",
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )

    outcome = await consolidation.consolidate(claim.claim_id)
    assert outcome.already_settled


@pytest.mark.asyncio
async def test_a_clean_handover_is_not_a_conflict(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    consolidation: ConsolidationService,
    ontology: None,
) -> None:
    """Successive assertions, not competing ones. Contesting a handover would make
    every ownership change a conflict."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    handover = _NOW + datetime.timedelta(days=30)

    first, _ = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=0,
        value="platform",
        asserted_valid_from=_NOW,
        asserted_valid_to=handover,
    )
    second, outcome = await _arrive(
        factory,
        tid,
        aid,
        subject,
        at=10,
        value="billing",
        asserted_valid_from=handover,
    )

    assert outcome.decision == DECISION_ADD
    assert (await _row(factory, first))["status"] == "staged"
