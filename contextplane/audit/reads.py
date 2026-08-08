"""Audit-log reads.

The write side of this table lives in two places for two different reasons:
``emit`` writes in a transaction of its own and swallows its failures, so a
failed audit row can never roll back the mutation it describes, while the
progression admin surface's ``record_audit_event`` (in
``contextplane.service.catalog.queries``) flushes inside the caller's
transaction so an override can commit its authorization record before the
override it authorizes. Reading is simpler than either: one keyset-paged
query, always scoped to one tenant, behind the admin audit-log endpoint.

Plain module-level functions taking an already-open ``AsyncSession``, matching
the shape of the query helpers the admin routers call. Nothing here imports
above ``storage`` -- this module reads one table through the ORM and knows
nothing about services, transports, or tenancy resolution.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.storage.models import AuditLog

__all__ = ["query_audit_log"]


async def query_audit_log(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str | None,
    target_type: str | None,
    target_id: uuid.UUID | None,
    from_dt: datetime.datetime | None,
    to_dt: datetime.datetime | None,
    cursor: tuple[datetime.datetime, uuid.UUID] | None,
    page_size: int,
) -> list[AuditLog]:
    """Keyset page of audit_log rows, always scoped to tenant_id.

    Ordered (ts DESC, audit_id DESC) — the same order the cursor is encoded
    against. Fetches page_size + 1 rows so the caller can tell whether a next
    page exists without a separate count query.
    """
    conditions = [AuditLog.tenant_id == tenant_id]
    if actor_id is not None:
        conditions.append(AuditLog.actor_id == actor_id)
    if action is not None:
        conditions.append(AuditLog.action == action)
    if target_type is not None:
        conditions.append(AuditLog.target_type == target_type)
    if target_id is not None:
        conditions.append(AuditLog.target_id == target_id)
    if from_dt is not None:
        conditions.append(AuditLog.ts >= from_dt)
    if to_dt is not None:
        conditions.append(AuditLog.ts <= to_dt)
    if cursor is not None:
        cursor_ts, cursor_audit_id = cursor
        conditions.append(
            or_(
                AuditLog.ts < cursor_ts,
                and_(AuditLog.ts == cursor_ts, AuditLog.audit_id < cursor_audit_id),
            )
        )

    stmt = (
        select(AuditLog)
        .where(and_(*conditions))
        .order_by(AuditLog.ts.desc(), AuditLog.audit_id.desc())
        .limit(page_size + 1)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
