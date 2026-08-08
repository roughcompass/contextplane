"""Admin endpoint for workspace actor personal-data purge (RTBF).

Exposes a single endpoint:

  DELETE /v1/admin/actors/{actor_id}/personal-data

This is a hard-delete operation (not a soft-delete). It physically removes all
workspace content authored by the target actor. The operation is idempotent:
a second invocation on the same actor_id returns counts of 0 (nothing left
to purge).

Requires admin role. The requesting admin is recorded in the audit log.

The endpoint returns 200 (not 204) because the counts in PurgeResult are
informative for the admin caller — they confirm what was actually deleted.

Registered via HttpMethodRouter, so ``CONTEXTPLANE_HTTP_METHODS_MODE`` controls
whether a POST-tunneled alias (``:purge``) is also exposed, the same as
every other tenant-admin mutation.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from contextplane.api.container import Services
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.routers._admin_common import _admin_required
from contextplane.service.workspace import WorkspaceService
from contextplane.service.workspace.purge import PurgeResult
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/admin", tags=["admin: workspaces"])


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------


class PurgeResultResponse(BaseModel):
    """JSON shape returned by DELETE /v1/admin/actors/{actor_id}/personal-data.

    `purged_entries` and `purged_workspaces` are the workspace subsystem's
    counts and are kept at the top level for callers that predate erasure
    reaching anything else.

    `subsystems` is the complete picture: one entry per subsystem the request
    reached, with that subsystem's own vocabulary for what it removed. A caller
    confirming an erasure should read this rather than the two flat counts,
    which describe one subsystem out of several.
    """

    purged_entries: int
    purged_workspaces: int
    subsystems: dict[str, dict[str, int]] = {}


# ---------------------------------------------------------------------------
# Service dependency
# ---------------------------------------------------------------------------


def _get_workspace_service(request: Request) -> WorkspaceService:
    """Return the singleton WorkspaceService stored on app.state.

    The singleton is built once at app startup by the workspace router's
    _build_workspace_service factory. All callers — this RTBF endpoint and
    the main workspace/entry CRUD router — share the same instance.
    """
    return request.app.state.workspace_service  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


async def delete_actor_personal_data(
    actor_id: uuid.UUID,
    request: Request,
    ctx: TenantContext = Depends(_admin_required),
) -> PurgeResultResponse:
    """Physically delete all workspace content authored by actor_id.

    This endpoint fulfills right-to-be-forgotten (RTBF) requests for workspace
    data. It performs a hard DELETE (not a soft-delete) across:

    - workspace_entries created by the actor
    - actor-owned workspaces (cascading their entries) once the actor is gone

    Workspaces never cross tenant boundaries — there is no separate share
    revocation step; tenant-owned workspaces in tenants the actor was a
    member of remain intact and are governed by their tenant's roster.

    The operation is idempotent. A second call returns counts of 0.

    Returns 200 with PurgeResult counts (not 204) so the admin caller can
    confirm what was actually purged.

    Raises 403 if the caller does not hold the admin role.
    """
    services: Services = request.app.state.services
    registry = services.erasure
    if registry is None:
        # No registry wired: fall back to the workspace path alone rather than
        # refusing. An erasure request that errors is worse than one that
        # covers less, and the response says which subsystems were reached.
        workspace_svc = _get_workspace_service(request)
        result: PurgeResult = await workspace_svc.purge_actor_personal_data(ctx, target_actor_id=actor_id)
        return PurgeResultResponse(
            purged_entries=result.purged_entries,
            purged_workspaces=result.purged_workspaces,
            subsystems={"workspace": {"entries": result.purged_entries, "workspaces": result.purged_workspaces}},
        )

    counts = await registry.erase_actor(ctx, actor_id)
    by_subsystem = {c.subsystem: c.removed for c in counts}
    workspace = by_subsystem.get("workspace", {})
    return PurgeResultResponse(
        purged_entries=workspace.get("entries", 0),
        purged_workspaces=workspace.get("workspaces", 0),
        subsystems=by_subsystem,
    )


# ---------------------------------------------------------------------------
# Mutation route — DELETE via HttpMethodRouter
# ---------------------------------------------------------------------------
#
# Same tenant-admin gate (_admin_required) and /v1/admin prefix as the
# vocabulary/schema/sync/extraction admin surfaces, all of which are already
# mode-switched. There is no service-operator surface in this API — every
# "admin: …" section is tenant-admin — so this endpoint gets the same
# treatment. Wrapping `router` itself (rather than a separate mutation
# router) keeps wiring/routes.py's single `include_router(router)` call
# sufficient.

_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)

_mr.add_mutation_route(
    path="/actors/{actor_id}/personal-data",
    action="purge",
    handler=delete_actor_personal_data,
    verb="DELETE",
    response_model=PurgeResultResponse,
    status_code=status.HTTP_200_OK,
    summary="Purge all workspace personal data for an actor (RTBF).",
)
