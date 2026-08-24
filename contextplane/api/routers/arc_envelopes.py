"""The autonomy envelope's operating surface: grant, suspend, reinstate, revoke.

`AutonomyEnvelopeService` has had all four acts since E7, wired into the
container and reachable from **no transport**. The control that decides what an
agent may do could be read and not operated: an incident response consisted of
editing rows (E23-T5).

**REST only, like `arc_admin.py`, and for the same reason with more force.** An
agent able to reinstate its own envelope is an agent that can end any suspension
imposed on it, which is the failure the envelope exists to prevent, arranged so
that the subject of the control operates it.

The four acts differ in who may perform them, and that difference is the
service's and not this router's. Suspension is tenant-scoped even for a global
envelope, because an incident is what it exists for and requiring a deployment
operator to switch off one tenant's agent would make "instant" depend on finding
one. Reinstatement and revocation are at the envelope's own scope, because both
are the first half of a substitution.

**Why a separate module rather than more of `arc_admin.py`.** The same seam
`arc_source_grants.py` was cut along, forced the same way: the ceiling. The
envelope is a lifecycle over one object with one service behind it, which is a
real boundary and not a slice taken to fit under a number -- these five routes
would belong together at any file length.
"""

from __future__ import annotations

import base64
import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from contextplane.api.container import Services
from contextplane.api.errors import build_error
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.routers.arc_admin import _Accepted, _arc_context, _translate
from contextplane.arc import (
    AutonomyEnvelopeService,
    BoundEnvelope,
    EnvelopeGrant,
    WorkloadIdentity,
)
from contextplane.types import TenantContext

router = APIRouter(tags=["arc: admin"], prefix="/v1/arc/admin")


class EnvelopeGrantRequest(BaseModel):
    """Bind one principal to one envelope revision."""

    revision_id: uuid.UUID
    principal_issuer: str = Field(min_length=1)
    principal_subject: str = Field(min_length=1)
    reason: str = Field(min_length=1, description="Why this principal is being governed by this envelope.")
    effective_from: datetime.datetime | None = Field(
        default=None,
        description=(
            "Defaults to now. Backdating is refused by the service: an envelope cannot be "
            "made to have governed a decision it did not."
        ),
    )
    effective_to: datetime.datetime | None = None
    audit_reference: str | None = None


class EnvelopeFlipRequest(BaseModel):
    """Suspend, reinstate or revoke one binding."""

    reason: str = Field(
        min_length=1,
        description=(
            "Required. A binding switched off with no reason leaves the next reader to work out "
            "why an agent stopped being able to act, during the incident where that matters most."
        ),
    )


class EnvelopeBindingResponse(BaseModel):
    """One binding, as the operating surface reports it."""

    binding_id: uuid.UUID
    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    principal_issuer: str
    principal_subject: str
    state: str
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None = None
    suspended_at: datetime.datetime | None = None
    suspension_reason: str | None = None
    revision_lifecycle_state: str = Field(
        description=(
            "The bound revision's own state. A binding is only checked for `active` at grant time, "
            "so a live binding to a superseded or revoked governance document is a state this "
            "reports rather than hides."
        )
    )
    is_in_force: bool = Field(
        description="Whether the binding itself is switched on. Says nothing about the revision's lifecycle."
    )


def _envelopes(request: Request) -> AutonomyEnvelopeService:
    services: Services = request.app.state.services
    service: AutonomyEnvelopeService | None = getattr(services, "arc_envelopes", None)
    if service is None:
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="autonomy envelopes are not configured on this deployment",
        )
    return service


@router.post("/envelopes/bindings", response_model=_Accepted, status_code=status.HTTP_201_CREATED)
async def grant_envelope(
    request: Request,
    body: EnvelopeGrantRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> _Accepted:
    """Bind a principal to an envelope revision."""
    arc_ctx = _arc_context(request, ctx)
    try:
        binding_id = await _envelopes(request).grant(
            arc_ctx,
            EnvelopeGrant(
                audit_reference=body.audit_reference,
                effective_from=body.effective_from,
                effective_to=body.effective_to,
                principal=WorkloadIdentity(issuer=body.principal_issuer, subject=body.principal_subject),
                reason=body.reason,
                revision_id=body.revision_id,
            ),
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return _Accepted(status="granted", revision_id=body.revision_id, binding_id=binding_id)


@router.post("/envelopes/bindings/{binding_id}/suspend", response_model=_Accepted)
async def suspend_envelope(
    request: Request,
    body: EnvelopeFlipRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    binding_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """Turn an envelope off without ending it.

    The instant-suspend path. Nothing the principal begins after this commits is
    authorised by the envelope, on any replica, because no replica holds a copy —
    which is what replaced the wall-clock SLO the plan originally carried.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _envelopes(request).suspend(arc_ctx, binding_id, reason=body.reason)
    except Exception as exc:
        raise _translate(exc) from exc
    return _Accepted(status="suspended", binding_id=binding_id)


@router.post("/envelopes/bindings/{binding_id}/reinstate", response_model=_Accepted)
async def reinstate_envelope(
    request: Request,
    body: EnvelopeFlipRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    binding_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """Turn a suspended envelope back on.

    Authorised like a grant rather than like a suspension, which the service
    decides: putting authority back in force at tenant scope would let a tenant
    admin undo a deployment operator's suspension of a global envelope.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _envelopes(request).reinstate(arc_ctx, binding_id, reason=body.reason)
    except Exception as exc:
        raise _translate(exc) from exc
    return _Accepted(status="reinstated", binding_id=binding_id)


@router.post("/envelopes/bindings/{binding_id}/revoke", response_model=_Accepted)
async def revoke_envelope(
    request: Request,
    body: EnvelopeFlipRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    binding_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """End the binding, closing its interval.

    Not the same act as suspending. A suspension is a posture somebody can
    reverse; this closes the interval, and the difference is what an auditor
    reading the record afterwards is trying to tell apart.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _envelopes(request).revoke(arc_ctx, binding_id, reason=body.reason)
    except Exception as exc:
        raise _translate(exc) from exc
    return _Accepted(status="revoked", binding_id=binding_id)


class EnvelopeBindingPage(BaseModel):
    """One page of the directory, with the bookmark for the next."""

    items: list[EnvelopeBindingResponse]
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Send back as `cursor` for the next page. Opaque -- it is not a timestamp to "
            "compare, decode or store, and treating it as one is how a client starts "
            "depending on an ordering nobody promised it."
        ),
    )


def _encode_cursor(effective_from: datetime.datetime, binding_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{effective_from.isoformat()}|{binding_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime.datetime, uuid.UUID]:
    """A malformed cursor is the caller's fault, and says only that.

    No detail on which half failed: a cursor is this service's own bookmark, and
    a caller constructing one is already outside the contract.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, _, identifier = raw.partition("|")
        return datetime.datetime.fromisoformat(stamp), uuid.UUID(identifier)
    except (ValueError, UnicodeDecodeError) as exc:
        raise build_error(
            status.HTTP_400_BAD_REQUEST,
            code="invalid_cursor",
            message="cursor is not one this service issued",
        ) from exc


def _binding_response(binding: BoundEnvelope) -> EnvelopeBindingResponse:
    """One mapper, so the directory and the resolve read cannot disagree.

    Two copies would be two chances for a field to appear on one and not the
    other, and `revision_lifecycle_state` is the one that matters: a list that
    dropped it would show a row as governed while the document behind it is not.
    """
    return EnvelopeBindingResponse(
        artifact_id=binding.artifact_id,
        binding_id=binding.binding_id,
        effective_from=binding.effective_from,
        effective_to=binding.effective_to,
        is_in_force=binding.is_in_force,
        principal_issuer=binding.principal.issuer,
        principal_subject=binding.principal.subject,
        revision_id=binding.revision_id,
        revision_lifecycle_state=binding.revision_lifecycle_state,
        state=binding.state,
        suspended_at=binding.suspended_at,
        suspension_reason=binding.suspension_reason,
    )


@router.get("/envelopes/bindings/directory", response_model=EnvelopeBindingPage)
async def list_envelope_bindings(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    cursor: str | None = None,
    limit: int = 50,
) -> EnvelopeBindingPage:
    """Who is governed in this tenant, and how.

    **The read the operating surface was missing.** `resolve` answers about a
    principal the caller can already name; until this, nothing told them the
    names. An operator during an incident had to already know the exact
    `(issuer, subject)` pair of the agent they were trying to stop, which is the
    same as not having the control.

    A separate path from the single-principal resolve rather than that route
    with its parameters made optional: one answers "is this agent governed" and
    the other "who is", and a route that returned an object or a page depending
    on which query parameters arrived would be two contracts wearing one URL.

    Suspended and revoked bindings are included. A closed interval is exactly
    what an operator asking "was this agent ever governed" is looking for.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        page, next_cursor = await _envelopes(request).list_bindings(
            arc_ctx,
            cursor=None if cursor is None else _decode_cursor(cursor),
            limit=limit,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    return EnvelopeBindingPage(
        items=[_binding_response(binding) for binding in page],
        next_cursor=None if next_cursor is None else _encode_cursor(*next_cursor),
    )


@router.get("/envelopes/bindings", response_model=EnvelopeBindingResponse | None)
async def resolve_envelope(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    principal_issuer: str,
    principal_subject: str,
    at: datetime.datetime | None = None,
) -> EnvelopeBindingResponse | None:
    """The envelope covering one principal, suspended or not.

    `null` when no binding covers the instant, which a caller must not read as
    "suspended": one is a principal nobody has governed, the other is a posture
    somebody chose, and collapsing them is what would let an ungoverned agent
    look controlled.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        found = await _envelopes(request).resolve(
            arc_ctx,
            WorkloadIdentity(issuer=principal_issuer, subject=principal_subject),
            at=at,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return None if found is None else _binding_response(found)


__all__ = ["router"]
