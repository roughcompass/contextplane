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
from registry.service.memory import (
    DEFAULT_PAGE,
    MAX_PAGE,
    MemoryService,
    SessionEvent,
)
from registry.types import TenantContext

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
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
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
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
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
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc


__all__ = ["PII_FIELD", "router"]
