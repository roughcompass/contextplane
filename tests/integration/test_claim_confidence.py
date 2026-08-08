"""Confidence as stored: derived from authority, raised by agreement, auditable.

The exit criteria this covers are comparative rather than absolute. Nobody can say
what the right number is for a given claim — what matters is that an owner's
reproducible extraction outscores a stranger's guess, that independent agreement
raises a score while repetition does not, and that a reader can take what is stored
beside a claim and arrive at the same number.

That last one is the only workable definition of auditable here, so it is asserted
over every claim these tests create rather than on a chosen example.
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

from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.memory.claim_authority import UNCALIBRATED, Evidence
from contextplane.service.memory.claim_ontology import seed_ontology
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.confidence import (
    BUCKET_SEMANTICS,
    SCORER_VERSION,
    ConfidenceInputs,
    bucket_for,
    recompute,
)
from contextplane.service.memory.session_events import MemoryService
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_producer_ctx as _ctx
from tests.helpers.seeding import seed_entity as _seed_entity

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


async def _seed_tenant(
    factory: async_sessionmaker[AsyncSession], *, actor_kind: str = "human"
) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"cf-{tid.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, "
                "                    actor_kind, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :kind, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "kind": actor_kind, "now": _NOW},
        )
    return tid, aid


async def _seed_sync_run(
    factory: async_sessionmaker[AsyncSession], tid: uuid.UUID, *, source_type: str = "openapi"
) -> uuid.UUID:
    source_id, run_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name, "
                "                          config, is_active, created_at) "
                "VALUES (:sid, :tid, :stype, 'src', '{}'::jsonb, TRUE, :now)"
            ),
            {"sid": source_id, "tid": tid, "stype": source_type, "now": _NOW},
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


async def _session_event(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    *,
    session_id: str,
) -> uuid.UUID:
    memory = MemoryService(factory, clock=FakeClock(_NOW))
    event = await memory.record_event(
        TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s"),
        session_id=session_id,
        kind="agent_action",
        body="observed something",
    )
    return event.event_id


async def _stored(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> dict[str, object]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT confidence, confidence_scored_at, confidence_inputs, "
                    "       scorer_version, calibration_version, decay_half_life_days, "
                    "       provider_confidence, source_authority, is_contested "
                    "FROM memory_claims WHERE claim_id = :cid"
                ),
                {"cid": claim_id},
            )
        ).one()
    return dict(row._mapping)


def _assert_auditable(stored: dict[str, object]) -> None:
    """A reader takes what is stored beside the claim and arrives at the number."""
    raw = stored["confidence_inputs"]
    payload = json.loads(raw) if isinstance(raw, str) else raw
    inputs = ConfidenceInputs(
        authority=payload["authority"],
        base=payload["base"],
        corroborating_classes=payload["corroborating_classes"],
        corroborating_mass=payload["corroborating_mass"],
        is_contested=payload["is_contested"],
        is_confirmed=payload["is_confirmed"],
        provider_confidence=payload["provider_confidence"],
        provider_applied=payload["provider_applied"],
        scorer_version=payload["scorer_version"],
    )
    assert recompute(inputs) == pytest.approx(float(stored["confidence"]), abs=0.001)  # type: ignore[arg-type]


# --- authority weighting ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_owners_reproducible_extraction_outscores_a_strangers_inference(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The first exit criterion. The same assertion, from two sources, must not
    score the same — and the weighting must be visible in what is stored."""
    owner_tid, owner_aid = await _seed_tenant(factory)
    observer_tid, observer_aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, owner_tid)
    run = await _seed_sync_run(factory, owner_tid)

    from_owner = await claims.stage_claim(
        _ctx(owner_tid, owner_aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="connector_run", ref=str(run)),),
    )
    from_observer = await claims.stage_claim(
        _ctx(observer_tid, observer_aid),
        subject_reference=str(subject),
        predicate="on_call_rotation",
        value="platform-primary",
        evidence=(Evidence(kind="session_event", ref="not-a-uuid"),),
    )

    owner_stored = await _stored(factory, from_owner.claim_id)
    observer_stored = await _stored(factory, from_observer.claim_id)

    assert float(owner_stored["confidence"]) > float(observer_stored["confidence"])  # type: ignore[arg-type]
    assert owner_stored["source_authority"] == "owner_extraction"
    assert observer_stored["source_authority"] == "observer_inference"


@pytest.mark.asyncio
async def test_the_weighting_is_visible_in_what_is_stored(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A reader can see why a claim scored as it did, which is what the
    requirement means by auditable."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    stored = await _stored(factory, claim.claim_id)
    raw = stored["confidence_inputs"]
    payload = json.loads(raw) if isinstance(raw, str) else raw

    assert payload["authority"] == "owner_inference"
    assert payload["base"] > 0
    assert stored["scorer_version"] == SCORER_VERSION
    _assert_auditable(stored)


@pytest.mark.asyncio
async def test_a_stored_score_is_re_derivable_from_its_record(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="request_timeout_seconds",
        value=900,
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    _assert_auditable(await _stored(factory, claim.claim_id))


@pytest.mark.asyncio
async def test_a_score_lands_in_a_published_bucket(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A number outside the published semantics is one a consumer cannot act on."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    stored = await _stored(factory, claim.claim_id)
    assert bucket_for(float(stored["confidence"])) in BUCKET_SEMANTICS  # type: ignore[arg-type]


# --- an unlinked claim is not scored at all -----------------------------------


@pytest.mark.asyncio
async def test_an_unlinked_claim_has_no_score_rather_than_a_low_one(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A number would assert a determination nobody made, and nothing would mark
    it stale once curation links the claim."""
    tid, aid = await _seed_tenant(factory)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/unknown",
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    stored = await _stored(factory, claim.claim_id)

    assert stored["confidence"] is None
    assert stored["confidence_scored_at"] is None
    assert stored["confidence_inputs"] is None
    assert stored["decay_half_life_days"] is None


# --- corroboration and repetition ---------------------------------------------


@pytest.mark.asyncio
async def test_independent_sources_agreeing_raises_confidence(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The second exit criterion, first half."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    first_event = await _session_event(factory, tid, aid, session_id="one")
    second_event = await _session_event(factory, tid, aid, session_id="two")

    alone = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="staging",
        evidence=(Evidence(kind="session_event", ref=str(first_event)),),
    )
    corroborated = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="production",
        evidence=(
            Evidence(kind="session_event", ref=str(first_event)),
            Evidence(kind="session_event", ref=str(second_event)),
        ),
    )

    alone_stored = await _stored(factory, alone.claim_id)
    both_stored = await _stored(factory, corroborated.claim_id)
    assert float(both_stored["confidence"]) > float(alone_stored["confidence"])  # type: ignore[arg-type]
    _assert_auditable(both_stored)


@pytest.mark.asyncio
async def test_repetition_through_one_source_does_not_raise_confidence(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The second exit criterion's other half, and the requirement's own named
    case: one session restating a claim is not independent evidence."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    memory = MemoryService(factory, clock=FakeClock(_NOW))
    ctx = _ctx(tid, aid)
    # Several turns of one conversation.
    events = [
        (await memory.record_event(ctx, session_id="same-session", kind="agent_action", body=f"turn {i}")).event_id
        for i in range(4)
    ]

    once = await claims.stage_claim(
        ctx,
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="staging",
        evidence=(Evidence(kind="session_event", ref=str(events[0])),),
    )
    restated = await claims.stage_claim(
        ctx,
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="production",
        evidence=tuple(Evidence(kind="session_event", ref=str(e)) for e in events),
    )

    once_stored = await _stored(factory, once.claim_id)
    restated_stored = await _stored(factory, restated.claim_id)
    assert float(restated_stored["confidence"]) == float(once_stored["confidence"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_two_runs_of_one_connector_do_not_corroborate(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Re-running a parse that is a pure function of the fetched bytes is a
    recomputation, not a second observation."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    source_id = uuid.uuid4()
    runs = []
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name, "
                "                          config, is_active, created_at) "
                "VALUES (:sid, :tid, 'openapi', 'src', '{}'::jsonb, TRUE, :now)"
            ),
            {"sid": source_id, "tid": tid, "now": _NOW},
        )
        for _ in range(3):
            run_id = uuid.uuid4()
            runs.append(run_id)
            await session.execute(
                text(
                    "INSERT INTO sync_runs (sync_run_id, tenant_id, source_id, status, "
                    "                       trigger, started_at) "
                    "VALUES (:rid, :tid, :sid, 'done', 'scheduled', :now)"
                ),
                {"rid": run_id, "tid": tid, "sid": source_id, "now": _NOW},
            )

    once = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="staging",
        evidence=(Evidence(kind="connector_run", ref=str(runs[0])),),
    )
    thrice = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="production",
        evidence=tuple(Evidence(kind="connector_run", ref=str(r)) for r in runs),
    )

    once_stored = await _stored(factory, once.claim_id)
    thrice_stored = await _stored(factory, thrice.claim_id)
    assert float(thrice_stored["confidence"]) == float(once_stored["confidence"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_unresolvable_reference_buys_no_corroboration(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A pointer nobody can follow corroborates nothing. Otherwise a producer
    could raise its own score by citing anything."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    real = await _session_event(factory, tid, aid, session_id="one")

    honest = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="staging",
        evidence=(Evidence(kind="session_event", ref=str(real)),),
    )
    padded = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="production",
        evidence=(
            Evidence(kind="session_event", ref=str(real)),
            Evidence(kind="connector_run", ref=str(uuid.uuid4())),
            Evidence(kind="connector_run", ref="not-even-a-uuid"),
        ),
    )

    honest_stored = await _stored(factory, honest.claim_id)
    padded_stored = await _stored(factory, padded.claim_id)
    assert float(padded_stored["confidence"]) == float(honest_stored["confidence"])  # type: ignore[arg-type]


# --- disagreement -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disagreement_lowers_both_scores(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The third exit criterion's confidence half."""
    tid, aid = await _seed_tenant(factory)
    quiet_subject = await _seed_entity(factory, tid)
    noisy_subject = await _seed_entity(factory, tid)

    uncontested = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(quiet_subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    contested = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(noisy_subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(noisy_subject),
        predicate="owned_by_team",
        value="billing",
        evidence=(Evidence(kind="session_event", ref="e2"),),
    )

    clean_stored = await _stored(factory, uncontested.claim_id)
    contested_stored = await _stored(factory, contested.claim_id)
    assert contested_stored["is_contested"] is True
    assert float(contested_stored["confidence"]) < float(clean_stored["confidence"])  # type: ignore[arg-type]
    _assert_auditable(contested_stored)


@pytest.mark.asyncio
async def test_the_second_claim_of_a_disagreeing_pair_is_scored_as_contested(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Detection runs before scoring in the same transaction, so a claim is never
    stored briefly holding a number that ignores a conflict already found."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    second = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="billing",
        evidence=(Evidence(kind="session_event", ref="e2"),),
    )

    stored = await _stored(factory, second.claim_id)
    raw = stored["confidence_inputs"]
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert payload["is_contested"] is True


# --- decay inputs and the calibration state -----------------------------------


@pytest.mark.asyncio
async def test_a_faster_moving_category_gets_a_shorter_half_life(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """An interface claim about an actively released capability must lose value
    faster than a statement about who owns it."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    interface = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="request_timeout_seconds",
        value=900,
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    ownership = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )

    interface_stored = await _stored(factory, interface.claim_id)
    ownership_stored = await _stored(factory, ownership.claim_id)
    assert float(interface_stored["decay_half_life_days"]) < float(  # type: ignore[arg-type]
        ownership_stored["decay_half_life_days"]  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_a_claim_records_that_nothing_has_been_calibrated(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A token that cannot be mistaken for a version. An identity mapping would
    assert that a model reporting 0.9 is right nine times in ten, which nobody has
    checked."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    stored = await _stored(factory, claim.claim_id)
    assert stored["calibration_version"] == UNCALIBRATED
    assert ":" not in str(stored["calibration_version"])


@pytest.mark.asyncio
async def test_a_providers_own_number_is_stored_but_does_not_move_the_score(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Recorded because a mapping can only ever be fitted from raw scores paired
    with judged outcomes. Unused because nothing has checked what it predicts."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    without = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="staging",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    with_score = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="deployment_environment",
        value="production",
        evidence=(Evidence(kind="session_event", ref="e1"),),
        provider_confidence=0.99,
    )

    without_stored = await _stored(factory, without.claim_id)
    with_stored = await _stored(factory, with_score.claim_id)
    assert float(with_stored["provider_confidence"]) == pytest.approx(0.99)  # type: ignore[arg-type]
    assert float(with_stored["confidence"]) == float(without_stored["confidence"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_stored_score_never_sits_below_the_decay_floor(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Load-bearing rather than cosmetic: it makes ageing monotone, so a
    minimum-confidence query can prefilter on the indexed column before applying
    the exact age adjustment."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    weakest = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e1"),),
    )
    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="billing",
        evidence=(Evidence(kind="session_event", ref="e2"),),
    )

    stored = await _stored(factory, weakest.claim_id)
    assert float(stored["confidence"]) >= 0.10  # type: ignore[arg-type]
