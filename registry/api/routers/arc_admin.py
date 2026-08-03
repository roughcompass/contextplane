"""ARC administration: registering and moving governed policy content.

REST only, deliberately not MCP. These routes mutate the governance context
that agents are then bound by, and an agent able to edit the rules it is
judged against is the failure this whole subsystem exists to prevent. The
read and resolution surfaces are available over both transports; this one is
not.

**Global operations authorize on identity, not role.** Every role in this
system is tenant-scoped -- each tenant has its own admins -- so no role can
serve as the deployment trust root. Deployment-wide writes match an exact
`(issuer, subject)` pair from an environment-backed allowlist instead, which
is an identity no tenant can grant itself.

**The allowlist is never echoed.** Audit records a fingerprint of it, so an
operator can tell *which* allowlist was in force when something was approved
without the audit log becoming a directory of privileged identities.

Every route here delegates its decision to `ArcAuthorizationService` or to
the operator-allowlist check below. A route that grew its own inline
comparison would be a second place for the two to drift apart, which is
exactly what the chokepoint exists to prevent -- and there is a test
asserting no route does.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from registry.api.errors import build_error
from registry.api.middleware.tenant import get_tenant_context
from registry.arc.service.artifact import ArtifactLifecycleError, ArtifactService
from registry.arc.service.authorization import ArcAuthorizationError
from registry.arc.service.exception import (
    ExceptionApproval,
    ExceptionDraft,
    ExceptionNotPermitted,
    ExceptionService,
)
from registry.arc.types import ArcRequestContext, AuthorityScope
from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.types import TenantContext

_log = logging.getLogger(__name__)

router = APIRouter(tags=["arc: admin"], prefix="/v1/arc/admin")


def operator_allowlist_fingerprint(allowlist: tuple[tuple[str, str], ...]) -> str:
    """A stable digest of the allowlist, for the audit record.

    Sorted so two deployments configured with the same operators in a
    different order fingerprint identically -- otherwise an audit trail
    would appear to show a configuration change that never happened.

    The digest, never the list: an audit log that enumerated privileged
    identities would hand an attacker the exact set of principals worth
    compromising.
    """
    material = "|".join(f"{issuer}\x00{subject}" for issuer, subject in sorted(allowlist))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _arc_context(request: Request, ctx: TenantContext) -> ArcRequestContext:
    claims = getattr(request.state, "oidc_claims", None) or {}
    try:
        return ArcRequestContext.from_validated_claims(ctx, claims)
    except ValueError as exc:
        raise build_error(
            status.HTTP_401_UNAUTHORIZED,
            code="unauthenticated",
            message="the credential carries no validated issuer",
        ) from exc


def _require_global_operator(request: Request, arc_ctx: ArcRequestContext) -> None:
    """Gate a deployment-wide operation on the exact identity pair.

    Compared as a whole pair. A subject that matches under an unexpected
    issuer is a different principal, and treating the subject alone as
    sufficient would let any IdP the deployment trusts mint operators.
    """
    settings = request.app.state.settings
    allowlist: tuple[tuple[str, str], ...] = tuple(getattr(settings, "arc_global_operator_allowlist", ()))
    if arc_ctx.operator_identity not in allowlist:
        raise build_error(
            status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="this operation requires deployment operator identity",
        )


def _artifacts(request: Request) -> ArtifactService:
    service = getattr(request.app.state, "arc_artifacts", None)
    if service is None:
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="ARC artifact administration is not configured on this deployment",
        )
    return service  # type: ignore[no-any-return]


class _Strict(BaseModel):
    """Closed request models.

    A misspelled field is rejected rather than dropped: an operator who
    believes they set a retention date and did not has registered
    governance that will behave differently from what they intended.
    """

    model_config = ConfigDict(extra="forbid")


class AttachEvidenceRequest(_Strict):
    evidence_id: uuid.UUID


class ActivateRequest(_Strict):
    supersedes: uuid.UUID | None = None


class RevokeRequest(_Strict):
    reason: str = Field(min_length=1, max_length=500)


class RevokeVerifierRequest(_Strict):
    reason: str = Field(min_length=1, max_length=500)


class RegisterVerifierRequest(_Strict):
    """Admit an approval verifier as a trust root.

    `public_key` is base64 on the wire and raw bytes in the service: the
    encoding is a transport concern. Note what is *absent* -- no private key,
    ever. Registration records the public half; the signing half stays with the
    approver, which is what separates registrar from signer.
    """

    approval_verifier_id: str = Field(min_length=1, max_length=200)
    verifier_kind: str = Field(pattern=r"^(operator_public_key|trusted_attestation_provider)$")
    allowed_evidence_types: list[str] = Field(min_length=1)
    scope_kind: str = Field(pattern=r"^(global|tenant)$")
    scope_tenant_id: uuid.UUID | None = None
    algorithm: str | None = Field(default=None, max_length=64)
    public_key: str | None = Field(default=None, max_length=512)
    provider_id: str | None = Field(default=None, max_length=200)
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None


class ExceptionApprovalBody(_Strict):
    """The evidence that authorizes one exception.

    Carried in full rather than as a bare evidence id. The service writes
    the evidence row and the exception row in one transaction -- they
    reference each other, which is why both foreign keys are deferrable --
    so there is no pre-existing evidence for an id to point at. Accepting
    one would mean either writing an exception with no approval or leaving
    evidence pointing at an exception that never gets created.
    """

    evidence_id: uuid.UUID
    approval_verifier_id: str = Field(min_length=1, max_length=200)
    approving_principal: str = Field(min_length=1, max_length=200)
    approving_role: str = Field(min_length=1, max_length=100)
    approved_payload_digest: str = Field(min_length=64, max_length=64)
    audit_log_reference: str = Field(min_length=1, max_length=500)
    approval_timestamp: datetime.datetime
    verifier_attestation: dict[str, Any] = Field(default_factory=dict)
    verifier_identity: str = Field(default="", max_length=200)


class ApproveExceptionRequest(_Strict):
    higher_scope_directive_id: uuid.UUID
    higher_scope_revision_id: uuid.UUID
    lower_scope_kind: str = Field(pattern=r"^(tenant|domain|capability|task)$")
    replacement_conflict_descriptor: dict[str, Any]
    approval: ExceptionApprovalBody
    effective_from: datetime.datetime
    exception_statement: str = Field(min_length=1, max_length=4000)
    justification: str = Field(min_length=1, max_length=4000)
    effective_until: datetime.datetime | None = None
    lower_scope_domain_id: str | None = Field(default=None, max_length=200)
    lower_scope_capability_id: uuid.UUID | None = None
    lower_scope_task_kind: str | None = Field(default=None, max_length=64)
    lower_scope_action_class: str | None = Field(default=None, max_length=64)
    lower_scope_environment: str | None = Field(default=None, max_length=64)
    lower_scope_data_sensitivity: str | None = Field(default=None, max_length=64)


class _Accepted(BaseModel):
    status: str
    revision_id: uuid.UUID | None = None
    approval_verifier_id: str | None = None
    evidence_id: uuid.UUID | None = None
    exception_id: uuid.UUID | None = None


def _translate(exc: Exception) -> Exception:
    """Map a service exception onto the HTTP envelope.

    One place, so a new route cannot invent its own mapping and report the
    same failure with a different status than an existing one.
    """
    if isinstance(exc, ArcAuthorizationError):
        return build_error(status.HTTP_403_FORBIDDEN, code="forbidden", message="not permitted")
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message="not found")
    if isinstance(exc, ExceptionNotPermitted):
        # 409 rather than 403: the caller may well be entitled to create
        # exceptions in general. What is refused is the *target* -- a
        # property of the governance, not of the caller -- and reporting it
        # as forbidden would send an operator to check their permissions.
        return build_error(status.HTTP_409_CONFLICT, code="exception_not_permitted", message=str(exc))
    if isinstance(exc, ArtifactLifecycleError):
        return build_error(status.HTTP_409_CONFLICT, code="lifecycle_conflict", message=str(exc))
    if isinstance(exc, ConflictError):
        return build_error(status.HTTP_409_CONFLICT, code="conflict", message=str(exc))
    if isinstance(exc, ValidationError):
        return build_error(status.HTTP_400_BAD_REQUEST, code="validation_error", message=str(exc))
    if isinstance(exc, IntegrityError):
        # A constraint the service did not check first. Reported as a conflict
        # rather than escaping as a 500: the request named something the
        # database refused, which is the caller's problem to see, and the
        # driver's message would otherwise leak SQL into the response.
        # Every such case is also a service-layer check that should exist --
        # this is the backstop, not the intended path.
        _log.warning("arc_admin.unchecked_constraint: %s", exc.orig)
        return build_error(
            status.HTTP_409_CONFLICT,
            code="conflict",
            message="the request conflicts with existing governance state",
        )
    return exc


# ---------------------------------------------------------------------------
# Revision lifecycle
# ---------------------------------------------------------------------------


@router.post("/revisions/{revision_id}/approval-evidence", response_model=_Accepted)
async def attach_approval_evidence(
    request: Request,
    body: AttachEvidenceRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    revision_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """Link a draft revision to the evidence approving it.

    A separate step from registration because the ordering is forced:
    activation evidence must name the revision it approves, and that id does
    not exist until the revision has been registered.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _artifacts(request).attach_approval_evidence(arc_ctx, revision_id, body.evidence_id)
    except Exception as exc:  # noqa: BLE001 - re-raised through one mapping
        raise _translate(exc) from exc
    return _Accepted(status="attached", revision_id=revision_id, evidence_id=body.evidence_id)


@router.post("/revisions/{revision_id}/activate", response_model=_Accepted)
async def activate_revision(
    request: Request,
    body: ActivateRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    revision_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """Put a revision into force, superseding the incumbent."""
    arc_ctx = _arc_context(request, ctx)
    try:
        await _artifacts(request).activate(arc_ctx, revision_id, supersedes=body.supersedes)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return _Accepted(status="active", revision_id=revision_id)


@router.post("/revisions/{revision_id}/revoke", response_model=_Accepted)
async def revoke_revision(
    request: Request,
    body: RevokeRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    revision_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """Withdraw a revision from force. Terminal.

    Any mandatory obligation it satisfied becomes a tombstone rather than
    disappearing, so matching resolutions keep blocking until an approved
    successor satisfies it.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _artifacts(request).revoke(arc_ctx, revision_id, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return _Accepted(status="revoked", revision_id=revision_id)


@router.post("/revisions/{revision_id}/invalidate", response_model=_Accepted)
async def invalidate_revision(
    request: Request,
    body: RevokeRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    revision_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """Mark a revision's content no longer trustworthy.

    Distinct from revocation: that says the rule no longer applies, this
    says the content itself was wrong or its upstream source is gone. The
    obligation tombstones differently so an auditor can tell them apart.

    Operator-driven rather than automatic, because deciding registered
    content is wrong is a judgement no worker should make.
    """
    arc_ctx = _arc_context(request, ctx)
    try:
        await _artifacts(request).invalidate(arc_ctx, revision_id, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return _Accepted(status="invalidated", revision_id=revision_id)


# ---------------------------------------------------------------------------
# Approval trust
# ---------------------------------------------------------------------------


@router.post("/approval-verifiers/{approval_verifier_id}/revoke", response_model=_Accepted)
async def revoke_approval_verifier(
    request: Request,
    body: RevokeVerifierRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    approval_verifier_id: Annotated[str, Path(min_length=1, max_length=200)],
) -> _Accepted:
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

    trust = getattr(request.app.state, "arc_approval_trust", None)
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
    await trust.revoke_verifier(arc_ctx, approval_verifier_id, reason=body.reason)
    return _Accepted(status="revoked", approval_verifier_id=approval_verifier_id)


@router.post("/approval-evidence/{evidence_id}/revoke", response_model=_Accepted)
async def revoke_approval_evidence(
    request: Request,
    body: RevokeVerifierRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    evidence_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """Withdraw one piece of approval evidence.

    Narrower than revoking a verifier: the verifier stays trusted, but this
    particular approval no longer counts -- an approval granted in error, or
    one whose approver turned out to lack the authority.
    """
    arc_ctx = _arc_context(request, ctx)
    _require_global_operator(request, arc_ctx)

    trust = getattr(request.app.state, "arc_approval_trust", None)
    if trust is None:
        raise build_error(
            status.HTTP_501_NOT_IMPLEMENTED,
            code="not_implemented",
            message="approval evidence revocation is not available on this deployment",
        )
    await trust.revoke_evidence(arc_ctx, evidence_id, reason=body.reason)
    return _Accepted(status="revoked", evidence_id=evidence_id)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def _exceptions(request: Request) -> ExceptionService:
    service = getattr(request.app.state, "arc_exceptions", None)
    if service is None:
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="ARC exception administration is not configured on this deployment",
        )
    return service  # type: ignore[no-any-return]


@router.post("/exceptions", response_model=_Accepted, status_code=status.HTTP_201_CREATED)
async def approve_context_exception(
    request: Request,
    body: ApproveExceptionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> _Accepted:
    """Approve an exception narrowing a higher-scope directive.

    Tenant-scoped rather than operator-gated: narrowing a rule *within* your
    own tenant is a tenant decision. What stops that becoming an escape
    hatch is the delegability check in the service -- a tenant cannot except
    a global directive that does not permit it, or global governance would
    be advisory.

    The exception's tenant is taken from the authenticated context, never
    from the body: one a caller could file against another tenant would be
    a way to weaken somebody else's rules.
    """
    arc_ctx = _arc_context(request, ctx)
    draft = ExceptionDraft(
        higher_scope_directive_id=body.higher_scope_directive_id,
        higher_scope_revision_id=body.higher_scope_revision_id,
        lower_scope_kind=AuthorityScope(body.lower_scope_kind),
        replacement_conflict_descriptor=body.replacement_conflict_descriptor,
        approval=ExceptionApproval(
            evidence_id=body.approval.evidence_id,
            approval_verifier_id=body.approval.approval_verifier_id,
            approving_principal=body.approval.approving_principal,
            approving_role=body.approval.approving_role,
            approved_payload_digest=body.approval.approved_payload_digest,
            audit_log_reference=body.approval.audit_log_reference,
            approval_timestamp=body.approval.approval_timestamp,
            verifier_attestation=dict(body.approval.verifier_attestation),
            verifier_identity=body.approval.verifier_identity,
        ),
        effective_from=body.effective_from,
        exception_statement=body.exception_statement,
        justification=body.justification,
        effective_until=body.effective_until,
        lower_scope_domain_id=body.lower_scope_domain_id,
        lower_scope_capability_id=body.lower_scope_capability_id,
        lower_scope_task_kind=body.lower_scope_task_kind,
        lower_scope_action_class=body.lower_scope_action_class,
        lower_scope_environment=body.lower_scope_environment,
        lower_scope_data_sensitivity=body.lower_scope_data_sensitivity,
    )
    try:
        exception_id = await _exceptions(request).approve_exception(arc_ctx, draft)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return _Accepted(status="approved", exception_id=exception_id)


@router.post("/exceptions/{exception_id}/revoke", response_model=_Accepted)
async def revoke_context_exception(
    request: Request,
    body: RevokeRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    exception_id: Annotated[uuid.UUID, Path()],
) -> _Accepted:
    """Withdraw an exception, restoring the directive it narrowed."""
    arc_ctx = _arc_context(request, ctx)
    try:
        await _exceptions(request).revoke_exception(arc_ctx, exception_id, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return _Accepted(status="revoked", exception_id=exception_id)


@router.post("/approval-verifiers", response_model=_Accepted, status_code=status.HTTP_201_CREATED)
async def register_approval_verifier(
    request: Request,
    body: RegisterVerifierRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> _Accepted:
    """Admit an approval verifier, deployment-wide.

    Operator identity regardless of the verifier's own scope, matching
    revocation. Registering a verifier decides *who counts as an approver*,
    and its blast radius is every activation and exception that verifier will
    ever vouch for -- the same blast radius revocation has, and therefore the
    same gate.

    This is not a way to forge an approval. What is recorded is a public key or
    a provider id; the private half stays with the approver, so the registrar
    and the signer are different parties by construction. An operator who
    registers a key they also hold is both, which no check at this layer can
    prevent -- so registration audits the credential and allowlist
    fingerprints instead, and an auditor can prove which configuration
    admitted which verifier.
    """
    arc_ctx = _arc_context(request, ctx)
    _require_global_operator(request, arc_ctx)

    registry = getattr(request.app.state, "arc_verifier_registry", None)
    if registry is None:
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="ARC approval-verifier administration is not configured on this deployment",
        )

    public_key: bytes | None = None
    if body.public_key is not None:
        try:
            public_key = base64.b64decode(body.public_key, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise build_error(
                status.HTTP_400_BAD_REQUEST,
                code="validation_error",
                message="public_key must be base64",
            ) from exc

    settings = request.app.state.settings
    allowlist: tuple[tuple[str, str], ...] = tuple(getattr(settings, "arc_global_operator_allowlist", ()))

    try:
        verifier_id = await registry.register(
            arc_ctx,
            approval_verifier_id=body.approval_verifier_id,
            verifier_kind=body.verifier_kind,
            allowed_evidence_types=frozenset(body.allowed_evidence_types),
            scope_kind=body.scope_kind,
            scope_tenant_id=body.scope_tenant_id,
            algorithm=body.algorithm,
            public_key=public_key,
            provider_id=body.provider_id,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            allowlist_fingerprint=operator_allowlist_fingerprint(allowlist),
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return _Accepted(status="registered", approval_verifier_id=verifier_id)


@router.get("/operator-identity", response_model=dict)
async def describe_operator_identity(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> dict[str, Any]:
    """Whether the caller holds deployment operator identity, and what this
    deployment is actually able to do.

    Exists so an operator can find out *before* attempting a governance
    write, rather than discovering it from a 403 in the middle of one. It
    reports only a boolean and the allowlist fingerprint -- never the
    allowlist, and never anyone else's membership.

    The capability flags are here rather than annotated onto each record they
    affect. What needs qualifying is a deployment-wide claim, read now; a
    caveat carried inside individual receipts is read one record at a time, at
    audit time, possibly years later. This is the one place an operator already
    checks before use.
    """
    arc_ctx = _arc_context(request, ctx)
    settings = request.app.state.settings
    allowlist: tuple[tuple[str, str], ...] = tuple(getattr(settings, "arc_global_operator_allowlist", ()))
    artifacts = getattr(request.app.state, "arc_artifacts", None)
    return {
        "is_global_operator": arc_ctx.operator_identity in allowlist,
        "allowlist_fingerprint": operator_allowlist_fingerprint(allowlist),
        # False means an activation would record an approval nothing validated,
        # so activation refuses outright rather than passing a check that
        # cannot fail.
        "approval_verification_enabled": bool(
            getattr(artifacts, "_approval_verification_enabled", False)
        ),
        # False means no receipt can be signed, so context resolution answers
        # 503 rather than issuing one it could not stand behind.
        "context_resolution_enabled": getattr(request.app.state, "arc_resolution", None) is not None,
        "checked_at": datetime.datetime.now(tz=datetime.UTC).isoformat(),
    }


__all__ = ["operator_allowlist_fingerprint", "router"]
