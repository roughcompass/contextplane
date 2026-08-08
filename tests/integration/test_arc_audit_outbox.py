"""The audit outbox: atomic with the state it describes.

The property under test is not "a row appears" but "the row and the state
change share a fate". Both directions are checked: a committed change always
has its audit row, and a rolled-back one leaves neither behind.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.arc.models import DEPLOYMENT_TENANT_ID
from contextplane.arc.service import audit_outbox
from contextplane.arc.service.audit_outbox import MAX_PAYLOAD_BYTES, AuditPayloadTooLarge
from contextplane.audit import actions
from tests.helpers.arc_fixtures import ArcSeed, seed_arc


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-outbox")


async def _rows_for(factory: async_sessionmaker[AsyncSession], marker: str) -> list:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT outbox_id, tenant_id, event_type, event_payload, drained_at, attempts "
                    "FROM arc_audit_outbox WHERE event_payload ->> 'marker' = :marker"
                ),
                {"marker": marker},
            )
        ).all()


@pytest.mark.asyncio
async def test_an_outbox_row_is_written_on_the_callers_session(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type=actions.ARC_CHALLENGE_ISSUED,
            payload={"marker": marker, "host_id": "host-1"},
        )

    rows = await _rows_for(factory, marker)
    assert len(rows) == 1
    assert rows[0].tenant_id == seed.tenant_id
    assert rows[0].event_type == actions.ARC_CHALLENGE_ISSUED
    assert rows[0].event_payload["host_id"] == "host-1"


@pytest.mark.asyncio
async def test_a_new_row_is_undrained_with_no_attempts(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The drain worker's starting state, so a row is picked up exactly once."""
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit(
            session, tenant_id=seed.tenant_id, event_type=actions.ARC_CONTEXT_RESOLVED, payload={"marker": marker}
        )

    row = (await _rows_for(factory, marker))[0]
    assert row.drained_at is None
    assert row.attempts == 0


@pytest.mark.asyncio
async def test_the_audit_row_rolls_back_with_the_state_it_describes(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The half that inline auditing on a separate connection gets wrong:
    recording an attempt that never committed."""
    marker = uuid.uuid4().hex
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE arc_artifacts SET slug = 'changed' WHERE artifact_id = :aid"),
                {"aid": seed.artifact_id},
            )
            await audit_outbox.emit(
                session,
                tenant_id=seed.tenant_id,
                event_type=actions.ARC_ARTIFACT_REGISTERED,
                payload={"marker": marker},
            )
            raise RuntimeError("the write fails after auditing")
    except RuntimeError:
        pass

    assert await _rows_for(factory, marker) == []

    async with factory() as session:
        slug = (
            await session.execute(
                text("SELECT slug FROM arc_artifacts WHERE artifact_id = :aid"), {"aid": seed.artifact_id}
            )
        ).scalar_one()
    assert slug != "changed"


@pytest.mark.asyncio
async def test_the_audit_row_commits_with_the_state_it_describes(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The other half: a committed change is never left unaudited."""
    marker = uuid.uuid4().hex
    new_slug = f"slug-{marker[:8]}"
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE arc_artifacts SET slug = :slug WHERE artifact_id = :aid"),
            {"aid": seed.artifact_id, "slug": new_slug},
        )
        await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type=actions.ARC_ARTIFACT_REGISTERED,
            payload={"marker": marker},
        )

    assert len(await _rows_for(factory, marker)) == 1
    async with factory() as session:
        slug = (
            await session.execute(
                text("SELECT slug FROM arc_artifacts WHERE artifact_id = :aid"), {"aid": seed.artifact_id}
            )
        ).scalar_one()
    assert slug == new_slug


@pytest.mark.asyncio
async def test_global_events_attribute_to_the_reserved_deployment_tenant(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Filing deployment activity under a real tenant would both mislead
    that tenant's auditor and leak that the activity happened."""
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit_global(
            session, event_type=actions.ARC_ARTIFACT_REGISTERED, payload={"marker": marker, "scope": "global"}
        )

    rows = await _rows_for(factory, marker)
    assert len(rows) == 1
    assert rows[0].tenant_id == DEPLOYMENT_TENANT_ID


@pytest.mark.asyncio
async def test_an_unknown_tenant_is_rejected_by_the_foreign_key(factory: async_sessionmaker[AsyncSession]) -> None:
    """An audit row attributed to nobody is not auditable."""
    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await audit_outbox.emit(
                session, tenant_id=uuid.uuid4(), event_type=actions.ARC_CONTEXT_RESOLVED, payload={}
            )


@pytest.mark.asyncio
async def test_an_oversized_payload_is_rejected_not_truncated(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """A truncated audit row looks complete and is not; an auditor has no
    way to tell something was dropped."""
    async with factory() as session, session.begin():
        with pytest.raises(AuditPayloadTooLarge, match="over the"):
            await audit_outbox.emit(
                session,
                tenant_id=seed.tenant_id,
                event_type=actions.ARC_CONTEXT_RESOLVED,
                payload={"blob": "x" * (MAX_PAYLOAD_BYTES + 1)},
            )


@pytest.mark.asyncio
async def test_a_payload_just_under_the_bound_is_accepted(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The bound is a real edge, so both sides of it are pinned."""
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type=actions.ARC_CONTEXT_RESOLVED,
            payload={"marker": marker, "blob": "x" * (MAX_PAYLOAD_BYTES - 200)},
        )
    assert len(await _rows_for(factory, marker)) == 1


@pytest.mark.asyncio
async def test_payload_key_order_does_not_change_the_stored_json(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """Two identical events must serialize identically, or downstream
    comparison and deduplication have nothing to work with."""
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type=actions.ARC_CONTEXT_RESOLVED,
            payload={"marker": marker, "b": 2, "a": 1},
        )
        await audit_outbox.emit(
            session,
            tenant_id=seed.tenant_id,
            event_type=actions.ARC_CONTEXT_RESOLVED,
            payload={"a": 1, "b": 2, "marker": marker},
        )

    rows = await _rows_for(factory, marker)
    assert len(rows) == 2
    assert rows[0].event_payload == rows[1].event_payload


@pytest.mark.asyncio
async def test_an_unknown_event_type_within_the_length_bound_is_accepted(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The column is length-bounded but has no closed vocabulary, so this
    documents the actual behaviour rather than an assumed CHECK. A closed
    vocabulary here is tracked separately as blocked work."""
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        await audit_outbox.emit(
            session, tenant_id=seed.tenant_id, event_type="arc.not.a.real.event", payload={"marker": marker}
        )
    assert len(await _rows_for(factory, marker)) == 1


@pytest.mark.asyncio
async def test_every_arc_action_constant_fits_the_column_bound(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """A constant longer than 128 characters would fail only on the request
    that first used it -- in production, not here."""
    arc_actions = [getattr(actions, name) for name in dir(actions) if name.startswith("ARC_")]
    assert arc_actions, "no ARC action constants found"

    async with factory() as session, session.begin():
        for action in arc_actions:
            assert len(action) <= 128, f"{action} exceeds the event_type bound"
            await audit_outbox.emit(
                session, tenant_id=seed.tenant_id, event_type=action, payload={"marker": uuid.uuid4().hex}
            )


@pytest.mark.asyncio
async def test_emit_returns_the_id_of_the_row_it_wrote(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    marker = uuid.uuid4().hex
    async with factory() as session, session.begin():
        outbox_id = await audit_outbox.emit(
            session, tenant_id=seed.tenant_id, event_type=actions.ARC_CONTEXT_RESOLVED, payload={"marker": marker}
        )

    assert (await _rows_for(factory, marker))[0].outbox_id == outbox_id
