"""The ARC read and resolution surface under `/v1/arc/*`.

Thin adapters, deliberately. Every route here parses a request, calls one
service method, and translates a typed ARC exception into an HTTP status.
No route makes an authorization decision of its own -- those live in
`ArcAuthorizationService`, and a route that grew its own check would be a
second place to get it wrong.

Two translation rules carry weight:

**A blocked resolution is HTTP 200.** It was authenticated, it produced a
receipt, and the receipt explains why it was blocked. Returning 403 would
tell the caller their credentials were the problem, discard the evidence,
and make "you may not do this" indistinguishable from "you are not who you
say you are".

**An unverified manifest is 403 with no receipt.** There was never a
trustworthy request to record. The reason code is bounded and identical
across every underlying cause: which check failed is exactly the probing
signal an attacker wants.
"""

from __future__ import annotations

import base64
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from registry.api.errors import build_error
from registry.api.middleware.tenant import get_tenant_context
from registry.arc.service.authorization import ArcAuthorizationError
from registry.arc.service.challenge import ChallengeService
from registry.arc.service.jit import DetailDenied, DetailIdempotencyConflict, DetailRequest, JitService
from registry.arc.service.receipt_read import ReceiptReader
from registry.arc.service.resolution import IdempotencyConflict, ManifestUnverified
from registry.arc.types import ArcRequestContext
from registry.exceptions import ConflictError, NotFoundError
from registry.types import TenantContext

router = APIRouter(tags=["arc"], prefix="/v1/arc")

# The bounded code every receipt-free rejection returns, regardless of which
# check refused it.
BLOCKED_MANIFEST_UNVERIFIED = "blocked_manifest_unverified"
DETAIL_DENIED = "detail_denied"


def _arc_context(request: Request, ctx: TenantContext) -> ArcRequestContext:
    """Build the ARC identity from what auth already validated.

    ARC does not re-parse the token. A second parser is a second place for
    the two to disagree about the issuer, and the issuer's whole value here
    is that it is the one the allowlist check already validated.
    """
    claims = getattr(request.state, "oidc_claims", None) or {}
    host_id = request.headers.get("x-arc-host-id")
    try:
        return ArcRequestContext.from_validated_claims(ctx, claims, host_id=host_id)
    except ValueError as exc:
        raise build_error(
            status.HTTP_401_UNAUTHORIZED,
            code="unauthenticated",
            message="the credential carries no validated issuer",
        ) from exc


def _require_host(arc_ctx: ArcRequestContext) -> str:
    if not arc_ctx.host_id:
        raise build_error(
            status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="this operation requires a registered agent host identity",
        )
    return arc_ctx.host_id


# ---------------------------------------------------------------------------
# Request and response models
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    """Closed request models.

    `extra="forbid"` matters more here than elsewhere: a caller that
    misspells a field must be told, not silently have it dropped and then
    believe it declared something ARC never saw.
    """

    model_config = ConfigDict(extra="forbid")


class ChallengeRequest(_Strict):
    session_id: str = Field(min_length=1, max_length=200)
    manifest_claims_digest: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ChallengeResponse(BaseModel):
    arc_nonce: str
    issued_at: str
    expires_at: str
    manifest_claims_digest: str


class DetailRequestBody(_Strict):
    context_handle: str = Field(min_length=1, max_length=512)
    request_kind: str = Field(pattern=r"^(directive|source_anchor|query)$")
    selector: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    max_response_bytes: int = Field(default=16384, ge=1, le=32768)
    continuation_token: str | None = None


class DetailPageResponse(BaseModel):
    profile: str
    receipt_id: uuid.UUID
    request_digest: str
    page_number: int
    items: list[dict[str, Any]]
    returned_bytes: int
    complete: bool
    continuation_token: str | None = None
    reason_codes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Challenge issuance
# ---------------------------------------------------------------------------


@router.post("/challenges", response_model=ChallengeResponse, status_code=status.HTTP_200_OK)
async def issue_context_challenge(
    request: Request,
    body: ChallengeRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> ChallengeResponse:
    """Issue a single-use challenge bound to this host and session.

    `host_id` and the tenant come from the authenticated context, never
    from the body -- a caller able to name its own host could bind a
    challenge to somebody else's identity.
    """
    arc_ctx = _arc_context(request, ctx)
    # Rejects here rather than letting the service raise a bare ValueError,
    # so a caller with no host identity gets 403 rather than a 500.
    _require_host(arc_ctx)
    challenges: ChallengeService = request.app.state.arc_challenges

    try:
        issued = await challenges.issue_challenge(
            arc_ctx,
            session_id=body.session_id,
            manifest_claims_digest=body.manifest_claims_digest,
            idempotency_key=body.idempotency_key,
        )
    except ConflictError as exc:
        raise build_error(
            status.HTTP_409_CONFLICT,
            code="idempotency_conflict",
            message="idempotency key already identifies a different challenge",
        ) from exc

    # Base64 on the wire, raw bytes in the service: the encoding is a
    # transport concern and the service should never see it.
    return ChallengeResponse(
        arc_nonce=base64.b64encode(issued.arc_nonce).decode("ascii"),
        issued_at=issued.issued_at.isoformat(),
        expires_at=issued.expires_at.isoformat(),
        manifest_claims_digest=issued.manifest_claims_digest,
    )


# ---------------------------------------------------------------------------
# JIT detail
# ---------------------------------------------------------------------------


@router.post(
    "/receipts/{receipt_id}/detail",
    response_model=DetailPageResponse,
    status_code=status.HTTP_200_OK,
)
async def retrieve_context_detail(
    request: Request,
    body: DetailRequestBody,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    receipt_id: Annotated[uuid.UUID, Path()],
) -> DetailPageResponse:
    """Return one authorized page of detail for a receipt."""
    arc_ctx = _arc_context(request, ctx)
    jit: JitService = request.app.state.arc_jit

    try:
        page = await jit.retrieve(
            arc_ctx,
            DetailRequest(
                receipt_id=receipt_id,
                context_handle=body.context_handle,
                request_kind=body.request_kind,
                selector=body.selector,
                idempotency_key=body.idempotency_key,
                max_response_bytes=body.max_response_bytes,
                continuation_token=body.continuation_token,
            ),
        )
    except DetailIdempotencyConflict as exc:
        raise build_error(
            status.HTTP_409_CONFLICT,
            code="idempotency_conflict",
            message="idempotency key already identifies a different detail request",
        ) from exc
    except DetailDenied as exc:
        # One status and one code for every denial reason. An invalid token,
        # a revoked artifact, and an audience refusal are intentionally
        # indistinguishable to the caller.
        raise build_error(
            status.HTTP_403_FORBIDDEN,
            code=DETAIL_DENIED,
            message="detail is not available to this caller",
        ) from exc

    return DetailPageResponse(
        profile="arc_detail_response_page_v1",
        receipt_id=page.receipt_id,
        request_digest=page.request_digest,
        page_number=page.page_number,
        items=list(page.items),
        returned_bytes=page.returned_bytes,
        complete=page.complete,
        continuation_token=page.continuation_token,
        reason_codes=list(page.reason_codes),
    )


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


@router.get("/receipts/{receipt_id}", status_code=status.HTTP_200_OK)
async def get_context_resolution_receipt(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    receipt_id: Annotated[uuid.UUID, Path()],
) -> dict[str, Any]:
    """Return one receipt the caller is entitled to read.

    A receipt in another tenant is reported as not-found rather than
    forbidden. Distinguishing the two would confirm the receipt exists,
    which is itself information the caller is not entitled to.
    """
    arc_ctx = _arc_context(request, ctx)
    receipts: ReceiptReader = request.app.state.arc_receipt_reader
    try:
        return await receipts.get_receipt(arc_ctx, receipt_id)
    except (NotFoundError, ArcAuthorizationError) as exc:
        raise build_error(
            status.HTTP_404_NOT_FOUND, code="not_found", message="receipt not found"
        ) from exc


@router.get("/receipts/{receipt_id}/explain", status_code=status.HTTP_200_OK)
async def explain_context_resolution(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    receipt_id: Annotated[uuid.UUID, Path()],
) -> dict[str, Any]:
    """Why this resolution produced the status it did.

    Answers "what applied to me, and what stopped me" from the receipt's own
    record rather than by re-running selection -- a re-run could disagree
    with what actually happened, which is the one thing an explanation must
    never do.
    """
    arc_ctx = _arc_context(request, ctx)
    receipts: ReceiptReader = request.app.state.arc_receipt_reader
    try:
        return await receipts.explain(arc_ctx, receipt_id)
    except (NotFoundError, ArcAuthorizationError) as exc:
        raise build_error(
            status.HTTP_404_NOT_FOUND, code="not_found", message="receipt not found"
        ) from exc


# ---------------------------------------------------------------------------
# Verification metadata
# ---------------------------------------------------------------------------


@router.get("/metadata", status_code=status.HTTP_200_OK)
async def get_verification_metadata(request: Request) -> dict[str, Any]:
    """Published key history, so an external verifier can check a receipt.

    Deliberately unauthenticated: it carries only public keys and profile
    names, and a verifier holding a receipt may not be a registry caller at
    all. Retired and compromised keys stay listed -- a receipt signed two
    years ago must remain verifiable, and dropping a compromised key would
    both break that and hide the compromise.
    """
    signing = request.app.state.arc_signing
    return {
        "receipt_event_signature_profile": "arc_receipt_event_sig_v1",
        "canonical_profiles": [
            "arc_manifest_claims_v1",
            "arc_context_bundle_content_v1",
            "arc_host_attestation_v1_payload",
            "arc_receipt_event_v1",
        ],
        "keys": [
            {
                "key_id": entry.key_id,
                "algorithm": entry.algorithm,
                "purpose": entry.purpose,
                "public_key": entry.public_key_b64,
                "signature_profile": entry.signature_profile,
                "valid_from": entry.valid_from,
                "valid_until": entry.valid_until,
                "compromised_at": entry.compromised_at,
                "replacement_key_id": entry.replacement_key_id,
            }
            for entry in signing.key_manifest()
        ],
    }


def arc_error_status(exc: Exception) -> int:
    """The status an ARC service exception maps to.

    Exposed so the MCP adapter can reuse exactly this mapping rather than
    inventing a parallel one that drifts.
    """
    if isinstance(exc, ManifestUnverified):
        return status.HTTP_403_FORBIDDEN
    if isinstance(exc, IdempotencyConflict | DetailIdempotencyConflict | ConflictError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, DetailDenied | ArcAuthorizationError):
        return status.HTTP_403_FORBIDDEN
    if isinstance(exc, NotFoundError):
        return status.HTTP_404_NOT_FOUND
    return status.HTTP_400_BAD_REQUEST


__all__ = ["BLOCKED_MANIFEST_UNVERIFIED", "DETAIL_DENIED", "arc_error_status", "router"]
