"""Atomic activation under `/v1/arc/revisions/*`.

Its own sibling router rather than a fourth section of `arc_authoring.py`/
`arc_approval.py`/`arc_observation.py`, on the same cohesion basis each of
those already used to split off `arc.py`/`arc_authoring.py` in turn (see
either module's own docstring): the ten-predicate gate is a distinct,
security-critical concern with its own three routes, not a natural
extension of proposal editing, D2 approval, or observation/qualification --
and every one of those three siblings is already substantial. Unlike the
other authoring-surface routes, these three are keyed by `revision_id`, not
`(proposal_id, proposal_version)`, matching Appendix A.1's own "Activation"
table.

Thin adapters, matching every other ARC router module's own rule: parse the
request, call one `ActivationService` method, translate its typed exception
into an HTTP status. No route makes an authorization or predicate decision
of its own -- that is `ActivationService`'s job, and `POST .../activate`
cannot succeed yet regardless of what any route here does (see
`registry.arc.service.activation`'s own module docstring).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status

from registry.api.errors import build_error
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.tenant import get_tenant_context
from registry.api.schemas.arc_authoring import (
    ActivateRequest,
    ActivationEligibilityResponse,
    ActivationPredicateStatus,
    ReasonRequest,
    RevisionResponse,
)
from registry.arc.service.activation import (
    ActivationEligibility,
    ActivationError,
    ActivationPredicateFailed,
    ActivationRequestMismatch,
    ActivationService,
    RevisionActivation,
)
from registry.arc.service.artifact import ArtifactLifecycleError
from registry.arc.service.authorization import ArcAuthorizationError
from registry.arc.types import ArcRequestContext
from registry.exceptions import ConflictError, NotFoundError, RegistryError
from registry.types import TenantContext
from registry.wiring.container import Services

router = APIRouter(tags=["arc: activation"], prefix="/v1/arc")


def _arc_context(request: Request, ctx: TenantContext) -> ArcRequestContext:
    """Duplicated from `arc_authoring.py` rather than imported -- see that
    module's own docstring for why each router module stays a self-
    contained adapter."""
    claims = getattr(request.state, "oidc_claims", None) or {}
    try:
        return ArcRequestContext.from_validated_claims(ctx, claims)
    except ValueError as exc:
        raise build_error(
            status.HTTP_401_UNAUTHORIZED,
            code="unauthenticated",
            message="the credential carries no validated issuer",
        ) from exc


def _activation(request: Request) -> ActivationService:
    services: Services = request.app.state.services
    return services.arc_activation


def _translate_error(exc: Exception) -> Exception:
    """One place, so a new route in this module cannot invent its own
    mapping and report the same failure with a different status."""
    if isinstance(exc, ActivationPredicateFailed):
        return build_error(status.HTTP_409_CONFLICT, code="arc_activation_predicate_failed", message=str(exc))
    if isinstance(exc, ActivationRequestMismatch):
        return build_error(status.HTTP_409_CONFLICT, code="arc_proposal_state_conflict", message=str(exc))
    if isinstance(exc, ActivationError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, ArtifactLifecycleError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, ArcAuthorizationError):
        return build_error(status.HTTP_403_FORBIDDEN, code="forbidden", message="not permitted")
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message=str(exc))
    if isinstance(exc, ConflictError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, RegistryError):
        return build_error(status.HTTP_400_BAD_REQUEST, code="bad_request", message=str(exc))
    return exc


def _eligibility_response(eligibility: ActivationEligibility) -> ActivationEligibilityResponse:
    return ActivationEligibilityResponse(
        eligible=eligibility.eligible,
        predicates=[
            ActivationPredicateStatus(
                name=p.name,  # type: ignore[arg-type]
                satisfied=p.satisfied,
                reason_code=p.reason_code,  # type: ignore[arg-type]
            )
            for p in eligibility.predicates
        ],
    )


def _revision_response(activation: RevisionActivation) -> RevisionResponse:
    return RevisionResponse(
        revision_id=activation.revision_id,
        artifact_id=activation.artifact_id,
        lifecycle_state=activation.lifecycle_state,  # type: ignore[arg-type]
        operational_integrity_state=activation.operational_integrity_state,  # type: ignore[arg-type]
        activated_at=activation.activated_at,
        revoked_at=activation.revoked_at,
    )


async def get_activation_eligibility(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    revision_id: Annotated[uuid.UUID, Path()],
) -> ActivationEligibilityResponse:
    """`GET /v1/arc/revisions/{revision_id}/activation-eligibility`. Always
    reports all ten predicates, in fixed order -- see `ActivationService.
    get_eligibility`'s own docstring for why the report is computed as if
    the calling principal were the one activating.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        eligibility = await _activation(request).get_eligibility(arc_ctx, revision_id)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _eligibility_response(eligibility)


async def activate_revision(
    request: Request,
    body: ActivateRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    revision_id: Annotated[uuid.UUID, Path()],
) -> RevisionResponse:
    """`POST /v1/arc/revisions/{revision_id}/activate`. Reachable, and --
    until a later commit wires real operational-integrity assessment into
    predicate 10 -- always refuses. See `registry.arc.service.activation`'s
    own module docstring.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        activation = await _activation(request).activate(
            arc_ctx,
            revision_id=revision_id,
            proposal_id=body.proposal_id,
            proposal_version=body.proposal_version,
            qualification_id=body.qualification_id,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _revision_response(activation)


async def revoke_revision(
    request: Request,
    body: ReasonRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    revision_id: Annotated[uuid.UUID, Path()],
) -> RevisionResponse:
    """`POST /v1/arc/revisions/{revision_id}/revoke`. Delegates to the
    shared lifecycle transition every revision -- authored or not -- uses;
    see `ActivationService.revoke`'s own docstring."""
    arc_ctx = _arc_context(request, ctx)
    try:
        activation = await _activation(request).revoke(
            arc_ctx, revision_id, reason_code=body.reason_code, note=body.note
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _revision_response(activation)


_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)
_mr.add_read_route(
    path="/revisions/{revision_id}/activation-eligibility",
    handler=get_activation_eligibility,
    response_model=ActivationEligibilityResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/revisions/{revision_id}/activate",
    action="activate",
    handler=activate_revision,
    verb="POST",
    response_model=RevisionResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/revisions/{revision_id}/revoke",
    action="revoke",
    handler=revoke_revision,
    verb="POST",
    response_model=RevisionResponse,
    status_code=status.HTTP_200_OK,
)

# Route handlers are registered above by reference (`add_mutation_route`/
# `add_read_route`), not imported by name elsewhere -- matching every other
# ARC router module's own `__all__`, which excludes its handlers for the
# same reason.
__all__ = ["router"]
