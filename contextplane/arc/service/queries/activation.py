"""Parametrized SQL for `ActivationService`: the ADR 040 global lock order's
`arc_artifacts`/`arc_authoring_proposal_versions`/`arc_observation_
qualifications` write-locks and compare-and-swaps, plus the plain read
`activate.py`'s response building needs.

`activation.py` owns the ten-predicate evaluation and the write orchestration;
this module owns getting rows in and out of the tables it touches --
matching every other queries module in this package's own stated convention.
`arc_revisions`'s own family lock and legal-transition check are *not*
duplicated here: `_lock_family`/`_assert_transition` in `artifact_integrity.py`
already do exactly this (ascending `revision_id` within the family), and
reusing them is what keeps "every write to `arc_revisions` agrees on the same
lock shape" true rather than merely intended -- see that module's own
docstring for why it is deliberately the acyclic base of this dependency
graph. `arc_artifacts`'s own row lock is the same story:
`queries/proposal.py::load_family_for_update` already is that lock.

Two functions here reuse a sibling queries module's private row-columns
constant and row-mapping function (`proposal.py`'s `_VERSION_COLUMNS`/
`_version_row`, `qualification.py`'s `_QUAL_COLS`/`_qualification_row`)
rather than re-declaring the same column list a third time: a `FOR UPDATE`
lock needs the identical row shape a plain read already returns, and two
independent column lists for one table is exactly how they drift apart.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.arc.service.queries.proposal import _VERSION_COLUMNS, VersionRow, _version_row
from contextplane.arc.service.queries.qualification import _QUAL_COLS, QualificationRow, _qualification_row

# ---------------------------------------------------------------------------
# Write-locks, ascending primary key within each class (ADR 040 order).
# ---------------------------------------------------------------------------


async def lock_proposal_version(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int
) -> VersionRow | None:
    """`SELECT ... FOR UPDATE` on the one proposal-version row activation
    reads and (on success) transitions -- the same row shape `queries/
    proposal.py::load_version` already returns, locked rather than plainly
    read, so `activation.py` recomputes every predicate against the exact
    row it is about to write to instead of an earlier, unlocked snapshot.
    """
    row = (
        await session.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM arc_authoring_proposal_versions "  # noqa: S608 - module constant
                "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version FOR UPDATE"
            ),
            {"proposal_id": proposal_id, "proposal_version": proposal_version},
        )
    ).one_or_none()
    if row is None:
        return None
    return _version_row(row)


async def lock_qualification(session: AsyncSession, qualification_id: uuid.UUID) -> QualificationRow | None:
    """`SELECT ... FOR UPDATE` on the named qualification row, if any --
    the last class in the ADR 040 write-lock order this task's activation
    touches (no operational-chain row is locked here: predicate 10 is
    hard-wired and reads nothing yet, see `activation.py`'s own module
    docstring).
    """
    row = (
        await session.execute(
            text(f"SELECT {_QUAL_COLS} FROM arc_observation_qualifications WHERE qualification_id = :id FOR UPDATE"),  # noqa: S608
            {"id": qualification_id},
        )
    ).one_or_none()
    return None if row is None else _qualification_row(row)


# ---------------------------------------------------------------------------
# The three writes a *won* activation performs, one statement each.
# ---------------------------------------------------------------------------


async def cas_active_revision(
    session: AsyncSession,
    *,
    artifact_id: uuid.UUID,
    expected_active_revision_id: uuid.UUID | None,
    new_active_revision_id: uuid.UUID,
) -> bool:
    """The family's own compare-and-swap: `active_revision_id` moves from
    exactly the baseline this candidate was reviewed against to the newly
    activated revision, in the same statement that decides it. `IS NOT
    DISTINCT FROM` is required, not cosmetic: a first-ever activation
    reviews no baseline at all, and Postgres `=` never matches `NULL`.
    """
    result = await session.execute(
        text(
            "UPDATE arc_artifacts SET active_revision_id = :new_id "
            "WHERE artifact_id = :artifact_id AND active_revision_id IS NOT DISTINCT FROM :expected_id"
        ),
        {"artifact_id": artifact_id, "new_id": new_active_revision_id, "expected_id": expected_active_revision_id},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def activate_revision_row(session: AsyncSession, *, revision_id: uuid.UUID, now: datetime.datetime) -> bool:
    """`draft -> active`, and the point at which `effective_from` becomes
    real: `submission.py` freezes it at submission time as an honest
    placeholder (see that module's own `_draft_revision` docstring, "decided
    at activation, not authoring") specifically for this statement to
    correct once activation is the step actually putting the revision into
    force.
    """
    result = await session.execute(
        text(
            "UPDATE arc_revisions SET lifecycle_state = 'active', activated_at = :now, effective_from = :now "
            "WHERE revision_id = :revision_id AND lifecycle_state = 'draft'"
        ),
        {"revision_id": revision_id, "now": now},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def supersede_revision_row(
    session: AsyncSession, *, revision_id: uuid.UUID, superseded_by_revision_id: uuid.UUID, now: datetime.datetime
) -> None:
    """Move the family's outgoing active revision to `superseded`, in the
    same transaction as the incoming one's activation. A no-op (zero rows)
    when there was no active revision to supersede -- a family's first
    activation -- which is exactly the baseline-drift predicate's `NULL`
    case above.
    """
    await session.execute(
        text(
            "UPDATE arc_revisions SET lifecycle_state = 'superseded', superseded_by_revision_id = :successor "
            "WHERE revision_id = :revision_id AND lifecycle_state = 'active'"
        ),
        {"revision_id": revision_id, "successor": superseded_by_revision_id, "now": now},
    )


# ---------------------------------------------------------------------------
# Plain reads.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RevisionRow:
    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    lifecycle_state: str
    activated_at: datetime.datetime | None
    revoked_at: datetime.datetime | None


async def load_revision(session: AsyncSession, revision_id: uuid.UUID) -> RevisionRow | None:
    row = (
        await session.execute(
            text(
                "SELECT revision_id, artifact_id, lifecycle_state, activated_at, revoked_at "
                "FROM arc_revisions WHERE revision_id = :revision_id"
            ),
            {"revision_id": revision_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return RevisionRow(
        revision_id=row.revision_id,
        artifact_id=row.artifact_id,
        lifecycle_state=row.lifecycle_state,
        activated_at=row.activated_at,
        revoked_at=row.revoked_at,
    )


__all__ = [
    "RevisionRow",
    "activate_revision_row",
    "cas_active_revision",
    "lock_proposal_version",
    "lock_qualification",
    "load_revision",
    "supersede_revision_row",
]
