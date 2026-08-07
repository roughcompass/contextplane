"""Shadow observation and qualification under `/v1/arc/*`.

Split from `arc_authoring.py`/`arc_approval.py` as its own sibling router on
the same cohesion basis `arc_approval.py` itself already used for D2
approval: this is a distinct ADR 041 concern (observation, qualification,
acceptance) with its own three routes, not a natural extension of either
existing file's stated scope, and both siblings are already close to this
repo's 800-line ceiling.

Thin adapters, matching every other ARC router module's own rule: parse
the request, call one service method, translate its typed exception into
an HTTP status. No route makes an authorization or actor-separation
decision of its own -- that is `QualificationService`'s job.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status

from registry.api.errors import build_error
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.tenant import get_tenant_context
from registry.api.schemas.arc_authoring import (
    ActorRef,
    DeltaCodeCounter,
    ObservationStatusResponse,
    QualificationAcceptanceRequest,
    QualificationResponse,
)
from registry.arc.service.authorization import ArcAuthorizationError
from registry.arc.service.qualification import (
    ObservationFailed,
    ObservationInsufficient,
    ObservationStatus,
    QualificationActorInvalid,
    QualificationComputation,
    QualificationService,
    QualificationUnavailable,
)
from registry.arc.service.shadow import ShadowError
from registry.arc.types import ArcRequestContext
from registry.exceptions import ConflictError, NotFoundError, RegistryError
from registry.types import TenantContext
from registry.wiring.container import Services

router = APIRouter(tags=["arc: observation"], prefix="/v1/arc")


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


def _qualification(request: Request) -> QualificationService:
    services: Services = request.app.state.services
    return services.arc_qualification


def _translate_error(exc: Exception) -> Exception:
    """One place, so a new route in this module cannot invent its own
    mapping and report the same failure with a different status."""
    if isinstance(exc, QualificationUnavailable):
        return build_error(status.HTTP_409_CONFLICT, code="arc_proposal_state_conflict", message=str(exc))
    if isinstance(exc, QualificationActorInvalid):
        return build_error(status.HTTP_403_FORBIDDEN, code="arc_qualification_actor_invalid", message=str(exc))
    if isinstance(exc, ObservationInsufficient):
        return build_error(status.HTTP_409_CONFLICT, code="arc_observation_insufficient", message=str(exc))
    if isinstance(exc, ObservationFailed):
        return build_error(status.HTTP_409_CONFLICT, code="arc_observation_failed", message=str(exc))
    if isinstance(exc, ShadowError):
        return build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, code="arc_proposal_validation_failed", message=str(exc)
        )
    if isinstance(exc, ArcAuthorizationError):
        return build_error(status.HTTP_403_FORBIDDEN, code="forbidden", message="not permitted")
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message=str(exc))
    if isinstance(exc, ConflictError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, RegistryError):
        return build_error(status.HTTP_400_BAD_REQUEST, code="bad_request", message=str(exc))
    return exc


def _qualification_response(computation: QualificationComputation) -> QualificationResponse:
    accepted_by = None
    if computation.accepted_by_issuer is not None and computation.accepted_by_subject is not None:
        accepted_by = ActorRef(issuer=computation.accepted_by_issuer, subject=computation.accepted_by_subject)
    return QualificationResponse(
        qualification_id=computation.qualification_id,
        decision=computation.decision,  # type: ignore[arg-type]
        candidate_review_package_digest=computation.candidate_review_package_digest,
        baseline_revision_id=computation.baseline_revision_id,
        # An "observation not required" result is ephemeral (no cohort was
        # ever frozen -- see `QualificationService.compute`'s own
        # docstring) and carries no real cohort digest to report; the
        # all-zero placeholder is a well-formed `Digest` string that no
        # real cohort could ever produce (every real one is a SHA-256 of
        # non-empty content), so it cannot be mistaken for one.
        cohort_digest=computation.cohort_digest or "0" * 64,
        expected_impact_envelope_digest=computation.expected_impact_envelope_digest,
        replay_corpus_digest=computation.replay_corpus_digest,
        qualification_algorithm_version=computation.qualification_algorithm_version,
        computed_at=computation.computed_at,
        accepted_at=computation.accepted_at,
        accepted_by=accepted_by,
        expires_at=computation.expires_at,
    )


def _observation_status_response(status_obj: ObservationStatus) -> ObservationStatusResponse:
    return ObservationStatusResponse(
        cohort_id=status_obj.cohort_id,
        cohort_digest=status_obj.cohort_digest,
        window_started_at=status_obj.window_started_at,
        window_deadline=status_obj.window_deadline,
        eligible_count=status_obj.eligible_count,
        observed_count=status_obj.observed_count,
        counters_by_delta_code=[
            DeltaCodeCounter(
                delta_code=code,  # type: ignore[arg-type]
                count=buckets["explained"] + buckets["unexplained"],
            )
            for code, buckets in sorted(status_obj.counters_by_delta_code.items())
        ],
        unexplained_count=status_obj.unexplained_count,
        out_of_envelope_count=status_obj.out_of_envelope_count,
        computed_decision=status_obj.computed_decision,  # type: ignore[arg-type]
        reason_codes=list(status_obj.reason_codes),
    )


async def get_observation_status(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ObservationStatusResponse:
    """`GET {PV}/observation`. Carries aggregate counters and cohort
    digests only -- see `queries/observation.py::load_aggregate_counters`
    for why a global candidate's response can never carry per-tenant
    detail."""
    arc_ctx = _arc_context(request, ctx)
    try:
        result = await _qualification(request).get_status(arc_ctx, proposal_id, proposal_version)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _observation_status_response(result)


async def qualify_proposal_version(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> QualificationResponse:
    """`POST {PV}/observation/qualify`. System-computed: freezes the
    cohort on first call, closes its window at the first correct boundary,
    and returns the current decision -- `insufficient`/`failed` included,
    never raised as an error, per `QualificationResponse.decision`'s own
    closed vocabulary."""
    arc_ctx = _arc_context(request, ctx)
    try:
        result = await _qualification(request).compute(arc_ctx, proposal_id, proposal_version)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _qualification_response(result)


async def accept_qualification(
    request: Request,
    body: QualificationAcceptanceRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> QualificationResponse:
    """`POST {PV}/observation/accept`. Refuses a non-positive decision,
    the submitter accepting their own submission, and (for a global-
    mandatory candidate) the approver accepting in place of a third
    distinct activator -- see `QualificationService.accept`'s own
    docstring for each rule.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        result = await _qualification(request).accept(
            arc_ctx,
            qualification_id=body.qualification_id,
            acknowledged_reason_codes=list(body.acknowledged_reason_codes),
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _qualification_response(result)


_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)
_mr.add_read_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/observation",
    handler=get_observation_status,
    response_model=ObservationStatusResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/observation/qualify",
    action="qualify",
    handler=qualify_proposal_version,
    verb="POST",
    response_model=QualificationResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/observation/accept",
    action="accept",
    handler=accept_qualification,
    verb="POST",
    response_model=QualificationResponse,
    status_code=status.HTTP_200_OK,
)

# Route handlers are registered above by reference (`add_mutation_route`/
# `add_read_route`), not imported by name elsewhere -- matching every
# other ARC router module's own `__all__`, which excludes its handlers for
# the same reason.
__all__ = ["router"]
