"""Tenant-scope learning aggregates.

Separate from `claim_serving.py`, which serves one cited claim to a consumer that
asked for it. These answer "how is this system doing" over a tenant's own
records: how staged claims are ageing, how large the contradiction backlog is,
what fraction of proposals are promoted.

**There is no suppression here.** Every cell carries its measured value, and a
breakdown is either built or the query returned no rows -- there is no third
"withheld" outcome and no partial total.

That is a deliberate reversal, recorded in
`.develop/adr/0017-per-actor-aggregates-are-no-longer-floored.md`. This module
used to enforce `MIN_COHORT_ACTORS`/`MIN_CELL_EVENTS` at construction and refuse
per-actor cells outright, on the reasoning that an aggregate over people's
reports must not be constructible below a threshold. E20 needs exactly the
surface that forbade -- a per-author accuracy read -- and the floor was removed
uniformly, for every actor kind, rather than carved out for agents.

**Read the ADR's dissent before adding a reader.** The protection this module
gave was structural: an aggregate that cannot be constructed cannot be leaked by
a misconfigured role, a log line, a cached response, or the next endpoint
somebody adds. What replaces it is authorization on the read -- which answers a
different question, and which nothing in this module enforces. If a caller needs
a per-human breakdown, the access decision belongs at that caller, following
E11-T3's precedent of authorization plus a recorded justification.

**One suppression that is not this module's and must not be removed with it.**
`signals/aggregates.py` withholds a `privacy_aggregates` cell whose recomputation
after an erasure would disclose a subject by subtraction. That is a differencing
defence over stored series, orthogonal to actor cardinality, and it survives
untouched -- see the differencing decision record.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.types import TenantContext

#: The one cohort these aggregates are computed over.
#:
#: Still a literal, and still the tenant -- That decision removed the *floor*, not the
#: grouping. Nothing here has a membership model to break down by, so a finer
#: cohort would have to be invented rather than read, and inventing one is a
#: separate decision from the one that ADR took.
COHORT_TENANT = "tenant"


@dataclasses.dataclass(frozen=True)
class Cell:
    """One reported figure.

    `value` is a plain field now. It used to be a property returning `None` when
    a floor withheld the figure, paired with a stored `measured_value` so a
    recompute could re-test the withheld number -- machinery that only existed to
    keep a suppressed cell honest about what it was hiding. With no suppression
    there is nothing to hide and nothing to re-test, so the two collapse into
    one.

    The counts stay. They are not floor inputs any more; they are what a reader
    needs to know how much a figure rests on, which is a legitimate thing to
    serve and was always serialized for the reported cells.
    """

    label: str
    actor_count: int
    event_count: int
    value: int

    @classmethod
    def measured(cls, label: str, *, actor_count: int, event_count: int, value: int) -> Cell:
        """Build a cell.

        Kept as a named constructor rather than dropped for the plain one, so
        every call site reads the same as it did and the diff that removed the
        floors is not also a diff that renamed everything.
        """
        return cls(label=label, actor_count=actor_count, event_count=event_count, value=value)


@dataclasses.dataclass(frozen=True)
class Breakdown:
    """A metric's cells and the total over them.

    Two outcomes, not three: a breakdown is built, or the query returned no rows
    and there are no cells. `partial` and `withheld` are gone with the floors
    that produced them -- `partial` meant "a cell was suppressed, so this total
    is over the survivors" and `withheld` meant "even the shape would identify
    somebody", and neither state is reachable now.

    `total` is therefore the true total, not a recomputation over survivors. The
    old one was deliberately *not* the true total, because publishing the real
    population beside a suppressed cell is the subtraction attack. With nothing
    suppressed there is nothing to subtract toward.
    """

    metric: str
    cohort_key: str
    window_start: datetime.datetime
    window_end: datetime.datetime
    cells: tuple[Cell, ...]
    total: int

    @property
    def denominator(self) -> int:
        """The population the total is over -- the same number, named for the reader.

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
    cohort_key: str = COHORT_TENANT,
) -> Breakdown:
    """Assemble one metric's cells and total them.

    What this used to do, and no longer does: test each cell against the floors,
    fold the withheld ones into an "other" remainder, abandon the whole breakdown
    if that remainder was itself too thin, and total over the survivors only. All
    four steps existed to stop a reader recovering a suppressed figure by
    subtraction, and the decision record for it removed the suppression they protected.

    What is left is a sum, which is why this stays a function rather than
    becoming a constructor call: the cells still arrive from three different
    queries, and one place that turns cells into a breakdown is still worth
    having when a fourth is added.
    """
    ordered = tuple(cells)
    return Breakdown(
        metric=metric,
        cohort_key=cohort_key,
        window_start=window_start,
        window_end=window_end,
        cells=ordered,
        total=sum(cell.value for cell in ordered),
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
    """Aggregate learning reads for one tenant.

    Every method returns a `Breakdown` built directly from its query's rows.
    There used to be no method returning raw counts, on the reasoning that the
    moment one existed the floor enforcement became a convention about which
    method to call. That reasoning went with the floors (recorded as an architecture decision); what is left
    is three named metrics rather than a general counting surface, which is a
    smaller claim and still worth keeping.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
            )
            for row in rows
        ]
        return build_breakdown(
            METRIC_PROMOTION_YIELD,
            window_start=window_start,
            window_end=window_end,
            cells=cells,
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
            )
            for row in rows
        ]
        return build_breakdown(
            metric,
            window_start=window_start,
            window_end=window_end,
            cells=cells,
        )


__all__ = [
    "COHORT_TENANT",
    "LEARNING_METRICS",
    "METRIC_CLAIM_AGING",
    "METRIC_CONTRADICTION_BACKLOG",
    "METRIC_PROMOTION_YIELD",
    "Breakdown",
    "Cell",
    "LearningReadService",
    "build_breakdown",
]
