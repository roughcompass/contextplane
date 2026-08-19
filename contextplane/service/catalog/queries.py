"""Query helpers for the admin surfaces over this package: the
vocabulary/entity-type-schema surface, artifact reads, and the
progression-definition/override surface.

Plain module-level functions, each taking an already-open ``AsyncSession``.
Opening and committing the session is the caller's job — the admin routers
that call these need to interleave a precondition check between the read and
the write (an ``If-Match`` header for the vocabulary surface, a pre-flight
graduation scan and an audit-before-commit write for the progression one),
so the transaction boundary has to stay visible to them. What lives here is
every ``select()``, ``session.get()``, and ``session.add()`` those routers
used to build inline.

``resolve_actor_names`` is the one function also used from the artifacts
router: bulk-resolving ``created_by`` to a display name is the same Actor
lookup regardless of which admin surface is asking.

``record_audit_event`` writes one ``audit_log`` row via the ORM rather than
the hand-built ``INSERT ... VALUES`` text the progression router used to run
twice (once for definitions, once for overrides). ``AuditLog.after_jsonb`` is
a JSONB column mapped to a plain dict, so ``session.add(AuditLog(...))``
needs no ``CAST(... AS jsonb)`` — the driver does that translation. It is
deliberately not ``contextplane.audit.emit``: that writer runs in a
transaction of its own and swallows its failures so a failed audit cannot
roll back the mutation it describes, whereas this one flushes inside the
caller's transaction and returns the id, which is what lets the override
path commit an authorization record before the override it authorizes.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.storage.models import (
    Actor,
    Attribute,
    AuditLog,
    Entity,
    EntityTypeSchema,
    Fact,
    ProgressionDefinition,
    ProgressionOverride,
    VocabularyValue,
)

__all__ = [
    "add_vocabulary_value",
    "close_active_progression_definitions",
    "get_current_stage_progression_attribute",
    "get_entity_type_schema",
    "get_entity_type_schema_by_id",
    "get_entity_type_schema_for_update",
    "get_fact",
    "get_progression_definition",
    "get_progression_override",
    "get_vocabulary_value_for_update",
    "insert_entity_type_schema",
    "insert_progression_definition",
    "insert_progression_override",
    "list_active_entities_of_type",
    "list_artifacts",
    "list_current_attributes",
    "list_entity_type_schemas",
    "list_progression_definitions",
    "list_progression_overrides",
    "list_vocabulary_values",
    "record_audit_event",
    "resolve_actor_names",
]


# ---------------------------------------------------------------------------
# Vocabulary values
# ---------------------------------------------------------------------------


async def list_vocabulary_values(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
) -> list[VocabularyValue]:
    """Every vocabulary value for (tenant_id, kind), including deprecated rows."""
    result = await session.execute(
        select(VocabularyValue).where(
            VocabularyValue.tenant_id == tenant_id,
            VocabularyValue.kind == kind,
        )
    )
    return list(result.scalars().all())


async def add_vocabulary_value(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    value: str,
) -> VocabularyValue | None:
    """Return the row for (tenant_id, kind, value), or None if it does not exist.

    Called immediately after ``VocabularyService.add_value`` commits, to
    return the inserted row's server-assigned fields (vocab_id, created_at).
    """
    result = await session.execute(
        select(VocabularyValue).where(
            VocabularyValue.tenant_id == tenant_id,
            VocabularyValue.kind == kind,
            VocabularyValue.value == value,
        )
    )
    return result.scalar_one_or_none()


async def get_vocabulary_value_for_update(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    value: str,
) -> VocabularyValue | None:
    """Load one vocabulary value row inside a caller-managed write transaction."""
    result = await session.execute(
        select(VocabularyValue).where(
            VocabularyValue.tenant_id == tenant_id,
            VocabularyValue.kind == kind,
            VocabularyValue.value == value,
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Entity-type schemas
# ---------------------------------------------------------------------------


async def list_entity_type_schemas(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> list[EntityTypeSchema]:
    """Every current (t_invalidated_at IS NULL) entity type schema for the tenant."""
    result = await session.execute(
        select(EntityTypeSchema).where(
            EntityTypeSchema.tenant_id == tenant_id,
            EntityTypeSchema.t_invalidated_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def insert_entity_type_schema(
    session: AsyncSession,
    *,
    schema_id: uuid.UUID,
    tenant_id: uuid.UUID,
    entity_type: str,
    json_schema: dict[str, Any],
    is_advisory: bool,
    valid_from: datetime.datetime,
    now: datetime.datetime,
    created_by: uuid.UUID | None,
) -> None:
    """Insert a new entity-type schema row. Caller commits."""
    session.add(
        EntityTypeSchema(
            schema_id=schema_id,
            tenant_id=tenant_id,
            entity_type=entity_type,
            json_schema=json_schema,
            is_advisory=is_advisory,
            t_valid_from=valid_from,
            t_valid_to=None,
            t_ingested_at=now,
            t_invalidated_at=None,
            created_by=created_by,
        )
    )
    await session.flush()


async def get_entity_type_schema_by_id(session: AsyncSession, schema_id: uuid.UUID) -> EntityTypeSchema | None:
    """Return the entity-type schema row by primary key, or None if absent."""
    return await session.get(EntityTypeSchema, schema_id)


async def get_entity_type_schema(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
) -> EntityTypeSchema | None:
    """The current (most-recent, not-invalidated) schema row for entity_type, or None."""
    result = await session.execute(
        select(EntityTypeSchema)
        .where(
            EntityTypeSchema.tenant_id == tenant_id,
            EntityTypeSchema.entity_type == entity_type,
            EntityTypeSchema.t_invalidated_at.is_(None),
        )
        .order_by(EntityTypeSchema.t_valid_from.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_entity_type_schema_for_update(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_type: str,
) -> EntityTypeSchema | None:
    """Same lookup as get_entity_type_schema, inside a caller-managed write transaction."""
    return await get_entity_type_schema(session, tenant_id=tenant_id, entity_type=entity_type)


# ---------------------------------------------------------------------------
# Artifacts (facts) + actor display-name resolution
# ---------------------------------------------------------------------------


async def resolve_actor_names(
    session: AsyncSession,
    actor_ids: set[uuid.UUID],
) -> dict[uuid.UUID, str | None]:
    """Bulk-load display_name for the given actor_ids. None-safe; empty set short-circuits.

    A value of None means the actor row exists but never set a display name —
    distinct from the key being absent, which means the id wasn't found at all.
    """
    if not actor_ids:
        return {}
    result = await session.execute(select(Actor).where(Actor.actor_id.in_(actor_ids)))
    return {a.actor_id: a.display_name for a in result.scalars().all()}


async def get_fact(session: AsyncSession, fact_id: uuid.UUID) -> Fact | None:
    """Return the fact row by primary key, or None if it does not exist."""
    return await session.get(Fact, fact_id)


async def list_artifacts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    category_filter: list[str] | None,
    cursor_before: tuple[datetime.datetime, uuid.UUID | str] | None,
    page_size: int,
) -> list[Fact]:
    """Keyset page of current, non-superseded facts for a capability.

    Ordered (t_ingested_at DESC, fact_id DESC) — the same order the cursor is
    encoded against. Fetches page_size + 1 rows so the caller can tell whether
    a next page exists without a separate count query.
    """
    stmt = (
        select(Fact)
        .where(
            Fact.tenant_id == tenant_id,
            Fact.entity_id == entity_id,
            Fact.t_invalidated_at.is_(None),
            Fact.t_valid_to.is_(None),
            Fact.is_authoritative_superseded.is_(False),
        )
        .order_by(Fact.t_ingested_at.desc(), Fact.fact_id.desc())
        .limit(page_size + 1)
    )
    if category_filter:
        stmt = stmt.where(Fact.category.in_(category_filter))
    if cursor_before is not None:
        stmt = stmt.where(tuple_(Fact.t_ingested_at, Fact.fact_id) < cursor_before)

    result = await session.execute(stmt)
    return list(result.scalars())


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
