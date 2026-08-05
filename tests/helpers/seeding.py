"""Shared SQL seed helpers for integration tests.

Every integration test needs a tenant + actor row before it can call a
service, and several also need a bare entity row to hang claims or
attributes off. Those two inserts had been retyped under local
`_seed_tenant` / `_seed_entity` names across the traversal and
memory-claim test files. Where the insert statement, column set, and
values were byte-for-byte the same, that copy lives here once.

What did *not* get promoted here: `tests/integration/test_claim_erasure.py`
defines a `_Corpus` class with seed methods for actors, entities, claims
(with supersession/confirmation/promotion linkage), provenance, and
embeddings -- by a wide margin the richest seed helper in the suite. It
stays local. Nothing else in the tree needs a claim shaped with
`superseded_by` / `confirms_claim_id` / `promotion_state` wiring, so
promoting it would mean generalizing a 25-column INSERT for a readership
of one file. The two primitives every *other* file actually re-implements
-- "insert a tenant + actor" and "insert one bare entity" -- are exactly
what `seed_tenant_and_actor` and `seed_entity` below cover.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Fixed seed timestamp for the reverse-traversal / blast-radius / closure-cache
# family of integration tests. An arbitrary past instant works because these
# rows are read by recency-agnostic queries; a fixed value (rather than
# `datetime.now()`) keeps repeated test runs byte-identical.
_TRAVERSAL_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)

# Same role for the claim-serving / extraction family, pinned separately
# because the two groups of tests were written independently and there is
# no requirement that they share an epoch.
_CLAIM_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


async def seed_tenant_and_actor(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a tenant + actor. Returns (tenant_id, actor_id).

    No api_token row is written -- these tests authenticate via the
    entitlement auth harness (REST paths) or construct a TenantContext
    directly (service-layer paths), neither of which reads api_tokens.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _TRAVERSAL_NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, "
                    "oidc_subject, created_at) "
                    "VALUES (:aid, :tid, :dn, :oidc, :now)"
                ),
                {
                    "aid": actor_id,
                    "tid": tenant_id,
                    "dn": f"actor-{slug}",
                    "oidc": f"oidc-sub-{slug}",
                    "now": _TRAVERSAL_NOW,
                },
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def seed_tenant_and_actor_unique_oidc(pg_url: str, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a tenant + actor with a per-actor-id oidc_subject.

    Differs from `seed_tenant_and_actor` only in the actor's oidc_subject:
    this variant suffixes it with the actor id so tests that seed more than
    one actor under the same slug don't collide on oidc_subject uniqueness.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    oidc_subject = f"oidc-sub-{slug}-{actor_id.hex[:8]}"
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:tid, :slug, :slug, :now, TRUE)"
                ),
                {"tid": tenant_id, "slug": slug, "now": _TRAVERSAL_NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                    "VALUES (:aid, :tid, :sub, :dn, :now)"
                ),
                {"aid": actor_id, "tid": tenant_id, "sub": oidc_subject, "dn": f"actor-{slug}", "now": _TRAVERSAL_NOW},
            )
    finally:
        await engine.dispose()
    return tenant_id, actor_id


async def seed_entity(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, *, visibility: str = "public"
) -> uuid.UUID:
    """Insert a single capability entity. Returns entity_id."""
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                "                      is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, :vis, TRUE, :now)"
            ),
            {"eid": eid, "tid": tenant_id, "name": f"cap-{eid.hex[:8]}", "vis": visibility, "now": _CLAIM_NOW},
        )
    return eid


async def seed_shared_entity(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> uuid.UUID:
    """Insert a single `tenant-shared` capability entity. Returns entity_id.

    A fixed-visibility sibling of `seed_entity`: the extraction-pipeline
    tests that use this one never vary visibility, so there is no
    parameter to thread through -- only the entity_type row shape is
    shared with `seed_entity`.
    """
    eid = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO entities (entity_id, tenant_id, entity_type, name, visibility, "
                "                      is_active, created_at) "
                "VALUES (:eid, :tid, 'capability', :name, 'tenant-shared', TRUE, :now)"
            ),
            {"eid": eid, "tid": tenant_id, "name": f"cap-{eid.hex[:8]}", "now": _CLAIM_NOW},
        )
    return eid


__all__ = [
    "seed_tenant_and_actor",
    "seed_tenant_and_actor_unique_oidc",
    "seed_entity",
    "seed_shared_entity",
]
