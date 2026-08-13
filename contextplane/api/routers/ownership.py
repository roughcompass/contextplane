"""/v1/ownership — assign, validate, supersede and revoke accountability.

**REST only, deliberately.** These are owner-authorized administrative actions,
and the architecture's ownership interface does not expose them to agents. That is
not an oversight to be corrected by adding tools later: an agent that could
validate an ownership assignment could establish accountability for anything it
could name, and the cross-surface parity suite asserts this exclusion so adding a
tool has to be an explicit decision rather than a drift.

**Ownership is not authorization.** Nothing here reads or writes an entitlement,
and a conformance suite inspects the auth code to prove the reverse — that no
authorization decision consults an ownership assignment. Wiring the two would make
"assign an owner" a privilege-escalation primitive.

**Every transition carries a reason.** The service requires it; this surface makes
it a required body field rather than an optional one so the refusal arrives as a
422 naming the field instead of a 500 from below.
"""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.ownership import queries as ownership_queries
from contextplane.ownership.service import (
    AssignmentNotFound,
    IllegalTransition,
    OwnershipError,
    SubjectMismatch,
)
from contextplane.types import TenantContext

if TYPE_CHECKING:
    from contextplane.api.container import Services

router = APIRouter(prefix="/v1/ownership", tags=["ownership"])

NonBlank = Annotated[str, Field(min_length=1)]


class AssignOwnershipRequestV1(BaseModel):
    """A new ownership assignment. Lands in `draft`; validation is a separate act."""

    model_config = ConfigDict(extra="forbid")

    owner_principal: NonBlank
    owned_target_kind: NonBlank
    owned_target_id: uuid.UUID
    role: NonBlank
    scope: NonBlank
    source: NonBlank
    profile_revision_id: uuid.UUID
    derivation_method: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    effective_from: datetime.datetime | None = None


class TransitionRequestV1(BaseModel):
    """A move, its reason, and — when superseding — what replaces it."""

    model_config = ConfigDict(extra="forbid")

    to_state: NonBlank
    reason: NonBlank
    replaced_by_assignment_id: uuid.UUID | None = None


class OwnershipAssignmentV1(BaseModel):
    """One assignment as the surface reports it.

    `is_pending` is computed and returned rather than left for a caller to derive
    from the state name: a UI showing an unvalidated assignment without the label
    presents a proposal as settled fact.
    """

    model_config = ConfigDict(extra="forbid")

    ownership_assignment_id: uuid.UUID
    owner_principal: str
    owned_target_kind: str
    owned_target_id: uuid.UUID
    role: str
    scope: str
    source: str
    derivation_method: str | None
    confidence: float | None
    validation_state: str
    is_pending: bool
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None
    provenance_id: uuid.UUID
    replaced_by_assignment_id: uuid.UUID | None
    revocation_reason: str | None
    recorded_by: str
    recorded_at: datetime.datetime


class OwnershipListV1(BaseModel):
    """A derived view — `owns` or `owned_by`, both read off the same rows."""

    model_config = ConfigDict(extra="forbid")

    items: list[OwnershipAssignmentV1]


@router.post(
    "/assignments",
    response_model=OwnershipAssignmentV1,
    status_code=status.HTTP_201_CREATED,
    summary="Assign ownership. The assignment lands in `draft`.",
)
async def assign(
    request: Request,
    body: AssignOwnershipRequestV1,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> OwnershipAssignmentV1:
    """Record a new assignment, checking the subject before anything is written."""
    services = _services(request)
    try:
        assignment = await services.ownership.assign(
            tenant_id=ctx.tenant_id,
            owner_principal=body.owner_principal,
            owned_target_kind=body.owned_target_kind,
            owned_target_id=body.owned_target_id,
            role=body.role,
            scope=body.scope,
            source=body.source,
            recorded_by=str(ctx.actor_id),
            profile_revision_id=body.profile_revision_id,
            derivation_method=body.derivation_method,
            confidence=body.confidence,
            effective_from=body.effective_from,
        )
    except SubjectMismatch as mismatch:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(mismatch)) from mismatch
    except OwnershipError as refused:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(refused)) from refused
    return _to_response(assignment)


@router.post(
    "/assignments/{assignment_id}:transition",
    response_model=OwnershipAssignmentV1,
    summary="Move an assignment through its lifecycle, recording actor, time and reason.",
)
async def transition(
    request: Request,
    assignment_id: uuid.UUID,
    body: TransitionRequestV1,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> OwnershipAssignmentV1:
    """Validate, supersede or revoke — whichever the lifecycle permits from here."""
    services = _services(request)
    try:
        moved = await services.ownership.transition(
            tenant_id=ctx.tenant_id,
            assignment_id=assignment_id,
            to_state=body.to_state,
            reason=body.reason,
            recorded_by=str(ctx.actor_id),
            replaced_by_assignment_id=body.replaced_by_assignment_id,
        )
    except AssignmentNotFound as missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)) from missing
    except IllegalTransition as illegal:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(illegal)) from illegal
    except OwnershipError as refused:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(refused)) from refused
    return _to_response(moved)


@router.get(
    "/assignments/{assignment_id}",
    response_model=OwnershipAssignmentV1,
    summary="Read one assignment in whatever state it holds.",
)
async def get_assignment(
    request: Request,
    assignment_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> OwnershipAssignmentV1:
    """Unfiltered by state: a revoked assignment is a different answer from none."""
    services = _services(request)
    async with services.session_factory() as session:
        assignment = await ownership_queries.get(session, tenant_id=ctx.tenant_id, assignment_id=assignment_id)
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ownership assignment not found")
    return _to_response(assignment)


@router.get(
    ":owns",
    response_model=OwnershipListV1,
    summary="What this principal owns — a derived view.",
)
async def owns(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    owner_principal: Annotated[str, Query(min_length=1)],
    include_pending: bool = False,
) -> OwnershipListV1:
    """Derived from the same rows the other direction reads, never stored twice."""
    services = _services(request)
    async with services.session_factory() as session:
        found = await ownership_queries.owned_by(
            session,
            tenant_id=ctx.tenant_id,
            owner_principal=owner_principal,
            at=services.clock.now(),
            include_pending=include_pending,
        )
    return OwnershipListV1(items=[_to_response(item) for item in found])


@router.get(
    ":owned-by",
    response_model=OwnershipListV1,
    summary="Who owns this target — the same rows, from the other end.",
)
async def owned_by(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    owned_target_kind: Annotated[str, Query(min_length=1)],
    owned_target_id: uuid.UUID,
    include_pending: bool = False,
) -> OwnershipListV1:
    """The inverse view. One row, two questions; nothing is duplicated to answer both."""
    services = _services(request)
    async with services.session_factory() as session:
        found = await ownership_queries.owners_of(
            session,
            tenant_id=ctx.tenant_id,
            owned_target_kind=owned_target_kind,
            owned_target_id=owned_target_id,
            at=services.clock.now(),
            include_pending=include_pending,
        )
    return OwnershipListV1(items=[_to_response(item) for item in found])


def _to_response(assignment: ownership_queries.OwnershipAssignment) -> OwnershipAssignmentV1:
    return OwnershipAssignmentV1(
        ownership_assignment_id=assignment.ownership_assignment_id,
        owner_principal=assignment.owner_principal,
        owned_target_kind=assignment.owned_target_kind,
        owned_target_id=assignment.owned_target_id,
        role=assignment.role,
        scope=assignment.scope,
        source=assignment.source,
        derivation_method=assignment.derivation_method,
        confidence=assignment.confidence,
        validation_state=assignment.validation_state,
        is_pending=assignment.is_pending,
        effective_from=assignment.effective_from,
        effective_to=assignment.effective_to,
        provenance_id=assignment.provenance_id,
        replaced_by_assignment_id=assignment.replaced_by_assignment_id,
        revocation_reason=assignment.revocation_reason,
        recorded_by=assignment.recorded_by,
        recorded_at=assignment.recorded_at,
    )


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


__all__ = ["router"]
