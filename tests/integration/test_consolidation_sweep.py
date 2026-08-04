"""The sweep that reconciles claims on a schedule.

It runs off the schedule rather than on the write for one reason: a claim's correct
decision can depend on a claim that has not arrived yet. Reconciling only at write time
would settle each claim against whatever existed at that instant and never look again,
so a conflict whose other side showed up second would go undetected forever.

The candidate query is therefore the load-bearing part, and it is what these tests
mostly exercise: a claim qualifies when it has never been reconciled *or* when something
newer arrived in its neighbourhood since.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.global_vocabulary import GlobalVocabularyService
from registry.service.memory.claim_ontology import seed_ontology
from registry.service.memory.claims import ClaimService, Evidence
from registry.service.memory.consolidation import ConsolidationService
from registry.types import TenantContext
from registry.workers.consolidation_sweep import ConsolidationSweepWorker
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


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


@pytest_asyncio.fixture(autouse=True)
async def only_this_tests_claims(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """The sweep reconciles across all tenants, which is correct in production and means
    a report counts whatever else is lying around unconsolidated. Without this, an
    assertion about how many claims a tick considered would depend on which tests ran
    before it rather than on behaviour."""
    async with factory() as session, session.begin():
        # Stamped far in the future rather than with the real clock. These tests run on
        # a fixed 2026 timestamp, so "now" is not reliably after the claims they create
        # -- and a claim reconciled *before* a neighbour arrived is still a candidate,
        # which is the whole point of the candidate query. A distant stamp makes
        # pre-existing rows unambiguously settled whatever the clocks are doing.
        await session.execute(
            text(
                "UPDATE memory_claims "
                "SET consolidated_at = TIMESTAMPTZ '2999-01-01 00:00:00+00' "
                "WHERE consolidated_at IS NULL "
                "   OR consolidated_at < TIMESTAMPTZ '2999-01-01 00:00:00+00'"
            )
        )
    yield


def _sweep(
    factory: async_sessionmaker[AsyncSession], *, at: int = 0, batch_size: int = 100
) -> ConsolidationSweepWorker:
    clock = FakeClock(_at(at))
    return ConsolidationSweepWorker(
        factory,
        ConsolidationService(factory, clock=clock),
        clock=clock,
        batch_size=batch_size,
    )


async def _seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid, aid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"swp-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'a', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"s-{aid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                "                      is_active, created_at) "
                "VALUES (:e, :t, 'capability', 'cap', 'public', TRUE, :n)"
            ),
            {"e": eid, "t": tid, "n": _NOW},
        )
    return tid, aid, eid


async def _stage_only(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    subject: uuid.UUID,
    *,
    at: int,
    value: object,
    predicate: str = "owned_by_team",
) -> uuid.UUID:
    """Stage without consolidating, so the sweep has something to find."""
    claim = await ClaimService(factory, clock=FakeClock(_at(at))).stage_claim(
        TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s"),
        subject_reference=str(subject),
        predicate=predicate,
        value=value,
        evidence=(Evidence(kind="session_event", ref=f"e{at}"),),
    )
    return claim.claim_id


async def _status(factory: async_sessionmaker[AsyncSession], claim_id: uuid.UUID) -> str:
    async with factory() as session:
        return str(
            (
                await session.execute(text("SELECT status FROM memory_claims WHERE claim_id = :cid"), {"cid": claim_id})
            ).scalar_one()
        )


# --- finding work -------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreconciled_claim_is_picked_up(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    tid, aid, subject = await _seed(factory)
    claim = await _stage_only(factory, tid, aid, subject, at=0, value="platform")

    report = await _sweep(factory, at=10).run_once()

    assert report.considered == 1
    assert report.decided == 1
    async with factory() as session:
        assert (
            await session.execute(
                text("SELECT consolidated_at FROM memory_claims WHERE claim_id = :cid"),
                {"cid": claim},
            )
        ).scalar_one() is not None


@pytest.mark.asyncio
async def test_an_empty_queue_is_not_an_error(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    report = await _sweep(factory).run_once()
    assert not report.had_work
    assert report.failed == 0


@pytest.mark.asyncio
async def test_a_reconciled_claim_is_not_picked_up_again(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Otherwise every tick would rewrite the same rows and the audit log would record
    how often the sweep ran rather than what it decided."""
    tid, aid, subject = await _seed(factory)
    await _stage_only(factory, tid, aid, subject, at=0, value="platform")

    first = await _sweep(factory, at=10).run_once()
    second = await _sweep(factory, at=20).run_once()

    assert first.decided == 1
    assert second.considered == 0


@pytest.mark.asyncio
async def test_a_newer_neighbour_brings_a_settled_claim_back(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """The half of the candidate query that keeps the sweep from being one-shot. A
    conflict whose other side arrives second is only found by looking again."""
    tid, aid, subject = await _seed(factory)
    first = await _stage_only(factory, tid, aid, subject, at=0, value="platform")
    await _sweep(factory, at=10).run_once()

    # A second claim arrives. Both are now candidates: the newcomer has never been
    # reconciled, and the first was reconciled before the newcomer existed.
    await _stage_only(factory, tid, aid, subject, at=20, value="billing")
    report = await _sweep(factory, at=30).run_once()

    assert report.considered == 2
    assert await _status(factory, first) == "superseded"


@pytest.mark.asyncio
async def test_oldest_claims_are_swept_first(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    """A claim waiting longest is the one whose absence from the store's answers has
    cost most."""
    tid, aid, subject = await _seed(factory)
    oldest = await _stage_only(factory, tid, aid, subject, at=0, value="platform")
    for i in range(1, 4):
        other = await _seed(factory)
        await _stage_only(factory, other[0], other[1], other[2], at=i * 10, value="x")

    report = await _sweep(factory, at=100, batch_size=1).run_once()

    assert report.considered == 1
    assert await _status(factory, oldest) in {"staged", "superseded"}
    async with factory() as session:
        assert (
            await session.execute(
                text("SELECT consolidated_at FROM memory_claims WHERE claim_id = :cid"),
                {"cid": oldest},
            )
        ).scalar_one() is not None, "the oldest claim should have been the one taken"


@pytest.mark.asyncio
async def test_a_tick_is_bounded_by_the_batch_size(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    """A backlog is drained across ticks rather than in one pass holding row locks for
    minutes, during which nothing else can consolidate."""
    tid, aid, subject = await _seed(factory)
    for i in range(7):
        other = await _seed(factory)
        await _stage_only(factory, other[0], other[1], other[2], at=i, value="x")

    report = await _sweep(factory, at=60, batch_size=3).run_once()
    assert report.considered == 3


@pytest.mark.asyncio
async def test_an_unlinked_claim_is_never_a_candidate(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """No subject means no neighbourhood. Including them would make the sweep revisit
    the same rows forever, since nothing about them can ever change."""
    tid, aid, _ = await _seed(factory)
    await ClaimService(factory, clock=FakeClock(_NOW)).stage_claim(
        TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s"),
        subject_reference="github:acme/unknown",
        predicate="owned_by_team",
        value="platform",
        evidence=(Evidence(kind="session_event", ref="e0"),),
    )

    report = await _sweep(factory, at=10).run_once()
    assert report.considered == 0


@pytest.mark.asyncio
async def test_a_superseded_claim_is_never_a_candidate(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Closed history is not relitigated."""
    tid, aid, subject = await _seed(factory)
    await _stage_only(factory, tid, aid, subject, at=0, value="platform")
    await _stage_only(factory, tid, aid, subject, at=10, value="billing")
    await _sweep(factory, at=20).run_once()

    report = await _sweep(factory, at=30).run_once()
    assert report.considered == 0


# --- failure isolation --------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_claim_does_not_stop_the_rest(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """A sweep that aborted on the first problem would stall permanently on a single
    bad row, and the backlog behind it would grow without anything saying why."""
    tid, aid, subject = await _seed(factory)
    for i in range(3):
        other = await _seed(factory)
        await _stage_only(factory, other[0], other[1], other[2], at=i, value="x")
    await _stage_only(factory, tid, aid, subject, at=10, value="platform")

    clock = FakeClock(_at(60))

    class _FailsOnce(ConsolidationService):
        def __init__(self) -> None:
            super().__init__(factory, clock=clock)
            self.calls = 0

        async def consolidate(self, claim_id: uuid.UUID):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 2:
                msg = "this neighbourhood is pathological"
                raise RuntimeError(msg)
            return await super().consolidate(claim_id)

    service = _FailsOnce()
    worker = ConsolidationSweepWorker(factory, service, clock=clock)
    report = await worker.run_once()

    assert report.failed == 1
    assert report.decided == report.considered - 1
    assert service.calls == report.considered, "every claim must still have been attempted"
