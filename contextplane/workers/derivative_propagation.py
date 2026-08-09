"""The propagation drain: do the work an erasure, expiry or revocation enqueued.

`retention.derivatives.enqueue_for_sources` decides *that* a derivative must be
rebuilt, deleted or redacted and writes one outbox item per cause. This worker is
what makes that true of the artefact itself. Until it runs, an erased person's
words are still in the vector, the summary and the export — the outbox row is a
promise, not a deletion.

**Each item is its own transaction.** A handler that fails on one derivative must
not roll back the twenty that already succeeded, because the successful ones are
deletions and re-doing them is not free: a rebuild re-reads sources that may
themselves be mid-erasure. Committing per item also means a crash resumes where it
stopped rather than at the start of the batch.

**A failure retries with backoff written to the row, never slept in the worker.**
A worker that slept would hold a connection and a scheduler slot doing nothing and
would lose its place on restart. `available_at` survives both.

**Retries are bounded, and exhaustion is loud rather than silent.** An item that
has failed `MAX_ATTEMPTS` times stops being retried and becomes `failed`, because
the alternative — retrying forever — turns a broken handler into a queue that looks
busy while the erased content stays where it is. A `failed` item is a compliance
incident and reads as one: `pending_overdue` counts it, and nothing clears it but a
fix and a re-enqueue.

**Claiming is `SELECT ... FOR UPDATE SKIP LOCKED`,** the same claim the other
drains use, so two instances and two overlapping ticks cannot process one item
twice. Idempotence in the handlers makes a double-apply harmless; the lock makes it
rare enough that the harmlessness is never load-bearing.

**Blocking derivatives go first.** A derivative marked blocking is one whose
continued existence makes a read unsafe — the fail-closed overdue behaviour keys off
it — so a queue that drained oldest-first would leave the dangerous artefacts behind
the harmless ones. Ordering is blocking, then oldest.

**This module is inert as shipped, and that is stated here rather than left to be
discovered.** Nothing constructs it and no scheduler job runs it, because no handler
exists for any derivative kind yet: a handler has to delete a vector, a full-text
document or an export, which means calling the subsystem that owns that artefact,
and this package sits below all of them in the import contract. So the handlers
belong with their artefacts and their registration belongs in the scheduler wiring —
both outside this module, and both still to be built.

The consequence is worth being blunt about, because a reader who assumed otherwise
would draw exactly the wrong conclusion from the code above: **an erasure currently
writes its tombstone, enqueues one propagation item per derivative, and nothing ever
applies them.** The queue grows and the artefacts stay. Every mechanism described
above is correct and tested; none of it runs. A release gate asserting that every
kind has a handler belongs with the change that adds the first one — asserting it
here would fail on the deliberate, recorded state of the tree rather than on a
defect, and a gate that is red by design teaches everyone to ignore it.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.retention import derivatives

_log = logging.getLogger(__name__)

#: How many times one item is retried before it is declared failed. Five attempts
#: across the backoff below spans roughly twenty minutes, which is long enough to
#: outlast a redeploy and short enough that a genuinely broken handler surfaces
#: within one operator's attention span rather than overnight.
MAX_ATTEMPTS = 5

#: Seconds to wait before attempt n+1. Flat-then-widening rather than exponential:
#: the failures this retries are locks and transient connection loss, which clear in
#: seconds, and an exponential tail would leave a blocking derivative alive for hours
#: over a fault that resolved in one.
BACKOFF_SECONDS: tuple[int, ...] = (5, 15, 60, 300)

STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_FAILED = "failed"


@dataclasses.dataclass(frozen=True)
class PropagationReport:
    """What one tick did, in the terms an operator asks about.

    `failed` is separate from `attempted` because they mean different things to a
    reader: an attempt that failed will be retried, and an item that reached
    `failed` will not be. Only the second is an incident.
    """

    claimed: int = 0
    applied: int = 0
    artefacts: int = 0
    retried: int = 0
    failed: int = 0

    @property
    def had_work(self) -> bool:
        return self.claimed > 0


def _backoff(attempts: int) -> datetime.timedelta:
    """How long before the next attempt, given how many have already failed."""
    index = min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)
    return datetime.timedelta(seconds=BACKOFF_SECONDS[index])


class DerivativePropagationWorker:
    """Drains `derivative_work_outbox` through the handler registered for each kind.

    Takes the handler registry rather than importing one, because the set of
    derivative kinds a deployment actually builds is a wiring decision and a worker
    that reached for a module-level registry would be untestable without one.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: derivatives.HandlerRegistry,
        *,
        batch_size: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._batch_size = batch_size

    async def run_once(self, *, now: datetime.datetime | None = None) -> PropagationReport:
        """Claim and apply up to one batch, and report what happened."""
        moment = now or datetime.datetime.now(datetime.UTC)
        report = PropagationReport()

        for item in await self._claim(moment):
            report = dataclasses.replace(report, claimed=report.claimed + 1)
            report = await self._apply_one(item, moment, report)

        return report

    async def _claim(self, now: datetime.datetime) -> list[dict[str, object]]:
        """Take a batch of due items, skipping any another instance already holds."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT w.work_id, w.tenant_id, w.derivative_id, w.operation,
                               w.trigger, w.attempts, r.derivative_kind, r.storage_locator,
                               r.audience_partition, r.classification, r.expires_at, r.blocking
                        FROM derivative_work_outbox AS w
                        JOIN derivative_registrations AS r
                          ON r.derivative_id = w.derivative_id
                        WHERE w.state = :pending AND w.available_at <= :now
                        ORDER BY r.blocking DESC, w.available_at ASC
                        LIMIT :limit
                        FOR UPDATE OF w SKIP LOCKED
                        """
                    ),
                    {"pending": STATE_PENDING, "now": now, "limit": self._batch_size},
                )
            ).mappings().all()

            if rows:
                await session.execute(
                    text(
                        "UPDATE derivative_work_outbox SET claimed_at = :now "
                        "WHERE work_id = ANY(:ids)"
                    ),
                    {"now": now, "ids": [row["work_id"] for row in rows]},
                )
            await session.commit()
            return [dict(row) for row in rows]

    async def _apply_one(
        self,
        item: dict[str, object],
        now: datetime.datetime,
        report: PropagationReport,
    ) -> PropagationReport:
        """Apply one item in its own transaction, and record the outcome on the row."""
        work_id = item["work_id"]
        kind = str(item["derivative_kind"])
        registration = derivatives.Registration(
            derivative_id=item["derivative_id"],  # type: ignore[arg-type]
            tenant_id=item["tenant_id"],  # type: ignore[arg-type]
            derivative_kind=kind,
            storage_locator=str(item["storage_locator"]),
            audience_partition=str(item["audience_partition"]),
            classification=str(item["classification"]),
            expires_at=item["expires_at"],  # type: ignore[arg-type]
            blocking=bool(item["blocking"]),
        )

        try:
            handler = self._registry.handler_for(kind)
            async with self._session_factory() as session:
                touched = await handler.apply(session, registration, str(item["operation"]))
                await session.execute(
                    text(
                        "UPDATE derivative_work_outbox "
                        "SET state = :done, completed_at = :now, last_error = NULL "
                        "WHERE work_id = :id"
                    ),
                    {"done": STATE_DONE, "now": now, "id": work_id},
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — recorded on the row, then re-queued
            # Deliberately broad. A handler reaches storage this worker knows
            # nothing about, so the exception types are open-ended, and the
            # response is the same for all of them: record it where an operator
            # will look and let the row decide whether to try again.
            return await self._record_failure(item, now, exc, report)

        _log.info(
            "derivative_propagation.applied: kind=%s operation=%s derivative=%s artefacts=%d",
            kind,
            item["operation"],
            registration.derivative_id,
            touched,
        )
        return dataclasses.replace(
            report, applied=report.applied + 1, artefacts=report.artefacts + touched
        )

    async def _record_failure(
        self,
        item: dict[str, object],
        now: datetime.datetime,
        exc: Exception,
        report: PropagationReport,
    ) -> PropagationReport:
        prior = item["attempts"]
        attempts = (int(prior) if isinstance(prior, int) else 0) + 1
        exhausted = attempts >= MAX_ATTEMPTS

        async with self._session_factory() as session:
            await session.execute(
                text(
                    "UPDATE derivative_work_outbox "
                    "SET attempts = :attempts, last_error = :error, state = :state, "
                    "    available_at = :available, claimed_at = NULL "
                    "WHERE work_id = :id"
                ),
                {
                    "attempts": attempts,
                    "error": f"{type(exc).__name__}: {exc}"[:2000],
                    "state": STATE_FAILED if exhausted else STATE_PENDING,
                    "available": now + _backoff(attempts),
                    "id": item["work_id"],
                },
            )
            await session.commit()

        if exhausted:
            # Loud, and named as what it is. The artefact still holds the content
            # this item was scheduled to remove.
            _log.error(
                "derivative_propagation.exhausted: kind=%s derivative=%s attempts=%d error=%s "
                "-- erased content may remain in this artefact",
                item["derivative_kind"],
                item["derivative_id"],
                attempts,
                exc,
            )
            return dataclasses.replace(report, failed=report.failed + 1)

        _log.warning(
            "derivative_propagation.retrying: kind=%s derivative=%s attempt=%d error=%s",
            item["derivative_kind"],
            item["derivative_id"],
            attempts,
            exc,
        )
        return dataclasses.replace(report, retried=report.retried + 1)


async def pending_overdue(
    session: AsyncSession, *, now: datetime.datetime, blocking_only: bool = False
) -> int:
    """How many items are past due, for the read paths that must fail closed.

    A blocking derivative whose propagation has not run is the case that makes a
    read unsafe, so the reader asks this before serving rather than trusting the
    queue to be empty. `failed` items count: an item nobody will retry is more
    overdue than one that is about to run, not less.
    """
    clause = "AND r.blocking IS TRUE" if blocking_only else ""
    result = await session.execute(
        text(
            f"""
            SELECT count(*) FROM derivative_work_outbox AS w
            JOIN derivative_registrations AS r ON r.derivative_id = w.derivative_id
            WHERE w.state IN (:pending, :failed) AND w.available_at <= :now {clause}
            """  # noqa: S608 — `clause` is a literal chosen here, not caller input
        ),
        {"pending": STATE_PENDING, "failed": STATE_FAILED, "now": now},
    )
    return int(result.scalar_one())
