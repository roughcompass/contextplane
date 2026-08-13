"""Read ownership, including the two directions nobody stores separately.

An assignment is one row joining an owner to a target. "What does this team own"
and "who owns this thing" are the same rows read from opposite ends, so both are
derived here rather than kept as two tables that can disagree — the same reason
the relationship surface derives its inverse instead of storing it.

**Ownership is not authorization, and these reads must never be mistaken for it.**
Nothing here returns a permission, a role the auth layer understands, or anything
an entitlement check consumes. An owner is who is accountable for a thing; what
they may *do* is decided somewhere else entirely, from the credential. Wiring one
to the other would make "assign an owner" a privilege-escalation primitive.

**Only assignments in force are returned by default.** A `draft` assignment is a
proposal nobody has validated and a `revoked` one was withdrawn; reporting either
as ownership would answer "who owns this" with somebody who does not.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DRAFT: Final = "draft"
PROPOSED: Final = "proposed"
VALIDATED: Final = "validated"
SUPERSEDED: Final = "superseded"
REVOKED: Final = "revoked"

OWNERSHIP_STATES: Final[frozenset[str]] = frozenset({DRAFT, PROPOSED, VALIDATED, SUPERSEDED, REVOKED})

#: The states that mean "this owner is accountable right now". `proposed` is not
#: among them: a proposal that counted as ownership would let anyone establish
#: accountability by asserting it and waiting.
IN_FORCE_STATES: Final[frozenset[str]] = frozenset({VALIDATED})

_COLUMNS: Final = (
    "ownership_assignment_id, tenant_id, owner_principal, owned_target_kind, owned_target_id,"
    " role, scope, source, derivation_method, confidence, validation_state,"
    " effective_from, effective_to, provenance_id, replaced_by_assignment_id,"
    " revocation_reason, recorded_by, recorded_at"
)

_IN_FORCE = "effective_from <= :at AND (effective_to IS NULL OR effective_to > :at)"


@dataclasses.dataclass(frozen=True)
class OwnershipAssignment:
    """One assignment as a reader sees it."""

    ownership_assignment_id: uuid.UUID
    tenant_id: uuid.UUID
    owner_principal: str
    owned_target_kind: str
    owned_target_id: uuid.UUID
    role: str
    scope: str
    source: str
    derivation_method: str | None
    confidence: float | None
    validation_state: str
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None
    provenance_id: uuid.UUID
    replaced_by_assignment_id: uuid.UUID | None
    revocation_reason: str | None
    recorded_by: str
    recorded_at: datetime.datetime

    @property
    def is_in_force(self) -> bool:
        """Whether this assignment currently establishes accountability."""
        return self.validation_state in IN_FORCE_STATES

    @property
    def is_pending(self) -> bool:
        """Whether this assignment is asserted but not yet validated.

        A caller showing ownership in a UI needs to distinguish "nobody has
        confirmed this" from "this is settled", and a pending assignment shown
        without the label reads as the latter.
        """
        return self.validation_state in {DRAFT, PROPOSED}


async def owned_by(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_principal: str,
    at: datetime.datetime,
    include_pending: bool = False,
) -> tuple[OwnershipAssignment, ...]:
    """What this principal owns — the `owns` view."""
    return await _read(
        session,
        where="tenant_id = :tenant AND owner_principal = :owner",
        parameters={"tenant": tenant_id, "owner": owner_principal, "at": at},
        include_pending=include_pending,
    )


async def owners_of(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owned_target_kind: str,
    owned_target_id: uuid.UUID,
    at: datetime.datetime,
    include_pending: bool = False,
) -> tuple[OwnershipAssignment, ...]:
    """Who owns this target — the `owned_by` view, from the other end of one row."""
    return await _read(
        session,
        where="tenant_id = :tenant AND owned_target_kind = :kind AND owned_target_id = :target",
        parameters={
            "tenant": tenant_id,
            "kind": owned_target_kind,
            "target": owned_target_id,
            "at": at,
        },
        include_pending=include_pending,
    )


async def get(session: AsyncSession, *, tenant_id: uuid.UUID, assignment_id: uuid.UUID) -> OwnershipAssignment | None:
    """One assignment in whatever state it holds, including revoked.

    Unfiltered by state on purpose: this is the read a transition path uses to
    decide what move is legal, and it has to see a revoked row rather than a
    missing one — refusing a transition is a different answer from "no such
    assignment".
    """
    row = (
        (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM ownership_assignments"  # noqa: S608
                    " WHERE tenant_id = :t AND ownership_assignment_id = :a"
                ),
                {"t": tenant_id, "a": assignment_id},
            )
        )
        .mappings()
        .first()
    )
    return None if row is None else _to_assignment(dict(row))


async def transitions_of(session: AsyncSession, *, assignment_id: uuid.UUID) -> tuple[Mapping[str, Any], ...]:
    """Every recorded move on this assignment, in sequence.

    Ordered by the stored sequence rather than by time: two transitions recorded
    in the same instant would otherwise have no defined order, and the sequence is
    unique per assignment precisely so the history reads the same way twice.
    """
    rows = (
        await session.execute(
            text(
                "SELECT sequence, from_state, to_state, reason, recorded_by, recorded_at"
                "  FROM ownership_assignment_transitions"
                " WHERE ownership_assignment_id = :a ORDER BY sequence"
            ),
            {"a": assignment_id},
        )
    ).mappings()
    return tuple(dict(row) for row in rows)


async def _read(
    session: AsyncSession,
    *,
    where: str,
    parameters: Mapping[str, object],
    include_pending: bool,
) -> tuple[OwnershipAssignment, ...]:
    states = sorted(IN_FORCE_STATES | ({DRAFT, PROPOSED} if include_pending else set()))
    rows = (
        await session.execute(
            text(
                # `where` and `_COLUMNS` are this module's own literals; every
                # value in the statement is bound.
                f"SELECT {_COLUMNS} FROM ownership_assignments"  # noqa: S608
                f" WHERE {where} AND {_IN_FORCE} AND validation_state = ANY(CAST(:states AS TEXT[]))"
                " ORDER BY owned_target_kind, owned_target_id, role, effective_from DESC"
            ),
            {**parameters, "states": states},
        )
    ).mappings()
    return tuple(_to_assignment(dict(row)) for row in rows)


def _to_assignment(row: Mapping[str, Any]) -> OwnershipAssignment:
    return OwnershipAssignment(**{field.name: row[field.name] for field in dataclasses.fields(OwnershipAssignment)})


def state_names(assignments: Sequence[OwnershipAssignment]) -> list[str]:
    """The states present in a result, for a caller reporting on a mixed set."""
    return sorted({assignment.validation_state for assignment in assignments})


__all__ = [
    "DRAFT",
    "IN_FORCE_STATES",
    "OWNERSHIP_STATES",
    "PROPOSED",
    "REVOKED",
    "SUPERSEDED",
    "VALIDATED",
    "OwnershipAssignment",
    "get",
    "owned_by",
    "owners_of",
    "state_names",
    "transitions_of",
]
