"""Admin audit-log query endpoint.

GET /v1/admin/audit — keyset-paginated audit log query (auditor role)
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel

from contextplane.api.errors import build_error
from contextplane.api.routers._admin_common import _auditor_required
from contextplane.pagination import InvalidCursorError, decode_cursor, encode_cursor
from contextplane.service.platform import queries as platform_queries
from contextplane.storage.models import AuditLog
from contextplane.types import TenantContext

router = APIRouter(prefix="/v1/admin")

_AUDIT_MAX_PAGE_SIZE = 500
_AUDIT_DEFAULT_PAGE_SIZE = 50


class AuditRow(BaseModel):
    """One audit event with its before/after states and metadata."""

    audit_id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: uuid.UUID
    before_jsonb: dict[str, Any] | None
    after_jsonb: dict[str, Any] | None
    ts: datetime.datetime
    request_id: str | None
    error_code: str | None


class AuditResponse(BaseModel):
    """Paginated audit log; next_cursor is null when no further events exist."""

    items: list[AuditRow]
    next_cursor: str | None


def _audit_to_row(a: AuditLog) -> AuditRow:
    return AuditRow(
        audit_id=a.audit_id,
        actor_id=a.actor_id,
        action=a.action,
        target_type=a.target_type,
        target_id=a.target_id,
        before_jsonb=a.before_jsonb,
        after_jsonb=a.after_jsonb,
        ts=a.ts,
        request_id=a.request_id,
        error_code=a.error_code,
    )


@router.get("/audit", response_model=AuditResponse, tags=["admin: audit"])
async def query_audit_log(
    request: Request,
    ctx: TenantContext = Depends(_auditor_required),
    actor_id: uuid.UUID | None = Query(None),
    action: str | None = Query(None),
    target_type: str | None = Query(None),
    target_id: uuid.UUID | None = Query(None),
    from_dt: datetime.datetime | None = Query(None, alias="from"),
    to_dt: datetime.datetime | None = Query(None, alias="to"),
    cursor: str | None = Query(None),
    page_size: int = Query(_AUDIT_DEFAULT_PAGE_SIZE, ge=1, le=_AUDIT_MAX_PAGE_SIZE),
) -> AuditResponse:
    """Query audit log with keyset pagination.

    tenant_id is always injected from TenantContext — callers cannot query
    another tenant's data.  Sorted DESC by (ts, audit_id).
    """
    factory = request.app.state.session_factory

    # Keyset: cursor encodes (ts, audit_id); page continues from rows strictly
    # before the cursor position (DESC order: ts < cursor_ts OR (ts == cursor_ts AND audit_id < cursor_id)).
    cursor_pair: tuple[datetime.datetime, uuid.UUID] | None = None
    if cursor is not None:
        try:
            cursor_payload = decode_cursor(cursor, strict=True)
            cursor_pair = (
                datetime.datetime.fromisoformat(cursor_payload["ts"]),
                uuid.UUID(cursor_payload["audit_id"]),
            )
        except (InvalidCursorError, KeyError, ValueError) as exc:
            raise build_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="invalid_cursor",
                message="invalid cursor",
            ) from exc

    # tenant_id always comes from ctx — callers cannot query another tenant's data.
    async with factory() as session:
        rows = await platform_queries.query_audit_log(
            session,
            tenant_id=ctx.tenant_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            from_dt=from_dt,
            to_dt=to_dt,
            cursor=cursor_pair,
            page_size=page_size,
        )

    next_cursor: str | None = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        last = rows[-1]
        next_cursor = encode_cursor({"ts": last.ts.isoformat(), "audit_id": str(last.audit_id)})

    return AuditResponse(items=[_audit_to_row(r) for r in rows], next_cursor=next_cursor)
