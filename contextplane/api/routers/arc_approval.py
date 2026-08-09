"""Review package, baseline diff, and D2 projection approval under
`/v1/arc/*`.

Split off `arc_authoring.py` on the same cohesion basis `arc_admin_
enrollment.py` already split off `arc_admin.py`: `arc_authoring.py` was
already close to this repo's 800-line ceiling before these routes existed,
so this landed as a second sibling from the start rather than a retrofit
split once that file was already over the line.

Thin adapters, matching every other ARC router module's own rule: parse the
request, call one service method, translate its typed exception into an
HTTP status. No route makes an authorization decision of its own -- that is
`ReviewPackageService`'s and `ApprovalChallengeService`'s job.

**No standalone `POST {PV}/approve` route exists here, or anywhere.**
`complete_approval_challenge` below is the only way `submitted` becomes
`approved`, and it is a side effect of `ApprovalChallengeService.complete`
succeeding, not a route this module invents. See that method's own
docstring.
"""

from __future__ import annotations

import base64
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, status

from contextplane.api.container import Services
from contextplane.api.errors import build_error
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.schemas.arc_authoring import (
    ActorRef,
    ApprovalChallengeRequest,
    ApprovalChallengeResponse,
    ApprovalCompletionRequest,
    BaselineDiffChange,
    BaselineDiffResponse,
    Citation,
    DetachedSignatureProof,
    ExpectedImpactEnvelope,
    FieldProvenance,
    JudgmentAuthor,
    ProjectionApprovalEvidenceResponse,
    ReachConfirmationItem,
    ReachConfirmationResponse,
    ReviewPackageResponse,
    SemanticTestResultItem,
    SemanticTestResultResponse,
    VerifierAttestationProof,
)
from contextplane.arc import (
    ArcAuthorizationError,
    ArcRequestContext,
    BaselineDiff,
    ReviewPackage,
    ReviewPackageIntegrityError,
    ReviewPackageService,
    ReviewPackageUnavailable,
)
from contextplane.arc import (
    approval_challenge as ac,
)
from contextplane.arc import (
    approval_challenge_verification as acv,
)
from contextplane.exceptions import ConflictError, NotFoundError, RegistryError
from contextplane.types import TenantContext

router = APIRouter(tags=["arc: approval"], prefix="/v1/arc")


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


def _review_package(request: Request) -> ReviewPackageService:
    services: Services = request.app.state.services
    return services.arc_review_package


def _approval_challenges(request: Request) -> ac.ApprovalChallengeService:
    services: Services = request.app.state.services
    return services.arc_approval_challenges


def _translate_error(exc: Exception) -> Exception:
    """One place, so a new route in this module cannot invent its own
    mapping and report the same failure with a different status. Narrower
    than `arc_authoring.py`'s own translator: only the exceptions this
    module's four handlers can actually raise are named here."""
    if isinstance(exc, ReviewPackageIntegrityError):
        # A persisted digest cache (the frozen envelope's own digest, or
        # the sticky risk classification) disagrees with what
        # `ReviewPackageService` just recomputed from the authoritative
        # rows behind it -- the same "cache drift" failure class
        # `arc_operational_integrity_failed` already names.
        return build_error(status.HTTP_409_CONFLICT, code="arc_operational_integrity_failed", message=str(exc))
    if isinstance(exc, ReviewPackageUnavailable):
        return build_error(status.HTTP_409_CONFLICT, code="arc_proposal_state_conflict", message=str(exc))
    if isinstance(exc, ac.ApprovalChallengeExpired):
        return build_error(status.HTTP_409_CONFLICT, code="arc_approval_challenge_expired", message=str(exc))
    if isinstance(exc, ac.ApprovalChallengeFailedTerminal):
        return build_error(status.HTTP_409_CONFLICT, code="arc_approval_challenge_failed", message=str(exc))
    if isinstance(exc, ac.ApprovalChallengeSuperseded):
        return build_error(status.HTTP_409_CONFLICT, code="arc_approval_challenge_superseded", message=str(exc))
    if isinstance(exc, ac.ApprovalAlreadyCompleted):
        return build_error(status.HTTP_409_CONFLICT, code="arc_approval_already_completed", message=str(exc))
    if isinstance(exc, ac.ApprovalChallengeLimitReached):
        return build_error(
            status.HTTP_429_TOO_MANY_REQUESTS, code="arc_approval_challenge_limit_reached", message=str(exc)
        )
    if isinstance(exc, ac.ApprovalIdempotencyConflict):
        return build_error(status.HTTP_409_CONFLICT, code="arc_idempotency_conflict", message=str(exc))
    if isinstance(exc, acv.ApprovalVerificationFailed):
        return build_error(status.HTTP_400_BAD_REQUEST, code="arc_approval_verification_failed", message=str(exc))
    if isinstance(exc, ArcAuthorizationError):
        return build_error(status.HTTP_403_FORBIDDEN, code="forbidden", message="not permitted")
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message=str(exc))
    if isinstance(exc, ConflictError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, RegistryError):
        return build_error(status.HTTP_400_BAD_REQUEST, code="bad_request", message=str(exc))
    return exc


# ---------------------------------------------------------------------------
# Review package and baseline diff
# ---------------------------------------------------------------------------


def _baseline_diff_response(diff: BaselineDiff) -> BaselineDiffResponse:
    return BaselineDiffResponse(
        baseline_revision_id=diff.baseline_revision_id,
        changes=[
            BaselineDiffChange(
                field_path=c.field_path,
                change_kind=c.change_kind,  # type: ignore[arg-type]
                before=c.before,
                after=c.after,
            )
            for c in diff.changes
        ],
    )


def _review_package_response(pkg: ReviewPackage) -> ReviewPackageResponse:
    field_provenance = [
        FieldProvenance(
            field_path=f.field_path,
            provenance_class=f.provenance_class,  # type: ignore[arg-type]
            source_evidence_id=f.source_evidence_id,
            source_anchor=f.source_anchor,
            excerpt_digest=f.excerpt_digest,
            author_role=f.author_role,
            derivation_profile=f.derivation_profile,
            author=(
                ActorRef(issuer=f.author_issuer, subject=f.author_subject)
                if f.author_issuer is not None and f.author_subject is not None
                else None
            ),
        )
        for f in pkg.field_provenance
    ]
    # Citations and judgment authors are both projections of the same
    # field-provenance rows, filtered to the one provenance class each
    # shape requires every field it names to be non-null for -- Appendix
    # A.6's own stated derivation, not a second read.
    citations = [
        Citation(
            field_path=f.field_path,
            source_evidence_id=f.source_evidence_id,
            source_anchor=f.source_anchor,
            excerpt_digest=f.excerpt_digest,
        )
        for f in pkg.field_provenance
        if f.provenance_class == "source_backed"
        and f.source_evidence_id is not None
        and f.source_anchor is not None
        and f.excerpt_digest is not None
    ]
    judgment_authors = [
        JudgmentAuthor(field_path=f.field_path, issuer=f.author_issuer, subject=f.author_subject, role=f.author_role)
        for f in pkg.field_provenance
        if f.provenance_class == "human_judgment"
        and f.author_issuer is not None
        and f.author_subject is not None
        and f.author_role is not None
    ]
    return ReviewPackageResponse(
        review_package_digest=pkg.review_package_digest,
        artifact_semantics_digest=pkg.artifact_semantics_digest,
        artifact_revision_digest=pkg.artifact_revision_digest,
        baseline_diff=_baseline_diff_response(pkg.baseline_diff),
        field_provenance=field_provenance,
        citations=citations,
        judgment_authors=judgment_authors,
        prose_readback=pkg.prose_readback,
        semantic_tests=SemanticTestResultResponse(
            results=[
                SemanticTestResultItem(test_id=t.test_id, passed=t.passed, expected=t.expected, actual=t.actual)
                for t in pkg.semantic_tests
            ]
        ),
        expected_impact_envelope=ExpectedImpactEnvelope.model_validate(pkg.expected_impact_envelope),
        risk_classification=pkg.risk_classification,  # type: ignore[arg-type]
        risk_algorithm_version=pkg.risk_algorithm_version,
        reach_confirmations=ReachConfirmationResponse(
            confirmations=[
                ReachConfirmationItem(
                    field_path=r.field_path,
                    confirmed=r.confirmed,
                    confirmed_at=r.confirmed_at,
                    confirmed_by=(
                        ActorRef(issuer=r.confirmed_by_issuer, subject=r.confirmed_by_subject)
                        if r.confirmed_by_issuer is not None and r.confirmed_by_subject is not None
                        else None
                    ),
                )
                for r in pkg.reach_confirmations
            ]
        ),
        submission_identity=ActorRef(issuer=pkg.submitted_by_issuer, subject=pkg.submitted_by_subject),
    )


async def get_baseline_diff(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> BaselineDiffResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        diff = await _review_package(request).get_baseline_diff(arc_ctx, proposal_id, proposal_version)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _baseline_diff_response(diff)


async def get_review_package(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
) -> ReviewPackageResponse:
    """`GET {PV}/review-package`: everything a projection approver reads
    before signing, and the same object whose digest the approval evidence
    binds. `ReviewPackageService.get_review_package` always recomputes `S`,
    `R`, and `A` from authoritative rows -- see that service's own module
    docstring for which persisted digest columns it cross-checks rather
    than trusts.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        package = await _review_package(request).get_review_package(arc_ctx, proposal_id, proposal_version)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _review_package_response(package)


# ---------------------------------------------------------------------------
# D2 projection approval: challenge creation and completion.
#
# Deliberately no `POST {PV}/approve` route: a successful completion below
# is what compare-and-swaps the bound proposal version from `submitted` to
# `approved`, atomically with the evidence write -- see `ApprovalChallenge
# Service.complete`'s own docstring. Adding a separate approve route would
# create a state reachable without verified evidence behind it.
# ---------------------------------------------------------------------------


def _adapt_challenge_proof(proof: DetachedSignatureProof | VerifierAttestationProof) -> acv.ProofInput:
    """Wire `ApprovalProof` union -> the D2 protocol's own plain shape.

    A different adaptation than `arc.py`'s `_adapt_proof`: that one targets
    `approval_trust.ApprovalProof` (the legacy `exception_approval` path);
    this one targets `approval_challenge_verification.ProofInput`, the D2
    protocol's own dataclass union. Two different service-layer shapes for
    two different protocols, kept apart rather than forced to share one.
    """
    if isinstance(proof, DetachedSignatureProof):
        return acv.DetachedSignatureProofInput(
            signature_algorithm=proof.signature_algorithm.value, signature_base64=proof.signature_base64
        )
    return acv.AttestationProofInput(
        provider_id=proof.provider_id, assertion_format=proof.assertion_format, assertion_base64=proof.assertion_base64
    )


def _approval_challenge_response(issued: ac.IssuedApprovalChallenge) -> ApprovalChallengeResponse:
    return ApprovalChallengeResponse(
        approval_challenge_id=issued.approval_challenge_id,
        canonical_evidence_bytes_base64=base64.b64encode(issued.canonical_evidence_bytes).decode("ascii"),
        signing_domain=issued.signing_domain,
        approval_nonce=issued.approval_nonce,
        expires_at=issued.expires_at,
    )


def _projection_evidence_response(evidence: ac.ProjectionApprovalEvidence) -> ProjectionApprovalEvidenceResponse:
    return ProjectionApprovalEvidenceResponse(
        evidence_id=evidence.evidence_id,
        proposal_id=evidence.proposal_id,
        proposal_version=evidence.proposal_version,
        revision_id=evidence.revision_id,
        approved_payload_digest=evidence.approved_payload_digest,
        approval_verifier_id=uuid.UUID(evidence.approval_verifier_id),
        approving_principal_issuer=evidence.approving_principal_issuer,
        approving_principal_subject=evidence.approving_principal_subject,
        verified_at=evidence.verified_at,
        revoked_at=evidence.revoked_at,
    )


async def create_approval_challenge(
    request: Request,
    body: ApprovalChallengeRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: Annotated[uuid.UUID, Path()],
    proposal_version: Annotated[int, Path()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ApprovalChallengeResponse:
    """`POST {PV}/approval-challenges`.

    An omitted `Idempotency-Key` is not refused the way source admission
    refuses one (Appendix A.5 states idempotency-key support as something
    every mutation route *accepts*, not universally *requires*): a caller
    that omits it gets a fresh, single-use key minted here, so the call
    still succeeds -- it simply carries no retry-deduplication of its own.
    """
    arc_ctx = _arc_context(request, ctx)
    key = idempotency_key if idempotency_key else uuid.uuid4().hex
    try:
        issued = await _approval_challenges(request).create_challenge(
            arc_ctx,
            proposal_id,
            proposal_version,
            approval_verifier_id=str(body.approval_verifier_id),
            idempotency_key=key,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _approval_challenge_response(issued)


async def complete_approval_challenge(
    request: Request,
    body: ApprovalCompletionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    approval_challenge_id: Annotated[uuid.UUID, Path()],
) -> ProjectionApprovalEvidenceResponse:
    """`POST /v1/arc/approval-challenges/{id}/complete`."""
    arc_ctx = _arc_context(request, ctx)
    try:
        evidence = await _approval_challenges(request).complete(
            arc_ctx, approval_challenge_id, proof=_adapt_challenge_proof(body.proof)
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _projection_evidence_response(evidence)


_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)
_mr.add_read_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/baseline-diff",
    handler=get_baseline_diff,
    response_model=BaselineDiffResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_read_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/review-package",
    handler=get_review_package,
    response_model=ReviewPackageResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_mutation_route(
    path="/proposals/{proposal_id}/versions/{proposal_version}/approval-challenges",
    action="create",
    handler=create_approval_challenge,
    verb="POST",
    response_model=ApprovalChallengeResponse,
    status_code=status.HTTP_201_CREATED,
)
_mr.add_mutation_route(
    path="/approval-challenges/{approval_challenge_id}/complete",
    action="complete",
    handler=complete_approval_challenge,
    verb="POST",
    response_model=ProjectionApprovalEvidenceResponse,
    status_code=status.HTTP_200_OK,
)

# Route handlers are registered above by reference (`add_mutation_route`/
# `add_read_route`), not imported by name elsewhere -- matching every other
# ARC router module's own `__all__`, which excludes its handlers for the
# same reason.
__all__ = ["router"]
