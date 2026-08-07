"""ARC administration: D1 verifier enrollment and revocation.

Sibling of `arc_admin.py`, split out purely for `scripts/check_file_sizes.py`'s
800-line ceiling -- the ARC tree carries zero allowlist entries, so a file
that would breach it is split along a real seam instead. This one owns the
three verifier-trust routes: issuing an enrollment challenge, completing it
into a registered verifier, and revoking one. All three share `arc_admin.py`'s
authentication/authorization helpers and exception-translation table rather
than duplicating them -- a second `_translate` would be a second place for
the two routers' error mappings to drift apart.

Mounted under the same `/v1/arc/admin` prefix as `arc_admin.router`, as a
second `APIRouter` `wiring/routes.py` includes right alongside it -- FastAPI
does not require one router per prefix.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, status

from registry.api.errors import build_error
from registry.api.middleware.tenant import get_tenant_context
from registry.api.routers.arc_admin import _arc_context, _require_global_operator, _translate
from registry.api.schemas.arc_authoring import (
    ApprovalVerifierResponse,
    DetachedSignatureProof,
    EnrollmentChallengeRequest,
    EnrollmentChallengeResponse,
    PrincipalBindingKind,
    ReasonRequest,
    VerifierAttestationProof,
    VerifierRegistrationRequest,
)
from registry.arc.service.enrollment import (
    AttestationProofInput,
    DetachedSignatureProofInput,
    EnrollmentService,
    ProofInput,
)
from registry.arc.service.queries.enrollment import VerifierRow
from registry.arc.service.verifier_registry import KIND_PROVIDER
from registry.types import TenantContext
from registry.wiring.container import Services

router = APIRouter(tags=["arc: admin"], prefix="/v1/arc/admin")


def _enrollment(request: Request) -> EnrollmentService:
    services: Services = request.app.state.services
    return services.arc_enrollment


def _verifier_response(row: VerifierRow) -> ApprovalVerifierResponse:
    """Map one `arc_approval_verifiers` row onto the wire response.

    `ApprovalVerifierResponse` (Appendix A.6) declares `binding_kind`/
    `principal_issuer`/`principal_subject` all required, not nullable. That
    is a clean fit for a D1-enrolled row (`principal_binding_kind` set) but
    not for a pre-existing, non-principal-bound `exception_approval`
    verifier the legacy `VerifierRegistry.register()` writer still creates
    -- the migration's own CHECK requires that column be NULL on exactly
    those rows, and the response schema has no way to say "not
    principal-bound." This is a genuine gap in the frozen wire contract,
    not one this route can close: reported here rather than silently
    papered over (see this task's outcome record for the escalation).

    For a legacy row, `binding_kind` is derived from the pre-existing,
    truthful `verifier_kind` column (the closest real equivalent, not an
    invented value) and `principal_issuer`/`principal_subject` report an
    empty string -- the one piece this response shape genuinely cannot
    represent for a verifier that was never principal-bound.
    """
    if row.principal_binding_kind is not None:
        binding_kind = PrincipalBindingKind(row.principal_binding_kind)
        principal_issuer = row.principal_issuer or ""
        principal_subject = row.principal_subject or ""
    else:
        binding_kind = (
            PrincipalBindingKind.PROVIDER_DELEGATED
            if row.verifier_kind == KIND_PROVIDER
            else PrincipalBindingKind.EXACT_PRINCIPAL
        )
        principal_issuer = ""
        principal_subject = ""
    credential_fingerprint = row.credential_fingerprint
    if credential_fingerprint is None:
        # Same derivation `VerifierRegistry`'s own audit trail already uses
        # for a legacy row's credential fingerprint -- grounded in the row's
        # real stored material, not invented.
        material = row.provider_id.encode("utf-8") if row.provider_id else b""
        credential_fingerprint = hashlib.sha256(material).hexdigest()
    return ApprovalVerifierResponse(
        approval_verifier_id=uuid.UUID(row.approval_verifier_id),
        binding_kind=binding_kind,
        principal_issuer=principal_issuer,
        principal_subject=principal_subject,
        provider_id=row.provider_id,
        credential_fingerprint=credential_fingerprint,
        owning_scope=row.scope_kind,  # type: ignore[arg-type]
        target_tenant_id=row.scope_tenant_id,
        evidence_types=list(row.allowed_evidence_types),  # type: ignore[arg-type]
        valid_from=row.valid_from,
        # `arc_approval_verifiers.valid_to` is nullable ("no expiry"); the
        # response schema's `valid_to` is required, so an unbounded
        # validity window is reported as the conventional far-future
        # sentinel rather than a fabricated near-term date.
        valid_to=row.valid_to or datetime.datetime(9999, 12, 31, tzinfo=datetime.UTC),
        enrolled_at=row.created_at,
        revoked_at=row.revoked_at,
    )


@router.post("/approval-verifiers/{approval_verifier_id}/revoke", response_model=ApprovalVerifierResponse)
async def revoke_approval_verifier(
    request: Request,
    body: ReasonRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    approval_verifier_id: Annotated[str, Path(min_length=1, max_length=200)],
) -> ApprovalVerifierResponse:
    """Withdraw trust in an approval verifier, deployment-wide.

    Requires operator identity regardless of the verifier's own scope. A
    tenant-scoped verifier is registrable by a tenant admin, but revoking
    one is a trust decision whose blast radius includes every revision and
    exception it ever vouched for -- so it is not a tenant-level action.

    The cascade (revoking affected revisions and exceptions, advancing
    obligation tombstones) is not implemented here; see the note in the
    route body.
    """
    arc_ctx = _arc_context(request, ctx)
    _require_global_operator(request, arc_ctx)

    services: Services = request.app.state.services
    trust = services.arc_approval_trust
    if trust is None:
        # Deliberately a clear 501 rather than a silent success. Revoking a
        # verifier without cascading to what it vouched for would leave
        # revisions active on withdrawn trust, which is worse than refusing
        # the operation outright.
        raise build_error(
            status.HTTP_501_NOT_IMPLEMENTED,
            code="not_implemented",
            message=(
                "verifier revocation requires the cascade that withdraws affected revisions and "
                "exceptions; it is not available on this deployment"
            ),
        )
    reason = body.note or body.reason_code
    try:
        await trust.revoke_verifier(arc_ctx, approval_verifier_id, reason=reason)
    except Exception as exc:
        raise _translate(exc) from exc

    row = await services.arc_enrollment.get_verifier(approval_verifier_id)
    if row is None:
        raise build_error(status.HTTP_404_NOT_FOUND, code="not_found", message="not found")
    return _verifier_response(row)


def _proof_input(proof: DetachedSignatureProof | VerifierAttestationProof) -> ProofInput:
    """Adapt the wire `ApprovalProof` discriminated union onto the service's
    own plain input type -- the router's adaptation, not the service's, per
    this codebase's convention of decoupling service signatures from the
    wire layer."""
    if isinstance(proof, DetachedSignatureProof):
        return DetachedSignatureProofInput(
            signature_algorithm=proof.signature_algorithm.value, signature_base64=proof.signature_base64
        )
    return AttestationProofInput(
        provider_id=proof.provider_id, assertion_format=proof.assertion_format, assertion_base64=proof.assertion_base64
    )


@router.post(
    "/approval-verifiers/enrollment-challenges",
    response_model=EnrollmentChallengeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_enrollment_challenge(
    request: Request,
    body: EnrollmentChallengeRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> EnrollmentChallengeResponse:
    """Issue a five-minute, single-use D1 enrollment challenge.

    Operator-gated deployment-wide, matching registration and revocation
    below: enrolling a verifier decides who counts as an approver, the same
    blast-radius decision the other two verifier-trust routes gate the same
    way. The enrolled principal need not be the caller (Appendix A.4) --
    only the *creating* identity is checked against the allowlist.
    """
    arc_ctx = _arc_context(request, ctx)
    _require_global_operator(request, arc_ctx)

    try:
        issued = await _enrollment(request).create_challenge(
            arc_ctx,
            binding_kind=body.binding_kind.value,
            principal_issuer=body.principal_issuer,
            principal_subject=body.principal_subject,
            provider_id=body.provider_id,
            provider_allowed_principal_issuer=body.provider_allowed_principal_issuer,
            owning_scope=body.owning_scope.value,
            target_tenant_id=body.target_tenant_id,
            evidence_types=[t.value for t in body.evidence_types],
            signature_algorithm=body.signature_algorithm.value,
            public_key_base64=body.public_key_base64,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return EnrollmentChallengeResponse(
        enrollment_challenge_id=issued.enrollment_challenge_id,
        canonical_enrollment_bytes_base64=base64.b64encode(issued.canonical_enrollment_bytes).decode("ascii"),
        signing_domain=issued.signing_domain,
        expires_at=issued.expires_at,
    )


@router.post("/approval-verifiers", response_model=ApprovalVerifierResponse, status_code=status.HTTP_201_CREATED)
async def register_approval_verifier(
    request: Request,
    body: VerifierRegistrationRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> ApprovalVerifierResponse:
    """Complete a D1 enrollment challenge, admitting the verifier it names.

    Replaces the pre-existing direct-registration route entirely: there is
    no longer a way to admit a principal-bound verifier without first
    proving possession of its credential (or having a configured provider
    attest to it) against the exact bytes `create_enrollment_challenge`
    committed. Operator-gated deployment-wide, matching registration's
    pre-existing gate -- see that route's own docstring.
    """
    arc_ctx = _arc_context(request, ctx)
    _require_global_operator(request, arc_ctx)

    try:
        row = await _enrollment(request).register_verifier(
            arc_ctx,
            enrollment_challenge_id=body.enrollment_challenge_id,
            proof=_proof_input(body.proof),
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return _verifier_response(row)


__all__ = ["router"]
