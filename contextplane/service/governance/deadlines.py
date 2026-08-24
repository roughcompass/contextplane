"""The notification clock: what a classification-as-material makes due, and when.

E4-T6, and it was blocked on a premise that stopped being true.

## Why this was held, and why it is not held any more

The clock was deliberately not built, on a recorded argument that it would be
machinery around **a classification that could not be made**: without materiality
thresholds nothing could be classified, so no deadline would ever be stamped and
the whole mechanism would sit inert.

That was correct when written. `classify()` then shipped -- a person
records a materiality with their reasoning, refused if already classified, with
`classified_at` and `classified_by` stored. So a classification-as-material *can*
be made, by the only route `0076` ever intended: somebody deciding and being
recorded as having decided.

The clock has a trigger. It is not machinery around something that cannot happen.

## What is still external, and what is not

**The thresholds stay external and nothing here touches them.** Whether a given
incident is major is a judgement `0076` rightly refused to make, and the only
route out of `unclassified` is still a person deciding.

**The durations default to the regulation's own and are overridable.** These are
a different kind of fact: how long after classification each report is due is
published text, not a judgement. A default sourced to it is not this service
deciding anything -- it is the service not making every deployment retype a
number the regulation already fixed. A tenant under a different regime, or an RTS
revision, overrides it without a release.

**Which numbers were used is recorded per obligation.** `deadline_basis` says
`default` or `tenant_policy`, because a default that changes in a later release
must not leave an auditor unable to say where a given deadline came from -- and
because a deployment silently running on defaults for a compliance clock is
something an operator should be able to see. `observe()` gauges it.

## Three instants, stamped, never computed

Stored at classification time rather than derived on read. A computed deadline
moves when the classification timestamp is corrected, and *"when was this due"*
is precisely the question an audit asks. The stored instant is what somebody was
working to, which is a different fact from what the current inputs would derive.

All three or none, held by a CHECK: a partially stamped obligation would let a
reader believe the missing ones were not due rather than not recorded.
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
from contextplane.service.governance.obligations import MATERIALITY_MATERIAL
from contextplane.types import Clock, TenantContext

#: A source note shorter than this is the same as none, matching the floor
#: `classify()` puts on a classification note and the CHECK in `0089`.
_MIN_SOURCE_NOTE: Final[int] = 20
_MAX_SOURCE_NOTE: Final[int] = 2000

#: Stamped from the built-in default, or from this tenant's own row.
BASIS_DEFAULT: Final[str] = "default"
BASIS_TENANT: Final[str] = "tenant_policy"

#: The durations DORA's reporting timeline fixes, as the default.
#:
#: **These are published durations, not a judgement this service made**, which is
#: what separates them from the materiality thresholds `0076` refused to invent.
#: A deployment whose regulator, regime or RTS version differs overrides them per
#: tenant, and every stamped obligation records which basis it used -- so a later
#: change to this constant cannot silently rewrite what somebody was working to.
#:
#: Whoever revisits these: they are a default, not a compliance opinion. The
#: obligation to confirm them against the deployment's own regulator is the
#: deployment's, and `source_note` on an override is where that gets written
#: down.
DEFAULT_POLICY_SOURCE: Final[str] = (
    "Built-in default following DORA's major-incident reporting timeline. "
    "Confirm against the regulation and RTS version this deployment is subject "
    "to, and override per tenant where it differs."
)

#: How close counts as approaching. A deployment-wide constant rather than
#: configuration, because it describes *when somebody wants to know*, not what
#: the regulation requires -- and a per-tenant value would make one unlabelled
#: gauge mean different things for different rows.
APPROACH_WINDOW: Final[datetime.timedelta] = datetime.timedelta(hours=24)

BREACHED_DEADLINES = Gauge(
    "contextplane_reporting_deadlines_breached",
    "Report deadlines that have passed with no report recorded, across the deployment. "
    "A healthy value is zero, and this is the one to page on.",
)

APPROACHING_DEADLINES = Gauge(
    "contextplane_reporting_deadlines_approaching",
    "Report deadlines falling due within the approach window, across the deployment.",
)

ON_DEFAULT_DEADLINES = Gauge(
    "contextplane_reporting_deadlines_on_default_policy",
    "Stamped obligations whose deadlines came from the built-in default rather "
    "than a tenant's own recorded policy. Not an error -- the default is the "
    "regulation's -- but a deployment that has confirmed nothing against its own "
    "regulator should be able to see that it has not.",
)

OLDEST_BREACH_AGE_SECONDS = Gauge(
    "contextplane_reporting_deadlines_oldest_breach_age_seconds",
    "Age of the longest-standing breached deadline anywhere in the deployment. "
    "Zero when nothing is breached. A value that stops advancing while the count "
    "is non-zero means the observer stopped, not that the breach did.",
)


@dataclasses.dataclass(frozen=True)
class DeadlinePolicy:
    """The three durations a tenant works to, and where they came from."""

    initial: datetime.timedelta
    intermediate: datetime.timedelta
    final: datetime.timedelta
    source_note: str

    def due_from(
        self, classified_at: datetime.datetime
    ) -> tuple[datetime.datetime, datetime.datetime, datetime.datetime]:
        """The three instants these durations make due from one classification."""
        return (
            classified_at + self.initial,
            classified_at + self.intermediate,
            classified_at + self.final,
        )


@dataclasses.dataclass(frozen=True)
class DeadlineState:
    """What the gauges publish, and what a reader needs to act on it.

    `breached` alone is not actionable for the same reason a bare backlog count
    is not: one that passed an hour ago and one that passed in March are the same
    number and completely different situations.
    """

    breached: int
    approaching: int
    on_default_policy: int
    oldest_breach_age_seconds: float


@dataclasses.dataclass(frozen=True)
class StampedDeadlines:
    """The three instants, and which durations produced them."""

    initial: datetime.datetime
    intermediate: datetime.datetime
    final: datetime.datetime
    #: `default` or `tenant_policy`. Carried back so a caller can tell an
    #: operator which numbers were used without a second read.
    basis: str


class ReportingDeadlineService:
    """Set a tenant's durations, stamp what a classification makes due, and gauge it."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        default_policy: DeadlinePolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._default = default_policy or _BUILT_IN_DEFAULT

    def default_policy(self) -> DeadlinePolicy:
        """The durations used where a tenant has recorded none.

        Injectable, so a deployment under another regime sets its own floor
        without every tenant having to override -- and so a test can state the
        numbers it is asserting about rather than importing them.
        """
        return self._default

    async def set_policy(
        self,
        ctx: TenantContext,
        *,
        initial: datetime.timedelta,
        intermediate: datetime.timedelta,
        final: datetime.timedelta,
        source_note: str,
    ) -> DeadlinePolicy:
        """Record the durations this tenant's regulator requires.

        Ordered and positive, refused here as well as by the CHECK: an
        intermediate report due before the initial one is not a configuration
        somebody meant, and a service that accepted it would produce a 500 from
        the constraint rather than an answer naming the field.

        Replacing an existing policy is permitted -- a regulation changes -- and
        **already-stamped deadlines do not move.** They are what somebody was
        working to, and rewriting them would rewrite the audit's answer to "when
        was this due".
        """
        if not (datetime.timedelta(0) < initial < intermediate < final):
            msg = (
                "deadlines must be positive and ordered initial < intermediate < final; "
                f"got {initial}, {intermediate}, {final}"
            )
            raise ValidationError(msg)
        cleaned = source_note.strip()
        if not _MIN_SOURCE_NOTE <= len(cleaned) <= _MAX_SOURCE_NOTE:
            msg = (
                f"a deadline source note must be {_MIN_SOURCE_NOTE}-{_MAX_SOURCE_NOTE} "
                "characters; three durations with no stated source are three numbers "
                "nobody can audit"
            )
            raise ValidationError(msg)
        if ctx.actor_id is None:
            msg = "recording a deadline policy requires an authenticated actor"
            raise ValidationError(msg)

        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO reporting_deadline_policies "
                    "  (tenant_id, initial_seconds, intermediate_seconds, final_seconds,"
                    "   source_note, recorded_at, recorded_by) "
                    "VALUES (:tid, :i, :m, :f, :note, :now, :actor) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET "
                    "  initial_seconds = EXCLUDED.initial_seconds, "
                    "  intermediate_seconds = EXCLUDED.intermediate_seconds, "
                    "  final_seconds = EXCLUDED.final_seconds, "
                    "  source_note = EXCLUDED.source_note, "
                    "  recorded_at = EXCLUDED.recorded_at, "
                    "  recorded_by = EXCLUDED.recorded_by"
                ),
                {
                    "actor": ctx.actor_id,
                    "f": int(final.total_seconds()),
                    "i": int(initial.total_seconds()),
                    "m": int(intermediate.total_seconds()),
                    "note": cleaned,
                    "now": self._clock.now(),
                    "tid": ctx.tenant_id,
                },
            )
        return DeadlinePolicy(final=final, initial=initial, intermediate=intermediate, source_note=cleaned)

    async def policy_for(self, ctx: TenantContext) -> DeadlinePolicy | None:
        """This tenant's durations, or `None`.

        `None` is a real answer and callers must not default it. A fallback here
        would be this module deciding what a regulator requires, which is the one
        thing it must never do.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT initial_seconds, intermediate_seconds, final_seconds, source_note "
                        "FROM reporting_deadline_policies WHERE tenant_id = :tid"
                    ),
                    {"tid": ctx.tenant_id},
                )
            ).one_or_none()
        if row is None:
            return None
        return DeadlinePolicy(
            final=datetime.timedelta(seconds=row.final_seconds),
            initial=datetime.timedelta(seconds=row.initial_seconds),
            intermediate=datetime.timedelta(seconds=row.intermediate_seconds),
            source_note=row.source_note,
        )

    async def stamp(self, ctx: TenantContext, obligation_id: uuid.UUID) -> StampedDeadlines:
        """Stamp the three deadlines a material classification made due.

        Always stamps: a tenant with no recorded policy gets the built-in
        default, and the row records which basis it used.

        Refuses an obligation that is not classified material, and one already
        stamped. The second is not idempotence-by-accident: re-stamping after a
        policy change would move a deadline somebody was working to, and that
        date is what an audit asks about.
        """
        async with self._session_factory() as session, session.begin():
            current = (
                await session.execute(
                    text(
                        "SELECT materiality, classified_at, initial_report_due_at "
                        "FROM reporting_obligations "
                        "WHERE obligation_id = :oid AND tenant_id = :tid FOR UPDATE"
                    ),
                    {"oid": obligation_id, "tid": ctx.tenant_id},
                )
            ).one_or_none()
            if current is None:
                msg = "no such reporting obligation"
                raise NotFoundError(msg)
            if current.materiality != MATERIALITY_MATERIAL or current.classified_at is None:
                msg = (
                    f"obligation {obligation_id} is {current.materiality!r}; deadlines follow "
                    "a classification as material and nothing else"
                )
                raise ConflictError(msg)
            if current.initial_report_due_at is not None:
                msg = (
                    f"obligation {obligation_id} already has deadlines; re-stamping would "
                    "move a date somebody was working to"
                )
                raise ConflictError(msg)

            configured = await self._policy_in(session, ctx)
            policy = configured or self.default_policy()
            basis = BASIS_TENANT if configured is not None else BASIS_DEFAULT

            initial, intermediate, final = policy.due_from(current.classified_at)
            await session.execute(
                text(
                    "UPDATE reporting_obligations SET "
                    "  initial_report_due_at = :i, intermediate_report_due_at = :m, "
                    "  final_report_due_at = :f, deadline_basis = :basis "
                    "WHERE obligation_id = :oid AND tenant_id = :tid"
                ),
                {
                    "basis": basis,
                    "f": final,
                    "i": initial,
                    "m": intermediate,
                    "oid": obligation_id,
                    "tid": ctx.tenant_id,
                },
            )
        return StampedDeadlines(basis=basis, final=final, initial=initial, intermediate=intermediate)

    async def observe(self) -> DeadlineState:
        """Publish the deployment-wide deadline state to the four gauges.

        Across every tenant, because the gauges carry no tenant label and a
        partial answer on an unlabelled series is a wrong answer rather than an
        incomplete one -- the argument `observe_backlog` already makes.

        Scheduled rather than computed at scrape time, and the risk that creates
        is the known one for any scheduled observer: a job that silently stops
        turns a deadline into "whenever somebody looks", and the value would sit
        at its last reading looking healthy. `oldest_breach_age_seconds` is the defence
        -- an age that stops advancing while the count is non-zero is itself the
        signal.
        """
        now = self._clock.now()
        horizon = now + APPROACH_WINDOW
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT "
                        "  COUNT(*) FILTER (WHERE d.due_at < :now) AS breached, "
                        "  COUNT(*) FILTER (WHERE d.due_at >= :now AND d.due_at < :horizon) "
                        "    AS approaching, "
                        "  MIN(d.due_at) FILTER (WHERE d.due_at < :now) AS oldest "
                        "FROM reporting_obligations AS o "
                        "CROSS JOIN LATERAL (VALUES "
                        "  (o.initial_report_due_at), (o.intermediate_report_due_at), "
                        "  (o.final_report_due_at)"
                        ") AS d(due_at) "
                        "WHERE o.initial_report_due_at IS NOT NULL"
                    ),
                    {"horizon": horizon, "now": now},
                )
            ).one()
            # Stamped from the built-in default rather than a tenant's own
            # numbers. Counted separately because "nothing is breached" and
            # "nothing is breached against durations nobody here confirmed" are
            # different reassurances.
            on_default = int(
                (
                    await session.execute(
                        text("SELECT COUNT(*) FROM reporting_obligations " "WHERE deadline_basis = :basis"),
                        {"basis": BASIS_DEFAULT},
                    )
                ).scalar_one()
            )

        breached = int(row.breached)
        oldest: datetime.datetime | None = row.oldest
        age = 0.0 if oldest is None else max(0.0, (now - oldest).total_seconds())

        # Set to zero rather than left absent when there is nothing to report. A
        # missing series is indistinguishable from a scrape that failed, and
        # these are the ones somebody alerts on.
        APPROACHING_DEADLINES.set(int(row.approaching))
        BREACHED_DEADLINES.set(breached)
        OLDEST_BREACH_AGE_SECONDS.set(age)
        ON_DEFAULT_DEADLINES.set(on_default)

        return DeadlineState(
            approaching=int(row.approaching),
            breached=breached,
            oldest_breach_age_seconds=age,
            on_default_policy=on_default,
        )

    async def _policy_in(self, session: AsyncSession, ctx: TenantContext) -> DeadlinePolicy | None:
        """`policy_for` against an open session, so stamping reads it inside the
        same transaction that locked the obligation."""
        row = (
            await session.execute(
                text(
                    "SELECT initial_seconds, intermediate_seconds, final_seconds, source_note "
                    "FROM reporting_deadline_policies WHERE tenant_id = :tid"
                ),
                {"tid": ctx.tenant_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return DeadlinePolicy(
            final=datetime.timedelta(seconds=row.final_seconds),
            initial=datetime.timedelta(seconds=row.initial_seconds),
            intermediate=datetime.timedelta(seconds=row.intermediate_seconds),
            source_note=row.source_note,
        )


#: DORA's major-incident reporting timeline: an initial notification within four
#: hours of classifying the incident as major, an intermediate report by
#: seventy-two hours, and a final report within one month. Expressed from
#: classification because that is the instant this service stamps from.
_BUILT_IN_DEFAULT: Final[DeadlinePolicy] = DeadlinePolicy(
    initial=datetime.timedelta(hours=4),
    intermediate=datetime.timedelta(hours=72),
    final=datetime.timedelta(days=30),
    source_note=DEFAULT_POLICY_SOURCE,
)


__all__ = [
    "APPROACHING_DEADLINES",
    "APPROACH_WINDOW",
    "BASIS_DEFAULT",
    "BASIS_TENANT",
    "BREACHED_DEADLINES",
    "DEFAULT_POLICY_SOURCE",
    "OLDEST_BREACH_AGE_SECONDS",
    "ON_DEFAULT_DEADLINES",
    "DeadlinePolicy",
    "DeadlineState",
    "ReportingDeadlineService",
    "StampedDeadlines",
]
