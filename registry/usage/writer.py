"""Recording usage off the request path, with loss that is bounded and counted.

The request path does one thing: put an already-built event on an in-memory queue
and return. No database call, no await on I/O, no lock. A separate task drains the
queue in batches.

**Why the queue is bounded.** An unbounded queue does not remove the failure mode,
it relocates it: a database that stops accepting writes turns into a process that
grows until the kernel kills it, and the symptom appears far from the cause. A
bounded queue fails in a way that is local, immediate, and countable.

**Why dropping is acceptable here and would not be elsewhere.** These rows measure;
they are not evidence. Losing some under sustained overload costs accuracy in a
graph. The audit log makes the opposite trade — it writes synchronously and its
failures are counted separately — because losing an audit row costs a compliance
record. Putting usage recording on the audit log's write path was considered and
refused for exactly this reason: it would give measurement data the cost of
evidence and give evidence the reliability of measurement.

**Silent loss is the actual defect.** A drop that nobody can see is
indistinguishable from traffic that never happened, and it is worse than an
outage because the graph still looks plausible. So every drop increments a counter
that is queryable in the operational tier, and the queue's depth is published as a
gauge. Loss is a number, not a mystery.

**Nothing here may break a request.** `record` catches everything. An exception
escaping instrumentation converts a working request into a 500 caused by the
attempt to measure it, which is the one outcome worse than not measuring.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.metrics import observe_dead_lettered, observe_queue_depth

__all__ = ["UsageEvent", "UsageWriter"]

_log = logging.getLogger(__name__)

# The queue name these metrics are published under. A dropped usage event *is* an
# abandoned queue item, so it joins the worker family Phase 1 already established
# rather than minting a metric family that would need its own surface pin.
_QUEUE = "usage_events"

# Ten thousand events. At the 50k-per-tenant-per-day design point this is several
# minutes of headroom for a single busy tenant — long enough to absorb a database
# restart or a slow vacuum, short enough that a wedged drain is noticed by its drop
# counter rather than by memory growth.
DEFAULT_MAX_QUEUE = 10_000

# Rows per INSERT. Large enough that the per-statement overhead disappears, small
# enough that one failed flush loses a bounded amount and retries cheaply.
DEFAULT_BATCH_SIZE = 200

# How long the drain waits for a batch to fill before writing what it has. Bounds
# how stale the table can be, which matters because the rollup job reads it.
DEFAULT_FLUSH_INTERVAL_S = 1.0


@dataclasses.dataclass(frozen=True, slots=True)
class UsageEvent:
    """One recorded call.

    Frozen because an event is a fact about something that already happened; a
    mutable one invites a caller to "correct" a measurement in flight.

    Every field is an identifier, a timestamp, a number, or a member of a closed
    vocabulary. There is deliberately nowhere to put text: see
    :mod:`registry.usage.vocabularies`.
    """

    occurred_at: datetime.datetime
    tenant_id: uuid.UUID
    surface: str
    operation: str
    outcome: str
    status_class: str
    latency_ms: int
    #: `None` means no identity was resolved — an unauthenticated call — never
    #: "not recorded". Dropping those rows would change the denominator of every
    #: rate computed from this table.
    actor_id: uuid.UUID | None = None
    result_count: int | None = None
    payload_bytes: int | None = None
    payload_tokens: int | None = None
    request_id: str | None = None
    subject_entity_ids: tuple[uuid.UUID, ...] = ()
    query_digest: str | None = None
    query_length: int | None = None


_INSERT = text(
    """
    INSERT INTO usage_events (
        event_id, occurred_at, tenant_id, actor_id, surface, operation,
        outcome, status_class, latency_ms, result_count, payload_bytes,
        payload_tokens, request_id, subject_entity_ids, query_digest, query_length
    ) VALUES (
        :event_id, :occurred_at, :tenant_id, :actor_id, :surface, :operation,
        :outcome, :status_class, :latency_ms, :result_count, :payload_bytes,
        :payload_tokens, :request_id, :subject_entity_ids, :query_digest, :query_length
    )
    """
)


def _params(event: UsageEvent) -> dict[str, Any]:
    return {
        # Generated at flush rather than at record: the id has no meaning to the
        # caller and generating it later keeps the request path shorter.
        "event_id": uuid.uuid4(),
        "occurred_at": event.occurred_at,
        "tenant_id": event.tenant_id,
        "actor_id": event.actor_id,
        "surface": event.surface,
        "operation": event.operation,
        "outcome": event.outcome,
        "status_class": event.status_class,
        "latency_ms": event.latency_ms,
        "result_count": event.result_count,
        "payload_bytes": event.payload_bytes,
        "payload_tokens": event.payload_tokens,
        "request_id": event.request_id,
        "subject_entity_ids": list(event.subject_entity_ids),
        "query_digest": event.query_digest,
        "query_length": event.query_length,
    }


class UsageWriter:
    """Bounded queue in front of the usage table.

    One instance per process, held on application state. Two instances would each
    hold their own buffer and each report their own depth, so the gauge would
    describe neither.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        self._session_factory = session_factory
        self._queue: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # ------------------------------------------------------------------
    # The request path — everything below this line runs off it
    # ------------------------------------------------------------------

    def record(self, event: UsageEvent) -> None:
        """Enqueue one event. Never blocks, never raises, never touches the database.

        Synchronous on purpose. An `async` version would have to be awaited from
        the middleware and the tool wrapper, which makes recording a suspension
        point on the request path — and a suspension point is where a slow drain
        starts costing request latency instead of just accuracy.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # The bounded queue doing its job. Counted, so sustained overload
            # shows up as a rising drop rate rather than as a graph that quietly
            # under-reports.
            observe_dead_lettered(queue=_QUEUE)
        except Exception:
            # Belt and braces. Whatever else went wrong, the caller's request is
            # not the place to find out about it.
            _log.debug("usage: enqueue failed", exc_info=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._drain_forever(), name="usage-writer-drain")

    async def stop(self) -> None:
        """Stop draining, after one last flush of whatever is queued.

        The final flush is why this is not just a cancel: a graceful shutdown
        should not throw away events that were already accepted, and on a
        rolling deploy that is most of a batch.
        """
        self._stopping = True
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await self._flush_once()

    # ------------------------------------------------------------------
    # The drain
    # ------------------------------------------------------------------

    async def _drain_forever(self) -> None:
        while not self._stopping:
            try:
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed flush must not kill the drain. If it did, the first
                # transient database error would silently stop recording for the
                # life of the process — and the only symptom would be a flat graph.
                _log.warning("usage: flush failed; continuing", exc_info=True)
            await asyncio.sleep(self._flush_interval_s)

    async def _flush_once(self) -> None:
        batch = self._take_batch()
        observe_queue_depth(queue=_QUEUE, depth=self._queue.qsize())
        if not batch:
            return
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(_INSERT, [_params(e) for e in batch])
        except Exception:
            # The batch is gone. Counted as dropped rather than requeued: a retry
            # loop in front of an unavailable database is how a bounded buffer
            # becomes an unbounded one, and these rows are not worth that.
            observe_dead_lettered(queue=_QUEUE, count=len(batch))
            raise

    def _take_batch(self) -> list[UsageEvent]:
        batch: list[UsageEvent] = []
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch
