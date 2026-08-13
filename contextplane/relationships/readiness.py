"""Whether an entity's required relationships are present, counted under lock.

Minimum cardinality is the one relationship rule that cannot be a write-time
refusal. An entity whose type requires an owner cannot be assembled at all if the
first write is rejected for the owner not existing yet — the owner edge and the
entity would each be waiting for the other. So the minimum gates *readiness*
instead: a draft may sit below it, and only a transition to ready is refused
while a required relationship is missing.

That makes readiness a counted question rather than a stored opinion, and the
count has to be taken under the same lock the maximum takes. Two transactions
each ending the last remaining edge, each observing the other's row as still in
force, would both leave an entity that reads as ready with nothing to back it —
the same unlocked count-then-write the maximum forbids, arrived at from below.

`readiness_state` is stored on the assertion rather than recomputed on read. A
value derived at read time answers with today's rules about a row asserted under
older ones, which is exactly the difference a governance audit is trying to see.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: The states an assertion may record. `blocked` is not "invalid": it is a row
#: that was accepted and whose entity cannot advance until something else lands,
#: which a reader has to be able to tell apart from a draft nobody has finished.
DRAFT: Final = "draft"
READY: Final = "ready"
BLOCKED: Final = "blocked"

READINESS_STATES: Final[frozenset[str]] = frozenset({DRAFT, READY, BLOCKED})

#: The column each cardinality scope counts over. `per_pair` counts both ends,
#: which is what makes it a duplicate check rather than a fan-out limit.
_SCOPE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "per_source": ("source_entity_id",),
    "per_destination": ("destination_entity_id",),
    "per_pair": ("source_entity_id", "destination_entity_id"),
}


class UnknownCardinalityScope(ValueError):
    """A scope this module cannot count over.

    Raised rather than defaulted to `per_source`: a scope nobody recognises means
    the definition and this code disagree about the vocabulary, and counting the
    wrong window would enforce a limit the profile never stated.
    """


def scope_predicate(cardinality_scope: str) -> str:
    """The SQL predicate that selects the assertions one scope counts.

    Returned as a fragment rather than a whole query because both the maximum
    check and the readiness check count the same window and must not drift into
    counting two slightly different ones.
    """
    columns = _SCOPE_COLUMNS.get(cardinality_scope)
    if columns is None:
        msg = f"unknown cardinality scope {cardinality_scope!r}; legal: {', '.join(sorted(_SCOPE_COLUMNS))}"
        raise UnknownCardinalityScope(msg)
    return " AND ".join(f"{column} = :{column}" for column in columns)


def scope_parameters(
    cardinality_scope: str,
    *,
    source_entity_id: uuid.UUID,
    destination_entity_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """The bindings `scope_predicate` expects, and only those.

    Only the columns in scope are bound, so a predicate and a parameter set that
    disagree fail loudly at the driver rather than silently counting a window
    nobody asked for.
    """
    columns = _SCOPE_COLUMNS.get(cardinality_scope)
    if columns is None:
        msg = f"unknown cardinality scope {cardinality_scope!r}; legal: {', '.join(sorted(_SCOPE_COLUMNS))}"
        raise UnknownCardinalityScope(msg)
    available = {"source_entity_id": source_entity_id, "destination_entity_id": destination_entity_id}
    return {column: available[column] for column in columns}


async def count_in_force(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    relationship_type: str,
    cardinality_scope: str,
    source_entity_id: uuid.UUID,
    destination_entity_id: uuid.UUID,
    at: datetime.datetime,
) -> int:
    """How many assertions of this type are in force in this scope at `at`.

    The caller must already hold the aggregate lock for
    `(binding, relationship_type, cardinality_scope)`. This function does not take
    it: a count that acquired its own lock would release the reader's
    expectations about what the number still means by the time it is used, and
    the whole point is that the count and the write that depends on it happen
    under one hold.

    "In force" is a half-open interval — `effective_from <= at` and either no end
    or an end strictly after `at`. A row ending exactly at `at` is not counted,
    which is what makes ending one assertion and starting its replacement at the
    same instant legal rather than a momentary double.
    """
    predicate = scope_predicate(cardinality_scope)
    parameters: dict[str, object] = {
        "tenant": tenant_id,
        "rtype": relationship_type,
        "at": at,
        **scope_parameters(
            cardinality_scope,
            source_entity_id=source_entity_id,
            destination_entity_id=destination_entity_id,
        ),
    }
    counted = (
        await session.execute(
            # `predicate` is built by `scope_predicate` from `_SCOPE_COLUMNS`, a
            # closed mapping of column names in this file. Nothing a caller supplies
            # reaches the statement text: the scope selects which fragment, and every
            # value is bound.
            text(
                "SELECT count(*) FROM relationship_metadata"  # noqa: S608
                " WHERE tenant_id = :tenant AND relationship_type = :rtype"
                f"   AND {predicate}"
                "   AND effective_from <= :at"
                "   AND (effective_to IS NULL OR effective_to > :at)"
            ),
            parameters,
        )
    ).scalar_one()
    return int(counted)


def readiness_for(*, observed: int, minimum: int) -> str:
    """The state an assertion records given what is in force alongside it.

    `observed` counts the window *including* the assertion being written, so a
    type requiring one is ready on its first edge rather than its second.

    A minimum of zero yields `ready` rather than `draft`: nothing is outstanding,
    and calling that a draft would make every unconstrained relationship look
    unfinished forever.
    """
    if minimum <= 0 or observed >= minimum:
        return READY
    return DRAFT


def blocks_activation(state: str) -> bool:
    """Whether an entity carrying this assertion may not become active.

    `draft` blocks and `blocked` blocks; only `ready` does not. Written as one
    function so the activation rule has a single reading — an `in (...)` spelled
    at each call site is where two of them end up disagreeing about `blocked`.
    """
    if state not in READINESS_STATES:
        msg = f"unknown readiness state {state!r}; legal: {', '.join(sorted(READINESS_STATES))}"
        raise ValueError(msg)
    return state != READY


__all__ = [
    "BLOCKED",
    "DRAFT",
    "READINESS_STATES",
    "READY",
    "UnknownCardinalityScope",
    "blocks_activation",
    "count_in_force",
    "readiness_for",
    "scope_parameters",
    "scope_predicate",
]
