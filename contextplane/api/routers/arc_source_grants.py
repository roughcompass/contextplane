"""Withdrawing a source grant — the half of the lifecycle that did not exist.

`arc_source_connectors` and `arc_source_upload_policies` had no revocation
column, no route, and no query that could write one. A connector names the
schemes, hosts and media types ARC may fetch, and in `allowed_verifier_ids` it
names *who may approve what it fetches* — for every future fetch. So the widest
control in this surface was the one with no off switch, and a permissive
connector registered during a migration was permanent (E14-T2).

**Why only the revocations are here, and not the registrations.** The register
routes go through `HttpMethodRouter`'s mode/separator contract, which
`arc_admin.py`'s own docstring explains and which a move would have to carry
intact. That is a separate change with its own risk, and bundling it into the
one that adds withdrawal would make a diff nobody can review as either. The
asymmetry is deliberate and temporary: `SourceGrantService` already owns both
halves, so the routes can join it whenever that move is worth making on its own.

The ceiling is what forced the question — `arc_admin.py` reached exactly 800
lines — but the answer would be the same file either way.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from contextplane.api.container import Services
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.routers.arc_admin import _Accepted, _arc_context, _translate
from contextplane.arc import SourceGrantService
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/arc/admin", tags=["arc: admin"])


def _source_grants(request: Request) -> SourceGrantService:
    """The grant lifecycle, separate from the path that uses a grant."""
    services: Services = request.app.state.services
    grants: SourceGrantService = services.arc_source_grants
    return grants


class RevokeGrantRequest(BaseModel):
    """Why a standing source grant is being withdrawn."""

    reason: str = Field(
        min_length=20,
        max_length=2000,
        description="A withdrawn grant with no stated cause is unreviewable afterwards.",
    )


@router.post("/source-connectors/{connector_id}/revoke", response_model=_Accepted)
async def revoke_source_connector(
    request: Request,
    body: RevokeGrantRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    connector_id: Annotated[str, Path()],
) -> _Accepted:
    """Withdraw a connector. It admits nothing further; what it admitted stands.

    Until this existed a connector could not be withdrawn at all — no column, no
    route, no query — which made the widest control in this surface the one with
    no off switch. A connector names who may approve everything it fetches, so a
    permissive one registered during a migration was permanent.

    **The withdrawal reaches forward only.** Material already admitted was
    validly admitted, and `revoked_at` is what lets an auditor place any
    admission on one side of it or the other.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _source_grants(request).revoke_connector(arc_ctx, connector_id=connector_id, reason=body.reason)
    except Exception as exc:
        raise _translate(exc) from exc
    return _Accepted(status="revoked")


@router.post("/source-upload-policies/{policy_id}/revoke", response_model=_Accepted)
async def revoke_source_upload_policy(
    request: Request,
    body: RevokeGrantRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    policy_id: Annotated[str, Path()],
) -> _Accepted:
    """Withdraw an upload policy. Same rule and same reach as a connector."""
    arc_ctx = _arc_context(request, ctx)
    try:
        await _source_grants(request).revoke_upload_policy(arc_ctx, policy_id=policy_id, reason=body.reason)
    except Exception as exc:
        raise _translate(exc) from exc
    return _Accepted(status="revoked")


__all__ = ["router"]
