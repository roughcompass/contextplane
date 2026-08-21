"""Graduating a tenant to enforcing: the pre-flight, and what it refuses.

Two failures this file exists to prevent, and they pull in opposite directions.

**Graduating a tenant with ungoverned principals turns a recorded would-be
refusal into an outage** for every one of them at once. That is what the offender
scan is for.

**Graduating a tenant nobody observed passes the gate vacuously.** An empty
offender list means "nobody would break" only if something was looked at, and a
tenant whose advisory stage never saw traffic produces exactly the same empty
list as a tenant whose agents are all correctly enveloped. `observed_nothing` is
the distinction, and `test_graduating_an_unobserved_tenant_is_refused` is what
keeps it load-bearing.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.service.authorization import ArcAuthorizationError, ArcAuthorizationService
from contextplane.arc.service.autonomy_graduation import (
    AutonomyGraduationService,
    GraduationBlocked,
    MigrationPlanRequired,
    NothingObserved,
    ScanTimedOut,
    scan_envelope_offenders,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.audit import actions
from contextplane.exceptions import ValidationError
from contextplane.types import TenantContext
from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc
from tests.helpers.clock import FakeClock

_ISSUER = "https://idp.example.test"
_IAM = "https://iam.example.test"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-graduation")


class _AllVisible:
    async def visible_entity_ids(self, ctx: object, entity_ids: object) -> list[uuid.UUID]:
        return list(entity_ids)  # type: ignore[arg-type]


def _service(
    factory: async_sessionmaker[AsyncSession], *, allowlist: tuple[tuple[str, str], ...] = ()
) -> AutonomyGraduationService:
    return AutonomyGraduationService(
        factory,
        authorization=ArcAuthorizationService(visibility=_AllVisible(), global_write_allowlist=allowlist),
        clock=FakeClock(ARC_NOW),
    )


@pytest.fixture
def graduation(factory: async_sessionmaker[AsyncSession]) -> AutonomyGraduationService:
    return _service(factory)


def _ctx(seed: ArcSeed, *, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        roles=roles if roles is not None else ["admin"],
        oidc_subject="operator-1",
    )
    return ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER}, host_id="h")


async def _record(
    factory: async_sessionmaker[AsyncSession],
    seed: ArcSeed,
    *,
    verdict: str,
    subject: str = "workload/deploy-agent",
    age_days: int = 1,
) -> None:
    """One advisory record, as the enforcement service would have written it."""
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO arc_envelope_advisory_records ("
                "  record_id, tenant_id, principal_issuer, principal_subject, verdict,"
                "  binding_id, intent_kind, session_id, decided_at"
                ") VALUES (:rec, :tid, :iss, :sub, :verdict, :bid, 'deployment', 's1', :at)"
            ),
            {
                "rec": uuid.uuid4(),
                "tid": seed.tenant_id,
                "iss": _IAM,
                "sub": subject,
                "verdict": verdict,
                # The CHECK ties these together: no_envelope has no binding, and
                # every other verdict must name one.
                "bid": None if verdict == "no_envelope" else uuid.uuid4(),
                "at": ARC_NOW - datetime.timedelta(days=age_days),
            },
        )


async def _stage(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> str:
    async with factory() as session:
        return str(
            (
                await session.execute(
                    text("SELECT envelope_enforcement_stage FROM tenants WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
            ).scalar_one()
        )


async def _audit(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> list[dict[str, object]]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, event_payload FROM arc_audit_outbox "
                    "WHERE tenant_id = :tid ORDER BY created_at"
                ),
                {"tid": tenant_id},
            )
        ).all()
    return [{"event_type": r[0], "payload": r[1]} for r in rows]


# --- the scan ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_no_envelope_is_an_offender(seed: ArcSeed, factory: async_sessionmaker[AsyncSession]) -> None:
    """A principal refused *outside* a real envelope is the system working.

    Graduating will not change that outcome, so it must not block the
    graduation. Only an ungoverned principal turns from a record into an outage.
    """
    await _record(factory, seed, verdict="no_envelope", subject="ungoverned")
    await _record(factory, seed, verdict="outside_envelope", subject="narrow")
    await _record(factory, seed, verdict="envelope_suspended", subject="paused")
    await _record(factory, seed, verdict="envelope_withdrawn", subject="stale")

    report = await scan_envelope_offenders(factory, tenant_id=seed.tenant_id, now=ARC_NOW)

    assert [o.principal_subject for o in report.offenders] == ["ungoverned"]
    assert report.records_scanned == 4, "the population counts every verdict, not only the blocking one"
    assert report.principals_seen == 4


@pytest.mark.asyncio
async def test_the_scan_aggregates_a_principal_rather_than_listing_each_act(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """An operator needs the list of principals to write envelopes for, not a
    log. Occurrences and the window are carried so a one-off is visibly
    different from a constant offender."""
    for age in (1, 2, 5):
        await _record(factory, seed, verdict="no_envelope", age_days=age)

    report = await scan_envelope_offenders(factory, tenant_id=seed.tenant_id, now=ARC_NOW)

    assert len(report.offenders) == 1
    [offender] = report.offenders
    assert offender.occurrences == 3
    assert offender.first_seen == (ARC_NOW - datetime.timedelta(days=5)).isoformat()
    assert offender.last_seen == (ARC_NOW - datetime.timedelta(days=1)).isoformat()


@pytest.mark.asyncio
async def test_records_outside_the_window_are_not_scanned(
    seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A principal that was ungoverned two months ago and has been enveloped
    since is not a reason to refuse today's graduation."""
    await _record(factory, seed, verdict="no_envelope", age_days=60)

    report = await scan_envelope_offenders(factory, tenant_id=seed.tenant_id, now=ARC_NOW)

    assert report.is_clean
    assert report.observed_nothing, "and the window is empty, which is a different fact"


@pytest.mark.asyncio
async def test_the_scan_is_tenant_scoped(seed: ArcSeed, factory: async_sessionmaker[AsyncSession]) -> None:
    other = await seed_arc(factory, slug_prefix="arc-graduation-other")
    await _record(factory, other, verdict="no_envelope")

    report = await scan_envelope_offenders(factory, tenant_id=seed.tenant_id, now=ARC_NOW)

    assert report.is_clean
    assert report.records_scanned == 0


# --- the pre-flight paths -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dry_run_reports_without_writing(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    await _record(factory, seed, verdict="no_envelope")

    report = await graduation.graduate(_ctx(seed), reason="checking", dry_run=True)

    assert len(report.offenders) == 1
    assert await _stage(factory, seed.tenant_id) == "advisory"
    assert await _audit(factory, seed.tenant_id) == []


@pytest.mark.asyncio
async def test_a_dry_run_reports_even_when_it_would_be_refused(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """`dry_run` names a caller expectation -- preview, do not write -- and a
    refusal would discard it. The progression pre-flight records the same rule:
    the scan cannot be conditioned on anything the caller also passed."""
    report = await graduation.graduate(_ctx(seed), reason="checking", dry_run=True)

    assert report.observed_nothing
    assert await _stage(factory, seed.tenant_id) == "advisory"


@pytest.mark.asyncio
async def test_offenders_block_the_graduation_and_are_named(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A refusal an operator cannot act on is a refusal they will force past."""
    await _record(factory, seed, verdict="no_envelope", subject="workload/lonely")

    with pytest.raises(GraduationBlocked) as caught:
        await graduation.graduate(_ctx(seed), reason="go")

    assert "workload/lonely" in str(caught.value)
    assert caught.value.report.offenders[0].principal_subject == "workload/lonely"
    assert await _stage(factory, seed.tenant_id) == "advisory"


@pytest.mark.asyncio
async def test_graduating_an_unobserved_tenant_is_refused(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The anti-vacuity rule, and the reason `observed_nothing` is separate from
    `is_clean`.

    A tenant the advisory stage never saw produces exactly the same empty
    offender list as one whose agents are all correctly enveloped. Treating the
    two alike would let a gate pass on silence.
    """
    with pytest.raises(NothingObserved, match="proves nothing"):
        await graduation.graduate(_ctx(seed), reason="go")

    assert await _stage(factory, seed.tenant_id) == "advisory"


@pytest.mark.asyncio
async def test_a_clean_observed_window_graduates(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Observed, and every refusal in it was the envelope working."""
    await _record(factory, seed, verdict="outside_envelope")

    report = await graduation.graduate(_ctx(seed), reason="rollout complete")

    assert report.is_clean
    assert not report.observed_nothing
    assert await _stage(factory, seed.tenant_id) == "enforcing"

    [event] = await _audit(factory, seed.tenant_id)
    assert event["event_type"] == actions.ARC_ENVELOPE_ENFORCEMENT_STAGE_SET
    assert event["payload"]["stage"] == "enforcing"  # type: ignore[index]
    assert event["payload"]["reason"] == "rollout complete"  # type: ignore[index]


@pytest.mark.asyncio
async def test_force_without_a_plan_is_a_caller_error(graduation: AutonomyGraduationService, seed: ArcSeed) -> None:
    """Raised before the scan, because nothing was scanned. Reporting offenders
    here would suggest the force was evaluated on their account."""
    with pytest.raises(MigrationPlanRequired):
        await graduation.graduate(_ctx(seed), reason="go", force=True)


@pytest.mark.asyncio
async def test_force_with_a_plan_graduates_over_offenders_and_records_why(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The plan is required so the force is reviewable afterwards, which means it
    has to reach the audit row rather than only the request."""
    await _record(factory, seed, verdict="no_envelope")

    await graduation.graduate(
        _ctx(seed), reason="accepted risk", force=True, migration_plan="agents A and B are being retired Friday"
    )

    assert await _stage(factory, seed.tenant_id) == "enforcing"
    [event] = await _audit(factory, seed.tenant_id)
    assert event["payload"]["forced"] is True  # type: ignore[index]
    assert "retired Friday" in str(event["payload"]["migration_plan"])  # type: ignore[index]
    assert event["payload"]["offenders_at_write"] == 1, (  # type: ignore[index]
        "the audit row records what was overridden, not just that something was"
    )


# --- who may move the stage ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_reader_cannot_graduate(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    await _record(factory, seed, verdict="outside_envelope")

    with pytest.raises(ArcAuthorizationError):
        await graduation.graduate(_ctx(seed, roles=["reader"]), reason="go")


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_demote(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Graduating narrows; demoting widens.

    A demotion turns every refusal this tenant is making back into a record,
    which is the one direction that hands authority back to a principal that was
    being refused -- so it costs the deployment-operator allowlist, the same line
    the envelope bindings draw.
    """
    await _record(factory, seed, verdict="outside_envelope")
    await graduation.graduate(_ctx(seed), reason="go")

    with pytest.raises(ArcAuthorizationError):
        await graduation.demote(_ctx(seed), reason="rolling back")

    assert await _stage(factory, seed.tenant_id) == "enforcing"


@pytest.mark.asyncio
async def test_a_deployment_operator_may_demote(seed: ArcSeed, factory: async_sessionmaker[AsyncSession]) -> None:
    await _record(factory, seed, verdict="outside_envelope")
    await _service(factory).graduate(_ctx(seed), reason="go")

    operator = _service(factory, allowlist=((_ISSUER, "operator-1"),))
    await operator.demote(_ctx(seed), reason="regression in the matrix")

    assert await _stage(factory, seed.tenant_id) == "advisory"
    events = await _audit(factory, seed.tenant_id)
    assert events[-1]["payload"]["stage"] == "advisory"  # type: ignore[index]


@pytest.mark.asyncio
async def test_every_stage_change_requires_a_reason(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """An authority record whose history reads "somebody changed this" is not an
    audit trail."""
    await _record(factory, seed, verdict="outside_envelope")

    with pytest.raises(ValidationError):
        await graduation.graduate(_ctx(seed), reason="   ")


@pytest.mark.asyncio
async def test_a_scan_that_does_not_finish_refuses_rather_than_reporting_clean(
    graduation: AutonomyGraduationService, seed: ArcSeed, factory: async_sessionmaker[AsyncSession]
) -> None:
    """An unknown offender set must not graduate.

    The gate's whole meaning is that an empty list implies something was looked
    at, so a scan that did not finish cannot fall back to an empty report. A
    zero timeout is the cheapest way to reach the branch; the real trigger is a
    tenant with a busy month, since this table gains a row per advisory refusal
    and nothing prunes it here.
    """
    await _record(factory, seed, verdict="no_envelope")

    with pytest.raises(ScanTimedOut, match="no stage change was written"):
        await graduation.graduate(_ctx(seed), reason="go", scan_timeout_seconds=0.0)

    assert await _stage(factory, seed.tenant_id) == "advisory"
