"""Reading back the ARC governance objects, which for a release nothing could.

Its own module rather than more of `arc_admin.py`, on that file's own splitting
basis and on a cleaner line than the one that forced it: `arc_admin.py` is the
*write* surface — enrol, grant, revoke, invalidate — and these are reads. The
800-line ceiling is what made the split urgent; reads and writes being different
jobs is why it lands here rather than as an extraction of convenience.

**One shape, per-object queries.** `GovernanceObjectResponse` is a contract so
four screens read alike. It is not produced by a union: the six ARC governance
objects agree on intent and disagree on schema — three spellings of scope, three
of tenant, and three notions of "in force", one of which does not exist —
and normalising in-force for the object that cannot be revoked would mean
inventing a state the schema does not have.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from contextplane.api.container import Services
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.arc import GovernanceObject
from contextplane.types import TenantContext

# The same tag as `arc_admin.py`, deliberately. This module is a *file* split --
# reads are a different job from writes and the ceiling made it urgent -- but
# `GET` and `POST` on `/approval-verifiers` are the same resource, and a path
# whose methods sit under different tags lands in neither section of the
# contract. The split is for readers of the code, not readers of the API.
router = APIRouter(prefix="/v1/arc/admin", tags=["arc: admin"])


class GovernanceObjectResponse(BaseModel):
    """One governed object, in the shape every kind answers in.

    Shared because four screens read these alike, and *not* produced by a union
    query: the six ARC governance objects agree on intent and disagree on schema
    — three spellings of scope, three of tenant, and three notions of "in force",
    one of which does not exist. The shape is a contract; normalising the tables
    behind it would mean inventing a state for the object that cannot be revoked.
    """

    kind: str
    object_id: str
    scope: str
    target_tenant_id: uuid.UUID | None
    #: Computed from the row's own columns, never stored, so one place decides
    #: what "in force" means and four callers cannot each decide differently.
    in_force: bool
    #: Null when nothing ends it. That is a state — an open-ended exception is a
    #: policy change wearing a smaller word — not a missing value.
    in_force_until: datetime.datetime | None
    created_at: datetime.datetime
    detail: dict[str, Any]


class GovernanceObjectListResponse(BaseModel):
    items: list[GovernanceObjectResponse]


def _governance_response(found: list[GovernanceObject]) -> GovernanceObjectListResponse:
    return GovernanceObjectListResponse(
        items=[
            GovernanceObjectResponse(
                kind=item.kind,
                object_id=item.object_id,
                scope=item.scope,
                target_tenant_id=item.target_tenant_id,
                in_force=item.in_force,
                in_force_until=item.in_force_until,
                created_at=item.created_at,
                detail=dict(item.detail),
            )
            for item in found
        ]
    )


@router.get("/approval-verifiers", response_model=GovernanceObjectListResponse)
async def list_approval_verifiers(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    in_force_only: bool = False,
) -> GovernanceObjectListResponse:
    """Enrolled verifiers visible to this tenant, newest first.

    Includes the **global** verifiers as well as the tenant's own, because a
    global verifier can approve for this tenant and a list that omitted them
    would answer "who may approve here" wrongly by exactly the set that matters
    most.

    The public key is not returned. A list of who may approve does not need the
    material, and a surface that carried it would be one more place it can leak.
    """
    services: Services = request.app.state.services
    found = await services.arc_governance_reads.list_approval_verifiers(
        tenant_id=ctx.tenant_id, in_force_only=in_force_only
    )
    return _governance_response(found)


@router.get("/exceptions", response_model=GovernanceObjectListResponse)
async def list_approved_exceptions(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    in_force_only: bool = False,
) -> GovernanceObjectListResponse:
    """Exceptions granted for this tenant, newest first.

    The register an exception is supposed to have. An exception is *defined* as
    a documented deviation, and until this existed one was invisible from the
    moment it was granted.

    Statements are returned only where the deployment stores them as plaintext;
    where they are encrypted at rest this does not reach for a key.
    `detail.has_statement` is what tells a reader "none was given" from "not
    shown here", because a list that decrypted every row would turn "which
    exceptions exist" into a bulk disclosure of why each was granted.
    """
    services: Services = request.app.state.services
    found = await services.arc_governance_reads.list_approved_exceptions(
        tenant_id=ctx.tenant_id, in_force_only=in_force_only
    )
    return _governance_response(found)


@router.get("/source-connectors", response_model=GovernanceObjectListResponse)
async def list_source_connectors(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    in_force_only: bool = False,
) -> GovernanceObjectListResponse:
    """What ARC may fetch, from where, and who may approve what comes back.

    `in_force_until` is null even for a live connector, and that is not a gap: a
    connector has no expiry, so withdrawal is the only thing that ends one. The
    corpora list below is the one where that field carries a date, and comparing
    the two tells a reader something true about how the grants differ.
    """
    services: Services = request.app.state.services
    return _governance_response(
        await services.arc_governance_reads.list_source_connectors(tenant_id=ctx.tenant_id, in_force_only=in_force_only)
    )


@router.get("/source-upload-policies", response_model=GovernanceObjectListResponse)
async def list_source_upload_policies(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    in_force_only: bool = False,
) -> GovernanceObjectListResponse:
    """The same grant, for material pushed in rather than fetched."""
    services: Services = request.app.state.services
    return _governance_response(
        await services.arc_governance_reads.list_upload_policies(tenant_id=ctx.tenant_id, in_force_only=in_force_only)
    )


@router.get("/observation-replay-corpora", response_model=GovernanceObjectListResponse)
async def list_replay_corpora(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    in_force_only: bool = False,
) -> GovernanceObjectListResponse:
    """What observation is replayed against, and until when.

    The only one of the three source grants that lapses on its own, which is why
    this is where `in_force_until` carries a date.
    """
    services: Services = request.app.state.services
    return _governance_response(
        await services.arc_governance_reads.list_replay_corpora(tenant_id=ctx.tenant_id, in_force_only=in_force_only)
    )


@router.get("/approval-evidence", response_model=GovernanceObjectListResponse)
async def list_approval_evidence(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    revision_id: uuid.UUID | None = None,
    in_force_only: bool = False,
) -> GovernanceObjectListResponse:
    """The approvals on record, and whether each still stands.

    `revision_id` narrows to one revision's approvals, which is the question the
    revision-lifecycle surface actually asks: it can attach evidence and until
    now had no way to see what was attached.

    **Whether an approval still stands is a join here, not a column** — revocation
    lives in its own table — and both halves are checked: an approval inside its
    validity window that has been withdrawn is withdrawn, and reading only the
    window would show it as good.
    """
    services: Services = request.app.state.services
    return _governance_response(
        await services.arc_governance_reads.list_approval_evidence(
            tenant_id=ctx.tenant_id, revision_id=revision_id, in_force_only=in_force_only
        )
    )


__all__ = ["router"]
