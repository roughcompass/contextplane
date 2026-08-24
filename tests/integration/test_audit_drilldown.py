"""The justification is the control, so these test the record more than the data.

E11-T3. The figure a drill-down returns is a count; what makes the surface
acceptable is that no read reaches it without a reason on the record, written
before the answer and in the same transaction.
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
from contextplane.service.memory.audit_drilldown import (
    METRIC_CLAIMS_AUTHORED,
    AuditDrilldownService,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 23, 12, 0, tzinfo=datetime.UTC)
_START = _NOW - datetime.timedelta(days=30)
_WHY = "Reviewing a complaint that this actor's claims were not being adjudicated."


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _ctx(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:t, :s, :s, :n, TRUE)"
            ),
            {"t": tid, "s": f"ad-{tid.hex[:8]}", "n": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'auditor', :sub, :n)"
            ),
            {"a": aid, "t": tid, "sub": f"aud-{aid.hex[:8]}", "n": _NOW},
        )
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["auditor"])


async def _recorded(factory: async_sessionmaker[AsyncSession], ctx: TenantContext) -> list[dict[str, object]]:
    async with factory() as session:
        return [
            dict(row)
            for row in (
                await session.execute(
                    text(
                        "SELECT subject_actor_id, metric, justification, auditor_actor_id "
                        "  FROM audit_justified_reads WHERE tenant_id = :t"
                    ),
                    {"t": ctx.tenant_id},
                )
            ).mappings()
        ]


def _service(factory: async_sessionmaker[AsyncSession]) -> AuditDrilldownService:
    return AuditDrilldownService(factory, clock=FakeClock(_NOW))


@pytest.mark.asyncio
async def test_a_read_records_its_justification_and_who_asked(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _ctx(factory)
    subject = uuid.uuid4()

    detail = await _service(factory).read_actor_metric(
        ctx,
        subject_actor_id=subject,
        metric=METRIC_CLAIMS_AUTHORED,
        window_start=_START,
        window_end=_NOW,
        justification=_WHY,
    )

    rows = await _recorded(factory, ctx)
    assert len(rows) == 1
    assert rows[0]["subject_actor_id"] == subject
    assert rows[0]["auditor_actor_id"] == ctx.actor_id
    assert rows[0]["justification"] == _WHY
    # The auditor can cite their own read: a surface that recorded something it
    # would not show the caller is one people learn to distrust.
    assert detail.read_id is not None


@pytest.mark.asyncio
async def test_a_read_with_too_thin_a_justification_records_nothing(factory: async_sessionmaker[AsyncSession]) -> None:
    """The log is of reads, not of attempts.

    Every refusal happens before the transaction opens, so a rejected request
    leaves no row. Mixing attempts into this table would make it useless for
    both questions — "what was looked at" and "what was tried".
    """
    ctx = await _ctx(factory)

    with pytest.raises(ValidationError, match="willing to have read back"):
        await _service(factory).read_actor_metric(
            ctx,
            subject_actor_id=uuid.uuid4(),
            metric=METRIC_CLAIMS_AUTHORED,
            window_start=_START,
            window_end=_NOW,
            justification="audit",
        )

    assert await _recorded(factory, ctx) == []


@pytest.mark.asyncio
async def test_an_unknown_metric_is_refused_before_anything_is_written(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _ctx(factory)

    with pytest.raises(ValidationError, match="unknown drill-down metric"):
        await _service(factory).read_actor_metric(
            ctx,
            subject_actor_id=uuid.uuid4(),
            metric="everything_they_have_ever_done",
            window_start=_START,
            window_end=_NOW,
            justification=_WHY,
        )

    assert await _recorded(factory, ctx) == []


@pytest.mark.asyncio
async def test_the_database_refuses_a_justification_that_is_not_a_sentence(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The floor is in the CHECK as well as the service.

    Twenty characters will not make a bad reason good, but it stops "audit" and
    "checking" from being reasons at all — and putting it in the database means
    a second writer cannot decide otherwise.
    """
    ctx = await _ctx(factory)
    with pytest.raises(Exception, match="ck_justified_read_reason"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO audit_justified_reads ("
                    "  read_id, tenant_id, auditor_actor_id, subject_actor_id, metric,"
                    "  window_start, window_end, justification, read_at"
                    ") VALUES (:r, :t, :a, :s, 'claims_authored', :ws, :we, 'audit', :n)"
                ),
                {
                    "r": uuid.uuid4(),
                    "t": ctx.tenant_id,
                    "a": ctx.actor_id,
                    "s": uuid.uuid4(),
                    "ws": _START,
                    "we": _NOW,
                    "n": _NOW,
                },
            )


@pytest.mark.asyncio
async def test_a_subject_can_see_who_has_been_looking(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The direction that makes the record more than bookkeeping: a log only its
    own author can read disciplines nobody."""
    ctx = await _ctx(factory)
    subject, other = uuid.uuid4(), uuid.uuid4()
    service = _service(factory)
    for target in (subject, subject, other):
        await service.read_actor_metric(
            ctx,
            subject_actor_id=target,
            metric=METRIC_CLAIMS_AUTHORED,
            window_start=_START,
            window_end=_NOW,
            justification=_WHY,
        )

    found = await service.reads_of_subject(ctx, subject_actor_id=subject)

    assert len(found) == 2
    assert {row["auditor_actor_id"] for row in found} == {str(ctx.actor_id)}
    assert {row["justification"] for row in found} == {_WHY}


@pytest.mark.asyncio
async def test_a_read_about_an_erased_actor_is_still_recorded(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`subject_actor_id` carries no foreign key, on purpose.

    An auditor may reasonably ask about an actor who has since been erased, and
    a foreign key would make the *record of the question* impossible after the
    answer stopped existing — precisely the period an auditor is most likely to
    be asking about.
    """
    ctx = await _ctx(factory)
    never_existed = uuid.uuid4()

    await _service(factory).read_actor_metric(
        ctx,
        subject_actor_id=never_existed,
        metric=METRIC_CLAIMS_AUTHORED,
        window_start=_START,
        window_end=_NOW,
        justification=_WHY,
    )

    rows = await _recorded(factory, ctx)
    assert [row["subject_actor_id"] for row in rows] == [never_existed]
