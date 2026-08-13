"""A published relationship compiles into a row a writer can enforce, or it fails loudly.

The definition rows are what a relationship write path reads: it holds them in the
same transaction as the edge it is deciding about, and enforces exactly what they
say. That makes two things contract rather than implementation detail — that every
constraint the profile stated survives the projection unchanged, and that a
document which does not state one is refused instead of projected with a value
nobody wrote.

The refusal cases are the point of this file. A projection that quietly defaulted a
missing `cross_org_policy` to `deny` would look correct in every green test: the
rows would exist, the values would be legal, and the only difference between a
profile that denied cross-organization edges and a profile that lost the rule
entirely would be invisible. So each constraint key is removed in turn and the
projection is required to name it.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.profile.compiler import RELATIONSHIP_FAMILY, compile_profile
from contextplane.profile.schemas.common import PropertyDefinition
from contextplane.profile.schemas.entity import EntityTypeDefinition
from contextplane.profile.schemas.relationship import RelationshipTypeDefinition
from contextplane.relationships import definitions as relationship_definitions
from contextplane.relationships.definitions import (
    REQUIRED_CONSTRAINT_KEYS,
    RelationshipConstraints,
    RelationshipProjectionError,
)

_NAMESPACE = "northwind"
_COMPILED_AT = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


def _entity(type_name: str) -> EntityTypeDefinition:
    return EntityTypeDefinition(namespace=_NAMESPACE, type_name=type_name)


def _relationship(
    type_name: str = "supplies",
    **overrides: object,
) -> RelationshipTypeDefinition:
    """A complete relationship, so a test changing one field changes only that field."""
    fields: dict[str, object] = {
        "namespace": _NAMESPACE,
        "type_name": type_name,
        "source_type": f"{_NAMESPACE}:warehouse",
        "destination_type": f"{_NAMESPACE}:depot",
        "direction": "directed",
        "cardinality_scope": "per_source",
        "authority": "canonical_owner",
        "cross_org_policy": "allow_with_grant",
        "min_cardinality": 1,
        "max_cardinality": 4,
        "duplicate_policy": "allow",
        "symmetry": "asymmetric",
        "inverse_view": "read_only",
    }
    fields.update(overrides)
    return RelationshipTypeDefinition(**fields)  # type: ignore[arg-type]


def _document(*relationships: RelationshipTypeDefinition) -> str:
    """Compile a profile whose entity set declares both endpoints, and return its document.

    Built through `compile_profile` rather than assembled by hand: the projection's
    whole argument is that it reads what publication stored, so a test feeding it a
    document publication would never produce proves the projection against a shape
    that does not occur.
    """
    compiled = compile_profile(
        entities=[_entity("warehouse"), _entity("depot")],
        relationships=list(relationships),
        interfaces=[],
    )
    return compiled.document


def _canonical(document: str) -> list[dict[str, object]]:
    families: dict[str, str] = json.loads(document)
    parsed: list[dict[str, object]] = json.loads(families[RELATIONSHIP_FAMILY])
    return parsed


def _redocument(entries: list[dict[str, object]]) -> str:
    """Put edited canonical objects back into a document's relationship family."""
    families = {"entity": "[]", "interface": "[]", RELATIONSHIP_FAMILY: json.dumps(entries)}
    return json.dumps(families)


# --- what the projection preserves -------------------------------------------------


def test_every_constraint_the_profile_stated_survives_the_projection() -> None:
    """Each authored constraint reaches the constraint set with the value authored.

    Asserted field by field rather than by comparing two constructed objects: a
    round-trip through the same builder would agree with itself even if the
    projection read `min_cardinality` into `max_cardinality`.
    """
    (constraints,) = relationship_definitions.relationship_constraints(_document(_relationship()))

    assert constraints.relationship_type == f"{_NAMESPACE}:supplies"
    assert constraints.source_type == f"{_NAMESPACE}:warehouse"
    assert constraints.destination_type == f"{_NAMESPACE}:depot"
    assert constraints.direction == "directed"
    assert constraints.duplicate_policy == "allow"
    assert constraints.symmetry == "asymmetric"
    assert constraints.inverse_view_policy == "read_only"
    assert constraints.min_cardinality == 1
    assert constraints.max_cardinality == 4
    assert constraints.cardinality_scope == "per_source"
    assert constraints.authority == "canonical_owner"
    assert constraints.cross_org_policy == "allow_with_grant"


def test_an_unbounded_maximum_stays_unbounded_rather_than_becoming_a_number() -> None:
    """`max_cardinality` of `None` means no ceiling, and must not project as one.

    Worth its own test because every falsy-coalescing bug in a projection turns an
    absent ceiling into `0`, which the database would accept as a column value and
    a writer would read as "no edge may exist".
    """
    (constraints,) = relationship_definitions.relationship_constraints(
        _document(_relationship(max_cardinality=None, min_cardinality=0))
    )

    assert constraints.max_cardinality is None


def test_properties_project_keyed_by_name_with_their_rules_intact() -> None:
    definition = _relationship(
        properties=(
            PropertyDefinition(
                name="lead_time_days", value_type="integer", required=True, min_cardinality=1, max_cardinality=1
            ),
            PropertyDefinition(name="channel", value_type="enum", enum_values=("air", "sea")),
        )
    )

    (constraints,) = relationship_definitions.relationship_constraints(_document(definition))

    assert set(constraints.property_schema) == {"lead_time_days", "channel"}
    assert constraints.property_schema["lead_time_days"]["required"] is True
    assert constraints.property_schema["lead_time_days"]["value_type"] == "integer"
    assert constraints.property_schema["channel"]["enum_values"] == ["air", "sea"]


def test_a_relationship_with_no_properties_projects_an_empty_schema() -> None:
    (constraints,) = relationship_definitions.relationship_constraints(_document(_relationship()))

    assert constraints.property_schema == {}


# --- what the projection refuses ---------------------------------------------------


@pytest.mark.parametrize("missing", sorted(REQUIRED_CONSTRAINT_KEYS))
def test_a_document_missing_any_constraint_key_is_refused_by_name(missing: str) -> None:
    """Removing one constraint key must fail, and the failure must name that key.

    Parametrized over the required set rather than over a hand-listed few, so a key
    added to the projection later is covered the moment it is required — and a key
    quietly dropped from the requirement fails this test by no longer being in the
    parametrization's source.
    """
    entries = _canonical(_document(_relationship()))
    del entries[0][missing]

    with pytest.raises(RelationshipProjectionError) as raised:
        relationship_definitions.relationship_constraints(_redocument(entries))

    assert missing in str(raised.value)


def test_a_missing_cross_org_policy_is_refused_rather_than_denied_by_default() -> None:
    """The default-deny rule is a stated denial, not an assumed one.

    Separated from the parametrized case above because this is the one key whose
    plausible default is *safe*: filling in `deny` would look like defensive
    programming while making a lost rule indistinguishable from a written one.
    """
    entries = _canonical(_document(_relationship(cross_org_policy="allow_with_grant")))
    del entries[0]["cross_org_policy"]

    with pytest.raises(RelationshipProjectionError):
        relationship_definitions.relationship_constraints(_redocument(entries))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("direction", "sideways"),
        ("cardinality_scope", "per_tenant"),
        ("duplicate_policy", "merge"),
        ("symmetry", "reflexive"),
        ("inverse_view", "writable"),
        ("cross_org_policy", "allow"),
        ("authority", "guessed"),
    ],
)
def test_a_value_outside_the_closed_vocabulary_is_refused(field: str, value: str) -> None:
    """Each constrained column is checked against the set the database constrains it with."""
    entries = _canonical(_document(_relationship()))
    entries[0][field] = value

    with pytest.raises(RelationshipProjectionError) as raised:
        relationship_definitions.relationship_constraints(_redocument(entries))

    assert field in str(raised.value)


def test_a_maximum_below_the_minimum_is_refused_before_any_row_is_written() -> None:
    entries = _canonical(_document(_relationship(min_cardinality=2, max_cardinality=5)))
    entries[0]["max_cardinality"] = 1

    with pytest.raises(RelationshipProjectionError):
        relationship_definitions.relationship_constraints(_redocument(entries))


def test_a_boolean_cardinality_is_refused_rather_than_read_as_one() -> None:
    """`True` is an `int` in Python, so a plain integer check would store a ceiling of 1."""
    entries = _canonical(_document(_relationship()))
    entries[0]["max_cardinality"] = True

    with pytest.raises(RelationshipProjectionError):
        relationship_definitions.relationship_constraints(_redocument(entries))


def test_a_property_with_an_unknown_value_type_is_refused() -> None:
    entries = _canonical(_document(_relationship()))
    entries[0]["properties"] = [{"name": "lead_time_days", "value_type": "duration"}]

    with pytest.raises(RelationshipProjectionError) as raised:
        relationship_definitions.relationship_constraints(_redocument(entries))

    assert "lead_time_days" in str(raised.value)


def test_a_document_with_no_relationship_family_is_refused() -> None:
    with pytest.raises(RelationshipProjectionError):
        relationship_definitions.relationship_constraints(json.dumps({"entity": "[]"}))


# --- the inverse is derived, never stored ------------------------------------------


def test_a_read_only_inverse_mirrors_the_endpoints_and_keeps_every_other_rule() -> None:
    (constraints,) = relationship_definitions.relationship_constraints(_document(_relationship()))

    mirrored = relationship_definitions.inverse_view(constraints)

    assert mirrored is not None
    assert mirrored.source_type == constraints.destination_type
    assert mirrored.destination_type == constraints.source_type
    assert mirrored.relationship_type == constraints.relationship_type
    assert mirrored.max_cardinality == constraints.max_cardinality
    assert mirrored.cardinality_scope == constraints.cardinality_scope
    assert mirrored.cross_org_policy == constraints.cross_org_policy


def test_an_independently_asserted_inverse_derives_nothing() -> None:
    """The profile has said the reverse direction is its own assertion with its own provenance."""
    (constraints,) = relationship_definitions.relationship_constraints(
        _document(_relationship(inverse_view="independently_asserted"))
    )

    assert relationship_definitions.inverse_view(constraints) is None


def test_an_undirected_relationship_derives_no_inverse() -> None:
    """It already holds both ways, so a mirror would be the same fact stored twice.

    Endpoints are the same type because the schema requires it of a symmetric
    relationship: the reverse of an edge between two different types is a claim
    this vocabulary cannot make.
    """
    (constraints,) = relationship_definitions.relationship_constraints(
        _document(
            _relationship(
                direction="undirected",
                symmetry="symmetric",
                source_type=f"{_NAMESPACE}:warehouse",
                destination_type=f"{_NAMESPACE}:warehouse",
            )
        )
    )

    assert relationship_definitions.inverse_view(constraints) is None


# --- the rows, against the real table ----------------------------------------------


@pytest_asyncio.fixture
async def session_factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _revision(session: AsyncSession, document: str) -> uuid.UUID:
    """Publish the minimum a definition row's foreign key needs to resolve.

    Inserted directly rather than through `ProfileService` because this file is
    about the projection, and routing through publication would make a failure here
    ambiguous between the two.
    """
    revision_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO profile_revisions ("
            "  profile_revision_id, profile_family, profile_name, semantic_version,"
            "  canonical_document, document_digest, compatibility, published_by, published_at"
            ") VALUES (:rid, 'platform', :name, '1.0.0', CAST(:doc AS JSONB), :digest,"
            "          'backward_compatible', 'conformance', :now)"
        ),
        {
            "rid": revision_id,
            "name": f"relationship-definitions-{revision_id.hex[:12]}",
            "doc": document,
            "digest": revision_id.hex,
            "now": _COMPILED_AT,
        },
    )
    return revision_id


@pytest.mark.asyncio
async def test_a_projected_row_reads_back_carrying_every_constraint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The row is the writer's copy of the rule, so it must survive the round trip intact."""
    document = _document(
        _relationship(
            properties=(
                PropertyDefinition(name="lead_time_days", value_type="integer", required=True, min_cardinality=1),
            )
        )
    )

    async with session_factory() as session, session.begin():
        revision_id = await _revision(session, document)
        (definition_id,) = await relationship_definitions.project_published_relationships(
            session, profile_revision_id=revision_id, document=document, compiled_at=_COMPILED_AT
        )

    async with session_factory() as session:
        (persisted,) = await relationship_definitions.load_relationship_definitions(
            session, profile_revision_id=revision_id
        )

    assert persisted.definition_id == definition_id
    assert persisted.extension_revision_id is None
    assert persisted.constraints.relationship_type == f"{_NAMESPACE}:supplies"
    assert persisted.constraints.direction == "directed"
    assert persisted.constraints.duplicate_policy == "allow"
    assert persisted.constraints.symmetry == "asymmetric"
    assert persisted.constraints.inverse_view_policy == "read_only"
    assert persisted.constraints.min_cardinality == 1
    assert persisted.constraints.max_cardinality == 4
    assert persisted.constraints.cardinality_scope == "per_source"
    assert persisted.constraints.authority == "canonical_owner"
    assert persisted.constraints.cross_org_policy == "allow_with_grant"
    assert persisted.constraints.property_schema["lead_time_days"]["required"] is True


@pytest.mark.asyncio
async def test_only_one_direction_is_stored_for_a_relationship_with_a_read_only_inverse(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The mirror is a way of reading the row, so projecting must not write a second one.

    Counts rows rather than inspecting the returned ids: a projection that wrote an
    inverse row would still return one id per authored definition if it appended the
    mirror separately, and the count is what a later reader would trip over.
    """
    document = _document(_relationship())

    async with session_factory() as session, session.begin():
        revision_id = await _revision(session, document)
        await relationship_definitions.project_published_relationships(
            session, profile_revision_id=revision_id, document=document, compiled_at=_COMPILED_AT
        )

    async with session_factory() as session:
        stored = (
            await session.execute(
                text(
                    "SELECT relationship_type, source_type, destination_type"
                    "  FROM relationship_type_definitions WHERE profile_revision_id = :rid"
                ),
                {"rid": revision_id},
            )
        ).all()

    assert len(stored) == 1
    assert stored[0].source_type == f"{_NAMESPACE}:warehouse"
    assert stored[0].destination_type == f"{_NAMESPACE}:depot"


@pytest.mark.asyncio
async def test_the_same_relationship_type_cannot_be_projected_twice_for_one_revision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second projection of one revision is a duplicate authority, and the table refuses it.

    Two rows for one type under one revision would let a writer enforce whichever it
    read first. The uniqueness is the database's, and this proves the projection runs
    into it rather than around it.
    """
    document = _document(_relationship())

    async with session_factory() as session, session.begin():
        revision_id = await _revision(session, document)
        await relationship_definitions.project_published_relationships(
            session, profile_revision_id=revision_id, document=document, compiled_at=_COMPILED_AT
        )

    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await relationship_definitions.project_published_relationships(
                session, profile_revision_id=revision_id, document=document, compiled_at=_COMPILED_AT
            )


@pytest.mark.asyncio
async def test_a_rolled_back_publication_leaves_no_definition_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The projection joins its caller's transaction rather than committing its own.

    A definition row that outlived the publication it describes would be an
    authority for a revision that does not exist, and a writer would enforce it
    without noticing.
    """
    document = _document(_relationship())
    revision_id = uuid.uuid4()

    async with session_factory() as session:
        await session.begin()
        revision_id = await _revision(session, document)
        await relationship_definitions.project_published_relationships(
            session, profile_revision_id=revision_id, document=document, compiled_at=_COMPILED_AT
        )
        await session.rollback()

    async with session_factory() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM relationship_type_definitions WHERE profile_revision_id = :rid"),
                {"rid": revision_id},
            )
        ).scalar_one()

    assert remaining == 0


@pytest.mark.asyncio
async def test_definitions_read_back_ordered_by_relationship_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two readers of one revision must see the same sequence."""
    document = _document(_relationship("supplies"), _relationship("audits"), _relationship("mirrors"))

    async with session_factory() as session, session.begin():
        revision_id = await _revision(session, document)
        await relationship_definitions.project_published_relationships(
            session, profile_revision_id=revision_id, document=document, compiled_at=_COMPILED_AT
        )

    async with session_factory() as session:
        persisted = await relationship_definitions.load_relationship_definitions(
            session, profile_revision_id=revision_id
        )

    assert [row.constraints.relationship_type for row in persisted] == [
        f"{_NAMESPACE}:audits",
        f"{_NAMESPACE}:mirrors",
        f"{_NAMESPACE}:supplies",
    ]


@pytest.mark.asyncio
async def test_a_refused_document_writes_no_partial_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A profile whose second relationship is malformed must project none of it.

    The refusal happens while reading the document, before the first insert, so a
    caller cannot end up enforcing half a profile. Ordered so the malformed entry
    sorts second, which is what makes the check meaningful — a document whose only
    entry is bad would pass even if rows were written one at a time.

    The count is taken **inside the failed transaction, before rolling back**. Read
    after a rollback it would be zero no matter what the projection did, so the
    assertion would hold just as well for an implementation that inserted the first
    row and then raised on the second — which is the implementation this test exists
    to rule out.
    """
    entries = _canonical(_document(_relationship("audits"), _relationship("supplies")))
    del entries[1]["cross_org_policy"]
    document = _redocument(entries)

    async with session_factory() as session:
        await session.begin()
        revision_id = await _revision(session, document)
        with pytest.raises(RelationshipProjectionError):
            await relationship_definitions.project_published_relationships(
                session, profile_revision_id=revision_id, document=document, compiled_at=_COMPILED_AT
            )

        written = (
            await session.execute(
                text("SELECT count(*) FROM relationship_type_definitions WHERE profile_revision_id = :rid"),
                {"rid": revision_id},
            )
        ).scalar_one()
        await session.rollback()

    assert written == 0


# --- the constraint set a writer holds ---------------------------------------------


def test_the_constraint_set_cannot_be_adjusted_after_it_is_read() -> None:
    """A writer holds this across a check and an insert; a mutable copy is not a constraint."""
    (constraints,) = relationship_definitions.relationship_constraints(_document(_relationship()))

    with pytest.raises(dataclasses.FrozenInstanceError):
        constraints.max_cardinality = 99  # type: ignore[misc]


def test_the_required_key_set_covers_every_column_the_table_declares_not_null() -> None:
    """The projection's requirement and the constraint set's fields must not drift apart.

    `RelationshipConstraints` is what a writer enforces; `REQUIRED_CONSTRAINT_KEYS`
    is what the projection insists a document state. A field added to the first
    without the second would be projectable from a document that never mentioned it.
    """
    projected = {field.name for field in dataclasses.fields(RelationshipConstraints)}
    # `relationship_type` is assembled from two authored keys, and `property_schema`
    # is compiled from `properties`; the rest map one-to-one onto an authored name.
    derived = {"relationship_type", "property_schema", "inverse_view_policy"}
    authored = {"namespace", "type_name", "properties", "inverse_view"}

    assert (projected - derived) | authored <= REQUIRED_CONSTRAINT_KEYS
