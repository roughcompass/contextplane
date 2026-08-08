"""Interface storage REST endpoints.

  PUT /v1/capabilities/{id}/interface  body: {interface_source, interface_format}
       → 200 + {interface_canonical}
  GET /v1/capabilities/{id}/interface[?as_of=...][?view=audit]
       → 200 + {interface_canonical, interface_source, interface_format, as_of}

Visibility-gated at the service layer. Producer or admin role required for writes;
read uses standard tenant-context visibility (assert_visible).

PUT is registered via HttpMethodRouter so ``CONTEXTPLANE_HTTP_METHODS_MODE``
controls whether a POST-tunneled alias is also exposed, the same as every
other tenant-facing mutation.

``?view=audit`` is accepted for API consistency on the GET endpoint.
The interface GET currently has no additional bitemporal row metadata to expose
beyond ``as_of`` (the service layer returns a composed record rather than raw
attribute rows), so ``view=audit`` is a no-op here. The parameter is defined
so clients can pass a uniform ``?view=audit`` across all endpoints without
special-casing.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel

from contextplane.api.auth.context import ROLE_ADMIN, ROLE_PRODUCER, require_roles
from contextplane.api.errors import map_catalog_error
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.routers._common import (
    ViewParam,
    get_service,
)
from contextplane.api.schemas.catalog import InterfaceReadResponse
from contextplane.api.schemas.common import Links
from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.service.catalog.interface_storage import InterfaceStorageService
from contextplane.service.governance.temporal import normalize_utc
from contextplane.types import TenantContext

_producer_or_admin = require_roles([ROLE_PRODUCER, ROLE_ADMIN])


class InterfacePutRequest(BaseModel):
    interface_source: Any
    interface_format: str


class InterfaceSurfaceResponse(BaseModel):
    operations: list[dict[str, Any]]
    events: list[dict[str, Any]]
    fields: list[dict[str, Any]]


def _svc(request: Request) -> InterfaceStorageService:
    return request.app.state.interface_storage  # type: ignore[no-any-return]


router = APIRouter(prefix="/v1/capabilities", tags=["interface"])


async def put_interface(
    capability_id: Annotated[str, Path(description="Capability UUID or slug")],
    body: InterfacePutRequest,
    request: Request,
    ctx: TenantContext = Depends(_producer_or_admin),
) -> InterfaceSurfaceResponse:
    """Normalize, soft-supersede prior versions, then write the new pair.

    The path segment accepts a UUID or slug-form name.
    """
    catalog_svc = get_service(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, capability_id)
        surface = await _svc(request).put_interface(
            ctx=ctx,
            capability_id=resolved.entity_id,
            interface_source=body.interface_source,
            interface_format=body.interface_format,
        )
    except (NotFoundError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return InterfaceSurfaceResponse(
        operations=surface.operations,
        events=surface.events,
        fields=surface.fields,
    )


# ---------------------------------------------------------------------------
# Mutation route — PUT via HttpMethodRouter
# ---------------------------------------------------------------------------
#
# This is a tenant-facing, producer-driven surface (not an operator admin
# panel), so it is mode-aware like every other agent-facing mutation:
# post_only deployments still need a way to replace a capability's interface.

_mode, _sep = get_mode_settings()
mutation_router = APIRouter(prefix="/v1/capabilities", tags=["interface"])
_mut_mr = HttpMethodRouter(mutation_router, mode=_mode, separator=_sep)

_mut_mr.add_mutation_route(
    path="/{capability_id}/interface",
    # "replace", not "update": PUT replaces the whole interface surface in one
    # write. Every other mutation in this API using "update" is a PATCH partial
    # update; reusing that verb here for a full-replacement PUT would blur a
    # distinction the alias name exists to carry.
    action="replace",
    handler=put_interface,
    verb="PUT",
    response_model=InterfaceSurfaceResponse,
    summary="Replace the capability's declared interface surface",
)


@router.get(
    "/{capability_id}/interface",
    response_model=InterfaceReadResponse,
    response_model_exclude_unset=True,
    response_model_by_alias=True,
    summary="Read the capability's declared interface surface",
)
async def get_interface(
    capability_id: Annotated[str, Path(description="Capability UUID or slug")],
    request: Request,
    as_of: str | None = Query(None, description="ISO-8601 UTC for time-travel"),
    view: ViewParam = "default",
    ctx: TenantContext = Depends(get_tenant_context),
) -> InterfaceReadResponse:
    """Return the active interface surface at ``as_of`` (or current truth).

    The path segment accepts a UUID or slug-form name.

    ``?view=audit`` is accepted for API consistency but is currently a no-op —
    the interface service returns a composed record rather than raw attribute
    rows, so no additional bitemporal metadata is available to surface.
    """

    as_of_dt: datetime.datetime | None = None
    if as_of is not None:
        try:
            as_of_dt = normalize_utc(datetime.datetime.fromisoformat(as_of))
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"as_of must be a timezone-aware ISO-8601 datetime: {exc}",
            ) from exc

    catalog_svc = get_service(request)
    try:
        resolved = await catalog_svc.resolve_entity_handle(ctx, capability_id)
        record = await _svc(request).get_interface(ctx=ctx, capability_id=resolved.entity_id, as_of=as_of_dt)
    except (NotFoundError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc

    canonical_payload: InterfaceSurfaceResponse | None = None
    if record.interface_canonical is not None:
        canonical_payload = InterfaceSurfaceResponse(
            operations=record.interface_canonical.operations,
            events=record.interface_canonical.events,
            fields=record.interface_canonical.fields,
        )

    return InterfaceReadResponse(
        capability_id=capability_id,
        interface_canonical=canonical_payload,
        interface_source=record.interface_source,
        interface_format=record.interface_format,
        as_of=record.as_of.isoformat() if record.as_of else None,
        _links=Links(
            self=f"/v1/capabilities/{capability_id}/interface",
            capability=f"/v1/capabilities/{capability_id}",
        ),
    )


__all__ = ["router", "mutation_router"]
