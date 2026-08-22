"""Failure-pattern reports, against a real database.

Two properties carry this module and neither is visible from a count.

`test_a_group_states_how_often_it_fails_and_not_only_how_often_it_appears` is the
one that matters most: reporting `incorrect_count` alone conflates a predicate
the agent uses constantly and mostly gets right — which dominates any failure
list by volume — with one it touches rarely and always gets wrong. The second is
the one worth changing an instruction over.

`test_the_report_is_stored_because_an_instruction_must_cite_one` is the other:
`agent_instruction`'s CHECK refuses to activate a version that does not name a
stored `report_id`, so a report nobody persisted is a report no instruction can
be justified by.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ValidationError
from contextplane.service.memory.agent_failure_patterns import AgentFailurePatternService
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_SEEDED = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_JUDGED = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_NOW = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)
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
                {"t": self.tenant_id, "s": f"fp-{self.tenant_id.hex[:8]}", "now": _SEEDED},
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

    async def judged(
        self,
        *,
        verdict: str,
        predicate: str = "owned_by_team",
        category: str = "ownership_stewardship",
        value: str = "platform",
        note: str | None = None,
    ) -> uuid.UUID:
        cid, eid = uuid.uuid4(), uuid.uuid4()
        async with self.factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                    "VALUES (:e, :t, 'capability', :n, 'tenant-shared', TRUE, :now)"
                ),
                {"e": eid, "t": self.tenant_id, "n": f"cap-{eid.hex[:8]}", "now": _SEEDED},
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
                    "  :cid, :t, :t, :a, :e, 'ref', :pred, 'prose',"
                    "  :cat, CAST(:val AS JSONB), :now, 'staged', 'private',"
                    "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST('{}' AS JSONB),"
                    "  'scorer.v1', 'calib.v1', 30)"
                ),
                {
                    "cid": cid,
                    "t": self.tenant_id,
                    "a": self.agent_id,
                    "e": eid,
                    "pred": predicate,
                    "cat": category,
                    "val": f'"{value}"',
                    "now": _SEEDED,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claim_adjudication ("
                    "  tenant_id, claim_id, adjudicated_by, verdict, observed_confidence,"
                    "  observed_bucket, calibration_version, source_authority, note, adjudicated_at"
                    ") VALUES (:t, :cid, :by, :v, 0.700, '0.7', 'calib.v1', 'observer_extraction', :note, :at)"
                ),
                {"t": self.tenant_id, "cid": cid, "by": self.reviewer_id, "v": verdict, "note": note, "at": _JUDGED},
            )
        return cid

    def ctx(self) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id, actor_id=self.reviewer_id, roles=["admin"], oidc_subject="reviewer"
        )

    def service(self) -> AgentFailurePatternService:
        return AgentFailurePatternService(self.factory, clock=FakeClock(_NOW))

    async def report(self, **kw: object):
        return await self.service().build_report(
            self.ctx(),
            author_actor_id=self.agent_id,
            window_start=_WINDOW[0],
            window_end=_WINDOW[1],
            **kw,  # type: ignore[arg-type]
        )


@pytest_asyncio.fixture
async def world(factory: async_sessionmaker[AsyncSession]) -> _World:
    return await _World(factory).build()


@pytest.mark.asyncio
async def test_a_group_states_how_often_it_fails_and_not_only_how_often_it_appears(world: _World) -> None:
    """The distinction that makes a report actionable.

    `owned_by_team` fails twice out of twelve — the agent uses it constantly and
    mostly gets it right. `depends_on` fails twice out of two. By raw failure
    count they are identical and `owned_by_team` sorts first; by rate they are
    not remotely the same problem, and only one of them is worth rewriting an
    instruction over.
    """
    for _ in range(2):
        await world.judged(verdict="incorrect", predicate="owned_by_team")
    for _ in range(10):
        await world.judged(verdict="correct", predicate="owned_by_team")
    for _ in range(2):
        await world.judged(verdict="incorrect", predicate="depends_on")

    report = await world.report()
    by_predicate = {group.predicate: group for group in report.groups}

    assert by_predicate["owned_by_team"].incorrect_count == 2
    assert by_predicate["owned_by_team"].total_count == 12
    assert by_predicate["owned_by_team"].rate == pytest.approx(2 / 12)

    assert by_predicate["depends_on"].incorrect_count == 2
    assert by_predicate["depends_on"].total_count == 2
    assert by_predicate["depends_on"].rate == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_the_report_is_stored_because_an_instruction_must_cite_one(world: _World) -> None:
    """`agent_instruction` refuses to activate without a stored `report_id`, so
    a report that existed only in a response could justify nothing."""
    await world.judged(verdict="incorrect")

    report = await world.report()

    async with world.factory() as session:
        stored = (
            await session.execute(
                text(
                    "SELECT author_actor_id, n_adjudicated, n_incorrect, groups, generated_at "
                    "  FROM agent_failure_pattern_report WHERE report_id = :r"
                ),
                {"r": report.report_id},
            )
        ).one()

    assert stored.author_actor_id == world.agent_id
    assert stored.n_adjudicated == 1
    assert stored.n_incorrect == 1
    assert stored.generated_at == _NOW
    assert stored.groups[0]["predicate"] == "owned_by_team"
    assert stored.groups[0]["rate"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_a_group_carries_the_values_and_the_reviewers_own_words(world: _World) -> None:
    """A number nobody can check is a lead nobody follows."""
    await world.judged(verdict="incorrect", value="wrong-team", note="the owner moved last quarter")

    report = await world.report()
    example = report.groups[0].examples[0]

    assert example.value == "wrong-team"
    assert example.note == "the owner moved last quarter"


@pytest.mark.asyncio
async def test_examples_are_capped_per_group_rather_than_per_report(world: _World) -> None:
    """A lateral join, so one noisy group cannot starve another of evidence."""
    for _ in range(4):
        await world.judged(verdict="incorrect", predicate="owned_by_team")
    for _ in range(4):
        await world.judged(verdict="incorrect", predicate="depends_on")

    report = await world.report(examples_per_group=2)

    assert len(report.groups) == 2
    for group in report.groups:
        assert len(group.examples) == 2, "the cap applied to the report rather than to each group"


@pytest.mark.asyncio
async def test_a_group_the_agent_never_got_wrong_is_absent(world: _World) -> None:
    """A failure report listing everything the agent touched is a list of its
    work, not of its failures."""
    await world.judged(verdict="correct", predicate="depends_on")
    await world.judged(verdict="incorrect", predicate="owned_by_team")

    report = await world.report()

    assert [group.predicate for group in report.groups] == ["owned_by_team"]


@pytest.mark.asyncio
async def test_a_clean_window_still_produces_a_stored_report(world: _World) -> None:
    """ "Nothing went wrong" is evidence too, and it is the baseline a later
    report is compared against — which is E20's whole premise."""
    await world.judged(verdict="correct")

    report = await world.report()

    assert report.groups == ()
    assert report.n_adjudicated == 1
    assert report.n_incorrect == 0

    async with world.factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM agent_failure_pattern_report WHERE report_id = :r"),
                {"r": report.report_id},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_the_report_carries_the_autonomy_dimension_too(world: _World) -> None:
    """Both questions in one artefact: what did it get wrong, and how often did
    it need help. An accurate agent that needs constant steering and an
    autonomous one that is often wrong need different instruction changes."""
    await world.judged(verdict="incorrect")
    async with world.factory() as session, session.begin():
        for seq, kind in enumerate(("user_message", "agent_action", "user_message"), start=1):
            await session.execute(
                text(
                    "INSERT INTO memory_session_events "
                    "  (tenant_id, actor_id, session_id, seq, kind, body, created_at, expires_at, size_bytes) "
                    "VALUES (:t, :a, 's-1', :q, :k, 'b', :now, :exp, 8)"
                ),
                {
                    "t": world.tenant_id,
                    "a": world.agent_id,
                    "q": seq,
                    "k": kind,
                    "now": _JUDGED,
                    "exp": _JUDGED + datetime.timedelta(days=30),
                },
            )

    report = await world.report()

    assert report.n_sessions == 1
    assert report.n_intervention_sessions == 1


@pytest.mark.asyncio
async def test_a_group_with_no_evidence_is_refused(world: _World) -> None:
    await world.judged(verdict="incorrect")

    with pytest.raises(ValidationError, match="examples_per_group"):
        await world.report(examples_per_group=0)
