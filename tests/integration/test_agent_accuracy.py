"""Per-author accuracy against a real database.

The unit tests cover the arithmetic. Everything here is about the query, and in
particular about the one decision that is invisible in a passing test unless it
is written on purpose: **which tenant column scopes the read.**

`author_tenant_id` is the tenant that ran the agent. `owning_tenant_id` is the
tenant that owns the claim's subject. Both are populated, both are plausible,
and swapping them answers a different question -- "how accurate were claims
about our capabilities, whoever wrote them" instead of "how accurate is our
agent". `test_accuracy_is_scoped_to_the_tenant_that_ran_the_agent` is the one
that fails if they are swapped; nothing else here would notice.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.service.memory.agent_accuracy import (
    BREAKDOWN_CATEGORY,
    BREAKDOWN_PREDICATE,
    AgentAccuracyService,
)
from contextplane.types import TenantContext

_SEEDED = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_JUDGED = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_WINDOW = (datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC), datetime.datetime(2026, 8, 22, tzinfo=datetime.UTC))


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _World:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.tenant_id = uuid.uuid4()
        self.agent_id = uuid.uuid4()
        self.reviewer_id = uuid.uuid4()

    async def build(self) -> _World:
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"t": self.tenant_id, "s": f"aa-{self.tenant_id.hex[:8]}", "now": _SEEDED},
            )
            for aid, name in ((self.agent_id, "agent"), (self.reviewer_id, "reviewer")):
                await session.execute(
                    text(
                        "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                        "VALUES (:a, :t, :n, :sub, :now)"
                    ),
                    {"a": aid, "t": self.tenant_id, "n": name, "sub": f"{name}-{aid.hex[:8]}", "now": _SEEDED},
                )
        return self

    async def judged_claim(
        self,
        *,
        verdict: str,
        author: uuid.UUID | None = None,
        author_tenant: uuid.UUID | None = None,
        owning_tenant: uuid.UUID | None = None,
        category: str = "ownership_stewardship",
        predicate: str = "owned_by_team",
        adjudicated_at: datetime.datetime | None = None,
    ) -> uuid.UUID:
        """One claim with one verdict on it."""
        cid, eid = uuid.uuid4(), uuid.uuid4()
        owner = owning_tenant or self.tenant_id
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                    "VALUES (:e, :t, 'capability', :n, 'tenant-shared', TRUE, :now)"
                ),
                {"e": eid, "t": owner, "n": f"cap-{eid.hex[:8]}", "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claims ("
                    "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                    "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                    "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                    "  consolidated_at, created_at, confidence, confidence_scored_at, confidence_inputs,"
                    "  scorer_version, calibration_version, decay_half_life_days"
                    ") VALUES ("
                    "  :cid, :owner, :author_t, :author, :e, 'ref', :pred, 'prose',"
                    "  :cat, CAST('\"v\"' AS JSONB), :now, 'staged', 'private',"
                    "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST('{}' AS JSONB),"
                    "  'scorer.v1', 'calib.v1', 30)"
                ),
                {
                    "cid": cid,
                    "owner": owner,
                    "author_t": author_tenant or self.tenant_id,
                    "author": author or self.agent_id,
                    "e": eid,
                    "pred": predicate,
                    "cat": category,
                    "now": _SEEDED,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claim_adjudication ("
                    "  tenant_id, claim_id, adjudicated_by, verdict, observed_confidence,"
                    "  observed_bucket, calibration_version, source_authority, adjudicated_at"
                    ") VALUES (:t, :cid, :by, :v, 0.700, '0.7', 'calib.v1', 'observer_extraction', :at)"
                ),
                {
                    "t": self.tenant_id,
                    "cid": cid,
                    "by": self.reviewer_id,
                    "v": verdict,
                    "at": adjudicated_at or _JUDGED,
                },
            )
        return cid

    def ctx(self) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id, actor_id=self.reviewer_id, roles=["admin"], oidc_subject="reviewer"
        )

    def service(self) -> AgentAccuracyService:
        return AgentAccuracyService(self.factory)


@pytest_asyncio.fixture
async def world(factory: async_sessionmaker[AsyncSession]) -> _World:
    return await _World(factory).build()


@pytest.mark.asyncio
async def test_accuracy_counts_correct_over_decided(world: _World) -> None:
    for verdict in ("correct", "correct", "correct", "incorrect"):
        await world.judged_claim(verdict=verdict)

    accuracy = await world.service().accuracy_for(
        world.ctx(), author_actor_id=world.agent_id, window_start=_WINDOW[0], window_end=_WINDOW[1]
    )

    assert accuracy.overall.n_correct == 3
    assert accuracy.overall.n_incorrect == 1
    assert accuracy.overall.rate == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_an_undecidable_verdict_is_counted_but_not_divided_by(world: _World) -> None:
    await world.judged_claim(verdict="correct")
    await world.judged_claim(verdict="undecidable")
    await world.judged_claim(verdict="undecidable")

    overall = (
        await world.service().accuracy_for(
            world.ctx(), author_actor_id=world.agent_id, window_start=_WINDOW[0], window_end=_WINDOW[1]
        )
    ).overall

    assert overall.n_adjudicated == 3
    assert overall.n_decided == 1
    assert overall.rate == pytest.approx(1.0), "the undecidables must not drag the rate down"


@pytest.mark.asyncio
async def test_accuracy_is_scoped_to_the_tenant_that_ran_the_agent(world: _World) -> None:
    """The one test that fails if `owning_tenant_id` is used instead.

    Two claims by the same agent, in the same window, judged the same way. One
    is about this tenant's own subject; the other is about a subject another
    tenant owns. Both were written *by this tenant's agent*, so both belong in
    its accuracy figure -- and a read scoped by the subject's owner would drop
    the second and report a different number without failing anything else.
    """
    stranger = await _World(world.factory).build()

    await world.judged_claim(verdict="correct")
    await world.judged_claim(verdict="incorrect", owning_tenant=stranger.tenant_id)

    overall = (
        await world.service().accuracy_for(
            world.ctx(), author_actor_id=world.agent_id, window_start=_WINDOW[0], window_end=_WINDOW[1]
        )
    ).overall

    assert overall.n_adjudicated == 2, (
        "a claim this tenant's agent wrote about another tenant's subject was dropped; the read is "
        "scoped by owning_tenant_id, which answers a different question"
    )
    assert overall.rate == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_another_tenants_agent_does_not_appear(world: _World) -> None:
    """The scoping still holds in the direction that matters for isolation."""
    stranger = await _World(world.factory).build()
    await stranger.judged_claim(verdict="incorrect")
    await world.judged_claim(verdict="correct")

    overall = (
        await world.service().accuracy_for(
            world.ctx(), author_actor_id=world.agent_id, window_start=_WINDOW[0], window_end=_WINDOW[1]
        )
    ).overall

    assert overall.n_adjudicated == 1
    assert overall.rate == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_the_window_is_half_open_on_the_adjudication_instant(world: _World) -> None:
    """Bounded by when the verdict was reached, not when the claim was written.

    A claim written last year and reviewed today belongs in today's window: the
    figure is about review outcomes, and reviewing old work is still work.
    """
    await world.judged_claim(verdict="correct", adjudicated_at=_WINDOW[0])
    await world.judged_claim(verdict="incorrect", adjudicated_at=_WINDOW[1])

    overall = (
        await world.service().accuracy_for(
            world.ctx(), author_actor_id=world.agent_id, window_start=_WINDOW[0], window_end=_WINDOW[1]
        )
    ).overall

    assert overall.n_adjudicated == 1, "the start is inclusive and the end exclusive"
    assert overall.n_correct == 1


@pytest.mark.asyncio
async def test_a_breakdown_groups_and_still_sums_to_the_header(world: _World) -> None:
    await world.judged_claim(verdict="correct", category="ownership_stewardship")
    await world.judged_claim(verdict="incorrect", category="ownership_stewardship")
    await world.judged_claim(verdict="correct", category="operational_lifecycle")

    accuracy = await world.service().accuracy_for(
        world.ctx(),
        author_actor_id=world.agent_id,
        window_start=_WINDOW[0],
        window_end=_WINDOW[1],
        breakdown=BREAKDOWN_CATEGORY,
    )

    by_label = {group.label: group for group in accuracy.groups}
    assert set(by_label) == {"ownership_stewardship", "operational_lifecycle"}
    assert by_label["ownership_stewardship"].rate == pytest.approx(0.5)
    assert by_label["operational_lifecycle"].rate == pytest.approx(1.0)
    assert accuracy.overall.n_adjudicated == 3


@pytest.mark.asyncio
async def test_a_predicate_breakdown_is_the_grain_a_prompt_fix_needs(world: _World) -> None:
    """Category says which area an agent is weak in; predicate says which claim
    it keeps getting wrong, which is what an instruction change can act on."""
    await world.judged_claim(verdict="incorrect", predicate="owned_by_team")
    await world.judged_claim(verdict="incorrect", predicate="owned_by_team")
    await world.judged_claim(verdict="correct", predicate="depends_on")

    accuracy = await world.service().accuracy_for(
        world.ctx(),
        author_actor_id=world.agent_id,
        window_start=_WINDOW[0],
        window_end=_WINDOW[1],
        breakdown=BREAKDOWN_PREDICATE,
    )

    by_label = {group.label: group for group in accuracy.groups}
    assert by_label["owned_by_team"].rate == 0.0
    assert by_label["depends_on"].rate == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_an_agent_nobody_reviewed_reports_no_groups_rather_than_zero(world: _World) -> None:
    """ "Never reviewed" and "reviewed and always wrong" are different facts, and
    a caller acts on them differently."""
    accuracy = await world.service().accuracy_for(
        world.ctx(), author_actor_id=uuid.uuid4(), window_start=_WINDOW[0], window_end=_WINDOW[1]
    )

    assert accuracy.groups == ()
    assert accuracy.overall.rate is None
