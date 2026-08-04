"""Staging becomes canonical here, and only through the tenant that owns the subject.

This is the first place a claim can change something outside the staging store, so the
tests are organised around the three properties that make that acceptable: nothing
consequential promotes without a person, nothing at all promotes automatically unless a
tenant opted in per predicate, and every promotion can be undone exactly.

The reversal tests are the sharp ones. "Restore the previous value" is not the same as
"restore the state that preceded this promotion", and the difference only shows up when
two promotions have stacked on one target -- so there is a test that stacks them.
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

from registry.audit import actions
from registry.security.pii_scanner import build_builtin_scanner
from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.memory import promotion_eligibility as elig
from registry.service.memory import promotion_targets
from registry.service.memory.claim_ontology import seed_ontology
from registry.service.memory.claims import ClaimService, Evidence
from registry.service.memory.confirmation import ConfirmationService
from registry.service.memory.consolidation import ConsolidationService
from registry.service.memory.curation_queue import (
    REASON_AWAITING_OWNER,
    REASON_UNLINKED,
    CurationQueueService,
)
from registry.service.memory.promotion import PromotionError, PromotionService
from registry.service.memory.promotion_guardrails import (
    BLOCKED_HIGH_IMPACT,
    BLOCKED_NOT_ALLOWLISTED,
    BLOCKED_NOT_OWNER,
    GuardrailService,
)
from registry.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)
_OWNER_ROLES = frozenset({"producer"})


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
def promotion(factory: async_sessionmaker[AsyncSession], claims: ClaimService) -> PromotionService:
    return PromotionService(factory, claims=claims, clock=FakeClock(_NOW))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"promo-{tid.hex[:8]}", "now": _NOW},
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


def _ctx(tid: uuid.UUID, aid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s")


async def _stage(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    subject: uuid.UUID,
    *,
    predicate: str = "owned_by_team",
    value: object = "platform",
    at: int = 0,
    **kw: object,
) -> uuid.UUID:
    """Stage a claim and reconcile it, which is what makes it promotable."""
    clock = FakeClock(_NOW + datetime.timedelta(minutes=at))
    claim = await ClaimService(factory, clock=clock).stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=(Evidence(kind="session_event", ref="e1"),),
        **kw,  # type: ignore[arg-type]
    )
    await ConsolidationService(factory, clock=clock).consolidate(claim.claim_id)
    return claim.claim_id


async def _attributes(
    factory: async_sessionmaker[AsyncSession], entity_id: uuid.UUID, key: str
) -> list[dict[str, object]]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT attr_id, value, t_valid_from, t_valid_to, t_invalidated_at "
                        "  FROM attributes WHERE entity_id = :eid AND key = :key "
                        " ORDER BY t_valid_from"
                    ),
                    {"eid": entity_id, "key": key},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _live_value(factory: async_sessionmaker[AsyncSession], entity_id: uuid.UUID, key: str) -> object:
    """What the graph says right now, by the same rule every reader uses."""
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


async def _value_as_of(
    factory: async_sessionmaker[AsyncSession],
    entity_id: uuid.UUID,
    key: str,
    as_of: datetime.datetime,
) -> object:
    """What the graph said at an instant, transaction-time aware.

    This is the query at issue: after a reversal, an `as_of` spanning the
    promotion must return what it returned before the promotion happened.
    """
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT value FROM attributes "
                    " WHERE entity_id = :eid AND key = :key "
                    "   AND t_valid_from <= :as_of "
                    "   AND (t_valid_to IS NULL OR t_valid_to > :as_of) "
                    "   AND t_invalidated_at IS NULL "
                    " ORDER BY t_valid_from DESC LIMIT 1"
                ),
                {"eid": entity_id, "key": key, "as_of": as_of},
            )
        ).first()
    return row[0] if row is not None else None


async def _audit_actions(factory: async_sessionmaker[AsyncSession], target: uuid.UUID) -> list[str]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT action FROM audit_log WHERE target_id = :t ORDER BY ts"),
                    {"t": target},
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


# --- the mapping ---------------------------------------------------------------


def test_every_promotable_predicate_has_exactly_one_target(ontology: None) -> None:
    """A predicate with two targets would write two different canonical shapes for
    one assertion, and nothing downstream could say which was meant."""
    for predicate, target in promotion_targets.TARGETS.items():
        assert target.kind in {"attribute", "edge"}, predicate
        assert target.key == predicate


def test_prose_has_no_canonical_target() -> None:
    """The one predicate the requirement bars by name. There is no typed canonical
    home for prose, so promoting it could only mean inventing one."""
    assert promotion_targets.target_for("session_summary") is None
    assert promotion_targets.unmapped_reason("session_summary") == promotion_targets.UNMAPPED_PROSE


def test_entity_references_become_edges_and_scalars_become_attributes() -> None:
    """The distinction the mapping exists to record: a relationship is not a
    property, however it is typed."""
    assert promotion_targets.TARGETS["depends_on"].kind == promotion_targets.TARGET_EDGE
    assert promotion_targets.TARGETS["owned_by_team"].kind == promotion_targets.TARGET_ATTRIBUTE


def test_a_predicate_outside_the_ontology_has_no_target() -> None:
    assert promotion_targets.target_for("invented_predicate") is None
    assert "not in the ontology" in str(promotion_targets.unmapped_reason("invented_predicate"))


# --- exit criterion 1: high-impact by consequence, never by confidence ---------


@pytest.mark.asyncio
async def test_a_deprecation_is_high_impact_and_confidence_is_not_why(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The inversion the requirement turns on. Certainty about a withdrawal is a
    reason to make sure somebody sees it, not a reason to skip review."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, predicate="lifecycle_state", value="deprecated")
    proposal = await promotion.propose(claim_id)

    assert proposal is not None
    assert proposal.high_impact
    assert elig.IMPACT_NARROWS_SURFACE in proposal.high_impact_reasons


@pytest.mark.asyncio
async def test_raising_confidence_does_not_change_the_classification(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Pins that confidence is not an input. Two identical claims differing only in
    score must classify identically -- otherwise the score is deciding review."""
    tid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    policy = elig.PromotionPolicy()

    base = {
        "predicate": "lifecycle_state",
        "value": "deprecated",
        "subject_entity_id": subject,
        "owning_tenant_id": tid,
        "author_tenant_id": tid,
        "claim_id": uuid.uuid4(),
        "is_contested": False,
    }
    async with factory() as session:
        low = await elig.assess_impact(session, {**base, "confidence": 0.01}, policy)
        high = await elig.assess_impact(session, {**base, "confidence": 0.99}, policy)

    assert low.reasons == high.reasons


@pytest.mark.asyncio
async def test_an_additive_claim_on_a_surface_predicate_is_not_high_impact(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The other half of the bias. If every claim touching a surface needed review,
    the queue would fill with routine additions and stop being read."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, predicate="lifecycle_state", value="ga")
    proposal = await promotion.propose(claim_id)

    assert proposal is not None
    assert not proposal.high_impact


@pytest.mark.asyncio
async def test_a_non_surface_predicate_reports_the_question_as_unasked(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """ "Not evaluated" and "evaluated, found safe" are different answers.

    Reporting the first as the second would claim a guarantee nobody checked.
    """
    tid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    async with factory() as session:
        assessment = await elig.assess_impact(
            session,
            {
                "predicate": "runbook_url",
                "value": "https://example/runbook",
                "subject_entity_id": subject,
                "owning_tenant_id": tid,
                "author_tenant_id": tid,
                "claim_id": uuid.uuid4(),
            },
            elig.PromotionPolicy(),
        )
    assert assessment.surface_evaluated is False
    assert not assessment.high_impact


# --- exit criterion 2: blast radius -------------------------------------------


@pytest.mark.asyncio
async def test_a_subject_many_things_depend_on_is_high_impact(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    async with factory() as session, session.begin():
        for _ in range(6):
            dependant = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, "
                    "                      visibility, is_active, created_at) "
                    "VALUES (:eid, :tid, 'capability', :n, 'public', TRUE, :now)"
                ),
                {"eid": dependant, "tid": tid, "n": f"dep-{dependant.hex[:8]}", "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO edges (edge_id, tenant_id, src_entity_id, rel, dst_entity_id, "
                    "                   t_valid_from, t_ingested_at) "
                    "VALUES (:e, :tid, :src, 'depends_on', :dst, :now, :now)"
                ),
                {"e": uuid.uuid4(), "tid": tid, "src": dependant, "dst": subject, "now": _NOW},
            )

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(claim_id)

    assert proposal is not None
    assert elig.IMPACT_BLAST_RADIUS in proposal.high_impact_reasons


# --- exit criterion 3: prose is ineligible but still useful --------------------


@pytest.mark.asyncio
async def test_a_session_summary_is_ineligible_but_still_readable(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """Ineligible is not rejected. The claim keeps serving; it simply has nowhere
    canonical to go."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, predicate="session_summary", value="we discussed the outage")

    assert await promotion.propose(claim_id) is None

    async with factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT status, promotion_state FROM memory_claims WHERE claim_id = :c"),
                    {"c": claim_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "staged", "an unpromotable claim is still a claim"
    assert row["promotion_state"] is None


# --- exit criterion 4: cross-tenant routes, never writes -----------------------


@pytest.mark.asyncio
async def test_a_claim_about_another_tenant_becomes_a_proposal_to_the_owner(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    owner = await _seed_tenant(factory)
    author = await _seed_tenant(factory)
    author_actor = await _seed_actor(factory, author)
    subject = await _seed_entity(factory, owner)

    claim_id = await _stage(factory, author, author_actor, subject, value="billing")
    proposal = await promotion.propose(claim_id)

    assert proposal is not None
    assert proposal.owner_tenant_id == owner
    assert proposal.author_tenant_id == author
    assert elig.IMPACT_CROSS_TENANT in proposal.high_impact_reasons
    assert actions.CLAIM_PROPOSAL_ROUTED in await _audit_actions(factory, claim_id)
    assert await _live_value(factory, subject, "owned_by_team") is None, "no write to their graph"


@pytest.mark.asyncio
async def test_an_actor_in_the_authoring_tenant_cannot_accept(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The whole point of routing. If the author could accept their own proposal,
    the proposal would be a formality rather than a request."""
    owner = await _seed_tenant(factory)
    author = await _seed_tenant(factory)
    author_actor = await _seed_actor(factory, author)
    subject = await _seed_entity(factory, owner)

    claim_id = await _stage(factory, author, author_actor, subject, value="billing")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None

    with pytest.raises(PromotionError, match="owns the subject"):
        await promotion.accept(
            proposal.proposal_id,
            actor_tenant_id=author,
            actor_id=author_actor,
            roles=_OWNER_ROLES,
        )


@pytest.mark.asyncio
async def test_the_right_tenant_with_the_wrong_role_is_also_refused(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """Tenant and role are two conditions, not one. Collapsing them would let the
    combination be satisfied by accident."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None

    with pytest.raises(PromotionError, match="producer or admin"):
        await promotion.accept(
            proposal.proposal_id,
            actor_tenant_id=tid,
            actor_id=aid,
            roles=frozenset({"consumer"}),
        )


# --- exit criterion 5: acceptance writes the graph -----------------------------


@pytest.mark.asyncio
async def test_accepting_writes_the_canonical_row_with_the_claims_interval(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The graph records when the fact holds, not when somebody got around to
    promoting it."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    asserted_from = _NOW - datetime.timedelta(days=30)

    claim_id = await _stage(factory, tid, aid, subject, value="platform", asserted_valid_from=asserted_from)
    proposal = await promotion.propose(claim_id)
    assert proposal is not None

    await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    rows = await _attributes(factory, subject, "owned_by_team")
    assert len(rows) == 1
    assert rows[0]["value"] == "platform"
    assert rows[0]["t_valid_from"] == asserted_from, "validity is the claim's, not the promotion's"

    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT promotion_state FROM memory_claims WHERE claim_id = :c"), {"c": claim_id}
            )
        ).scalar_one()
    assert state == "promoted"
    assert actions.CLAIM_PROMOTED in await _audit_actions(factory, claim_id)


@pytest.mark.asyncio
async def test_a_promotion_over_an_existing_value_closes_the_one_it_replaces(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    for index, team in enumerate(["platform", "billing"]):
        claim_id = await _stage(factory, tid, aid, subject, value=team, at=index * 10)
        proposal = await promotion.propose(claim_id)
        assert proposal is not None
        await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    assert await _live_value(factory, subject, "owned_by_team") == "billing"
    rows = await _attributes(factory, subject, "owned_by_team")
    assert len(rows) == 2, "the replaced row is closed, not deleted"
    assert rows[0]["t_valid_to"] is not None


# --- exit criterion 6: accept with amendment -----------------------------------


@pytest.mark.asyncio
async def test_an_amendment_promotes_the_corrected_value_and_records_both(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platfrom")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None

    await promotion.accept(
        proposal.proposal_id,
        actor_tenant_id=tid,
        actor_id=aid,
        roles=_OWNER_ROLES,
        amended_value="platform",
    )

    assert await _live_value(factory, subject, "owned_by_team") == "platform"
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT state, proposed_value, amended_value "
                        "  FROM memory_promotion_proposal WHERE proposal_id = :p"
                    ),
                    {"p": proposal.proposal_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["state"] == "amended"
    assert row["proposed_value"] == "platfrom", "what was proposed is still on the record"
    assert row["amended_value"] == "platform"


# --- exit criterion 7: rejection ------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejected_claim_stays_in_staging_with_its_reason(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None

    await promotion.reject(
        proposal.proposal_id,
        actor_tenant_id=tid,
        actor_id=aid,
        roles=_OWNER_ROLES,
        reason="incorrect",
    )

    async with factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT status, promotion_state FROM memory_claims WHERE claim_id = :c"),
                    {"c": claim_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "staged", "rejection does not delete the claim"
    assert row["promotion_state"] == "rejected"
    assert await _live_value(factory, subject, "owned_by_team") is None


@pytest.mark.asyncio
async def test_a_restatement_collapses_into_the_rejected_claim_rather_than_requeueing(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The first line of defence, which is consolidation rather than the rejection
    record: a restatement of a live claim is a duplicate and collapses into it.

    Named for what it actually holds. It passes even with the rejection record
    ignored, so it is not the test that covers repetition wearing down a refusal --
    the one below is.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    first = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(first)
    assert proposal is not None
    await promotion.reject(
        proposal.proposal_id,
        actor_tenant_id=tid,
        actor_id=aid,
        roles=_OWNER_ROLES,
        reason="incorrect",
    )

    again = await _stage(factory, tid, aid, subject, value="platform", at=20)
    assert await promotion.propose(again) is None, "the same assertion does not re-queue"


@pytest.mark.asyncio
async def test_a_rejected_assertion_stays_refused_after_something_else_replaces_it(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """Repetition must not wear down a refusal.

    While the rejected claim is live, a restatement simply collapses into it, so the
    rejection record is never consulted. The path that reaches it is the one where
    something *else* has since superseded the rejected claim: the neighbourhood no
    longer holds the refused value, so the next identical assertion is a clean,
    uncontested claim that would sail through on its own merits. Without the
    rejection record, an agent could revive any refused assertion by waiting for one
    unrelated change and then saying it again.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    refused = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(refused)
    assert proposal is not None
    await promotion.reject(
        proposal.proposal_id,
        actor_tenant_id=tid,
        actor_id=aid,
        roles=_OWNER_ROLES,
        reason="incorrect",
    )

    # Something unrelated supersedes it, so the refused value is no longer live.
    await _stage(factory, tid, aid, subject, value="billing", at=20)

    again = await _stage(factory, tid, aid, subject, value="platform", at=40)
    assert (
        await promotion.propose(again) is None
    ), "the rejection record is what refuses this; without it repetition would win"


@pytest.mark.asyncio
async def test_stronger_standing_can_revive_a_rejected_assertion(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The one way out, and the reason the suppression can be as strong as it is.

    Volume cannot overturn a rejection; better standing can. Without this a refusal
    of a weak inference would bar the owner from ever asserting the same thing
    themselves, which turns a review decision into a permanent veto.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    refused = await _stage(factory, tid, aid, subject, value="platform")
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE memory_claims SET source_authority = 'owner_inference' WHERE claim_id = :c"),
            {"c": refused},
        )
    proposal = await promotion.propose(refused)
    assert proposal is not None
    await promotion.reject(
        proposal.proposal_id,
        actor_tenant_id=tid,
        actor_id=aid,
        roles=_OWNER_ROLES,
        reason="incorrect",
    )

    await _stage(factory, tid, aid, subject, value="billing", at=20)

    stronger = await _stage(factory, tid, aid, subject, value="platform", at=40)
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE memory_claims SET source_authority = 'owner_human' WHERE claim_id = :c"),
            {"c": stronger},
        )

    assert await promotion.propose(stronger) is not None


@pytest.mark.asyncio
async def test_a_different_assertion_is_unaffected_by_an_earlier_rejection(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The other half: a rejection refuses one assertion, not the subject."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    first = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(first)
    assert proposal is not None
    await promotion.reject(
        proposal.proposal_id,
        actor_tenant_id=tid,
        actor_id=aid,
        roles=_OWNER_ROLES,
        reason="incorrect",
    )

    other = await _stage(factory, tid, aid, subject, value="billing", at=20)
    assert await promotion.propose(other) is not None


@pytest.mark.asyncio
async def test_a_rejection_reason_must_come_from_the_closed_vocabulary(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None

    with pytest.raises(PromotionError, match="rejection reason"):
        await promotion.reject(
            proposal.proposal_id,
            actor_tenant_id=tid,
            actor_id=aid,
            roles=_OWNER_ROLES,
            reason="because I said so",
        )


# --- exit criterion 8: reversal --------------------------------------------------


@pytest.mark.asyncio
async def test_reversing_restores_what_the_graph_said_before(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The property that makes machine-originated writes defensible: being wrong
    costs one audited operation."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    first = await _stage(factory, tid, aid, subject, value="platform")
    p1 = await promotion.propose(first)
    assert p1 is not None
    await promotion.accept(p1.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    second = await _stage(factory, tid, aid, subject, value="billing", at=10)
    p2 = await promotion.propose(second)
    assert p2 is not None
    promotion_id = await promotion.accept(p2.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)
    assert await _live_value(factory, subject, "owned_by_team") == "billing"

    await promotion.reverse(promotion_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES, reason="wrong")

    assert await _live_value(factory, subject, "owned_by_team") == "platform"


@pytest.mark.asyncio
async def test_an_as_of_query_after_a_reversal_matches_the_pre_promotion_answer(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """Reversal fidelity, verified rather than asserted. Restoring the value but not
    its interval would leave a gap an `as_of` query falls into."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    probe = _NOW + datetime.timedelta(minutes=5)

    first = await _stage(factory, tid, aid, subject, value="platform")
    p1 = await promotion.propose(first)
    assert p1 is not None
    await promotion.accept(p1.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    before = await _value_as_of(factory, subject, "owned_by_team", probe)

    second = await _stage(factory, tid, aid, subject, value="billing", at=10)
    p2 = await promotion.propose(second)
    assert p2 is not None
    promotion_id = await promotion.accept(p2.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)
    await promotion.reverse(promotion_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES, reason="wrong")

    after = await _value_as_of(factory, subject, "owned_by_team", probe)
    assert after == before, "the reversal left the earlier answer changed"


@pytest.mark.asyncio
async def test_reversing_an_older_promotion_under_a_newer_one_is_refused(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The case that separates "restore the previous value" from "restore the state
    that preceded this promotion".

    A changed B to C; a later promotion changed C to D. Writing B back now would
    silently destroy the later change, so the later one must be reversed first.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    ids: list[uuid.UUID] = []
    for index, team in enumerate(["platform", "billing", "search"]):
        claim_id = await _stage(factory, tid, aid, subject, value=team, at=index * 10)
        proposal = await promotion.propose(claim_id)
        assert proposal is not None
        ids.append(await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES))

    with pytest.raises(PromotionError, match="no longer live"):
        await promotion.reverse(ids[1], actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES, reason="wrong")

    assert await _live_value(factory, subject, "owned_by_team") == "search", "nothing was destroyed"


@pytest.mark.asyncio
async def test_reversing_twice_is_refused(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None
    promotion_id = await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    await promotion.reverse(promotion_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES, reason="wrong")
    with pytest.raises(PromotionError, match="already reversed"):
        await promotion.reverse(promotion_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES, reason="again")


@pytest.mark.asyncio
async def test_a_reversal_returns_the_claim_to_unpromoted_and_is_audited(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None
    promotion_id = await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)
    await promotion.reverse(promotion_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES, reason="wrong")

    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT promotion_state FROM memory_claims WHERE claim_id = :c"), {"c": claim_id}
            )
        ).scalar_one()
    assert state == "reversed"
    assert actions.CLAIM_PROMOTION_REVERSED in await _audit_actions(factory, claim_id)


@pytest.mark.asyncio
async def test_only_the_owning_tenant_may_reverse(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    other = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    stranger = await _seed_actor(factory, other)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None
    promotion_id = await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    with pytest.raises(PromotionError, match="only the owning tenant"):
        await promotion.reverse(
            promotion_id,
            actor_tenant_id=other,
            actor_id=stranger,
            roles=_OWNER_ROLES,
            reason="not mine",
        )


# --- eligibility ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconsolidated_claim_is_not_eligible(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, promotion: PromotionService, ontology: None
) -> None:
    """Promoting before reconciliation would canonicalise a claim that a duplicate or
    a stronger conflicting claim was about to settle."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    assert await promotion.propose(claim.claim_id) is None


@pytest.mark.asyncio
async def test_a_contested_claim_is_not_eligible(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    async with factory() as session, session.begin():
        await session.execute(text("UPDATE memory_claims SET is_contested = TRUE WHERE claim_id = :c"), {"c": claim_id})

    assert await promotion.propose(claim_id) is None


@pytest.mark.asyncio
async def test_every_blocking_reason_is_reported_not_only_the_first(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """A curator fixing one blocker only to be shown the next is how a queue stops
    being worked."""
    assessment = elig.assess_eligibility(
        {
            "status": "unlinked",
            "subject_entity_id": None,
            "predicate": "session_summary",
            "consolidated_at": None,
            "is_contested": True,
        },
        elig.PromotionPolicy(),
    )
    assert not assessment.eligible
    assert elig.INELIGIBLE_UNLINKED in assessment.reasons
    assert elig.INELIGIBLE_CONTESTED in assessment.reasons
    assert elig.INELIGIBLE_NO_TARGET in assessment.reasons


@pytest.mark.asyncio
async def test_a_claim_below_the_tenant_floor_is_not_eligible(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO memory_promotion_policy (tenant_id, confidence_floor) " "VALUES (:tid, 0.99)"),
            {"tid": tid},
        )

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    assert await promotion.propose(claim_id) is None


@pytest.mark.asyncio
async def test_one_claim_cannot_have_two_open_proposals(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """Two reviewers deciding the same thing differently, with the second write
    silently winning, is the failure this prevents."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    assert await promotion.propose(claim_id) is not None
    assert await promotion.propose(claim_id) is None, "already proposed"


# --- the proposal artefact ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_proposal_states_the_current_value_and_the_proposed_one(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """A reviewer decides from the proposal alone. Without the current value they
    cannot see what would change."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    first = await _stage(factory, tid, aid, subject, value="platform")
    p1 = await promotion.propose(first)
    assert p1 is not None
    assert p1.current_value is None, "nothing there yet"
    await promotion.accept(p1.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    second = await _stage(factory, tid, aid, subject, value="billing", at=10)
    p2 = await promotion.propose(second)
    assert p2 is not None
    assert p2.current_value == "platform"
    assert p2.proposed_value == "billing"


@pytest.mark.asyncio
async def test_the_proposal_records_the_mapping_version_it_used(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """A mapping later found wrong has to be traceable to exactly the rows written
    under it."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None

    async with factory() as session:
        version = (
            await session.execute(
                text("SELECT mapping_version FROM memory_promotion_proposal WHERE proposal_id = :p"),
                {"p": proposal.proposal_id},
            )
        ).scalar_one()
    assert version == promotion_targets.MAPPING_VERSION


@pytest.mark.asyncio
async def test_all_high_impact_reasons_are_listed_not_just_the_first(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """A reviewer needs every reason. A classifier that stopped at the first would
    hide the rest behind whichever happened to be checked earliest."""
    owner = await _seed_tenant(factory)
    author = await _seed_tenant(factory)
    author_actor = await _seed_actor(factory, author)
    subject = await _seed_entity(factory, owner)
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO memory_promotion_policy (tenant_id, always_review) " "VALUES (:tid, CAST(:ar AS JSONB))"),
            {"tid": owner, "ar": json.dumps(["lifecycle_state"])},
        )

    claim_id = await _stage(factory, author, author_actor, subject, predicate="lifecycle_state", value="deprecated")
    proposal = await promotion.propose(claim_id)

    assert proposal is not None
    assert elig.IMPACT_NARROWS_SURFACE in proposal.high_impact_reasons
    assert elig.IMPACT_CROSS_TENANT in proposal.high_impact_reasons
    assert elig.IMPACT_ALWAYS_REVIEW in proposal.high_impact_reasons


@pytest.mark.asyncio
async def test_superseding_a_human_confirmation_is_high_impact(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    promotion: PromotionService,
    ontology: None,
) -> None:
    """A successor to something a human vouched for still needs a human.

    Written as a clean handover -- the first claim's interval ends where the second
    begins -- because a claim that *contradicts* a confirmation is contested, and a
    contested claim is ineligible before this check is ever reached. The case that
    actually survives to classification is the orderly one: nobody disagrees, and
    promoting it would still overwrite what the confirmation established.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    handover = _NOW + datetime.timedelta(days=30)

    original = await _stage(
        factory,
        tid,
        aid,
        subject,
        value="platform",
        asserted_valid_from=_NOW,
        asserted_valid_to=handover,
    )
    confirmation = ConfirmationService(factory, claims=claims, clock=FakeClock(_NOW))
    await confirmation.confirm(_ctx(tid, aid), claim_id=original)

    later = await _stage(factory, tid, aid, subject, value="billing", at=20, asserted_valid_from=handover)
    proposal = await promotion.propose(later)

    assert proposal is not None, "a handover is not a disagreement, so it stays eligible"
    assert elig.IMPACT_SUPERSEDES_CONFIRMED in proposal.high_impact_reasons


# --- edges ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_entity_reference_promotes_to_an_edge(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """A relationship is not a property, however it is typed."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    other = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, predicate="depends_on", value=str(other))
    proposal = await promotion.propose(claim_id)
    assert proposal is not None
    assert proposal.target_kind == promotion_targets.TARGET_EDGE

    await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT dst_entity_id FROM edges "
                    " WHERE src_entity_id = :s AND rel = 'depends_on' AND t_invalidated_at IS NULL"
                ),
                {"s": subject},
            )
        ).first()
    assert row is not None
    assert row[0] == other


@pytest.mark.asyncio
async def test_an_edge_is_never_written_across_a_tenant_boundary(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The owner consented to a claim about their capability. They cannot consent on
    behalf of the tenant at the other end of the edge."""
    tid = await _seed_tenant(factory)
    other_tenant = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    foreign = await _seed_entity(factory, other_tenant)

    claim_id = await _stage(factory, tid, aid, subject, predicate="depends_on", value=str(foreign))
    proposal = await promotion.propose(claim_id)
    assert proposal is not None

    with pytest.raises(PromotionError, match="tenant boundary"):
        await promotion.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)


# --- exit criterion 9: the default posture ---------------------------------------


@pytest.mark.asyncio
async def test_a_fresh_tenant_has_an_empty_allowlist(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    """No seed row, no default predicate, no global switch. A deployment that
    promoted anything before somebody asked would make the safe posture depend on an
    operator knowing to turn it off."""
    tid = await _seed_tenant(factory)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))
    assert await guardrails.allowlist_for(tid) == frozenset()


@pytest.mark.asyncio
async def test_a_fully_eligible_claim_still_does_not_auto_promote(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Eligible, uncontested, owner-originated, not high-impact -- and it still
    waits, because nobody opted its predicate in."""
    tid = await _seed_tenant(factory)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))

    decision = await guardrails.may_auto_promote(
        tenant_id=tid,
        predicate="owned_by_team",
        high_impact=False,
        eligible=True,
        author_is_owner=True,
    )

    assert not decision.permitted
    assert BLOCKED_NOT_ALLOWLISTED in decision.blocked_by


@pytest.mark.asyncio
async def test_allowlisting_a_predicate_permits_it_and_is_audited(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))

    await guardrails.allow(tid, "owned_by_team", actor_id=aid)

    decision = await guardrails.may_auto_promote(
        tenant_id=tid,
        predicate="owned_by_team",
        high_impact=False,
        eligible=True,
        author_is_owner=True,
    )
    assert decision.permitted
    assert actions.CLAIM_AUTOPROMOTE_ALLOWED in await _audit_actions(factory, tid)


@pytest.mark.asyncio
async def test_allowlisting_one_predicate_does_not_permit_another(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """An entry names one predicate. An entry that widened past its own name would
    make the allowlist a switch."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))
    await guardrails.allow(tid, "owned_by_team", actor_id=aid)

    decision = await guardrails.may_auto_promote(
        tenant_id=tid,
        predicate="runbook_url",
        high_impact=False,
        eligible=True,
        author_is_owner=True,
    )
    assert not decision.permitted


@pytest.mark.asyncio
async def test_an_allowlisted_predicate_still_does_not_auto_promote_when_high_impact(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """The condition that no configuration can switch off. If the allowlist could
    override it, the classification would be advisory."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))
    await guardrails.allow(tid, "lifecycle_state", actor_id=aid)

    decision = await guardrails.may_auto_promote(
        tenant_id=tid,
        predicate="lifecycle_state",
        high_impact=True,
        eligible=True,
        author_is_owner=True,
    )
    assert not decision.permitted
    assert BLOCKED_HIGH_IMPACT in decision.blocked_by


@pytest.mark.asyncio
async def test_an_allowlisted_predicate_from_a_non_owner_does_not_auto_promote(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))
    await guardrails.allow(tid, "owned_by_team", actor_id=aid)

    decision = await guardrails.may_auto_promote(
        tenant_id=tid,
        predicate="owned_by_team",
        high_impact=False,
        eligible=True,
        author_is_owner=False,
    )
    assert not decision.permitted
    assert BLOCKED_NOT_OWNER in decision.blocked_by


@pytest.mark.asyncio
async def test_revoking_an_entry_is_audited_and_takes_effect(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))
    await guardrails.allow(tid, "owned_by_team", actor_id=aid)
    await guardrails.revoke(tid, "owned_by_team", actor_id=aid)

    assert await guardrails.allowlist_for(tid) == frozenset()
    assert actions.CLAIM_AUTOPROMOTE_REVOKED in await _audit_actions(factory, tid)


@pytest.mark.asyncio
async def test_the_allowlist_is_per_tenant(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    """One tenant opting in must not decide for another."""
    first = await _seed_tenant(factory)
    second = await _seed_tenant(factory)
    aid = await _seed_actor(factory, first)
    guardrails = GuardrailService(factory, clock=FakeClock(_NOW))
    await guardrails.allow(first, "owned_by_team", actor_id=aid)

    assert await guardrails.allowlist_for(second) == frozenset()


def test_a_wildcard_entry_is_rejected_by_the_schema() -> None:
    """A wildcard is how an allowlist stops being one. Enforced in the database so
    it holds for any writer, not only this service."""
    # The CHECK constraint is the enforcement; this test names it so that removing
    # it fails here rather than silently permitting '*'.
    from pathlib import Path

    migration = Path("registry/storage/migrations/versions/0001_baseline_schema.py").read_text()
    assert "predicate <> '*'" in migration


# --- the curation queue ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unlinked_claim_reaches_the_queue_with_the_right_actions(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)

    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="something nobody can resolve",
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )

    items = await CurationQueueService(factory).items_for(tid)
    assert [i.reason for i in items] == [REASON_UNLINKED]
    assert "link" in items[0].available_actions


@pytest.mark.asyncio
async def test_a_high_impact_proposal_waits_for_its_owner_and_offers_no_accept(
    factory: async_sessionmaker[AsyncSession], promotion: PromotionService, ontology: None
) -> None:
    """The queue does not offer accept or reject. Those belong to the owner's review
    path, which checks tenancy and role -- a second door on that decision would be a
    way around the check."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)

    claim_id = await _stage(factory, tid, aid, subject, predicate="lifecycle_state", value="deprecated")
    proposal = await promotion.propose(claim_id)
    assert proposal is not None and proposal.high_impact

    items = await CurationQueueService(factory).items_for(tid)
    awaiting = [i for i in items if i.reason == REASON_AWAITING_OWNER]
    assert len(awaiting) == 1
    assert awaiting[0].proposal_id == proposal.proposal_id
    assert "accept" not in awaiting[0].available_actions
    assert "reject" not in awaiting[0].available_actions


@pytest.mark.asyncio
async def test_the_queue_distinguishes_machine_extraction_from_human_authorship(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The audit the curator role exists to perform. A queue showing only the value
    would make a machine's guess and a person's decision look identical.

    Read from the claim's own authority tier rather than from whether a confirmation
    points at it: a confirmation supersedes what it confirms, so a "has been
    confirmed" flag would be false on every row still in the queue.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)

    # Linked subjects, because an unlinked claim has no owner to derive owner-tier
    # authority from -- the schema refuses that combination outright.
    by_hand = await _seed_entity(factory, tid)
    by_model = await _seed_entity(factory, tid)

    for subject, authority in ((by_hand, "owner_human"), (by_model, "owner_extraction")):
        claim_id = await _stage(factory, tid, aid, subject, value="platform")
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE memory_claims SET source_authority = :a, is_contested = TRUE " " WHERE claim_id = :c"),
                {"a": authority, "c": claim_id},
            )

    items = await CurationQueueService(factory).items_for(tid)
    backing = {i.subject_entity_id: i.human_backed for i in items}
    assert backing == {by_hand: True, by_model: False}


@pytest.mark.asyncio
async def test_one_claim_appears_once_however_many_reasons_apply(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A queue that listed a claim under every applicable heading would show more
    work than exists."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="unresolvable",
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE memory_claims SET is_contested = TRUE WHERE claim_id = :c"),
            {"c": claim.claim_id},
        )

    items = await CurationQueueService(factory).items_for(tid)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_the_queue_is_scoped_to_one_tenant(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    first = await _seed_tenant(factory)
    second = await _seed_tenant(factory)
    aid = await _seed_actor(factory, first)

    await claims.stage_claim(
        _ctx(first, aid),
        subject_reference="unresolvable",
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )

    assert await CurationQueueService(factory).items_for(second) == ()


@pytest.mark.asyncio
async def test_counts_report_each_kind_separately(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """ "There are 40 things" is not a queue anybody works."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)

    for index in range(2):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=f"unresolvable-{index}",
            predicate="owned_by_team",
            value="platform",
            evidence=(Evidence(kind="session_event", ref="e1"),),
        )

    counts = await CurationQueueService(factory).counts_for(tid)
    assert counts == {REASON_UNLINKED: 2}


# --- the PII boundary -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_value_carrying_pii_is_refused_at_the_canonical_boundary(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Scanned on the way into the graph, not on the way into staging.

    A claim may legitimately carry an account number while it is a private
    observation about one tenant's session. What it must not do is cross into the
    shared graph, where a different audience reads it under different rules -- so
    the check belongs at the boundary being crossed.
    """
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    blocking = build_builtin_scanner(tenant_policy="block")
    service = PromotionService(
        factory,
        claims=ClaimService(factory, clock=FakeClock(_NOW)),
        clock=FakeClock(_NOW),
        pii_scanner=blocking,
    )

    claim_id = await _stage(factory, tid, aid, subject, predicate="escalation_contact", value="oncall@example.com")
    proposal = await service.propose(claim_id)
    assert proposal is not None, "staging is unaffected -- the claim is still a claim"

    with pytest.raises(PromotionError, match="canonical graph"):
        await service.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)

    assert await _live_value(factory, subject, "escalation_contact") is None


@pytest.mark.asyncio
async def test_an_amendment_is_scanned_not_only_the_proposed_value(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """A reviewer must not be able to introduce PII the claim never carried. A path
    that scanned only what the machine proposed would let exactly that through."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    blocking = build_builtin_scanner(tenant_policy="block")
    service = PromotionService(
        factory,
        claims=ClaimService(factory, clock=FakeClock(_NOW)),
        clock=FakeClock(_NOW),
        pii_scanner=blocking,
    )

    claim_id = await _stage(factory, tid, aid, subject, predicate="escalation_contact", value="platform-oncall")
    proposal = await service.propose(claim_id)
    assert proposal is not None

    with pytest.raises(PromotionError, match="canonical graph"):
        await service.accept(
            proposal.proposal_id,
            actor_tenant_id=tid,
            actor_id=aid,
            roles=_OWNER_ROLES,
            amended_value="oncall@example.com",
        )


@pytest.mark.asyncio
async def test_ordinary_values_are_not_blocked(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    """The other half of the bias. A scanner that blocked ordinary team names would
    make promotion unusable, and an unusable control gets removed."""
    tid = await _seed_tenant(factory)
    aid = await _seed_actor(factory, tid)
    subject = await _seed_entity(factory, tid)
    service = PromotionService(
        factory,
        claims=ClaimService(factory, clock=FakeClock(_NOW)),
        clock=FakeClock(_NOW),
        pii_scanner=build_builtin_scanner(tenant_policy="block"),
    )

    claim_id = await _stage(factory, tid, aid, subject, value="platform")
    proposal = await service.propose(claim_id)
    assert proposal is not None
    await service.accept(proposal.proposal_id, actor_tenant_id=tid, actor_id=aid, roles=_OWNER_ROLES)
    assert await _live_value(factory, subject, "owned_by_team") == "platform"
