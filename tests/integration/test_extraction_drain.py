"""The drain: queued windows in, staged claims out, failures isolated and bounded.

Extraction is never on the ingest hot path, so the interesting behaviour is all in
the queue: whether a burst of turns becomes one job or ten, whether one session's
provider failure stalls the others, whether a retriable failure backs off and a
terminal one stops immediately, and whether an exhausted row ends up somewhere a
person can find it.

The failure paths get more tests than the happy path, because they are what
decides whether a stalled pipeline is visible or silent.
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

from registry.extraction.provider import (
    USAGE_ESTIMATED,
    CandidateClaim,
    ExtractionRequest,
    ExtractionResult,
    NoOpProvider,
    ProviderError,
    TokenUsage,
)
from registry.extraction.service import ExtractionService
from registry.extraction.strategies import OBSERVATION, PREFERENCE, SUMMARY
from registry.service.claim_ontology import seed_ontology
from registry.service.claims import ClaimService
from registry.service.global_vocabulary import GlobalVocabularyService
from registry.service.memory import MemoryService
from registry.types import TenantContext
from registry.workers.extraction_drain import (
    BACKOFF_SCHEDULE_S,
    MAX_ATTEMPTS,
    ExtractionDrainWorker,
    enqueue_extraction,
)
from tests.helpers.clock import FakeClock

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


@pytest_asyncio.fixture(autouse=True)
async def empty_queue(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Start every test with an empty outbox.

    The drain claims across all tenants, which is correct in production and
    means a `DrainReport` counts whatever else happens to be queued. Without
    this, a test's own assertions about how many rows it drained would depend on
    which tests ran before it — and would pass or fail by ordering rather than by
    behaviour.
    """
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM lmm_extraction_outbox"))
        await session.execute(text("DELETE FROM lmm_extraction_outbox_failed"))
    yield


# --- test doubles ------------------------------------------------------------


class _ScriptedProvider:
    """Returns, or raises, whatever the test says — per call."""

    provider_id = "scripted"

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[ExtractionRequest] = []

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls.append(request)
        outcome = self._outcomes[min(len(self.calls) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, ExtractionResult)
        return outcome


def _result(*claims: CandidateClaim) -> ExtractionResult:
    return ExtractionResult(
        claims=claims,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, cached_prompt_tokens=0, source=USAGE_ESTIMATED),
        model_id="scripted",
        duration_ms=1,
    )


class _EchoProvider:
    """Claims one fact about a fixed subject, citing the first event it saw.

    Enough to prove the pipeline ran without depending on any extraction logic.
    """

    provider_id = "echo"

    def __init__(self, subject: uuid.UUID) -> None:
        self._subject = subject

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        if not request.events:
            return _result()
        return _result(
            CandidateClaim(
                subject_reference=str(self._subject),
                predicate="owned_by_team",
                value="platform",
                evidence_event_ids=(str(request.events[0].event_id),),
            )
        )


# --- fixtures ----------------------------------------------------------------


async def _seed_tenant(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"drn-{tid.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return tid, aid


async def _seed_entity(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> uuid.UUID:
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                "                      is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, 'tenant-shared', TRUE, :now)"
            ),
            {"eid": eid, "tid": tid, "name": f"cap-{eid.hex[:8]}", "now": _NOW},
        )
    return eid


def _ctx(tid: uuid.UUID, aid: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["producer"], oidc_subject="s")


async def _record(
    factory: async_sessionmaker[AsyncSession],
    tid: uuid.UUID,
    aid: uuid.UUID,
    *,
    session_id: str,
    body: str,
    strategies: tuple[object, ...] = (OBSERVATION,),
) -> int:
    """Write an event and queue it, in one transaction — as ingest will."""
    memory = MemoryService(factory, clock=FakeClock(_NOW))
    event = await memory.record_event(_ctx(tid, aid), session_id=session_id, kind="user_message", body=body)
    async with factory() as session, session.begin():
        await enqueue_extraction(
            session,
            tenant_id=tid,
            actor_id=aid,
            session_id=session_id,
            seq=event.seq,
            strategies=strategies,  # type: ignore[arg-type]
        )
    return event.seq


def _worker(factory: async_sessionmaker[AsyncSession], provider: object, **kw: object) -> ExtractionDrainWorker:
    return ExtractionDrainWorker(
        factory,
        provider,  # type: ignore[arg-type]
        ExtractionService(factory, ClaimService(factory, clock=FakeClock(_NOW))),
        clock=FakeClock(_NOW),
        **kw,  # type: ignore[arg-type]
    )


async def _pending(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> list[dict[str, object]]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT strategy_id, from_seq, through_seq, attempts, next_attempt_at, "
                    "       last_error "
                    "FROM lmm_extraction_outbox WHERE tenant_id = :tid ORDER BY strategy_id"
                ),
                {"tid": tid},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def _dead(factory: async_sessionmaker[AsyncSession], tid: uuid.UUID) -> list[dict[str, object]]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT strategy_id, attempts, last_error, from_seq, through_seq "
                    "FROM lmm_extraction_outbox_failed WHERE tenant_id = :tid"
                ),
                {"tid": tid},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


def _counter(name: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


# --- enqueue -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_burst_of_events_leaves_one_job_not_one_per_event(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Extraction reads a window of turns, because a claim usually spans several.
    A job per event would either re-extract the same window repeatedly or force
    each turn to be self-contained, and conversations are not."""
    tid, aid = await _seed_tenant(factory)
    for i in range(5):
        await _record(factory, tid, aid, session_id="s1", body=f"turn {i}")

    rows = await _pending(factory, tid)
    assert len(rows) == 1
    assert (rows[0]["from_seq"], rows[0]["through_seq"]) == (1, 5)


@pytest.mark.asyncio
async def test_each_strategy_gets_its_own_row(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    """They fail and retry independently. One row for all of them would make the
    slowest strategy the latency of every strategy."""
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="x", strategies=(OBSERVATION, PREFERENCE, SUMMARY))

    rows = await _pending(factory, tid)
    assert {r["strategy_id"] for r in rows} == {
        OBSERVATION.strategy_id,
        PREFERENCE.strategy_id,
        SUMMARY.strategy_id,
    }


@pytest.mark.asyncio
async def test_a_new_event_makes_a_backing_off_row_eligible_again(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """The earlier failure may have been about the window, and there is now more
    of it. Leaving it backing off would delay material that might well succeed."""
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="first")
    worker = _worker(factory, _ScriptedProvider(ProviderError("rate limited", is_retriable=True)))
    await worker.run_once()
    assert (await _pending(factory, tid))[0]["next_attempt_at"] is not None

    await _record(factory, tid, aid, session_id="s1", body="second")
    assert (await _pending(factory, tid))[0]["next_attempt_at"] is None


# --- the happy path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_drained_window_becomes_a_staged_claim(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    await _record(factory, tid, aid, session_id="s1", body="the platform team owns it")

    report = await _worker(factory, _EchoProvider(subject)).run_once()

    assert report.claimed == 1
    assert report.staged_claims == 1
    assert await _pending(factory, tid) == []


@pytest.mark.asyncio
async def test_the_window_reaches_the_provider_in_sequence_order(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """A transcript handed to a model out of order produces claims about a
    conversation that did not happen."""
    tid, aid = await _seed_tenant(factory)
    for i in range(4):
        await _record(factory, tid, aid, session_id="s1", body=f"turn {i}")

    provider = _ScriptedProvider(_result())
    await _worker(factory, provider).run_once()

    seqs = [e.seq for e in provider.calls[0].events]
    assert seqs == sorted(seqs) == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_the_noop_provider_drains_without_producing_or_failing(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Exit criterion 1. An unconfigured deployment must behave exactly like one
    that has no extraction feature at all."""
    tid, aid = await _seed_tenant(factory)
    for i in range(10):
        await _record(factory, tid, aid, session_id="s1", body=f"turn {i}")

    report = await _worker(factory, NoOpProvider()).run_once()

    assert report.claimed == 1
    assert report.staged_claims == 0
    assert await _dead(factory, tid) == []
    async with factory() as session:
        claims = (
            await session.execute(text("SELECT count(*) FROM lmm_claims WHERE author_tenant_id = :tid"), {"tid": tid})
        ).scalar_one()
    assert claims == 0


@pytest.mark.asyncio
async def test_a_turn_arriving_during_the_call_is_not_dropped(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """The enqueue that covers the new turn upserts onto the row being completed.
    Deleting unconditionally would lose it, and nothing would ever notice: the
    turn is stored, it simply never gets extracted.

    The write happens inside the provider call, which is the only way to
    reproduce the race — doing it beforehand just widens the window the worker
    reads.
    """
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="first")

    class _WritesDuringTheCall:
        provider_id = "racy"

        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, request: ExtractionRequest) -> ExtractionResult:
            self.calls += 1
            if self.calls == 1:
                await _record(factory, tid, aid, session_id="s1", body="arrived mid-call")
            return _result()

    await _worker(factory, _WritesDuringTheCall()).run_once()

    rows = await _pending(factory, tid)
    assert len(rows) == 1, "the turn that arrived mid-call must still be queued"
    assert rows[0]["from_seq"] == 2
    assert rows[0]["through_seq"] == 2


# --- failure handling --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retriable_failure_backs_off_on_the_schedule(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="x")

    worker = _worker(factory, _ScriptedProvider(ProviderError("rate limited", is_retriable=True)))
    report = await worker.run_once()

    assert report.retried == 1
    row = (await _pending(factory, tid))[0]
    assert row["attempts"] == 1
    assert row["next_attempt_at"] == _NOW + datetime.timedelta(seconds=BACKOFF_SCHEDULE_S[0])
    assert "rate limited" in str(row["last_error"])


@pytest.mark.asyncio
async def test_a_backing_off_row_is_not_claimed_before_its_time(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Filtered in the claim query rather than fetched and skipped. The
    difference matters when most of the queue is backing off."""
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="x")
    provider = _ScriptedProvider(ProviderError("later", is_retriable=True))
    worker = _worker(factory, provider)

    await worker.run_once()
    second = await worker.run_once()

    assert second.claimed == 0
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_the_row_is_claimed_once_the_backoff_has_elapsed(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="x")
    provider = _ScriptedProvider(ProviderError("later", is_retriable=True))

    await _worker(factory, provider).run_once()
    later = _NOW + datetime.timedelta(seconds=BACKOFF_SCHEDULE_S[0] + 1)
    resumed = ExtractionDrainWorker(
        factory,
        provider,  # type: ignore[arg-type]
        ExtractionService(factory, ClaimService(factory, clock=FakeClock(later))),
        clock=FakeClock(later),
    )
    report = await resumed.run_once()

    assert report.claimed == 1
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_a_terminal_failure_dead_letters_immediately(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """A rejected key means "never, until somebody changes something". Serving
    out the retries would be three more calls with the same wrong credential,
    and the backoff would hide the real problem behind a busy-looking queue."""
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="x")

    provider = _ScriptedProvider(ProviderError("authentication rejected (HTTP 401)", is_retriable=False))
    report = await _worker(factory, provider).run_once()

    assert report.dead_lettered == 1
    assert await _pending(factory, tid) == []
    dead = await _dead(factory, tid)
    assert len(dead) == 1
    assert "authentication rejected" in str(dead[0]["last_error"])
    assert len(provider.calls) == 1, "a terminal failure must not be retried"


@pytest.mark.asyncio
async def test_a_row_dead_letters_after_the_last_retry(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Three attempts is enough to ride out a restart or a rate-limit window,
    and few enough that a genuinely broken prompt surfaces the same day."""
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="x")
    provider = _ScriptedProvider(ProviderError("still failing", is_retriable=True))

    now = _NOW
    for attempt in range(MAX_ATTEMPTS):
        worker = ExtractionDrainWorker(
            factory,
            provider,  # type: ignore[arg-type]
            ExtractionService(factory, ClaimService(factory, clock=FakeClock(now))),
            clock=FakeClock(now),
        )
        await worker.run_once()
        if attempt < MAX_ATTEMPTS - 1:
            now = now + datetime.timedelta(seconds=BACKOFF_SCHEDULE_S[attempt] + 1)

    assert await _pending(factory, tid) == []
    dead = await _dead(factory, tid)
    assert len(dead) == 1
    assert dead[0]["attempts"] == MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_the_dead_letter_keeps_the_window_it_failed_on(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """A row that exhausted its retries is a question for a person, and "it
    failed" is not enough to decide whether to fix a prompt or replay a window."""
    tid, aid = await _seed_tenant(factory)
    for i in range(3):
        await _record(factory, tid, aid, session_id="s1", body=f"turn {i}")

    provider = _ScriptedProvider(ProviderError("nope", is_retriable=False))
    await _worker(factory, provider).run_once()

    dead = (await _dead(factory, tid))[0]
    assert (dead["from_seq"], dead["through_seq"]) == (1, 3)


@pytest.mark.asyncio
async def test_one_sessions_failure_does_not_stall_the_others(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Exit criterion 5. Each queued window is its own attempt and its own
    transaction."""
    tid, aid = await _seed_tenant(factory)
    subject = await _seed_entity(factory, tid)
    for i in range(5):
        await _record(factory, tid, aid, session_id=f"s{i}", body="the team owns it")

    class _FailOnSecond:
        provider_id = "fail-one"

        def __init__(self) -> None:
            self.calls = 0
            self._echo = _EchoProvider(subject)

        async def extract(self, request: ExtractionRequest) -> ExtractionResult:
            self.calls += 1
            if self.calls == 2:
                raise ProviderError("this one broke", is_retriable=True)
            return await self._echo.extract(request)

    provider = _FailOnSecond()
    report = await _worker(factory, provider).run_once()

    assert report.claimed == 5
    assert report.staged_claims == 4
    assert report.retried == 1
    assert provider.calls == 5, "every sibling must still have been attempted"


@pytest.mark.asyncio
async def test_an_unknown_strategy_dead_letters_rather_than_looping(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """No number of attempts makes an unknown strategy known, and leaving it
    queued would grow the backlog without bound after a rollback."""
    tid, aid = await _seed_tenant(factory)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO lmm_extraction_outbox "
                "  (tenant_id, actor_id, session_id, strategy_id, from_seq, through_seq) "
                "VALUES (:tid, :aid, 's1', 'retired_strategy', 1, 1)"
            ),
            {"tid": tid, "aid": aid},
        )

    report = await _worker(factory, _ScriptedProvider(_result())).run_once()

    assert report.dead_lettered == 1
    assert await _pending(factory, tid) == []


@pytest.mark.asyncio
async def test_a_vanished_window_completes_rather_than_retrying_forever(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Erased, expired, or already consumed. There is nothing to extract, and the
    queue row would otherwise be immortal."""
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="x")
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM memory_session_events WHERE tenant_id = :tid"), {"tid": tid})

    report = await _worker(factory, _ScriptedProvider(_result())).run_once()

    assert report.claimed == 1
    assert await _pending(factory, tid) == []
    assert await _dead(factory, tid) == []


@pytest.mark.asyncio
async def test_an_invalidated_event_is_not_handed_to_the_provider(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """A deleted turn must not be sent to a third party after deletion. Soft
    deletion is still deletion from the caller's point of view."""
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="keep me")
    await _record(factory, tid, aid, session_id="s1", body="delete me")
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE memory_session_events "
                "SET invalidated_at = :now, invalidated_reason = 'actor_deleted' "
                "WHERE tenant_id = :tid AND seq = 2"
            ),
            {"tid": tid, "now": _NOW},
        )

    provider = _ScriptedProvider(_result())
    await _worker(factory, provider).run_once()

    bodies = [e.body for e in provider.calls[0].events]
    assert bodies == ["keep me"]


# --- batching and metrics ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_tick_is_bounded_by_the_batch_size(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    """One tenant with a large backlog must not monopolize a tick."""
    tid, aid = await _seed_tenant(factory)
    for i in range(7):
        await _record(factory, tid, aid, session_id=f"s{i}", body="x")

    report = await _worker(factory, _ScriptedProvider(_result()), batch_size=3).run_once()
    assert report.claimed == 3


@pytest.mark.asyncio
async def test_the_window_handed_to_the_provider_is_bounded(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Cost is superlinear in context and a model's attention is not. A larger
    window is extracted across successive runs instead."""
    tid, aid = await _seed_tenant(factory)
    for i in range(12):
        await _record(factory, tid, aid, session_id="s1", body=f"turn {i}")

    provider = _ScriptedProvider(_result())
    await _worker(factory, provider, window_events=5).run_once()

    assert len(provider.calls[0].events) == 5


@pytest.mark.asyncio
async def test_a_bounded_window_leaves_the_rest_queued(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Otherwise a busy session would silently lose everything past the window."""
    tid, aid = await _seed_tenant(factory)
    for i in range(12):
        await _record(factory, tid, aid, session_id="s1", body=f"turn {i}")

    await _worker(factory, _ScriptedProvider(_result()), window_events=5).run_once()

    rows = await _pending(factory, tid)
    assert len(rows) == 1
    assert rows[0]["from_seq"] == 6
    assert rows[0]["through_seq"] == 12


@pytest.mark.asyncio
async def test_an_empty_queue_is_not_an_error(factory: async_sessionmaker[AsyncSession], ontology: None) -> None:
    report = await _worker(factory, _ScriptedProvider(_result())).run_once()
    assert report.claimed == 0
    assert not report.had_work


@pytest.mark.asyncio
async def test_dead_lettering_is_counted_per_strategy(
    factory: async_sessionmaker[AsyncSession], ontology: None
) -> None:
    """Which prompt is failing, not just that something is."""
    tid, aid = await _seed_tenant(factory)
    await _record(factory, tid, aid, session_id="s1", body="x")
    metric = "registry_extraction_dead_lettered_total"
    before = _counter(metric, strategy=OBSERVATION.strategy_id)

    await _worker(factory, _ScriptedProvider(ProviderError("terminal", is_retriable=False))).run_once()

    assert _counter(metric, strategy=OBSERVATION.strategy_id) == before + 1
