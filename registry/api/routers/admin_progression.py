"""Admin CRUD endpoints for progression_definitions and progression_overrides.

Progression definition endpoints (five, require admin role):

  POST   /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions
  GET    /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}
  PUT    /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}
  DELETE /v1/admin/tenants/{tenant_id}/progression-definitions/{progression_id}

PUT semantics are supersession, not in-place mutation: a new row is inserted and
the previously-active row for the same (tenant_id, entity_type) has its
t_valid_to set to now in the same transaction.

DELETE is a soft-delete: t_valid_to is set to now; t_invalidated_at remains NULL
(no successor row is created).

PUT and DELETE are registered via HttpMethodRouter, so
``REGISTRY_HTTP_METHODS_MODE`` controls whether POST-tunneled aliases
(``:supersede``, ``:delete``) are also exposed, the same as every other
tenant-admin mutation.

Progression override endpoints (two, require admin role):

  POST   /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides
  GET    /v1/admin/tenants/{tenant_id}/entities/{entity_id}/progression-overrides

Override creation uses audit-before-commit ordering: the audit_log row is written
and committed in its own transaction before the override row is inserted. If the
audit write fails the override is never created; an uncommitted override can never
exist without a committed audit record. The override row stores audit_event_id
pointing at that audit row so the two records are semantically linked.

Audit events emitted:
  - progression.definition.published    (definition POST and PUT)
  - progression.definition.soft_deleted (definition DELETE)
  - progression.override.created        (override POST)
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.api.errors import build_error
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.routers._admin_common import _admin_required
from registry.audit import actions
from registry.exceptions import ValidationError
from registry.service.platform import queries as platform_queries
from registry.service.platform.progression import scan_graduation_offenders, validate_progression_definition
from registry.storage.models import ProgressionDefinition, ProgressionOverride
from registry.types import TenantContext

router = APIRouter(prefix="/v1/admin", tags=["admin: progression"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class ProgressionDefinitionCreate(BaseModel):
    entity_type: str
    definition: dict[str, Any]
    is_advisory: bool = True


class ProgressionDefinitionUpdate(BaseModel):
    definition: dict[str, Any]
    is_advisory: bool | None = None
    # Pre-flight graduation controls — only evaluated when is_advisory flips True→False.
    dry_run: bool = False
    force: bool = False
    migration_plan: str | None = None
    force_timeout_seconds: float = 30.0


class ProgressionDefinitionResponse(BaseModel):
    progression_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    definition: dict[str, Any]
    is_advisory: bool
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime | None
    t_ingested_at: datetime.datetime
    t_invalidated_at: datetime.datetime | None


class ProgressionOverrideCreate(BaseModel):
    from_state: str
    to_state: str
    gate_id: str
    bypass_skip_rules: bool = False
    reason: str
    t_valid_to: datetime.datetime | None = None


class ProgressionOverrideResponse(BaseModel):
    override_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_id: uuid.UUID
    from_state: str
    to_state: str
    gate_id: str
    bypass_skip_rules: bool
    reason: str
    authorized_by: uuid.UUID
    t_valid_from: datetime.datetime
    t_valid_to: datetime.datetime
    consumed_at: datetime.datetime | None
    audit_event_id: uuid.UUID


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _to_response(row: ProgressionDefinition) -> ProgressionDefinitionResponse:
    return ProgressionDefinitionResponse(
        progression_id=row.progression_id,
        tenant_id=row.tenant_id,
        entity_type=row.entity_type,
        definition=dict(row.definition) if row.definition else {},
        is_advisory=row.is_advisory,
        t_valid_from=row.t_valid_from,
        t_valid_to=row.t_valid_to,
        t_ingested_at=row.t_ingested_at,
        t_invalidated_at=row.t_invalidated_at,
    )


def _override_to_response(row: ProgressionOverride) -> ProgressionOverrideResponse:
    return ProgressionOverrideResponse(
        override_id=row.override_id,
        tenant_id=row.tenant_id,
        entity_id=row.entity_id,
        from_state=row.from_state,
        to_state=row.to_state,
        gate_id=row.gate_id,
        bypass_skip_rules=row.bypass_skip_rules,
        reason=row.reason,
        authorized_by=row.authorized_by,
        t_valid_from=row.t_valid_from,
        t_valid_to=row.t_valid_to,
        consumed_at=row.consumed_at,
        audit_event_id=row.audit_event_id,
    )


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


async def _emit_audit(
    session: AsyncSession,
    ctx: TenantContext,
    action: str,
    payload: dict[str, Any],
    now: datetime.datetime,
) -> None:
    """Write one audit_log row for a progression definition admin event.

    Thin wrapper kept here (rather than called directly at each call site) so
    the two audit call sites in this router, and the tests that patch this
    name, do not need to know record_audit_event lives in queries.py.
    """
    await platform_queries.record_audit_event(
        session,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        action=action,
        target_type="progression_definition",
        target_id=uuid.UUID(payload["progression_id"]),
        after_jsonb=payload,
        ts=now,
    )


async def _emit_override_audit(
    session: AsyncSession,
    ctx: TenantContext,
    entity_id: uuid.UUID,
    override_id: uuid.UUID,
    payload: dict[str, Any],
    now: datetime.datetime,
) -> uuid.UUID:
    """Write one audit_log row for a progression override creation event.

    Writes before_jsonb=null, after_jsonb=<override spec>. Returns the new
    audit_id so the caller can store it on the override row (audit-before-commit
    ordering: audit row is committed before the override row is inserted).
    """
    return await platform_queries.record_audit_event(
        session,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        action=actions.PROGRESSION_OVERRIDE_CREATED,
        target_type="progression_override",
        target_id=entity_id,
        after_jsonb=payload,
        ts=now,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/tenants/{tenant_id}/progression-definitions",
    response_model=ProgressionDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_progression_definition(
    tenant_id: uuid.UUID,
    body: ProgressionDefinitionCreate,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> ProgressionDefinitionResponse:
    """Create the first progression definition for a (tenant, entity_type) pair.

    Validates the definition JSONB against the meta-schema before persisting.
    Returns 422 with structured error paths on schema violations.
    Returns 403 if the caller does not hold the admin role.

    The tenant_id in the URL must match the caller's tenant — the admin role
    dependency already resolves the tenant from the token; cross-tenant writes
    are rejected because ctx.tenant_id will not match a different tenant_id path
    parameter (enforced below).
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant write rejected")

    try:
        validate_progression_definition(body.definition)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    now = datetime.datetime.now(tz=datetime.UTC)
    progression_id = uuid.uuid4()

    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        await platform_queries.insert_progression_definition(
            session,
            progression_id=progression_id,
            tenant_id=ctx.tenant_id,
            entity_type=body.entity_type,
            definition=body.definition,
            is_advisory=body.is_advisory,
            now=now,
        )
        await _emit_audit(
            session,
            ctx,
            action=actions.PROGRESSION_DEFINITION_PUBLISHED,
            payload={
                "progression_id": str(progression_id),
                "entity_type": body.entity_type,
                "is_advisory": body.is_advisory,
                "action": "created",
            },
            now=now,
        )

    async with factory() as session:
        row = await platform_queries.get_progression_definition(session, progression_id)
        if row is None:
            raise HTTPException(status_code=500, detail="progression definition row missing after insert")
    return _to_response(row)


@router.get(
    "/tenants/{tenant_id}/progression-definitions",
    response_model=list[ProgressionDefinitionResponse],
)
async def list_progression_definitions(
    tenant_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> list[ProgressionDefinitionResponse]:
    """Return all currently-active progression definitions for the tenant.

    Active means: t_valid_to IS NULL AND t_invalidated_at IS NULL.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant read rejected")

    factory = request.app.state.session_factory
    async with factory() as session:
        rows = await platform_queries.list_progression_definitions(session, tenant_id=ctx.tenant_id)
    return [_to_response(r) for r in rows]


@router.get(
    "/tenants/{tenant_id}/progression-definitions/{progression_id}",
    response_model=ProgressionDefinitionResponse,
)
async def get_progression_definition(
    tenant_id: uuid.UUID,
    progression_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> ProgressionDefinitionResponse:
    """Return a specific progression definition by progression_id.

    Returns 404 if the row does not exist or belongs to a different tenant.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant read rejected")

    factory = request.app.state.session_factory
    async with factory() as session:
        row = await platform_queries.get_progression_definition(session, progression_id)
    if row is None or row.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="progression definition not found")
    return _to_response(row)


async def _load_prior_definition(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    progression_id: uuid.UUID,
) -> ProgressionDefinition:
    """Return the definition being superseded, or raise 404.

    Read outside the write transaction: what this call decides (is this an
    advisory→enforcing graduation, and for which entity_type) only gates
    whether a pre-flight scan runs before any write is attempted.
    """
    async with factory() as session:
        prior = await platform_queries.get_progression_definition(session, progression_id)
    if prior is None or prior.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="progression definition not found")
    return prior


def _resolve_advisory_flip(prior: ProgressionDefinition, body: ProgressionDefinitionUpdate) -> tuple[bool, bool]:
    """Return (new_is_advisory, advisory_flip) for the proposed update.

    advisory_flip is True only for True→False: graduating out of advisory
    mode is the one transition the pre-flight scan exists to protect.
    """
    new_is_advisory = body.is_advisory if body.is_advisory is not None else prior.is_advisory
    advisory_flip = prior.is_advisory is True and new_is_advisory is False
    return new_is_advisory, advisory_flip


async def _run_graduation_preflight(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    entity_type: str,
    body: ProgressionDefinitionUpdate,
) -> Response | None:
    """Enforce the four pre-flight outcomes for an advisory→enforcing graduation.

    Returns a Response to send immediately (the dry_run report), or None to
    signal the caller should proceed to the write. Every other outcome
    (missing migration_plan, scan timeout, offenders present) raises the
    HTTP error the caller propagates unchanged.

    Only called when advisory_flip is True — a non-flip PUT never scans.
    """
    if body.force and not body.migration_plan:
        # Path B (guard) — force without migration_plan is a caller error, not
        # a finding: nothing was scanned, so this raises before Path D/E can.
        raise build_error(
            status.HTTP_400_BAD_REQUEST,
            code="migration_plan_required",
            message="force=true requires migration_plan to be provided",
        )

    if body.force and body.migration_plan:
        # Path D — operator has accepted the risk; skip pre-flight, write immediately.
        return None

    # Path A/B/C/E — run the scan, bounded by force_timeout_seconds.
    try:
        offenders = await asyncio.wait_for(
            scan_graduation_offenders(
                factory,
                tenant_id=ctx.tenant_id,
                entity_type=entity_type,
                definition=body.definition,
            ),
            timeout=body.force_timeout_seconds,
        )
    except TimeoutError:
        # Path C — partial results; nothing is written on a bounded scan that
        # didn't finish, so there is nothing partial to report except "unknown".
        raise build_error(
            status.HTTP_409_CONFLICT,
            code="preflight_timeout",
            message="pre-flight scan exceeded force_timeout_seconds; no definition written",
        ) from None

    offender_dicts = [dataclasses.asdict(o) for o in offenders]

    if body.dry_run:
        # Path A — report findings without writing.
        return Response(
            content=json.dumps({"dry_run": True, "offenders": offender_dicts}),
            status_code=status.HTTP_200_OK,
            media_type="application/json",
        )

    if offender_dicts:
        # Path B — blocked; caller must use force=True. Offenders are encoded
        # into the message as JSON so they survive the error envelope
        # normalisation and remain accessible to the caller.
        raise build_error(
            status.HTTP_409_CONFLICT,
            code="preflight_offenders_present",
            message=json.dumps(
                {
                    "offenders": offender_dicts,
                    "hint": "Pass force=true with migration_plan to proceed.",
                }
            ),
        )

    # Path E — zero offenders; proceed to write.
    return None


async def _write_supersession(
    factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    *,
    progression_id: uuid.UUID,
    new_progression_id: uuid.UUID,
    entity_type: str,
    body: ProgressionDefinitionUpdate,
    new_is_advisory: bool,
    advisory_flip: bool,
    now: datetime.datetime,
) -> None:
    """Close the active row for (tenant_id, entity_type) and insert its successor.

    One transaction: reloading the prior row here (rather than trusting the
    pre-flight read) closes the TOCTOU gap a concurrent supersession would
    otherwise open between that read and this write.
    """
    async with factory() as session, session.begin():
        prior_write = await platform_queries.get_progression_definition(session, progression_id)
        if prior_write is None or prior_write.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="progression definition not found")

        await platform_queries.close_active_progression_definitions(
            session, tenant_id=ctx.tenant_id, entity_type=entity_type, now=now
        )
        await platform_queries.insert_progression_definition(
            session,
            progression_id=new_progression_id,
            tenant_id=ctx.tenant_id,
            entity_type=entity_type,
            definition=body.definition,
            is_advisory=new_is_advisory,
            now=now,
        )

        # Build audit payload. When the operator force-bypassed pre-flight with a
        # migration_plan, include the plan in the audit record so the bypass is
        # visible in the audit log and traceable.
        audit_payload: dict[str, Any] = {
            "progression_id": str(new_progression_id),
            "superseded_id": str(progression_id),
            "entity_type": entity_type,
            "is_advisory": new_is_advisory,
            "action": "superseded",
        }
        if advisory_flip and body.force and body.migration_plan:
            audit_payload["migration_plan"] = body.migration_plan

        await _emit_audit(
            session,
            ctx,
            action=actions.PROGRESSION_DEFINITION_PUBLISHED,
            payload=audit_payload,
            now=now,
        )


async def supersede_progression_definition(
    tenant_id: uuid.UUID,
    progression_id: uuid.UUID,
    body: ProgressionDefinitionUpdate,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> ProgressionDefinitionResponse:
    """Supersede a progression definition — inserts a new row and closes the active one.

    The progression_id in the URL identifies which active definition to supersede.
    A new row is inserted; the previously-active row for the same (tenant_id,
    entity_type) has its t_valid_to set to now. Both writes happen in a single
    transaction so there is never a gap or overlap in the validity window.

    When the incoming body flips is_advisory from True to False, a pre-flight scan
    runs before writing. The scan validates every entity of the same (tenant_id,
    entity_type) against the proposed enforcing definition and collects offenders.
    Four outcome paths:

    - dry_run=True: return 200 with offender list; do NOT write.
    - force=True + migration_plan: skip scan, write immediately; migration_plan is
      recorded in the audit payload so the bypass is discoverable.
    - force=True without migration_plan: return 400.
    - Scan times out (force_timeout_seconds exceeded): return 409 with partial results.
    - Offenders found with force=False: return 409 with offender list.
    - Zero offenders: write normally.

    Validates the new definition JSONB before writing.
    Emits audit event progression.definition.published.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant write rejected")

    try:
        validate_progression_definition(body.definition)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    now = datetime.datetime.now(tz=datetime.UTC)
    new_progression_id = uuid.uuid4()
    factory = request.app.state.session_factory

    prior = await _load_prior_definition(factory, ctx, progression_id)
    entity_type = prior.entity_type
    new_is_advisory, advisory_flip = _resolve_advisory_flip(prior, body)

    if advisory_flip:
        early_response = await _run_graduation_preflight(factory, ctx, entity_type, body)
        if early_response is not None:
            return early_response  # type: ignore[return-value]

    await _write_supersession(
        factory,
        ctx,
        progression_id=progression_id,
        new_progression_id=new_progression_id,
        entity_type=entity_type,
        body=body,
        new_is_advisory=new_is_advisory,
        advisory_flip=advisory_flip,
        now=now,
    )

    async with factory() as session:
        row = await platform_queries.get_progression_definition(session, new_progression_id)
        if row is None:
            raise HTTPException(status_code=500, detail="progression definition row missing after supersession")
    return _to_response(row)


async def soft_delete_progression_definition(
    tenant_id: uuid.UUID,
    progression_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> Response:
    """Soft-delete a progression definition by setting t_valid_to = now.

    No successor row is inserted. t_invalidated_at remains NULL.
    Emits audit event progression.definition.soft_deleted.
    Returns 404 if not found or not owned by this tenant.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant write rejected")

    now = datetime.datetime.now(tz=datetime.UTC)

    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        row = await platform_queries.get_progression_definition(session, progression_id)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="progression definition not found")
        row.t_valid_to = now
        await session.flush()
        await _emit_audit(
            session,
            ctx,
            action=actions.PROGRESSION_DEFINITION_SOFT_DELETED,
            payload={
                "progression_id": str(progression_id),
                "entity_type": row.entity_type,
                "action": "soft_deleted",
            },
            now=now,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Mutation routes — PUT/DELETE via HttpMethodRouter
# ---------------------------------------------------------------------------
#
# Same tenant-admin gate (_admin_required) and /v1/admin prefix as the
# vocabulary/schema/sync/extraction admin surfaces, all of which are already
# mode-switched. There is no service-operator surface in this API — every
# "admin: …" section is tenant-admin — so these two get the same treatment.
# Wrapping `router` itself (rather than a separate mutation router) keeps
# wiring/routes.py's single `include_router(router)` call sufficient.

_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)

_mr.add_mutation_route(
    path="/tenants/{tenant_id}/progression-definitions/{progression_id}",
    action="supersede",
    handler=supersede_progression_definition,
    verb="PUT",
    response_model=ProgressionDefinitionResponse,
    status_code=status.HTTP_200_OK,
)

_mr.add_mutation_route(
    path="/tenants/{tenant_id}/progression-definitions/{progression_id}",
    action="delete",
    handler=soft_delete_progression_definition,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)


# ---------------------------------------------------------------------------
# Override endpoints
# ---------------------------------------------------------------------------

_ONE_HOUR = datetime.timedelta(hours=1)


@router.post(
    "/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
    response_model=ProgressionOverrideResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_progression_override(
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    body: ProgressionOverrideCreate,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> ProgressionOverrideResponse:
    """Create a single-use progression gate override for a specific entity.

    Follows audit-before-commit ordering: the audit_log row is written and
    committed in its own transaction before the override row is inserted.
    If the audit write fails the override is never created — a silently-created
    override with no audit record is structurally impossible.

    Default t_valid_to: now + 1 hour when the caller omits the field.
    Default bypass_skip_rules: False — must be an explicit opt-in.
    authorized_by is always set to the actor making the request.
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant write rejected")

    now = datetime.datetime.now(tz=datetime.UTC)
    t_valid_to = body.t_valid_to if body.t_valid_to is not None else now + _ONE_HOUR
    override_id = uuid.uuid4()

    factory = request.app.state.session_factory

    # Step 1: write audit row in its own committed transaction.
    # If this fails we raise HTTP 500 and never reach the override insert.
    audit_payload: dict[str, Any] = {
        "override_id": str(override_id),
        "entity_id": str(entity_id),
        "from_state": body.from_state,
        "to_state": body.to_state,
        "gate_id": body.gate_id,
        "bypass_skip_rules": body.bypass_skip_rules,
        "reason": body.reason,
        "authorized_by": str(ctx.actor_id),
        "t_valid_to": t_valid_to.isoformat(),
    }
    try:
        async with factory() as audit_session, audit_session.begin():
            audit_id = await _emit_override_audit(
                audit_session,
                ctx,
                entity_id,
                override_id,
                audit_payload,
                now,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="audit write failed; override not created",
        ) from exc

    # Step 2: insert override row referencing the committed audit row.
    async with factory() as session, session.begin():
        await platform_queries.insert_progression_override(
            session,
            override_id=override_id,
            tenant_id=ctx.tenant_id,
            entity_id=entity_id,
            from_state=body.from_state,
            to_state=body.to_state,
            gate_id=body.gate_id,
            bypass_skip_rules=body.bypass_skip_rules,
            reason=body.reason,
            authorized_by=ctx.actor_id,
            t_valid_from=now,
            t_valid_to=t_valid_to,
            audit_event_id=audit_id,
        )

    async with factory() as session:
        row = await platform_queries.get_progression_override(session, override_id)
        if row is None:
            raise HTTPException(status_code=500, detail="override row missing after insert")
    return _override_to_response(row)


@router.get(
    "/tenants/{tenant_id}/entities/{entity_id}/progression-overrides",
    response_model=list[ProgressionOverrideResponse],
)
async def list_progression_overrides(
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
    consumed: bool | None = Query(default=None),
    expired: bool | None = Query(default=None),
    from_state: str | None = Query(default=None),
    to_state: str | None = Query(default=None),
) -> list[ProgressionOverrideResponse]:
    """List progression overrides for an entity with optional filters.

    Query parameters:
      consumed=true   — only overrides where consumed_at IS NOT NULL
      consumed=false  — only overrides where consumed_at IS NULL
      expired=true    — only overrides where t_valid_to < now()
      expired=false   — only overrides where t_valid_to >= now()
      from_state      — exact match on from_state
      to_state        — exact match on to_state
    """
    if ctx.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-tenant read rejected")

    now = datetime.datetime.now(tz=datetime.UTC)

    factory = request.app.state.session_factory
    async with factory() as session:
        rows = await platform_queries.list_progression_overrides(
            session,
            tenant_id=ctx.tenant_id,
            entity_id=entity_id,
            consumed=consumed,
            expired=expired,
            from_state=from_state,
            to_state=to_state,
            now=now,
        )
    return [_override_to_response(r) for r in rows]
