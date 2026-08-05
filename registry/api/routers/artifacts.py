"""POST/GET/DELETE /v1/capabilities/{entity_id}/artifacts — facts attached to capabilities.

DELETE is registered via HttpMethodRouter so REGISTRY_HTTP_METHODS_MODE controls
the exposed verb surface.

The PII scanner runs on every artifact body before writing:
  - Queries pii_patterns.policy_override and pii_field_policies for the tenant.
  - field_type = "artifact.body"
  - action_taken == "block" → 422 before the fact row is written.
  - All matches written to pii_detection_log unconditionally.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from registry.api.auth.context import ROLE_ADMIN, ROLE_PRODUCER, require_roles
from registry.api.cursor import InvalidCursorError, decode_cursor, encode_cursor
from registry.api.errors import build_error, map_catalog_error
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from registry.api.middleware.tenant import get_tenant_context
from registry.api.pii_guard import run_pii_scan
from registry.api.routers._common import (
    ViewParam,
    get_service,
)
from registry.api.schemas import ArtifactListResponse, ArtifactResponse, CreateArtifactRequest, Links
from registry.exceptions import CatalogError, NotFoundError
from registry.service.catalog import queries as catalog_queries
from registry.storage.models import Fact
from registry.types import FactRef, TenantContext

_PII_ARTIFACT_FIELD = "artifact.body"

# Producer or admin required to attach or remove artifacts from capabilities.
# Consumers and auditors may only read.
_producer_or_admin = require_roles([ROLE_PRODUCER, ROLE_ADMIN])


router = APIRouter(prefix="/v1/capabilities/{entity_id}/artifacts", tags=["artifacts"])


_DEFAULT_LIST_FIELDS: frozenset[str] = frozenset(
    {"fact_id", "category", "title", "body_format", "created_at", "created_by_display_name"},
)
_DEFAULT_GET_FIELDS: frozenset[str] = frozenset(
    {"fact_id", "category", "title", "body", "body_format", "created_at", "created_by_display_name"},
)
_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {"fact_id", "category", "title", "body", "body_format", "created_at", "created_by_display_name"},
)


def _parse_fields(fields: str | None, default: frozenset[str]) -> frozenset[str]:
    """Parse the `?fields=` CSV. 422 on unknown values; default when absent."""
    if fields is None or not fields.strip():
        return default
    requested = {f.strip() for f in fields.split(",") if f.strip()}
    unknown = requested - _ALLOWED_FIELDS
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f"unknown fields: {sorted(unknown)}. " f"Known: {sorted(_ALLOWED_FIELDS)}"),
        )
    # fact_id is always included so the response is addressable.
    return frozenset(requested | {"fact_id"})


def _to_response(
    fact: FactRef,
    *,
    audit: bool = False,
    fields: frozenset[str] | None = None,
    created_by_display_name: str | None = None,
    entity_handle: str | None = None,
) -> ArtifactResponse:
    """Convert a FactRef to the artifact response shape.

    The ``fields`` set selects which UI columns are populated; missing
    columns stay unset (and are excluded by ``response_model_exclude_unset``).
    ``audit=True`` additionally fills in the bitemporal + tenant columns.
    ``entity_handle`` is the address form the caller used (slug or UUID);
    URLs in ``_links`` mirror that form.
    """
    selected = fields if fields is not None else _DEFAULT_GET_FIELDS
    kwargs: dict[str, object] = {"fact_id": fact.fact_id}
    if "category" in selected:
        kwargs["category"] = fact.category
    if "title" in selected:
        kwargs["title"] = fact.title
    if "body" in selected:
        kwargs["body"] = fact.body
    if "body_format" in selected:
        kwargs["body_format"] = fact.body_format
    if "created_at" in selected:
        kwargs["created_at"] = fact.t_ingested_at
    if "created_by_display_name" in selected:
        kwargs["created_by_display_name"] = created_by_display_name

    if audit:
        kwargs.update(
            tenant_id=fact.tenant_id,
            entity_id=fact.entity_id,
            is_authoritative=fact.is_authoritative,
            valid_from=fact.t_valid_from,
            valid_to=fact.t_valid_to,
            ingested_at=fact.t_ingested_at,
            invalidated_at=fact.t_invalidated_at,
        )

    # _links — caller-address-form aware. The parent entity uses the
    # handle the caller passed in (slug or UUID); the artifact itself
    # uses its fact_id since artifacts are UUID-addressed.
    eh = entity_handle if entity_handle is not None else str(fact.entity_id)
    kwargs["links"] = Links(
        self=f"/v1/capabilities/{eh}/artifacts/{fact.fact_id}",
        capability=f"/v1/capabilities/{eh}",
    )
    return ArtifactResponse(**kwargs)  # type: ignore[arg-type]


def _row_to_factref(f: Fact) -> FactRef:
    return FactRef(
        fact_id=f.fact_id,
        tenant_id=f.tenant_id,
        entity_id=f.entity_id,
        category=f.category,
        body=f.body,
        is_authoritative=f.is_authoritative,
        is_authoritative_superseded=f.is_authoritative_superseded,
        sync_run_id=f.sync_run_id,
        t_valid_from=f.t_valid_from,
        t_valid_to=f.t_valid_to,
        t_ingested_at=f.t_ingested_at,
        t_invalidated_at=f.t_invalidated_at,
        title=f.title,
        body_format=f.body_format,
        created_by=f.created_by,
    )


@router.post(
    "",
    response_model=ArtifactResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_artifact(
    entity_id: Annotated[
        str,
        Path(description="Capability UUID or slug-form name (e.g. 'salt-design-system')"),
    ],
    body: CreateArtifactRequest,
    request: Request,
    view: ViewParam = "default",
    idem: IdempotencyContext = Depends(get_idempotency_context),
    ctx: TenantContext = Depends(_producer_or_admin),
) -> ArtifactResponse:
    from fastapi.responses import JSONResponse

    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    audit = view == "audit"
    # PII scan on artifact body before writing — raises HTTP 422 if action_taken == 'block'.
    await run_pii_scan(request, ctx, body.body, _PII_ARTIFACT_FIELD)

    service = get_service(request)
    try:
        resolved = await service.resolve_entity_handle(ctx, entity_id)
        fact = await service.create_fact(
            ctx,
            entity_id=resolved.entity_id,
            category=body.category,
            body=body.body,
            valid_from=body.valid_from,
            title=body.title,
            body_format=body.body_format,
        )
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc

    # Resolve actor display name for the creator.
    factory = request.app.state.session_factory
    async with factory() as session:
        names = await catalog_queries.resolve_actor_names(session, {fact.created_by} if fact.created_by else set())
    response = _to_response(
        fact,
        audit=audit,
        fields=_DEFAULT_GET_FIELDS,
        created_by_display_name=names.get(fact.created_by) if fact.created_by else None,
        entity_handle=entity_id,
    )
    await idem.persist(ctx, 201, response.model_dump(mode="json"))
    return response


@router.get("", response_model=ArtifactListResponse, response_model_exclude_unset=True, response_model_by_alias=True)
async def list_artifacts(
    entity_id: Annotated[
        str,
        Path(description="Capability UUID or slug-form name (e.g. 'salt-design-system')"),
    ],
    request: Request,
    view: ViewParam = "default",
    category: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated list of categories to filter by (e.g. " "'overview,release_note'). Default: no filter."
            ),
        ),
    ] = None,
    fields: Annotated[
        str | None,
        Query(
            description=(
                "Sparse-field selection. Default for list: "
                "fact_id,category,title,body_format,created_at,created_by_display_name "
                "(body excluded). Add `body` explicitly to include it. "
                "Allowed: fact_id,category,title,body,body_format,created_at,created_by_display_name."
            ),
        ),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(
            description="Opaque cursor returned by the previous page. Omit to start from the first page.",
        ),
    ] = None,
    page_size: Annotated[int, Query(ge=1, le=200, description="Items per page (max 200)")] = 20,
    page: Annotated[
        int | None,
        Query(
            description="Deprecated offset page number. Not accepted — use cursor= instead.",
            include_in_schema=False,
        ),
    ] = None,
    ctx: TenantContext = Depends(get_tenant_context),
) -> ArtifactListResponse:
    """List artifacts for a capability, ordered by ingestion time descending.

    Pagination is keyset-based: the response carries ``next_cursor`` (or null
    when no further pages exist). Pass ``cursor=<value>`` on the next request
    to retrieve the following page.

    The legacy ``?page=N`` offset parameter is no longer accepted. Clients
    that send it receive a 422 with code ``page_param_deprecated``.
    """
    if page is not None:
        raise build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="page_param_deprecated",
            message=(
                "The ?page= offset parameter is not accepted. "
                "Use cursor= pagination instead: omit cursor for the first page, "
                "then pass the next_cursor value returned in each response."
            ),
        )

    try:
        cursor_payload = decode_cursor(cursor, strict=cursor is not None)
    except InvalidCursorError as exc:
        raise build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="invalid_cursor",
            message="The cursor value is invalid or has been tampered with.",
        ) from exc

    audit = view == "audit"
    selected = _parse_fields(fields, _DEFAULT_LIST_FIELDS)
    category_filter = [c.strip() for c in category.split(",") if c.strip()] if category else None

    service = get_service(request)
    try:
        resolved = await service.resolve_entity_handle(ctx, entity_id)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc

    # Keyset predicate on (t_ingested_at DESC, fact_id DESC).
    # When a cursor is present, skip rows at or after the cursor position.
    cursor_before: tuple[datetime.datetime, str] | None = None
    if cursor_payload:
        cursor_ts = cursor_payload.get("ts")
        cursor_id = cursor_payload.get("id")
        if cursor_ts and cursor_id:
            cursor_before = (datetime.datetime.fromisoformat(cursor_ts), cursor_id)

    factory = request.app.state.session_factory
    async with factory() as session:
        facts = await catalog_queries.list_artifacts(
            session,
            tenant_id=ctx.tenant_id,
            entity_id=resolved.entity_id,
            category_filter=category_filter,
            cursor_before=cursor_before,
            page_size=page_size,
        )

        has_more = len(facts) > page_size
        page_facts = facts[:page_size]

        # Bulk-resolve display names for all distinct creators.
        creator_ids = {f.created_by for f in page_facts if f.created_by}
        names = await catalog_queries.resolve_actor_names(session, creator_ids)

    next_cursor: str | None = None
    if has_more and page_facts:
        last = page_facts[-1]
        next_cursor = encode_cursor({"ts": last.t_ingested_at.isoformat(), "id": str(last.fact_id)})

    return ArtifactListResponse(
        items=[
            _to_response(
                _row_to_factref(f),
                audit=audit,
                fields=selected,
                created_by_display_name=names.get(f.created_by) if f.created_by else None,
                entity_handle=entity_id,
            )
            for f in page_facts
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/{fact_id}", response_model=ArtifactResponse, response_model_exclude_unset=True, response_model_by_alias=True
)
async def get_artifact(
    entity_id: Annotated[
        str,
        Path(description="Capability UUID or slug-form name (e.g. 'salt-design-system')"),
    ],
    fact_id: uuid.UUID,
    request: Request,
    view: ViewParam = "default",
    fields: Annotated[
        str | None,
        Query(
            description=(
                "Sparse-field selection. Default for get: all UI fields including body. "
                "Allowed: fact_id,category,title,body,body_format,created_at,created_by_display_name."
            ),
        ),
    ] = None,
    ctx: TenantContext = Depends(get_tenant_context),
) -> ArtifactResponse:
    audit = view == "audit"
    selected = _parse_fields(fields, _DEFAULT_GET_FIELDS)
    service = get_service(request)
    try:
        resolved = await service.resolve_entity_handle(ctx, entity_id)
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc

    factory = request.app.state.session_factory
    async with factory() as session:
        fact = await catalog_queries.get_fact(session, fact_id)
        if fact is None or fact.tenant_id != ctx.tenant_id or fact.entity_id != resolved.entity_id:
            raise map_catalog_error(NotFoundError(f"artifact {fact_id} not found"))
        names = await catalog_queries.resolve_actor_names(session, {fact.created_by} if fact.created_by else set())

    return _to_response(
        _row_to_factref(fact),
        audit=audit,
        fields=selected,
        created_by_display_name=names.get(fact.created_by) if fact.created_by else None,
        entity_handle=entity_id,
    )


# ---------------------------------------------------------------------------
# Mutation handler — registered via HttpMethodRouter
# ---------------------------------------------------------------------------


async def delete_artifact(
    entity_id: str,
    fact_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_producer_or_admin),
) -> Response:
    """Soft-delete idempotency: 204 on first or repeat delete; 404 on never-existing.

    Path segment accepts UUID or slug-form name.
    """
    service = get_service(request)
    try:
        # Resolve the parent — if the slug doesn't resolve, the fact under it
        # implicitly doesn't exist either.
        await service.resolve_entity_handle(ctx, entity_id)
        await service.delete_fact(ctx, fact_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CatalogError as exc:
        raise map_catalog_error(exc) from exc
    _ = entity_id  # path scope; isolation enforced by service-layer tenant check
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Mutation router — included separately in main.py
# ---------------------------------------------------------------------------

_mutation_base = APIRouter(prefix="/v1/capabilities/{entity_id}/artifacts", tags=["artifacts"])
_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(_mutation_base, mode=_mode, separator=_sep)

_mr.add_mutation_route(
    path="/{fact_id}",
    action="delete",
    handler=delete_artifact,
    verb="DELETE",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)

mutation_router = _mutation_base
