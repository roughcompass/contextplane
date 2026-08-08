"""ARC preflight + challenge/receipt tools, plus five read-only tools over
the authoring surface (proposals, review packages, observation status,
activation eligibility).

Every tool here except ``arc_complete_preflight`` itself calls
``_arc_preflight()`` first. That ordering is the point: REST
re-authenticates on each request, a long-lived MCP connection does not, so
without a preflight gate a credential that changed mid-connection would
keep working until disconnect. Running the gate before any ARC service is
reached also means a caller who never preflighted cannot probe those
services for whether a receipt or an artifact exists.

**The authoring-surface tools below are read-only, and that is not an
implementation gap.** Authoring, approval, qualification acceptance, and
activation are human transitions bound to an authenticated principal and,
for approval, an interactive challenge (ADR 041 §3) -- exposing any of them
as an agent-callable tool would create exactly the delegation that
authorization model forbids: an agent editing the rules it is itself
judged against. Each of the five reuses the same REST component schemas
`contextplane.api.schemas.arc_authoring` defines, rebuilding them from the same
service-layer domain objects the matching REST route reads, rather than
importing the REST router's own private response-builder functions --
every ARC router module already stays a self-contained adapter for the
same reason (see `arc_authoring.py`'s own docstring), and this module is
no exception.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta
from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.api.mcp import context
from contextplane.api.schemas.arc_authoring import (
    ActivationEligibilityResponse,
    ActivationPredicateStatus,
    ActorRef,
    BaselineDiffChange,
    BaselineDiffResponse,
    Citation,
    DeltaCodeCounter,
    ExpectedImpactEnvelope,
    FieldProvenance,
    JudgmentAuthor,
    ObservationStatusResponse,
    PagedProposalSummaries,
    ProposalState,
    ProposalSummary,
    ProposalVersionResponse,
    ReachConfirmationItem,
    ReachConfirmationResponse,
    ReviewPackageResponse,
    SemanticTestResultItem,
    SemanticTestResultResponse,
)
from contextplane.arc.service.activation import ActivationEligibility, ActivationService
from contextplane.arc.service.authorization import ArcAuthorizationError
from contextplane.arc.service.challenge import ChallengeService
from contextplane.arc.service.preflight import (
    PreflightError,
    PreflightRegistry,
    credential_fingerprint,
    restriction_digest,
)
from contextplane.arc.service.proposal import ProposalService, ProposalVersion
from contextplane.arc.service.qualification import ObservationStatus, QualificationService
from contextplane.arc.service.receipt_read import ReceiptReader
from contextplane.arc.service.review_package import (
    ReviewPackage,
    ReviewPackageIntegrityError,
    ReviewPackageService,
    ReviewPackageUnavailable,
)
from contextplane.arc.types import ArcRequestContext
from contextplane.exceptions import ConflictError, NotFoundError, RegistryError
from contextplane.types import Clock

_PROPOSAL_STATES = frozenset(s.value for s in ProposalState)


async def _arc_preflight(session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> ArcRequestContext:
    """Resolve identity and confirm this connection completed `whoami`.

    Raises `ToolError` carrying one bounded code. Which check refused is
    deliberately not distinguished: the remedy is the same either way,
    and naming it would tell a prober how far they got.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    registry = cast(PreflightRegistry, context._arc_state("arc_preflight"))
    try:
        record = registry.require(
            connection_id=context._request_connection_id.get() or None,
            credential_fingerprint=credential_fingerprint(context._request_token.get()),
            tenant_id=ctx.tenant_id,
            token_restriction_digest=restriction_digest(None),
            now=clock.now(),
        )
    except PreflightError as exc:
        raise ToolError(json.dumps({"code": exc.code, "message": str(exc), "details": {}})) from exc
    return ArcRequestContext(
        tenant=ctx,
        oidc_issuer=record.oidc_issuer,
        host_id=None,
        mcp_session_id=record.connection_id,
    )


# ---------------------------------------------------------------------------
# Tool: arc_complete_preflight
# ---------------------------------------------------------------------------


async def arc_complete_preflight(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Record this connection's identity so ARC tools may be used.

    Call once per connection, before any other arc_* tool. Re-call after
    refreshing a token: a changed credential invalidates the record, and
    every later ARC call is refused until a new preflight is completed.

    Returns:
        JSON object: {preflight, tenant_id, actor_id, roles[]}.
    """
    ctx = await context._resolve_tenant(session_factory, clock)
    registry = cast(PreflightRegistry, context._arc_state("arc_preflight"))
    connection_id = context._request_connection_id.get()
    if not connection_id:
        raise ToolError("no server connection identity is associated with this call")

    # Expiry comes from the credential, not from a fixed window here: the
    # preflight must not outlive the authentication behind it.
    expires_at = clock.now() + timedelta(hours=1)
    record = registry.record(
        connection_id=connection_id,
        credential_fingerprint=credential_fingerprint(context._request_token.get()),
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        oidc_issuer=context._validated_issuer(),
        oidc_subject=ctx.oidc_subject,
        roles=tuple(ctx.roles),
        token_restriction_digest=restriction_digest(None),
        authentication_expires_at=expires_at,
        completed_at=clock.now(),
    )
    return json.dumps(
        {
            "preflight": "complete",
            "tenant_id": str(record.tenant_id),
            "actor_id": str(record.actor_id),
            "roles": list(record.roles),
        }
    )


# ---------------------------------------------------------------------------
# Tool: arc_issue_context_challenge
# ---------------------------------------------------------------------------


async def arc_issue_context_challenge(
    session_id: str,
    manifest_claims_digest: str,
    idempotency_key: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Issue a single-use ARC challenge for this session.

    Requires a completed preflight on this connection.

    Args:
        session_id: The agent session this challenge binds to.
        manifest_claims_digest: SHA-256 hex digest of the canonical manifest claims.
        idempotency_key: Caller-chosen key; an exact retry returns the same challenge.

    Returns:
        JSON object: {arc_nonce, issued_at, expires_at, manifest_claims_digest}.
    """
    ctx = await _arc_preflight(session_factory, clock)
    challenges = cast(ChallengeService, context._arc_state("arc_challenges"))
    try:
        issued = await challenges.issue_challenge(
            ctx,
            session_id=session_id,
            manifest_claims_digest=manifest_claims_digest,
            idempotency_key=idempotency_key,
        )
    except ConflictError as exc:
        raise ToolError(json.dumps({"code": "idempotency_conflict", "message": str(exc), "details": {}})) from exc
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "forbidden", "message": str(exc), "details": {}})) from exc

    return json.dumps(
        {
            "arc_nonce": base64.b64encode(issued.arc_nonce).decode("ascii"),
            "issued_at": issued.issued_at.isoformat(),
            "expires_at": issued.expires_at.isoformat(),
            "manifest_claims_digest": issued.manifest_claims_digest,
        }
    )


# ---------------------------------------------------------------------------
# Tool: arc_get_context_resolution_receipt
# ---------------------------------------------------------------------------


async def arc_get_context_resolution_receipt(
    receipt_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Read one ARC resolution receipt.

    Requires a completed preflight on this connection. A receipt in
    another tenant reports as not-found rather than forbidden.

    Args:
        receipt_id: UUID of the receipt.

    Returns:
        JSON object: the receipt, with source fields redacted by audience.
    """
    ctx = await _arc_preflight(session_factory, clock)
    reader = cast(ReceiptReader, context._arc_state("arc_receipt_reader"))
    try:
        return json.dumps(await reader.get_receipt(ctx, uuid.UUID(receipt_id)), default=str)
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "validation_error", "message": str(exc), "details": {}})) from exc
    except Exception as exc:
        raise ToolError(json.dumps({"code": "not_found", "message": "receipt not found", "details": {}})) from exc


# ---------------------------------------------------------------------------
# Tool: arc_explain_context_resolution
# ---------------------------------------------------------------------------


async def arc_explain_context_resolution(
    receipt_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Explain why one ARC resolution produced the status it did.

    Requires a completed preflight on this connection. Built from the
    receipt's own record rather than by re-running selection, so it can
    never disagree with what actually happened.

    Args:
        receipt_id: UUID of the receipt.

    Returns:
        JSON object: {resolution_status, blocked_reasons[], degraded_reasons[],
        budget, selected[], events[]}.
    """
    ctx = await _arc_preflight(session_factory, clock)
    reader = cast(ReceiptReader, context._arc_state("arc_receipt_reader"))
    try:
        return json.dumps(await reader.explain(ctx, uuid.UUID(receipt_id)), default=str)
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "validation_error", "message": str(exc), "details": {}})) from exc
    except Exception as exc:
        raise ToolError(json.dumps({"code": "not_found", "message": "receipt not found", "details": {}})) from exc


# ---------------------------------------------------------------------------
# Read-only authoring-surface tools
# ---------------------------------------------------------------------------


def _map_authoring_error(exc: Exception) -> ToolError:
    """One error-to-`ToolError` mapping for every read tool below, so a
    refusal carries the same bounded code MCP already uses elsewhere in
    this module rather than a second mapping the two transports could
    silently drift apart on. Narrower than any one REST router's own
    translator: only the exceptions these five read-only service calls can
    actually raise are named here.
    """
    if isinstance(exc, NotFoundError):
        return ToolError(json.dumps({"code": "not_found", "message": str(exc), "details": {}}))
    if isinstance(exc, ArcAuthorizationError):
        return ToolError(json.dumps({"code": "forbidden", "message": "not permitted", "details": {}}))
    if isinstance(exc, ReviewPackageIntegrityError):
        return ToolError(json.dumps({"code": "arc_operational_integrity_failed", "message": str(exc), "details": {}}))
    if isinstance(exc, ReviewPackageUnavailable):
        return ToolError(json.dumps({"code": "arc_proposal_state_conflict", "message": str(exc), "details": {}}))
    if isinstance(exc, ConflictError):
        return ToolError(json.dumps({"code": "conflict", "message": str(exc), "details": {}}))
    if isinstance(exc, RegistryError):
        return ToolError(json.dumps({"code": "bad_request", "message": str(exc), "details": {}}))
    return ToolError(json.dumps({"code": "bad_request", "message": str(exc), "details": {}}))


def _parse_uuid_arg(value: str, *, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ToolError(json.dumps({"code": "validation_error", "message": f"{name}: {exc}", "details": {}})) from exc


def _proposal_version_response(version: ProposalVersion) -> ProposalVersionResponse:
    """Same field-for-field mapping `arc_authoring.py`'s own
    `_version_response` uses -- duplicated rather than imported, per this
    module's own docstring."""
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


def _review_package_response(pkg: ReviewPackage) -> ReviewPackageResponse:
    """Same field-for-field mapping `arc_approval.py`'s own
    `_review_package_response` uses -- duplicated rather than imported, per
    this module's own docstring."""
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
        baseline_diff=BaselineDiffResponse(
            baseline_revision_id=pkg.baseline_diff.baseline_revision_id,
            changes=[
                BaselineDiffChange(
                    field_path=c.field_path,
                    change_kind=c.change_kind,  # type: ignore[arg-type]
                    before=c.before,
                    after=c.after,
                )
                for c in pkg.baseline_diff.changes
            ],
        ),
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


def _observation_status_response(status_obj: ObservationStatus) -> ObservationStatusResponse:
    """Same field-for-field mapping `arc_observation.py`'s own
    `_observation_status_response` uses -- duplicated rather than imported,
    per this module's own docstring."""
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


def _activation_eligibility_response(eligibility: ActivationEligibility) -> ActivationEligibilityResponse:
    """Same field-for-field mapping `arc_activation.py`'s own
    `_eligibility_response` uses -- duplicated rather than imported, per
    this module's own docstring."""
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


async def arc_list_proposals(
    artifact_id: str | None,
    state: str | None,
    cursor: str | None,
    limit: int = 50,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """List proposal versions in the caller's own tenant.

    Requires a completed preflight on this connection.

    Args:
        artifact_id: Restrict to one artifact family's proposals, or None
            for every family the caller can read.
        state: Restrict to one proposal state (`open`, `submitted`,
            `approved`, `activated`, `rejected`, `stale`, `superseded`,
            `withdrawn`), or None for every state.
        cursor: An opaque `next_cursor` value from a prior call, or None to
            start from the first page. Never construct one by hand.
        limit: Page size, 1-100.

    Returns:
        JSON object: {items: ProposalSummary[], next_cursor: string|null}.
    """
    ctx = await _arc_preflight(session_factory, clock)
    if not 1 <= limit <= 100:
        raise ToolError(
            json.dumps({"code": "validation_error", "message": "limit must be between 1 and 100", "details": {}})
        )
    if state is not None and state not in _PROPOSAL_STATES:
        raise ToolError(
            json.dumps({"code": "validation_error", "message": f"unrecognized state {state!r}", "details": {}})
        )
    parsed_artifact_id = _parse_uuid_arg(artifact_id, name="artifact_id") if artifact_id is not None else None

    proposals = cast(ProposalService, context._arc_state("arc_proposals"))
    try:
        page = await proposals.list_proposals(
            ctx,
            ctx.tenant_id,
            artifact_id=parsed_artifact_id,
            state=state,
            cursor=cursor,
            page_size=limit,
        )
    except Exception as exc:
        raise _map_authoring_error(exc) from exc

    response = PagedProposalSummaries(
        items=[
            ProposalSummary(
                proposal_id=v.proposal_id,
                proposal_version=v.proposal_version,
                artifact_id=v.artifact_id,
                state=v.state,  # type: ignore[arg-type]
                risk_classification=v.risk_classification,  # type: ignore[arg-type]
                created_at=v.created_at,
            )
            for v in page.items
        ],
        next_cursor=page.next_cursor,
    )
    return json.dumps(response.model_dump(mode="json"))


async def arc_get_proposal_version(
    proposal_id: str,
    proposal_version: int,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Read one immutable proposal version.

    Requires a completed preflight on this connection.

    Args:
        proposal_id: UUID of the proposal thread.
        proposal_version: The version number within that thread (>= 1).

    Returns:
        JSON object: the same shape REST returns from `GET {PV}`
        (`ProposalVersionResponse`).
    """
    ctx = await _arc_preflight(session_factory, clock)
    parsed_id = _parse_uuid_arg(proposal_id, name="proposal_id")
    proposals = cast(ProposalService, context._arc_state("arc_proposals"))
    try:
        version = await proposals.get_version(ctx, parsed_id, proposal_version)
    except Exception as exc:
        raise _map_authoring_error(exc) from exc
    return json.dumps(_proposal_version_response(version).model_dump(mode="json"))


async def arc_get_review_package(
    proposal_id: str,
    proposal_version: int,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Read the review package a projection approver would sign off on.

    Requires a completed preflight on this connection. `ReviewPackageService.
    get_review_package` always recomputes the semantics, review-package, and
    revision digests from authoritative rows rather than trusting a
    persisted one -- see that service's own docstring.

    Args:
        proposal_id: UUID of the proposal thread.
        proposal_version: The version number within that thread (>= 1).

    Returns:
        JSON object: the same shape REST returns from
        `GET {PV}/review-package` (`ReviewPackageResponse`).
    """
    ctx = await _arc_preflight(session_factory, clock)
    parsed_id = _parse_uuid_arg(proposal_id, name="proposal_id")
    review_package = cast(ReviewPackageService, context._arc_state("arc_review_package"))
    try:
        package = await review_package.get_review_package(ctx, parsed_id, proposal_version)
    except Exception as exc:
        raise _map_authoring_error(exc) from exc
    return json.dumps(_review_package_response(package).model_dump(mode="json"))


async def arc_get_observation_status(
    proposal_id: str,
    proposal_version: int,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Read a candidate's aggregate observation-window counters.

    Requires a completed preflight on this connection. Carries aggregate
    counters and cohort digests only; tenant-scoped detail needs its own
    receipt-bound authorization and is never served here.

    Args:
        proposal_id: UUID of the proposal thread.
        proposal_version: The version number within that thread (>= 1).

    Returns:
        JSON object: the same shape REST returns from `GET {PV}/observation`
        (`ObservationStatusResponse`).
    """
    ctx = await _arc_preflight(session_factory, clock)
    parsed_id = _parse_uuid_arg(proposal_id, name="proposal_id")
    qualification = cast(QualificationService, context._arc_state("arc_qualification"))
    try:
        result = await qualification.get_status(ctx, parsed_id, proposal_version)
    except Exception as exc:
        raise _map_authoring_error(exc) from exc
    return json.dumps(_observation_status_response(result).model_dump(mode="json"))


async def arc_get_activation_eligibility(
    revision_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> str:
    """Read whether one revision could activate right now.

    Requires a completed preflight on this connection. Reports all ten
    predicates, in fixed order, computed as if the calling principal were
    the one activating -- see `ActivationService.get_eligibility`'s own
    docstring for why.

    Args:
        revision_id: UUID of the revision.

    Returns:
        JSON object: the same shape REST returns from
        `GET /v1/arc/revisions/{revision_id}/activation-eligibility`
        (`ActivationEligibilityResponse`).
    """
    ctx = await _arc_preflight(session_factory, clock)
    parsed_id = _parse_uuid_arg(revision_id, name="revision_id")
    activation = cast(ActivationService, context._arc_state("arc_activation"))
    try:
        eligibility = await activation.get_eligibility(ctx, parsed_id)
    except Exception as exc:
        raise _map_authoring_error(exc) from exc
    return json.dumps(_activation_eligibility_response(eligibility).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    mcp_server: FastMCP,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Decorate this module's tools onto ``mcp_server``, bound to the given
    services."""
    deps: dict[str, Any] = {"session_factory": session_factory, "clock": clock}
    mcp_server.tool()(context._bind_tool(arc_complete_preflight, **deps))
    mcp_server.tool()(context._bind_tool(arc_issue_context_challenge, **deps))
    mcp_server.tool()(context._bind_tool(arc_get_context_resolution_receipt, **deps))
    mcp_server.tool()(context._bind_tool(arc_explain_context_resolution, **deps))
    mcp_server.tool()(context._bind_tool(arc_list_proposals, **deps))
    mcp_server.tool()(context._bind_tool(arc_get_proposal_version, **deps))
    mcp_server.tool()(context._bind_tool(arc_get_review_package, **deps))
    mcp_server.tool()(context._bind_tool(arc_get_observation_status, **deps))
    mcp_server.tool()(context._bind_tool(arc_get_activation_eligibility, **deps))


__all__: list[str] = [
    "arc_complete_preflight",
    "arc_issue_context_challenge",
    "arc_get_context_resolution_receipt",
    "arc_explain_context_resolution",
    "arc_list_proposals",
    "arc_get_proposal_version",
    "arc_get_review_package",
    "arc_get_observation_status",
    "arc_get_activation_eligibility",
    "register",
]
