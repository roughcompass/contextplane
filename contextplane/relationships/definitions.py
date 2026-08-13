"""Project a published profile's relationships into the rows writers enforce against.

A relationship definition is authored, composed and compiled as Python objects,
but a write path cannot reach any of those: it runs in a transaction, against a
binding, and needs the constraint set for one relationship type as data it can
read and lock alongside the rows it is about to change. `relationship_type_definitions`
is that form, and this module is the only thing that writes it.

Three properties carry the weight.

**The projection is taken from the published document, never from a parallel
object.** `publish_revision` digests a canonical document and stores those exact
bytes; the digest is what every later reader authenticates against. Projecting
from an in-memory definition set that was merely *expected* to match those bytes
would let a row and the document it claims to represent disagree, with the digest
still verifying and nothing able to tell which one a writer had actually enforced.
So `project_published_relationships` parses the stored document and writes what it
finds there.

**An absent constraint is refused, not defaulted.** Every column this table
declares is `NOT NULL`, and `cross_org_policy` is the reason why: an omitted
cross-organization policy has to *be* a denial that somebody wrote down, rather
than a denial a reader assumed. A parser that filled in `deny` for a missing key
would produce exactly the same row for a document that stated the policy and a
document that lost it, so a truncated or hand-edited document would project
silently instead of failing. Every constraint key is therefore required, and its
value is checked against the same closed vocabulary the database constrains.

**One physical direction is stored, so the inverse is derived and never written.**
An edge is asserted once, in the direction the definition declares. `inverse_view`
returns the mirrored constraint set for reading; it produces no row, and nothing
here inserts one. A stored inverse would be a second copy of one fact, free to
drift from the first and impossible to keep in step under concurrent writes.

Two authored fields are deliberately not projected: `allows_self_reference` and
`extension_points`. Neither has a column, and neither is a constraint on an edge
at write time — the first is a shape question the authoring surface answers, and
the second governs what an extension may attach to, which composition settles
before a profile ever compiles. Both remain readable in the canonical document.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final, Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.profile.compiler import RELATIONSHIP_FAMILY
from contextplane.profile.schemas.common import AUTHORITY_RANK, VALUE_TYPES
from contextplane.profile.schemas.relationship import (
    CARDINALITY_SCOPES,
    CROSS_ORG_POLICIES,
    DIRECTIONS,
    DUPLICATE_POLICIES,
    INVERSE_VIEWS,
    SYMMETRIES,
)


class RelationshipProjectionError(Exception):
    """A published document does not carry a projectable relationship definition.

    Raised rather than returning a partial row: a definition missing a constraint
    is not a definition with one fewer rule, it is a definition whose rule nobody
    stated, and a writer enforcing the rest of it would be enforcing something the
    profile never said.
    """


#: The keys a canonical relationship object must carry for a row to be written.
#: Checked as a set before any value is read, so a document missing three fields
#: reports three names rather than the first one a lookup happened to reach.
REQUIRED_CONSTRAINT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "namespace",
        "type_name",
        "source_type",
        "destination_type",
        "direction",
        "cardinality_scope",
        "authority",
        "cross_org_policy",
        "min_cardinality",
        "max_cardinality",
        "duplicate_policy",
        "symmetry",
        "inverse_view",
        "properties",
    }
)

#: Each constrained column against the closed set the database checks it with.
#: Read from the schema module rather than restated, so a vocabulary that grows
#: there cannot be silently narrower here.
_VOCABULARIES: Final[Mapping[str, frozenset[str]]] = {
    "direction": frozenset(DIRECTIONS),
    "cardinality_scope": frozenset(CARDINALITY_SCOPES),
    "duplicate_policy": frozenset(DUPLICATE_POLICIES),
    "symmetry": frozenset(SYMMETRIES),
    "inverse_view": frozenset(INVERSE_VIEWS),
    "cross_org_policy": frozenset(CROSS_ORG_POLICIES),
    "authority": frozenset(AUTHORITY_RANK),
}


@dataclasses.dataclass(frozen=True)
class RelationshipConstraints:
    """One relationship type's enforceable constraint set, as one row's worth of data.

    Frozen because a writer holds this while it decides whether an edge may be
    written; a constraint set that could be adjusted between the check and the
    insert is not a constraint set.

    `relationship_type` is the qualified `<namespace>:<type>` name, which is what
    the table's uniqueness is taken over and what an assertion names. The two
    halves are not kept separately: a writer that could rebuild the qualified name
    itself is a second place for the spelling to be decided.
    """

    relationship_type: str
    source_type: str
    destination_type: str
    direction: str
    property_schema: Mapping[str, Any]
    duplicate_policy: str
    symmetry: str
    inverse_view_policy: str
    min_cardinality: int
    max_cardinality: int | None
    cardinality_scope: str
    authority: str
    cross_org_policy: str

    @classmethod
    def from_canonical(cls, canonical: Mapping[str, Any]) -> Self:
        """Read one canonical relationship object into its constraint set.

        Every key is required and every constrained value is checked here rather
        than left to the database. The database would refuse the same row, but it
        would refuse it as an `IntegrityError` naming a constraint, at the end of a
        transaction that has already written its siblings — so the profile that
        cannot be projected is reported by name, before anything is inserted.
        """
        missing = sorted(REQUIRED_CONSTRAINT_KEYS - set(canonical))
        if missing:
            name = _describe(canonical)
            msg = (
                f"{name}: canonical relationship is missing {', '.join(missing)}; every constraint this table "
                "stores is NOT NULL because an unstated rule must fail rather than resolve to whatever a reader "
                "assumes — an absent cross-organization policy in particular is not an implicit denial"
            )
            raise RelationshipProjectionError(msg)

        for field, vocabulary in _VOCABULARIES.items():
            value = canonical[field]
            if value not in vocabulary:
                name = _describe(canonical)
                msg = f"{name}: unknown {field} {value!r}; legal values are {', '.join(sorted(vocabulary))}"
                raise RelationshipProjectionError(msg)

        minimum = _cardinality(canonical, "min_cardinality", canonical_name=_describe(canonical))
        maximum = canonical["max_cardinality"]
        if maximum is not None:
            maximum = _cardinality(canonical, "max_cardinality", canonical_name=_describe(canonical))
            if maximum < minimum:
                msg = (
                    f"{_describe(canonical)}: maximum cardinality {maximum} is below minimum {minimum}; "
                    "no edge count satisfies the window"
                )
                raise RelationshipProjectionError(msg)

        return cls(
            relationship_type=f"{canonical['namespace']}:{canonical['type_name']}",
            source_type=str(canonical["source_type"]),
            destination_type=str(canonical["destination_type"]),
            direction=str(canonical["direction"]),
            property_schema=property_schema(canonical["properties"], canonical_name=_describe(canonical)),
            duplicate_policy=str(canonical["duplicate_policy"]),
            symmetry=str(canonical["symmetry"]),
            inverse_view_policy=str(canonical["inverse_view"]),
            min_cardinality=minimum,
            max_cardinality=maximum,
            cardinality_scope=str(canonical["cardinality_scope"]),
            authority=str(canonical["authority"]),
            cross_org_policy=str(canonical["cross_org_policy"]),
        )


@dataclasses.dataclass(frozen=True)
class PersistedRelationshipDefinition:
    """A definition row as it was read back, with the identity it was written under.

    Carries the row's own `definition_id` because that is what an assertion
    references: a governed edge names the definition it was written against, so a
    later definition change shows up as a difference rather than rewriting what the
    edge meant when it was asserted.
    """

    definition_id: uuid.UUID
    profile_revision_id: uuid.UUID
    extension_revision_id: uuid.UUID | None
    constraints: RelationshipConstraints
    compiled_at: datetime.datetime


def property_schema(properties: Sequence[Any], *, canonical_name: str = "relationship") -> dict[str, Any]:
    """Compile canonical property objects into the schema one row stores.

    Keyed by property name rather than kept as a list. A writer validating an
    asserted property looks it up by name, and a list would make every validation
    a scan whose cost grows with a schema the writer did not choose. The name stays
    inside its own entry as well, so an entry lifted out of the mapping for an
    error message still says what it is about.
    """
    schema: dict[str, Any] = {}
    for entry in properties:
        if not isinstance(entry, Mapping) or "name" not in entry:
            msg = f"{canonical_name}: property entry {entry!r} carries no name; a rule about nothing cannot be enforced"
            raise RelationshipProjectionError(msg)
        value_type = entry.get("value_type")
        if value_type not in VALUE_TYPES:
            msg = (
                f"{canonical_name}: property {entry['name']!r} has unknown value type {value_type!r}; "
                f"legal types are {', '.join(sorted(VALUE_TYPES))}"
            )
            raise RelationshipProjectionError(msg)
        schema[str(entry["name"])] = dict(entry)
    return schema


def inverse_view(constraints: RelationshipConstraints) -> RelationshipConstraints | None:
    """The mirrored constraint set a reader sees, or `None` when there is none to derive.

    Returns a value rather than writing one: the inverse of a stored edge is a way
    of reading it, not a second edge. Two cases have nothing to derive and say so
    with `None` rather than returning a mirror that would mislead —

    - `independently_asserted`, where the profile has said the reverse direction is
      its own assertion carrying its own provenance. Deriving a view here would put
      a read-only mirror in front of rows somebody is entitled to write, and a
      caller could not tell which of the two it was looking at.
    - `undirected`, where the relationship already holds in both directions. Its
      mirror is itself, so a separate inverse would be the same fact twice.

    The mirror keeps every constraint except the endpoints, which swap. Cardinality
    deliberately does not invert: `max_cardinality` under `per_source` counts edges
    leaving one source, and reading that same window from the other end is what
    `per_destination` is for. Recomputing it here would answer a question the
    profile never asked and contradict the row.
    """
    if constraints.inverse_view_policy == "independently_asserted":
        return None
    if constraints.direction == "undirected":
        return None
    return dataclasses.replace(
        constraints,
        source_type=constraints.destination_type,
        destination_type=constraints.source_type,
    )


def relationship_constraints(document: str) -> tuple[RelationshipConstraints, ...]:
    """Every relationship in a published profile document, in the document's order.

    The document is the three-family object `publish_revision` stored, whose
    relationship family is itself a canonical JSON string — each family is
    canonicalized alone so that two families sharing a qualified name cannot make
    the combined digest depend on which one was listed first. Ordering is left as
    the document has it, which is already sorted by qualified name.
    """
    try:
        families = json.loads(document)
        family = json.loads(families[RELATIONSHIP_FAMILY])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        msg = f"published document carries no readable {RELATIONSHIP_FAMILY} family: {error}"
        raise RelationshipProjectionError(msg) from error

    return tuple(RelationshipConstraints.from_canonical(entry) for entry in family)


async def project_published_relationships(
    session: AsyncSession,
    *,
    profile_revision_id: uuid.UUID,
    document: str,
    compiled_at: datetime.datetime,
    extension_revision_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, ...]:
    """Write one definition row per relationship in a published document.

    Takes the document rather than a definition set for the reason this module
    exists: the rows are the projection of the bytes that were published and
    digested, so a row cannot describe a rule the stored profile does not state.

    Does not commit. The projection belongs to whatever transaction published or
    activated the revision it describes — a definition row that survived a
    publication that rolled back would be an authority for a revision that does not
    exist, and a writer would enforce it happily.
    """
    definition_ids: list[uuid.UUID] = []
    for constraints in relationship_constraints(document):
        definition_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO relationship_type_definitions ("
                "  definition_id, profile_revision_id, extension_revision_id, relationship_type,"
                "  source_type, destination_type, direction, property_schema,"
                "  duplicate_policy, symmetry, inverse_view_policy,"
                "  min_cardinality, max_cardinality, cardinality_scope,"
                "  authority, cross_org_policy, compiled_at"
                ") VALUES (:did, :rid, :eid, :rtype,"
                "          :source, :destination, :direction, CAST(:schema AS JSONB),"
                "          :duplicates, :symmetry, :inverse,"
                "          :minimum, :maximum, :scope,"
                "          :authority, :cross_org, :compiled_at)"
            ),
            {
                "did": definition_id,
                "rid": profile_revision_id,
                "eid": extension_revision_id,
                "rtype": constraints.relationship_type,
                "source": constraints.source_type,
                "destination": constraints.destination_type,
                "direction": constraints.direction,
                "schema": json.dumps(constraints.property_schema, sort_keys=True, separators=(",", ":")),
                "duplicates": constraints.duplicate_policy,
                "symmetry": constraints.symmetry,
                "inverse": constraints.inverse_view_policy,
                "minimum": constraints.min_cardinality,
                "maximum": constraints.max_cardinality,
                "scope": constraints.cardinality_scope,
                "authority": constraints.authority,
                "cross_org": constraints.cross_org_policy,
                "compiled_at": compiled_at,
            },
        )
        definition_ids.append(definition_id)
    return tuple(definition_ids)


async def load_relationship_definitions(
    session: AsyncSession,
    *,
    profile_revision_id: uuid.UUID,
) -> tuple[PersistedRelationshipDefinition, ...]:
    """Every definition projected for one revision, ordered by relationship type.

    Ordered by the name rather than by insertion, so two readers of one revision
    see the same sequence and a caller comparing revisions is comparing like with
    like. Extension-contributed rows come back alongside the base ones: a writer
    enforces the profile a binding resolves to, and which document a rule arrived
    in is provenance the row already carries.
    """
    rows = (
        await session.execute(
            text(
                "SELECT definition_id, profile_revision_id, extension_revision_id, relationship_type,"
                "       source_type, destination_type, direction, property_schema,"
                "       duplicate_policy, symmetry, inverse_view_policy,"
                "       min_cardinality, max_cardinality, cardinality_scope,"
                "       authority, cross_org_policy, compiled_at"
                "  FROM relationship_type_definitions"
                " WHERE profile_revision_id = :rid"
                " ORDER BY relationship_type"
            ),
            {"rid": profile_revision_id},
        )
    ).mappings()

    return tuple(
        PersistedRelationshipDefinition(
            definition_id=row["definition_id"],
            profile_revision_id=row["profile_revision_id"],
            extension_revision_id=row["extension_revision_id"],
            constraints=RelationshipConstraints(
                relationship_type=row["relationship_type"],
                source_type=row["source_type"],
                destination_type=row["destination_type"],
                direction=row["direction"],
                property_schema=row["property_schema"],
                duplicate_policy=row["duplicate_policy"],
                symmetry=row["symmetry"],
                inverse_view_policy=row["inverse_view_policy"],
                min_cardinality=row["min_cardinality"],
                max_cardinality=row["max_cardinality"],
                cardinality_scope=row["cardinality_scope"],
                authority=row["authority"],
                cross_org_policy=row["cross_org_policy"],
            ),
            compiled_at=row["compiled_at"],
        )
        for row in rows
    )


def _describe(canonical: Mapping[str, Any]) -> str:
    """Name a canonical object for an error, even when the naming keys are what is missing."""
    namespace = canonical.get("namespace")
    type_name = canonical.get("type_name")
    if namespace is None or type_name is None:
        return "<unnamed relationship>"
    return f"{namespace}:{type_name}"


def _cardinality(canonical: Mapping[str, Any], field: str, *, canonical_name: str) -> int:
    """Read a cardinality bound as a whole number, refusing anything that is not one.

    `bool` is rejected explicitly: it is an `int` in Python, so `True` would pass a
    plain integer check and store a maximum of one that nobody wrote.
    """
    value: object = canonical[field]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{canonical_name}: {field} {value!r} is not a whole number"
        raise RelationshipProjectionError(msg)
    if value < 0:
        msg = f"{canonical_name}: {field} {value} is negative"
        raise RelationshipProjectionError(msg)
    return value


__all__ = [
    "REQUIRED_CONSTRAINT_KEYS",
    "PersistedRelationshipDefinition",
    "RelationshipConstraints",
    "RelationshipProjectionError",
    "inverse_view",
    "load_relationship_definitions",
    "project_published_relationships",
    "property_schema",
    "relationship_constraints",
]
