"""The writer's three promises, each a separate way this could ruin a request.

1. Recording touches no database on the request path.
2. A full buffer costs one failed enqueue and a counted drop — never an exception.
3. An unreachable database costs recorded events and nothing else.

The third is the one that turns instrumentation into an outage, and it is the
reason `record` catches broadly rather than narrowly: the failure that matters is
the one nobody predicted, so the guard cannot be a list of anticipated exceptions.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from registry.usage.writer import UsageEvent, UsageWriter

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


def _event(**overrides: object) -> UsageEvent:
    base: dict = {
        "occurred_at": _NOW,
        "tenant_id": uuid.uuid4(),
        "surface": "rest",
        "operation": "/v1/capabilities",
        "outcome": "ok",
        "status_class": "2xx",
        "latency_ms": 7,
    }
    base.update(overrides)
    return UsageEvent(**base)  # type: ignore[arg-type]


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _drops() -> float:
    return _sample("registry_worker_dead_lettered_total", queue="usage_events")


def _session_factory(*, fail: bool = False) -> tuple[MagicMock, list]:
    """A factory plus the list of statements it saw, so tests can assert on I/O."""
    executed: list = []
    session = AsyncMock()
    if fail:
        session.execute = AsyncMock(side_effect=RuntimeError("database is unreachable"))
    else:

        async def execute(statement: object, params: object = None) -> MagicMock:
            executed.append((statement, params))
            return MagicMock()

        session.execute = execute

    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=None)
    begin.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, executed


# ---------------------------------------------------------------------------
# 1. Nothing touches the database on the request path
# ---------------------------------------------------------------------------


def test_recording_opens_no_session() -> None:
    """The property that keeps NF2.1 reachable at all.

    Asserted against the factory rather than by timing: a latency measurement
    would pass on a fast machine with a synchronous insert hidden inside, whereas
    a factory that was never called cannot have talked to a database.
    """
    factory, _ = _session_factory()
    writer = UsageWriter(factory)

    for _ in range(50):
        writer.record(_event())

    factory.assert_not_called()


def test_recording_is_synchronous() -> None:
    # If `record` were a coroutine, the middleware would have to await it and
    # recording would become a suspension point on the request path — where a slow
    # drain stops costing accuracy and starts costing request latency.
    assert not asyncio.iscoroutinefunction(UsageWriter.record)


# ---------------------------------------------------------------------------
# 2. A full buffer drops, counts, and stays quiet
# ---------------------------------------------------------------------------


def test_a_full_buffer_counts_the_drop_instead_of_raising() -> None:
    factory, _ = _session_factory()
    writer = UsageWriter(factory, max_queue=3)

    before = _drops()
    for _ in range(10):
        writer.record(_event())  # must not raise

    # Three accepted, seven refused. The refusals are the number an operator needs.
    assert _drops() == before + 7


def test_the_queue_depth_is_published() -> None:
    """Depth answers "is the drain keeping up", which the drop counter cannot.

    A zero drop count is consistent with both a healthy drain and one that is
    steadily falling behind but has not yet hit the ceiling.
    """
    factory, _ = _session_factory()
    writer = UsageWriter(factory, max_queue=100)
    for _ in range(4):
        writer.record(_event())

    asyncio.run(writer._flush_once())  # noqa: SLF001 - the flush is what publishes it
    assert _sample("registry_worker_queue_depth", queue="usage_events") == 0


# ---------------------------------------------------------------------------
# 3. An unreachable database costs events and nothing else
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_database_never_reaches_the_caller() -> None:
    """The failure that turns measurement into an outage, if it escapes.

    `record` is what a request calls, so the assertion is that recording stays
    silent while the database is down — the batch is lost and counted, and the
    request that produced it never learns anything went wrong.
    """
    factory, _ = _session_factory(fail=True)
    writer = UsageWriter(factory)

    for _ in range(5):
        writer.record(_event())  # the request path: must not raise

    before = _drops()
    with pytest.raises(RuntimeError, match="unreachable"):
        await writer._flush_once()  # noqa: SLF001 - the drain sees the error, not the caller

    # Lost, and counted as lost. Not requeued: a retry loop in front of a dead
    # database is how a bounded buffer becomes an unbounded one.
    assert _drops() == before + 5


@pytest.mark.asyncio
async def test_a_failed_flush_does_not_kill_the_drain() -> None:
    """Otherwise the first transient error stops recording for the process's life.

    The only symptom would be a graph that goes flat and stays flat, which reads
    as "nobody used it" rather than "we stopped looking".
    """
    factory, _ = _session_factory(fail=True)
    writer = UsageWriter(factory, flush_interval_s=0.01)

    await writer.start()
    for _ in range(3):
        writer.record(_event())
    await asyncio.sleep(0.05)  # long enough for several failed flushes

    assert writer._task is not None  # noqa: SLF001
    assert not writer._task.done()  # noqa: SLF001
    await writer.stop()


# ---------------------------------------------------------------------------
# The drain does its job when the database works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_reach_the_database_in_one_batched_statement() -> None:
    factory, executed = _session_factory()
    writer = UsageWriter(factory, batch_size=100)

    for _ in range(12):
        writer.record(_event())
    await writer._flush_once()  # noqa: SLF001

    assert len(executed) == 1, "twelve events should be one INSERT, not twelve"
    _, params = executed[0]
    assert isinstance(params, list) and len(params) == 12


@pytest.mark.asyncio
async def test_a_batch_is_capped_and_the_remainder_waits() -> None:
    factory, executed = _session_factory()
    writer = UsageWriter(factory, batch_size=5)

    for _ in range(12):
        writer.record(_event())
    await writer._flush_once()  # noqa: SLF001

    _, params = executed[0]
    assert len(params) == 5
    assert writer._queue.qsize() == 7  # noqa: SLF001


@pytest.mark.asyncio
async def test_stopping_flushes_what_was_already_accepted() -> None:
    # A rolling deploy would otherwise discard most of a batch on every restart.
    factory, executed = _session_factory()
    writer = UsageWriter(factory, flush_interval_s=10.0)

    await writer.start()
    writer.record(_event())
    await writer.stop()

    assert executed, "stop() should flush the queue rather than discard it"


@pytest.mark.asyncio
async def test_an_empty_queue_writes_nothing() -> None:
    factory, executed = _session_factory()
    await UsageWriter(factory)._flush_once()  # noqa: SLF001
    assert executed == []


# ---------------------------------------------------------------------------
# The event itself
# ---------------------------------------------------------------------------


def test_an_event_is_immutable() -> None:
    # An event is a fact about something that already happened. A mutable one
    # invites a caller to "correct" a measurement in flight.
    event = _event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.latency_ms = 99  # type: ignore[misc]


def test_an_unauthenticated_call_records_with_no_actor() -> None:
    # None means no identity was resolved, never "not recorded".
    assert _event(actor_id=None).actor_id is None
