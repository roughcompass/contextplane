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
  split (`api/cursor.py`).
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
registry/`): nothing in this codebase writes a queryable record of a
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
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from registry.api.cursor import InvalidCursorError, decode_cursor, encode_cursor
from registry.api.errors import build_error, map_catalog_error
from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings
from registry.api.middleware.tenant import get_tenant_context
from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.service.memory.claims import ClaimService, StagedClaim
from registry.service.memory.curation_queue import CurationQueueService, QueueItem
from registry.service.memory.promotion import PromotionService, Proposal
from registry.types import TenantContext
from registry.usage.results import stash_result_count
from registry.wiring.container import Services

router = APIRouter(tags=["memory curation"], prefix="/v1/memory")

_mode, _sep = get_mode_settings()
mutation_router = APIRouter(tags=["memory curation"], prefix="/v1/memory")
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


class _Strict(BaseModel):
    """Closed view models, request and response alike.

    A request field the caller misspelled and had silently dropped would
    look like it took effect when it did not; a response model left open
    could grow an undocumented field with nobody noticing the contract
    changed.
    """

    model_config = ConfigDict(extra="forbid")


# --- curation queue ---------------------------------------------------------


class QueueItemResponse(_Strict):
    claim_id: uuid.UUID
    reason: str
    subject_reference: str
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    confidence: float | None
    created_at: datetime.datetime
    human_backed: bool
    proposal_id: uuid.UUID | None
    available_actions: list[str]


class QueueListResponse(_Strict):
    items: list[QueueItemResponse]
    next_cursor: str | None


class QueueCountsResponse(_Strict):
    counts: dict[str, int]


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


# --- link / discard ----------------------------------------------------------


class LinkClaimRequest(_Strict):
    subject_reference: str = Field(min_length=1)


class LinkedClaimResponse(_Strict):
    claim_id: uuid.UUID
    subject_entity_id: uuid.UUID | None
    predicate: str
    value: Any
    status: str
    visibility: str
    owning_tenant_id: uuid.UUID | None
    source_authority: str
    is_contested: bool


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


class DiscardClaimRequest(_Strict):
    reason: str = Field(min_length=1)


class DiscardResponse(_Strict):
    status: str


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


class ProposalResponse(_Strict):
    proposal_id: uuid.UUID
    claim_id: uuid.UUID
    owner_tenant_id: uuid.UUID
    author_tenant_id: uuid.UUID
    subject_entity_id: uuid.UUID
    predicate: str
    target_kind: str
    target_key: str
    current_value: Any
    proposed_value: Any
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    high_impact: bool
    high_impact_reasons: list[str]
    state: str
    created_at: datetime.datetime | None


class ProposalListResponse(_Strict):
    items: list[ProposalResponse]
    next_cursor: str | None


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


class ReviewProposalRequest(_Strict):
    state: Literal["accepted", "rejected"]
    # `None` and "not sent" are different: an accept with no amendment must
    # promote the claim's own proposed value, never a caller-shaped null.
    # `model_fields_set` on the parsed body is how the handler tells them
    # apart -- see `review_promotion_proposal` below.
    amended_value: Any = None
    reason: str | None = Field(default=None, min_length=1)


class ProposalDecisionResponse(_Strict):
    # Nested rather than flattened: `proposal` is always "the row's current
    # state", and `promotion_id` is always "the promotion this call itself
    # just created, or None" -- collapsing the two onto one flat model would
    # make a `null` on a later GET of the same shape ambiguous between "never
    # promoted" and "not asked about here".
    proposal: ProposalResponse
    promotion_id: uuid.UUID | None


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


class ReversePromotionRequest(_Strict):
    reason: str = Field(min_length=1)


class ReversePromotionResponse(_Strict):
    status: str


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


__all__ = ["mutation_router", "router"]
