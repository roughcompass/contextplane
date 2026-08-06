"""Claims that disagree, detected mechanically and recorded as a pair.

The single most consequential rule here is that a set-valued predicate never
disagrees with itself. Getting that wrong would mark every second dependency
contested — and a contested claim cannot be promoted and always needs review,
which no reviewer can resolve when both values are true and neither supersedes
the other. So the multi-valued tests carry as much weight as the single-valued
ones.

Detection runs in the same transaction as the write, so a claim and the
disagreements it creates commit together. A claim staged and uncontested while it
conflicts with something already stored would pass a promotion gate that reads
only the flag.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.exceptions import ValidationError
from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.memory.claim_authority import Evidence
from registry.service.memory.claim_ontology import seed_ontology
from registry.service.memory.claim_writer import ClaimService
from registry.service.memory.contest import (
    RESOLUTION_SUPERSEDED,
    resolve,
)
from tests.helpers.clock import FakeClock
from tests.helpers.context import claim_producer_ctx as _ctx
from tests.helpers.seeding import seed_shared_entity as _seed_entity

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


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"con-{tid.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return tid, aid


async def _contests(factory: async_sessionmaker[AsyncSession], subject: uuid.UUID) -> list[dict[str, object]]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT contest_id, predicate, lower_value, upper_value, resolved_at, "
                    "       resolution "
                    "FROM memory_claim_contest WHERE subject_entity_id = :eid"
                ),
                {"eid": subject},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def _flag(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> bool:
    async with factory() as session:
        return bool(
            (
                await session.execute(
                    text("SELECT is_contested FROM memory_claims WHERE claim_id = :cid"),
                    {"cid": claim_id},
                )
            ).scalar_one()
        )


def _counter(name: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


# --- single-valued predicates disagree ---------------------------------------


@pytest.mark.asyncio
async def test_two_owners_over_the_same_interval_disagree(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Accountability is singular. Two accountable teams is the pathology the
    predicate exists to surface."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    first = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    second = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="billing",
        evidence=_EV,
    )

    assert second.is_contested
    assert await _flag(factory, first.claim_id), "the older claim must be marked too"
    assert await _flag(factory, second.claim_id)
    contests = await _contests(factory, subject)
    assert len(contests) == 1
    assert contests[0]["predicate"] == "owned_by_team"


@pytest.mark.asyncio
async def test_the_recorded_pair_shows_both_values(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """ "This claim is contested" is not actionable. What it disagrees with, over
    which values, is."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for team in ("platform", "billing"):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=team,
            evidence=_EV,
        )

    contest = (await _contests(factory, subject))[0]
    assert {contest["lower_value"], contest["upper_value"]} == {"platform", "billing"}


@pytest.mark.asyncio
async def test_the_same_value_twice_does_not_disagree(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Two sources saying the same thing is agreement, not conflict."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for _ in range(2):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value="platform",
            evidence=_EV,
        )

    assert await _contests(factory, subject) == []


@pytest.mark.asyncio
async def test_case_and_spacing_differences_do_not_disagree(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """ "Platform" and "platform" are one team. Flagging that would make the
    detector fire on typing."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for team in ("Platform", " platform "):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=team,
            evidence=_EV,
        )

    assert await _contests(factory, subject) == []


@pytest.mark.asyncio
async def test_durations_within_tolerance_do_not_disagree(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for seconds in (900, 905):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="request_timeout_seconds",
            value=seconds,
            evidence=_EV,
        )

    assert await _contests(factory, subject) == []


@pytest.mark.asyncio
async def test_durations_outside_tolerance_disagree(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for seconds in (900, 1800):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="request_timeout_seconds",
            value=seconds,
            evidence=_EV,
        )

    assert len(await _contests(factory, subject)) == 1


# --- set-valued predicates never disagree with themselves ---------------------


@pytest.mark.asyncio
async def test_two_dependencies_are_two_facts_not_a_disagreement(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The case that would break everything. A capability depends on many things;
    flagging the second would make the claim permanently unpromotable with no
    resolution available, because both are true."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for _ in range(3):
        target = await _seed_entity(factory, tid)
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="depends_on",
            value=str(target),
            evidence=_EV,
        )

    assert await _contests(factory, subject) == []


@pytest.mark.asyncio
async def test_several_environments_are_not_a_disagreement(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """A capability is in staging and production at once."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for env in ("staging", "production", "canary"):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="deployment_environment",
            value=env,
            evidence=_EV,
        )

    assert await _contests(factory, subject) == []


@pytest.mark.asyncio
async def test_several_operations_are_not_a_disagreement(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for op in ("getUser", "createUser", "deleteUser"):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="exposes_operation",
            value=op,
            evidence=_EV,
        )

    assert await _contests(factory, subject) == []


@pytest.mark.asyncio
async def test_several_escalation_contacts_are_not_a_disagreement(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """An escalation path is a ladder. The rotation beside it is single-valued,
    and that pair is why cardinality cannot follow the category."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for contact in ("team-lead", "director", "exec-oncall"):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="escalation_contact",
            value=contact,
            evidence=_EV,
        )

    assert await _contests(factory, subject) == []


# --- intervals ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_handover_is_a_succession_not_a_disagreement(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """One claim ending exactly when the next begins is how an ownership change
    is recorded. Contesting it would make every handover a conflict."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    handover = _NOW + datetime.timedelta(days=30)

    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
        asserted_valid_from=_NOW,
        asserted_valid_to=handover,
    )
    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="billing",
        evidence=_EV,
        asserted_valid_from=handover,
    )

    assert await _contests(factory, subject) == []


@pytest.mark.asyncio
async def test_overlapping_intervals_with_different_values_disagree(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
        asserted_valid_from=_NOW,
        asserted_valid_to=_NOW + datetime.timedelta(days=60),
    )
    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="billing",
        evidence=_EV,
        asserted_valid_from=_NOW + datetime.timedelta(days=30),
    )

    assert len(await _contests(factory, subject)) == 1


# --- scoping and idempotency --------------------------------------------------


@pytest.mark.asyncio
async def test_claims_about_different_subjects_do_not_disagree(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    first_subject = await _seed_entity(factory, tid)
    second_subject = await _seed_entity(factory, tid)

    for subject, team in ((first_subject, "platform"), (second_subject, "billing")):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=team,
            evidence=_EV,
        )

    assert await _contests(factory, first_subject) == []
    assert await _contests(factory, second_subject) == []


@pytest.mark.asyncio
async def test_different_predicates_do_not_disagree(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Same-predicate only. A cross-predicate conflict is a different problem
    needing a relation the ontology does not have."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)

    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )
    await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference=str(subject),
        predicate="on_call_rotation",
        value="billing",
        evidence=_EV,
    )

    assert await _contests(factory, subject) == []


@pytest.mark.asyncio
async def test_a_third_disagreeing_claim_records_two_more_pairs(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Pairwise, so three mutually incompatible claims are three disagreements.
    Recording only one would leave two pairs invisible to a reviewer."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for team in ("platform", "billing", "infra"):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=team,
            evidence=_EV,
        )

    assert len(await _contests(factory, subject)) == 3


@pytest.mark.asyncio
async def test_an_unlinked_claim_has_no_neighbourhood(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """No subject means nothing to disagree about, and such a claim is excluded
    from scoring anyway."""
    tid, aid = await _seed_tenant(factory)

    claim = await claims.stage_claim(
        _ctx(tid, aid),
        subject_reference="github:acme/unknown",
        predicate="owned_by_team",
        value="platform",
        evidence=_EV,
    )

    assert not claim.is_contested
    assert not await _flag(factory, claim.claim_id)


@pytest.mark.asyncio
async def test_a_disagreement_is_counted_by_predicate(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """Which predicate is generating conflicts, not just that some are."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    metric = "registry_claim_contest_detected_total"
    before = _counter(metric, predicate="owned_by_team")

    for team in ("platform", "billing"):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=team,
            evidence=_EV,
        )

    assert _counter(metric, predicate="owned_by_team") == before + 1


# --- resolution ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolving_the_only_disagreement_clears_both_flags(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    staged = [
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=team,
            evidence=_EV,
        )
        for team in ("platform", "billing")
    ]
    contest_id = (await _contests(factory, subject))[0]["contest_id"]

    async with factory() as session, session.begin():
        await resolve(
            session,
            contest_id=contest_id,  # type: ignore[arg-type]
            resolution=RESOLUTION_SUPERSEDED,
            now=_NOW,
        )

    assert not await _flag(factory, staged[0].claim_id)
    assert not await _flag(factory, staged[1].claim_id)


@pytest.mark.asyncio
async def test_resolving_one_of_several_leaves_the_flag_set(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """The flag caches "does an unresolved disagreement exist", so settling one
    does not settle the others. Clearing it unconditionally would let a still-
    conflicted claim through a promotion gate."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    staged = [
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=team,
            evidence=_EV,
        )
        for team in ("platform", "billing", "infra")
    ]
    contest_id = (await _contests(factory, subject))[0]["contest_id"]

    async with factory() as session, session.begin():
        await resolve(
            session,
            contest_id=contest_id,  # type: ignore[arg-type]
            resolution=RESOLUTION_SUPERSEDED,
            now=_NOW,
        )

    still_flagged = [c for c in staged if await _flag(factory, c.claim_id)]
    assert len(still_flagged) >= 2


@pytest.mark.asyncio
async def test_a_resolved_disagreement_is_kept_not_deleted(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    """That two sources disagreed is a fact about the store's history. A
    resolution that erased its own cause could not be reviewed."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for team in ("platform", "billing"):
        await claims.stage_claim(
            _ctx(tid, aid),
            subject_reference=str(subject),
            predicate="owned_by_team",
            value=team,
            evidence=_EV,
        )
    contest_id = (await _contests(factory, subject))[0]["contest_id"]

    async with factory() as session, session.begin():
        await resolve(
            session,
            contest_id=contest_id,  # type: ignore[arg-type]
            resolution=RESOLUTION_SUPERSEDED,
            now=_NOW,
        )

    rows = await _contests(factory, subject)
    assert len(rows) == 1
    assert rows[0]["resolved_at"] is not None
    assert rows[0]["resolution"] == RESOLUTION_SUPERSEDED


@pytest.mark.asyncio
async def test_an_unknown_resolution_is_refused(
    factory: async_sessionmaker[AsyncSession], claims: ClaimService, ontology: None
) -> None:
    _tid, _aid = await _seed_tenant(factory)
    with pytest.raises(ValidationError, match="unknown"):
        async with factory() as session, session.begin():
            await resolve(session, contest_id=uuid.uuid4(), resolution="whatever", now=_NOW)
