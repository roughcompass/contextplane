"""An agent's own session memory under `/v1/memory/*`.

Thin adapters. Every route resolves the caller, calls one service method, and
translates a typed error into a status.

**No route accepts an actor identifier.** A caller able to name an actor could
read another's sessions, and this is the one surface in the system where that
would not be caught downstream — sessions carry no visibility setting and no
sharing mode, so the actor on the credential is the only thing scoping them.
The omission is the control.

**A session that is not yours is 404, not 403.** Distinguishing them confirms
the session exists, which is itself something the caller is not entitled to
know. The same rule the receipt surface uses.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from registry.api.errors import build_error
from registry.api.middleware.tenant import get_tenant_context
from registry.api.pii_guard import run_pii_scan
from registry.exceptions import NotFoundError, ValidationError
from registry.service.memory.claim_serving import (
    PERSONA_AGENT,
    PERSONAS,
    ClaimQuery,
    ClaimServingService,
    ServedClaim,
)
from registry.service.memory.session_events import (
    DEFAULT_PAGE,
    MAX_PAGE,
    MemoryService,
    SessionEvent,
)
from registry.types import TenantContext
from registry.usage.results import stash_result_count

router = APIRouter(tags=["memory"], prefix="/v1/memory")

# The logical field the PII scanner and its detection log record this under, so
# a tenant can set a policy for conversation bodies distinct from artifacts.
PII_FIELD = "memory_session_event.body"


def _service(request: Request) -> MemoryService:
    service = getattr(request.app.state, "memory", None)
    if service is None:
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="session memory is not configured on this deployment",
        )
    return service  # type: ignore[no-any-return]


class _Strict(BaseModel):
    """Closed request models.

    Worth more here than elsewhere: a caller that misspells `metadata` and has
    it silently dropped believes it attached a filter key that was never
    stored, and will not find those events again.
    """

    model_config = ConfigDict(extra="forbid")


class RecordEventRequest(_Strict):
    kind: str = Field(pattern=r"^(user_message|agent_action|tool_invocation)$")
    body: str = Field(min_length=1)
    tool_name: str | None = Field(default=None, max_length=200)
    # Structure-region, deliberately: indexed and filterable, and therefore
    # *not* PII-scanned, redacted, or encrypted. Documented at the surface
    # because a caller who puts a customer email in a metadata value has put it
    # somewhere the scanner never looks.
    metadata: dict[str, str] = Field(default_factory=dict)


class EventResponse(BaseModel):
    event_id: uuid.UUID
    session_id: str
    seq: int
    kind: str
    body: str
    tool_name: str | None
    metadata: dict[str, Any]
    created_at: datetime.datetime


class SessionResponse(BaseModel):
    session_id: str
    event_count: int
    first_activity_at: datetime.datetime
    last_activity_at: datetime.datetime


def _event(event: SessionEvent) -> EventResponse:
    return EventResponse(
        event_id=event.event_id,
        session_id=event.session_id,
        seq=event.seq,
        kind=event.kind,
        body=event.body,
        tool_name=event.tool_name,
        metadata=dict(event.metadata),
        created_at=event.created_at,
    )


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, NotFoundError):
        return build_error(status.HTTP_404_NOT_FOUND, code="not_found", message="not found")
    if isinstance(exc, ValidationError):
        return build_error(status.HTTP_400_BAD_REQUEST, code="validation_error", message=str(exc))
    return exc


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    since: datetime.datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
) -> list[SessionResponse]:
    """The caller's own sessions, most recently active first.

    The entry point for resuming earlier work: an agent that has lost its
    context asks what it was doing before deciding which session to replay.
    """
    try:
        sessions = await _service(request).list_sessions(ctx, since=since, limit=limit)
    except Exception as exc:
        raise _translate(exc) from exc
    stash_result_count(request, len(sessions))
    return [
        SessionResponse(
            session_id=s.session_id,
            event_count=s.event_count,
            first_activity_at=s.first_activity_at,
            last_activity_at=s.last_activity_at,
        )
        for s in sessions
    ]


@router.post(
    "/sessions/{session_id}/events",
    response_model=EventResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_event(
    request: Request,
    body: RecordEventRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    session_id: Annotated[str, Path(min_length=1, max_length=200)],
) -> EventResponse:
    """Append one immutable event.

    The session is not created here; it exists because its events do. There is
    no update route: an event is write-once, removable only by the author, by
    retention, or by an erasure request.

    The body is scanned before storage and a blocking tenant policy refuses the
    write. `metadata` is not scanned -- see the request model.
    """
    await run_pii_scan(request, ctx, body.body, PII_FIELD)
    try:
        event = await _service(request).record_event(
            ctx,
            session_id=session_id,
            kind=body.kind,
            body=body.body,
            tool_name=body.tool_name,
            metadata=dict(body.metadata),
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return _event(event)


@router.get("/sessions/{session_id}/events", response_model=list[EventResponse])
async def list_session_events(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    session_id: Annotated[str, Path(min_length=1, max_length=200)],
    since: int | None = None,
    until: int | None = None,
    kind: str | None = None,
    cursor: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    order: Annotated[str, Query(pattern=r"^(asc|desc)$")] = "asc",
) -> list[EventResponse]:
    """Replay a session in sequence order, forward or reverse.

    `since`, `until` and `cursor` are sequence numbers rather than timestamps
    or offsets. A timestamp cannot order a burst of events recorded in the same
    microsecond, and an offset over an append-only log re-reads shifting
    windows as new events arrive mid-page.

    Reverse order with a small limit is how a resuming agent asks for "the last
    few turns" without reading a whole conversation.
    """
    try:
        events = await _service(request).list_events(
            ctx,
            session_id=session_id,
            since_seq=since,
            until_seq=until,
            kind=kind,
            cursor=cursor,
            limit=limit,
            order=order,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    stash_result_count(request, len(events))
    return [_event(e) for e in events]


@router.get("/sessions/{session_id}/events/{event_id}", response_model=EventResponse)
async def get_session_event(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    session_id: Annotated[str, Path(min_length=1, max_length=200)],
    event_id: Annotated[uuid.UUID, Path()],
) -> EventResponse:
    try:
        event = await _service(request).get_event(ctx, session_id=session_id, event_id=event_id)
    except Exception as exc:
        raise _translate(exc) from exc
    return _event(event)


@router.delete(
    "/sessions/{session_id}/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Explicit, because FastAPI otherwise infers a response model from the
    # `-> None` annotation. Under postponed evaluation that resolves to a
    # truthy NoneType, which trips FastAPI's own assertion that a 204 must
    # carry no body.
    response_model=None,
)
async def delete_session_event(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    session_id: Annotated[str, Path(min_length=1, max_length=200)],
    event_id: Annotated[uuid.UUID, Path()],
) -> None:
    """Remove one of the caller's own events before retention elapses.

    Soft-invalidation: the event leaves every read path but stays addressable
    for audit. Physical erasure is a separate operation with a separate
    justification.
    """
    try:
        await _service(request).delete_event(ctx, session_id=session_id, event_id=event_id)
    except Exception as exc:
        raise _translate(exc) from exc


__all__ = ["PII_FIELD", "router"]


# --- claim retrieval ----------------------------------------------------------
#
# Separate from the session routes above because they answer different questions.
# A session read returns what an agent said; a claim read returns what the system
# now believes, with the evidence for it. The second is the governed surface, and
# everything it returns is labelled as recalled rather than authoritative.


class CitationResponse(_Strict):
    """A resolvable handle to the evidence behind a claim."""

    kind: str
    ref: str
    excerpt: str | None = None


class ClaimResponse(_Strict):
    """A claim with everything needed to check it.

    Every field below the value is part of the citation payload, and none is
    optional. A response type with optional citations would let a serving path
    return an unverifiable answer that still validated.
    """

    claim_id: uuid.UUID
    subject_entity_id: uuid.UUID
    predicate: str
    value: Any
    claim_category: str
    confidence: float
    authority: str
    valid_from: datetime.datetime
    valid_to: datetime.datetime | None
    as_of: datetime.datetime
    human_confirmed: bool
    citations: list[CitationResponse]
    label: str
    trust: str
    trust_note: str


def _claim_service(request: Request) -> ClaimServingService:
    service = getattr(request.app.state, "claim_serving", None)
    if service is None:
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="claim retrieval is not configured on this deployment",
        )
    return service  # type: ignore[no-any-return]


def _to_response(claim: ServedClaim) -> ClaimResponse:
    return ClaimResponse(
        claim_id=claim.claim_id,
        subject_entity_id=claim.subject_entity_id,
        predicate=claim.predicate,
        value=claim.value,
        claim_category=claim.claim_category,
        confidence=claim.confidence,
        authority=claim.authority,
        valid_from=claim.valid_from,
        valid_to=claim.valid_to,
        as_of=claim.as_of,
        human_confirmed=claim.human_confirmed,
        citations=[CitationResponse(kind=c.kind, ref=c.ref, excerpt=c.excerpt) for c in claim.citations],
        label=claim.label,
        trust=claim.trust,
        trust_note=claim.trust_note,
    )


@router.get("/claims", response_model=list[ClaimResponse])
async def query_claims(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    subject_entity_id: uuid.UUID | None = None,
    predicate: str | None = None,
    category: str | None = None,
    namespace_prefix: str | None = None,
    min_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    as_of: datetime.datetime | None = None,
    persona: str = PERSONA_AGENT,
    limit: Annotated[int, Query(ge=1, le=ClaimQuery.MAX_LIMIT)] = 10,
) -> list[ClaimResponse]:
    """What the system believes, by exact structural match.

    An indexed lookup rather than ranked retrieval: the caller names the subject
    and the predicate and gets the claims that match, not the claims that resemble
    the question. `as_of` reads the history, so "what did we believe last month" is
    answerable from the same route.
    """
    try:
        spec = ClaimQuery(
            subject_entity_id=subject_entity_id,
            predicate=predicate,
            category=category,
            namespace_prefix=namespace_prefix,
            min_confidence=min_confidence,
            as_of=as_of,
            persona=persona,
            limit=limit,
        )
    except ValueError as exc:
        raise build_error(status.HTTP_422_UNPROCESSABLE_ENTITY, code="invalid_query", message=str(exc)) from exc

    claims = await _claim_service(request).query(ctx, spec)
    stash_result_count(request, len(claims))
    return [_to_response(c) for c in claims]


@router.get("/claims/search", response_model=list[ClaimResponse])
async def search_claims(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    q: str,
    namespace_prefix: str | None = None,
    category: str | None = None,
    min_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    persona: str = PERSONA_AGENT,
    top_k: Annotated[int, Query(ge=1, le=ClaimQuery.MAX_LIMIT)] = 10,
) -> list[ClaimResponse]:
    """Semantic search over remembered claims, for when the predicate is unknown.

    The counterpart to the structural lookup above. That one needs the caller to name
    what they are asking for; this one takes a question in prose and ranks claims by
    how close they are to it, fusing a vector arm with a lexical one.

    Declared before `/claims/{claim_id}`, and that ordering is load-bearing. FastAPI
    matches in declaration order, so with the id route first a request for
    `/claims/search` binds `search` to a UUID path parameter and fails validation --
    it does not fall through to the next route.
    """
    embedder = getattr(request.app.state, "embedder", None)
    if embedder is None:
        raise build_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="semantic retrieval is not configured on this deployment",
        )
    if persona not in PERSONAS:
        raise build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="invalid_query",
            message=f"unknown persona {persona!r}",
        )
    try:
        claims = await _claim_service(request).retrieve(
            ctx,
            query=q,
            embedder=embedder,
            namespace_prefix=namespace_prefix,
            category=category,
            min_confidence=min_confidence,
            persona=persona,
            top_k=top_k,
        )
    except ValueError as exc:
        raise build_error(status.HTTP_422_UNPROCESSABLE_ENTITY, code="invalid_query", message=str(exc)) from exc
    stash_result_count(request, len(claims))
    return [_to_response(c) for c in claims]


@router.get("/claims/{claim_id}", response_model=ClaimResponse)
async def get_claim(
    request: Request,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    claim_id: Annotated[uuid.UUID, Path()],
    persona: str = PERSONA_AGENT,
) -> ClaimResponse:
    """One claim, or 404.

    A claim the caller may not see is absent rather than forbidden. Telling them it
    exists but is not theirs is an existence oracle over every claim in the
    deployment, and the subject of a claim is often the sensitive part.
    """
    if persona not in PERSONAS:
        raise build_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="invalid_query",
            message=f"unknown persona {persona!r}",
        )
    claim = await _claim_service(request).get(ctx, claim_id, persona=persona)
    if claim is None:
        raise build_error(status.HTTP_404_NOT_FOUND, code="not_found", message="no such claim")
    return _to_response(claim)
