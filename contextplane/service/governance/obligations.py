"""Reporting obligations: nominating one, classifying it, and the delay that is a gauge.

The decision that this object exists was recorded before anything implemented
it. This is the record and the two operations on it, and deliberately nothing
else.

## What is not here, and will not be until somebody else decides

**Automatic classification.** Deciding that an obligation is material needs a
ratified threshold set, which is external and is not this team's to write.
So the only path out of `unclassified` is an explicit human decision that
records who made it and why. A placeholder threshold presented as a compliance
feature is worse than an absent one, because the absent one is visible.

**Deadlines.** Three deadlines stamped at classification time need a
classification that can be made. Building the clock against a classification
nobody can reach produces machinery that never fires.

## Why the backlog is a gauge and not a log line

`unclassified` is the state most obligations are in most of the time, so its
count is not an error condition and its healthy value is not zero. A log line
fires once and is read never; a gauge is a number somebody's dashboard already
shows. The one that matters is not the count but the *age* -- a backlog of five
nominated this morning and a backlog of five nominated in March are the same
count and completely different situations, which is why
`OLDEST_UNCLASSIFIED_AGE_SECONDS` exists beside the count rather than instead
of it.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Final

from prometheus_client import Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.types import Clock, TenantContext

#: The state a nomination arrives in, named rather than modelled as NULL: "nobody
#: has decided" and "the column was added later" must not be the same value.
MATERIALITY_UNCLASSIFIED: Final[str] = "unclassified"
MATERIALITY_NOT_MATERIAL: Final[str] = "not_material"
MATERIALITY_MATERIAL: Final[str] = "material"

#: The closed set, in the order a reader would put them. Mirrors the CHECK in
#: migration 0076; the service refuses an unknown value before the database has
#: to, so the caller gets a sentence rather than a constraint violation.
MATERIALITY_VALUES: Final[tuple[str, ...]] = (
    MATERIALITY_UNCLASSIFIED,
    MATERIALITY_NOT_MATERIAL,
    MATERIALITY_MATERIAL,
)

#: The two a classification decision may set. `unclassified` is where a row
#: starts and is not something a decision can conclude -- "I have decided it is
#: undecided" is not a decision, and allowing it would let an actor clear a
#: classification while leaving their name on it.
CLASSIFIABLE: Final[frozenset[str]] = frozenset({MATERIALITY_NOT_MATERIAL, MATERIALITY_MATERIAL})

_MIN_SUMMARY = 10
_MAX_SUMMARY = 4000
_MIN_NOTE = 20
_MAX_NOTE = 2000

#: Deployment-wide, and **unlabelled on purpose**. A `tenant_id` label would turn
#: one metric into one time series per tenant, which is the cardinality failure
#: `tests/conformance/test_metric_surface.py` exists to catch: the Prometheus that
#: dies from it dies months later, in production, under load. Per-tenant numbers
#: are a read (`unclassified_backlog`), not a metric.
UNCLASSIFIED_BACKLOG = Gauge(
    "contextplane_reporting_obligations_unclassified",
    "Reporting obligations nobody has classified yet, across the deployment. " "A healthy value is not zero.",
)

OLDEST_UNCLASSIFIED_AGE_SECONDS = Gauge(
    "contextplane_reporting_obligations_oldest_unclassified_age_seconds",
    "Age of the longest-waiting unclassified obligation anywhere in the deployment. " "Zero when the backlog is empty.",
)


@dataclasses.dataclass(frozen=True)
class ReportingObligation:
    """One tracked obligation, classified or not."""

    obligation_id: uuid.UUID
    summary: str
    materiality: str
    nominated_at: datetime.datetime
    nominated_by: uuid.UUID
    classified_at: datetime.datetime | None
    classified_by: uuid.UUID | None
    classification_note: str | None

    #: Stamped by `ReportingDeadlineService` when a classification as material
    #: starts the clock (E4-T6). All three or none, held by a CHECK. Carried on
    #: the read because E4-T6's goal is that approach and breach are visible
    #: *without anybody asking*, and a detail read that omitted them would make
    #: the operator ask.
    initial_report_due_at: datetime.datetime | None = None
    intermediate_report_due_at: datetime.datetime | None = None
    final_report_due_at: datetime.datetime | None = None
    #: `default` or `tenant_policy`: which durations produced the three above.
    deadline_basis: str | None = None


@dataclasses.dataclass(frozen=True)
class UnclassifiedBacklog:
    """What the gauge reports, and what a reader needs to act on it.

    The count alone is not actionable: five nominated this morning and five
    nominated in March are the same number and completely different situations.
    """

    count: int
    oldest_age_seconds: float


_COLUMNS = (
    "obligation_id, summary, materiality, nominated_at, nominated_by, "
    "classified_at, classified_by, classification_note, "
    "initial_report_due_at, intermediate_report_due_at, final_report_due_at, deadline_basis"
)


# ---------------------------------------------------------------------------
# What the obligation is about
# ---------------------------------------------------------------------------

#: This obligation, as a citing subject. Named here rather than in the reference
#: module for the same reason `SUBJECT_RECEIPT` and `SUBJECT_TASK_CHECKPOINT`
#: live with the things they name: the subject type belongs to whatever owns the
#: subject, and a single module holding all of them would be a place every new
#: subject has to be remembered in.
SUBJECT_REPORTING_OBLIGATION: Final = "reporting_obligation"

#: The only reference kind an obligation may cite, and the refusal is the point.
#: The decision that named this governed object names the relationship exactly
#: too -- an obligation references an
#: *incident* in the sense the tree already uses, the external record -- and
#: binding a `deployment` or a `build` here would quietly make the relationship
#: mean something else while every read still called it the incident.
_INCIDENT_KIND: Final = "incident"

_BIND = """
INSERT INTO context_reference_bindings (binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at)
VALUES (:bid, :tid, :rid, :subject_type, :oid, :now)
ON CONFLICT DO NOTHING
RETURNING binding_id
"""

_CITED = """
SELECT r.reference_id, r.source_system, r.source_namespace, r.external_id, r.kind,
       r.authorized_uri, r.observed_at, b.bound_at
  FROM context_reference_bindings b
  JOIN context_external_references r ON r.reference_id = b.reference_id
 WHERE b.tenant_id = :tid AND b.subject_type = :subject_type AND b.subject_id = :oid
 ORDER BY b.bound_at, r.reference_id
"""

_KIND_OF = """
SELECT kind FROM context_external_references
 WHERE reference_id = :rid AND tenant_id = :tid
"""


def _age_seconds(oldest: datetime.datetime | None, now: datetime.datetime) -> float:
    """Seconds since the longest wait began, or zero when nothing is waiting."""
    return 0.0 if oldest is None else max(0.0, (now - oldest).total_seconds())


def _to_obligation(row: object) -> ReportingObligation:
    return ReportingObligation(
        obligation_id=row.obligation_id,  # type: ignore[attr-defined]
        summary=row.summary,  # type: ignore[attr-defined]
        materiality=row.materiality,  # type: ignore[attr-defined]
        nominated_at=row.nominated_at,  # type: ignore[attr-defined]
        nominated_by=row.nominated_by,  # type: ignore[attr-defined]
        classified_at=row.classified_at,  # type: ignore[attr-defined]
        classified_by=row.classified_by,  # type: ignore[attr-defined]
        classification_note=row.classification_note,  # type: ignore[attr-defined]
        initial_report_due_at=row.initial_report_due_at,  # type: ignore[attr-defined]
        intermediate_report_due_at=row.intermediate_report_due_at,  # type: ignore[attr-defined]
        final_report_due_at=row.final_report_due_at,  # type: ignore[attr-defined]
        deadline_basis=row.deadline_basis,  # type: ignore[attr-defined]
    )


class ReportingObligationService:
    """Nominate, classify and count. The only writer of `reporting_obligations`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def nominate(
        self,
        ctx: TenantContext,
        *,
        summary: str,
    ) -> ReportingObligation:
        """Record that something may need reporting, without deciding whether it does.

        Nomination is deliberately cheap and deliberately separate from
        classification. A surface that required a materiality up front would get
        a guess, and a guessed classification is worse than an honest
        `unclassified` because it stops anybody looking again.
        """
        cleaned = summary.strip()
        if not _MIN_SUMMARY <= len(cleaned) <= _MAX_SUMMARY:
            msg = (
                f"summary must be {_MIN_SUMMARY}-{_MAX_SUMMARY} characters; "
                "an obligation nobody can identify later is not a record"
            )
            raise ValidationError(msg)

        obligation_id = uuid.uuid4()
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO reporting_obligations "
                    "  (obligation_id, tenant_id, summary, materiality, nominated_at, nominated_by) "
                    "VALUES (:oid, :tid, :summary, :materiality, :now, :actor)"
                ),
                {
                    "oid": obligation_id,
                    "tid": ctx.tenant_id,
                    "summary": cleaned,
                    "materiality": MATERIALITY_UNCLASSIFIED,
                    "now": now,
                    "actor": ctx.actor_id,
                },
            )

        return ReportingObligation(
            obligation_id=obligation_id,
            summary=cleaned,
            materiality=MATERIALITY_UNCLASSIFIED,
            nominated_at=now,
            nominated_by=ctx.actor_id,
            classified_at=None,
            classified_by=None,
            classification_note=None,
        )

    async def classify(
        self,
        ctx: TenantContext,
        *,
        obligation_id: uuid.UUID,
        materiality: str,
        note: str,
    ) -> ReportingObligation:
        """Record a human decision about one obligation, with who and why.

        Refuses a second classification rather than overwriting. A materiality
        that changed with no record of the first answer would make the audit
        trail describe only the most recent opinion, and the first answer is the
        one somebody acted on.
        """
        if materiality not in CLASSIFIABLE:
            msg = (
                f"materiality must be one of {sorted(CLASSIFIABLE)}; "
                f"got {materiality!r}. `{MATERIALITY_UNCLASSIFIED}` is where a row starts, "
                "not a conclusion a decision can reach"
            )
            raise ValidationError(msg)
        cleaned = note.strip()
        if not _MIN_NOTE <= len(cleaned) <= _MAX_NOTE:
            msg = (
                f"a classification note must be {_MIN_NOTE}-{_MAX_NOTE} characters; "
                "a one-word rationale is the same as none"
            )
            raise ValidationError(msg)

        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            # Locked, because two classifications racing would both read
            # `unclassified` and the second would overwrite the first's actor.
            current = (
                await session.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM reporting_obligations "  # noqa: S608 - _COLUMNS is a fixed module-level column list, not caller input
                        "WHERE obligation_id = :oid AND tenant_id = :tid FOR UPDATE"
                    ),
                    {"oid": obligation_id, "tid": ctx.tenant_id},
                )
            ).one_or_none()
            if current is None:
                msg = "no such reporting obligation"
                raise NotFoundError(msg)
            if current.materiality != MATERIALITY_UNCLASSIFIED:
                msg = (
                    f"obligation {obligation_id} is already classified as "
                    f"{current.materiality!r}; a reclassification is a new decision "
                    "and needs its own record, not an overwrite of this one"
                )
                raise ConflictError(msg)

            await session.execute(
                text(
                    "UPDATE reporting_obligations SET "
                    "  materiality = :materiality, classified_at = :now, "
                    "  classified_by = :actor, classification_note = :note "
                    "WHERE obligation_id = :oid AND tenant_id = :tid"
                ),
                {
                    "materiality": materiality,
                    "now": now,
                    "actor": ctx.actor_id,
                    "note": cleaned,
                    "oid": obligation_id,
                    "tid": ctx.tenant_id,
                },
            )

        return ReportingObligation(
            obligation_id=obligation_id,
            summary=current.summary,
            materiality=materiality,
            nominated_at=current.nominated_at,
            nominated_by=current.nominated_by,
            classified_at=now,
            classified_by=ctx.actor_id,
            classification_note=cleaned,
        )

    async def get(self, ctx: TenantContext, *, obligation_id: uuid.UUID) -> ReportingObligation:
        """One obligation, scoped to the caller's tenant."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM reporting_obligations "  # noqa: S608 - _COLUMNS is a fixed module-level column list, not caller input
                        "WHERE obligation_id = :oid AND tenant_id = :tid"
                    ),
                    {"oid": obligation_id, "tid": ctx.tenant_id},
                )
            ).one_or_none()
        if row is None:
            msg = "no such reporting obligation"
            raise NotFoundError(msg)
        return _to_obligation(row)

    async def cite_incident(
        self,
        ctx: TenantContext,
        *,
        obligation_id: uuid.UUID,
        reference_id: uuid.UUID,
    ) -> bool:
        """Record that this obligation is about that incident. Returns whether it was new.

        **This is the relationship the decision promised and nothing implemented**,
        and it needed no new table: `context_external_references` already models
        the external record and `context_reference_bindings` already binds one to
        a subject. The whole of the gap was that `reporting_obligation` was not a
        legal `subject_type`.

        Refuses a reference that is not an incident. That decision says an
        obligation references an *incident*; admitting a `build` or a
        `deployment` would leave every read still calling it the incident while
        it had become something else.

        Idempotent per pair. Citing the same incident twice is one relationship
        stated twice, and a citation list that grew on re-statement would read as
        two independent records of the same event.

        The obligation is read first, so citing one that does not exist -- or
        belongs to another tenant -- is a `NotFoundError` rather than a binding
        pointing at nothing.
        """
        await self.get(ctx, obligation_id=obligation_id)

        async with self._session_factory() as session, session.begin():
            kind = (
                await session.execute(text(_KIND_OF), {"rid": reference_id, "tid": ctx.tenant_id})
            ).scalar_one_or_none()
            if kind is None:
                msg = "no such external reference in this tenant"
                raise NotFoundError(msg)
            if kind != _INCIDENT_KIND:
                msg = (
                    f"a reporting obligation cites an {_INCIDENT_KIND!r} reference and this one is "
                    f"{kind!r}; the relationship is about the incident that created the obligation"
                )
                raise ValidationError(msg)
            # `RETURNING` rather than `rowcount`: the driver's row count is
            # typed as unavailable on an async result, and asking the statement
            # what it wrote is a better answer than asking the cursor anyway.
            written = (
                await session.execute(
                    text(_BIND),
                    {
                        "bid": uuid.uuid4(),
                        "tid": ctx.tenant_id,
                        "rid": reference_id,
                        "subject_type": SUBJECT_REPORTING_OBLIGATION,
                        "oid": obligation_id,
                        "now": self._clock.now(),
                    },
                )
            ).scalar_one_or_none()
        return written is not None

    async def incidents_for(
        self,
        ctx: TenantContext,
        *,
        obligation_id: uuid.UUID,
    ) -> tuple[dict[str, object], ...]:
        """Every incident this obligation cites, oldest citation first.

        Returns an empty tuple for an obligation nobody has matched to a record
        yet, which is the state most of them start in -- 0076 made `summary`
        free text precisely so a nomination need not wait for the link. An empty
        result here is a nomination in progress, not a missing one.
        """
        await self.get(ctx, obligation_id=obligation_id)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(_CITED),
                        {
                            "tid": ctx.tenant_id,
                            "subject_type": SUBJECT_REPORTING_OBLIGATION,
                            "oid": obligation_id,
                        },
                    )
                )
                .mappings()
                .all()
            )
        return tuple(dict(row) for row in rows)

    async def unclassified_backlog(self, ctx: TenantContext) -> UnclassifiedBacklog:
        """How many are waiting for this tenant, and how long the longest has waited.

        Sets no gauge. A per-tenant read that published a deployment-wide gauge
        would make the series report whichever tenant asked most recently, which
        is worse than not publishing it: the number would look live and be
        wrong. `observe_backlog` is the one that publishes, and it counts
        everybody.
        """
        now = self._clock.now()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS waiting, MIN(nominated_at) AS oldest "
                        "FROM reporting_obligations "
                        "WHERE tenant_id = :tid AND materiality = :unclassified"
                    ),
                    {"tid": ctx.tenant_id, "unclassified": MATERIALITY_UNCLASSIFIED},
                )
            ).one()

        return UnclassifiedBacklog(
            count=int(row.waiting),
            oldest_age_seconds=_age_seconds(row.oldest, now),
        )

    async def observe_backlog(self) -> UnclassifiedBacklog:
        """Publish the deployment-wide backlog to the two gauges.

        Across every tenant, because the gauges carry no tenant label and a
        partial answer on an unlabelled series is a wrong answer rather than an
        incomplete one.

        Scheduled rather than computed at scrape time, and the risk that creates
        is worth naming: a job that silently stops turns a gauge into "whenever
        somebody last looked", and the value would sit at its last reading
        looking healthy. `contextplane_reporting_obligations_oldest_unclassified_age_seconds`
        is the defence -- a stalled observer freezes the age, and an age that
        stops advancing while the count is non-zero is itself the signal.
        """
        now = self._clock.now()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS waiting, MIN(nominated_at) AS oldest "
                        "FROM reporting_obligations WHERE materiality = :unclassified"
                    ),
                    {"unclassified": MATERIALITY_UNCLASSIFIED},
                )
            ).one()

        count = int(row.waiting)
        age = _age_seconds(row.oldest, now)
        # Set to zero rather than left absent when the backlog is empty. A
        # missing series is indistinguishable from a scrape that failed, and
        # this is the pair somebody alerts on.
        UNCLASSIFIED_BACKLOG.set(count)
        OLDEST_UNCLASSIFIED_AGE_SECONDS.set(age)
        return UnclassifiedBacklog(count=count, oldest_age_seconds=age)
