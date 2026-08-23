"""The review budget against a real database. E5-T2.

Contract under test
----------------------------------------------------
Three things a unit test cannot show:

**The fallback is fail-closed, and it is the tenant's own heaviest plan.** An
unregistered category must not escape every rule that names one — the rule E1's
audit established for an unrecognised sensitivity tier. And the fallback is not
a constant: a tenant reviewing everything closely should not have an unknown
category drop to a laxer number this module chose.

**A row whose three numbers disagree is refused on read.** `min_sample` is
stored rather than computed at read so a reviewer's budget cannot change because
a floating-point library did — which means a row edited directly in the database
would otherwise serve as though it were derived.

**The category CHECK is generated from the vocabulary.** A value outside
`CLAIM_CATEGORIES` is refused by the schema, not only by the service, so a
second write path cannot introduce one.

Uses a real Postgres container via the session-scoped ``pg_container`` fixture.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory.sampling_policy import (
    SamplingPolicyService,
    minimum_sample,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.UTC)
_REASON = "Dependency claims are cheap to verify and expensive to get wrong."


class _World:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.tenant_id = uuid.uuid4()
        self.actor_id = uuid.uuid4()

    async def build(self) -> _World:
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"now": _NOW, "s": f"sp-{self.tenant_id.hex[:8]}", "t": self.tenant_id},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'op', :sub, :now)"
                ),
                {"a": self.actor_id, "now": _NOW, "sub": f"sp-{self.actor_id.hex[:8]}", "t": self.tenant_id},
            )
        return self

    def ctx(self, *, roles: list[str] | None = None) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, actor_id=self.actor_id, roles=roles or ["producer"])

    def service(self) -> SamplingPolicyService:
        return SamplingPolicyService(self.factory, clock=FakeClock(_NOW))


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[_World]:
    engine = create_async_engine(pg_container, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield await _World(factory).build()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_policy_round_trips_with_the_sample_its_inputs_derive(world: _World) -> None:
    service = world.service()

    await service.set_policy(
        world.ctx(),
        claim_category="dependency",
        defect_tolerance=0.05,
        consumers_risk=0.10,
        reason=_REASON,
    )
    found = await service.policy_for(world.ctx(), claim_category="dependency")

    assert found.min_sample == 45
    assert found.min_sample == minimum_sample(0.05, 0.10)
    assert found.recomputes()


@pytest.mark.asyncio
async def test_an_unregistered_category_falls_to_the_tenants_heaviest_plan(world: _World) -> None:
    """The fail-closed rule. A value nobody registered must not escape every
    rule that names one — and the fallback is the tenant's own strictest policy,
    not a constant, so a tenant reviewing everything closely keeps that bar."""
    service = world.service()
    await service.set_policy(
        world.ctx(),
        claim_category="dependency",
        defect_tolerance=0.20,
        consumers_risk=0.10,
        reason="Dependency claims are the cheapest here to re-derive from the graph.",
    )
    await service.set_policy(
        world.ctx(),
        claim_category="incident_history",
        defect_tolerance=0.02,
        consumers_risk=0.10,
        reason="An incident recorded wrongly is quoted back for years afterwards.",
    )

    unknown = await service.policy_for(world.ctx(), claim_category="not_a_category")

    assert unknown.min_sample == minimum_sample(0.02, 0.10), "the heaviest plan, not the laxest"
    assert unknown.claim_category == "incident_history"


@pytest.mark.asyncio
async def test_a_tenant_with_no_policy_at_all_gets_the_governed_floor(world: _World) -> None:
    """Silence is not permission. A tenant that has configured nothing has not
    decided that little review is acceptable."""
    found = await world.service().policy_for(world.ctx(), claim_category="dependency")

    assert found.min_sample == 299
    assert found.recomputes()


@pytest.mark.asyncio
async def test_setting_a_policy_twice_replaces_it_rather_than_conflicting(world: _World) -> None:
    """A tenant revising its own tolerance is the normal case, not an error."""
    service = world.service()
    await service.set_policy(
        world.ctx(),
        claim_category="dependency",
        defect_tolerance=0.20,
        consumers_risk=0.10,
        reason=_REASON,
    )
    await service.set_policy(
        world.ctx(),
        claim_category="dependency",
        defect_tolerance=0.05,
        consumers_risk=0.10,
        reason="Tightened after a quarter in which two dependency claims were wrong.",
    )

    found = await service.policy_for(world.ctx(), claim_category="dependency")
    assert found.min_sample == 45
    assert len(await service.policies(world.ctx())) == 1


@pytest.mark.asyncio
async def test_a_row_edited_in_the_database_is_refused_rather_than_served(world: _World) -> None:
    """`min_sample` is stored so a budget cannot move under a reviewer, which
    means a hand-edited row would otherwise serve as though it were derived."""
    service = world.service()
    await service.set_policy(
        world.ctx(),
        claim_category="dependency",
        defect_tolerance=0.05,
        consumers_risk=0.10,
        reason=_REASON,
    )
    async with world.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE claim_sampling_policies SET min_sample = 5 "
                " WHERE tenant_id = :t AND claim_category = 'dependency'"
            ),
            {"t": world.tenant_id},
        )

    with pytest.raises(ConflictError, match="no longer follows"):
        await service.policy_for(world.ctx(), claim_category="dependency")


@pytest.mark.asyncio
async def test_the_schema_refuses_a_category_outside_the_vocabulary(world: _World) -> None:
    """The CHECK is generated from `CLAIM_CATEGORIES`, so a second write path
    cannot introduce a value the service would have refused."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with world.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO claim_sampling_policies "
                    "  (tenant_id, claim_category, defect_tolerance, consumers_risk, min_sample, "
                    "   set_by, reason) "
                    "VALUES (:t, 'not_a_category', 0.05, 0.10, 45, :a, :reason)"
                ),
                {"a": world.actor_id, "reason": _REASON, "t": world.tenant_id},
            )


@pytest.mark.asyncio
async def test_the_service_refuses_an_unknown_category_before_writing(world: _World) -> None:
    with pytest.raises(ValidationError, match="unknown claim category"):
        await world.service().set_policy(
            world.ctx(),
            claim_category="not_a_category",
            defect_tolerance=0.05,
            consumers_risk=0.10,
            reason=_REASON,
        )


@pytest.mark.asyncio
async def test_a_policy_needs_a_reason_somebody_can_review(world: _World) -> None:
    with pytest.raises(ValidationError, match="stated reason"):
        await world.service().set_policy(
            world.ctx(),
            claim_category="dependency",
            defect_tolerance=0.05,
            consumers_risk=0.10,
            reason="prod",
        )


@pytest.mark.asyncio
async def test_removing_a_policy_falls_back_rather_than_leaving_the_category_ungoverned(
    world: _World,
) -> None:
    """Deleting is not the same as setting a lax policy, and the fallback is
    what makes it safe."""
    service = world.service()
    await service.set_policy(
        world.ctx(),
        claim_category="dependency",
        defect_tolerance=0.20,
        consumers_risk=0.10,
        reason=_REASON,
    )

    await service.remove(world.ctx(), claim_category="dependency")
    found = await service.policy_for(world.ctx(), claim_category="dependency")

    assert found.min_sample == 299, "the governed floor, not nothing"
    with pytest.raises(NotFoundError):
        await service.remove(world.ctx(), claim_category="dependency")


@pytest.mark.asyncio
async def test_setting_a_policy_requires_the_operator_role(world: _World) -> None:
    with pytest.raises(PermissionError):
        await world.service().set_policy(
            world.ctx(roles=["consumer"]),
            claim_category="dependency",
            defect_tolerance=0.05,
            consumers_risk=0.10,
            reason=_REASON,
        )
