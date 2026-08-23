"""The sweep, against a real database: what it records, once, and when.

Every assertion here is about something only a database can answer — the
seeding read, the idempotence, and the constraint that backs it.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.service.memory.trust_transitions import TrustTransitionSweep, transitions_for
from tests.helpers.clock import FakeClock

_SCORED = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed_claim(
    factory: async_sessionmaker[AsyncSession],
    *,
    confidence: float,
    half_life_days: float,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A claim with a stored score, which is all the sweep reads."""
    tid, aid, cid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"tt-{tid.hex[:8]}", "n": _SCORED},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'a', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"s-{aid.hex[:8]}", "n": _SCORED},
        )
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                "VALUES (:e, :t, 'capability', :nm, 'tenant-shared', TRUE, :n)"
            ),
            {"e": eid, "t": tid, "nm": f"svc-{eid.hex[:8]}", "n": _SCORED},
        )
        await session.execute(
            text(
                "INSERT INTO memory_claims ("
                "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                "  confidence, confidence_scored_at, confidence_inputs, scorer_version,"
                "  calibration_version, decay_half_life_days, created_at"
                ") VALUES (:c, :t, :t, :a, :e, 'svc:x', 'owned_by_team', 'string', 'ownership_stewardship',"
                "          CAST('\"platform\"' AS JSONB), :n, 'staged', 'tenant-shared', 'owner_human', 10,"
                "          CAST(:conf AS NUMERIC), :n, CAST('{\"seed\": true}' AS JSONB), 'v1',"
                "          'uncalibrated', CAST(:hl AS NUMERIC), :n)"
            ),
            {"c": cid, "t": tid, "a": aid, "e": eid, "n": _SCORED, "conf": confidence, "hl": half_life_days},
        )
    return tid, cid


def _sweep(factory: async_sessionmaker[AsyncSession], *, now: datetime.datetime) -> TrustTransitionSweep:
    return TrustTransitionSweep(factory, clock=FakeClock(now))


@pytest.mark.asyncio
async def test_a_claim_that_has_not_decayed_records_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Examined and left alone. A sweep that recorded on every pass would turn a
    healthy store into a decay history.

    Asserted on this claim rather than on `report.recorded`: the sweep scans the
    whole store by design, and the container is shared, so any assertion on its
    totals is really an assertion about what else happens to be in the database.
    """
    _tid, cid = await _seed_claim(factory, confidence=0.90, half_life_days=270.0)

    await _sweep(factory, now=_SCORED).run_once()

    async with factory() as session:
        assert await transitions_for(session, claim_id=cid) == []


@pytest.mark.asyncio
async def test_a_claim_that_crossed_a_boundary_is_recorded_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The seeding read is what makes the first pass correct.

    With no prior transition the sweep compares against the bucket the *stored*
    score falls in — where the claim started, before any decay. Without that it
    would have nothing to compare against on the first pass and would either
    invent a transition or miss a real one.
    """
    _tid, cid = await _seed_claim(factory, confidence=0.90, half_life_days=30.0)

    # Decay runs toward `DECAY_FLOOR`, not toward zero:
    # `floor + (stored - floor) * 2**(-age/half_life)`. After one half-life a
    # 0.90 claim sits at 0.10 + 0.80/2 = 0.50, which is `moderate`.
    later = _SCORED + datetime.timedelta(days=30)
    report = await _sweep(factory, now=later).run_once()

    # `recorded` counts the whole pass and the container is shared, so the
    # assertion that matters is about this claim rather than the total.
    assert report.recorded >= 1
    async with factory() as session:
        history = await transitions_for(session, claim_id=cid)
    assert [(h["from_bucket"], h["to_bucket"]) for h in history] == [("confirmed", "moderate")]
    assert history[0]["observed_at"] == later


@pytest.mark.asyncio
async def test_running_the_sweep_twice_records_it_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The property the schedule depends on.

    The first pass moves the last-seen bucket to where the claim already is, so
    the second finds nothing. Without it, an aggressive interval would inflate
    every claim's decay history in proportion to how often the job ran.
    """
    _tid, cid = await _seed_claim(factory, confidence=0.90, half_life_days=30.0)
    later = _SCORED + datetime.timedelta(days=30)

    await _sweep(factory, now=later).run_once()
    await _sweep(factory, now=later + datetime.timedelta(hours=6)).run_once()

    # One row after two passes, which is the whole property. Counted on this
    # claim rather than on the reports, for the same reason as above.
    async with factory() as session:
        assert len(await transitions_for(session, claim_id=cid)) == 1


@pytest.mark.asyncio
async def test_a_second_fall_is_recorded_from_where_it_was_last_seen(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The chain is contiguous: each `from_bucket` is the previous `to_bucket`.

    A sweep comparing against the stored score every time would record
    `confirmed -> weak` on the second pass, losing that it had already been
    through `moderate` — and a reviewer reading the history would see one drop
    where there were two.
    """
    _tid, cid = await _seed_claim(factory, confidence=0.90, half_life_days=30.0)

    await _sweep(factory, now=_SCORED + datetime.timedelta(days=30)).run_once()
    await _sweep(factory, now=_SCORED + datetime.timedelta(days=75)).run_once()

    async with factory() as session:
        history = await transitions_for(session, claim_id=cid)
    steps = [(h["from_bucket"], h["to_bucket"]) for h in history]
    assert len(steps) == 2
    assert steps[0][1] == steps[1][0], "each fall starts where the last one ended"


@pytest.mark.asyncio
async def test_the_frozen_confidence_is_the_value_at_the_observation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """What a review asks afterwards: what was this worth when we let it go.

    Stored rather than recomputed, because the effective number keeps falling —
    reading it later would answer a different question than the one asked.
    """
    _tid, cid = await _seed_claim(factory, confidence=0.90, half_life_days=30.0)
    later = _SCORED + datetime.timedelta(days=30)
    await _sweep(factory, now=later).run_once()

    async with factory() as session:
        history = await transitions_for(session, claim_id=cid)
        # Much later, the effective value is far lower; the record does not move.
        await _sweep(factory, now=_SCORED + datetime.timedelta(days=365)).run_once()
        again = await transitions_for(session, claim_id=cid)

    assert history[0]["effective_confidence"] == pytest.approx(0.50, abs=0.01)
    assert again[0]["effective_confidence"] == history[0]["effective_confidence"]


@pytest.mark.asyncio
async def test_a_prose_claim_never_appears(factory: async_sessionmaker[AsyncSession]) -> None:
    """`NON_DECAYING_VALUE_TYPES` is `{"prose"}`, so this record is a history of
    trust *lost to time* rather than a complete history of trust."""
    tid, aid, cid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"tp-{tid.hex[:8]}", "n": _SCORED},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'a', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"p-{aid.hex[:8]}", "n": _SCORED},
        )
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, is_active, created_at) "
                "VALUES (:e, :t, 'capability', :nm, 'tenant-shared', TRUE, :n)"
            ),
            {"e": eid, "t": tid, "nm": f"svc-{eid.hex[:8]}", "n": _SCORED},
        )
        await session.execute(
            text(
                "INSERT INTO memory_claims ("
                "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                "  confidence, confidence_scored_at, confidence_inputs, scorer_version,"
                "  calibration_version, decay_half_life_days, created_at"
                ") VALUES (:c, :t, :t, :a, :e, 'svc:y', 'summary', 'prose', 'session_summary',"
                "          CAST('\"text\"' AS JSONB), :n, 'staged', 'tenant-shared', 'owner_human', 10,"
                "          CAST(0.90 AS NUMERIC), :n, CAST('{\"seed\": true}' AS JSONB), 'v1',"
                "          'uncalibrated', CAST(30 AS NUMERIC), :n)"
            ),
            {"c": cid, "t": tid, "a": aid, "e": eid, "n": _SCORED},
        )

    await _sweep(factory, now=_SCORED + datetime.timedelta(days=365)).run_once()

    async with factory() as session:
        assert await transitions_for(session, claim_id=cid) == []


@pytest.mark.asyncio
async def test_the_database_refuses_a_transition_that_did_not_move(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The idempotence is enforced, not hoped for.

    A bug that lost the comparison would write a decay history that never
    happened; this makes it fail loudly instead.
    """
    tid, cid = await _seed_claim(factory, confidence=0.90, half_life_days=270.0)
    with pytest.raises(Exception, match="ck_trust_transition_moved"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO claim_trust_transitions "
                    "  (transition_id, tenant_id, claim_id, from_bucket, to_bucket, "
                    "   effective_confidence, observed_at) "
                    "VALUES (:i, :t, :c, 'strong', 'strong', 0.7, :n)"
                ),
                {"i": uuid.uuid4(), "t": tid, "c": cid, "n": _SCORED},
            )


@pytest.mark.asyncio
async def test_a_claim_past_the_first_batch_is_still_examined(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The defect a two-worker run surfaced, pinned behaviourally.

    The sweep read `ORDER BY claim_id LIMIT 500` with no cursor, and nothing in
    its predicate excludes a claim it has already looked at — a claim with a
    transition recorded is still eligible for the next one. So it examined the
    same first page on every pass, forever, and **every claim beyond it decayed
    unobserved.** The test that caught it looked like flakiness because the
    failure depended on how many other claims the run happened to have seeded.

    `batch=1` over three claims is the smallest arrangement that tells the two
    apart: a sweep that stops at its first page records one, and a sweep that
    walks records all three.
    """
    seeded = []
    for _ in range(3):
        _tid, cid = await _seed_claim(factory, confidence=0.90, half_life_days=30.0)
        seeded.append(cid)

    later = _SCORED + datetime.timedelta(days=30)
    await TrustTransitionSweep(factory, clock=FakeClock(later), batch=1).run_once()

    async with factory() as session:
        for cid in seeded:
            assert await transitions_for(session, claim_id=cid), "a claim past the first batch must still be examined"
