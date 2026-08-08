"""Admin sync-source CRUD + sync-run query endpoints.

Sync-source: POST/GET/GET{id}/PATCH{id}/DELETE{id}/POST{id}/trigger
Sync-run:    GET/GET{run_id}/GET{run_id}/superseded
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contextplane.api.middleware.etag import check_if_match, compute_etag, latest_timestamp
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from contextplane.api.routers._admin_common import _admin_required
from contextplane.api.schemas.common import Links
from contextplane.ingest import queries as ingest_queries
from contextplane.ingest.runner import run_sync_job
from contextplane.storage.models import SyncRun, SyncSource
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/admin")


class SyncSourceCreate(BaseModel):
    """Body for POST /sync-sources; `source_type` must name a registered connector."""

    source_type: str
    display_name: str
    config: dict[str, object] = Field(default_factory=dict)
    credentials_ref: str | None = None
    schedule: str | None = None


class SyncSourcePatch(BaseModel):
    """Body for PATCH /sync-sources/{id}; every field optional, an omitted one keeps its current value."""

    display_name: str | None = None
    config: dict[str, object] | None = None
    credentials_ref: str | None = None
    schedule: str | None = None
    is_active: bool | None = None


class SyncSourceResponse(BaseModel):
    """A sync source's configuration and status, returned by the source CRUD routes."""

    source_id: uuid.UUID
    tenant_id: uuid.UUID
    source_type: str
    display_name: str
    config: dict[str, object]
    credentials_ref: str | None
    schedule: str | None
    is_active: bool
    created_at: datetime.datetime
    created_by: uuid.UUID | None
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class TriggerResponse(BaseModel):
    """Acknowledgement that a manual sync run was scheduled -- `status` is always "queued" at this point."""

    sync_run_id: uuid.UUID
    source_id: uuid.UUID
    status: str
    trigger: str
    started_at: datetime.datetime


class SyncRunResponse(BaseModel):
    """One sync run's outcome, returned by the run-listing and run-detail routes."""

    sync_run_id: uuid.UUID
    source_id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    trigger: str
    started_at: datetime.datetime
    finished_at: datetime.datetime | None
    duration_s: int | None
    artifact_count: int | None
    error_summary: str | None
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class SupersededFactResponse(BaseModel):
    """A fact that a later sync run marked no longer authoritative for this run."""

    fact_id: uuid.UUID
    entity_id: uuid.UUID
    category: str
    body: str
    sync_run_id: uuid.UUID | None
    t_valid_from: datetime.datetime
    t_ingested_at: datetime.datetime


def _source_to_response(s: SyncSource, *, include_links: bool = False) -> SyncSourceResponse:
    return SyncSourceResponse(
        source_id=s.source_id,
        tenant_id=s.tenant_id,
        source_type=s.source_type,
        display_name=s.display_name,
        config=dict(s.config) if s.config else {},
        credentials_ref=s.credentials_ref,
        schedule=s.schedule,
        is_active=s.is_active,
        created_at=s.created_at,
        created_by=s.created_by,
        _links=Links(self=f"/v1/admin/sync-sources/{s.source_id}") if include_links else None,
    )


def _run_to_response(r: SyncRun, *, include_links: bool = False) -> SyncRunResponse:
    return SyncRunResponse(
        sync_run_id=r.sync_run_id,
        source_id=r.source_id,
        tenant_id=r.tenant_id,
        status=r.status,
        trigger=r.trigger,
        started_at=r.started_at,
        finished_at=r.finished_at,
        duration_s=r.duration_s,
        artifact_count=r.artifact_count,
        error_summary=r.error_summary,
        _links=Links(self=f"/v1/admin/sync-runs/{r.sync_run_id}") if include_links else None,
    )


@router.post(
    "/sync-sources",
    response_model=SyncSourceResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin: sync"],
)
async def create_sync_source(
    body: SyncSourceCreate,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> SyncSourceResponse:
    """Create a new sync source.

    Validates the connector exists and calls ``connector.validate()`` before
    persisting.  Upserts the sync-worker actor via the runner helper so that
    actor_id is available for subsequent sync runs.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original response; same key + different body returns 409.
    """
    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    # Imported here, not at module level: tests patch
    # contextplane.ingest.connector_registry.get_connector directly, and a
    # module-level `from ... import get_connector` would bind once at this
    # router's own import time, before any test's patch ever applies.
    from contextplane.ingest.connector_registry import (  # noqa: PLC0415 - must stay patchable via contextplane.ingest.connector_registry.get_connector
        UnknownConnectorError,
        get_connector,
    )

    # Validate source_type is known.
    try:
        ConnectorClass = get_connector(body.source_type)
    except UnknownConnectorError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # Validate credentials (connector.validate raises CredentialError on failure).
    connector = ConnectorClass()
    try:
        await connector.validate(body.credentials_ref)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"connector validation failed: {exc}",
        ) from exc

    now = datetime.datetime.now(tz=datetime.UTC)
    source_id = uuid.uuid4()

    factory = request.app.state.session_factory
    await ingest_queries.create_sync_source(
        factory,
        source_id=source_id,
        tenant_id=ctx.tenant_id,
        source_type=body.source_type,
        display_name=body.display_name,
        config=body.config,
        credentials_ref=body.credentials_ref,
        schedule=body.schedule,
        created_by=ctx.actor_id,
        now=now,
    )

    row = await ingest_queries.get_sync_source(factory, source_id)
    if row is None:
        raise HTTPException(status_code=500, detail="source row missing after insert")
    response = _source_to_response(row)
    await idem.persist(ctx, 201, response.model_dump(mode="json"))
    return response


@router.get("/sync-sources", response_model=list[SyncSourceResponse], tags=["admin: sync"])
async def list_sync_sources(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
    active_only: bool = Query(True),
) -> list[SyncSourceResponse]:
    """List this tenant's sync sources; `active_only` (default true) hides soft-deleted rows."""
    factory = request.app.state.session_factory
    sources = await ingest_queries.list_sync_sources(factory, ctx.tenant_id, active_only=active_only)
    return [_source_to_response(s) for s in sources]


@router.get(
    "/sync-sources/{source_id}",
    response_model=SyncSourceResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    tags=["admin: sync"],
)
async def get_sync_source(
    source_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> SyncSourceResponse:
    """Return a single sync-source record.

    Emits an ``ETag`` header computed from the source identifier and its
    ``created_at`` timestamp.  Clients can echo this value as ``If-Match``
    on subsequent PATCH calls for optimistic concurrency.
    """
    factory = request.app.state.session_factory
    source = await ingest_queries.get_sync_source(factory, source_id)
    # is_active=False is a soft-deleted row; the contract says GET on a
    # soft-deleted source returns 404 (the row is hidden, not surfaced).
    if source is None or source.tenant_id != ctx.tenant_id or not source.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sync_source not found")
    response = _source_to_response(source, include_links=True)
    etag = compute_etag(source.source_id, latest_timestamp(source.created_at))
    body = response.model_dump(by_alias=True, exclude_unset=True, mode="json")
    return JSONResponse(content=body, headers={"ETag": etag})  # type: ignore[return-value]


async def patch_sync_source(
    source_id: uuid.UUID,
    body: SyncSourcePatch,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> SyncSourceResponse:
    """Partial-update a sync source configuration.

    Honours the ``If-Match`` request header (advisory): if present and stale,
    returns 412 Precondition Failed; if absent, logs a debug warning and
    accepts the write.  ETag is computed before the write so a stale
    precondition fails fast.

    Recommended flow: GET /v1/admin/sync-sources/{id} → ETag header → PATCH
    with If-Match.
    """
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        source = await ingest_queries.get_sync_source_for_update(session, source_id)
        if source is None or source.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sync_source not found")
        # Compute the pre-write ETag so a stale If-Match fails before any field
        # is mutated. created_at is the stable timestamp for sync_sources (no
        # updated_at column on this model).
        pre_etag = compute_etag(source.source_id, latest_timestamp(source.created_at))
        check_if_match(
            request.headers.get("if-match"),
            pre_etag,
            resource_kind="sync_source",
        )
        if body.display_name is not None:
            source.display_name = body.display_name
        if body.config is not None:
            source.config = body.config
        if body.credentials_ref is not None:
            source.credentials_ref = body.credentials_ref
        if body.schedule is not None:
            source.schedule = body.schedule
        if body.is_active is not None:
            source.is_active = body.is_active
        await session.flush()
        return _source_to_response(source)


async def delete_sync_source(
    source_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Soft-delete: sets is_active=FALSE."""
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        source = await ingest_queries.get_sync_source_for_update(session, source_id)
        if source is None or source.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sync_source not found")
        source.is_active = False
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sync-sources/{source_id}/trigger",
    response_model=TriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["admin: sync"],
)
async def trigger_sync(
    source_id: uuid.UUID,
    request: Request,
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_admin_required),
) -> TriggerResponse:
    """Enqueue an immediate manual sync run for *source_id*.

    Creates a ``sync_runs`` row with ``trigger='manual'`` and schedules a
    one-shot APScheduler job (date trigger = now).  Returns 202 immediately.

    Honours ``X-Idempotency-Key``: same key + same body replays the
    original 202 response, preventing duplicate trigger submissions on retry.
    """
    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    factory = request.app.state.session_factory
    source = await ingest_queries.get_sync_source(factory, source_id)
    if source is None or source.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sync_source not found")
    if not source.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="sync_source is inactive; re-activate before triggering",
        )

    scheduler = request.app.state.scheduler
    settings = request.app.state.settings
    catalog = request.app.state.catalog
    now = datetime.datetime.now(tz=datetime.UTC)
    job_id = f"manual:{source_id}:{uuid.uuid4()}"

    scheduler.add_job(
        run_sync_job,
        trigger="date",
        run_date=now,
        kwargs={
            "source_id": str(source_id),
            "session_factory": factory,
            "catalog": catalog,
            "settings": settings,
            "trigger": "manual",
        },
        id=job_id,
        replace_existing=True,
        name=f"manual:{source.display_name}",
    )

    response = TriggerResponse(
        sync_run_id=uuid.uuid4(),
        source_id=source_id,
        status="queued",
        trigger="manual",
        started_at=now,
    )
    await idem.persist(ctx, 202, response.model_dump(mode="json"))
    return response


@router.get("/sync-runs", response_model=list[SyncRunResponse], tags=["admin: sync"])
async def list_sync_runs(
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
    source_id: uuid.UUID | None = Query(None),
    run_status: str | None = Query(None, alias="status"),
    from_dt: datetime.datetime | None = Query(None, alias="from"),
    to_dt: datetime.datetime | None = Query(None, alias="to"),
) -> list[SyncRunResponse]:
    """List this tenant's sync runs, optionally filtered by source, status, and a `from`/`to` window."""
    factory = request.app.state.session_factory
    runs = await ingest_queries.list_sync_runs(
        factory,
        ctx.tenant_id,
        source_id=source_id,
        run_status=run_status,
        from_dt=from_dt,
        to_dt=to_dt,
    )
    return [_run_to_response(r) for r in runs]


@router.get(
    "/sync-runs/{sync_run_id}",
    response_model=SyncRunResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    tags=["admin: sync"],
)
async def get_sync_run(
    sync_run_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> SyncRunResponse:
    """Return a single sync run's outcome, 404 if it does not exist or belongs to another tenant."""
    factory = request.app.state.session_factory
    run = await ingest_queries.get_sync_run(factory, sync_run_id)
    if run is None or run.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sync_run not found")
    return _run_to_response(run, include_links=True)


@router.get(
    "/sync-runs/{sync_run_id}/superseded",
    response_model=list[SupersededFactResponse],
    tags=["admin: sync"],
)
async def get_superseded_facts_for_run(
    sync_run_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[SupersededFactResponse]:
    """Return all facts with ``is_authoritative_superseded=TRUE`` for this run."""
    factory = request.app.state.session_factory
    async with factory() as session:
        # Confirm run exists and belongs to this tenant.
        run = await ingest_queries.get_sync_run_for_update(session, sync_run_id)
        if run is None or run.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sync_run not found")

        facts = await ingest_queries.list_superseded_facts(session, tenant_id=ctx.tenant_id, sync_run_id=sync_run_id)

    return [
        SupersededFactResponse(
            fact_id=f.fact_id,
            entity_id=f.entity_id,
            category=f.category,
            body=f.body,
            sync_run_id=f.sync_run_id,
            t_valid_from=f.t_valid_from,
            t_ingested_at=f.t_ingested_at,
        )
        for f in facts
    ]


_mutation_base = APIRouter(prefix="/v1/admin")
_mode, _sep = get_mode_settings()
_mutation_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mutation_mr.add_mutation_route(
    path="/sync-sources/{source_id}",
    action="update",
    handler=patch_sync_source,
    verb="PATCH",
    response_model=SyncSourceResponse,
    tags=["admin: sync"],
)

_mutation_mr.add_mutation_route(
    path="/sync-sources/{source_id}",
    action="delete",
    handler=delete_sync_source,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["admin: sync"],
)

mutation_router = _mutation_base
