"""The notification clock, against a real database. E4-T6.

The clock was held on the argument that it would be machinery around a
classification nobody could make -- true while nothing could leave
`unclassified`, and false once `classify()` shipped. These tests are what the
clock does after somebody has classified.

**A missed deadline must be loud.** The gauges are the whole point, and the one
that matters is the age series: a scheduled observer that silently stops leaves
the count at its last reading, looking healthy. An age that stops advancing while
the count is non-zero is the signal that the observer, not the breach, is the
problem — the same defence the unclassified backlog already carries.

**Three instants, stamped, never computed.** A computed deadline moves when the
classification timestamp is corrected, and "when was this due" is precisely the
question an audit asks. So a policy change must not move a deadline already
stamped, and there is a test for exactly that.

**The default is the regulation's; the override is the deployment's.** Which of
the two produced a given row is recorded on it, because a default that changes in
a later release must not leave an auditor unable to say where a deadline came
from.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ConflictError, ValidationError
from contextplane.service.governance.deadlines import (
    APPROACH_WINDOW,
    BASIS_DEFAULT,
    BASIS_TENANT,
    ReportingDeadlineService,
)
from contextplane.service.governance.obligations import (
    MATERIALITY_MATERIAL,
    MATERIALITY_NOT_MATERIAL,
    ReportingObligationService,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)
_NOTE = "Confirmed against the RTS in force for this entity on 2026-08-24."
_WHY = "Client-facing settlement was unavailable for ninety minutes across two regions."


class _World:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.tenant_id = uuid.uuid4()
        self.actor_id = uuid.uuid4()
        self.clock = FakeClock(_NOW)

    async def build(self) -> _World:
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"now": _NOW, "s": f"dl-{self.tenant_id.hex[:8]}", "t": self.tenant_id},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'operator', :sub, :now)"
                ),
                {
                    "a": self.actor_id,
                    "now": _NOW,
                    "sub": f"dl-{self.actor_id.hex[:8]}",
                    "t": self.tenant_id,
                },
            )
        return self

    def ctx(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, actor_id=self.actor_id, roles=["admin"])

    def obligations(self) -> ReportingObligationService:
        return ReportingObligationService(self.factory, clock=self.clock)

    def deadlines(self) -> ReportingDeadlineService:
        return ReportingDeadlineService(self.factory, clock=self.clock)

    async def classified_material(self) -> uuid.UUID:
        service = self.obligations()
        obligation = await service.nominate(self.ctx(), summary="Settlement outage")
        await service.classify(
            self.ctx(),
            obligation_id=obligation.obligation_id,
            materiality=MATERIALITY_MATERIAL,
            note=_WHY,
        )
        return obligation.obligation_id


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[_World]:
    engine = create_async_engine(pg_container, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield await _World(factory).build()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_classifying_as_material_stamps_three_ordered_deadlines(world: _World) -> None:
    obligation_id = await world.classified_material()

    stamped = await world.deadlines().stamp(world.ctx(), obligation_id)

    assert stamped.initial < stamped.intermediate < stamped.final
    # From the classification instant, not from now-at-stamp-time.
    assert stamped.initial > _NOW
    assert stamped.basis == BASIS_DEFAULT


@pytest.mark.asyncio
async def test_a_tenant_policy_overrides_the_default_and_says_so(world: _World) -> None:
    """The default is the regulation's; a deployment under another regime sets
    its own without a release, and the row records which it used."""
    await world.deadlines().set_policy(
        world.ctx(),
        initial=datetime.timedelta(hours=1),
        intermediate=datetime.timedelta(hours=8),
        final=datetime.timedelta(days=7),
        source_note=_NOTE,
    )
    obligation_id = await world.classified_material()

    stamped = await world.deadlines().stamp(world.ctx(), obligation_id)

    assert stamped.basis == BASIS_TENANT
    assert stamped.initial == _NOW + datetime.timedelta(hours=1)
    assert stamped.final == _NOW + datetime.timedelta(days=7)


@pytest.mark.asyncio
async def test_changing_the_policy_does_not_move_a_deadline_already_stamped(
    world: _World,
) -> None:
    """The reason the instants are stored rather than computed.

    A deadline is what somebody was working to. Recomputing it from current
    inputs would rewrite the audit's answer to "when was this due", which is
    exactly the question the record exists to answer.
    """
    obligation_id = await world.classified_material()
    service = world.deadlines()
    before = await service.stamp(world.ctx(), obligation_id)

    await service.set_policy(
        world.ctx(),
        initial=datetime.timedelta(minutes=15),
        intermediate=datetime.timedelta(hours=1),
        final=datetime.timedelta(days=1),
        source_note=_NOTE,
    )
    reread = await world.obligations().get(world.ctx(), obligation_id=obligation_id)

    assert reread.initial_report_due_at == before.initial
    assert reread.final_report_due_at == before.final


@pytest.mark.asyncio
async def test_deadlines_do_not_follow_a_classification_that_is_not_material(
    world: _World,
) -> None:
    """`not_material` is a decision somebody made, and it makes nothing due."""
    service = world.obligations()
    obligation = await service.nominate(world.ctx(), summary="Brief latency blip in the reporting path")
    await service.classify(
        world.ctx(),
        obligation_id=obligation.obligation_id,
        materiality=MATERIALITY_NOT_MATERIAL,
        note="Recovered inside the monitoring interval with no client impact.",
    )

    with pytest.raises(ConflictError, match="not_material"):
        await world.deadlines().stamp(world.ctx(), obligation.obligation_id)


@pytest.mark.asyncio
async def test_an_unclassified_obligation_has_no_deadlines(world: _World) -> None:
    """A deadline on an unclassified row would be a deadline for a decision
    nobody made. Refused by the service and by a CHECK."""
    obligation = await world.obligations().nominate(world.ctx(), summary="Unclassified nomination awaiting review")

    with pytest.raises(ConflictError):
        await world.deadlines().stamp(world.ctx(), obligation.obligation_id)


@pytest.mark.asyncio
async def test_restamping_is_refused(world: _World) -> None:
    obligation_id = await world.classified_material()
    service = world.deadlines()
    await service.stamp(world.ctx(), obligation_id)

    with pytest.raises(ConflictError, match="already has deadlines"):
        await service.stamp(world.ctx(), obligation_id)


@pytest.mark.asyncio
async def test_an_out_of_order_policy_is_refused_by_the_service(world: _World) -> None:
    """Named field first rather than a 500 from the CHECK behind it."""
    with pytest.raises(ValidationError, match="ordered"):
        await world.deadlines().set_policy(
            world.ctx(),
            initial=datetime.timedelta(hours=8),
            intermediate=datetime.timedelta(hours=1),
            final=datetime.timedelta(days=7),
            source_note=_NOTE,
        )


@pytest.mark.asyncio
async def test_a_policy_with_no_stated_source_is_refused(world: _World) -> None:
    """Three durations with no stated source are three numbers nobody can
    audit — the same discipline a classification note carries."""
    with pytest.raises(ValidationError, match="source note"):
        await world.deadlines().set_policy(
            world.ctx(),
            initial=datetime.timedelta(hours=4),
            intermediate=datetime.timedelta(hours=72),
            final=datetime.timedelta(days=30),
            source_note="RTS",
        )


@pytest.mark.asyncio
async def test_a_breached_deadline_is_counted_and_aged(world: _World) -> None:
    """The gauge pair somebody alerts on.

    The count alone is not actionable: one that passed an hour ago and one that
    passed in March are the same number and completely different situations.

    Asserted as a delta because `observe()` is deployment-wide on purpose -- the
    gauges carry no tenant label, so a partial answer would be a wrong one -- and
    a shared test database means somebody else's rows are legitimately in it.
    """
    service = world.deadlines()
    world.clock.set(_NOW + datetime.timedelta(hours=6))
    before = await service.observe()

    world.clock.set(_NOW)
    obligation_id = await world.classified_material()
    await service.stamp(world.ctx(), obligation_id)

    # Past the initial deadline but not the intermediate one.
    world.clock.set(_NOW + datetime.timedelta(hours=6))
    after = await service.observe()

    assert after.breached == before.breached + 1
    # Two hours past a four-hour initial deadline.
    assert after.oldest_breach_age_seconds >= 2 * 3600 - 1


@pytest.mark.asyncio
async def test_a_deadline_inside_the_window_is_approaching_rather_than_breached(
    world: _World,
) -> None:
    service = world.deadlines()
    world.clock.set(_NOW + datetime.timedelta(hours=3))
    before = await service.observe()

    world.clock.set(_NOW)
    obligation_id = await world.classified_material()
    await service.stamp(world.ctx(), obligation_id)

    # An hour before the initial deadline falls due.
    world.clock.set(_NOW + datetime.timedelta(hours=3))
    after = await service.observe()

    assert after.breached == before.breached
    assert after.approaching == before.approaching + 1


@pytest.mark.asyncio
async def test_running_on_the_default_is_visible(world: _World) -> None:
    """Not an error -- the default is the regulation's -- but a deployment that
    has confirmed nothing against its own regulator should be able to see that
    it has not."""
    service = world.deadlines()
    before = await service.observe()

    obligation_id = await world.classified_material()
    await service.stamp(world.ctx(), obligation_id)
    after = await service.observe()

    assert after.on_default_policy == before.on_default_policy + 1


@pytest.mark.asyncio
async def test_the_gauges_are_set_rather_than_left_absent(world: _World) -> None:
    """A missing series is indistinguishable from a scrape that failed, and
    these are the ones somebody alerts on.

    Read back off the collectors rather than off the return value, because the
    return value proving a number was computed says nothing about whether it was
    published.
    """
    from contextplane.service.governance.deadlines import (
        BREACHED_DEADLINES,
        OLDEST_BREACH_AGE_SECONDS,
    )

    state = await world.deadlines().observe()

    assert BREACHED_DEADLINES._value.get() == state.breached
    assert OLDEST_BREACH_AGE_SECONDS._value.get() == state.oldest_breach_age_seconds


@pytest.mark.asyncio
async def test_the_approach_window_is_a_deployment_constant(world: _World) -> None:
    """Deliberately not per-tenant: it describes when somebody wants to know,
    not what the regulation requires, and one unlabelled gauge cannot mean
    different things for different rows."""
    assert APPROACH_WINDOW > datetime.timedelta(0)
