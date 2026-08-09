"""Model-backed drafting and reach confirmations, under `/v1/arc/*`.

A new sibling of `contextplane.api.routers.arc_authoring` rather than an
addition to it: two other tasks in this phase's wave (candidate-semantics
storage, submission/materialisation) are concurrently editing
`arc_authoring.py` and `arc.py` respectively, and `arc.py` is already
within 24 lines of the repo's 800-line lint ceiling. A third concurrent
editor of either file is the outcome most likely to produce a conflicting
edit that nobody notices until CI; a dedicated file for exactly the two
routes this task owns (`POST {PV}/draft`, `POST {PV}/reach-confirmations`)
avoids that outright rather than relying on careful sequencing.

Thin adapters, matching every sibling router's own rule: parse the request,
call one `DrafterService` method, translate its typed exception into an
HTTP status. No route makes an authorization decision of its own.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status

from contextplane.api.container import Services
from contextplane.api.errors import build_error
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.schemas.arc_authoring import (
    ArtifactSemanticsPartial,
    Citation,
    DraftPatchResponse,
    DraftRequest,
    ReachConfirmationItem,
    ReachConfirmationRequest,
    ReachConfirmationResponse,
)
from contextplane.arc import (
    ArcAuthorizationError,
    ArcRequestContext,
    DrafterModelDisabled,
    DrafterService,
    ProposalStateConflict,
    ReachConfirmationRecord,
    SourceAdmissionRefused,
    SourceStatusUnavailable,
)
from contextplane.exceptions import ConflictError, NotFoundError, RegistryError
from contextplane.types import TenantContext

router = APIRouter(tags=["arc: drafting"], prefix="/v1/arc")


def _arc_context(request: Request, ctx: TenantContext) -> ArcRequestContext:
    """Duplicated from `arc.py`/`arc_authoring.py` rather than imported --
    each router module is a self-contained adapter over the service layer,
    matching both siblings' own stated reason for the same duplication."""
    claims = getattr(request.state, "oidc_claims", None) or {}
    try:
        return ArcRequestContext.from_validated_claims(ctx, claims)
    except ValueError as exc:
        raise build_error(
            status.HTTP_401_UNAUTHORIZED,
            code="unauthenticated",
            message="the credential carries no validated issuer",
        ) from exc


def _drafter(request: Request) -> DrafterService:
    services: Services = request.app.state.services
    return services.arc_drafter


def _translate_error(exc: Exception) -> Exception:
    """One place, so this router cannot invent its own mapping and report
    the same failure with a different status than its siblings do."""
    if isinstance(exc, DrafterModelDisabled):
        return build_error(status.HTTP_409_CONFLICT, code="arc_drafter_model_disabled", message=str(exc))
    if isinstance(exc, SourceStatusUnavailable):
        return build_error(status.HTTP_409_CONFLICT, code="arc_source_status_unavailable", message=str(exc))
    if isinstance(exc, SourceAdmissionRefused):
        return build_error(status.HTTP_400_BAD_REQUEST, code="arc_source_admission_refused", message=str(exc))
    if isinstance(exc, ProposalStateConflict):
        return build_error(status.HTTP_409_CONFLICT, code="arc_proposal_state_conflict", message=str(exc))
    if isinstance(exc, ArcAuthorizationError):
        return build_error(status.HTTP_403_FORBIDDEN, code="forbidden", message="not permitted")
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message=str(exc))
    if isinstance(exc, ConflictError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, RegistryError):
        return build_error(status.HTTP_400_BAD_REQUEST, code="bad_request", message=str(exc))
    return exc


def _confirmation_item(record: ReachConfirmationRecord) -> ReachConfirmationItem:
    actor = None
    if record.confirmed_by_issuer is not None and record.confirmed_by_subject is not None:
        actor = {"issuer": record.confirmed_by_issuer, "subject": record.confirmed_by_subject}
    return ReachConfirmationItem(
        field_path=record.field_path,
        confirmed=record.confirmed,
        confirmed_at=record.confirmed_at,
        confirmed_by=actor,  # type: ignore[arg-type]
    )


async def draft_proposal_version(
    request: Request,
    body: DraftRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> DraftPatchResponse:
    """`POST {PV}/draft`: ask the drafter sandbox for a citation-bound
    patch.

    Refuses with `arc_drafter_model_disabled` on every deployment running
    the committed decision artifact (`outcome: human_only`) -- see
    `DrafterService.draft`'s own docstring for why that refusal is
    provably the first thing this call does.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        result = await _drafter(request).draft(
            arc_ctx,
            proposal_id,
            proposal_version,
            source_evidence_id=body.source_evidence_id,
            target_field_paths=body.target_field_paths,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return DraftPatchResponse(
        patch=ArtifactSemanticsPartial(**result.patch),
        citations=[
            Citation(
                field_path=c.field_path,
                source_evidence_id=c.source_evidence_id,
                source_anchor=c.source_anchor,
                excerpt_digest=c.excerpt_digest,
            )
            for c in result.citations
        ],
        declined_field_paths=list(result.declined_field_paths),
    )


async def confirm_reach(
    request: Request,
    body: ReachConfirmationRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ReachConfirmationResponse:
    """`POST {PV}/reach-confirmations`: record that the caller has reviewed
    each named field path's reach, for this frozen candidate."""
    arc_ctx = _arc_context(request, ctx)
    try:
        records = await _drafter(request).confirm_reach(
            arc_ctx, proposal_id, proposal_version, field_paths=body.field_paths
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return ReachConfirmationResponse(confirmations=[_confirmation_item(r) for r in records])


_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/draft",
    action="draft",
    handler=draft_proposal_version,
    verb="POST",
    response_model=DraftPatchResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/reach-confirmations",
    action="confirm",
    handler=confirm_reach,
    verb="POST",
    response_model=ReachConfirmationResponse,
    status_code=status.HTTP_200_OK,
)

# Route handlers are registered above by reference, not imported by name
# elsewhere -- matching every sibling router's own `__all__`.
__all__ = ["router"]
