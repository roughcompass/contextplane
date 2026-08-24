"""Accepting a migrated lot, against a real database. E12-T3 and E12-T4.

ADR 0022 decides that a migration is a lot, that the lot is accepted on a sample
a *person* inspected, and that `disposition_actor` records the act while
`approval_authority` records who stands behind it. This is that decision under
test, and four of these can only be shown against a database.

**The halt is real and it refuses the whole lot.** `require_minimum_sample` is
E5's, inherited rather than redefined, and an import that proceeded on a short
sample would leave a number that still looks like a guarantee. The test that
matters most is that a halted lot leaves the queue exactly as it found it —
a partially accepted lot is the one state acceptance sampling has no vocabulary
for.

**A policy cannot clear its own floor.** `inspected_dispositions` excludes
automated disposals, so disposing more can never satisfy the requirement to
inspect more. Asserted by accepting one lot and then finding the floor no easier
for the next.

**The disposition is refused by the database if the vocabulary and the schema
disagree.** `ck_case_disposition` pins the set; migration 0088 widens it by
exactly one, and a service-only change would give a write that passes every
check in Python and 500s on the one path an operator reaches mid-migration.

**Nothing here writes canon.** `migrated_canonical` asks the promotion surface,
exactly as the three proposal dispositions do.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.service.memory.curation_cases import (
    DISPOSITION_BY_HUMAN,
    DISPOSITION_BY_POLICY,
    DISPOSITION_CONFIRM,
    DISPOSITION_MIGRATED_CANONICAL,
    DISPOSITIONS,
    TARGET_CANONICAL_FACT,
    CurationCaseService,
)
from contextplane.service.memory.migration_acceptance import (
    MigratedClaim,
    MigrationAcceptanceService,
)
from contextplane.service.memory.sampling_policy import (
    SampleTooSmall,
    SamplingPolicyService,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)
_EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_REASON = "Dependency claims are cheap to verify and expensive to get wrong."


class _World:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.tenant_id = uuid.uuid4()
        self.actor_id = uuid.uuid4()
        self.principal = uuid.uuid4()

    async def build(self) -> _World:
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"now": _NOW, "s": f"mc-{self.tenant_id.hex[:8]}", "t": self.tenant_id},
            )
            for actor, name in ((self.actor_id, "operator"), (self.principal, "sync-worker:cmdb")):
                await session.execute(
                    text(
                        "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                        "VALUES (:a, :t, :d, :sub, :now)"
                    ),
                    {
                        "a": actor,
                        "d": name,
                        "now": _NOW,
                        "sub": f"mc-{actor.hex[:8]}",
                        "t": self.tenant_id,
                    },
                )
        return self

    def ctx(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant_id, actor_id=self.actor_id, roles=["producer", "admin"])

    def policy_ctx(self) -> TenantContext:
        """The automation principal's own context.

        A separate context because `record_disposition` compares the caller
        against the case's owner, and routing to a principal the caller is not
        would be routing a case to somebody else and then deciding it.
        """
        return TenantContext(tenant_id=self.tenant_id, actor_id=self.principal, roles=["producer"])

    def cases(self) -> CurationCaseService:
        return CurationCaseService(self.factory)

    def sampling(self) -> SamplingPolicyService:
        return SamplingPolicyService(self.factory, clock=FakeClock(_NOW))

    def acceptance(self) -> MigrationAcceptanceService:
        return MigrationAcceptanceService(cases=self.cases(), sampling=self.sampling())

    async def set_floor(self, *, defect_tolerance: float) -> None:
        await self.sampling().set_policy(
            self.ctx(),
            claim_category="dependency",
            defect_tolerance=defect_tolerance,
            consumers_risk=0.10,
            reason=_REASON,
        )

    async def inspect(self, count: int) -> None:
        """`count` dispositions made by a person, which is the only kind that
        counts toward a floor."""
        cases = self.cases()
        for index in range(count):
            case = await cases.open_case(
                self.ctx(),
                subject_reference=f"system:seed/inspected-{index}",
                predicate="owned_by",
                now=_NOW,
            )
            await cases.route_case(self.ctx(), case_id=case.case_id, owner_id=str(self.actor_id), now=_NOW)
            await cases.record_disposition(
                self.ctx(),
                case_id=case.case_id,
                disposition=DISPOSITION_CONFIRM,
                now=_NOW,
                actor_kind=DISPOSITION_BY_HUMAN,
            )

    async def open_cases(self) -> int:
        async with self.factory() as session:
            return int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM curation_cases " "WHERE tenant_id = :t AND resolved_at IS NULL"),
                        {"t": self.tenant_id},
                    )
                ).scalar_one()
            )

    async def rows(self, disposition: str) -> list[tuple[str, str]]:
        async with self.factory() as session:
            return [
                (row[0], row[1])
                for row in (
                    await session.execute(
                        text(
                            "SELECT disposition_actor_kind, approval_authority FROM curation_cases "
                            "WHERE tenant_id = :t AND disposition = :d"
                        ),
                        {"d": disposition, "t": self.tenant_id},
                    )
                ).all()
            ]


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[_World]:
    engine = create_async_engine(pg_container, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield await _World(factory).build()
    finally:
        await engine.dispose()


def _lot(size: int) -> tuple[MigratedClaim, ...]:
    return tuple(MigratedClaim(subject_reference=f"system:cmdb/host-{i}", predicate="owned_by") for i in range(size))


@pytest.mark.asyncio
async def test_a_lot_whose_sample_a_person_inspected_is_accepted(world: _World) -> None:
    # 0.20 tolerance at 0.10 risk derives a floor of 11.
    await world.set_floor(defect_tolerance=0.20)
    await world.inspect(11)

    result = await world.acceptance().accept_lot(
        world.policy_ctx(),
        claim_category="dependency",
        claims=_lot(3),
        inspection_since=_EPOCH,
        principal=world.principal,
        now=_NOW,
    )

    assert result.disposed == 3
    assert result.inspected >= result.min_sample


@pytest.mark.asyncio
async def test_a_short_sample_refuses_the_whole_lot_and_writes_nothing(world: _World) -> None:
    """The property that makes the halt meaningful.

    A lot that halted halfway would leave some rows disposed and some not, which
    is a partially accepted lot — and acceptance sampling has no vocabulary for
    one. So the floor is checked before any case is touched, and this asserts the
    queue is exactly as it was.
    """
    await world.set_floor(defect_tolerance=0.05)  # floor of 45
    await world.inspect(2)
    before = await world.open_cases()

    with pytest.raises(SampleTooSmall) as raised:
        await world.acceptance().accept_lot(
            world.policy_ctx(),
            claim_category="dependency",
            claims=_lot(4),
            inspection_since=_EPOCH,
            principal=world.principal,
            now=_NOW,
        )

    assert "45" in str(raised.value)
    assert await world.open_cases() == before
    assert await world.rows(DISPOSITION_MIGRATED_CANONICAL) == []


@pytest.mark.asyncio
async def test_the_policy_cannot_clear_its_own_floor_by_disposing_more(world: _World) -> None:
    """The safety property the whole design rests on.

    `inspected_dispositions` excludes automated disposals, so accepting one lot
    must leave the floor exactly as hard for the next. A version that counted its
    own disposals would let a large enough migration authorise itself.
    """
    await world.set_floor(defect_tolerance=0.20)  # floor of 11
    await world.inspect(11)
    service = world.acceptance()

    await service.accept_lot(
        world.policy_ctx(),
        claim_category="dependency",
        claims=_lot(30),
        inspection_since=_EPOCH,
        principal=world.principal,
        now=_NOW,
    )
    after = await service.accept_lot(
        world.policy_ctx(),
        claim_category="dependency",
        claims=_lot(1),
        inspection_since=_EPOCH,
        principal=world.principal,
        now=_NOW,
    )

    # 30 automated disposals in between, and the count is unmoved.
    assert after.inspected == 11


@pytest.mark.asyncio
async def test_the_act_is_recorded_as_policy_and_the_authority_is_not_widened(
    world: _World,
) -> None:
    """E12's constraint, and ADR 0022's answer to it.

    `disposition_actor` says a batch job performed the act. `approval_authority`
    says who stands behind it, and it is `catalog_owner` — the authority that
    already owned the canonical graph. Conflating them is what would make a
    policy's write indistinguishable from an approver's.
    """
    await world.set_floor(defect_tolerance=0.20)
    await world.inspect(11)

    await world.acceptance().accept_lot(
        world.policy_ctx(),
        claim_category="dependency",
        claims=_lot(2),
        inspection_since=_EPOCH,
        principal=world.principal,
        now=_NOW,
    )

    rows = await world.rows(DISPOSITION_MIGRATED_CANONICAL)
    assert len(rows) == 2
    assert {actor for actor, _ in rows} == {DISPOSITION_BY_POLICY}
    assert {authority for _, authority in rows} == {"catalog_owner"}


@pytest.mark.asyncio
async def test_the_schema_accepts_the_seventh_disposition(world: _World) -> None:
    """Migration 0088 is why this passes.

    `ck_case_disposition` pins the vocabulary, so adding the value in Python
    alone would give a write that clears every service check and is refused by
    the database — a 500 on the one path an operator reaches mid-migration.
    """
    await world.set_floor(defect_tolerance=0.20)
    await world.inspect(11)

    await world.acceptance().accept_lot(
        world.policy_ctx(),
        claim_category="dependency",
        claims=_lot(1),
        inspection_since=_EPOCH,
        principal=world.principal,
        now=_NOW,
    )

    assert len(await world.rows(DISPOSITION_MIGRATED_CANONICAL)) == 1


def test_migrated_canonical_asks_the_promotion_surface_rather_than_writing_canon() -> None:
    """It carries a target kind, like the three proposal dispositions.

    A disposition that wrote canon from the curation table would be a second
    write path into the canonical graph, reachable from a queue whose whole
    purpose is deciding what to *ask* for.
    """
    policy = DISPOSITIONS[DISPOSITION_MIGRATED_CANONICAL]

    assert policy.target_kind == TARGET_CANONICAL_FACT
    assert policy.approval_authority == "catalog_owner"


def test_only_the_evidence_threshold_differs_from_promoting_a_canonical_fact() -> None:
    """ADR 0022's core claim, pinned.

    Scope, supersession and rollback are properties of the *target*, so a
    migration that answered them differently would be writing into a second
    canonical graph. What differs is the evidence: a statement about a lot rather
    than about one claim.
    """
    from contextplane.service.memory.curation_cases import DISPOSITION_PROPOSE_CANONICAL

    migrated = DISPOSITIONS[DISPOSITION_MIGRATED_CANONICAL]
    promoted = DISPOSITIONS[DISPOSITION_PROPOSE_CANONICAL]

    assert migrated.scope == promoted.scope
    assert migrated.supersession == promoted.supersession
    assert migrated.rollback == promoted.rollback
    assert migrated.evidence_threshold != promoted.evidence_threshold
    assert "lot" in migrated.evidence_threshold
