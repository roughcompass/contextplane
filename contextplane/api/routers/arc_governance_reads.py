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

import dataclasses
import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from contextplane.api.container import Services
from contextplane.api.errors import map_catalog_error
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.arc import (
    LIFECYCLE_STATES,
    REVISION_MAX_PAGE_SIZE,
    GovernanceObject,
    parse_revision_cursor,
)
from contextplane.exceptions import ValidationError
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


class RevisionIndexRow(BaseModel):
    """One revision, and what a reader needs before opening it.

    The three activation fields are columns, not a verdict. Whether a revision
    can activate is ten predicates computed as if the caller were the one
    activating, and that stays at
    `GET /v1/arc/revisions/{revision_id}/activation-eligibility` — a list that
    answered it would be a second, weaker computation two surfaces could
    disagree over.
    """

    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    artifact_slug: str
    artifact_kind: str
    lifecycle_state: str
    source_system: str
    source_revision_locator: str
    content_digest: str
    approval_evidence_id: uuid.UUID | None
    effective_from: datetime.datetime
    effective_until: datetime.datetime | None
    review_expires_at: datetime.datetime
    activated_at: datetime.datetime | None
    revoked_at: datetime.datetime | None
    created_at: datetime.datetime
    #: How many resolutions were made under this revision. The field the two
    #: terminal acts differ over: invalidate puts these in question, revoke
    #: leaves them standing.
    resolutions_under_revision: int
    is_draft: bool
    has_approval_evidence: bool
    review_expired: bool
    is_terminal: bool


class RevisionIndexResponse(BaseModel):
    items: list[RevisionIndexRow]
    next_cursor: str | None


@router.get("/revisions", response_model=RevisionIndexResponse)
async def list_arc_revisions(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    lifecycle_state: str | None = Query(None, description=f"One of {list(LIFECYCLE_STATES)}."),
    artifact_id: uuid.UUID | None = Query(None),
    cursor: str | None = Query(None),
    page_size: int = Query(50, ge=1, le=REVISION_MAX_PAGE_SIZE),
) -> RevisionIndexResponse:
    """Which revisions exist — the read the lifecycle screen never had.

    Seven paths already act on a revision and every one is keyed by an id the
    caller must already hold. That is why the lifecycle screen is four text
    boxes: nothing could have been designed well against a surface with no way
    to ask what exists.

    Each row carries `resolutions_under_revision`, which is the field the choice
    between the two terminal acts turns on — invalidate puts what was decided
    under a revision in question, revoke leaves it standing — so a reader is not
    choosing between them on the strength of a paragraph.
    """
    services: Services = request.app.state.services
    try:
        page = await services.arc_revision_index.list_revisions(
            ctx,
            lifecycle_state=lifecycle_state,
            artifact_id=artifact_id,
            cursor=parse_revision_cursor(cursor),
            page_size=page_size,
        )
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return RevisionIndexResponse(
        items=[RevisionIndexRow(**dataclasses.asdict(row)) for row in page.items],
        next_cursor=page.next_cursor,
    )
