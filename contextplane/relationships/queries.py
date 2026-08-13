"""Read governed relationships, including the inverse direction nobody stored.

One physical direction is stored. A caller asking "what depends on this?" is
asking about the same rows a caller asking "what does this depend on?" reads,
approached from the other end — so the inverse is produced here by reading the
stored direction backwards, not by keeping a second copy.

That is the whole reason this module exists rather than each caller writing its
own `SELECT`. Two query sites, one filtering on `source_entity_id` and one on
`destination_entity_id`, are two places for the in-force interval to be spelled
slightly differently, and an inverse read that disagreed with the forward read
about which rows are current would be the hardest kind of bug to see: both
answers look plausible and neither is obviously wrong.

**An inverse row says so.** `GovernedRelationship` carries `is_inverse`, so a
caller that read a row backwards can tell it is looking at a view rather than a
second edge. The flag is a label, not the enforcement: what actually prevents the
mirror from being stored is in the write path, which refuses an assertion whose
reverse is already in force for a type publishing a read-only inverse.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Selected explicitly rather than with `*` so a column added to the table does
#: not silently change what this module hands back, and so the inverse
#: projection below can be written against a known shape.
_COLUMNS: Final = (
    "relationship_id, tenant_id, relationship_type, relationship_type_definition_id,"
    " source_entity_id, destination_entity_id, cardinality_scope, properties,"
    " effective_from, effective_to, readiness_state, provenance_id, profile_binding_id, recorded_at"
)

#: A row is in force at an instant when its half-open interval contains it. A row
#: ending exactly at the instant is already over, which is what lets one
#: assertion end and its successor begin at the same moment without overlapping.
_IN_FORCE = "effective_from <= :at AND (effective_to IS NULL OR effective_to > :at)"


@dataclasses.dataclass(frozen=True)
class GovernedRelationship:
    """One governed assertion as a reader sees it, forward or inverted.

    `is_inverse` is not cosmetic. An inverted row has had its endpoints swapped
    to answer a question from the other end; it describes the same stored fact,
    so a caller that treated one as a second edge would be double-counting. The
    flag says which it is holding; the write path is what refuses to store the
    mirror.
    """

    relationship_id: uuid.UUID
    tenant_id: uuid.UUID
    relationship_type: str
    definition_id: uuid.UUID
    source_entity_id: uuid.UUID
    destination_entity_id: uuid.UUID
    cardinality_scope: str
    properties: Mapping[str, Any]
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None
    readiness_state: str
    provenance_id: uuid.UUID
    profile_binding_id: uuid.UUID
    recorded_at: datetime.datetime
    is_inverse: bool = False

    def inverted(self) -> GovernedRelationship:
        """The same stored fact read from the other end.

        Only the endpoints move. `cardinality_scope` deliberately does not
        invert: a `per_source` maximum counts edges leaving one source, and
        reading that window from the other end is what `per_destination` means —
        rewriting it here would report a limit the profile never set.
        """
        return dataclasses.replace(
            self,
            source_entity_id=self.destination_entity_id,
            destination_entity_id=self.source_entity_id,
            is_inverse=not self.is_inverse,
        )


async def outgoing(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_entity_id: uuid.UUID,
    at: datetime.datetime,
    relationship_type: str | None = None,
) -> tuple[GovernedRelationship, ...]:
    """Assertions in force from this entity, in the stored direction."""
    return await _read(
        session,
        column="source_entity_id",
        entity_id=source_entity_id,
        tenant_id=tenant_id,
        at=at,
        relationship_type=relationship_type,
        invert=False,
    )


async def incoming(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    destination_entity_id: uuid.UUID,
    at: datetime.datetime,
    relationship_type: str | None = None,
) -> tuple[GovernedRelationship, ...]:
    """Assertions in force pointing at this entity, read as inverse views.

    Every row comes back with `is_inverse` set and its endpoints swapped, so the
    caller reads them the way it asked the question — "what points at me" — while
    the flag keeps saying these are one stored direction seen backwards.
    """
    return await _read(
        session,
        column="destination_entity_id",
        entity_id=destination_entity_id,
        tenant_id=tenant_id,
        at=at,
        relationship_type=relationship_type,
        invert=True,
    )


async def in_force_between(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    relationship_type: str,
    source_entity_id: uuid.UUID,
    destination_entity_id: uuid.UUID,
    at: datetime.datetime,
) -> GovernedRelationship | None:
    """The assertion of this type in force over this ordered pair, if any.

    At most one can exist: the table's temporal exclusion forbids two of one type
    over one ordered pair whose intervals overlap. This is the duplicate check the
    write path runs, and it is a read of the same constraint the database will
    enforce a moment later rather than a second opinion about it.
    """
    rows = await _rows(
        session,
        where=(
            "tenant_id = :tenant AND relationship_type = :rtype"
            " AND source_entity_id = :src AND destination_entity_id = :dst"
            f" AND {_IN_FORCE}"
        ),
        parameters={
            "tenant": tenant_id,
            "rtype": relationship_type,
            "src": source_entity_id,
            "dst": destination_entity_id,
            "at": at,
        },
    )
    if not rows:
        return None
    return _to_relationship(rows[0])


async def _read(
    session: AsyncSession,
    *,
    column: str,
    entity_id: uuid.UUID,
    tenant_id: uuid.UUID,
    at: datetime.datetime,
    relationship_type: str | None,
    invert: bool,
) -> tuple[GovernedRelationship, ...]:
    parameters: dict[str, object] = {"tenant": tenant_id, "entity": entity_id, "at": at}
    where = f"tenant_id = :tenant AND {column} = :entity AND {_IN_FORCE}"
    if relationship_type is not None:
        where += " AND relationship_type = :rtype"
        parameters["rtype"] = relationship_type

    rows = await _rows(session, where=where, parameters=parameters)
    read = tuple(_to_relationship(row) for row in rows)
    return tuple(item.inverted() for item in read) if invert else read


async def _rows(
    session: AsyncSession,
    *,
    where: str,
    parameters: Mapping[str, object],
) -> Sequence[Mapping[str, Any]]:
    """Ordered by relationship type then start, so two readers agree on sequence."""
    result = await session.execute(
        # `where` is assembled from module constants and a fixed column name chosen
        # by this module's own callers; every value is bound, so no caller input
        # reaches the statement text.
        text(
            f"SELECT {_COLUMNS} FROM relationship_metadata"  # noqa: S608
            f" WHERE {where}"
            " ORDER BY relationship_type, effective_from DESC, relationship_id"
        ),
        dict(parameters),
    )
    return [dict(row) for row in result.mappings()]


def _to_relationship(row: Mapping[str, Any]) -> GovernedRelationship:
    return GovernedRelationship(
        relationship_id=row["relationship_id"],
        tenant_id=row["tenant_id"],
        relationship_type=row["relationship_type"],
        definition_id=row["relationship_type_definition_id"],
        source_entity_id=row["source_entity_id"],
        destination_entity_id=row["destination_entity_id"],
        cardinality_scope=row["cardinality_scope"],
        properties=row["properties"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        readiness_state=row["readiness_state"],
        provenance_id=row["provenance_id"],
        profile_binding_id=row["profile_binding_id"],
        recorded_at=row["recorded_at"],
    )


__all__ = ["GovernedRelationship", "in_force_between", "incoming", "outgoing"]
