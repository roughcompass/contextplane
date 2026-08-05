"""Human confirmation, and what a machine can and cannot do to it afterwards.

The rule that matters: a machine claim can contest a confirmed claim but cannot
supersede it. Contesting lowers both scores and routes the pair for review;
superseding replaces it. Only the first is available to model output, and that is
enforced by comparing authority ranks rather than by trusting a caller.

Confirmation supersedes rather than mutates, so the original keeps its score and its
provenance. That is what lets a reader see both what a machine estimated and what a
person then said — and it is why several tests here assert on the *old* row.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.exceptions import ConflictError
from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.memory.claim_ontology import seed_ontology
from registry.service.memory.claims import ClaimService, Evidence
from registry.service.memory.confidence import BUCKET_CONFIRMED, bucket_for
from registry.service.memory.confirmation import (
    VERDICT_CORRECT,
    VERDICT_UNDECIDABLE,
    ConfirmationService,
)
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_producer_ctx as _ctx
from tests.helpers.seeding import seed_entity as _seed_entity

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)
_EV = (Evidence(kind="session_event", ref="evt-1"),)


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
def confirmations(factory: async_sessionmaker[AsyncSession], claims: ClaimService) -> ConfirmationService:
    # The same claim service the tests stage through, because creating a claim is
    # its job whether the caller is extraction or a confirmation.
    return ConfirmationService(factory, claims, clock=FakeClock(_NOW))


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"cnf-{tid.hex[:8]}", "now": _NOW},
        )
    return tid


async def _seed_actor(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, *, kind: str) -> uuid.UUID:
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


async def _claim_row(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT confidence, source_authority, confidence_hold_until, "
                    "       confirms_claim_id, confirmed_by, superseded_by, is_contested "
                    "FROM memory_claims WHERE claim_id = :cid"
                ),
                {"cid": claim_id},
            )
        ).one()
    return dict(row._mapping)


# --- confirmation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_confirmation_raises_authority_to_the_human_tier(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """The fifth exit criterion's first part."""
    tid = await _seed_tenant(factory)
    machine = await _seed_actor(factory, tid, kind="sync_worker")
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, machine),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    assert original.source_authority == "owner_inference"

    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)
    assert confirmed.source_authority == "owner_human"


@pytest.mark.asyncio
async def test_a_confirmation_takes_the_confirmed_confidence_value(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)

    assert confirmed.bucket == BUCKET_CONFIRMED
    row = await _claim_row(factory, confirmed.claim_id)
    assert bucket_for(float(row["confidence"])) == BUCKET_CONFIRMED  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_confirmation_suspends_decay(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)

    row = await _claim_row(factory, confirmed.claim_id)
    assert row["confidence_hold_until"] is not None
    assert confirmed.hold_until > _NOW


@pytest.mark.asyncio
async def test_confirming_supersedes_rather_than_mutating(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """The original keeps its score and its authority. That is what lets a reader
    see both what a machine estimated and what a person then said — mutating in
    place would leave an audit record showing a claim that had always been
    confirmed."""
    tid = await _seed_tenant(factory)
    machine = await _seed_actor(factory, tid, kind="sync_worker")
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, machine),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    before = await _claim_row(factory, original.claim_id)
    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)
    after = await _claim_row(factory, original.claim_id)

    assert after["source_authority"] == before["source_authority"]
    assert float(after["confidence"]) == float(before["confidence"])  # type: ignore[arg-type]
    assert after["superseded_by"] == confirmed.claim_id


@pytest.mark.asyncio
async def test_the_confirmation_keeps_the_original_provenance_and_adds_a_human_act(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """What the machine saw is still why the claim was first made. The confirmation
    adds a human act on top rather than replacing the trail."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)

    async with factory() as session:
        kinds = {
            r.evidence_kind
            for r in (
                await session.execute(
                    text("SELECT evidence_kind FROM memory_claim_provenance WHERE claim_id = :cid"),
                    {"cid": confirmed.claim_id},
                )
            ).all()
        }
    assert "session_event" in kinds, "the original evidence must survive"
    assert "curator" in kinds, "the human act must be recorded"


@pytest.mark.asyncio
async def test_a_service_principal_cannot_confirm(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """Otherwise the human tier is reachable by any worker that calls this method,
    and the tier stops meaning that a person looked."""
    tid = await _seed_tenant(factory)
    worker = await _seed_actor(factory, tid, kind="sync_worker")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, worker),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    with pytest.raises(PermissionError, match="human principal"):
        await confirmations.confirm(_ctx(tid, worker), claim_id=original.claim_id)


@pytest.mark.asyncio
async def test_a_non_owner_confirmation_is_recorded_but_ranks_below_an_owner(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """A human on a consuming team reviewing somebody else's capability is real and
    worth recording. It still cannot outrank the owner."""
    owner_tid = await _seed_tenant(factory)
    observer_tid = await _seed_tenant(factory)
    owner_human = await _seed_actor(factory, owner_tid, kind="human")
    observer_human = await _seed_actor(factory, observer_tid, kind="human")
    subject = await _seed_entity(factory, owner_tid)

    original = await claims.stage_claim(
        _ctx(owner_tid, owner_human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    confirmed = await confirmations.confirm(_ctx(observer_tid, observer_human), claim_id=original.claim_id)
    assert confirmed.source_authority == "observer_human"


@pytest.mark.asyncio
async def test_an_already_superseded_claim_cannot_be_confirmed_again(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """Confirming a superseded claim would create a second live descendant of one
    assertion, and nothing downstream could tell which was current."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)

    with pytest.raises(ConflictError, match="already superseded"):
        await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)


@pytest.mark.asyncio
async def test_an_unlinked_claim_cannot_be_confirmed(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """There is nothing to confirm about a capability the catalog does not have.
    Linking it is the prior step."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")

    unlinked = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference="github:acme/unknown",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    with pytest.raises(ConflictError, match="no resolved subject"):
        await confirmations.confirm(_ctx(tid, human), claim_id=unlinked.claim_id)


# --- what a machine may do to a confirmed claim -------------------------------


@pytest.mark.asyncio
async def test_a_machine_claim_can_contest_a_confirmed_claim(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """The fifth exit criterion's second part. Contesting is available to anything:
    a disagreement is a fact about two claims, not a privilege."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    machine = await _seed_actor(factory, tid, kind="sync_worker")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)

    disagreeing = await claims.stage_claim(
        _ctx(tid, machine),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="billing",
        evidence=(Evidence(kind="session_event", ref="evt-2"),),
    )

    assert disagreeing.is_contested
    confirmed_row = await _claim_row(factory, confirmed.claim_id)
    assert confirmed_row["is_contested"] is True


@pytest.mark.asyncio
async def test_a_machine_claim_cannot_supersede_a_confirmed_one(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """Model output must not quietly overturn a human decision. Compared by
    authority rank, so adding a tier cannot accidentally create a pair that
    compares the wrong way."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)

    for machine_tier in ("owner_extraction", "owner_inference", "observer_inference"):
        assert not await confirmations.can_supersede(
            candidate_authority=machine_tier,
            incumbent_authority=confirmed.source_authority,
        ), machine_tier


@pytest.mark.asyncio
async def test_an_equal_authority_may_supersede(
    factory: async_sessionmaker[AsyncSession], confirmations: ConfirmationService
) -> None:
    """A later human decision replaces an earlier one. Requiring strictly higher
    would make a confirmed claim permanently unchangeable, including by the person
    who confirmed it."""
    assert await confirmations.can_supersede(candidate_authority="owner_human", incumbent_authority="owner_human")


@pytest.mark.asyncio
async def test_a_contested_confirmation_leaves_the_confirmed_bucket(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """A confirmed claim that is contested is not confirmed-and-uncontested, and
    the bucket should say so."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    original = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    confirmed = await confirmations.confirm(_ctx(tid, human), claim_id=original.claim_id)
    await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="billing",
        evidence=(Evidence(kind="session_event", ref="evt-2"),),
    )

    row = await _claim_row(factory, confirmed.claim_id)
    assert bucket_for(float(row["confidence"])) != BUCKET_CONFIRMED  # type: ignore[arg-type]


# --- judged outcomes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_judged_outcome_records_what_the_reviewer_saw(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """A score works out differently at a different instant, so calibrating against
    a number nobody looked at would measure the wrong thing."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    await confirmations.adjudicate(
        _ctx(tid, human),
        claim_id=claim.claim_id,
        verdict=VERDICT_CORRECT,
        observed_confidence=0.42,
    )

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT verdict, observed_confidence, observed_bucket, "
                    "       calibration_version, source_authority "
                    "FROM memory_claim_adjudication WHERE claim_id = :cid"
                ),
                {"cid": claim.claim_id},
            )
        ).one()

    assert row.verdict == VERDICT_CORRECT
    assert float(row.observed_confidence) == pytest.approx(0.42)
    assert row.observed_bucket == bucket_for(0.42)
    assert row.calibration_version == "uncalibrated"
    assert row.source_authority == "owner_inference"


@pytest.mark.asyncio
async def test_a_reviewer_judging_twice_corrects_rather_than_votes_twice(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """One person changing their mind is a correction. Two rows would let one
    reviewer weight a fit twice."""
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    for verdict in (VERDICT_CORRECT, VERDICT_UNDECIDABLE):
        await confirmations.adjudicate(
            _ctx(tid, human),
            claim_id=claim.claim_id,
            verdict=verdict,
            observed_confidence=0.42,
        )

    async with factory() as session:
        rows = (
            await session.execute(
                text("SELECT verdict FROM memory_claim_adjudication WHERE claim_id = :cid"),
                {"cid": claim.claim_id},
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].verdict == VERDICT_UNDECIDABLE


@pytest.mark.asyncio
async def test_two_reviewers_disagreeing_is_kept_as_two_outcomes(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    """Two people disagreeing about one claim is real signal about how hard the
    claim is to judge. Collapsing it would discard that."""
    tid = await _seed_tenant(factory)
    first = await _seed_actor(factory, tid, kind="human")
    second = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, first),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    await confirmations.adjudicate(
        _ctx(tid, first),
        claim_id=claim.claim_id,
        verdict=VERDICT_CORRECT,
        observed_confidence=0.42,
    )
    await confirmations.adjudicate(
        _ctx(tid, second),
        claim_id=claim.claim_id,
        verdict="incorrect",
        observed_confidence=0.42,
    )

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM memory_claim_adjudication WHERE claim_id = :cid"),
                {"cid": claim.claim_id},
            )
        ).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_an_unknown_verdict_is_refused(
    factory: async_sessionmaker[AsyncSession],
    claims: ClaimService,
    confirmations: ConfirmationService,
    ontology: None,
) -> None:
    tid = await _seed_tenant(factory)
    human = await _seed_actor(factory, tid, kind="human")
    subject = await _seed_entity(factory, tid)
    claim = await claims.stage_claim(
        _ctx(tid, human),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    with pytest.raises(ValueError, match="unknown verdict"):
        await confirmations.adjudicate(
            _ctx(tid, human),
            claim_id=claim.claim_id,
            verdict="probably",
            observed_confidence=0.5,
        )
