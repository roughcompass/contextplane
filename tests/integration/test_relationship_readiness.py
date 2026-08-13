"""Required relationships gate readiness, and a draft is allowed to sit below them.

Minimum cardinality is the one relationship rule that cannot be a write-time
refusal. An entity whose type requires an owner cannot be assembled at all if the
first write is rejected for the owner not being there yet — the edge and the
entity would each be waiting for the other. So the minimum is checked as a count
and recorded as a state, and only a transition to ready is refused while
something required is missing.

That makes two things worth proving separately, because an implementation can get
one right and the other wrong without either showing up in the other's tests:

- the *state* an assertion records, which must reflect the window as it stood
  when the row was written, not as it stands when somebody reads it back;
- the *count* that state came from, which has to be taken over the same window
  the maximum counts, under the same lock, or the two rules disagree about what
  "in force" means and an entity can be simultaneously over its maximum and
  under its minimum.

The stored-versus-derived question is the one this file exists to pin. A
`readiness_state` recomputed on read answers with today's rules about a row
asserted under older ones — which is precisely the difference a governance audit
is looking for, so the value has to survive a later change unchanged.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.profile.compiler import compile_profile
from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.profile.schemas.relationship import RelationshipTypeDefinition
from contextplane.relationships import definitions as relationship_definitions
from contextplane.relationships import queries, readiness
from contextplane.relationships.service import Endpoint, RelationshipWriteService

_NS = "northwind"
_WAREHOUSE = f"{_NS}:warehouse"
_DEPOT = f"{_NS}:depot"
_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)
_LATER = _NOW + datetime.timedelta(hours=1)


class _TableResolver:
    """Resolves endpoints from the rows these tests actually wrote."""

    async def resolve(self, session: AsyncSession, *, tenant_id: uuid.UUID, entity_id: uuid.UUID) -> Endpoint | None:
        row = (
            await session.execute(
                text("SELECT entity_id, entity_type, tenant_id FROM entities WHERE entity_id = :eid"),
                {"eid": entity_id},
            )
        ).first()
        return None if row is None else Endpoint(entity_id=row[0], entity_type=row[1], tenant_id=row[2])


def _relationship(**overrides: object) -> RelationshipTypeDefinition:
    fields: dict[str, object] = {
        "namespace": _NS,
        "type_name": "supplies",
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


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


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


async def _setup(
    factory: async_sessionmaker[AsyncSession], definition: RelationshipTypeDefinition
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """A bound tenant, one source, and three depots to attach to it."""
    document = compile_profile(
        entities=[
            EntityTypeDefinition(namespace=_NS, type_name="warehouse"),
            EntityTypeDefinition(namespace=_NS, type_name="depot"),
        ],
        relationships=[definition],
        interfaces=[],
    ).document
    tenant_id, revision_id = uuid.uuid4(), uuid.uuid4()

    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'ready')"),
            {"t": tenant_id, "s": f"rd-{tenant_id.hex[:10]}"},
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
                "name": f"rd-{revision_id.hex[:12]}",
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
                ") VALUES (:bid, :tid, :rid, :digest, 'active', :now, 'test', 'test', :now)"
            ),
            {
                "bid": uuid.uuid4(),
                "tid": tenant_id,
                "rid": revision_id,
                "digest": revision_id.hex,
                "now": _NOW,
            },
        )
        source = await _entity(session, tenant_id, _WAREHOUSE)
        depots = [await _entity(session, tenant_id, _DEPOT) for _ in range(3)]

    return tenant_id, source, depots


async def _assert_edge(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    source: uuid.UUID,
    destination: uuid.UUID,
) -> str:
    async with factory() as session, session.begin():
        result = await RelationshipWriteService(endpoints=_TableResolver()).assert_relationship(
            session,
            tenant_id=tenant_id,
            actor_id=None,
            relationship_type=f"{_NS}:supplies",
            source_entity_id=source,
            destination_entity_id=destination,
            now=_NOW,
        )
    return result.readiness_state


# --- what state an assertion records ------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconstrained_relationship_is_ready_immediately(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A minimum of zero has nothing outstanding, so calling it a draft would be wrong.

    Every unconstrained relationship in the graph would otherwise read as
    unfinished forever, which makes the state useless for finding the ones that
    genuinely are.
    """
    tenant_id, source, depots = await _setup(factory, _relationship(min_cardinality=0))

    assert await _assert_edge(factory, tenant_id, source, depots[0]) == readiness.READY


@pytest.mark.asyncio
async def test_a_type_requiring_one_is_ready_on_its_first_edge(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The count includes the assertion being written, or nothing ever satisfies a minimum of one."""
    tenant_id, source, depots = await _setup(factory, _relationship(min_cardinality=1))

    assert await _assert_edge(factory, tenant_id, source, depots[0]) == readiness.READY


@pytest.mark.asyncio
async def test_a_draft_may_exist_below_the_minimum_rather_than_being_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The write succeeds and records `draft`; it is not rejected.

    This is the whole reason the minimum is a readiness rule. A refusal here would
    make an entity requiring two relationships impossible to build, because the
    first write would always be the one that had only one.
    """
    tenant_id, source, depots = await _setup(factory, _relationship(min_cardinality=2))

    state = await _assert_edge(factory, tenant_id, source, depots[0])

    assert state == readiness.DRAFT
    async with factory() as session:
        stored = await queries.outgoing(session, tenant_id=tenant_id, source_entity_id=source, at=_NOW)
    assert len(stored) == 1, "the below-minimum assertion must exist, not have been refused"


@pytest.mark.asyncio
async def test_reaching_the_minimum_makes_the_next_assertion_ready(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, source, depots = await _setup(factory, _relationship(min_cardinality=2))

    first = await _assert_edge(factory, tenant_id, source, depots[0])
    second = await _assert_edge(factory, tenant_id, source, depots[1])

    assert first == readiness.DRAFT
    assert second == readiness.READY


@pytest.mark.asyncio
async def test_an_earlier_draft_keeps_its_state_when_the_minimum_is_later_met(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The state is what the window looked like at write time, and stays that way.

    This is the test that distinguishes a stored state from a derived one. A
    `readiness_state` recomputed on read would flip the first row to `ready` the
    moment the second landed, and an audit asking "was this assertion complete
    when it was made?" would get today's answer to a question about the past.
    """
    tenant_id, source, depots = await _setup(factory, _relationship(min_cardinality=2))

    await _assert_edge(factory, tenant_id, source, depots[0])
    await _assert_edge(factory, tenant_id, source, depots[1])

    async with factory() as session:
        stored = await queries.outgoing(session, tenant_id=tenant_id, source_entity_id=source, at=_NOW)

    by_destination = {row.destination_entity_id: row.readiness_state for row in stored}
    assert by_destination[depots[0]] == readiness.DRAFT
    assert by_destination[depots[1]] == readiness.READY


@pytest.mark.asyncio
async def test_a_draft_blocks_activation_and_a_ready_assertion_does_not(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The activation rule reads the stored state through one function, not an inline set."""
    assert readiness.blocks_activation(readiness.DRAFT)
    assert readiness.blocks_activation(readiness.BLOCKED)
    assert not readiness.blocks_activation(readiness.READY)


@pytest.mark.asyncio
async def test_an_unknown_readiness_state_is_refused_rather_than_treated_as_ready(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A state nobody recognises must not fall through to "does not block"."""
    with pytest.raises(ValueError, match="unknown readiness state"):
        readiness.blocks_activation("probably_fine")


# --- the count the state came from --------------------------------------------------


@pytest.mark.asyncio
async def test_the_count_is_taken_over_the_scope_the_definition_names(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`per_source` counts edges leaving one source, not every edge of the type.

    Two sources each with one edge: a count that ignored the scope would see two
    and call a minimum of two satisfied, which would mark an entity ready on the
    strength of a relationship belonging to something else.
    """
    tenant_id, source, depots = await _setup(factory, _relationship(min_cardinality=2))
    async with factory() as session, session.begin():
        other_source = await _entity(session, tenant_id, _WAREHOUSE)

    await _assert_edge(factory, tenant_id, source, depots[0])
    state = await _assert_edge(factory, tenant_id, other_source, depots[1])

    assert state == readiness.DRAFT, "the second source has one edge of its own, not two"


@pytest.mark.asyncio
async def test_the_count_excludes_an_assertion_that_has_already_ended(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "In force" is a half-open interval, so a row ending at the instant is over.

    This is what lets one assertion end and its replacement begin at the same
    moment without the pair momentarily counting as two.
    """
    tenant_id, source, depots = await _setup(factory, _relationship(min_cardinality=1))
    await _assert_edge(factory, tenant_id, source, depots[0])

    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE relationship_metadata SET effective_to = :end WHERE tenant_id = :t"),
            {"end": _LATER, "t": tenant_id},
        )

    async with factory() as session:
        before_end = await readiness.count_in_force(
            session,
            tenant_id=tenant_id,
            relationship_type=f"{_NS}:supplies",
            cardinality_scope="per_source",
            source_entity_id=source,
            destination_entity_id=depots[0],
            at=_NOW,
        )
        at_end = await readiness.count_in_force(
            session,
            tenant_id=tenant_id,
            relationship_type=f"{_NS}:supplies",
            cardinality_scope="per_source",
            source_entity_id=source,
            destination_entity_id=depots[0],
            at=_LATER,
        )

    assert before_end == 1
    assert at_end == 0


@pytest.mark.asyncio
async def test_a_scope_the_vocabulary_does_not_know_is_refused_not_defaulted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Defaulting to `per_source` would enforce a window the profile never stated."""
    with pytest.raises(readiness.UnknownCardinalityScope):
        readiness.scope_predicate("per_tenant")


@pytest.mark.asyncio
async def test_each_scope_binds_only_the_columns_it_counts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A predicate and a parameter set that disagree must not silently count a wider window."""
    source, destination = uuid.uuid4(), uuid.uuid4()

    for scope, expected in (
        ("per_source", {"source_entity_id"}),
        ("per_destination", {"destination_entity_id"}),
        ("per_pair", {"source_entity_id", "destination_entity_id"}),
    ):
        parameters = readiness.scope_parameters(scope, source_entity_id=source, destination_entity_id=destination)
        assert set(parameters) == expected
        for column in expected:
            assert f":{column}" in readiness.scope_predicate(scope)


@pytest.mark.asyncio
async def test_the_inverse_view_reports_the_stored_readiness_unchanged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reading an assertion backwards must not change what it says about itself.

    An inverse is the same row seen from the other end, so a caller reading
    "what supplies me" has to see the same readiness the forward reader sees —
    otherwise one stored fact carries two governance answers.
    """
    tenant_id, source, depots = await _setup(factory, _relationship(min_cardinality=2))
    await _assert_edge(factory, tenant_id, source, depots[0])

    async with factory() as session:
        forward = await queries.outgoing(session, tenant_id=tenant_id, source_entity_id=source, at=_NOW)
        backward = await queries.incoming(session, tenant_id=tenant_id, destination_entity_id=depots[0], at=_NOW)

    assert forward[0].readiness_state == backward[0].readiness_state == readiness.DRAFT
    assert not forward[0].is_inverse
    assert backward[0].is_inverse
    assert backward[0].source_entity_id == depots[0]
    assert backward[0].destination_entity_id == source
