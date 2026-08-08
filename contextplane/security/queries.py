"""Query helpers for the tenant PII pattern + field-policy admin surface.

Plain module-level functions, each taking an already-open ``AsyncSession``.
These are the CRUD reads and writes behind the admin PII endpoints — separate
from ``contextplane.security.pii_guard``, which reads the same two tables at scan time
to resolve the effective policy for one write. That module stays as it is;
this one exists so the admin router that lets an operator manage the rows
``pii_guard`` reads no longer builds `select()`/`session.add()` calls inline.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.storage.models import PiiFieldPolicyRow, PiiPatternRow

__all__ = [
    "delete_pii_field_policy",
    "delete_pii_pattern",
    "get_pii_field_policy_for_update",
    "get_pii_pattern_for_update",
    "insert_pii_field_policy",
    "insert_pii_pattern",
    "list_pii_field_policies",
    "list_pii_patterns",
]


# ---------------------------------------------------------------------------
# pii_patterns
# ---------------------------------------------------------------------------


async def insert_pii_pattern(
    session: AsyncSession,
    *,
    pattern_id: uuid.UUID,
    tenant_id: uuid.UUID,
    name: str,
    category: str,
    regex: str,
    policy_override: str | None,
    is_enabled: bool,
    created_at: datetime.datetime,
    created_by: uuid.UUID | None,
) -> None:
    """Insert a tenant-authored PII pattern row. is_system is always False here."""
    session.add(
        PiiPatternRow(
            pattern_id=pattern_id,
            tenant_id=tenant_id,
            name=name,
            category=category,
            regex=regex,
            is_system=False,
            detector_module=None,
            policy_override=policy_override,
            is_enabled=is_enabled,
            created_at=created_at,
            created_by=created_by,
        )
    )
    await session.flush()


async def list_pii_patterns(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[PiiPatternRow]:
    """Every PII pattern for the tenant, including system-seeded rows, oldest first."""
    result = await session.execute(
        select(PiiPatternRow).where(PiiPatternRow.tenant_id == tenant_id).order_by(PiiPatternRow.created_at)
    )
    return list(result.scalars().all())


async def get_pii_pattern_for_update(session: AsyncSession, pattern_id: uuid.UUID) -> PiiPatternRow | None:
    """Load one PII pattern row by primary key, inside a caller-managed write transaction."""
    return await session.get(PiiPatternRow, pattern_id)


async def delete_pii_pattern(session: AsyncSession, row: PiiPatternRow) -> None:
    """Hard-delete an already-loaded PII pattern row."""
    await session.delete(row)


# ---------------------------------------------------------------------------
# pii_field_policies
# ---------------------------------------------------------------------------


async def insert_pii_field_policy(
    session: AsyncSession,
    *,
    policy_id: uuid.UUID,
    tenant_id: uuid.UUID,
    field_type: str,
    pattern_id: uuid.UUID | None,
    policy: str,
    created_at: datetime.datetime,
) -> None:
    """Insert a per-field PII policy override row.

    Raises sqlalchemy.exc.IntegrityError on the (tenant_id, field_type)
    unique-when-pattern_id-is-null constraint; the caller maps that to 409.
    """
    session.add(
        PiiFieldPolicyRow(
            policy_id=policy_id,
            tenant_id=tenant_id,
            field_type=field_type,
            pattern_id=pattern_id,
            policy=policy,
            created_at=created_at,
        )
    )
    await session.flush()


async def list_pii_field_policies(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[PiiFieldPolicyRow]:
    """Every per-field PII policy override for the tenant, oldest first."""
    result = await session.execute(
        select(PiiFieldPolicyRow).where(PiiFieldPolicyRow.tenant_id == tenant_id).order_by(PiiFieldPolicyRow.created_at)
    )
    return list(result.scalars().all())


async def get_pii_field_policy_for_update(session: AsyncSession, policy_id: uuid.UUID) -> PiiFieldPolicyRow | None:
    """Load one PII field-policy row by primary key, inside a caller-managed write transaction."""
    return await session.get(PiiFieldPolicyRow, policy_id)


async def delete_pii_field_policy(session: AsyncSession, row: PiiFieldPolicyRow) -> None:
    """Hard-delete an already-loaded PII field-policy row."""
    await session.delete(row)
