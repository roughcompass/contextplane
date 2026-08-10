"""Materializing aggregate cells, and never publishing a difference between two.

Nothing wrote `privacy_aggregates` before this module. The read surfaces compute
their breakdowns live and floor them on the way out, which is correct for a
question asked now; it is not a stored series, and a stored series is what an
owner asks for when the question is whether a number moved. This writer is that
series, and storing it is what makes the hard part hard.

**The hard part is not the floors — it is the recompute.** A cell computed over a
window, published, and then computed again after an erasure discloses the erased
subject's exact contribution as the difference between the two figures. Every
floor in this system holds perfectly while that happens: both figures clear the
minimum, neither names anybody, and subtracting them names one person's
contribution precisely. So the rule this module exists to enforce is not "floor
the cell" but **a cell whose recomputed value disagrees with the one already
published is withheld from then on, and nothing is ever published for it again.**

That is why a bare re-run of the aggregation job is the forbidden design, and why
it is forbidden even though a bare re-run enforces every floor correctly.

**Three mechanisms carry it, and none of them is a step somebody remembers.**

1. *One version of a cell, ever.* The table's unique cell key makes a predecessor
   unstorable, so a recompute has nowhere to leave the figure it replaced. The
   upsert below is the only writer, and it is a single statement: the comparison
   between the stored figure and the new one happens inside the database, against
   the row it is about to overwrite, with no window in which two versions exist.
2. *Withholding is one-way.* Once a cell is withheld it stays withheld through
   every later pass. Stickiness is the half that is easy to leave out and fatal
   to omit: a cell suppressed at the erasure and recomputed cleanly a day later
   would publish the post-erasure figure beside the reader's memory of the
   pre-erasure one, which is the same subtraction one pass later.
3. *A withheld cell keeps no counts.* Its value goes to NULL and its actor count
   to zero, because an actor count is a figure too — a cell that withheld its
   value while reporting that it now covers six actors instead of seven has
   disclosed that the erased subject was one person.

**Windows are keyed on the row's own write instant, never on the time it reports.**
`created_at` and `ingested_at` are assigned when the row is stored, so a window
that has ended can never gain a row: its membership only ever shrinks, and it
shrinks exactly when something is erased, minimized, revoked or expired. That is
what makes mechanism 1 sound. Windowing on `event_time` — the instant the source
says the thing happened — would let a late submission change a published cell, and
then "the value changed" would no longer mean "something was removed" and every
late arrival would withhold a cell that had nothing wrong with it.

**Erasure is what triggers the recompute, and the tombstone ledger is how it is
noticed.** A tombstone records that a record was erased and when, but not what
window the erased record sat in — the row is gone, so nothing can be joined back
to it. So the trigger is deliberately coarse: a tenant with a tombstone newer than
a stored cell's computation has every such cell recomputed, and the comparison
above decides which of them actually moved. Coarse in what it re-examines, exact
in what it withholds.

**No cohort finer than the tenant.** The cohort is a literal for the reason the
read surfaces give: there is no membership model to group by, and inventing one
would build the per-team performance surface the policy forbids rather than one
this module is merely withholding pending a floor.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.metrics import observe_worker_run
from contextplane.retention import policies
from contextplane.service.memory.learning_reads import (
    COHORT_TENANT,
    Breakdown,
    Cell,
    Floors,
    build_breakdown,
)
from contextplane.signals.feedback import KIND_DIAGNOSTIC
from contextplane.types import Clock, SystemClock

_log = logging.getLogger(__name__)

METRIC_FEEDBACK_RATING_MIX = "feedback_rating_mix"
METRIC_SIGNAL_SOURCE_MIX = "signal_source_mix"

#: Every metric this writer materializes, closed so a pin can assert the whole set
#: and so a metric cannot be computed by one pass and forgotten by the next.
AGGREGATE_METRICS: tuple[str, ...] = (
    METRIC_FEEDBACK_RATING_MIX,
    METRIC_SIGNAL_SOURCE_MIX,
)

#: Which record class each metric is built from, so the cell inherits that class's
#: retention rather than a period chosen here. An aggregate is a derivative of its
#: sources and must not outlive them.
_SOURCE_CLASS_FOR: dict[str, str] = {
    METRIC_FEEDBACK_RATING_MIX: policies.RECORD_CONTEXT_FEEDBACK,
    METRIC_SIGNAL_SOURCE_MIX: policies.RECORD_EXTERNAL_SIGNAL,
}

#: The erasures that can move one of these cells. A tombstone for any other class
#: is real but irrelevant here, and treating it as a trigger would recompute every
#: window on every unrelated erasure.
_SOURCE_CLASSES: tuple[str, ...] = tuple(dict.fromkeys(_SOURCE_CLASS_FOR.values()))

#: One day per cell. Coarse deliberately: a finer window over a small population is
#: a timeline of when individuals were active, which is the shape these aggregates
#: exist to avoid rather than a resolution they happen not to offer.
WINDOW = datetime.timedelta(days=1)

#: How many complete windows one pass recomputes. More than one because a pass can
#: be missed and a window nobody computed is a hole in the series; bounded because
#: recomputing the whole history every hour would be the same work forever.
TRAILING_WINDOWS = 7

#: Cells one tenant re-examines per pass after an erasure. Bounded for the reason
#: every sweep here is bounded: a tenant with a long history must not hold one
#: transaction open across all of it.
DEFAULT_SUSPECT_BATCH = 200

_TENANTS_SQL = "SELECT tenant_id FROM tenants"

#: The newest erasure that could have moved one of this tenant's cells. `max` and
#: not a row scan: what is needed is a watermark to compare computations against,
#: not the tombstones themselves, and none of them says which window it touched.
_ERASURE_WATERMARK_SQL = """
SELECT max(effective_at) AS latest
  FROM source_tombstones
 WHERE tenant_id = :tenant
   AND record_class = ANY(:classes)
"""

#: Cells computed before that watermark, which is exactly the set an erasure may
#: have invalidated. Already-withheld cells are excluded: withholding is one-way,
#: so recomputing one could only confirm what it already refuses to say.
_SUSPECT_CELLS_SQL = """
SELECT metric, window_start, window_end
  FROM privacy_aggregates
 WHERE tenant_id = :tenant
   AND cohort_key = :cohort
   AND NOT suppressed
   AND computed_at < :watermark
 ORDER BY window_start DESC
 LIMIT :limit
"""

#: Both a per-rating breakdown and the window's own distinct-reporter count, in one
#: statement. The grouping set matters: summing per-label distinct counts would
#: count a reporter once per rating they used and inflate the very figure the actor
#: floor is tested against, and two statements would let a write land between them.
#:
#: Diagnostics are excluded in the WHERE clause rather than filtered afterwards,
#: matching the read surfaces: a report about the system's own plumbing is not a
#: verdict on served context, and a burst of them would read as a collapse in it.
_FEEDBACK_MIX_SQL = """
SELECT
    rating AS label,
    count(*) AS event_count,
    count(DISTINCT reporter_id) AS actor_count,
    GROUPING(rating) AS is_total
FROM context_feedback
WHERE tenant_id = :tenant
  AND kind <> :diagnostic_kind
  AND created_at >= :window_start
  AND created_at < :window_end
GROUP BY GROUPING SETS ((rating), ())
"""

#: The same shape over the signal ledger. Revoked signals are left out: a source
#: that withdrew an observation withdrew it, and counting it would make revocation
#: visible only as a number that never moves.
_SIGNAL_MIX_SQL = """
SELECT
    source_system AS label,
    count(*) AS event_count,
    count(DISTINCT producer_id) AS actor_count,
    GROUPING(source_system) AS is_total
FROM external_signals
WHERE tenant_id = :tenant
  AND revoked_at IS NULL
  AND ingested_at >= :window_start
  AND ingested_at < :window_end
GROUP BY GROUPING SETS ((source_system), ())
"""

_MIX_SQL_FOR: dict[str, str] = {
    METRIC_FEEDBACK_RATING_MIX: _FEEDBACK_MIX_SQL,
    METRIC_SIGNAL_SOURCE_MIX: _SIGNAL_MIX_SQL,
}

#: The only statement that writes this table, and the whole differencing defence.
#:
#: `withhold` is evaluated against the row being overwritten, inside the database,
#: so there is no read-then-write a concurrent pass could interleave with. It is
#: true in three cases, and the third is the one this module exists for: the cell
#: was already withheld (withholding is one-way), the new computation does not
#: clear the floors, or the new value disagrees with the published one — which,
#: over a window that can only shrink, means something in it was removed.
#:
#: A withheld cell keeps no figures at all. `value` goes to NULL because the
#: table's own CHECK refuses a suppressed cell that carries one, and `actor_count`
#: goes to zero because the count is itself a figure two versions can be
#: subtracted from.
_UPSERT_CELL_SQL = """
INSERT INTO privacy_aggregates (
    tenant_id, cohort_key, metric, window_start, window_end,
    actor_count, value, suppressed, partial, policy_version, computed_at, expires_at
)
VALUES (
    :tenant, :cohort, :metric, :window_start, :window_end,
    :actor_count, CAST(:value AS jsonb), :suppressed, :partial,
    :policy_version, :now, :expires_at
)
ON CONFLICT (tenant_id, cohort_key, metric, window_start, window_end) DO UPDATE SET
    suppressed = (
        privacy_aggregates.suppressed
        OR EXCLUDED.suppressed
        OR privacy_aggregates.value IS DISTINCT FROM EXCLUDED.value
    ),
    value = CASE
        WHEN privacy_aggregates.suppressed
             OR EXCLUDED.suppressed
             OR privacy_aggregates.value IS DISTINCT FROM EXCLUDED.value
        THEN NULL
        ELSE EXCLUDED.value
    END,
    actor_count = CASE
        WHEN privacy_aggregates.suppressed
             OR EXCLUDED.suppressed
             OR privacy_aggregates.value IS DISTINCT FROM EXCLUDED.value
        THEN 0
        ELSE EXCLUDED.actor_count
    END,
    partial = CASE
        WHEN privacy_aggregates.suppressed
             OR EXCLUDED.suppressed
             OR privacy_aggregates.value IS DISTINCT FROM EXCLUDED.value
        THEN FALSE
        ELSE EXCLUDED.partial
    END,
    policy_version = EXCLUDED.policy_version,
    computed_at = EXCLUDED.computed_at,
    expires_at = EXCLUDED.expires_at
RETURNING suppressed
"""


@dataclasses.dataclass(frozen=True)
class PrivacyAggregateReport:
    """What one pass did, in the terms an operator asks about.

    `withheld` is reported separately from `written` because it is not a failure
    and not a skip: it is the differencing defence doing its job, and a pass whose
    withheld count jumps is a pass that followed an erasure. Folded into either of
    the other two it would be unreadable.
    """

    tenants: int = 0
    written: int = 0
    withheld: int = 0
    recomputed: int = 0
    ran_at: datetime.datetime | None = None

    @property
    def had_work(self) -> bool:
        """Whether this pass touched a cell at all, either way."""
        return bool(self.written or self.withheld)


def _window_bounds(now: datetime.datetime, *, back: int) -> tuple[datetime.datetime, datetime.datetime]:
    """The `back`-th complete window before `now`, counting 1 as the most recent.

    Never the window in progress. A window still accepting rows would be published
    and then legitimately recomputed to a different figure, which the upsert would
    correctly read as a removal and withhold — turning the defence into a machine
    that withholds every cell it has ever written.
    """
    midnight = datetime.datetime.combine(now.date(), datetime.time.min, tzinfo=datetime.UTC)
    end = midnight - WINDOW * (back - 1)
    return end - WINDOW, end


def _cell_expiry(metric: str, window_end: datetime.datetime) -> datetime.datetime:
    """When this cell stops being retainable: its source class's own deadline.

    Anchored at the window's end rather than at the computation, so a recompute
    cannot extend the life of an aggregate over old records by touching it.

    No fallback is offered. Every class read here is bounded today, and one that
    later became event-bounded would leave no instant this function could honestly
    supply — so the refusal stands rather than a guessed expiry being written onto
    a stored aggregate over records somebody may already have asked to erase.
    """
    return policies.minimum_expiry([policies.expiry_deadline(_SOURCE_CLASS_FOR[metric], window_end)])


class PrivacyAggregateWriter:
    """Writes the stored aggregate series, and refuses to publish a difference.

    Constructed once and run on an interval. Every write goes through one upsert,
    which is what makes "one version of a cell, ever" a property of the statement
    rather than of the order this class happens to do things in.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        floors: Floors | None = None,
        clock: Clock | None = None,
        trailing_windows: int = TRAILING_WINDOWS,
        suspect_batch: int = DEFAULT_SUSPECT_BATCH,
    ) -> None:
        self._session_factory = session_factory
        # `Floors` refuses anything looser than the approved minima at
        # construction, so a deployment that configured less finds out here rather
        # than in a published cell.
        self._floors = floors or Floors()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._trailing_windows = trailing_windows
        self._suspect_batch = suspect_batch

    @property
    def floors(self) -> Floors:
        """The floors in force, so a caller can report them beside the series."""
        return self._floors

    async def run_once(self) -> PrivacyAggregateReport:
        """Timed wrapper. The work itself is in `_run_inner`.

        Background work is the one place a failure is otherwise invisible: nothing
        is on a request path, so nobody receives an error and the only symptom is a
        series that quietly stops advancing.
        """
        with observe_worker_run("privacy_aggregates"):
            return await self._run_inner()

    async def _run_inner(self) -> PrivacyAggregateReport:
        now = self._clock.now()
        report = PrivacyAggregateReport(ran_at=now)
        for tenant_id in await self._tenants():
            report = dataclasses.replace(report, tenants=report.tenants + 1)
            report = await self._sweep_tenant(tenant_id, now, report)
        return report

    async def _tenants(self) -> list[uuid.UUID]:
        async with self._session_factory() as session:
            rows = (await session.execute(text(_TENANTS_SQL))).all()
        return [uuid.UUID(str(row[0])) for row in rows]

    async def _sweep_tenant(
        self,
        tenant_id: uuid.UUID,
        now: datetime.datetime,
        report: PrivacyAggregateReport,
    ) -> PrivacyAggregateReport:
        """One tenant: the trailing windows, then whatever an erasure invalidated.

        In that order on purpose. The trailing pass is what advances the series;
        the erasure pass is what retracts from it, and a retraction must not be
        undone by the advance in the same tick.
        """
        due: list[tuple[str, datetime.datetime, datetime.datetime]] = []
        for back in range(1, self._trailing_windows + 1):
            window_start, window_end = _window_bounds(now, back=back)
            due.extend((metric, window_start, window_end) for metric in AGGREGATE_METRICS)

        suspect = await self._suspect_cells(tenant_id)
        report = dataclasses.replace(report, recomputed=report.recomputed + len(suspect))
        # A cell can be in both lists — a recent erasure against a recent window —
        # and computing it twice in one tick is harmless but pointless.
        seen = set(due)
        due.extend(cell for cell in suspect if cell not in seen)

        async with self._session_factory() as session:
            for metric, window_start, window_end in due:
                withheld = await self._write_cell(
                    session,
                    tenant_id,
                    metric=metric,
                    window_start=window_start,
                    window_end=window_end,
                    now=now,
                )
                report = dataclasses.replace(
                    report,
                    written=report.written + (0 if withheld else 1),
                    withheld=report.withheld + (1 if withheld else 0),
                )
            await session.commit()
        return report

    async def _suspect_cells(
        self,
        tenant_id: uuid.UUID,
    ) -> list[tuple[str, datetime.datetime, datetime.datetime]]:
        """Every published cell an erasure may have invalidated, newest window first.

        The watermark is the newest tombstone against a class these cells are built
        from. Nothing narrower is available: an erased record leaves no row to join
        its window back from, which is the point of erasing it.
        """
        async with self._session_factory() as session:
            watermark = (
                await session.execute(
                    text(_ERASURE_WATERMARK_SQL),
                    {"tenant": tenant_id, "classes": list(_SOURCE_CLASSES)},
                )
            ).scalar()
            if watermark is None:
                return []
            rows = (
                await session.execute(
                    text(_SUSPECT_CELLS_SQL),
                    {
                        "tenant": tenant_id,
                        "cohort": COHORT_TENANT,
                        "watermark": watermark,
                        "limit": self._suspect_batch,
                    },
                )
            ).all()

        if len(rows) == self._suspect_batch:
            # Said out loud rather than left to a series that catches up eventually:
            # a tenant whose backlog exceeds one batch has published cells still
            # carrying a figure an erasure has already invalidated.
            _log.warning(
                "privacy_aggregates.recompute_truncated: tenant=%s stopped at the batch "
                "ceiling with erasure-invalidated cells remaining",
                tenant_id,
            )
        return [(str(row.metric), row.window_start, row.window_end) for row in rows]

    async def _write_cell(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        metric: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        now: datetime.datetime,
    ) -> bool:
        """Compute one cell and upsert it. Returns whether it ended up withheld.

        A window holding nothing is written too, as a withheld cell. That is not
        noise: it is the difference between "computed, nothing to report" and
        "never computed", and without it a window whose every row was erased would
        be indistinguishable from one the writer had not reached yet.
        """
        breakdown, actor_count = await self._compute(
            session,
            tenant_id,
            metric=metric,
            window_start=window_start,
            window_end=window_end,
        )
        # Withheld at computation for two independent reasons, and the upsert can
        # still withhold a cell this side considers reportable: the floors here
        # cannot see the figure already published, and the statement can.
        suppressed = breakdown.withheld or not self._floors.admits(
            actor_count=actor_count,
            event_count=sum(cell.event_count for cell in breakdown.cells),
        )
        value = None if suppressed else json.dumps(_serialize(breakdown))
        result = await session.execute(
            text(_UPSERT_CELL_SQL),
            {
                "tenant": tenant_id,
                "cohort": COHORT_TENANT,
                "metric": metric,
                "window_start": window_start,
                "window_end": window_end,
                "actor_count": 0 if suppressed else actor_count,
                "value": value,
                "suppressed": suppressed,
                "partial": False if suppressed else breakdown.partial,
                "policy_version": policies.POLICY_VERSION,
                "now": now,
                "expires_at": _cell_expiry(metric, window_end),
            },
        )
        stored = result.scalar()
        return bool(suppressed if stored is None else stored)

    async def _compute(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        metric: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> tuple[Breakdown, int]:
        """The window's floored breakdown, and its distinct-actor count.

        The actor count comes from the statement's own total row rather than from
        summing the per-label counts, which would count one reporter once per label
        they touched and hand the floor an inflated number to clear.
        """
        params: dict[str, object] = {
            "tenant": tenant_id,
            "window_start": window_start,
            "window_end": window_end,
        }
        if metric == METRIC_FEEDBACK_RATING_MIX:
            params["diagnostic_kind"] = KIND_DIAGNOSTIC
        rows = (await session.execute(text(_MIX_SQL_FOR[metric]), params)).all()

        actor_count = 0
        cells: list[Cell] = []
        for row in rows:
            if int(row.is_total):
                actor_count = int(row.actor_count)
                continue
            cells.append(
                Cell.measured(
                    str(row.label),
                    actor_count=int(row.actor_count),
                    event_count=int(row.event_count),
                    value=int(row.event_count),
                    floors=self._floors,
                )
            )
        return (
            build_breakdown(
                metric,
                window_start=window_start,
                window_end=window_end,
                cells=cells,
                floors=self._floors,
            ),
            actor_count,
        )


def _serialize(breakdown: Breakdown) -> dict[str, object]:
    """The stored shape of a reportable cell.

    Only what survived the floors: `breakdown.cells` holds the reported figures and
    the combined remainder, never a suppressed cell's own value. `partial` rides on
    its own column rather than in here so a reader cannot serve the total without
    having read whether it is whole.
    """
    return {
        "cells": {cell.label: cell.value for cell in breakdown.cells},
        "total": breakdown.total,
    }


def metrics_for_source(record_class: str) -> Sequence[str]:
    """Which stored metrics an erasure of this class can move.

    Exposed for the same reason the metric tuple is closed: a caller reasoning
    about what an erasure invalidates should ask this module rather than keep its
    own copy of the mapping.
    """
    return tuple(metric for metric, source in _SOURCE_CLASS_FOR.items() if source == record_class)


__all__ = [
    "AGGREGATE_METRICS",
    "DEFAULT_SUSPECT_BATCH",
    "METRIC_FEEDBACK_RATING_MIX",
    "METRIC_SIGNAL_SOURCE_MIX",
    "TRAILING_WINDOWS",
    "WINDOW",
    "PrivacyAggregateReport",
    "PrivacyAggregateWriter",
    "metrics_for_source",
]
