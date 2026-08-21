"""Graduating a tenant to enforcing: a scan over who it would break.

The transition from `advisory` to `enforcing` is not a flag flip. It runs a
pre-flight first, in the shape `_run_graduation_preflight` already uses for
progression definitions: a dry-run that reports without writing, a refusal
carrying the offender list when the scan is non-empty, a bounded scan, and a
force path that requires a written migration plan.

Reused rather than reinvented because it is a working implementation of exactly
this transition, and because four disagreeing enforcement vocabularies already
ship in this service -- a fifth mechanism for the same job would make it
four-against-one.

**The scan asks who would break, not how many refusals there would be.** On day
one every principal is unenveloped, so a count of would-be refusals fires on
every request and measures nothing. The answerable question is which principals
acted with no envelope at all, and it is answerable only because the advisory
stage recorded them.

**Only `no_envelope` is an offender.** A principal refused *outside* a real
envelope is a governance finding: the envelope exists, it is in force, and it
declined the act -- which is the system working, and graduating will not change
it. `envelope_suspended` and `envelope_withdrawn` are likewise the machinery
doing its job. `no_envelope` alone means the rollout is incomplete for that
principal, and it is the only refusal graduation would convert from a record
into an outage.

**The report carries the population it scanned, not only the offenders it
found.** An empty offender list means "nobody would break" only if something was
looked at; if no advisory records exist at all, an empty list means the advisory
stage never observed this tenant, and graduating on that is passing a gate
vacuously. So `records_scanned` and `principals_seen` are reported alongside, and
the route refuses a graduation with no observations rather than treating silence
as consent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.arc.service import audit_outbox
from contextplane.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from contextplane.arc.types import ArcRequestContext, AuthorityScope
from contextplane.audit import actions
from contextplane.exceptions import RegistryError, ValidationError
from contextplane.types import Clock

#: How far back the scan looks by default. Thirty days, matching the
#: `advisory_until` window `contextplane/service/catalog/schema.py` already uses
#: for the one registry that has a graduation window -- a new number here would
#: need an argument for why envelope observation differs from schema
#: observation, and there is not one.
DEFAULT_OBSERVATION_DAYS = 30

#: The verdict that blocks graduation. The other three are the envelope working.
_BLOCKING_VERDICT = "no_envelope"

#: How long the scan may take before the graduation is refused rather than
#: guessed at. Thirty seconds, matching `force_timeout_seconds`' default on
#: the progression pre-flight this mirrors.
DEFAULT_SCAN_TIMEOUT_SECONDS = 30.0

#: Operator prose, bounded before it reaches an audit payload.
_REASON_LIMIT = 200
_PLAN_LIMIT = 2000

_OFFENDERS = text(
    """
    SELECT principal_issuer, principal_subject,
           count(*)        AS occurrences,
           min(decided_at) AS first_seen,
           max(decided_at) AS last_seen
    FROM arc_envelope_advisory_records
    WHERE tenant_id = :tenant_id
      AND verdict = :verdict
      AND decided_at >= :since
    GROUP BY principal_issuer, principal_subject
    ORDER BY principal_subject, principal_issuer
    """
)

#: The anti-vacuity half. Counted over *every* verdict, because a tenant whose
#: agents all acted inside their envelopes produces no rows at all and must not
#: be confused with one nothing ever observed.
_POPULATION = text(
    """
    SELECT count(*) AS records,
           count(DISTINCT (principal_issuer, principal_subject)) AS principals
    FROM arc_envelope_advisory_records
    WHERE tenant_id = :tenant_id
      AND decided_at >= :since
    """
)


@dataclasses.dataclass(frozen=True)
class EnvelopeOffender:
    """One principal that acted with no envelope inside the observation window.

    The identity halves are strings rather than a `WorkloadIdentity`, and the
    instants are ISO strings, because the only consumer is the admin route's
    JSON report -- building the JSON-ready shape here means the route needs no
    conversion step, which is the choice `GraduationOffender` already made.
    """

    principal_issuer: str
    principal_subject: str
    occurrences: int
    first_seen: str
    last_seen: str


@dataclasses.dataclass(frozen=True)
class GraduationReport:
    """What the pre-flight found, including what it looked at."""

    offenders: tuple[EnvelopeOffender, ...]
    records_scanned: int
    principals_seen: int
    since: str

    @property
    def is_clean(self) -> bool:
        return not self.offenders

    @property
    def observed_nothing(self) -> bool:
        """Whether the window is empty, which is not the same as clean.

        An empty offender list over an empty window says the advisory stage
        never saw this tenant, not that graduating is safe. Kept separate from
        `is_clean` so a caller cannot read one and get the other.
        """
        return self.records_scanned == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "offenders": [dataclasses.asdict(o) for o in self.offenders],
            "records_scanned": self.records_scanned,
            "principals_seen": self.principals_seen,
            "since": self.since,
        }


async def scan_envelope_offenders(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    now: datetime.datetime,
    observation_days: int = DEFAULT_OBSERVATION_DAYS,
) -> GraduationReport:
    """Which principals would this tenant's graduation break, and what was looked at.

    `now` is a parameter rather than a clock read so the same window can be
    re-scanned and give the same answer -- an operator comparing a dry run
    against the write that follows it needs the two to agree.
    """
    since = now - datetime.timedelta(days=observation_days)
    params = {"tenant_id": tenant_id, "since": since}

    async with session_factory() as session:
        offender_rows = (await session.execute(_OFFENDERS, {**params, "verdict": _BLOCKING_VERDICT})).all()
        population = (await session.execute(_POPULATION, params)).one()

    return GraduationReport(
        offenders=tuple(
            EnvelopeOffender(
                principal_issuer=row.principal_issuer,
                principal_subject=row.principal_subject,
                occurrences=int(row.occurrences),
                first_seen=row.first_seen.isoformat(),
                last_seen=row.last_seen.isoformat(),
            )
            for row in offender_rows
        ),
        records_scanned=int(population.records),
        principals_seen=int(population.principals),
        since=since.isoformat(),
    )


class GraduationBlocked(RegistryError):
    """The scan found principals this graduation would break.

    Carries the report so the caller can show who, rather than only that. A
    refusal an operator cannot act on is a refusal they will force past.
    """

    def __init__(self, report: GraduationReport) -> None:
        self.report = report
        names = ", ".join(f"{o.principal_subject}" for o in report.offenders[:5])
        more = "" if len(report.offenders) <= 5 else f" (+{len(report.offenders) - 5} more)"
        super().__init__(
            f"{len(report.offenders)} principal(s) acted with no envelope in this window "
            f"and would be refused: {names}{more}. Pass force with a migration plan to proceed."
        )


class NothingObserved(RegistryError):
    """The observation window is empty, so the scan proves nothing.

    Distinct from `GraduationBlocked` because the operator's next step differs:
    a blocked graduation needs envelopes written, an unobserved one needs the
    advisory stage to actually see traffic first. Treating silence as consent is
    how a gate passes vacuously.
    """


class ScanTimedOut(RegistryError):
    """The offender scan did not finish inside its bound.

    Its own error rather than an empty report: an unknown offender set must
    not graduate, because the gate's whole meaning is that an empty list
    implies something was looked at.
    """


class MigrationPlanRequired(ValidationError):
    """`force` without a written plan. A caller error, not a finding.

    Raised before the scan runs, because nothing was scanned -- reporting
    offenders here would suggest the force was evaluated and rejected on their
    account, and it was not.
    """


class AutonomyGraduationService:
    """Moves a tenant between enforcement stages, with a pre-flight in front."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock

    async def graduate(
        self,
        ctx: ArcRequestContext,
        *,
        reason: str,
        dry_run: bool = False,
        force: bool = False,
        migration_plan: str | None = None,
        observation_days: int = DEFAULT_OBSERVATION_DAYS,
        scan_timeout_seconds: float = DEFAULT_SCAN_TIMEOUT_SECONDS,
    ) -> GraduationReport:
        """Move the requesting tenant to `enforcing`, or report what would stop it.

        Tenant admin, because graduating only narrows: every act it changes is
        one that was already being recorded as a would-be refusal. Going the
        other way widens and is `demote`, which costs more.

        The pre-flight runs on every call including `force`, except for the
        `force`-without-a-plan guard which is a caller error. Conditioning the
        scan on anything would let a caller's `dry_run` be silently discarded --
        the failure the progression pre-flight's own docstring records.
        """
        self._authorization.assert_request_tenant(ctx)
        _require_reason(reason)
        self._authorization.assert_can_write_artifact(
            ctx, ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=ctx.tenant_id)
        )
        if force and not migration_plan:
            msg = "force requires a written migration_plan"
            raise MigrationPlanRequired(msg)

        try:
            report = await asyncio.wait_for(
                scan_envelope_offenders(
                    self._session_factory,
                    tenant_id=ctx.tenant_id,
                    now=self._clock.now(),
                    observation_days=observation_days,
                ),
                timeout=scan_timeout_seconds,
            )
        except TimeoutError:
            # Nothing is written on a scan that did not finish, so there is
            # nothing partial to report except "unknown" -- and an unknown
            # offender set must not graduate, because the whole point of the
            # gate is that an empty list has to mean something was looked at.
            #
            # Not theatre despite the scan being two indexed aggregates: this
            # table gains a row per advisory refusal and is never pruned here,
            # so a tenant with a busy month is exactly the case where the
            # GROUP BY stops being instant.
            msg = f"the offender scan exceeded {scan_timeout_seconds}s; no stage change was written"
            raise ScanTimedOut(msg) from None
        if dry_run:
            return report
        if not force:
            if report.observed_nothing:
                msg = (
                    "no envelope decisions were recorded in this window, so the scan proves nothing; "
                    "let the advisory stage observe traffic, or force with a migration plan"
                )
                raise NothingObserved(msg)
            if not report.is_clean:
                raise GraduationBlocked(report)

        await self._set_stage(ctx, "enforcing", reason=reason, report=report, migration_plan=migration_plan)
        return report

    async def demote(self, ctx: ArcRequestContext, *, reason: str) -> None:
        """Move the requesting tenant back to `advisory`.

        The deployment-operator allowlist, not tenant admin. Graduating narrows
        and demoting widens: it turns every refusal this tenant is making back
        into a record, which is the one direction that can hand authority back
        to a principal that was being refused. No pre-flight -- there is nothing
        a demotion can strand.
        """
        self._authorization.assert_request_tenant(ctx)
        _require_reason(reason)
        self._authorization.assert_can_write_artifact(ctx, ArtifactScope(scope=AuthorityScope.GLOBAL))
        await self._set_stage(ctx, "advisory", reason=reason, report=None, migration_plan=None)

    async def _set_stage(
        self,
        ctx: ArcRequestContext,
        stage: str,
        *,
        reason: str,
        report: GraduationReport | None,
        migration_plan: str | None,
    ) -> None:
        """Write the stage and its audit row in one transaction.

        Together, unlike the advisory record: this *is* the state change, so an
        audit row that survived a rolled-back graduation would describe
        something that did not happen.
        """
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE tenants SET envelope_enforcement_stage = :stage WHERE tenant_id = :tid"),
                {"stage": stage, "tid": ctx.tenant_id},
            )
            payload: dict[str, object] = {
                "stage": stage,
                "actor": str(ctx.actor_id),
                "reason": reason[:_REASON_LIMIT],
            }
            if migration_plan is not None:
                # Recorded on the audit row, which is the whole point of
                # requiring it: a force with no durable record of why is a force
                # nobody can review afterwards.
                payload["migration_plan"] = migration_plan[:_PLAN_LIMIT]
                payload["forced"] = True
            if report is not None:
                payload["offenders_at_write"] = len(report.offenders)
                payload["records_scanned"] = report.records_scanned
            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_ENVELOPE_ENFORCEMENT_STAGE_SET,
                payload=payload,
            )


def _require_reason(reason: str) -> None:
    if not reason.strip():
        msg = "an enforcement stage change requires a reason"
        raise ValidationError(msg)


__all__ = [
    "DEFAULT_OBSERVATION_DAYS",
    "DEFAULT_SCAN_TIMEOUT_SECONDS",
    "AutonomyGraduationService",
    "EnvelopeOffender",
    "GraduationBlocked",
    "GraduationReport",
    "MigrationPlanRequired",
    "NothingObserved",
    "ScanTimedOut",
    "scan_envelope_offenders",
]
