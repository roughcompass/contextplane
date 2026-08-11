"""Learning aggregates, and the floors that make them constructible at all.

Separate from `claim_serving.py`, which serves one cited claim to a consumer that
asked for it. These answer "how is this system doing", and the difference is not
presentational: a cited claim is a thing somebody may read, while an aggregate over
people's reports is a thing that must not be constructible below a threshold. Two
modules, because a single one would eventually grow a code path that returns a
count of one.

**The floors live here and are applied at construction.** Both aggregate surfaces
import them from this module rather than each keeping a copy, because "the same
floors, enforced uniformly" is unachievable with two definitions that merely agree
today. This module sits below the feedback aggregates in the import graph, which is
what lets it be the single home; the alternative — returning raw counts and letting
each caller apply the rule — is the leak, since an unfloored aggregate that exists
at all can be logged, cached or served by the next code path somebody adds.

**Why suppression alone is not enough.** Withholding a small cell still discloses
it when a true total is published beside the survivors: a reader subtracts. So a
total is recomputed over reported cells only and labelled partial, and the true
total is never served next to a suppressed cell. That is the one rule here whose
absence would defeat every other rule in the module.

**What these aggregates deliberately cannot express.** There is no per-actor cell
and no cohort finer than the tenant. Not an omission: the only permitted use is
measuring system quality, and individual surveillance and team-performance
evaluation are both forbidden — so a per-team breakdown is not a feature this
withholds pending a floor, it is a surface that must not exist. The actor floor
then does the work it is for: a metric nobody but a handful of people contributed
to is withheld even at tenant scope.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.types import TenantContext

#: The approved minima. Code may enforce stricter values; nothing may enforce
#: looser, and `Floors` refuses rather than clamping so a deployment that asked
#: for less finds out at construction instead of in a published cell.
MIN_COHORT_ACTORS = 5
MIN_CELL_EVENTS = 5

#: The one cohort these aggregates are computed over. A literal rather than a
#: column because there is no membership model to group by, and inventing one
#: would build the team-performance surface the policy forbids.
COHORT_TENANT = "tenant"

#: The label a combined remainder carries. It is only ever emitted when the
#: remainder itself clears both floors — otherwise the whole breakdown goes.
BUCKET_OTHER = "other"


class FloorsTooLoose(ValidationError):
    """Raised when a caller asks for a floor below the approved minimum.

    A refusal rather than a silent clamp: a deployment that configured three
    actors per cohort and got five would keep believing it had configured three,
    and the next person to read that configuration would trust it.
    """


@dataclasses.dataclass(frozen=True)
class Floors:
    """The thresholds a cell must clear before it may carry a value."""

    min_actors: int = MIN_COHORT_ACTORS
    min_events: int = MIN_CELL_EVENTS

    def __post_init__(self) -> None:
        if self.min_actors < MIN_COHORT_ACTORS:
            msg = (
                f"cohort actor floor {self.min_actors} is below the approved minimum "
                f"of {MIN_COHORT_ACTORS}; stricter is permitted, looser is not"
            )
            raise FloorsTooLoose(msg)
        if self.min_events < MIN_CELL_EVENTS:
            msg = (
                f"cell event floor {self.min_events} is below the approved minimum "
                f"of {MIN_CELL_EVENTS}; stricter is permitted, looser is not"
            )
            raise FloorsTooLoose(msg)

    def admits(self, *, actor_count: int, event_count: int) -> bool:
        """Whether a cell with these counts may be reported at all."""
        return actor_count >= self.min_actors and event_count >= self.min_events


@dataclasses.dataclass(frozen=True)
class Cell:
    """One reportable figure, or the record that one was withheld.

    `value` is None exactly when `suppressed` is true — a property rather than a
    stored field, so there is no way to construct a cell whose disclosed value
    disagrees with its own suppression flag.

    `measured_value` and the counts stay on a suppressed cell on purpose. They are
    what a later recompute re-tests against the floors, and what lets a thin cell be
    folded into a remainder that clears them — a remainder built from the disclosed
    values would sum a column of nulls and report an "other" bucket of zero, which
    is how this was wrong before a test asked. None of the three is serialized.
    """

    label: str
    actor_count: int
    event_count: int
    measured_value: int
    suppressed: bool

    @property
    def value(self) -> int | None:
        """The figure, or None when a floor withheld it."""
        return None if self.suppressed else self.measured_value

    @classmethod
    def measured(cls, label: str, *, actor_count: int, event_count: int, value: int, floors: Floors) -> Cell:
        """Build a cell already tested against the floors.

        The only constructor callers use, so there is no path that produces a
        reportable cell without having asked whether it may be reported.
        """
        return cls(
            label=label,
            actor_count=actor_count,
            event_count=event_count,
            measured_value=value,
            suppressed=not floors.admits(actor_count=actor_count, event_count=event_count),
        )


@dataclasses.dataclass(frozen=True)
class Breakdown:
    """A metric's cells, its total over the reported ones, and whether it is whole.

    `withheld` is the third outcome, and it is not the same as every cell being
    suppressed: it means the breakdown was abandoned because the remainder could
    not be combined into a bucket that clears the floors, so not even the shape of
    the distribution is served.
    """

    metric: str
    cohort_key: str
    window_start: datetime.datetime
    window_end: datetime.datetime
    cells: tuple[Cell, ...]
    total: int | None
    partial: bool
    withheld: bool

    @property
    def denominator(self) -> int | None:
        """The population the total is over — the same number, named for the reader.

        Served alongside the total because a rate without its denominator is the
        figure most often misread as a count.
        """
        return self.total


def build_breakdown(
    metric: str,
    *,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    cells: Sequence[Cell],
    floors: Floors,
    cohort_key: str = COHORT_TENANT,
) -> Breakdown:
    """Apply suppression, combination and partial totalling to one metric's cells.

    The order matters and is fixed. Suppressed cells are combined into a remainder
    first, because a remainder that clears the floors is more informative than a
    row of withheld cells; if it does not clear them, the whole breakdown is
    withheld rather than served with the survivors, since survivors plus a known
    population is the subtraction attack in a different shape.
    """
    reported = [cell for cell in cells if not cell.suppressed]
    hidden = [cell for cell in cells if cell.suppressed]

    combined: list[Cell] = list(reported)
    if hidden:
        remainder = Cell.measured(
            BUCKET_OTHER,
            actor_count=sum(cell.actor_count for cell in hidden),
            event_count=sum(cell.event_count for cell in hidden),
            # The measured figures, not the disclosed ones: every hidden cell has
            # withheld its value, so summing `value` would build a remainder of zero.
            value=sum(cell.measured_value for cell in hidden),
            floors=floors,
        )
        if remainder.suppressed:
            # Nothing is served: not the survivors, not the shape. A breakdown
            # whose remainder identifies a handful of people by elimination is
            # withheld entire.
            return Breakdown(
                metric=metric,
                cohort_key=cohort_key,
                window_start=window_start,
                window_end=window_end,
                cells=(),
                total=None,
                partial=False,
                withheld=True,
            )
        combined.append(remainder)

    if not combined:
        return Breakdown(
            metric=metric,
            cohort_key=cohort_key,
            window_start=window_start,
            window_end=window_end,
            cells=(),
            total=None,
            partial=False,
            withheld=True,
        )

    # Recomputed over reported cells only. Deliberately not the true total: with a
    # cell withheld, publishing the real population is what lets a reader recover
    # the withheld figure by subtraction. Every cell in `combined` is reportable by
    # construction, so this sums exactly what the caller is shown.
    total = sum(cell.measured_value for cell in combined)
    return Breakdown(
        metric=metric,
        cohort_key=cohort_key,
        window_start=window_start,
        window_end=window_end,
        cells=tuple(combined),
        total=total,
        partial=bool(hidden),
        withheld=False,
    )


#: Age buckets, in days. Coarse on purpose: a per-day aging curve over a small
#: population is a timeline of individual activity, and the question these answer
#: is whether a backlog is growing, not when one person filed something.
_AGE_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0-6d", 0, 7),
    ("7-29d", 7, 30),
    ("30-89d", 30, 90),
    ("90d+", 90, None),
)

METRIC_CLAIM_AGING = "claim_aging"
METRIC_CONTRADICTION_BACKLOG = "contradiction_backlog"
METRIC_PROMOTION_YIELD = "promotion_yield"

#: Every metric this module serves. Closed so the router cannot advertise one
#: nothing computes, and so the conformance gate can assert the whole set.
LEARNING_METRICS: tuple[str, ...] = (
    METRIC_CLAIM_AGING,
    METRIC_CONTRADICTION_BACKLOG,
    METRIC_PROMOTION_YIELD,
)

# Aging of staged claims. Grouped by bucket, counting distinct authoring actors per
# bucket so the floor is tested against the people who contributed to the cell
# rather than against the row count, which one prolific actor can carry alone.
#
# **Scoped by `owning_tenant_id`, which is the tenant that owns the claim's
# subject — not `author_tenant_id`, the tenant that wrote it.** The two differ,
# and the difference decides whether this aggregate is a disclosure: claim reads
# are authorized on the owning tenant, so counting rows by author would total
# claims the calling tenant is not permitted to read, and report them in a form
# the floors were built to prevent. `owning_tenant_id` is nullable only for an
# unlinked claim, and this query filters `status = 'staged'`, so every row it
# touches has a resolved subject and therefore an owner — nothing is dropped by
# the stricter column.
_CLAIM_AGING_SQL = """
SELECT
    width_bucket(
        EXTRACT(EPOCH FROM (:now - created_at)) / 86400.0,
        ARRAY[7, 30, 90]::double precision[]
    ) AS bucket_index,
    count(*) AS event_count,
    count(DISTINCT author_actor_id) AS actor_count
FROM memory_claims
WHERE owning_tenant_id = :tenant
  AND status = 'staged'
  AND created_at >= :window_start
  AND created_at < :window_end
GROUP BY bucket_index
"""

# Open and routed contradiction cases, aged the same way. A resolved case is not
# backlog, and counting it would make the backlog look like throughput.
#
# Aged from `created_at`, which is when the case came into existence. The table
# records `routed_at` and `resolved_at` besides, and neither can age a backlog:
# `routed_at` is null until somebody routes it, so ageing by it would hide the
# unrouted cases — the ones that have waited longest and matter most — and
# `resolved_at` is null for every row this query selects.
_CONTRADICTION_BACKLOG_SQL = """
SELECT
    width_bucket(
        EXTRACT(EPOCH FROM (:now - created_at)) / 86400.0,
        ARRAY[7, 30, 90]::double precision[]
    ) AS bucket_index,
    count(*) AS event_count,
    count(DISTINCT coalesce(owner_id, '')) AS actor_count
FROM curation_cases
WHERE tenant_id = :tenant
  AND status IN ('open', 'routed')
  AND created_at >= :window_start
  AND created_at < :window_end
GROUP BY bucket_index
"""

# Promotion yield: what became of the derivation attempts in the window. The cell
# is per outcome, and the actor count is the distinct authorities that produced
# them, because an outcome carried by one source is one source's opinion.
_PROMOTION_YIELD_SQL = """
SELECT
    status AS label,
    count(*) AS event_count,
    count(DISTINCT source_authority) AS actor_count
FROM claim_derivations
WHERE tenant_id = :tenant
  AND created_at >= :window_start
  AND created_at < :window_end
GROUP BY status
"""


def _bucket_label(bucket_index: int | None) -> str:
    """Map Postgres' `width_bucket` result onto the declared bucket labels.

    `width_bucket` returns 0 for a value below the first bound, which here means
    an age under seven days, so the index is the position in the declared tuple.
    """
    index = 0 if bucket_index is None else int(bucket_index)
    index = max(0, min(index, len(_AGE_BUCKETS) - 1))
    return _AGE_BUCKETS[index][0]


class LearningReadService:
    """Aggregate learning reads for one tenant, floored at construction.

    Every method returns `Breakdown` objects that have already been through the
    floors. There is deliberately no method that returns raw counts: the moment one
    exists, the enforcement is a convention about which method to call.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        floors: Floors | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._floors = floors or Floors()

    @property
    def floors(self) -> Floors:
        """The floors in force, so a caller can serve them beside the figures."""
        return self._floors

    async def claim_aging(
        self,
        ctx: TenantContext,
        *,
        now: datetime.datetime,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Breakdown:
        """How long staged claims have been waiting, in coarse buckets."""
        return await self._aged(
            METRIC_CLAIM_AGING,
            _CLAIM_AGING_SQL,
            ctx.tenant_id,
            now=now,
            window_start=window_start,
            window_end=window_end,
        )

    async def contradiction_backlog(
        self,
        ctx: TenantContext,
        *,
        now: datetime.datetime,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Breakdown:
        """Unresolved contradiction cases, aged. Resolved cases are not backlog."""
        return await self._aged(
            METRIC_CONTRADICTION_BACKLOG,
            _CONTRADICTION_BACKLOG_SQL,
            ctx.tenant_id,
            now=now,
            window_start=window_start,
            window_end=window_end,
        )

    async def promotion_yield(
        self,
        ctx: TenantContext,
        *,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Breakdown:
        """What became of the derivation attempts made in the window."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(_PROMOTION_YIELD_SQL),
                    {"tenant": ctx.tenant_id, "window_start": window_start, "window_end": window_end},
                )
            ).all()

        cells = [
            Cell.measured(
                str(row.label),
                actor_count=int(row.actor_count),
                event_count=int(row.event_count),
                value=int(row.event_count),
                floors=self._floors,
            )
            for row in rows
        ]
        return build_breakdown(
            METRIC_PROMOTION_YIELD,
            window_start=window_start,
            window_end=window_end,
            cells=cells,
            floors=self._floors,
        )

    async def _aged(
        self,
        metric: str,
        statement: str,
        tenant_id: uuid.UUID,
        *,
        now: datetime.datetime,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Breakdown:
        """Shared shape for the two age-bucketed metrics.

        One implementation because the two differ only in which table and predicate
        define a waiting item; duplicating the bucket mapping is how the two would
        drift into using different bucket boundaries while reporting the same labels.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(statement),
                    {
                        "tenant": tenant_id,
                        "now": now,
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).all()

        cells = [
            Cell.measured(
                _bucket_label(row.bucket_index),
                actor_count=int(row.actor_count),
                event_count=int(row.event_count),
                value=int(row.event_count),
                floors=self._floors,
            )
            for row in rows
        ]
        return build_breakdown(
            metric,
            window_start=window_start,
            window_end=window_end,
            cells=cells,
            floors=self._floors,
        )


__all__ = [
    "BUCKET_OTHER",
    "COHORT_TENANT",
    "LEARNING_METRICS",
    "METRIC_CLAIM_AGING",
    "METRIC_CONTRADICTION_BACKLOG",
    "METRIC_PROMOTION_YIELD",
    "MIN_CELL_EVENTS",
    "MIN_COHORT_ACTORS",
    "Breakdown",
    "Cell",
    "Floors",
    "FloorsTooLoose",
    "LearningReadService",
    "build_breakdown",
]
