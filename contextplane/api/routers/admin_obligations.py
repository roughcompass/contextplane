"""Nominate a reporting obligation, classify it, and read the backlog.

The object this exposes was decided and then built by nobody for a release. It
is exposed here in the same change that creates it, because a governed record
nothing can reach is the defect this plan has now found five times: a mechanism
that is correct, tested, and consulted by nothing.

**Nominating and classifying are separate routes, not one route with a
materiality field.** A create that accepted a classification would get a guess,
because the person who noticed the thing is rarely the person entitled to
classify it. A guessed materiality is worse than an honest `unclassified`: it
stops anybody looking again.

**There is no route that classifies automatically, and there will not be one
until a threshold set is ratified.** That set is external and is not this team's
to write. The absence is deliberate and is the honest state — a placeholder
threshold behind a compliance-shaped endpoint is worse than an absent one,
because the absent one is visible.

**REST only, and that is the convention rather than an omission.** No
`/v1/admin` surface in this codebase has an MCP twin -- not quarantine, not the
PII policy editor, not the lifecycle admin. The MCP tools are the agent-facing
surface and these operations are operator-facing, so the parity rule that
applies to the memory and context surfaces does not reach here. What does reach
here is the half of that rule that matters: the refusals live in
`ReportingObligationService`, so a second transport added later inherits them
rather than needing them re-stated.

**The backlog is a read, and its healthy value is not zero.** `unclassified` is
the state most obligations are in most of the time. A surface that presented the
count as an error would train its reader to ignore it, so the response carries
the age of the longest wait beside the count -- five nominated this morning and
five nominated in March are the same number and a different situation.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from contextplane.api.container import Services
from contextplane.api.errors import map_catalog_error
from contextplane.api.routers._admin_common import _admin_required
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.governance.actors import (
    ALL_KINDS,
    DECLARABLE_KINDS,
    MAX_OWNER,
    MIN_OWNER,
    Principal,
)
from contextplane.service.governance.actors import (
    MAX_PAGE_SIZE as ACTOR_MAX_PAGE_SIZE,
)
from contextplane.service.governance.actors import (
    parse_cursor as parse_actor_cursor,
)
from contextplane.service.governance.deadlines import StampedDeadlines
from contextplane.service.governance.obligations import (
    MATERIALITY_MATERIAL,
    MATERIALITY_NOT_MATERIAL,
    ReportingObligation,
)
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/admin", tags=["admin: obligations"])


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


class NominateObligationRequest(BaseModel):
    """What may need reporting, in the reporter's own words."""

    summary: str = Field(
        min_length=10,
        max_length=4000,
        description="An obligation nobody can identify later is not a record.",
    )


class ClassifyObligationRequest(BaseModel):
    """A decision somebody made, with the reason they made it.

    `unclassified` is absent from this type on purpose: it is where a row starts
    and is not a conclusion a decision can reach. Allowing it would let an actor
    clear a classification while leaving their name recorded as having made one.
    """

    materiality: Literal["material", "not_material"]
    note: str = Field(
        min_length=20,
        max_length=2000,
        description="Why. A one-word rationale is the same as none.",
    )


class ObligationResponse(BaseModel):
    """One obligation. The classification fields are all set or all null."""

    obligation_id: uuid.UUID
    summary: str
    materiality: str
    nominated_at: datetime.datetime
    nominated_by: uuid.UUID
    classified_at: datetime.datetime | None
    classified_by: uuid.UUID | None
    classification_note: str | None

    #: Set together when a classification as material starts the clock, and null
    #: on anything not classified material. All three or none: a partial set
    #: would let a reader believe the missing ones were not due rather than not
    #: recorded.
    initial_report_due_at: datetime.datetime | None = None
    intermediate_report_due_at: datetime.datetime | None = None
    final_report_due_at: datetime.datetime | None = None
    deadline_basis: str | None = Field(
        default=None,
        description=(
            "`default` or `tenant_policy` — which durations produced the three instants. "
            "Recorded because a default that changes in a later release must not leave an "
            "auditor unable to say where a given deadline came from."
        ),
    )


class BacklogResponse(BaseModel):
    """The unclassified backlog, and how long the longest has waited.

    Both, because the count alone is not actionable.
    """

    unclassified_count: int
    oldest_age_seconds: float


def _response(obligation: ReportingObligation, stamped: StampedDeadlines | None = None) -> ObligationResponse:
    return ObligationResponse(
        obligation_id=obligation.obligation_id,
        summary=obligation.summary,
        materiality=obligation.materiality,
        nominated_at=obligation.nominated_at,
        nominated_by=obligation.nominated_by,
        classified_at=obligation.classified_at,
        classified_by=obligation.classified_by,
        classification_note=obligation.classification_note,
        # The freshly stamped instants when the classify path just produced
        # them, and otherwise whatever the row already carries. A read must show
        # the deadlines without the caller having asked a second surface, which
        # is E4-T6's "visible without anybody asking".
        deadline_basis=obligation.deadline_basis if stamped is None else stamped.basis,
        final_report_due_at=(obligation.final_report_due_at if stamped is None else stamped.final),
        initial_report_due_at=(obligation.initial_report_due_at if stamped is None else stamped.initial),
        intermediate_report_due_at=(obligation.intermediate_report_due_at if stamped is None else stamped.intermediate),
    )


@router.post("/reporting-obligations", response_model=ObligationResponse, status_code=201)
async def nominate_obligation(
    request: Request,
    body: NominateObligationRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> ObligationResponse:
    """Record that something may need reporting, without deciding whether it does."""
    try:
        obligation = await _services(request).reporting_obligations.nominate(ctx, summary=body.summary)
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return _response(obligation)


@router.post("/reporting-obligations/{obligation_id}/classify", response_model=ObligationResponse)
async def classify_obligation(
    request: Request,
    body: ClassifyObligationRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    obligation_id: Annotated[uuid.UUID, Path()],
) -> ObligationResponse:
    """Decide one obligation's materiality, on the record.

    Refuses a second classification rather than overwriting: the first answer is
    the one somebody acted on, and an overwrite would leave the trail describing
    only the most recent opinion.
    """
    services = _services(request)
    try:
        obligation = await services.reporting_obligations.classify(
            ctx,
            obligation_id=obligation_id,
            materiality=body.materiality,
            note=body.note,
        )
        # The clock starts here rather than on a later call, and that is E4-T6's
        # own requirement: the deadlines are stamped *at classification time*,
        # so a separate "now start the clock" step would be a window in which an
        # obligation is material and nothing is due.
        stamped = None
        if obligation.materiality == MATERIALITY_MATERIAL:
            stamped = await services.reporting_deadlines.stamp(ctx, obligation_id)
    except (ConflictError, NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return _response(obligation, stamped)


class DeadlinePolicyRequest(BaseModel):
    """The durations this tenant's regulator requires, overriding the default."""

    initial_seconds: int = Field(gt=0)
    intermediate_seconds: int = Field(gt=0)
    final_seconds: int = Field(gt=0)
    source_note: str = Field(
        min_length=20,
        max_length=2000,
        description=(
            "Which regulation, article and RTS version these come from. Three durations "
            "with no stated source are three numbers nobody can audit."
        ),
    )


class DeadlinePolicyResponse(BaseModel):
    initial_seconds: int
    intermediate_seconds: int
    final_seconds: int
    source_note: str
    is_default: bool = Field(
        description=(
            "Whether these are the built-in default rather than a policy this tenant "
            "recorded. The default follows the regulation; confirming it against this "
            "deployment's own regulator is the deployment's job, and this says whether "
            "anybody has."
        )
    )


@router.put("/reporting-deadline-policy", response_model=DeadlinePolicyResponse)
async def set_deadline_policy(
    request: Request,
    body: DeadlinePolicyRequest,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> DeadlinePolicyResponse:
    """Record what this tenant's regime requires, overriding the default.

    Already-stamped deadlines do not move. They are what somebody was working
    to, and rewriting them would rewrite the audit's answer to "when was this
    due".
    """
    try:
        policy = await _services(request).reporting_deadlines.set_policy(
            ctx,
            initial=datetime.timedelta(seconds=body.initial_seconds),
            intermediate=datetime.timedelta(seconds=body.intermediate_seconds),
            final=datetime.timedelta(seconds=body.final_seconds),
            source_note=body.source_note,
        )
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return DeadlinePolicyResponse(
        final_seconds=int(policy.final.total_seconds()),
        initial_seconds=int(policy.initial.total_seconds()),
        intermediate_seconds=int(policy.intermediate.total_seconds()),
        is_default=False,
        source_note=policy.source_note,
    )


@router.get("/reporting-deadline-policy", response_model=DeadlinePolicyResponse)
async def get_deadline_policy(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> DeadlinePolicyResponse:
    """The durations in force for this tenant, and whether anybody chose them."""
    deadlines = _services(request).reporting_deadlines
    configured = await deadlines.policy_for(ctx)
    policy = configured or deadlines.default_policy()
    return DeadlinePolicyResponse(
        final_seconds=int(policy.final.total_seconds()),
        initial_seconds=int(policy.initial.total_seconds()),
        intermediate_seconds=int(policy.intermediate.total_seconds()),
        is_default=configured is None,
        source_note=policy.source_note,
    )


class CitedIncidentResponse(BaseModel):
    """One external incident record this obligation is about."""

    reference_id: uuid.UUID
    source_system: str
    source_namespace: str
    external_id: str
    authorized_uri: str | None
    observed_at: datetime.datetime | None
    bound_at: datetime.datetime


class ObligationEvidenceResponse(BaseModel):
    """One obligation, the incidents it cites, and the claims citing those."""

    obligation: ObligationResponse
    incidents: list[CitedIncidentResponse]
    #: Claim **ids** paired with the incident each cites, never claim content.
    #: An export that inlined values would be a second serving path with none of
    #: the servability rules the real one applies.
    citing_claims: list[dict[str, object]]
    provenance: str
    is_matched: bool = Field(
        description=(
            "Whether anybody has yet said which record this obligation concerns. False is a "
            "nomination in progress rather than a defect, and a reader of an empty bundle "
            "needs to be able to tell those apart."
        )
    )


@router.get(
    "/reporting-obligations/{obligation_id}:evidence",
    response_model=ObligationEvidenceResponse,
)
async def obligation_evidence(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    obligation_id: Annotated[uuid.UUID, Path()],
) -> ObligationEvidenceResponse:
    """Everything recorded about one obligation, in one read.

    **This route is why the service exists, and it was missing.**
    `ObligationEvidenceService` shipped as E4-T7's deliverable, wired into the
    container and reached by no transport -- so the export nobody could call was
    recorded as delivered. An export surface with no caller is not an export,
    and this is the seventh instance of that defect the plan has caught.

    The scope is a join and nothing is inferred: an obligation, the incidents it
    cites, and the claims whose provenance names those incidents. A claim reaches
    this bundle only by citing an incident this obligation cites.

    Ids, never claim content. Somebody assembling a regulatory submission needs
    to know *which* records bear on it; serving the values here would be a second
    serving path with none of the servability rules the real one applies.
    """
    try:
        bundle = await _services(request).obligation_evidence.bundle_for(ctx, obligation_id=obligation_id)
    except NotFoundError as exc:
        raise map_catalog_error(exc) from exc
    return ObligationEvidenceResponse(
        citing_claims=[dict(row) for row in bundle.citing_claims],
        incidents=[
            CitedIncidentResponse(
                authorized_uri=incident.authorized_uri,
                bound_at=incident.bound_at,
                external_id=incident.external_id,
                observed_at=incident.observed_at,
                reference_id=incident.reference_id,
                source_namespace=incident.source_namespace,
                source_system=incident.source_system,
            )
            for incident in bundle.incidents
        ],
        is_matched=bundle.is_matched,
        obligation=_response(bundle.obligation),
        provenance=bundle.provenance,
    )


@router.get("/reporting-obligations/{obligation_id}", response_model=ObligationResponse)
async def get_obligation(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    obligation_id: Annotated[uuid.UUID, Path()],
) -> ObligationResponse:
    """One obligation, scoped to the caller's tenant."""
    try:
        obligation = await _services(request).reporting_obligations.get(ctx, obligation_id=obligation_id)
    except NotFoundError as exc:
        raise map_catalog_error(exc) from exc
    return _response(obligation)


@router.get("/reporting-obligations:backlog", response_model=BacklogResponse)
async def obligation_backlog(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> BacklogResponse:
    """How many obligations are waiting, and how long the longest has waited.

    A read rather than a scheduled report, and both numbers rather than one: a
    scheduled job that silently stops turns a backlog into "whenever somebody
    looks", and a count with no age cannot tell a morning's work from a
    six-month lapse.
    """
    backlog = await _services(request).reporting_obligations.unclassified_backlog(ctx)
    return BacklogResponse(
        unclassified_count=backlog.count,
        oldest_age_seconds=backlog.oldest_age_seconds,
    )


#: Named so a reader of this module can see the closed set without opening the
#: service. Kept in step with `ClassifyObligationRequest` by
#: `tests/unit/test_reporting_obligations_routes.py`, because a `Literal` cannot
#: be enumerated at runtime and the two would otherwise drift silently.
CLASSIFIABLE_ON_THE_WIRE: frozenset[str] = frozenset({MATERIALITY_MATERIAL, MATERIALITY_NOT_MATERIAL})

__all__ = ["CLASSIFIABLE_ON_THE_WIRE", "router"]


# --- the principal directory --------------------------------------------------


class PrincipalResponse(BaseModel):
    """One principal, and what is known about it.

    `is_declared` is the field a roster reader needs: `actor_kind` alone cannot
    tell a declared human from a principal nobody has spoken about, and both
    read as `human` under the old default.
    """

    actor_id: uuid.UUID
    display_name: str | None
    oidc_subject: str
    actor_kind: str
    owner_principal: str | None
    declared_at: datetime.datetime | None
    declared_by: uuid.UUID | None
    created_at: datetime.datetime
    is_declared: bool


class PrincipalPageResponse(BaseModel):
    items: list[PrincipalResponse]
    next_cursor: str | None


class DeclarePrincipalRequest(BaseModel):
    """What a principal is, and who is accountable for it."""

    actor_kind: str = Field(description=f"One of {list(DECLARABLE_KINDS)}.")
    owner_principal: str = Field(
        min_length=MIN_OWNER,
        max_length=MAX_OWNER,
        description=(
            "Who to talk to about this principal. A principal whose owner is unrecorded is "
            "one nobody is accountable for."
        ),
    )


def _principal_response(principal: Principal) -> PrincipalResponse:
    return PrincipalResponse(
        actor_id=principal.actor_id,
        display_name=principal.display_name,
        oidc_subject=principal.oidc_subject,
        actor_kind=principal.actor_kind,
        owner_principal=principal.owner_principal,
        declared_at=principal.declared_at,
        declared_by=principal.declared_by,
        created_at=principal.created_at,
        is_declared=principal.is_declared,
    )


@router.get("/actors", response_model=PrincipalPageResponse)
async def list_principals(
    request: Request,
    ctx: Annotated[TenantContext, Depends(_admin_required)],
    actor_kind: str | None = Query(None, description=f"One of {list(ALL_KINDS)}."),
    cursor: str | None = Query(None),
    page_size: int = Query(50, ge=1, le=ACTOR_MAX_PAGE_SIZE),
) -> PrincipalPageResponse:
    """Every principal in this tenant, newest first.

    **Undeclared principals are returned, not filtered.** An agent nobody has
    declared is the state most deployments start in, and a roster that hid it
    would answer "we have no agents" to a deployment that has eleven. The row
    says what is not known and the caller can act on it.
    """
    services: Services = request.app.state.services
    try:
        page = await services.actor_directory.list_principals(
            ctx,
            actor_kind=actor_kind,
            cursor=parse_actor_cursor(cursor),
            page_size=page_size,
        )
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc
    return PrincipalPageResponse(
        items=[_principal_response(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/actors/{actor_id}/declare", response_model=PrincipalResponse)
async def declare_principal(
    request: Request,
    body: DeclarePrincipalRequest,
    actor_id: Annotated[uuid.UUID, Path()],
    ctx: Annotated[TenantContext, Depends(_admin_required)],
) -> PrincipalResponse:
    """Say what a principal is, and who is accountable for it.

    A declaration, never a classification. A human in an IDE and an unattended
    agent arrive over the identical transport, so nothing here reads behaviour
    to guess — somebody says, and the row records that they said so and when.

    Re-declaring overwrites: a principal that was a person's session and is now
    an unattended agent is a real change, and refusing it would leave the roster
    wrong in the direction that matters.
    """
    services: Services = request.app.state.services
    try:
        principal = await services.actor_directory.declare(
            ctx,
            actor_id=actor_id,
            actor_kind=body.actor_kind,
            owner_principal=body.owner_principal,
        )
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return _principal_response(principal)
