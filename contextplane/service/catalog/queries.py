"""Query helpers for the admin vocabulary/capability-type-schema surface and
artifact reads.

Plain module-level functions, each taking an already-open ``AsyncSession``.
Opening and committing the session is the caller's job — the admin routers
that call these need to interleave an ``If-Match`` precondition check (a
transport concern) between the read and the write, so the transaction
boundary has to stay visible to them. What lives here is every ``select()``,
``session.get()``, and ``session.add()`` those routers used to build inline.

``resolve_actor_names`` is the one function also used from the artifacts
router: bulk-resolving ``created_by`` to a display name is the same Actor
lookup regardless of which admin surface is asking.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.storage.models import Actor, CapabilityTypeSchema, Fact, VocabularyValue

__all__ = [
    "add_vocabulary_value",
    "get_capability_type",
    "get_capability_type_by_id",
    "get_capability_type_for_update",
    "get_fact",
    "get_vocabulary_value_for_update",
    "insert_capability_type",
    "list_artifacts",
    "list_capability_types",
    "list_vocabulary_values",
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
# Capability-type schemas
# ---------------------------------------------------------------------------


async def list_capability_types(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> list[CapabilityTypeSchema]:
    """Every current (t_invalidated_at IS NULL) capability type schema for the tenant."""
    result = await session.execute(
        select(CapabilityTypeSchema).where(
            CapabilityTypeSchema.tenant_id == tenant_id,
            CapabilityTypeSchema.t_invalidated_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def insert_capability_type(
    session: AsyncSession,
    *,
    schema_id: uuid.UUID,
    tenant_id: uuid.UUID,
    type_name: str,
    json_schema: dict[str, Any],
    is_advisory: bool,
    valid_from: datetime.datetime,
    now: datetime.datetime,
    created_by: uuid.UUID | None,
) -> None:
    """Insert a new capability-type schema row. Caller commits."""
    session.add(
        CapabilityTypeSchema(
            schema_id=schema_id,
            tenant_id=tenant_id,
            type_name=type_name,
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


async def get_capability_type_by_id(session: AsyncSession, schema_id: uuid.UUID) -> CapabilityTypeSchema | None:
    """Return the capability-type schema row by primary key, or None if absent."""
    return await session.get(CapabilityTypeSchema, schema_id)


async def get_capability_type(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    type_name: str,
) -> CapabilityTypeSchema | None:
    """The current (most-recent, not-invalidated) schema row for type_name, or None."""
    result = await session.execute(
        select(CapabilityTypeSchema)
        .where(
            CapabilityTypeSchema.tenant_id == tenant_id,
            CapabilityTypeSchema.type_name == type_name,
            CapabilityTypeSchema.t_invalidated_at.is_(None),
        )
        .order_by(CapabilityTypeSchema.t_valid_from.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_capability_type_for_update(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    type_name: str,
) -> CapabilityTypeSchema | None:
    """Same lookup as get_capability_type, inside a caller-managed write transaction."""
    return await get_capability_type(session, tenant_id=tenant_id, type_name=type_name)


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
