"""Curator actions over staged claims: the curation queue and its verdicts.

First router in the memory-curation surface -- the file every later REST
task in this area adds to -- so it also sets the conventions the rest of the
file follows:

- View models are closed (`extra="forbid"`): a caller that misspells a field
  and has it silently dropped believes an argument took effect when it did
  not.
- Every service-raised exception maps through `map_catalog_error`, never a
  bespoke per-route translation.
- List routes paginate by keyset, never offset: `cursor`/`page_size` in,
  `next_cursor` out, following the admin audit-log route's own encode/decode
  split (`pagination.py`).
- Services come off the typed container (`request.app.state.services`),
  never a bare `app.state.<name>` read -- a caller asking for a field the
  container does not declare gets a construction error at startup, not a
  silent `None` three frames into a request handler.

**Why `:link` and `:discard` are plain `@router.post` routes, not run
through `HttpMethodRouter`.** Every mutation elsewhere in this codebase maps
a genuine alternate HTTP verb (PATCH, PUT, DELETE) to a POST-tunneled alias
for deployments whose proxy strips those verbs -- `HttpMethodRouter` exists
to switch between the two surfaces. A curator action like "link this claim
to a subject" or "discard this claim" has no such alternate verb: POST
already is the one and only conventional form, in every deployment mode.
Feeding an already-suffixed path (`.../{id}:link`) through
`HttpMethodRouter.add_mutation_route` would work under the default `rest`
mode but double the suffix under `post_only`/`both`
(`.../{id}:link:link`), leaving the route unreachable at its documented
address on exactly the deployments that need the tunnel form. A handful of
existing admin routes already settle this the same way (plain `@router.post`
for actions with no alternate verb) -- there is nothing to switch between,
so nothing is registered through the switch.

**The queue's `containment_refused` reason is gone.** Re-verified at
implementation time (`grep -rn "REASON_REFUSED\\|containment_refused"
contextplane/`): nothing in this codebase writes a queryable record of a
containment-refused candidate -- the queue's own SQL never emitted that
reason, and the direct-assertion route this surface will later grow refuses
synchronously without ever staging or queuing the attempt. Building a SQL
arm for a case nothing populates would be a dead branch; the vocabulary
entry (and its advertised actions) were dropped instead of wired up.

**Promotion review adds this file's first genuine verb mutation.** Accepting
or rejecting a proposal has a real conventional verb (PATCH) *and* a real
POST-tunnel alias for proxies that strip PATCH -- unlike `:link`/`:discard`
above, there is something to switch between across deployment modes, so this
one goes through `HttpMethodRouter.add_mutation_route` on a second,
mode-aware `mutation_router` (mirroring `api/routers/memory.py`'s own
`router` / `mutation_router` split). Its alias action is `"update"`: every
other bare-resource PATCH in this codebase (`capabilities.py`,
`admin_vocab.py`, `admin_sync.py`, `subscriptions.py`, `workspaces.py`, …)
names its POST-tunnel alias `:update`, and a proposal review is exactly that
shape -- one resource, one PATCH, no sibling verb it needs to be told apart
from. Reserving a distinct alias word for this one route would read as a
different kind of action to a client scanning the alias vocabulary across
routers, when structurally it is the same one.

**`:reverse` stays a plain POST**, for the same reason `:link` and `:discard`
do: undoing a specific promotion has no alternate HTTP verb to switch
between, so there is nothing for `HttpMethodRouter` to do here either.

**`:confirm` and `:adjudicate` are plain POSTs for the same reason.** A
human putting their name to a claim, or a reviewer recording a verdict, is
not a resource update with an alternate PATCH/PUT form -- there is nothing
to switch between across deployment modes, so neither goes through
`HttpMethodRouter`. Both wrap `ConfirmationService`, which already exists
and is already integration-tested against direct calls; this file gives it
a route, nothing more.

**Claim history is this file's own tenant-enforcement wrap, not the
service's.** `ClaimHistoryService.chain_for`/`believed_at` take no tenant
context by design -- they read only claim rows by id or by subject, nothing
else. Wiring either straight to a route would make claim
and subject ids a cross-tenant existence oracle, so both routes resolve the
subject through the visibility chokepoint (`service/governance/visibility.py`)
before calling either method; an invisible or absent subject answers
identically to a nonexistent one. `/history` goes one step further: a
claim's own visibility can be narrower than the subject it describes (an
observer's private note about a public capability -- see
`ClaimService._derive_visibility`), so `claim_serving.py` checks both the
claim and its subject before serving one, and this route mirrors that same
dual check, including per entry as the chain walk crosses a supersession
that narrowed visibility partway through. The rows behind that check come
from `ClaimHistoryService.visibility_rows_for` -- this router holds no SQL
of its own, the same as every other route in this file; only the chokepoint
call and the claim-visible rule live here. `believed_at` is bounded and
unpaginated by design (a subject's belief set at one instant, not a growing
list), so it takes no cursor.

**Raising a capability request is the other place this file wraps a
ctx-less-shaped lookup in the chokepoint, for a subtler reason than claim
history's.** `CapabilityRequestService.raise_request` *does* take a tenant
context, but its own subject lookup is a bare existence check (`entity_id =
:eid AND is_active`), not a visibility filter -- the service's job there is
routing (which tenant owns this?), not authorization. Called directly, that
lookup turns a request's accept/refuse outcome into a cross-tenant existence
oracle: a caller can't read a private entity, but they could learn that one
exists just by whether their request landed. So the route resolves the
subject through the chokepoint first and raises the *identical* error the
service raises for a subject that plain does not exist -- an invisible
subject cannot be told apart from a missing one, from outside this route,
including by status code and message. `for_owner`/`raised_by` gain the same
`(cursor, page_size)` keyset shape as every other list route here, on
`(created_at, request_id)`. The PATCH `transition` route (alias `:update`,
same reasoning as the proposal review PATCH above) and the plain-POST
`:link-promotion` action leave every authority and lifecycle check to
`CapabilityRequestService` itself -- this file's role for both is tenant
context and nothing else.

**Direct claim assertion is a plain resource `POST`, not a tunneled
action.** `POST /v1/memory/claims` creates a new staged (or unlinked) claim
from an argument list a caller supplies directly, the same shape
`POST /capabilities` and `POST /capability-requests` already are in this
codebase -- a collection create, with no alternate HTTP verb to switch
between, so it is a plain `@router.post`, the same reasoning `:link` and
`:discard` document above for why they skip `HttpMethodRouter` too. Unlike
those two, it **does** honour `X-Idempotency-Key`/`Idempotency-Key`
(`api/middleware/idempotency.py`), the same three-line lookup/persist shape
`create_capability` and `create_artifact` already use elsewhere: a client
retrying a timed-out create is exactly the case that would otherwise stage a
second, distinct claim with no way to tell it apart from the first.

`ClaimService.stage_claim` is the one write path, and it runs neither of the
two checks every other producer of a claim already applies at its own layer
(extraction refuses directive content and blocking PII before it ever calls
`stage_claim`; a connector's governance gate does the same). A caller
asserting a claim directly has no such layer in front of it, so this route
(and its MCP equivalent) calls `service/memory/claim_assertion.py`'s
`stage_claim_defended` instead of `stage_claim` itself -- the one place both
checks are implemented, so neither surface can grow its own copy or quietly
skip one. Two refusals get a structured body ahead of `map_catalog_error`,
following the same "check the specific exception before falling through to
the generic translator" shape `workspaces.py`'s `_ws_exc_to_http` uses for
`WorkspacePiiBlocked`: `CandidateRefused` is a `RegistryError`, not a
`CatalogError` -- deliberately, so nothing routes it through the catalog
error tree by accident -- and would otherwise fall through
`map_catalog_error`'s generic branch to an uninformative 400, so it is
special-cased here into a 422 carrying `code="containment_refused"` and the
trigger that fired. `ClaimPiiBlocked` *is* a `ValidationError`
(`map_catalog_error` would already give it a 422), but is special-cased the
same way so `matched_patterns` survives as a structured field rather than
collapsing into `str(exc)`. Both response bodies here include a `message`
key alongside `code` -- unlike `WorkspacePiiBlocked`'s own raw detail dict,
which omits one -- because the global envelope handler
(`wiring/http_app.py`) collapses any `HTTPException.detail` dict lacking a
`message` key to a stringified fallback with the wrong `code`, silently
discarding every other key with it; `middleware/tenant.py`'s
`_select_tenant_grant` documents the same requirement inline for its own
structured 400 body. Verified directly against `coerce_to_envelope`: a dict
carrying both `code` and `message` survives with its extra keys (`trigger`,
`matched_patterns`) intact; one missing `message` does not.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from contextplane.api.container import Services
from contextplane.api.errors import build_error, map_catalog_error
from contextplane.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from contextplane.api.middleware.idempotency import IdempotencyContext, get_idempotency_context
from contextplane.api.middleware.tenant import get_tenant_context
from contextplane.api.schemas.memory_curation import (
    AdjudicateClaimRequest,
    AdjudicateClaimResponse,
    AssertClaimRequest,
    AssertClaimResponse,
    BelievedClaimResponse,
    BelievedClaimsResponse,
    CapabilityRequestListResponse,
    CapabilityRequestResponse,
    ClaimHistoryResponse,
    ConfirmationResponse,
    DiscardClaimRequest,
    DiscardResponse,
    LinkClaimRequest,
    LinkedClaimResponse,
    LinkRequestToPromotionRequest,
    LinkRequestToPromotionResponse,
    ProposalDecisionResponse,
    ProposalListResponse,
    ProposalResponse,
    QueueCountsResponse,
    QueueItemResponse,
    QueueListResponse,
    RaiseCapabilityRequestRequest,
    RequestHistoryResponse,
    RequestTransitionResponse,
    ReversePromotionRequest,
    ReversePromotionResponse,
    ReviewProposalRequest,
    TransitionRequestRequest,
)
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.extraction.containment import CandidateRefused
from contextplane.pagination import InvalidCursorError, decode_cursor, encode_cursor
from contextplane.service.governance.temporal import normalize_utc
from contextplane.service.governance.visibility import VisibilityService
from contextplane.service.memory.capability_requests import CapabilityRequest, CapabilityRequestService, Transition
from contextplane.service.memory.claim_assertion import ClaimPiiBlocked, stage_claim_defended
from contextplane.service.memory.claim_authority import Evidence, StagedClaim
from contextplane.service.memory.claim_history import BelievedClaim, ClaimHistoryService, ClaimVisibility
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.confirmation import Confirmation, ConfirmationService
from contextplane.service.memory.contest import ContradictionGroup, groups_for
from contextplane.service.memory.curation_queue import CurationCase, CurationQueueService, QueueItem
from contextplane.service.memory.promotion import PromotionService, Proposal
from contextplane.types import TenantContext
from contextplane.usage.results import stash_result_count

router = APIRouter(tags=["memory: curation"], prefix="/v1/memory")

_mode, _sep = get_mode_settings()
mutation_router = APIRouter(tags=["memory: curation"], prefix="/v1/memory")
_mut_mr = HttpMethodRouter(mutation_router, mode=_mode, separator=_sep)

_DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_SIZE = 500


def _curation_queue(request: Request) -> CurationQueueService:
    services: Services = request.app.state.services
    return services.curation_queue


def _claims(request: Request) -> ClaimService:
    services: Services = request.app.state.services
    return services.claims


def _promotion(request: Request) -> PromotionService:
    services: Services = request.app.state.services
    return services.promotion


def _confirmations(request: Request) -> ConfirmationService:
    services: Services = request.app.state.services
    return services.confirmations


def _claim_history(request: Request) -> ClaimHistoryService:
    services: Services = request.app.state.services
    return services.claim_history


def _visibility(request: Request) -> VisibilityService:
    services: Services = request.app.state.services
    return services.visibility


def _capability_requests(request: Request) -> CapabilityRequestService:
    services: Services = request.app.state.services
    return services.capability_requests


# --- curation queue ---------------------------------------------------------


def _to_queue_item_response(item: QueueItem) -> QueueItemResponse:
    return QueueItemResponse(
        claim_id=item.claim_id,
        reason=item.reason,
        subject_reference=item.subject_reference,
        subject_entity_id=item.subject_entity_id,
        predicate=item.predicate,
        value=item.value,
        confidence=item.confidence,
        created_at=item.created_at,
        human_backed=item.human_backed,
        proposal_id=item.proposal_id,
        available_actions=list(item.available_actions),
    )


@router.get(
    "/curation-queue",
    response_model=QueueListResponse | QueueCountsResponse,
)
async def get_curation_queue(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    counts: bool = Query(False),
    cursor: str | None = Query(None),
    page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = _DEFAULT_PAGE_SIZE,
) -> QueueListResponse | QueueCountsResponse:
    """Everything needing curator attention in the caller's tenant.

    `?counts=true` returns the per-reason tally instead of the item list --
    the number a curator needs to see before opening the queue is not the
    same as the page they open, and a tally needs no pagination of its own.

    Not registered through `HttpMethodRouter`: this is a plain read, always
    exposed, with nothing to switch between across deployment modes.
    """
    queue = _curation_queue(request)
    if counts:
        tally = await queue.counts_for(ctx.tenant_id)
        return QueueCountsResponse(counts=tally)

    cursor_pair: tuple[datetime.datetime, uuid.UUID] | None = None
    if cursor is not None:
        try:
            payload = decode_cursor(cursor, strict=True)
            cursor_pair = (
                datetime.datetime.fromisoformat(payload["created_at"]),
                uuid.UUID(payload["claim_id"]),
            )
        except (InvalidCursorError, KeyError, ValueError) as exc:
            raise build_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="invalid_cursor",
                message="invalid cursor",
            ) from exc

    items = await queue.items_for(ctx.tenant_id, cursor=cursor_pair, page_size=page_size)

    next_cursor: str | None = None
    if len(items) > page_size:
        items = items[:page_size]
        last = items[-1]
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "claim_id": str(last.claim_id)})

    stash_result_count(request, len(items))
    return QueueListResponse(
        items=[_to_queue_item_response(i) for i in items],
        next_cursor=next_cursor,
    )


# --- contradiction groups and their cases -------------------------------------
#
# These view models are defined here rather than in `api/schemas/memory_curation.py`
# alongside this router's older ones. That file's models deliberately carry no
# docstrings (a Pydantic class docstring becomes an OpenAPI component
# `description`, and they were extracted move-only against a frozen snapshot), and
# this router's own D101 ratchet requires them -- so the two groups cannot follow
# one convention in one file. Defining them at their point of use is also what
# most routers in this codebase already do. Relocating them is a mechanical move
# whenever the schemas module's no-docstring exemption is revisited.


class ContradictionGroupResponse(BaseModel):
    """One disagreement axis, as a reviewer needs to see it.

    `member_count` is the number of claims in disagreement, not the number of
    pairs detected: a three-way disagreement is three pairs and three members,
    and reporting the pair count would overstate how much is contested.
    """

    model_config = ConfigDict(extra="forbid")

    subject_entity_id: uuid.UUID
    subject_reference: str
    predicate: str
    claim_ids: list[uuid.UUID]
    contest_ids: list[uuid.UUID]
    first_detected_at: datetime.datetime
    member_count: int


class ContradictionGroupListResponse(BaseModel):
    """Every open disagreement in the caller's tenant."""

    model_config = ConfigDict(extra="forbid")

    groups: list[ContradictionGroupResponse]


class CurationCaseResponse(BaseModel):
    """A contradiction routed to an owner, and what was decided about it.

    `approval_authority` and `evidence_threshold` are recorded at disposition
    time rather than derived on read: a decision whose approver is decided
    afterwards is a decision nobody is accountable for.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: uuid.UUID
    tenant_id: uuid.UUID
    subject_reference: str
    predicate: str
    status: str
    created_at: datetime.datetime
    raised_by_derivation_id: uuid.UUID | None
    owner_id: str | None
    routed_at: datetime.datetime | None
    disposition: str | None
    approval_authority: str | None
    evidence_threshold: str | None
    resolved_at: datetime.datetime | None
    target_kind: str | None


class CurationCaseListResponse(BaseModel):
    """A page of contradiction cases, oldest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[CurationCaseResponse]
    next_cursor: str | None


class OpenCurationCaseRequest(BaseModel):
    """The axis a new case is about."""

    model_config = ConfigDict(extra="forbid")

    subject_reference: str = Field(min_length=1)
    predicate: str = Field(min_length=1)


class RouteCurationCaseRequest(BaseModel):
    """Who becomes accountable for deciding the case.

    Text rather than an actor id: an accountable owner can be a rota or a team
    address that has no actor row of its own.
    """

    model_config = ConfigDict(extra="forbid")

    owner_id: str = Field(min_length=1)


class RecordDispositionRequest(BaseModel):
    """What the accountable owner decided.

    The three `propose_*` values ask another surface to write something and do
    not perform that write; each target keeps its own approval contract.
    """

    model_config = ConfigDict(extra="forbid")

    disposition: Literal[
        "confirm",
        "reject",
        "supersede",
        "propose_canonical",
        "propose_runbook",
        "propose_arc",
    ]


def _to_group_response(group: ContradictionGroup) -> ContradictionGroupResponse:
    return ContradictionGroupResponse(
        subject_entity_id=group.subject_entity_id,
        subject_reference=group.subject_reference,
        predicate=group.predicate,
        claim_ids=list(group.claim_ids),
        contest_ids=list(group.contest_ids),
        first_detected_at=group.first_detected_at,
        member_count=group.member_count,
    )


def _to_case_response(case: CurationCase) -> CurationCaseResponse:
    return CurationCaseResponse(
        case_id=case.case_id,
        tenant_id=case.tenant_id,
        subject_reference=case.subject_reference,
        predicate=case.predicate,
        status=case.status,
        created_at=case.created_at,
        raised_by_derivation_id=case.raised_by_derivation_id,
        owner_id=case.owner_id,
        routed_at=case.routed_at,
        disposition=case.disposition,
        approval_authority=case.approval_authority,
        evidence_threshold=case.evidence_threshold,
        resolved_at=case.resolved_at,
        target_kind=case.target_kind,
    )


@router.get("/contradiction-groups", response_model=ContradictionGroupListResponse)
async def list_contradiction_groups(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    predicate: str | None = Query(None),
) -> ContradictionGroupListResponse:
    """Open contradictions in the caller's tenant, one entry per axis.

    Unpaginated, unlike every list route around it, and deliberately: the result
    is one row per *unresolved* disagreement axis in one tenant, which the
    curation queue already surfaces under its own `contested` reason and which
    curation drains. Detection also caps how many pairs a single axis can
    produce, so no one subject can inflate this into an unbounded read.

    A plain read, so nothing goes through `HttpMethodRouter`.
    """
    services: Services = request.app.state.services
    async with services.session_factory() as session:
        groups = await groups_for(session, tenant_id=ctx.tenant_id, predicate=predicate)

    stash_result_count(request, len(groups))
    return ContradictionGroupListResponse(groups=[_to_group_response(g) for g in groups])


@router.post("/curation-cases", response_model=CurationCaseResponse, status_code=status.HTTP_201_CREATED)
async def open_curation_case(
    body: OpenCurationCaseRequest,
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> CurationCaseResponse:
    """Put one contradiction axis in front of a person.

    Idempotent while a case on the same axis is unresolved, and it returns the
    already-open case rather than refusing: re-detecting the same contradiction
    is the normal path, and a second row would split one disagreement into two
    entries that two owners could decide differently. `201` is therefore the
    status for "there is a case on this axis", not a promise that this call is
    what created it -- the same shape the idempotent creates elsewhere in this
    surface use.

    A collection create with no alternate verb, so it is a plain `POST`.
    """
    services: Services = request.app.state.services
    try:
        case = await _curation_queue(request).open_case(
            ctx,
            subject_reference=body.subject_reference,
            predicate=body.predicate,
            now=services.clock.now(),
        )
    except (ValidationError, ConflictError, NotFoundError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_case_response(case)


@router.get("/curation-cases", response_model=CurationCaseListResponse)
async def list_curation_cases(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    case_status: Annotated[str | None, Query(alias="status")] = None,
    cursor: str | None = Query(None),
    page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = _DEFAULT_PAGE_SIZE,
) -> CurationCaseListResponse:
    """Contradiction cases in the caller's tenant, oldest first.

    Same keyset shape as the queue route above, on `(created_at, case_id)`: an
    aged contradiction is the one worth surfacing, and a queue whose tail is
    never reached is a queue that queued for nothing.

    The query parameter is `status`; the handler argument is `case_status`
    because `status` is this module's imported `fastapi.status`, and shadowing it
    inside one handler would break the `build_error` calls in the same function.
    """
    queue = _curation_queue(request)

    cursor_pair: tuple[datetime.datetime, uuid.UUID] | None = None
    if cursor is not None:
        try:
            payload = decode_cursor(cursor, strict=True)
            cursor_pair = (
                datetime.datetime.fromisoformat(payload["created_at"]),
                uuid.UUID(payload["case_id"]),
            )
        except (InvalidCursorError, KeyError, ValueError) as exc:
            raise build_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="invalid_cursor",
                message="invalid cursor",
            ) from exc

    try:
        cases = await queue.cases_for(ctx.tenant_id, status=case_status, cursor=cursor_pair, page_size=page_size)
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc

    next_cursor: str | None = None
    if len(cases) > page_size:
        cases = cases[:page_size]
        last = cases[-1]
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "case_id": str(last.case_id)})

    stash_result_count(request, len(cases))
    return CurationCaseListResponse(
        items=[_to_case_response(c) for c in cases],
        next_cursor=next_cursor,
    )


@router.get("/curation-cases/{case_id}", response_model=CurationCaseResponse)
async def get_curation_case(
    case_id: uuid.UUID,
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> CurationCaseResponse:
    """One case, if it belongs to the caller's tenant.

    A case in another tenant answers exactly as one that does not exist, so a
    case id cannot confirm that somebody else is reviewing a contradiction.
    """
    try:
        case = await _curation_queue(request).case(ctx, case_id)
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_case_response(case)


@router.post("/curation-cases/{case_id}:route", response_model=CurationCaseResponse)
async def route_curation_case(
    case_id: uuid.UUID,
    body: RouteCurationCaseRequest,
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> CurationCaseResponse:
    """Name the person accountable for deciding this case.

    Re-routing an already-routed case is allowed and audited -- escalation is a
    real move, and a queue that could not hand a case on would strand it on
    whoever it reached first. A resolved case is refused rather than reopened.

    A curator action with no alternate HTTP verb, so it is a plain `POST` for
    the same reason `:link` and `:discard` are.
    """
    services: Services = request.app.state.services
    try:
        case = await _curation_queue(request).route_case(
            ctx,
            case_id=case_id,
            owner_id=body.owner_id,
            now=services.clock.now(),
        )
    except (NotFoundError, ConflictError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_case_response(case)


@router.post("/curation-cases/{case_id}:disposition", response_model=CurationCaseResponse)
async def record_case_disposition(
    case_id: uuid.UUID,
    body: RecordDispositionRequest,
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> CurationCaseResponse:
    """Record what the accountable owner decided, and on whose authority.

    Authorization is the service's, not this route's: only the owner the case is
    routed to may decide it, because being able to read a case is not authority
    to settle it. That check is what makes "routed to an owner" mean anything, so
    it lives with the write it guards rather than being re-derived here.

    Nothing here performs the write a `propose_*` disposition asks for. The
    surfaces that own canonical facts, runbooks, and agent-readiness artifacts
    each have their own approval contract; collapsing "decided" and "written"
    into one moment is the confusion an accountable owner exists to prevent.
    """
    services: Services = request.app.state.services
    try:
        case = await _curation_queue(request).record_disposition(
            ctx,
            case_id=case_id,
            disposition=body.disposition,
            now=services.clock.now(),
        )
    except PermissionError as exc:
        raise build_error(
            status.HTTP_403_FORBIDDEN,
            code="not_the_accountable_owner",
            message=str(exc),
        ) from exc
    except (NotFoundError, ConflictError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_case_response(case)


# --- link / discard ----------------------------------------------------------


def _to_linked_claim_response(claim: StagedClaim) -> LinkedClaimResponse:
    return LinkedClaimResponse(
        claim_id=claim.claim_id,
        subject_entity_id=claim.subject_entity_id,
        predicate=claim.predicate,
        value=claim.value,
        status=claim.status,
        visibility=claim.visibility,
        owning_tenant_id=claim.owning_tenant_id,
        source_authority=claim.source_authority,
        is_contested=claim.is_contested,
    )


@router.post("/claims/{claim_id}:link", response_model=LinkedClaimResponse)
async def link_claim_subject(
    request: Request,
    body: LinkClaimRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    claim_id: uuid.UUID,
) -> LinkedClaimResponse:
    """Give a subjectless claim a home.

    Curator-only, and the service is the one gate: role, tenancy, and the
    claim's current status are all asserted by `ClaimService.link_subject`
    itself, not re-checked here.
    """
    try:
        claim = await _claims(request).link_subject(
            ctx,
            claim_id=claim_id,
            subject_reference=body.subject_reference,
        )
    except (NotFoundError, ConflictError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_linked_claim_response(claim)


@router.post("/claims/{claim_id}:discard", response_model=DiscardResponse)
async def discard_claim(
    request: Request,
    body: DiscardClaimRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    claim_id: uuid.UUID,
) -> DiscardResponse:
    """Refuse a claim outright: it never serves again.

    Works on a staged claim or one still unlinked -- a reference that will
    never resolve has this as its only way out of the queue. Curator-only,
    the same bar `link_subject` sets.
    """
    try:
        await _claims(request).discard(ctx, claim_id=claim_id, reason=body.reason)
    except (NotFoundError, ConflictError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return DiscardResponse(status="discarded")


# --- promotion proposals ------------------------------------------------------


def _to_proposal_response(proposal: Proposal) -> ProposalResponse:
    return ProposalResponse(
        proposal_id=proposal.proposal_id,
        claim_id=proposal.claim_id,
        owner_tenant_id=proposal.owner_tenant_id,
        author_tenant_id=proposal.author_tenant_id,
        subject_entity_id=proposal.subject_entity_id,
        predicate=proposal.predicate,
        target_kind=proposal.target_kind,
        target_key=proposal.target_key,
        current_value=proposal.current_value,
        proposed_value=proposal.proposed_value,
        valid_from=proposal.valid_from,
        valid_to=proposal.valid_to,
        high_impact=proposal.high_impact,
        high_impact_reasons=list(proposal.high_impact_reasons),
        state=proposal.state,
        created_at=proposal.created_at,
    )


@router.get("/promotion-proposals", response_model=ProposalListResponse)
async def list_promotion_proposals(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    state: Literal["open", "accepted", "amended", "rejected"] = "open",
    cursor: str | None = Query(None),
    page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = _DEFAULT_PAGE_SIZE,
) -> ProposalListResponse:
    """Proposals owned by the caller's tenant, oldest first.

    `state` defaults to `"open"` -- the review queue, not the full history --
    matching the curation queue's own "what needs my attention" framing.
    Keyset-paginated the same way: `cursor`/`page_size` in, `next_cursor` out.
    """
    cursor_pair: tuple[datetime.datetime, uuid.UUID] | None = None
    if cursor is not None:
        try:
            payload = decode_cursor(cursor, strict=True)
            cursor_pair = (
                datetime.datetime.fromisoformat(payload["created_at"]),
                uuid.UUID(payload["proposal_id"]),
            )
        except (InvalidCursorError, KeyError, ValueError) as exc:
            raise build_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="invalid_cursor",
                message="invalid cursor",
            ) from exc

    proposals = await _promotion(request).proposals_for(
        ctx.tenant_id, state=state, cursor=cursor_pair, page_size=page_size
    )

    next_cursor: str | None = None
    if len(proposals) > page_size:
        proposals = proposals[:page_size]
        last = proposals[-1]
        if last.created_at is None:
            # The read path (`proposals_for`) always fills `created_at` from
            # the row it loaded -- only `propose()`'s in-transaction
            # construction leaves it unset, and that path never reaches here.
            raise RuntimeError("proposals_for returned a proposal with no created_at")
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "proposal_id": str(last.proposal_id)})

    stash_result_count(request, len(proposals))
    return ProposalListResponse(
        items=[_to_proposal_response(p) for p in proposals],
        next_cursor=next_cursor,
    )


@router.get("/promotion-proposals/{proposal_id}", response_model=ProposalResponse)
async def get_promotion_proposal(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: uuid.UUID,
) -> ProposalResponse:
    """One proposal, if it exists and the caller's tenant owns its subject.

    `PromotionService.get_proposal` runs no tenancy filter of its own (its
    docstring is explicit that the caller must apply one) -- a proposal
    addressed to a different tenant is reported identically to one that does
    not exist, so this route is never a cross-tenant existence oracle.
    """
    proposal = await _promotion(request).get_proposal(proposal_id)
    if proposal is None or proposal.owner_tenant_id != ctx.tenant_id:
        raise map_catalog_error(NotFoundError("no such proposal"))
    return _to_proposal_response(proposal)


async def review_promotion_proposal(
    request: Request,
    body: ReviewProposalRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    proposal_id: uuid.UUID,
) -> ProposalDecisionResponse:
    """Accept (optionally amending the value) or reject an open proposal.

    Authority is entirely the service's own gate: `PromotionService.accept`/
    `reject` both call `_assert_may_review` (owner tenant, `producer` or
    `admin` role) after loading the proposal under `FOR UPDATE`. This route
    resolves tenant context and nothing else -- a second role check here
    would just be a second place it could drift from the service's.
    """
    promotion = _promotion(request)
    roles = frozenset(ctx.roles)

    if body.state == "rejected" and "amended_value" in body.model_fields_set:
        raise map_catalog_error(ValidationError("amended_value is only valid when accepting a proposal"))
    if body.state == "accepted" and body.reason is not None:
        raise map_catalog_error(ValidationError("reason is only valid when rejecting a proposal"))

    promotion_id: uuid.UUID | None = None
    try:
        if body.state == "accepted":
            accept_kwargs: dict[str, Any] = {}
            if "amended_value" in body.model_fields_set:
                accept_kwargs["amended_value"] = body.amended_value
            promotion_id = await promotion.accept(
                proposal_id,
                actor_tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                roles=roles,
                **accept_kwargs,
            )
        else:
            if body.reason is None:
                raise ValidationError("rejecting a proposal requires a reason")
            await promotion.reject(
                proposal_id,
                actor_tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                roles=roles,
                reason=body.reason,
            )
    except (NotFoundError, ConflictError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc

    updated = await promotion.get_proposal(proposal_id)
    if updated is None:
        # Can't happen on the path that just decided it, but `get_proposal`'s
        # own type says `Proposal | None` and this route never assumes a
        # narrower contract than the service actually offers.
        raise map_catalog_error(NotFoundError("no such proposal"))
    return ProposalDecisionResponse(proposal=_to_proposal_response(updated), promotion_id=promotion_id)


_mut_mr.add_mutation_route(
    path="/promotion-proposals/{proposal_id}",
    action="update",
    handler=review_promotion_proposal,
    verb="PATCH",
    response_model=ProposalDecisionResponse,
)


# --- promotion reversal --------------------------------------------------------


@router.post("/promotions/{promotion_id}:reverse", response_model=ReversePromotionResponse)
async def reverse_promotion(
    request: Request,
    body: ReversePromotionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    promotion_id: uuid.UUID,
) -> ReversePromotionResponse:
    """Undo a promotion, restoring whatever the canonical graph said before it.

    Plain POST, not run through `HttpMethodRouter` -- the same reasoning
    `:link`/`:discard` document above: there is no alternate verb for
    "reverse this specific promotion", so there is nothing to switch between
    across deployment modes. `PromotionService.reverse` refuses (409) when a
    later promotion has already built on the row this one created; the
    caller reverses that one first.
    """
    try:
        await _promotion(request).reverse(
            promotion_id,
            actor_tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            roles=frozenset(ctx.roles),
            reason=body.reason,
        )
    except (NotFoundError, ConflictError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return ReversePromotionResponse(status="reversed")


# --- confirmation --------------------------------------------------------------


def _to_confirmation_response(confirmation: Confirmation) -> ConfirmationResponse:
    return ConfirmationResponse(
        claim_id=confirmation.claim_id,
        confirms_claim_id=confirmation.confirms_claim_id,
        source_authority=confirmation.source_authority,
        confidence=confirmation.confidence,
        bucket=confirmation.bucket,
        hold_until=confirmation.hold_until,
    )


@router.post("/claims/{claim_id}:confirm", response_model=ConfirmationResponse)
async def confirm_claim(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    claim_id: uuid.UUID,
) -> ConfirmationResponse:
    """A human puts their name to a claim, producing a new one that supersedes it.

    No request body: everything `ConfirmationService.confirm` needs beyond
    the claim id comes from the caller's own tenant context. The human-vs-
    service distinction is not a role a caller can assert here -- the
    service derives it from the authenticated actor's own `actor_kind`, so
    a worker calling this route gets the same `PermissionError` (403) a
    direct call would raise; a route-level check would just be a second
    place that gate could drift from the service's.
    """
    try:
        confirmation = await _confirmations(request).confirm(ctx, claim_id=claim_id)
    except (NotFoundError, ConflictError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_confirmation_response(confirmation)


# --- adjudication ----------------------------------------------------------------


@router.post("/claims/{claim_id}:adjudicate", response_model=AdjudicateClaimResponse)
async def adjudicate_claim(
    request: Request,
    body: AdjudicateClaimRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    claim_id: uuid.UUID,
) -> AdjudicateClaimResponse:
    """Record whether a claim turned out to be correct.

    The only input a calibration fit is ever built from, which is why
    `verdict` and `observed_confidence` are constrained at this view model
    rather than left to `ConfirmationService.adjudicate`'s own
    `ValidationError` checks: `verdict` is a closed `Literal`,
    `observed_confidence` is range-bound `[0, 1]` -- a caller who sends an
    unknown verdict or an out-of-range confidence gets a 422 from request
    validation, before the service (and its calibration observation table)
    is ever touched.
    """
    try:
        await _confirmations(request).adjudicate(
            ctx,
            claim_id=claim_id,
            verdict=body.verdict,
            observed_confidence=body.observed_confidence,
            note=body.note,
        )
    except NotFoundError as exc:
        raise map_catalog_error(exc) from exc
    return AdjudicateClaimResponse(status="recorded")


# --- claim history -------------------------------------------------------------


def _to_believed_claim_response(claim: BelievedClaim) -> BelievedClaimResponse:
    return BelievedClaimResponse(
        claim_id=claim.claim_id,
        predicate=claim.predicate,
        value=claim.value,
        source_authority=claim.source_authority,
        confidence=claim.confidence,
        bucket=claim.bucket,
        status=claim.status,
        superseded_by=claim.superseded_by,
        superseded_reason=claim.superseded_reason,
        created_at=claim.created_at,
        t_invalidated_at=claim.t_invalidated_at,
        is_contested=claim.is_contested,
        was_current=claim.was_current,
    )


def _claim_visible(ctx: TenantContext, claim: ClaimVisibility) -> bool:
    """The same predicate `ClaimServingService._claim_visible` applies at read.

    Tenant-shared is not resolved past the owning tenant here either -- the
    claim tables carry no per-claim share list, so a claim meant for wider
    reading than its own tenant is expressed by marking it public, the same
    limitation that rule documents. A second copy of this rule is how one
    enforcement site starts disagreeing with another about who can read a
    claim; it is repeated here, not re-derived, because this route cannot
    import a private method off another service's class.
    """
    if claim.owning_tenant_id == ctx.tenant_id:
        return True
    return claim.visibility == "public"


@router.get("/claims/{claim_id}/history", response_model=ClaimHistoryResponse)
async def get_claim_history(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    claim_id: uuid.UUID,
) -> ClaimHistoryResponse:
    """The claim's full supersession/confirmation chain, oldest first.

    `ClaimHistoryService.chain_for` takes no tenant context by design, so
    this route is the one place tenant enforcement happens for it. The
    requested claim must exist, its own visibility must pass, and its
    subject must resolve as visible through the chokepoint -- any of the
    three failing answers identically (404), so a claim id is never a
    cross-tenant existence oracle. Every entry the chain walk returns is
    then filtered by the same claim-level check: a chain can cross a
    supersession that narrowed visibility partway through (two independently
    staged claims about the same fact do not have to request the same
    visibility), and serving that entry to a caller who could not have read
    it directly would leak through the one row this route already cleared.

    The rows this decision runs against come from
    `ClaimHistoryService.visibility_rows_for` -- this router holds no SQL of
    its own, only the chokepoint call and the claim-visible rule above.
    """
    history = _claim_history(request)
    anchor_rows = await history.visibility_rows_for([claim_id])
    anchor = anchor_rows.get(claim_id)
    if anchor is None or not _claim_visible(ctx, anchor):
        raise map_catalog_error(NotFoundError("no such claim"))
    if anchor.subject_entity_id is None or not await _visibility(request).filter_entities(
        ctx, [anchor.subject_entity_id]
    ):
        raise map_catalog_error(NotFoundError("no such claim"))

    chain = await history.chain_for(claim_id)
    chain_visibility = await history.visibility_rows_for([c.claim_id for c in chain])
    visible_chain = [
        c for c in chain if (row := chain_visibility.get(c.claim_id)) is not None and _claim_visible(ctx, row)
    ]

    stash_result_count(request, len(visible_chain))
    return ClaimHistoryResponse(items=[_to_believed_claim_response(c) for c in visible_chain])


def _parse_as_of(as_of: str) -> datetime.datetime:
    """Parse a required ISO-8601 as_of string into a UTC-aware datetime.

    Matches `retrieval.py`'s own `_parse_as_of`: a naive datetime is rejected
    by `normalize_utc` and mapped to 422, not 500 -- a time-travel query that
    silently guessed a timezone would answer "as of when" differently
    depending on the server's own clock, the one thing this parameter cannot
    afford to leave ambiguous.
    """
    try:
        dt = datetime.datetime.fromisoformat(as_of)
        return normalize_utc(dt)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"as_of must be a timezone-aware ISO-8601 datetime: {exc}",
        ) from exc


@router.get("/claims/believed", response_model=BelievedClaimsResponse)
async def get_believed_claims(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    subject_entity_id: uuid.UUID = Query(...),
    predicate: str | None = Query(None),
    as_of: str = Query(..., description="ISO-8601 UTC datetime for time-travel"),
) -> BelievedClaimsResponse:
    """What the store believed about a subject at a past instant.

    `ClaimHistoryService.believed_at` takes no tenant context by design, so
    this route resolves the subject through the chokepoint first -- a
    subject the caller cannot see answers identically to one that does not
    exist, so a subject id is never a cross-tenant existence oracle here
    either. Bounded by the subject and unpaginated on purpose: the answer is
    one subject's belief set at one instant, not a list that grows without
    it, so there is no cursor to give it.
    """
    as_of_dt = _parse_as_of(as_of)
    if not await _visibility(request).filter_entities(ctx, [subject_entity_id]):
        raise map_catalog_error(NotFoundError("no such subject"))

    claims = await _claim_history(request).believed_at(
        subject_entity_id=subject_entity_id, predicate=predicate, as_of=as_of_dt
    )

    stash_result_count(request, len(claims))
    return BelievedClaimsResponse(items=[_to_believed_claim_response(c) for c in claims])


# --- capability requests -------------------------------------------------------


def _to_capability_request_response(item: CapabilityRequest) -> CapabilityRequestResponse:
    return CapabilityRequestResponse(
        request_id=item.request_id,
        owner_tenant_id=item.owner_tenant_id,
        requester_tenant_id=item.requester_tenant_id,
        subject_entity_id=item.subject_entity_id,
        request_category=item.request_category,
        title=item.title,
        body=item.body,
        status=item.status,
        decision_reason=item.decision_reason,
        resulting_promotion_id=item.resulting_promotion_id,
        created_at=item.created_at,
    )


def _to_transition_response(item: Transition) -> RequestTransitionResponse:
    return RequestTransitionResponse(
        from_status=item.from_status,
        to_status=item.to_status,
        reason=item.reason,
        occurred_at=item.occurred_at,
    )


@router.post("/capability-requests", response_model=CapabilityRequestResponse, status_code=status.HTTP_201_CREATED)
async def raise_capability_request(
    request: Request,
    body: RaiseCapabilityRequestRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> CapabilityRequestResponse:
    """Ask the tenant that owns a capability for something, routed by the subject.

    `CapabilityRequestService.raise_request` resolves the owning tenant with a
    bare existence check (`subject exists and is active`), not a visibility
    filter -- its own docstring frames that as a routing lookup, not a read a
    caller sees the result of. Called directly from a route, that lookup turns
    into a cross-tenant existence oracle: a caller could tell a private entity
    apart from a nonexistent one just by whether the request is accepted or
    refused. So this route resolves the subject through the chokepoint first
    and, when it comes back invisible, raises the *identical* error the
    service itself raises for an absent subject -- same status, same message
    -- rather than inventing a second "you can't see that" answer that would
    itself be the tell. Absent and invisible are the same answer everywhere
    else in this file; this is the one place that rule has to be enforced a
    layer above the service that would otherwise skip it.
    """
    if not await _visibility(request).filter_entities(ctx, [body.subject_entity_id]):
        raise map_catalog_error(NotFoundError("no such capability"))
    try:
        created = await _capability_requests(request).raise_request(
            ctx,
            subject_entity_id=body.subject_entity_id,
            request_category=body.request_category,
            title=body.title,
            body=body.body,
        )
    except (NotFoundError, ValidationError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_capability_request_response(created)


@router.get("/capability-requests", response_model=CapabilityRequestListResponse)
async def list_capability_requests(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    role: Literal["owner", "requester"] = "owner",
    open_only: bool = Query(True),
    cursor: str | None = Query(None),
    page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = _DEFAULT_PAGE_SIZE,
) -> CapabilityRequestListResponse:
    """What is waiting on this tenant to decide, or what it has asked for.

    `role=owner` (the default) is the review queue -- `CapabilityRequestService.for_owner`,
    narrowed to still-open requests by `open_only` (default `true`), the same
    "what needs my attention" framing the curation queue and the proposal
    queue use. `role=requester` is the tenant's own outbound history --
    `raised_by` -- and `open_only` has no meaning there: a declined or
    duplicate-marked request is exactly the signal this surface exists to
    keep visible (see the module docstring), so requester mode always shows
    everything rather than silently filtering a view meant to show
    everything. Keyset-paginated on `(created_at, request_id)`, the same
    `cursor`/`page_size` in, `next_cursor` out shape every other list route
    in this file uses.
    """
    cursor_pair: tuple[datetime.datetime, uuid.UUID] | None = None
    if cursor is not None:
        try:
            payload = decode_cursor(cursor, strict=True)
            cursor_pair = (
                datetime.datetime.fromisoformat(payload["created_at"]),
                uuid.UUID(payload["request_id"]),
            )
        except (InvalidCursorError, KeyError, ValueError) as exc:
            raise build_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="invalid_cursor",
                message="invalid cursor",
            ) from exc

    svc = _capability_requests(request)
    if role == "owner":
        items = await svc.for_owner(ctx, open_only=open_only, cursor=cursor_pair, page_size=page_size)
    else:
        items = await svc.raised_by(ctx, cursor=cursor_pair, page_size=page_size)

    next_cursor: str | None = None
    if len(items) > page_size:
        items = items[:page_size]
        last = items[-1]
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "request_id": str(last.request_id)})

    stash_result_count(request, len(items))
    return CapabilityRequestListResponse(
        items=[_to_capability_request_response(i) for i in items],
        next_cursor=next_cursor,
    )


@router.get("/capability-requests/{request_id}", response_model=CapabilityRequestResponse)
async def get_capability_request(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    request_id: uuid.UUID,
) -> CapabilityRequestResponse:
    """One request, visible to its owner or the tenant that raised it.

    `CapabilityRequestService.get` already scopes to those two tenants and
    returns `None` to anyone else -- the same "absent and not-yours look
    identical" rule the raise route above enforces on the way in -- so there
    is nothing further to wrap here; this route just turns `None` into 404.
    """
    found = await _capability_requests(request).get(ctx, request_id)
    if found is None:
        raise map_catalog_error(NotFoundError("no such request"))
    return _to_capability_request_response(found)


@router.get("/capability-requests/{request_id}/history", response_model=RequestHistoryResponse)
async def get_capability_request_history(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    request_id: uuid.UUID,
) -> RequestHistoryResponse:
    """Every transition, in order.

    `CapabilityRequestService.history` already scopes to the caller the same
    way `get` does -- an empty tuple for a request that does not exist and
    an empty tuple for one that exists but belongs to neither of the
    caller's tenants, indistinguishable on purpose. This route reports both
    as an empty list rather than manufacturing a 404 the service's own
    return type gives no way to tell apart from "no transitions yet".
    """
    history = await _capability_requests(request).history(ctx, request_id)
    return RequestHistoryResponse(items=[_to_transition_response(t) for t in history])


async def transition_capability_request(
    request: Request,
    body: TransitionRequestRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    request_id: uuid.UUID,
) -> CapabilityRequestResponse:
    """Move a request along its lifecycle: acknowledge, accept, decline, mark
    duplicate, or resolve.

    Authority is entirely `CapabilityRequestService.transition`'s own gate
    (owning tenant, `producer`/`admin` role, and the legal-transition check)
    -- this route resolves tenant context and nothing else, the same
    division of labor `review_promotion_proposal` above uses for the
    proposal PATCH. `to_status` is a closed `Literal` so an unknown target
    status is a 422 from request validation rather than reaching the
    service's own transition-table check; illegal-but-named transitions
    (e.g. skipping straight to `accepted`) and a missing reason on a
    decision that requires one are still the service's 409/422 to raise.
    """
    try:
        updated = await _capability_requests(request).transition(
            ctx,
            request_id=request_id,
            to_status=body.to_status,
            reason=body.reason,
        )
    except (NotFoundError, ConflictError, ValidationError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return _to_capability_request_response(updated)


_mut_mr.add_mutation_route(
    path="/capability-requests/{request_id}",
    action="update",
    handler=transition_capability_request,
    verb="PATCH",
    response_model=CapabilityRequestResponse,
)


@router.post("/capability-requests/{request_id}:link-promotion", response_model=LinkRequestToPromotionResponse)
async def link_capability_request_to_promotion(
    request: Request,
    body: LinkRequestToPromotionRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    request_id: uuid.UUID,
) -> LinkRequestToPromotionResponse:
    """Record that an accepted (or already-resolved) request produced a
    canonical change, closing the loop visibly for the requester.

    Plain POST, not run through `HttpMethodRouter` -- the same reasoning
    `:link`/`:discard`/`:reverse` document above: there is no alternate verb
    for "point this request at the change it produced", so there is nothing
    to switch between across deployment modes.
    `CapabilityRequestService.link_to_promotion` refuses (409) a request that
    is not yet accepted or resolved -- a declined request cannot point at a
    change nobody agreed to make.
    """
    try:
        await _capability_requests(request).link_to_promotion(
            ctx, request_id=request_id, promotion_id=body.promotion_id
        )
    except (NotFoundError, ConflictError, PermissionError) as exc:
        raise map_catalog_error(exc) from exc
    return LinkRequestToPromotionResponse(status="linked")


# --- direct claim assertion ----------------------------------------------------


def _to_assert_claim_response(claim: StagedClaim) -> AssertClaimResponse:
    return AssertClaimResponse(
        claim_id=claim.claim_id,
        subject_entity_id=claim.subject_entity_id,
        predicate=claim.predicate,
        value=claim.value,
        status=claim.status,
        visibility=claim.visibility,
        owning_tenant_id=claim.owning_tenant_id,
        source_authority=claim.source_authority,
        is_contested=claim.is_contested,
    )


# `memory`, not `memory: curation`: this is the write half of `/v1/memory/claims`,
# whose read half is `memory`. Curation is what a human does to a claim that
# already exists, and asserting one is not that.
@router.post(
    "/claims",
    response_model=AssertClaimResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["memory"],
)
async def assert_claim(
    request: Request,
    body: AssertClaimRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    idem: IdempotencyContext = Depends(get_idempotency_context),
) -> AssertClaimResponse:
    """The agent-to-ingest feedback path: assert a claim directly, not through extraction.

    `stage_claim_defended` (`service/memory/claim_assertion.py`) runs
    directive-containment and PII checks before ever calling
    `ClaimService.stage_claim` -- see the module docstring above for why
    this route cannot call `stage_claim` directly. Ontology validation,
    subject resolution, authority derivation, and visibility all remain
    `stage_claim`'s own job, unchanged: an unresolvable `subject_reference`
    still lands the claim `unlinked` rather than refusing the write, and
    nothing here ever reaches the canonical graph -- promotion is the only
    path onto that, and it runs later, by a different actor, through its own
    review gate.
    """
    hit = await idem.lookup(ctx)
    if hit is not None:
        return JSONResponse(content=hit[1], status_code=hit[0])  # type: ignore[return-value]

    services: Services = request.app.state.services
    try:
        staged = await stage_claim_defended(
            services.session_factory,
            services.claims,
            ctx,
            subject_reference=body.subject_reference,
            predicate=body.predicate,
            value=body.value,
            evidence=tuple(Evidence(kind=item.kind, ref=item.ref, excerpt=item.excerpt) for item in body.evidence),
            asserted_valid_from=body.asserted_valid_from,
            asserted_valid_to=body.asserted_valid_to,
            visibility=body.visibility,
            namespace=body.namespace,
        )
    except CandidateRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "containment_refused",
                "message": str(exc),
                "trigger": exc.trigger,
            },
        ) from exc
    except ClaimPiiBlocked as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "pii_blocked",
                "message": str(exc),
                "matched_patterns": list(exc.matched_patterns),
            },
        ) from exc
    except ValidationError as exc:
        raise map_catalog_error(exc) from exc

    response = _to_assert_claim_response(staged)
    await idem.persist(ctx, status.HTTP_201_CREATED, response.model_dump(mode="json"))
    return response


__all__ = ["mutation_router", "router"]
