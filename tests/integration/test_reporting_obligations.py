"""The obligation record: what it stores, what it refuses, and what it counts."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ConflictError, NotFoundError
from contextplane.service.governance.obligations import (
    MATERIALITY_MATERIAL,
    MATERIALITY_NOT_MATERIAL,
    MATERIALITY_UNCLASSIFIED,
    ReportingObligationService,
)
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 22, 9, 0, tzinfo=datetime.UTC)
_SUMMARY = "A connector fetched customer records it was not scoped to read."
_NOTE = "Scoped to a sandbox tenant with synthetic data; no customer material left the estate."


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> TenantContext:
    tid, aid = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, :slug, :now, TRUE)"
            ),
            {"tid": tid, "slug": f"obl-{tid.hex[:8]}", "now": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:aid, :tid, 'a', :sub, :now)"
            ),
            {"aid": aid, "tid": tid, "sub": f"s-{aid.hex[:8]}", "now": _NOW},
        )
    return TenantContext(tenant_id=tid, actor_id=aid, roles=["admin"])


def _service(factory: async_sessionmaker[AsyncSession], *, now: datetime.datetime = _NOW):
    return ReportingObligationService(factory, clock=FakeClock(now))


@pytest.mark.asyncio
async def test_a_nomination_lands_unclassified_and_says_so(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nomination is deliberately cheap and deliberately not a classification.

    A surface that demanded a materiality up front would get a guess, and a
    guessed classification is worse than an honest `unclassified` because it
    stops anybody looking again.
    """
    ctx = await _seed(factory)

    obligation = await _service(factory).nominate(ctx, summary=_SUMMARY)

    assert obligation.materiality == MATERIALITY_UNCLASSIFIED
    assert obligation.classified_at is None
    assert obligation.classified_by is None
    assert obligation.nominated_by == ctx.actor_id


@pytest.mark.asyncio
async def test_a_classification_records_who_decided_and_why(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _seed(factory)
    service = _service(factory)
    obligation = await service.nominate(ctx, summary=_SUMMARY)

    classified = await service.classify(
        ctx,
        obligation_id=obligation.obligation_id,
        materiality=MATERIALITY_NOT_MATERIAL,
        note=_NOTE,
    )

    assert classified.materiality == MATERIALITY_NOT_MATERIAL
    assert classified.classified_by == ctx.actor_id
    assert classified.classification_note == _NOTE
    # The nomination instant survives the classification: the delay gauge
    # measures from when it arrived, never from when somebody got to it.
    assert classified.nominated_at == obligation.nominated_at


@pytest.mark.asyncio
async def test_a_second_classification_is_refused_rather_than_overwriting_the_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first answer is the one somebody acted on.

    An overwrite would leave the audit trail describing only the most recent
    opinion, which is the one nobody relied on.
    """
    ctx = await _seed(factory)
    service = _service(factory)
    obligation = await service.nominate(ctx, summary=_SUMMARY)
    await service.classify(ctx, obligation_id=obligation.obligation_id, materiality=MATERIALITY_MATERIAL, note=_NOTE)

    with pytest.raises(ConflictError, match="already classified"):
        await service.classify(
            ctx,
            obligation_id=obligation.obligation_id,
            materiality=MATERIALITY_NOT_MATERIAL,
            note=_NOTE,
        )

    assert (await service.get(ctx, obligation_id=obligation.obligation_id)).materiality == (MATERIALITY_MATERIAL)


@pytest.mark.asyncio
async def test_the_database_refuses_a_half_classified_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `classified_at` with no actor is a decision with nobody behind it.

    Written directly rather than through the service, because the point is that
    the constraint holds against a writer that bypasses the service entirely.
    """
    ctx = await _seed(factory)
    with pytest.raises(Exception, match="ck_obligation_classification_is_attributed"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO reporting_obligations "
                    "  (obligation_id, tenant_id, summary, materiality, nominated_by, classified_at) "
                    "VALUES (:oid, :tid, :summary, 'material', :actor, :now)"
                ),
                {
                    "oid": uuid.uuid4(),
                    "tid": ctx.tenant_id,
                    "summary": _SUMMARY,
                    "actor": ctx.actor_id,
                    "now": _NOW,
                },
            )


@pytest.mark.asyncio
async def test_another_tenants_obligation_is_not_readable(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _seed(factory)
    other = await _seed(factory)
    obligation = await _service(factory).nominate(ctx, summary=_SUMMARY)

    with pytest.raises(NotFoundError):
        await _service(factory).get(other, obligation_id=obligation.obligation_id)


@pytest.mark.asyncio
async def test_the_backlog_reports_the_age_of_the_longest_wait(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The count alone is not actionable.

    Five nominated this morning and five nominated in March are the same number
    and completely different situations, which is why the age ships beside the
    count rather than instead of it.
    """
    ctx = await _seed(factory)
    await _service(factory).nominate(ctx, summary=_SUMMARY)
    await _service(factory, now=_NOW + datetime.timedelta(hours=2)).nominate(
        ctx, summary="A second thing that may need reporting."
    )

    later = _NOW + datetime.timedelta(days=3)
    backlog = await _service(factory, now=later).unclassified_backlog(ctx)

    assert backlog.count == 2
    assert backlog.oldest_age_seconds == pytest.approx(3 * 24 * 3600)


@pytest.mark.asyncio
async def test_an_empty_backlog_reports_zero_rather_than_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A missing series is indistinguishable from a scrape that failed, and this
    is the gauge somebody alerts on."""
    ctx = await _seed(factory)

    backlog = await _service(factory).unclassified_backlog(ctx)

    assert backlog.count == 0
    assert backlog.oldest_age_seconds == 0.0


@pytest.mark.asyncio
async def test_a_classified_obligation_leaves_the_backlog(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx = await _seed(factory)
    service = _service(factory)
    obligation = await service.nominate(ctx, summary=_SUMMARY)
    assert (await service.unclassified_backlog(ctx)).count == 1

    await service.classify(ctx, obligation_id=obligation.obligation_id, materiality=MATERIALITY_MATERIAL, note=_NOTE)

    assert (await service.unclassified_backlog(ctx)).count == 0
