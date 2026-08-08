"""Erasing an actor from usage, against a real database.

The headline test is the one asserting what erasure does *not* do: every rollup
value is byte-identical before and after. That is the property the whole retention
design rests on — an aggregate with no actor identifier is not personal data, so
erasing a person must not be able to change a number that has already been
reported for a closed month.

The rest is the ordinary erasure contract: tenant-scoped, idempotent, and complete.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.types import TenantContext
from contextplane.usage.erasure import UsageErasure
from contextplane.usage.rollups import roll_up_day
from contextplane.usage.writer import UsageEvent, UsageWriter

_DAY = datetime.date(2026, 5, 14)
_AT = datetime.datetime.combine(_DAY, datetime.time(9, 0), tzinfo=datetime.UTC)


@pytest.fixture
async def factory(pg_container: str):
    engine = create_async_engine(pg_container)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=frozenset({"admin"}))


async def _insert(factory: async_sessionmaker[AsyncSession], rows: list[dict]) -> None:
    async with factory() as session:
        for row in rows:
            await session.execute(
                text(
                    "INSERT INTO usage_events (event_id, occurred_at, tenant_id, actor_id, surface,"
                    " operation, outcome, status_class, latency_ms, subject_entity_ids)"
                    " VALUES (:e,:o,:t,:a,'rest','/v1/capabilities','ok','2xx',:l,:se)"
                ),
                {
                    "e": uuid.uuid4(),
                    "o": row.get("at", _AT),
                    "t": row["tenant"],
                    "a": row["actor"],
                    "l": row.get("latency", 10),
                    "se": row.get("entities", []),
                },
            )
        await session.commit()


async def _count(factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID, actor: uuid.UUID | None) -> int:
    sql = "SELECT count(*) FROM usage_events WHERE tenant_id = :t"
    params: dict[str, object] = {"t": tenant}
    if actor is not None:
        sql += " AND actor_id = :a"
        params["a"] = actor
    async with factory() as session:
        return int((await session.execute(text(sql), params)).scalar_one())


async def _rollup_rows(factory: async_sessionmaker[AsyncSession], tenant: uuid.UUID) -> list[dict]:
    """Every rollup value across all three grains, in a stable order."""
    out: list[dict] = []
    async with factory() as session:
        for table in (
            "usage_rollup_tenant_day",
            "usage_rollup_capability_day",
            "usage_rollup_tool_day",
        ):
            rows = (
                await session.execute(
                    text(f"SELECT * FROM {table} WHERE tenant_id = :t ORDER BY 1, 2, 3"),
                    {"t": tenant},
                )
            ).mappings()
            # `computed_at` is excluded deliberately: it records when the rollup ran,
            # not what it measured, and comparing it would make this test fail for a
            # rerun rather than for a changed number.
            out.extend({k: v for k, v in dict(r).items() if k != "computed_at"} for r in rows)
    return out


# ---------------------------------------------------------------------------
# The property the retention design rests on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erasing_an_actor_leaves_every_rollup_value_identical(factory) -> None:
    """An erasure must not be able to change a number that was already reported.

    Rollups hold distinct-actor counts and no actor identifier, so they are not the
    erased person's data. If erasure rewrote them, a figure quoted for a closed month
    would silently stop matching itself — and the way anyone would find out is by
    being contradicted in a meeting.
    """
    tenant = uuid.uuid4()
    victim, other = uuid.uuid4(), uuid.uuid4()
    await _insert(
        factory,
        [
            {"tenant": tenant, "actor": victim, "latency": 10},
            {"tenant": tenant, "actor": victim, "latency": 20},
            {"tenant": tenant, "actor": other, "latency": 30},
        ],
    )
    await roll_up_day(factory, _DAY)
    before = await _rollup_rows(factory, tenant)
    assert before, "no rollups to compare — the fixture proved nothing"
    assert before[0]["calls"] == 3
    assert before[0]["distinct_actors"] == 2

    await UsageErasure(factory).erase_actor(_ctx(tenant, victim), victim)

    assert await _count(factory, tenant, victim) == 0
    assert await _rollup_rows(factory, tenant) == before


@pytest.mark.asyncio
async def test_a_rollup_recomputed_after_an_erasure_does_change(factory) -> None:
    """The honest limit of the guarantee above, stated rather than left implicit.

    Nothing preserves a rollup against being recomputed from rows that are now gone.
    The schedule only ever recomputes yesterday and today, so a closed month is
    safe — but a backfill over an erased window would produce different numbers, and
    an operator should know that before running one rather than after.
    """
    tenant = uuid.uuid4()
    victim = uuid.uuid4()
    await _insert(factory, [{"tenant": tenant, "actor": victim}, {"tenant": tenant, "actor": uuid.uuid4()}])
    await roll_up_day(factory, _DAY)
    before = await _rollup_rows(factory, tenant)

    await UsageErasure(factory).erase_actor(_ctx(tenant, victim), victim)
    await roll_up_day(factory, _DAY)

    after = await _rollup_rows(factory, tenant)
    assert before[0]["calls"] == 2
    assert after[0]["calls"] == 1, "a recompute over erased rows should reflect the erasure"


# ---------------------------------------------------------------------------
# The erasure contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_target_actors_rows_go(factory) -> None:
    tenant = uuid.uuid4()
    victim, bystander = uuid.uuid4(), uuid.uuid4()
    await _insert(
        factory,
        [
            {"tenant": tenant, "actor": victim},
            {"tenant": tenant, "actor": bystander},
            {"tenant": tenant, "actor": None},
        ],
    )

    counts = await UsageErasure(factory).erase_actor(_ctx(tenant, victim), victim)

    assert counts["usage_events"] == 1
    assert await _count(factory, tenant, bystander) == 1
    # An unauthenticated call has no actor, so it is nobody's personal data and
    # `actor_id = :actor` never matches it.
    assert await _count(factory, tenant, None) == 2


@pytest.mark.asyncio
async def test_another_tenants_rows_for_the_same_actor_survive(factory) -> None:
    """Tenant scoping, which an unscoped delete would silently discard.

    Actor ids are unique system-wide, so `WHERE actor_id = :actor` alone would
    work — and would let one tenant's administrator delete rows recording calls
    made inside another tenant. That is a cross-tenant write wearing a compliance
    request as a disguise.
    """
    actor = uuid.uuid4()
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await _insert(factory, [{"tenant": mine, "actor": actor}, {"tenant": theirs, "actor": actor}])

    await UsageErasure(factory).erase_actor(_ctx(mine, actor), actor)

    assert await _count(factory, mine, actor) == 0
    assert await _count(factory, theirs, actor) == 1


@pytest.mark.asyncio
async def test_a_second_erasure_succeeds_and_reports_nothing_left(factory) -> None:
    # Retrying a partially-failed erasure is the normal case, not the exception, so
    # a participant that errored on already-erased data would break the retry
    # exactly when it was needed.
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    await _insert(factory, [{"tenant": tenant, "actor": actor}])
    participant = UsageErasure(factory)

    first = await participant.erase_actor(_ctx(tenant, actor), actor)
    second = await participant.erase_actor(_ctx(tenant, actor), actor)

    assert first["usage_events"] == 1
    assert second["usage_events"] == 0


@pytest.mark.asyncio
async def test_erasing_an_actor_who_never_called_is_not_an_error(factory) -> None:
    tenant = uuid.uuid4()
    counts = await UsageErasure(factory).erase_actor(_ctx(tenant, uuid.uuid4()), uuid.uuid4())
    assert counts == {"usage_events": 0, "usage_events_buffered": 0}


@pytest.mark.asyncio
async def test_the_delete_runs_in_batches_until_nothing_is_left(factory) -> None:
    """No ceiling, unlike the retention sweep.

    Expiry may stop early and come back in an hour. An erasure that stopped early
    would report success with rows still there, which is the failure the registry
    exists to prevent. Driven with a batch size below the row count so the loop has
    to run more than once to be correct.
    """
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    await _insert(factory, [{"tenant": tenant, "actor": actor} for _ in range(7)])

    counts = await UsageErasure(factory, batch_size=2).erase_actor(_ctx(tenant, actor), actor)

    assert counts["usage_events"] == 7
    assert await _count(factory, tenant, actor) == 0


# ---------------------------------------------------------------------------
# The buffer, which is the second place these rows live
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_buffered_event_cannot_resurrect_the_erased_actor(factory) -> None:
    """The gap that makes an erasure worse than useless if missed.

    An event buffered when the request arrives would flush after the delete and
    reinsert the actor into a table they had just been erased from — while the
    receipt said they were gone.
    """
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    writer = UsageWriter(factory)
    writer.record(
        UsageEvent(
            occurred_at=_AT,
            tenant_id=tenant,
            actor_id=actor,
            surface="rest",
            operation="/v1/capabilities",
            outcome="ok",
            status_class="2xx",
            latency_ms=5,
        )
    )

    counts = await UsageErasure(factory, writer=writer).erase_actor(_ctx(tenant, actor), actor)

    assert counts["usage_events_buffered"] == 1
    # Flushing now must not bring it back, because it is no longer queued.
    await writer._flush_once()
    assert await _count(factory, tenant, actor) == 0


@pytest.mark.asyncio
async def test_discarding_one_actor_keeps_everyone_elses_queued_events(factory) -> None:
    # A drain-and-refill that dropped the whole buffer would lose unrelated
    # tenants' usage entirely, and the loss would be invisible: no error, just
    # numbers quietly lower than reality.
    tenant, victim, bystander = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    writer = UsageWriter(factory)

    def event(actor: uuid.UUID) -> UsageEvent:
        return UsageEvent(
            occurred_at=_AT,
            tenant_id=tenant,
            actor_id=actor,
            surface="rest",
            operation="/v1/capabilities",
            outcome="ok",
            status_class="2xx",
            latency_ms=5,
        )

    writer.record(event(victim))
    writer.record(event(bystander))
    writer.record(event(victim))

    assert writer.discard_actor(tenant, victim) == 2

    await writer._flush_once()
    assert await _count(factory, tenant, bystander) == 1
    assert await _count(factory, tenant, victim) == 0


@pytest.mark.asyncio
async def test_the_same_actor_in_another_tenant_is_not_discarded(factory) -> None:
    # The buffer holds every tenant's events, so the discard has to be as
    # tenant-scoped as the delete is.
    mine, theirs, actor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    writer = UsageWriter(factory)

    for tenant in (mine, theirs):
        writer.record(
            UsageEvent(
                occurred_at=_AT,
                tenant_id=tenant,
                actor_id=actor,
                surface="rest",
                operation="/v1/capabilities",
                outcome="ok",
                status_class="2xx",
                latency_ms=5,
            )
        )

    assert writer.discard_actor(mine, actor) == 1

    await writer._flush_once()
    assert await _count(factory, theirs, actor) == 1
