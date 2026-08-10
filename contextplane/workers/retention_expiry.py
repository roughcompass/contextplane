"""The retention clock, running: reduce what is due, and schedule what it derived.

Every record class carries an approved retention period, and until this worker
existed nothing consulted one on a timer. Erasure covered the case where somebody
asks; this covers the case where nobody does — which is most records, most of the
time, and the case a retention policy is actually about.

Two obligations, and they are separate on purpose:

**Content that is past its clock is reduced by the family that owns it.** The
families are `signals`, `context` and `workspaces`, all of which sit *above* this
package in the import contract, so this worker cannot name one. It takes the
reductions as callables wiring hands it. That is not indirection for testability;
it is the only direction the layering permits, and a version of this module that
imported a family to call it would invert the graph.

**A derivative past its own expiry is queued for removal here.** Its expiry is the
minimum across every source it read, copied at registration, so "past due" is one
indexed comparison rather than a join across five tables that each store expiry
differently. Enqueuing is all this does: the drain applies it, and separating the
two means a handler that fails leaves a retryable row rather than an expiry pass
that has to be re-run from the start.

**Nothing is deleted without asking about holds.** `partition_by_hold` is called
before anything is enqueued, and what it holds back is reported rather than
skipped. A paused clock defeats the fail-closed overdue behaviour by design, so
the records it is defeating it for have to be visible to an operator — a
suspended deletion that nobody can attribute to a hold is indistinguishable from
one that was simply missed.

**Per-tenant, in bounded batches.** A tenant with a large backlog is drained over
several passes rather than one long transaction, and a pass that stops on the
ceiling says so, because a backlog outpacing the schedule is a different problem
from one that is current and reads identically in a bare count.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.metrics import observe_worker_run
from contextplane.retention import derivatives, holds, policies
from contextplane.types import Clock, SystemClock, TenantContext

_log = logging.getLogger(__name__)

#: Rows one tenant contributes to a single pass. Bounded so a tenant with a large
#: backlog cannot hold a transaction open across all of it.
DEFAULT_BATCH = 500

#: How many passes one tick makes over a tenant before it stops and says so. A
#: backlog needing more than this is not being kept up with, which an operator
#: should hear rather than infer from a count that never reaches zero.
MAX_BATCHES = 20

#: The identity background work runs under. A nil UUID cannot collide with an
#: actor created through the REST surface, which uses `gen_random_uuid()`.
_SYSTEM_ACTOR_ID = uuid.UUID(int=0)

#: Every tenant that could hold a due record. Disabled tenants are included
#: deliberately: a tenant that stopped being served did not stop being subject to
#: retention, and skipping them is how content outlives its period by being
#: forgotten rather than by being kept.
_TENANTS_SQL = "SELECT tenant_id FROM tenants"

#: Derivatives whose own expiry has passed, oldest first. The expiry was computed
#: as the minimum across every source at registration time, so this comparison is
#: the whole of "no source that built this is still retainable".
#:
#: Items already queued for this cause are excluded here as well as by the
#: outbox's own uniqueness: the unique index makes a re-enqueue a no-op, but
#: without this clause every pass would re-select the same rows and a backlog
#: would never advance past its first batch.
_DUE_DERIVATIVES_SQL = """
SELECT r.derivative_id
  FROM derivative_registrations AS r
 WHERE r.tenant_id = :tenant
   AND r.expires_at <= :now
   AND NOT EXISTS (
       SELECT 1 FROM derivative_work_outbox AS w
        WHERE w.derivative_id = r.derivative_id
          AND w.operation = :operation
          AND w.trigger = :trigger
          AND w.tombstone_id IS NULL
   )
 ORDER BY r.blocking DESC, r.expires_at ASC
 LIMIT :limit
"""

#: One item per derivative per cause. `ON CONFLICT DO NOTHING` rather than a
#: read-then-write for the reason the erasure enqueuer gives: the check and the
#: insert are one statement, so two overlapping ticks enqueue once between them.
#:
#: `tombstone_id` stays NULL — an expiry has no tombstone to name, and the outbox
#: index is `NULLS NOT DISTINCT` precisely so that idempotence still holds for the
#: triggers that have none.
_ENQUEUE_EXPIRED_SQL = """
INSERT INTO derivative_work_outbox (tenant_id, derivative_id, operation, trigger, available_at)
SELECT r.tenant_id, r.derivative_id, CAST(:operation AS text), CAST(:trigger AS text), CAST(:now AS timestamptz)
  FROM derivative_registrations AS r
 WHERE r.derivative_id = ANY(:ids)
ON CONFLICT DO NOTHING
RETURNING work_id
"""


@dataclasses.dataclass(frozen=True)
class ExpiryMinimizer:
    """One family's content-clock reduction, named by the class it reduces.

    `record_class` is carried beside the callable so the sweep's log and report
    say which policy did the work, rather than an index into a list wiring
    happened to order a particular way. The callable returns how many rows it
    reduced; it consults the hold seam itself, because it is the thing that knows
    which rows it is about to touch.
    """

    record_class: str
    reduce: Callable[[TenantContext, datetime.datetime], Awaitable[int]]


@dataclasses.dataclass(frozen=True)
class RetentionExpiryReport:
    """What one tick did, in the terms an operator asks about.

    `held` is separate from everything else because it is the one number that
    means "correctly not done": records past their period that a legal hold is
    keeping. A tick with a large `held` and nothing enqueued is healthy; the same
    tick with `held` folded into a skip count would be unreadable.
    """

    tenants: int = 0
    minimized: int = 0
    enqueued: int = 0
    held: int = 0
    truncated: bool = False
    ran_at: datetime.datetime | None = None

    @property
    def had_work(self) -> bool:
        return bool(self.minimized or self.enqueued)


class RetentionExpiryWorker:
    """Sweeps every tenant's due records through the approved retention policy.

    Takes its family reductions as injected callables and its hold store as a
    parameter. Both are wiring's to choose: the layering forbids this package
    naming a family, and the hold store is a deployment's — the shipped one can
    hold nothing, and the seam is what lets a real one drop in without revisiting
    a single call site.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        hold_store: holds.HoldStore,
        *,
        minimizers: Sequence[ExpiryMinimizer] = (),
        clock: Clock | None = None,
        batch_size: int = DEFAULT_BATCH,
    ) -> None:
        self._session_factory = session_factory
        self._holds = hold_store
        self._minimizers = tuple(minimizers)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._batch_size = batch_size

    async def run_once(self) -> RetentionExpiryReport:
        """Timed wrapper. The work itself is in `_run_inner`.

        Background work is the one place a failure is otherwise invisible: nothing
        is on a request path, so nobody receives an error and the only symptom is
        a retention period quietly not being enforced.
        """
        with observe_worker_run("retention_expiry"):
            return await self._run_inner()

    async def _run_inner(self) -> RetentionExpiryReport:
        now = self._clock.now()
        report = RetentionExpiryReport(ran_at=now)

        for tenant_id in await self._tenants():
            report = dataclasses.replace(report, tenants=report.tenants + 1)
            report = await self._sweep_tenant(tenant_id, now, report)

        if report.truncated:
            _log.warning(
                "retention_expiry.truncated: stopped at the batch ceiling with work remaining; "
                "the retention sweep is not keeping up"
            )
        return report

    async def _tenants(self) -> list[uuid.UUID]:
        async with self._session_factory() as session:
            rows = (await session.execute(text(_TENANTS_SQL))).all()
        return [uuid.UUID(str(row[0])) for row in rows]

    async def _sweep_tenant(
        self,
        tenant_id: uuid.UUID,
        now: datetime.datetime,
        report: RetentionExpiryReport,
    ) -> RetentionExpiryReport:
        """One tenant: reduce due content, then queue every over-age derivative."""
        ctx = TenantContext(tenant_id=tenant_id, actor_id=_SYSTEM_ACTOR_ID, roles=["system"])

        for minimizer in self._minimizers:
            reduced = await minimizer.reduce(ctx, now)
            if reduced:
                _log.info(
                    "retention_expiry.minimized: tenant=%s record_class=%s rows=%d",
                    tenant_id,
                    minimizer.record_class,
                    reduced,
                )
            report = dataclasses.replace(report, minimized=report.minimized + reduced)

        batches = 0
        while batches < MAX_BATCHES:
            enqueued, held, exhausted = await self._expire_derivatives(tenant_id, now)
            report = dataclasses.replace(
                report,
                enqueued=report.enqueued + enqueued,
                held=report.held + held,
            )
            batches += 1
            if exhausted:
                return report

        return dataclasses.replace(report, truncated=True)

    async def _expire_derivatives(
        self,
        tenant_id: uuid.UUID,
        now: datetime.datetime,
    ) -> tuple[int, int, bool]:
        """One bounded batch: select due, consult holds, enqueue the rest.

        Returns what it enqueued, what a hold kept, and whether this tenant is
        drained — "drained" meaning the selection came back short of the batch
        size, which is the only signal that does not require a second query.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(_DUE_DERIVATIVES_SQL),
                    {
                        "tenant": tenant_id,
                        "now": now,
                        "operation": derivatives.OPERATION_DELETE,
                        "trigger": derivatives.TRIGGER_EXPIRY,
                        "limit": self._batch_size,
                    },
                )
            ).all()
            due = [uuid.UUID(str(row[0])) for row in rows]
            exhausted = len(due) < self._batch_size

            deletable, held = await holds.partition_by_hold(
                self._holds, tenant_id, policies.RECORD_DERIVATIVE, due, now=now
            )
            if held:
                # Named, not counted away. A hold is the only legitimate reason a
                # record outlives its period, and it has to be attributable.
                _log.info(
                    "retention_expiry.held: tenant=%s record_class=%s held=%d",
                    tenant_id,
                    policies.RECORD_DERIVATIVE,
                    len(held),
                )
            if not deletable:
                return 0, len(held), exhausted

            result = await session.execute(
                text(_ENQUEUE_EXPIRED_SQL),
                {
                    "ids": list(deletable),
                    "operation": derivatives.OPERATION_DELETE,
                    "trigger": derivatives.TRIGGER_EXPIRY,
                    "now": now,
                },
            )
            enqueued = len(result.fetchall())
            await session.commit()

        if enqueued:
            _log.info(
                "retention_expiry.enqueued: tenant=%s trigger=%s items=%d",
                tenant_id,
                derivatives.TRIGGER_EXPIRY,
                enqueued,
            )
        return enqueued, len(held), exhausted

    async def held_overdue(self, tenant_id: uuid.UUID) -> Sequence[holds.HeldOverdue]:
        """Every record a hold is keeping past its period, for the operator report.

        On the worker rather than read straight off the store because the caller
        that wants this is the one that wants the sweep's own clock, and reading
        the store with a different `now` would report a hold as active that this
        pass had already treated as lapsed.
        """
        return await self._holds.held_overdue(tenant_id, now=self._clock.now())


__all__ = [
    "DEFAULT_BATCH",
    "MAX_BATCHES",
    "ExpiryMinimizer",
    "RetentionExpiryReport",
    "RetentionExpiryWorker",
]
