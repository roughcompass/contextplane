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
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from registry.api.cursor import InvalidCursorError, decode_cursor, encode_cursor
from registry.api.errors import build_error, map_catalog_error
from registry.api.middleware.tenant import get_tenant_context
from registry.exceptions import ConflictError, NotFoundError, ValidationError
from registry.service.memory.claims import ClaimService, StagedClaim
from registry.service.memory.curation_queue import CurationQueueService, QueueItem
from registry.types import TenantContext
from registry.usage.results import stash_result_count
from registry.wiring.container import Services

router = APIRouter(tags=["memory curation"], prefix="/v1/memory")

_DEFAULT_PAGE_SIZE = 100
_MAX_PAGE_SIZE = 500


def _curation_queue(request: Request) -> CurationQueueService:
    services: Services = request.app.state.services
    return services.curation_queue


def _claims(request: Request) -> ClaimService:
    services: Services = request.app.state.services
    return services.claims


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


__all__ = ["router"]
