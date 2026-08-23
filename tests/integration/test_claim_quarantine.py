"""Quarantine withholds, revert restores, and neither can be talked out of it.

E4-T2, against a real database because every property here is a property of the
serving SQL rather than of the service that writes the column.

**The test that decides whether this design was worth it** is
`test_a_quarantined_claim_is_withheld_from_a_read_asking_about_an_earlier_instant`.
The rejected alternative was to express quarantine as a `t_invalidated_at`
write, which is the bitemporal idiom this schema uses everywhere else. It is
defeated by a query parameter: `as_of` is caller-supplied on both transports,
and the `t_invalidated_at` term is `as_of`-relative on purpose. That test is the
one that fails if somebody later "simplifies" the column away.

**What is deliberately not asserted here.** That the vectors leave the index.
The propagation enqueue is a recall concern and is asynchronous by design --
correctness commits with the column write, in the same transaction. Asserting
the index state here would either need the drain to run (making this a test of
the drain) or would encode a race. `test_embedding_derivative_erasure.py` owns
the index-follows-servability property.
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
from contextplane.service.memory.claim_serving import ClaimQuery, ClaimServingService
from contextplane.service.memory.quarantine import (
    SELECTOR_CONNECTOR_RUN,
    SELECTOR_NAMESPACE_PREFIX,
    SELECTOR_STRATEGY,
    QuarantineService,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_SEEDED = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_QUARANTINED = datetime.datetime(2026, 8, 20, 12, 0, tzinfo=datetime.UTC)
_LATER = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _World:
    """One tenant with claims planted under a known provenance."""

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
                {"t": self.tenant_id, "s": f"q-{self.tenant_id.hex[:8]}", "now": _SEEDED},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'op', :sub, :now)"
                ),
                {"a": self.actor_id, "t": self.tenant_id, "sub": f"q-{self.actor_id.hex[:8]}", "now": _SEEDED},
            )
        return self

    async def claim(self, *, strategy: str, namespace: str, run: str | None = None) -> uuid.UUID:
        """A servable claim: staged, consolidated, not invalidated, not withheld."""
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
                    "  scorer_version, calibration_version, decay_half_life_days, namespace, strategy_id"
                    ") VALUES ("
                    "  :cid, :t, :t, :a, :e, 'ref', 'owned_by_team', 'prose',"
                    "  'ownership_stewardship', CAST('\"platform\"' AS JSONB), :now, 'staged', 'private',"
                    "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST('{}' AS JSONB),"
                    "  'scorer.v1', 'calib.v1', 30, :ns, :strat)"
                ),
                {
                    "cid": cid,
                    "t": self.tenant_id,
                    "a": self.actor_id,
                    "e": eid,
                    "now": _SEEDED,
                    "ns": namespace,
                    "strat": strategy,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) "
                    "VALUES (:cid, :kind, :ref)"
                ),
                {"cid": cid, "kind": "connector_run" if run else "curator", "ref": run or f"seed:{cid}"},
            )
        return cid

    def ctx(self, *, roles: list[str] | None = None) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            roles=roles if roles is not None else ["producer"],
            oidc_subject="op",
        )

    def quarantine(self) -> QuarantineService:
        return QuarantineService(self.factory, clock=FakeClock(_QUARANTINED))

    def serving(self) -> ClaimServingService:
        return ClaimServingService(self.factory, clock=FakeClock(_LATER))


@pytest_asyncio.fixture
async def world(factory: async_sessionmaker[AsyncSession]) -> _World:
    return await _World(factory).build()


async def _served(world: _World, *, as_of: datetime.datetime | None = None) -> set[uuid.UUID]:
    served = await world.serving().query(world.ctx(), ClaimQuery(as_of=as_of or _LATER))
    return {claim.claim_id for claim in served}


@pytest.mark.asyncio
async def test_quarantining_a_connector_run_withholds_only_that_runs_claims(world: _World) -> None:
    bad = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    good = await world.claim(strategy="extract.v1", namespace="team/a", run="run-43")

    assert await _served(world) == {bad, good}

    applied = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="connector emitted nonsense"
    )

    assert applied.matched == (bad,)
    assert await _served(world) == {good}


@pytest.mark.asyncio
async def test_a_quarantined_claim_is_withheld_from_a_read_asking_about_an_earlier_instant(world: _World) -> None:
    """The test that decides whether the dedicated column was worth adding.

    A `t_invalidated_at` write would pass every other test in this file and fail
    this one, because that term is compared against `as_of` -- deliberately, so
    a historical read still sees what was believed then. `as_of` is caller
    supplied on both transports, so under that design an agent asking about
    yesterday is served the content an operator withheld today.

    `quarantined_at` is read unconditionally. There is no instant at which a
    withheld claim comes back.
    """
    cid = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    await world.quarantine().apply(world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad run")

    before_the_quarantine = _QUARANTINED - datetime.timedelta(days=5)
    assert before_the_quarantine > _SEEDED, "the claim must already exist at the instant being asked about"

    assert await _served(world, as_of=before_the_quarantine) == set(), (
        f"claim {cid} came back when asked about {before_the_quarantine.isoformat()}, which is before it was "
        "withheld. The quarantine is as_of-relative, which means a caller can read around it."
    )


@pytest.mark.asyncio
async def test_reverting_restores_exactly_what_was_withheld(world: _World) -> None:
    """Restored from the recorded membership, not by re-running the predicate.

    A claim written *after* the quarantine and matching the same predicate is
    the case that separates the two designs: it was never withheld, so revert
    must not claim to restore it.
    """
    withheld = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    applied = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad run"
    )
    arrived_later = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")

    assert await _served(world) == {arrived_later}

    restored = await QuarantineService(world.factory, clock=FakeClock(_LATER)).revert(
        world.ctx(), quarantine_id=applied.quarantine_id
    )

    assert restored == 1
    assert await _served(world) == {withheld, arrived_later}


@pytest.mark.asyncio
async def test_reverting_one_of_two_overlapping_quarantines_leaves_the_claim_withheld(world: _World) -> None:
    """Otherwise reverting the older quietly undoes the newer.

    Both predicates reach the same claim. Reverting the first must not restore
    it while the second is still in force -- an operator undoing yesterday's
    incident would otherwise republish content today's incident is withholding.
    """
    cid = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    first = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="by run"
    )
    second = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_STRATEGY, value="extract.v1", reason="by extractor"
    )
    assert cid in first.matched and cid in second.matched

    later = QuarantineService(world.factory, clock=FakeClock(_LATER))
    assert await later.revert(world.ctx(), quarantine_id=first.quarantine_id) == 0
    assert await _served(world) == set()

    assert await later.revert(world.ctx(), quarantine_id=second.quarantine_id) == 1
    assert await _served(world) == {cid}


@pytest.mark.asyncio
async def test_a_namespace_prefix_reaches_the_namespaces_under_it(world: _World) -> None:
    under = await world.claim(strategy="extract.v1", namespace="team/platform/api")
    sibling = await world.claim(strategy="extract.v1", namespace="team/payments")

    applied = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_NAMESPACE_PREFIX, value="team/platform", reason="namespace retired"
    )

    assert applied.matched == (under,)
    assert await _served(world) == {sibling}


@pytest.mark.asyncio
async def test_the_ledger_records_who_withheld_what_and_why(world: _World) -> None:
    """A boolean on the claim would answer none of these questions."""
    await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    applied = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="emitted nonsense after a bad deploy"
    )

    async with world.factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT predicate, matched_count, reason, applied_by, applied_at, reverted_at "
                    "  FROM claim_quarantines WHERE quarantine_id = :qid"
                ),
                {"qid": applied.quarantine_id},
            )
        ).one()

    assert row.predicate == {"selector": SELECTOR_CONNECTOR_RUN, "value": "run-42"}
    assert row.matched_count == 1
    assert row.reason == "emitted nonsense after a bad deploy"
    assert row.applied_by == world.actor_id
    assert row.applied_at == _QUARANTINED
    assert row.reverted_at is None


@pytest.mark.asyncio
async def test_a_reverted_quarantine_keeps_its_row(world: _World) -> None:
    """Revert is a status flip, not a delete — the position this schema already
    takes for grants and for envelope suspension. The fact that content was
    withheld for a period is what an incident review is asking about."""
    await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    applied = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad run"
    )
    await QuarantineService(world.factory, clock=FakeClock(_LATER)).revert(
        world.ctx(), quarantine_id=applied.quarantine_id
    )

    async with world.factory() as session:
        row = (
            await session.execute(
                text("SELECT reverted_at, reverted_by, matched_count FROM claim_quarantines WHERE quarantine_id = :q"),
                {"q": applied.quarantine_id},
            )
        ).one()
        members = (
            await session.execute(
                text("SELECT count(*) FROM claim_quarantine_members WHERE quarantine_id = :q"),
                {"q": applied.quarantine_id},
            )
        ).scalar_one()

    assert row.reverted_at == _LATER
    assert row.reverted_by == world.actor_id
    assert row.matched_count == 1
    assert members == 1, "the membership is the record of what was withheld and must outlive the withholding"


@pytest.mark.asyncio
async def test_a_predicate_matching_nothing_is_refused_rather_than_recorded(world: _World) -> None:
    """A quarantine that withheld nothing reads, later, as one that was tried
    and worked. Refusing makes the operator look at their predicate."""
    await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")

    with pytest.raises(ConflictError, match="matches no claim"):
        await world.quarantine().apply(world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-nope", reason="typo")


@pytest.mark.asyncio
async def test_reverting_twice_is_refused(world: _World) -> None:
    await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    applied = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad run"
    )
    later = QuarantineService(world.factory, clock=FakeClock(_LATER))
    await later.revert(world.ctx(), quarantine_id=applied.quarantine_id)

    with pytest.raises(ConflictError, match="already reverted"):
        await later.revert(world.ctx(), quarantine_id=applied.quarantine_id)


@pytest.mark.asyncio
async def test_another_tenants_quarantine_is_not_found_rather_than_forbidden(world: _World) -> None:
    """Same reasoning the task surfaces use: distinguishing "not yours" from
    "no such thing" is an existence oracle over every quarantine in the
    deployment."""
    await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    applied = await world.quarantine().apply(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="bad run"
    )

    stranger = await _World(world.factory).build()
    with pytest.raises(NotFoundError):
        await stranger.quarantine().revert(stranger.ctx(), quarantine_id=applied.quarantine_id)


@pytest.mark.asyncio
async def test_a_consumer_cannot_quarantine(world: _World) -> None:
    await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    with pytest.raises(PermissionError):
        await world.quarantine().apply(
            world.ctx(roles=["consumer"]), selector=SELECTOR_CONNECTOR_RUN, value="run-42", reason="nope"
        )


@pytest.mark.asyncio
async def test_an_unknown_selector_is_refused(world: _World) -> None:
    with pytest.raises(ValidationError, match="unknown quarantine selector"):
        await world.quarantine().apply(world.ctx(), selector="confidence", value="0.5", reason="nope")


@pytest.mark.asyncio
async def test_the_preview_reaches_what_the_apply_would_without_withholding_anything(world: _World) -> None:
    cid = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")

    previewed = await world.quarantine().preview(world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42")

    assert previewed.matched == (cid,)
    assert await _served(world) == {cid}, "a preview must not withhold anything"


# --- E4-T3: the preview says what depends on what it would withhold -----------
#
# Two sets that mean different things. `matched` is exact and is what would be
# withheld; `downstream` is advisory and is withheld by nothing. The traversal
# behind it is the one that already exists, called once per seed -- not a
# widened one and not a second one, which is the failure this task named.


class _RecordingBlastRadius:
    """The closure traversal, stubbed, recording exactly how it was called.

    A stub rather than a real graph because what is under test is the *seeding*:
    which roots the preview picks, how many, in which direction and to what
    depth. Building an edge graph would test the traversal instead, which has
    its own tests and is deliberately not re-proved here.
    """

    def __init__(self, reach: dict[uuid.UUID, list[uuid.UUID]]) -> None:
        self._reach = reach
        self.calls: list[tuple[uuid.UUID, str, int]] = []

    async def get_blast_radius(
        self, ctx: TenantContext, entity_id: uuid.UUID, direction: str = "reverse", depth: int = 5
    ) -> object:
        del ctx
        self.calls.append((entity_id, direction, depth))
        nodes = [_Node(reached) for reached in self._reach.get(entity_id, [])]
        return _Traversal(nodes)


class _Node:
    def __init__(self, entity_id: uuid.UUID) -> None:
        self.entity_id = entity_id


class _Traversal:
    def __init__(self, nodes: list[_Node]) -> None:
        self.nodes = nodes


async def _subject_of(world: _World, claim_id: uuid.UUID) -> uuid.UUID:
    async with world.factory() as session:
        row = await session.execute(
            text("SELECT subject_entity_id FROM memory_claims WHERE claim_id = :c"), {"c": claim_id}
        )
        return uuid.UUID(str(row.scalar_one()))


@pytest.mark.asyncio
async def test_the_preview_reports_what_depends_on_the_claims_it_would_withhold(world: _World) -> None:
    first = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    second = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    subjects = sorted({await _subject_of(world, first), await _subject_of(world, second)})
    dependant = uuid.uuid4()
    radius = _RecordingBlastRadius({subjects[0]: [dependant], subjects[1]: [dependant]})

    previewed = await QuarantineService(world.factory, clock=FakeClock(_QUARANTINED), blast_radius=radius).preview(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42"
    )

    assert previewed.matched == tuple(sorted((first, second)))
    assert previewed.subjects == tuple(subjects)
    assert previewed.downstream == (dependant,), "one dependant reached twice is one dependant"
    assert not previewed.truncated


@pytest.mark.asyncio
async def test_the_preview_seeds_the_existing_traversal_once_per_subject(world: _World) -> None:
    """Reverse, at the traversal's own depth cap. A claim four hops downstream
    rests on the withheld content exactly as much as one hop does."""
    claim_id = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    subject = await _subject_of(world, claim_id)
    radius = _RecordingBlastRadius({})

    await QuarantineService(world.factory, clock=FakeClock(_QUARANTINED), blast_radius=radius).preview(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42"
    )

    assert radius.calls == [(subject, "reverse", 5)]


@pytest.mark.asyncio
async def test_a_subject_is_not_reported_as_downstream_of_itself(world: _World) -> None:
    """It is already in `matched`'s subjects. Counting it twice would inflate
    the advisory set with the exact set beside it."""
    claim_id = await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")
    subject = await _subject_of(world, claim_id)
    radius = _RecordingBlastRadius({subject: [subject]})

    previewed = await QuarantineService(world.factory, clock=FakeClock(_QUARANTINED), blast_radius=radius).preview(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42"
    )

    assert previewed.downstream == ()


@pytest.mark.asyncio
async def test_a_preview_with_no_traversal_wired_says_its_answer_is_incomplete(world: _World) -> None:
    """Rather than reporting an empty downstream set as though it were the
    answer. `truncated` is what tells the two apart."""
    await world.claim(strategy="extract.v1", namespace="team/a", run="run-42")

    previewed = await world.quarantine().preview(world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-42")

    assert previewed.downstream == ()
    assert previewed.seeds_total == 1
    assert previewed.seeds_traversed == 0
    assert previewed.truncated, "an untraversed subject is not a subject with no dependants"


@pytest.mark.asyncio
async def test_a_predicate_matching_nothing_previews_an_untruncated_empty_answer(world: _World) -> None:
    radius = _RecordingBlastRadius({})

    previewed = await QuarantineService(world.factory, clock=FakeClock(_QUARANTINED), blast_radius=radius).preview(
        world.ctx(), selector=SELECTOR_CONNECTOR_RUN, value="run-nothing"
    )

    assert previewed.matched == ()
    assert previewed.seeds_total == 0
    assert not previewed.truncated, "nothing to traverse is a complete answer, not a capped one"
    assert radius.calls == []
