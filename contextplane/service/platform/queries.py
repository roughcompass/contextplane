"""Query helpers for progression-definition/override admin, audit-log reads,
and the audit_log write both of those admin surfaces need.

Plain module-level functions, each taking an already-open ``AsyncSession``.
The admin progression router owns transaction scope (it interleaves a
pre-flight scan and, on POST/PUT/DELETE, an audit-before-commit write between
reads and writes), so the boundary has to stay visible to it — what moves
here is every ``select()``, ``session.get()``, and ``session.add()`` call
those routers used to build inline.

``record_audit_event`` writes one ``audit_log`` row via the ORM rather than
the hand-built ``INSERT ... VALUES`` text the progression router used to run
twice (once for definitions, once for overrides). ``AuditLog.after_jsonb`` is
a JSONB column mapped to a plain dict, so ``session.add(AuditLog(...))``
needs no ``CAST(... AS jsonb)`` — the driver does that translation. This is
the same insert shape ``ProgressionDefinition``/``ProgressionOverride`` rows
already use a few lines away in the same router, so the two audit call sites
no longer need a different, raw-SQL path to write the same table.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.storage.models import (
    Attribute,
    AuditLog,
    Entity,
    ProgressionDefinition,
    ProgressionOverride,
)

__all__ = [
    "close_active_progression_definitions",
    "get_progression_definition",
    "get_progression_override",
    "insert_progression_definition",
    "insert_progression_override",
    "list_active_entities_of_type",
    "list_current_attributes",
    "list_progression_definitions",
    "list_progression_overrides",
    "query_audit_log",
    "record_audit_event",
]


# ---------------------------------------------------------------------------
# Progression definitions
# ---------------------------------------------------------------------------


async def insert_progression_definition(
    session: AsyncSession,
    *,
    progression_id: uuid.UUID,
    tenant_id: uuid.UUID,
    entity_type: str,
    definition: dict[str, Any],
    is_advisory: bool,
    now: datetime.datetime,
) -> None:
    """Insert a new progression-definition row, open (t_valid_to=NULL).

    Used both for a first-time POST and for the new row a PUT supersession
    inserts — the two are the same write, differing only in whether a prior
    active row for (tenant_id, entity_type) also gets closed in the same
    transaction (see close_active_progression_definitions).
    """
    session.add(
        ProgressionDefinition(
            progression_id=progression_id,
            tenant_id=tenant_id,
            entity_type=entity_type,
            definition=definition,
            is_advisory=is_advisory,
            t_valid_from=now,
            t_valid_to=None,
            t_ingested_at=now,
            t_invalidated_at=None,
        )
    )
    await session.flush()


async def get_progression_definition(session: AsyncSession, progression_id: uuid.UUID) -> ProgressionDefinition | None:
    """Return the progression definition by primary key, or None if absent."""
    return await session.get(ProgressionDefinition, progression_id)


async def list_progression_definitions(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[ProgressionDefinition]:
    """Every currently-active (t_valid_to IS NULL, t_invalidated_at IS NULL) definition."""
    result = await session.execute(
        select(ProgressionDefinition).where(
            ProgressionDefinition.tenant_id == tenant_id,
            ProgressionDefinition.t_valid_to.is_(None),
            ProgressionDefinition.t_invalidated_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def close_active_progression_definitions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
    now: datetime.datetime,
) -> list[ProgressionDefinition]:
    """Set t_valid_to=now on every currently-active row for (tenant_id, entity_type).

    Returns the rows closed, so a supersession that expected exactly one
    active predecessor can still see what it closed. Loaded and mutated
    inside the caller's write transaction so the close and the new row's
    insert commit together.
    """
    result = await session.execute(
        select(ProgressionDefinition).where(
            ProgressionDefinition.tenant_id == tenant_id,
            ProgressionDefinition.entity_type == entity_type,
            ProgressionDefinition.t_valid_to.is_(None),
            ProgressionDefinition.t_invalidated_at.is_(None),
        )
    )
    active_rows = list(result.scalars().all())
    for row in active_rows:
        row.t_valid_to = now
    return active_rows


# ---------------------------------------------------------------------------
# Progression overrides
# ---------------------------------------------------------------------------


async def insert_progression_override(
    session: AsyncSession,
    *,
    override_id: uuid.UUID,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    from_state: str,
    to_state: str,
    gate_id: str,
    bypass_skip_rules: bool,
    reason: str,
    authorized_by: uuid.UUID,
    t_valid_from: datetime.datetime,
    t_valid_to: datetime.datetime,
    audit_event_id: uuid.UUID,
) -> None:
    """Insert a single-use progression override row, referencing its (already-committed) audit row."""
    session.add(
        ProgressionOverride(
            override_id=override_id,
            tenant_id=tenant_id,
            entity_id=entity_id,
            from_state=from_state,
            to_state=to_state,
            gate_id=gate_id,
            bypass_skip_rules=bypass_skip_rules,
            reason=reason,
            authorized_by=authorized_by,
            t_valid_from=t_valid_from,
            t_valid_to=t_valid_to,
            consumed_at=None,
            audit_event_id=audit_event_id,
        )
    )


async def get_progression_override(session: AsyncSession, override_id: uuid.UUID) -> ProgressionOverride | None:
    """Return the progression override by primary key, or None if absent."""
    return await session.get(ProgressionOverride, override_id)


async def list_progression_overrides(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    consumed: bool | None,
    expired: bool | None,
    from_state: str | None,
    to_state: str | None,
    now: datetime.datetime,
) -> list[ProgressionOverride]:
    """Filter progression overrides by consumption, expiry, and state transition."""
    stmt = select(ProgressionOverride).where(
        ProgressionOverride.tenant_id == tenant_id,
        ProgressionOverride.entity_id == entity_id,
    )
    if consumed is True:
        stmt = stmt.where(ProgressionOverride.consumed_at.is_not(None))
    elif consumed is False:
        stmt = stmt.where(ProgressionOverride.consumed_at.is_(None))

    if expired is True:
        stmt = stmt.where(ProgressionOverride.t_valid_to < now)
    elif expired is False:
        stmt = stmt.where(ProgressionOverride.t_valid_to >= now)

    if from_state is not None:
        stmt = stmt.where(ProgressionOverride.from_state == from_state)
    if to_state is not None:
        stmt = stmt.where(ProgressionOverride.to_state == to_state)

    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Pre-flight graduation scan (Entity + Attribute reads)
# ---------------------------------------------------------------------------


async def list_active_entities_of_type(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
) -> list[Entity]:
    """Every active entity of (tenant_id, entity_type) — the pre-flight scan's population.

    Scoped to the caller's own tenant_id, same as every other read in this
    module; see the visibility-chokepoint allowlist entry for this file.
    """
    result = await session.execute(
        select(Entity).where(
            Entity.tenant_id == tenant_id,
            Entity.entity_type == entity_type,
            Entity.is_active.is_(True),
        )
    )
    return list(result.scalars().all())


async def get_current_stage_progression_attribute(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
) -> Attribute | None:
    """The entity's current (not invalidated, not superseded) stage_progression value, or None."""
    result = await session.execute(
        select(Attribute)
        .where(
            Attribute.tenant_id == tenant_id,
            Attribute.entity_id == entity_id,
            Attribute.key == "stage_progression",
            Attribute.t_invalidated_at.is_(None),
            Attribute.t_valid_to.is_(None),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_current_attributes(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
) -> list[Attribute]:
    """Every current attribute row for the entity — the pre-flight scan's gate-evaluation input."""
    result = await session.execute(
        select(Attribute).where(
            Attribute.tenant_id == tenant_id,
            Attribute.entity_id == entity_id,
            Attribute.t_invalidated_at.is_(None),
            Attribute.t_valid_to.is_(None),
        )
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Audit log — write (progression definitions/overrides) and read (admin_audit)
# ---------------------------------------------------------------------------


async def record_audit_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    after_jsonb: dict[str, Any],
    ts: datetime.datetime,
) -> uuid.UUID:
    """Insert one audit_log row and return its audit_id.

    before_jsonb is always NULL here: both progression call sites record a
    creation or a state change, never a diff against a prior value. Flushes
    (does not commit) so the caller decides the transaction boundary — the
    override path commits this row on its own before inserting the override
    it authorizes; the definition path writes both in one transaction.
    """
    audit_id = uuid.uuid4()
    session.add(
        AuditLog(
            audit_id=audit_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_jsonb=None,
            after_jsonb=after_jsonb,
            ts=ts,
            request_id=None,
            error_code=None,
        )
    )
    await session.flush()
    return audit_id


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
