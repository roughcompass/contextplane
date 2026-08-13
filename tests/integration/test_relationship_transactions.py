"""A governed relationship write admits only valid winners, including under race.

Every rule here has a version that passes without concurrency and fails with it,
which is why the fixtures at the bottom of this file matter more than the ones at
the top. An unlocked count-then-write satisfies every single-writer test: count
three under a maximum of four, insert, read back four. It is only when two
transactions count the same three that the missing lock shows itself, and by then
the invalid rows are already in the graph.

So the concurrency tests are written to fail against exactly that implementation.
Each starts two real transactions on two real connections, drives them to the
point where both have validated, and only then lets either commit. Against the
shipped write path one wins and one is refused; against a version with the
advisory lock removed, both would land — which is asserted by counting rows, not
by trusting that no exception surfaced.

The four writes a governed assertion makes -- the edge, its provenance, its
governed row and its closure-outbox entry, plus the audit row -- are checked to
commit together and to vanish together, because a graph holding a relationship
its audit trail never recorded is worse than one holding neither.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.profile.compiler import compile_profile
from contextplane.profile.schemas.common import PropertyDefinition
from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.profile.schemas.relationship import RelationshipTypeDefinition
from contextplane.relationships import definitions as relationship_definitions
from contextplane.relationships import queries
from contextplane.relationships.service import (
    CROSS_ORG_DENIED,
    DUPLICATE_REFUSED,
    ENDPOINT_MISSING,
    ENDPOINT_TYPE_MISMATCH,
    INVERSE_NOT_WRITABLE,
    MAXIMUM_EXCEEDED,
    NO_ACTIVE_BINDING,
    UNDECLARED_PROPERTY,
    UNKNOWN_TYPE,
    Endpoint,
    RelationshipWriteRefused,
    RelationshipWriteService,
)

_NS = "northwind"
_WAREHOUSE = f"{_NS}:warehouse"
_DEPOT = f"{_NS}:depot"
_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


class _TableResolver:
    """Resolves endpoints by reading `entities` directly.

    A test double for the port the composition root fills with a
    visibility-backed implementation. It reads the table this test wrote to, so
    the write path under test is exercised with real endpoint facts rather than
    with a canned answer that could agree with a broken check by accident.
    """

    async def resolve(self, session: AsyncSession, *, tenant_id: uuid.UUID, entity_id: uuid.UUID) -> Endpoint | None:
        row = (
            await session.execute(
                text("SELECT entity_id, entity_type, tenant_id FROM entities WHERE entity_id = :eid"),
                {"eid": entity_id},
            )
        ).first()
        if row is None:
            return None
        return Endpoint(entity_id=row[0], entity_type=row[1], tenant_id=row[2])


class _AllowCrossOrg:
    """Grants every crossing, so the grant-backed branch has a way to be exercised."""

    async def permits(self, session: AsyncSession, **_: object) -> bool:
        return True


def _relationship(
    type_name: str = "supplies",
    **overrides: object,
) -> RelationshipTypeDefinition:
    fields: dict[str, object] = {
        "namespace": _NS,
        "type_name": type_name,
        "source_type": _WAREHOUSE,
        "destination_type": _DEPOT,
        "direction": "directed",
        "cardinality_scope": "per_source",
        "authority": "canonical_owner",
        "cross_org_policy": "deny",
        "min_cardinality": 0,
        "max_cardinality": None,
        "duplicate_policy": "reject",
        "symmetry": "asymmetric",
        "inverse_view": "read_only",
    }
    fields.update(overrides)
    return RelationshipTypeDefinition(**fields)  # type: ignore[arg-type]


def _document(*relationships: RelationshipTypeDefinition) -> str:
    return compile_profile(
        entities=[
            EntityTypeDefinition(namespace=_NS, type_name="warehouse"),
            EntityTypeDefinition(namespace=_NS, type_name="depot"),
        ],
        relationships=list(relationships),
        interfaces=[],
    ).document


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _Fixture:
    """One tenant, bound to a profile, with projected definitions and two entities."""

    def __init__(self, tenant_id: uuid.UUID, source: uuid.UUID, destination: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.source = source
        self.destination = destination


async def _setup(
    factory: async_sessionmaker[AsyncSession],
    *relationships: RelationshipTypeDefinition,
    bind_state: str = "active",
    depots: int = 1,
) -> tuple[_Fixture, list[uuid.UUID]]:
    """Publish, project, bind, and create the entities the assertions will join."""
    document = _document(*(relationships or (_relationship(),)))
    tenant_id = uuid.uuid4()
    revision_id = uuid.uuid4()

    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'rel')"),
            {"t": tenant_id, "s": f"rt-{tenant_id.hex[:10]}"},
        )
        await session.execute(
            text(
                "INSERT INTO profile_revisions ("
                "  profile_revision_id, profile_family, profile_name, semantic_version,"
                "  canonical_document, document_digest, compatibility, published_by, published_at"
                ") VALUES (:rid, 'platform', :name, '1.0.0', CAST(:doc AS JSONB), :digest,"
                "          'backward_compatible', 'test', :now)"
            ),
            {
                "rid": revision_id,
                "name": f"rel-{revision_id.hex[:12]}",
                "doc": document,
                "digest": revision_id.hex,
                "now": _NOW,
            },
        )
        await relationship_definitions.project_published_relationships(
            session, profile_revision_id=revision_id, document=document, compiled_at=_NOW
        )
        await session.execute(
            text(
                "INSERT INTO profile_bindings ("
                "  binding_id, tenant_id, profile_revision_id, extension_set_digest, state,"
                "  effective_from, actor, reason, recorded_at"
                ") VALUES (:bid, :tid, :rid, :digest, :state, :now, 'test', 'test', :now)"
            ),
            {
                "bid": uuid.uuid4(),
                "tid": tenant_id,
                "rid": revision_id,
                "digest": revision_id.hex,
                "state": bind_state,
                "now": _NOW,
            },
        )
        source = await _entity(session, tenant_id, _WAREHOUSE)
        destinations = [await _entity(session, tenant_id, _DEPOT) for _ in range(depots)]

    return _Fixture(tenant_id, source, destinations[0]), destinations


async def _entity(session: AsyncSession, tenant_id: uuid.UUID, entity_type: str) -> uuid.UUID:
    entity_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO entities (entity_id, tenant_id, entity_type, name, is_active, created_at)"
            " VALUES (:eid, :tid, :etype, :name, TRUE, :now)"
        ),
        {
            "eid": entity_id,
            "tid": tenant_id,
            "etype": entity_type,
            "name": f"e-{entity_id.hex[:10]}",
            "now": _NOW,
        },
    )
    return entity_id


def _service(**kwargs: object) -> RelationshipWriteService:
    return RelationshipWriteService(endpoints=_TableResolver(), **kwargs)  # type: ignore[arg-type]


async def _assert_one(
    factory: async_sessionmaker[AsyncSession],
    fixture: _Fixture,
    *,
    destination: uuid.UUID | None = None,
    relationship_type: str = f"{_NS}:supplies",
    service: RelationshipWriteService | None = None,
    **kwargs: object,
) -> object:
    async with factory() as session, session.begin():
        return await (service or _service()).assert_relationship(
            session,
            tenant_id=fixture.tenant_id,
            actor_id=None,
            relationship_type=relationship_type,
            source_entity_id=fixture.source,
            destination_entity_id=destination if destination is not None else fixture.destination,
            now=_NOW,
            **kwargs,  # type: ignore[arg-type]
        )


async def _count(factory: async_sessionmaker[AsyncSession], table: str, tenant_id: uuid.UUID) -> int:
    async with factory() as session:
        return (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()


# --- the happy path writes everything, once ----------------------------------------


@pytest.mark.asyncio
async def test_a_governed_assertion_writes_edge_provenance_row_outbox_and_audit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """All five rows, or the assertion is not governed and the graph is not consistent."""
    fixture, _ = await _setup(factory)

    result = await _assert_one(factory, fixture)

    assert await _count(factory, "edges", fixture.tenant_id) == 1
    assert await _count(factory, "assertion_provenance", fixture.tenant_id) == 1
    assert await _count(factory, "relationship_metadata", fixture.tenant_id) == 1
    assert await _count(factory, "closure_outbox", fixture.tenant_id) == 1
    assert await _count(factory, "audit_log", fixture.tenant_id) == 1
    assert result.readiness_state == "ready"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_refused_assertion_leaves_none_of_the_five_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A refusal after the edge would leave an ungoverned edge nothing owns.

    Counted per table rather than in aggregate: an implementation that wrote the
    edge and then failed on the governed row would leave exactly one orphan, and a
    single total would let it hide behind the others being zero.
    """
    fixture, _ = await _setup(factory, _relationship(max_cardinality=1))
    await _assert_one(factory, fixture)

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture, destination=await _extra_depot(factory, fixture))

    assert refused.value.code == MAXIMUM_EXCEEDED
    assert await _count(factory, "edges", fixture.tenant_id) == 1
    assert await _count(factory, "assertion_provenance", fixture.tenant_id) == 1
    assert await _count(factory, "relationship_metadata", fixture.tenant_id) == 1
    assert await _count(factory, "closure_outbox", fixture.tenant_id) == 1


async def _extra_depot(factory: async_sessionmaker[AsyncSession], fixture: _Fixture) -> uuid.UUID:
    async with factory() as session, session.begin():
        return await _entity(session, fixture.tenant_id, _DEPOT)


# --- row-local refusals -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_type_the_profile_never_declared_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _ = await _setup(factory)

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture, relationship_type=f"{_NS}:invented")

    assert refused.value.code == UNKNOWN_TYPE


@pytest.mark.asyncio
async def test_a_tenant_with_no_active_binding_cannot_assert(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A governed row must name the revision that validated it; validating is not active."""
    fixture, _ = await _setup(factory, bind_state="validating")

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture)

    assert refused.value.code == NO_ACTIVE_BINDING


@pytest.mark.asyncio
async def test_an_endpoint_that_does_not_resolve_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _ = await _setup(factory)

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture, destination=uuid.uuid4())

    assert refused.value.code == ENDPOINT_MISSING


@pytest.mark.asyncio
async def test_an_endpoint_of_the_wrong_type_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The definition says which types it joins; a warehouse is not a depot."""
    fixture, _ = await _setup(factory)
    async with factory() as session, session.begin():
        wrong = await _entity(session, fixture.tenant_id, _WAREHOUSE)

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture, destination=wrong)

    assert refused.value.code == ENDPOINT_TYPE_MISMATCH


@pytest.mark.asyncio
async def test_a_property_the_definition_does_not_declare_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _ = await _setup(factory)

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture, properties={"surprise": "x"})

    assert refused.value.code == UNDECLARED_PROPERTY


@pytest.mark.asyncio
async def test_a_declared_property_is_stored_on_the_governed_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The refusal above proves nothing unless a declared property still lands."""
    fixture, _ = await _setup(
        factory,
        _relationship(properties=(PropertyDefinition(name="lead_time_days", value_type="integer"),)),
    )

    await _assert_one(factory, fixture, properties={"lead_time_days": 3})

    async with factory() as session:
        stored = await queries.outgoing(session, tenant_id=fixture.tenant_id, source_entity_id=fixture.source, at=_NOW)
    assert stored[0].properties == {"lead_time_days": 3}


@pytest.mark.asyncio
async def test_a_duplicate_is_refused_when_the_type_rejects_duplicates(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _ = await _setup(factory)
    await _assert_one(factory, fixture)

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture)

    assert refused.value.code == DUPLICATE_REFUSED


@pytest.mark.asyncio
async def test_the_mirror_of_a_read_only_inverse_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Storing the reverse of a derived view would hold one fact twice.

    Uses a type whose endpoints are the same, because for a type joining two
    different types the endpoint check already refuses the mirror — this is the
    case that check cannot see.
    """
    fixture, _ = await _setup(
        factory,
        _relationship("peers", source_type=_WAREHOUSE, destination_type=_WAREHOUSE),
    )
    async with factory() as session, session.begin():
        other = await _entity(session, fixture.tenant_id, _WAREHOUSE)

    await _assert_one(factory, fixture, destination=other, relationship_type=f"{_NS}:peers")

    async with factory() as session, session.begin():
        with pytest.raises(RelationshipWriteRefused) as refused:
            await _service().assert_relationship(
                session,
                tenant_id=fixture.tenant_id,
                actor_id=None,
                relationship_type=f"{_NS}:peers",
                source_entity_id=other,
                destination_entity_id=fixture.source,
                now=_NOW,
            )

    assert refused.value.code == INVERSE_NOT_WRITABLE


@pytest.mark.asyncio
async def test_a_cross_organization_edge_is_denied_without_a_grant(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An absent grant system denies rather than permits."""
    fixture, _ = await _setup(factory, _relationship(cross_org_policy="allow_with_grant"))
    foreign = await _foreign_depot(factory)

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture, destination=foreign)

    assert refused.value.code == CROSS_ORG_DENIED


@pytest.mark.asyncio
async def test_a_cross_organization_edge_is_denied_outright_when_the_type_denies(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A granting policy cannot rescue a type whose own policy is `deny`."""
    fixture, _ = await _setup(factory)
    foreign = await _foreign_depot(factory)

    with pytest.raises(RelationshipWriteRefused) as refused:
        await _assert_one(factory, fixture, destination=foreign, service=_service(cross_org=_AllowCrossOrg()))

    assert refused.value.code == CROSS_ORG_DENIED


@pytest.mark.asyncio
async def test_a_granted_cross_organization_edge_is_written(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The denials above would also pass for a path that refuses every crossing."""
    fixture, _ = await _setup(factory, _relationship(cross_org_policy="allow_with_grant"))
    foreign = await _foreign_depot(factory)

    await _assert_one(factory, fixture, destination=foreign, service=_service(cross_org=_AllowCrossOrg()))

    assert await _count(factory, "relationship_metadata", fixture.tenant_id) == 1


async def _foreign_depot(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    other_tenant = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'other')"),
            {"t": other_tenant, "s": f"ot-{other_tenant.hex[:10]}"},
        )
        return await _entity(session, other_tenant, _DEPOT)


# --- the races ----------------------------------------------------------------------


async def _racing_assert(
    factory: async_sessionmaker[AsyncSession],
    fixture: _Fixture,
    destination: uuid.UUID,
    ready: asyncio.Event,
    release: asyncio.Event,
) -> str:
    """Validate and write on one connection, then wait before committing.

    The wait is what turns two sequential writes into a race. Without it each
    transaction commits before the other begins, and an implementation with no
    lock at all passes — which is the implementation these tests exist to reject.
    """
    async with factory() as session:
        await session.begin()
        try:
            await _service().assert_relationship(
                session,
                tenant_id=fixture.tenant_id,
                actor_id=None,
                relationship_type=f"{_NS}:supplies",
                source_entity_id=fixture.source,
                destination_entity_id=destination,
                now=_NOW,
            )
        except RelationshipWriteRefused as refused:
            await session.rollback()
            ready.set()
            return refused.code
        except Exception:
            await session.rollback()
            ready.set()
            return "database_refused"
        ready.set()
        await release.wait()
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            return "database_refused"
        return "written"


async def _hold_and_race(
    factory: async_sessionmaker[AsyncSession],
    fixture: _Fixture,
    *,
    first_destination: uuid.UUID,
    second_destination: uuid.UUID,
) -> list[str]:
    """Drive a real race and assert the second writer was actually serialised.

    The order matters, and getting it wrong makes the test worthless. An earlier
    version started the second writer and released the first immediately; the
    second never reached its own validation until the first had committed, so the
    two ran in sequence and the fixture passed with the advisory lock deleted.

    This version keeps the first transaction open and gives the second nothing to
    wait for but the lock. If the second finishes while the first still holds, it
    was never serialised — that is asserted here rather than inferred from the row
    count, because a row count alone cannot tell "the lock worked" from "the two
    happened to run one after the other".
    """
    first_ready, second_ready = asyncio.Event(), asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    release_second.set()

    first = asyncio.create_task(_racing_assert(factory, fixture, first_destination, first_ready, release_first))
    await asyncio.wait_for(first_ready.wait(), timeout=30)

    second = asyncio.create_task(_racing_assert(factory, fixture, second_destination, second_ready, release_second))
    finished, _pending = await asyncio.wait({second}, timeout=3.0)
    assert not finished, (
        "the second writer completed while the first still held its transaction open; "
        "the aggregate scope was not locked, so both counted the same window"
    )

    release_first.set()
    return sorted(await asyncio.wait_for(asyncio.gather(first, second), timeout=30))


@pytest.mark.asyncio
async def test_two_concurrent_writers_cannot_both_exceed_the_maximum(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The count and the insert must happen under one hold, or both writers win.

    A maximum of one with nothing yet in force: each transaction, counting alone,
    sees zero and believes it may write. The two target *different* destinations,
    so no unique or exclusion constraint can save the implementation — the lock is
    the only thing standing between one valid row and two invalid ones.
    """
    fixture, _ = await _setup(factory, _relationship(max_cardinality=1), depots=2)
    second_destination = await _extra_depot(factory, fixture)

    outcomes = await _hold_and_race(
        factory,
        fixture,
        first_destination=fixture.destination,
        second_destination=second_destination,
    )

    written = await _count(factory, "relationship_metadata", fixture.tenant_id)
    assert written == 1, f"maximum of 1 admitted {written} assertions; outcomes were {outcomes}"
    assert outcomes.count("written") == 1
    assert MAXIMUM_EXCEEDED in outcomes


@pytest.mark.asyncio
async def test_two_concurrent_writers_of_one_pair_admit_only_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The duplicate check and the temporal exclusion must agree on the winner.

    Both transactions target the same ordered pair. The loser is refused either by
    the application check, having re-read the winner's committed row under the
    lock, or by the exclusion constraint; which of the two is not the point. What
    matters is that exactly one row exists and the loser learned it lost.

    **This one does not prove the lock, and is not meant to.** Two writers of one
    ordered pair are serialised by the exclusion constraint whether or not the
    advisory lock exists — the second writer's insert blocks on the first's
    uncommitted row. Deleting the lock leaves this test green. The maximum test
    above is the one that discriminates, because its two writers target different
    destinations and no constraint covers them; that was verified by removing the
    lock and watching exactly that test, and only that test, go red.
    """
    fixture, _ = await _setup(factory)

    outcomes = await _hold_and_race(
        factory,
        fixture,
        first_destination=fixture.destination,
        second_destination=fixture.destination,
    )

    written = await _count(factory, "relationship_metadata", fixture.tenant_id)
    assert written == 1, f"one ordered pair admitted {written} assertions; outcomes were {outcomes}"
    assert outcomes.count("written") == 1
    assert DUPLICATE_REFUSED in outcomes or "database_refused" in outcomes


@pytest.mark.asyncio
async def test_concurrent_writers_in_different_scopes_do_not_block_each_other(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The lock is per scope key, so unrelated sources must both succeed.

    Without this, a lock taken too broadly — on the whole table, or on the binding
    alone — would pass every test above while serialising every relationship write
    in the deployment. The two writers here share a binding and a type and differ
    only in source, which is what the key's third component is for.
    """
    fixture, _ = await _setup(factory)
    async with factory() as session, session.begin():
        other_source = await _entity(session, fixture.tenant_id, _WAREHOUSE)
        other_destination = await _entity(session, fixture.tenant_id, _DEPOT)

    async def write(source: uuid.UUID, destination: uuid.UUID) -> None:
        async with factory() as session, session.begin():
            await _service().assert_relationship(
                session,
                tenant_id=fixture.tenant_id,
                actor_id=None,
                relationship_type=f"{_NS}:supplies",
                source_entity_id=source,
                destination_entity_id=destination,
                now=_NOW,
            )

    await asyncio.wait_for(
        asyncio.gather(
            write(fixture.source, fixture.destination),
            write(other_source, other_destination),
        ),
        timeout=30,
    )

    assert await _count(factory, "relationship_metadata", fixture.tenant_id) == 2
