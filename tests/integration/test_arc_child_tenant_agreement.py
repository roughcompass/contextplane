"""A directive or rule may only name the tenant its revision names.

Both child tables carry a `tenant_id` that is a copy of their revision's. Until
this constraint existed, nothing enforced the copy: `ArtifactService` derives it
from the parent inside the INSERT and no UPDATE mutates it, so agreement held by
convention alone.

That convention was carrying more weight than a convention should. The corpus
query filters candidates on the *revision's* tenant, and `rule_applies` checks
the requesting tenant only for `tenant`-scoped rules -- a `domain`-,
`capability`-, or `task`-scoped rule gets no tenant check anywhere downstream.
One predicate over one column was the whole boundary between one tenant's
governance and another's resolution.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.helpers.arc_fixtures import ARC_NOW, ArcSeed, seed_arc


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-tenant-agree")


async def _insert_directive(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed, *, tenant_id: uuid.UUID | None
) -> None:
    directive_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO arc_directive_identities (directive_id, artifact_id) VALUES (:did, :aid)"),
            {"did": directive_id, "aid": seed.artifact_id},
        )
        # Ciphertext rather than plaintext when the tenant is NULL: the schema
        # separately forbids plaintext on a global row, and hitting that CHECK
        # would mask whether the foreign key did anything.
        column, value = (
            ("compact_statement_plaintext", "x")
            if tenant_id is not None
            else ("compact_statement_ciphertext", b"sealed")
        )
        await session.execute(
            text(
                "INSERT INTO arc_directives ("
                "  directive_id, revision_id, tenant_id, directive_type,"
                f"  {column}, source_anchor"  # noqa: S608
                ") VALUES (:did, :rid, :tid, 'citation_only', :value, 'anchor')"
            ),
            {"did": directive_id, "rid": seed.revision_id, "tid": tenant_id, "value": value},
        )


@pytest.mark.asyncio
async def test_a_directive_naming_another_tenant_is_refused(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The case that could actually widen a boundary: a concrete, different
    tenant. Downstream nothing would catch it -- visibility is decided from the
    revision, and a domain-scoped rule is never tenant-checked."""
    other = await seed_arc(factory, slug_prefix="arc-tenant-agree-other")

    with pytest.raises(IntegrityError):
        await _insert_directive(factory, seed, tenant_id=other.tenant_id)


@pytest.mark.asyncio
async def test_a_directive_naming_its_own_revisions_tenant_is_accepted(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The control. A constraint that rejected the legitimate shape would be
    worse than none, because the write path only ever produces this shape."""
    await _insert_directive(factory, seed, tenant_id=seed.tenant_id)


@pytest.mark.asyncio
async def test_a_rule_naming_another_tenant_is_refused(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    other = await seed_arc(factory, slug_prefix="arc-tenant-agree-rule")

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO arc_applicability_rules ("
                    "  rule_id, revision_id, tenant_id, scope, effective_from"
                    ") VALUES (:rid, :rev, :tid, 'global', :efrom)"
                ),
                {
                    "rid": uuid.uuid4(),
                    "rev": seed.revision_id,
                    "tid": other.tenant_id,
                    "efrom": ARC_NOW - datetime.timedelta(days=1),
                },
            )


@pytest.mark.asyncio
async def test_a_null_child_tenant_is_still_accepted_and_this_is_deliberate(
    factory: async_sessionmaker[AsyncSession], seed: ArcSeed
) -> None:
    """The documented residue, pinned so it is a known limit rather than a
    surprise.

    `MATCH SIMPLE` satisfies a composite foreign key whenever any referencing
    column is NULL, so a NULL child tenant under a tenant-owned revision is
    storable. `MATCH FULL` would reject it but would also forbid global
    children, since `revision_id` is NOT NULL and MATCH FULL requires all
    referencing columns to be NULL together.

    Harmless for the boundary this protects: visibility comes from the
    revision's tenant, so a NULL child cannot widen anything. If that ever
    stops being true, this test is the place that says so.
    """
    await _insert_directive(factory, seed, tenant_id=None)
