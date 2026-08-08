"""Query helpers for sync-source and sync-run administration.

Plain module-level functions. Standalone reads take a ``session_factory`` and
open their own session, matching ``contextplane.service.catalog.identity``'s
``resolve_whoami``. The two writes that must interleave with a caller-held
precondition check (patch, delete) take an already-open ``session`` instead,
so the admin router can still run ``check_if_match`` between the read and the
write without a second round trip.

``create_sync_source`` owns its own transaction rather than taking a shared
session: provisioning the sync-worker actor and inserting the source row are
one atomic unit, and no other endpoint needs to interleave anything with it.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.ingest import runner
from contextplane.storage.models import Fact, SyncRun, SyncSource

__all__ = [
    "create_sync_source",
    "get_sync_run",
    "get_sync_run_for_update",
    "get_sync_source",
    "get_sync_source_for_update",
    "list_superseded_facts",
    "list_sync_runs",
    "list_sync_sources",
]


async def create_sync_source(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_id: uuid.UUID,
    tenant_id: uuid.UUID,
    source_type: str,
    display_name: str,
    config: dict[str, object],
    credentials_ref: str | None,
    schedule: str | None,
    created_by: uuid.UUID | None,
    now: datetime.datetime,
) -> None:
    """Upsert the sync-worker actor for source_type, then insert the source row.

    One transaction: a source row naming an actor that was never provisioned
    would leave sync runs with nothing to attribute themselves to.
    """
    async with session_factory() as session, session.begin():
        await runner.resolve_sync_actor(session, tenant_id, source_type)
        session.add(
            SyncSource(
                source_id=source_id,
                tenant_id=tenant_id,
                source_type=source_type,
                display_name=display_name,
                config=config,
                credentials_ref=credentials_ref,
                schedule=schedule,
                is_active=True,
                created_at=now,
                created_by=created_by,
            )
        )
        await session.flush()


async def get_sync_source(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
) -> SyncSource | None:
    """Return the sync source by primary key, or None if it does not exist."""
    async with session_factory() as session:
        return await session.get(SyncSource, source_id)


async def get_sync_source_for_update(session: AsyncSession, source_id: uuid.UUID) -> SyncSource | None:
    """Same lookup as get_sync_source, inside a caller-managed write transaction."""
    return await session.get(SyncSource, source_id)


async def list_sync_sources(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    active_only: bool,
) -> list[SyncSource]:
    async with session_factory() as session:
        stmt = select(SyncSource).where(SyncSource.tenant_id == tenant_id)
        if active_only:
            stmt = stmt.where(SyncSource.is_active.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_sync_run(
    session_factory: async_sessionmaker[AsyncSession],
    sync_run_id: uuid.UUID,
) -> SyncRun | None:
    """Return the sync run by primary key, or None if it does not exist."""
    async with session_factory() as session:
        return await session.get(SyncRun, sync_run_id)


async def get_sync_run_for_update(session: AsyncSession, sync_run_id: uuid.UUID) -> SyncRun | None:
    """Same lookup as get_sync_run, inside a session the caller already opened."""
    return await session.get(SyncRun, sync_run_id)


async def list_sync_runs(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    source_id: uuid.UUID | None,
    run_status: str | None,
    from_dt: datetime.datetime | None,
    to_dt: datetime.datetime | None,
) -> list[SyncRun]:
    conditions = [SyncRun.tenant_id == tenant_id]
    if source_id is not None:
        conditions.append(SyncRun.source_id == source_id)
    if run_status is not None:
        conditions.append(SyncRun.status == run_status)
    if from_dt is not None:
        conditions.append(SyncRun.started_at >= from_dt)
    if to_dt is not None:
        conditions.append(SyncRun.started_at <= to_dt)

    async with session_factory() as session:
        stmt = select(SyncRun).where(and_(*conditions)).order_by(SyncRun.started_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_superseded_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    sync_run_id: uuid.UUID,
) -> list[Fact]:
    """Every fact with is_authoritative_superseded=TRUE for this run.

    Called inside the same session that already confirmed the run exists and
    belongs to this tenant, so no second tenant check is needed here.
    """
    result = await session.execute(
        select(Fact).where(
            Fact.tenant_id == tenant_id,
            Fact.sync_run_id == sync_run_id,
            Fact.is_authoritative_superseded.is_(True),
        )
    )
    return list(result.scalars().all())
