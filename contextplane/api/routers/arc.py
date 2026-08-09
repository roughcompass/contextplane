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
import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Header, Path, Request, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from contextplane.api.container import Services
from contextplane.api.errors import build_error
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.schemas.arc_authoring import (
    ConnectorFetchRequest,
    DetachedSignatureProof,
    SourceEvidenceResponse,
    UploadAdmissionRequest,
    VerifierAttestationProof,
)
from contextplane.arc import (
    CANONICAL_PROFILE_VERSIONS,
    ApprovalProof,
    ArcAuthorizationError,
    ArcRequestContext,
    ArcVocabularyError,
    AttestationEnvelope,
    ChallengeService,
    ConnectorFetchAdmission,
    CorpusReader,
    DetailDenied,
    DetailIdempotencyConflict,
    DetailRequest,
    IdempotencyConflict,
    JitService,
    ManifestClaims,
    ManifestUnverified,
    ReceiptReader,
    ResolutionRequest,
    ResolutionService,
    SourceAdmissionRefused,
    SourceAdmissionService,
    SourceEvidence,
    SourceIdempotencyConflict,
    UploadAdmission,
    iter_upload_file,
    manifest_claims_digest,
    parse_manifest,
)
from contextplane.exceptions import ConflictError, NotFoundError
from contextplane.types import TenantContext

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


class ManifestBody(_Strict):
    """The task manifest, in exactly the field set the host canonicalized.

    Every field is a string or a list of strings because that is what was
    signed. Re-typing one here would change the bytes the digest is computed
    over, and the attestation would stop verifying against a manifest the
    caller did in fact send.
    """

    session_id: str = Field(min_length=1, max_length=200)
    task_kind: str = Field(min_length=1, max_length=64)
    requested_action_classes: list[str] = Field(default_factory=list)
    capability_ids: list[str] = Field(default_factory=list)
    domain_ids: list[str] = Field(default_factory=list)
    environment: str = Field(min_length=1, max_length=64)
    data_sensitivity: str = Field(min_length=1, max_length=64)
    repository_identity: str = Field(min_length=1, max_length=512)
    supported_context_bundle_content_profiles: list[str] = Field(default_factory=list)
    task_summary: str | None = Field(default=None, max_length=2000)


class AttestationBody(_Strict):
    """The host's signed envelope, passed through untouched.

    `payload` stays an open string map rather than a typed model: it is the
    object the host canonicalized and signed, and validating its shape here
    would mean re-encoding it to check the signature.
    """

    profile: str = Field(min_length=1, max_length=128)
    signer_key_id: str = Field(min_length=1, max_length=200)
    attestation_id: str = Field(min_length=1, max_length=200)
    issued_at: datetime.datetime
    expires_at: datetime.datetime
    payload: dict[str, str]
    signature: str = Field(min_length=1, max_length=1024)


class ResolveContextRequest(_Strict):
    manifest: ManifestBody
    attestation: AttestationBody
    max_context_bytes: int = Field(default=12288, ge=1024, le=65536)


class ResolveContextResponse(BaseModel):
    profile: str
    receipt_id: uuid.UUID
    status: str
    replayed: bool
    directives: list[dict[str, Any]] = Field(default_factory=list)
    cap_facts: list[dict[str, Any]] = Field(default_factory=list)
    rendered_content_bytes: int = 0
    budget_limit_bytes: int = 0
    blocked_reasons: list[str] = Field(default_factory=list)
    degraded_reasons: list[str] = Field(default_factory=list)
    omission_reasons: list[str] = Field(default_factory=list)


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
# Resolution
# ---------------------------------------------------------------------------


@router.post("/resolve", response_model=ResolveContextResponse, status_code=status.HTTP_200_OK)
async def resolve_context(
    request: Request,
    body: ResolveContextRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> ResolveContextResponse:
    """Resolve a manifest into a governed context bundle.

    The two translation rules this module opens with both land here. A
    `blocked` bundle returns 200 with its receipt, because it was
    authenticated and the receipt explains itself. An unverified manifest
    returns 403 with no receipt and one bounded reason code.

    The corpus is assembled before the resolution transaction opens, which
    is what lets selection stay a pure function of its input. The clock is
    read once inside the service and applied to that input, so a candidate
    is never selected under one instant and evaluated under another.
    """
    arc_ctx = _arc_context(request, ctx)
    host_id = _require_host(arc_ctx)

    claims = ManifestClaims(
        session_id=body.manifest.session_id,
        task_kind=body.manifest.task_kind,
        requested_action_classes=tuple(body.manifest.requested_action_classes),
        capability_ids=tuple(body.manifest.capability_ids),
        domain_ids=tuple(body.manifest.domain_ids),
        environment=body.manifest.environment,
        data_sensitivity=body.manifest.data_sensitivity,
        repository_identity=body.manifest.repository_identity,
        supported_context_bundle_content_profiles=tuple(body.manifest.supported_context_bundle_content_profiles),
        task_summary=body.manifest.task_summary,
    )

    try:
        manifest = parse_manifest(claims)
    except ArcVocabularyError as exc:
        # A closed vocabulary refused the value. Safe to report specifically:
        # the caller sent it, so it tells them nothing they did not already
        # know, and "task_kind is not one of ours" is otherwise a very
        # confusing 403.
        raise build_error(status.HTTP_400_BAD_REQUEST, code="invalid_manifest", message=str(exc)) from exc

    # Checked after the request is validated, not before. Whether the body is
    # well-formed does not depend on how this deployment is configured, and
    # answering "not configured" to a caller whose manifest is malformed
    # sends them looking at the wrong thing -- while leaving the closed-shape
    # rejection Pydantic already performs inconsistent with this one.
    services: Services = request.app.state.services
    resolution: ResolutionService | None = services.arc_resolution
    corpus: CorpusReader | None = services.arc_corpus
    if resolution is None or corpus is None:
        # Resolution needs a configured key hierarchy -- receipts are signed
        # and the retained response is sealed. A deployment without one
        # cannot produce a receipt it could later stand behind, and issuing
        # an unsigned one would be worse than refusing.
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="context resolution is not configured on this deployment",
        )

    # One clock read for the whole request: the corpus is assembled at this
    # instant and selection evaluates against the same one.
    as_of = request.app.state.arc_clock.now()
    candidates = await corpus.assemble(
        tenant_id=arc_ctx.tenant_id,
        manifest=manifest,
        as_of=as_of,
    )

    try:
        outcome = await resolution.resolve(
            ResolutionRequest(
                ctx=arc_ctx,
                host_id=host_id,
                manifest=claims,
                envelope=AttestationEnvelope(
                    profile=body.attestation.profile,
                    signer_key_id=body.attestation.signer_key_id,
                    attestation_id=body.attestation.attestation_id,
                    issued_at=body.attestation.issued_at,
                    expires_at=body.attestation.expires_at,
                    payload=dict(body.attestation.payload),
                    signature=body.attestation.signature,
                ),
                manifest_fingerprint=manifest_claims_digest(claims.as_claims_dict()),
                candidates=candidates,
                budget_limit_bytes=body.max_context_bytes,
            ),
            as_of=as_of,
        )
    except ManifestUnverified as exc:
        # One code for every underlying cause. An expired attestation, an
        # unknown signer key, a consumed challenge, and a bad signature are
        # deliberately indistinguishable: which check failed is exactly the
        # probing signal an attacker wants.
        raise build_error(
            status.HTTP_403_FORBIDDEN,
            code=BLOCKED_MANIFEST_UNVERIFIED,
            message="the manifest is not backed by a trusted attestation",
        ) from exc
    except IdempotencyConflict as exc:
        raise build_error(
            status.HTTP_409_CONFLICT,
            code="idempotency_conflict",
            message="this attestation already resolved a different manifest",
        ) from exc

    bundle = outcome.bundle
    return ResolveContextResponse(
        profile="arc_context_bundle_content_v1",
        receipt_id=outcome.receipt_id,
        status=str(outcome.status),
        replayed=outcome.replayed,
        # A replay returns the receipt and status but no bundle: the original
        # response is sealed in the receipt, and re-assembling it here would
        # risk handing back content that differs from what was actually
        # given. The caller retried and gets told it was a retry.
        directives=[] if bundle is None else [dict(d) for d in bundle.directives],
        cap_facts=[] if bundle is None else [dict(f) for f in bundle.cap_facts],
        rendered_content_bytes=0 if bundle is None else bundle.rendered_content_bytes,
        budget_limit_bytes=0 if bundle is None else bundle.budget_limit_bytes,
        blocked_reasons=[] if bundle is None else list(bundle.blocked_reasons),
        degraded_reasons=[] if bundle is None else list(bundle.degraded_reasons),
        omission_reasons=[] if bundle is None else list(bundle.omission_reasons),
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
        raise build_error(status.HTTP_404_NOT_FOUND, code="not_found", message="receipt not found") from exc


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
        raise build_error(status.HTTP_404_NOT_FOUND, code="not_found", message="receipt not found") from exc


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
        # From the same mapping a receipt records as its provenance. Listed
        # here as a literal, this had already drifted: it omitted the
        # approval-evidence profile, so a verifier checking evidence against
        # the advertised set would conclude ARC does not canonicalize it.
        "canonical_profiles": sorted(CANONICAL_PROFILE_VERSIONS.values()),
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


# ---------------------------------------------------------------------------
# Source admission (ADR 039 §1)
# ---------------------------------------------------------------------------


def _source_admission(request: Request) -> SourceAdmissionService:
    services: Services = request.app.state.services
    return services.arc_source_admission


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise build_error(
            status.HTTP_400_BAD_REQUEST,
            code="arc_source_admission_refused",
            message="the Idempotency-Key header is required for source admission",
        )
    return idempotency_key


def _adapt_proof(proof: DetachedSignatureProof | VerifierAttestationProof) -> ApprovalProof:
    """Wire `ApprovalProof` union -> the service's own plain shape.

    Kept at the router boundary so `source_admission.py` never imports a
    pydantic request model.
    """
    if isinstance(proof, DetachedSignatureProof):
        return ApprovalProof(
            verification_method=proof.verification_method.value,
            signature_algorithm=proof.signature_algorithm.value,
            signature_base64=proof.signature_base64,
        )
    return ApprovalProof(
        verification_method=proof.verification_method.value,
        provider_id=proof.provider_id,
        assertion_format=proof.assertion_format,
        assertion_base64=proof.assertion_base64,
    )


def _evidence_response(evidence: SourceEvidence) -> SourceEvidenceResponse:
    return SourceEvidenceResponse(
        source_evidence_id=evidence.source_evidence_id,
        source_system=evidence.source_system,
        source_revision_locator=evidence.source_revision_locator,
        source_content_digest=evidence.source_content_digest,
        source_content_type=evidence.source_content_type,
        source_content_bytes=evidence.source_content_bytes,
        admission_method=evidence.admission_method,  # type: ignore[arg-type]
        connector_id=evidence.connector_id,
        policy_id=evidence.policy_id,
        verification_method=evidence.verification_method,  # type: ignore[arg-type]
        verifier_id=evidence.verifier_id,
        admitted_at=evidence.admitted_at,
        verified_at=evidence.verified_at,
        expires_at=evidence.expires_at,
        status=evidence.status,  # type: ignore[arg-type]
        status_checked_at=evidence.status_checked_at,
        next_check_at=evidence.next_check_at,
    )


def _translate_source_admission_error(exc: Exception) -> Exception:
    """One place, so a new source-admission route cannot invent its own
    mapping and report the same failure with a different status."""
    if isinstance(exc, SourceAdmissionRefused):
        return build_error(status.HTTP_400_BAD_REQUEST, code="arc_source_admission_refused", message=str(exc))
    if isinstance(exc, SourceIdempotencyConflict):
        return build_error(status.HTTP_409_CONFLICT, code="arc_idempotency_conflict", message=str(exc))
    if isinstance(exc, ArcAuthorizationError):
        return build_error(status.HTTP_403_FORBIDDEN, code="forbidden", message="not permitted")
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message="source evidence not found")
    if isinstance(exc, ConflictError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    return exc


def _upload_admission_metadata(metadata: Annotated[str, Form()]) -> UploadAdmissionRequest:
    """Parse the multipart `metadata` part against the closed request model.

    A plain `Form()` string, not a declared body, so FastAPI's own request-
    validation path never sees it; a malformed part is re-raised as
    `RequestValidationError` so it still comes back through the one
    exception handler every other 422 in this API goes through, rather
    than escaping as an unhandled 500.
    """
    try:
        return UploadAdmissionRequest.model_validate_json(metadata)
    except PydanticValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


async def admit_source_upload(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    metadata: Annotated[UploadAdmissionRequest, Depends(_upload_admission_metadata)],
    body: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SourceEvidenceResponse:
    """Admit source bytes the caller uploads directly.

    The `body` part streams through a hard 10 MiB ceiling while this
    deployment hashes it; the claim's own `source_content_digest` is
    checked against that computed digest, never trusted in its place.
    """
    arc_ctx = _arc_context(request, ctx)
    key = _require_idempotency_key(idempotency_key)
    admission = UploadAdmission(
        policy_id=metadata.policy_id,
        source_system=metadata.source_system,
        source_revision_locator=metadata.source_revision_locator,
        source_content_type=metadata.source_content_type,
        claim=metadata.claim.model_dump(mode="json"),
        verifier_id=metadata.verifier_id,
        proof=_adapt_proof(metadata.proof),
        idempotency_key=key,
    )
    try:
        evidence = await _source_admission(request).admit_upload(arc_ctx, admission, iter_upload_file(body.read))
    except Exception as exc:
        raise _translate_source_admission_error(exc) from exc
    return _evidence_response(evidence)


async def admit_source_connector_fetch(
    request: Request,
    body: ConnectorFetchRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SourceEvidenceResponse:
    """Admit source bytes a registered connector fetches.

    The caller names only a registered connector and immutable locator; it
    can never supply a fetch host, credential, or redirect target — those
    are validated against the connector's own allowlist on every hop.
    """
    arc_ctx = _arc_context(request, ctx)
    key = _require_idempotency_key(idempotency_key)
    admission = ConnectorFetchAdmission(
        connector_id=body.connector_id,
        source_revision_locator=body.source_revision_locator,
        claim=body.claim.model_dump(mode="json"),
        verifier_id=body.verifier_id,
        proof=_adapt_proof(body.proof),
        idempotency_key=key,
    )
    try:
        evidence = await _source_admission(request).admit_connector_fetch(arc_ctx, admission)
    except Exception as exc:
        raise _translate_source_admission_error(exc) from exc
    return _evidence_response(evidence)


async def get_source_evidence(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    source_evidence_id: Annotated[uuid.UUID, Path()],
) -> SourceEvidenceResponse:
    arc_ctx = _arc_context(request, ctx)
    try:
        evidence = await _source_admission(request).get_evidence(arc_ctx, source_evidence_id)
    except Exception as exc:
        raise _translate_source_admission_error(exc) from exc
    return _evidence_response(evidence)


async def get_source_body(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    source_evidence_id: Annotated[uuid.UUID, Path()],
) -> Response:
    arc_ctx = _arc_context(request, ctx)
    try:
        content, content_type = await _source_admission(request).get_body(arc_ctx, source_evidence_id)
    except Exception as exc:
        raise _translate_source_admission_error(exc) from exc
    return Response(content=content, media_type=content_type)


_mode, _sep = get_mode_settings()
_mr = HttpMethodRouter(router, mode=_mode, separator=_sep)
_mr.add_mutation_route(
    path="/sources/uploads",
    action="admit",
    handler=admit_source_upload,
    verb="POST",
    response_model=SourceEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
_mr.add_mutation_route(
    path="/sources/connector-fetches",
    action="admit",
    handler=admit_source_connector_fetch,
    verb="POST",
    response_model=SourceEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
_mr.add_read_route(
    path="/sources/{source_evidence_id}",
    handler=get_source_evidence,
    response_model=SourceEvidenceResponse,
    status_code=status.HTTP_200_OK,
)
_mr.add_read_route(
    path="/sources/{source_evidence_id}/body",
    handler=get_source_body,
    status_code=status.HTTP_200_OK,
)


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
