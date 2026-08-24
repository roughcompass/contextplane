"""The obligation record: what it stores, what it refuses, and what it counts."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
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


# --- what the obligation is about ---------------------------------------------


async def _reference(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    kind: str = "incident",
    external_id: str = "INC-4417",
) -> uuid.UUID:
    """One external record, of a stated kind."""
    reference_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_external_references "
                "(reference_id, tenant_id, source_system, source_namespace, kind, external_id, "
                " classification, external_authority, collision_key) "
                "VALUES (:rid, :t, 'pagerduty', 'prod', :kind, :eid, 'internal', 'pagerduty', :ckey)"
            ),
            {"rid": reference_id, "t": ctx.tenant_id, "kind": kind, "eid": external_id, "ckey": reference_id.hex},
        )
    return reference_id


@pytest.mark.asyncio
async def test_an_obligation_can_cite_the_incident_it_is_about(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The relationship the governing decision named and nothing implemented.

    It needed no new table: the external record and the binding both already
    existed, and the whole of the gap was that `reporting_obligation` was not a
    legal `subject_type`.
    """
    ctx = await _seed(factory)
    service = _service(factory)
    obligation = await service.nominate(ctx, summary=_SUMMARY)
    incident = await _reference(factory, ctx)

    assert await service.cite_incident(ctx, obligation_id=obligation.obligation_id, reference_id=incident)

    cited = await service.incidents_for(ctx, obligation_id=obligation.obligation_id)
    assert [row["external_id"] for row in cited] == ["INC-4417"]
    assert [row["kind"] for row in cited] == ["incident"]


@pytest.mark.asyncio
async def test_citing_the_same_incident_twice_states_one_relationship_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A citation list that grew on re-statement would read as two independent
    records of the same event."""
    ctx = await _seed(factory)
    service = _service(factory)
    obligation = await service.nominate(ctx, summary=_SUMMARY)
    incident = await _reference(factory, ctx)

    first = await service.cite_incident(ctx, obligation_id=obligation.obligation_id, reference_id=incident)
    second = await service.cite_incident(ctx, obligation_id=obligation.obligation_id, reference_id=incident)

    assert (first, second) == (True, False)
    assert len(await service.incidents_for(ctx, obligation_id=obligation.obligation_id)) == 1


@pytest.mark.asyncio
async def test_an_obligation_refuses_to_cite_something_that_is_not_an_incident(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The decision says an obligation references an *incident*.

    Admitting a `deployment` or a `build` would leave every read still calling
    it the incident while it had quietly become something else — and a reader
    checking what an obligation was about would be told about a deploy.
    """
    ctx = await _seed(factory)
    service = _service(factory)
    obligation = await service.nominate(ctx, summary=_SUMMARY)
    deployment = await _reference(factory, ctx, kind="deployment", external_id="deploy-91")

    with pytest.raises(ValidationError, match="is about the incident"):
        await service.cite_incident(ctx, obligation_id=obligation.obligation_id, reference_id=deployment)

    assert await service.incidents_for(ctx, obligation_id=obligation.obligation_id) == ()


@pytest.mark.asyncio
async def test_an_obligation_nobody_has_matched_yet_cites_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty result is a nomination in progress, not a missing one.

    0076 made `summary` free text precisely so a nomination need not wait for
    the link — *"an obligation can be nominated before anybody knows which
    record it concerns, and refusing the nomination until the link exists would
    lose the nomination."*
    """
    ctx = await _seed(factory)
    service = _service(factory)
    obligation = await service.nominate(ctx, summary=_SUMMARY)

    assert await service.incidents_for(ctx, obligation_id=obligation.obligation_id) == ()


@pytest.mark.asyncio
async def test_citing_from_another_tenant_finds_neither_end(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both the obligation and the reference are read in the caller's tenant, so
    a citation cannot be used to confirm that either exists somewhere else."""
    mine, theirs = await _seed(factory), await _seed(factory)
    service = _service(factory)
    obligation = await service.nominate(mine, summary=_SUMMARY)
    their_incident = await _reference(factory, theirs)

    with pytest.raises(NotFoundError, match="external reference"):
        await service.cite_incident(mine, obligation_id=obligation.obligation_id, reference_id=their_incident)

    with pytest.raises(NotFoundError, match="reporting obligation"):
        await service.cite_incident(theirs, obligation_id=obligation.obligation_id, reference_id=their_incident)


@pytest.mark.asyncio
async def test_the_database_refuses_a_subject_type_no_rule_admits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The widened CHECK is the property; the service is one writer's discipline.

    Asserted against the constraint directly, so removing the service's own
    vocabulary would not quietly remove the guarantee too.
    """
    ctx = await _seed(factory)
    with pytest.raises(Exception, match="ck_reference_binding_subject_type"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO context_reference_bindings "
                    "(binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at) "
                    "VALUES (:b, :t, :r, 'something_nobody_declared', :s, :n)"
                ),
                {
                    "b": uuid.uuid4(),
                    "t": ctx.tenant_id,
                    "r": await _reference(factory, ctx, external_id="INC-9999"),
                    "s": uuid.uuid4(),
                    "n": _NOW,
                },
            )
