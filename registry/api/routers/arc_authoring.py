"""The artifact-family and proposal surface under `/v1/arc/*`.

Kept apart from `registry.api.routers.arc` (source admission, resolution,
receipts) on the same cohesion basis `arc_admin.py` already split off
registration/administration: artifact families and proposal threads are
their own concern, and folding them into `arc.py` would have pushed that
module well past the repo's line-count ceiling for no cohesion gain. Later
authoring-surface tasks (editing, submission, approval, observation,
activation) are expected to keep landing routes here rather than reopening
`arc.py`.

Thin adapters, matching `arc.py`'s own rule: parse the request, call one
`ProposalService` method, translate its typed exception into an HTTP
status. No route makes an authorization decision of its own.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status

from registry.api.errors import build_error
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.tenant import get_tenant_context
from registry.api.schemas.arc_authoring import (
    ArtifactFamilyCreate,
    ArtifactFamilyResponse,
    EmptyRequest,
    ProposalOpenRequest,
    ProposalPatchRequest,
    ProposalSummary,
    ProposalThreadResponse,
    ProposalVersionResponse,
    ReasonRequest,
    RefusalCode,
    SemanticTestRequest,
    SemanticTestResultItem,
    SemanticTestResultResponse,
    SubmitRequest,
    ValidationErrorItem,
    ValidationResponse,
)
from registry.arc.service.authorization import ArcAuthorizationError
from registry.arc.service.envelope import EnvelopeInvalid
from registry.arc.service.operational_chain import (
    OperationalChainIdempotencyConflict,
    OperationalChainIntegrityError,
)
from registry.arc.service.proposal import (
    ArtifactFamily,
    ProposalService,
    ProposalStateConflict,
    ProposalThread,
    ProposalVersion,
)
from registry.arc.service.provenance import (
    ActorNotCallerSupplied,
    ProvenanceInvalid,
    ProvenanceService,
    SemanticsValidationFailed,
)
from registry.arc.service.risk import RiskClassificationError
from registry.arc.service.semantic_tests import SemanticTestResult, SemanticTestService
from registry.arc.service.submission import (
    ArtifactMaterialisationService,
    CandidateSemanticsMissing,
    SubmissionPrerequisiteUnavailable,
)
from registry.arc.types import ArcRequestContext
from registry.exceptions import ConflictError, NotFoundError, RegistryError
from registry.types import TenantContext
from registry.wiring.container import Services

router = APIRouter(tags=["arc: authoring"], prefix="/v1/arc")


def _arc_context(request: Request, ctx: TenantContext) -> ArcRequestContext:
    """Build the ARC identity from what auth already validated.

    Duplicated from `arc.py` rather than imported: `arc_admin.py` makes the
    same choice for the same reason -- each router module is a self-
    contained adapter over the service layer, and importing a private
    helper across router modules would couple two files that otherwise
    have no reason to change together.
    """
    claims = getattr(request.state, "oidc_claims", None) or {}
    try:
        return ArcRequestContext.from_validated_claims(ctx, claims)
    except ValueError as exc:
        raise build_error(
            status.HTTP_401_UNAUTHORIZED,
            code="unauthenticated",
            message="the credential carries no validated issuer",
        ) from exc


def _proposals(request: Request) -> ProposalService:
    services: Services = request.app.state.services
    return services.arc_proposals


def _provenance(request: Request) -> ProvenanceService:
    services: Services = request.app.state.services
    return services.arc_provenance


def _semantic_tests(request: Request) -> SemanticTestService:
    services: Services = request.app.state.services
    return services.arc_semantic_tests


def _materialisation(request: Request) -> ArtifactMaterialisationService:
    services: Services = request.app.state.services
    return services.arc_materialisation


def _translate_error(exc: Exception) -> Exception:
    """One place, so a new authoring route cannot invent its own mapping
    and report the same failure with a different status."""
    if isinstance(exc, ProposalStateConflict):
        return build_error(status.HTTP_409_CONFLICT, code="arc_proposal_state_conflict", message=str(exc))
    if isinstance(exc, ActorNotCallerSupplied):
        return build_error(status.HTTP_400_BAD_REQUEST, code="arc_actor_not_caller_supplied", message=str(exc))
    if isinstance(exc, ProvenanceInvalid):
        return build_error(status.HTTP_422_UNPROCESSABLE_ENTITY, code="arc_provenance_invalid", message=str(exc))
    if isinstance(exc, SemanticsValidationFailed):
        return build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, code="arc_proposal_validation_failed", message=str(exc)
        )
    if isinstance(exc, CandidateSemanticsMissing):
        return build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, code="arc_proposal_validation_failed", message=str(exc)
        )
    if isinstance(exc, SubmissionPrerequisiteUnavailable):
        return build_error(status.HTTP_409_CONFLICT, code="arc_operational_integrity_pending", message=str(exc))
    if isinstance(exc, EnvelopeInvalid):
        return build_error(status.HTTP_422_UNPROCESSABLE_ENTITY, code="arc_envelope_invalid", message=str(exc))
    if isinstance(exc, RiskClassificationError):
        # Covers `UnknownRiskAlgorithmVersion` too (a subclass) -- both are
        # candidate-content defects a real deployment should never surface,
        # since proposal validation is supposed to reject a zero-rule
        # candidate before classification ever runs; `arc_proposal_
        # validation_failed` is the closest existing code for "the frozen
        # candidate this transaction tried to classify was not valid."
        return build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, code="arc_proposal_validation_failed", message=str(exc)
        )
    if isinstance(exc, OperationalChainIdempotencyConflict):
        return build_error(status.HTTP_409_CONFLICT, code="arc_idempotency_conflict", message=str(exc))
    if isinstance(exc, OperationalChainIntegrityError):
        return build_error(status.HTTP_409_CONFLICT, code="arc_operational_integrity_failed", message=str(exc))
    if isinstance(exc, ArcAuthorizationError):
        return build_error(status.HTTP_403_FORBIDDEN, code="forbidden", message="not permitted")
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message=str(exc))
    if isinstance(exc, ConflictError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, RegistryError):
        return build_error(status.HTTP_400_BAD_REQUEST, code="bad_request", message=str(exc))
    return exc


def _family_response(family: ArtifactFamily) -> ArtifactFamilyResponse:
    return ArtifactFamilyResponse(
        artifact_id=family.artifact_id,
        slug=family.slug,
        kind=family.kind,  # type: ignore[arg-type]
        owning_scope=family.owning_scope,  # type: ignore[arg-type]
        target_tenant_id=family.target_tenant_id,
        title=family.title,
        active_revision_id=family.active_revision_id,
        created_at=family.created_at,
        created_by={"issuer": family.created_by_issuer, "subject": family.created_by_subject},  # type: ignore[arg-type]
    )


def _version_response(version: ProposalVersion) -> ProposalVersionResponse:
    return ProposalVersionResponse(
        proposal_id=version.proposal_id,
        proposal_version=version.proposal_version,
        artifact_id=version.artifact_id,
        state=version.state,  # type: ignore[arg-type]
        revision_id=version.revision_id,
        source_evidence_id=version.source_evidence_id,
        reviewed_baseline_revision_id=version.reviewed_baseline_revision_id,
        risk_classification=version.risk_classification,  # type: ignore[arg-type]
        risk_algorithm_version=version.risk_algorithm_version,
        allowed_transitions=list(version.allowed_transitions),  # type: ignore[arg-type]
        available_actions=list(version.available_actions),  # type: ignore[arg-type]
        reason_codes=list(version.reason_codes),
        operational_integrity_state=version.operational_integrity_state,  # type: ignore[arg-type]
        created_at=version.created_at,
        frozen_at=version.frozen_at,
    )


def _thread_response(thread: ProposalThread) -> ProposalThreadResponse:
    return ProposalThreadResponse(
        proposal_id=thread.proposal_id,
        artifact_id=thread.artifact_id,
        latest_version=thread.latest_version,
        versions=[
            ProposalSummary(
                proposal_id=v.proposal_id,
                proposal_version=v.proposal_version,
                artifact_id=v.artifact_id,
                state=v.state,  # type: ignore[arg-type]
                risk_classification=v.risk_classification,  # type: ignore[arg-type]
                created_at=v.created_at,
            )
            for v in thread.versions
        ],
    )


# ---------------------------------------------------------------------------
# Artifact families
# ---------------------------------------------------------------------------


async def create_artifact_family(
    request: Request,
    body: ArtifactFamilyCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> ArtifactFamilyResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        family = await _proposals(request).create_family(
            arc_ctx,
            slug=body.slug,
            kind=body.kind.value,
            owning_scope=body.owning_scope.value,
            target_tenant_id=body.target_tenant_id,
            title=body.title,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _family_response(family)


async def get_artifact_family(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    artifact_id: Annotated[uuid.UUID, Path()],
) -> ArtifactFamilyResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        family = await _proposals(request).get_family(arc_ctx, artifact_id)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _family_response(family)


# ---------------------------------------------------------------------------
# Proposal threads and versions
# ---------------------------------------------------------------------------


async def open_proposal(
    request: Request,
    body: ProposalOpenRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    artifact_id: Annotated[uuid.UUID, Path()],
) -> ProposalVersionResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        version = await _proposals(request).open_proposal(
            arc_ctx,
            artifact_id=artifact_id,
            source_evidence_id=body.source_evidence_id,
            reviewed_baseline_revision_id=body.reviewed_baseline_revision_id,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _version_response(version)


async def get_proposal_thread(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
) -> ProposalThreadResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        thread = await _proposals(request).get_thread(arc_ctx, proposal_id)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _thread_response(thread)


async def get_proposal_version(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ProposalVersionResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        version = await _proposals(request).get_version(arc_ctx, proposal_id, proposal_version)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _version_response(version)


async def withdraw_proposal_version(
    request: Request,
    body: ReasonRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ProposalVersionResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        version = await _proposals(request).withdraw(
            arc_ctx, proposal_id, proposal_version, reason_code=body.reason_code, note=body.note
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _version_response(version)


async def reject_proposal_version(
    request: Request,
    body: ReasonRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ProposalVersionResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        version = await _proposals(request).reject(
            arc_ctx, proposal_id, proposal_version, reason_code=body.reason_code, note=body.note
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _version_response(version)


async def supersede_proposal_version(
    request: Request,
    body: ReasonRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ProposalVersionResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        version = await _proposals(request).supersede(
            arc_ctx, proposal_id, proposal_version, reason_code=body.reason_code, note=body.note
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _version_response(version)


# ---------------------------------------------------------------------------
# Field provenance, conditional validation, semantic tests
# ---------------------------------------------------------------------------


async def edit_proposal_version(
    request: Request,
    body: ProposalPatchRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ProposalVersionResponse:
    """`PATCH {PV}`: validates and persists the candidate semantics and
    field provenance in one call.

    `ProvenanceService.edit` validates `semantics` (closed-schema,
    duplicate-identifier, ambiguous-selector) and every `field_provenance`
    entry's conditional shape before writing either, then persists both --
    the candidate document into `arc_authoring_proposal_versions.semantics`,
    the entries into `arc_authoring_field_provenance` -- in one transaction.
    The returned `ProposalVersionResponse` is a fresh read of the version,
    so `available_actions` accounts for the fact that editing an open
    version is now legal.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _provenance(request).edit(
            arc_ctx,
            proposal_id,
            proposal_version,
            semantics=body.semantics.model_dump(mode="json"),
            entries=body.field_provenance,
        )
        version = await _proposals(request).get_version(arc_ctx, proposal_id, proposal_version)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _version_response(version)


async def validate_proposal_version(
    request: Request,
    body: EmptyRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ValidationResponse:
    """`POST {PV}/validate`: re-checks the persisted candidate semantics
    (if a `PATCH` has ever written one) and every persisted
    `field_provenance` row's conditional shape.

    See `ProvenanceService.revalidate_stored`'s own docstring: both halves
    are read fresh from storage on this call, so this reports on what is
    actually persisted right now, not on whatever was true when a prior
    `PATCH` wrote it.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        result = await _provenance(request).revalidate_stored(arc_ctx, proposal_id, proposal_version)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return ValidationResponse(
        valid=result.valid,
        errors=[
            ValidationErrorItem(field_path=e.field_path, code=RefusalCode(e.code), message=e.message)
            for e in result.errors
        ],
    )


async def run_semantic_tests(
    request: Request,
    body: SemanticTestRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> SemanticTestResultResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        results = await _semantic_tests(request).run(arc_ctx, proposal_id, proposal_version, tests=body.tests)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _semantic_test_result_response(results)


def _semantic_test_result_response(results: tuple[SemanticTestResult, ...]) -> SemanticTestResultResponse:
    return SemanticTestResultResponse(
        results=[
            SemanticTestResultItem(test_id=r.test_id, passed=r.passed, expected=r.expected, actual=r.actual)
            for r in results
        ]
    )


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


async def submit_proposal_version(
    request: Request,
    body: SubmitRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ProposalVersionResponse:
    """`POST {PV}/submit`.

    Refuses on every deployment today with `arc_operational_integrity_pending`
    -- see `ArtifactMaterialisationService`'s own module docstring for why --
    before this handler, or the service method it calls, touches a session.
    Once the two collaborators it needs are wired, the same call freezes the
    version, materialises its bound draft revision, and returns a fresh
    read of it, matching every other transition route's fresh-read
    convention.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _materialisation(request).submit(
            arc_ctx,
            proposal_id,
            proposal_version,
            expected_impact_envelope=body.expected_impact_envelope.model_dump(mode="json"),
        )
        version = await _proposals(request).get_version(arc_ctx, proposal_id, proposal_version)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _version_response(version)


_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)
_mr.add_mutation_route(
    path="/artifacts",
    action="create",
    handler=create_artifact_family,
    verb="POST",
    response_model=ArtifactFamilyResponse,
    status_code=status.HTTP_201_CREATED,
)
_mr.add_read_route(
    path="/artifacts/{artifact_id}",
    handler=get_artifact_family,
    response_model=ArtifactFamilyResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/artifacts/{artifact_id}/proposals",
    action="open",
    handler=open_proposal,
    verb="POST",
    response_model=ProposalVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
_mr.add_read_route(
    path="/proposals/{proposal_id}",
    handler=get_proposal_thread,
    response_model=ProposalThreadResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_read_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}",
    handler=get_proposal_version,
    response_model=ProposalVersionResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}",
    action="edit",
    handler=edit_proposal_version,
    verb="PATCH",
    response_model=ProposalVersionResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/validate",
    action="validate",
    handler=validate_proposal_version,
    verb="POST",
    response_model=ValidationResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/semantic-tests",
    action="run",
    handler=run_semantic_tests,
    verb="POST",
    response_model=SemanticTestResultResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/submit",
    action="submit",
    handler=submit_proposal_version,
    verb="POST",
    response_model=ProposalVersionResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/withdraw",
    action="withdraw",
    handler=withdraw_proposal_version,
    verb="POST",
    response_model=ProposalVersionResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/reject",
    action="reject",
    handler=reject_proposal_version,
    verb="POST",
    response_model=ProposalVersionResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/supersede",
    action="supersede",
    handler=supersede_proposal_version,
    verb="POST",
    response_model=ProposalVersionResponse,
    status_code=status.HTTP_200_OK,
)

# Route handlers are registered above by reference (`add_mutation_route`/
# `add_read_route`), not imported by name elsewhere -- matching `arc.py`'s
# own `__all__`, which excludes its handlers for the same reason.
__all__ = ["router"]
