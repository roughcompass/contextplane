"""Parametrized SQL for `arc_authoring_reach_confirmations`.

Sibling of `drafter.py`, matching `queries/proposal.py`'s own convention:
every function takes an already-open `AsyncSession` and issues exactly the
statements its name promises, so the caller controls what commits together.

Per-field survival is the point of `upsert_reach_confirmation`, same
reasoning as `queries/provenance.py::upsert_field_provenance`: it writes
exactly one `(proposal_id, proposal_version, field_path)` row via `INSERT
... ON CONFLICT ... DO UPDATE`, never a delete-then-reinsert of the whole
set for a version -- a caller confirming reach for field B must not disturb
field A's already-recorded confirmation.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class ReachConfirmationRow:
    proposal_id: uuid.UUID
    proposal_version: int
    field_path: str
    confirmed: bool
    confirmed_at: datetime.datetime | None
    confirmed_by_issuer: str | None
    confirmed_by_subject: str | None


_COLUMNS = (
    "proposal_id, proposal_version, field_path, confirmed, confirmed_at, confirmed_by_issuer, confirmed_by_subject"
)


def _row(raw: Any) -> ReachConfirmationRow:  # noqa: ANN401 - a raw SQLAlchemy Row has no narrower public type
    return ReachConfirmationRow(
        proposal_id=raw.proposal_id,
        proposal_version=raw.proposal_version,
        field_path=raw.field_path,
        confirmed=raw.confirmed,
        confirmed_at=raw.confirmed_at,
        confirmed_by_issuer=raw.confirmed_by_issuer,
        confirmed_by_subject=raw.confirmed_by_subject,
    )


async def upsert_reach_confirmation(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    field_path: str,
    confirmed: bool,
    confirmed_at: datetime.datetime | None,
    confirmed_by_issuer: str | None,
    confirmed_by_subject: str | None,
) -> None:
    """Write exactly one field's reach-confirmation row, in place.

    `ON CONFLICT (proposal_id, proposal_version, field_path) DO UPDATE`
    replaces this one row's own columns -- rows for every other
    `field_path` on this version are untouched, since they are never named
    in the `WHERE` this statement resolves against.
    """
    await session.execute(
        text(
            "INSERT INTO arc_authoring_reach_confirmations ("
            "  proposal_id, proposal_version, field_path, confirmed,"
            "  confirmed_at, confirmed_by_issuer, confirmed_by_subject"
            ") VALUES ("
            "  :proposal_id, :proposal_version, :field_path, :confirmed,"
            "  :confirmed_at, :confirmed_by_issuer, :confirmed_by_subject"
            ") ON CONFLICT (proposal_id, proposal_version, field_path) DO UPDATE SET"
            "  confirmed = EXCLUDED.confirmed,"
            "  confirmed_at = EXCLUDED.confirmed_at,"
            "  confirmed_by_issuer = EXCLUDED.confirmed_by_issuer,"
            "  confirmed_by_subject = EXCLUDED.confirmed_by_subject"
        ),
        {
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "field_path": field_path,
            "confirmed": confirmed,
            "confirmed_at": confirmed_at,
            "confirmed_by_issuer": confirmed_by_issuer,
            "confirmed_by_subject": confirmed_by_subject,
        },
    )


async def load_reach_confirmations(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int
) -> list[ReachConfirmationRow]:
    rows = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM arc_authoring_reach_confirmations "  # noqa: S608 - constant column list, bound parameters below
            "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version ORDER BY field_path"
        ),
        {"proposal_id": proposal_id, "proposal_version": proposal_version},
    )
    return [_row(row) for row in rows]


async def load_reach_confirmations_for_paths(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int, field_paths: list[str]
) -> list[ReachConfirmationRow]:
    """Same read as `load_reach_confirmations`, restricted to exactly the
    given field paths -- what `DrafterService.confirm_reach` returns after
    writing, so the response reflects only the fields the caller asked
    about rather than the whole, potentially larger, persisted set."""
    rows = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM arc_authoring_reach_confirmations "  # noqa: S608 - constant column list, bound parameters below
            "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version "
            "AND field_path = ANY(:field_paths) ORDER BY field_path"
        ),
        {"proposal_id": proposal_id, "proposal_version": proposal_version, "field_paths": field_paths},
    )
    return [_row(row) for row in rows]


__all__ = [
    "ReachConfirmationRow",
    "load_reach_confirmations",
    "load_reach_confirmations_for_paths",
    "upsert_reach_confirmation",
]
