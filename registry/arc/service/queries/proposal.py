"""Parametrized SQL for artifact families, proposal threads, and proposal
versions.

`proposal.py` owns authorization, the state-machine, and the compare-and-swap
shape; this module owns getting rows in and out of the three tables that
service touches (`arc_artifacts`, `arc_authoring_proposals`,
`arc_authoring_proposal_versions`). Every function takes an already-open
`AsyncSession` -- none of them opens its own transaction -- so the caller
controls exactly what commits together, matching the source-admission
queries module's own convention.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FamilyRow:
    artifact_id: uuid.UUID
    tenant_id: uuid.UUID | None
    slug: str
    kind: str
    title: str
    active_revision_id: uuid.UUID | None
    created_at: datetime.datetime
    created_by_issuer: str
    created_by_subject: str


@dataclasses.dataclass(frozen=True)
class ThreadRow:
    proposal_id: uuid.UUID
    artifact_id: uuid.UUID
    created_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class VersionRow:
    proposal_id: uuid.UUID
    proposal_version: int
    artifact_id: uuid.UUID
    tenant_id: uuid.UUID | None
    state: str
    source_evidence_id: uuid.UUID
    reviewed_baseline_revision_id: uuid.UUID | None
    revision_id: uuid.UUID | None
    risk_classification: str | None
    risk_algorithm_version: str | None
    opened_by_issuer: str
    opened_by_subject: str
    created_at: datetime.datetime
    frozen_at: datetime.datetime | None
    terminal_reason_code: str | None
    terminal_note: str | None
    terminal_by_issuer: str | None
    terminal_by_subject: str | None
    terminalized_at: datetime.datetime | None
    # Defaulted, not just typed optional: every pre-existing construction
    # site across the test tree built a `VersionRow` before this column
    # existed, and a required field here would break every one of them for
    # no gain -- `None` is also the correct value for any version no
    # `PATCH` has touched yet.
    semantics: dict[str, Any] | None = None


_VERSION_COLUMNS = (
    "proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id, "
    "reviewed_baseline_revision_id, revision_id, risk_classification, risk_algorithm_version, "
    "opened_by_issuer, opened_by_subject, created_at, frozen_at, terminal_reason_code, "
    "terminal_note, terminal_by_issuer, terminal_by_subject, terminalized_at, semantics"
)


def _version_row(row: Any) -> VersionRow:  # noqa: ANN401 - a raw SQLAlchemy Row has no narrower public type
    return VersionRow(
        proposal_id=row.proposal_id,
        proposal_version=row.proposal_version,
        artifact_id=row.artifact_id,
        tenant_id=row.tenant_id,
        state=row.state,
        source_evidence_id=row.source_evidence_id,
        reviewed_baseline_revision_id=row.reviewed_baseline_revision_id,
        revision_id=row.revision_id,
        risk_classification=row.risk_classification,
        risk_algorithm_version=row.risk_algorithm_version,
        opened_by_issuer=row.opened_by_issuer,
        opened_by_subject=row.opened_by_subject,
        created_at=row.created_at,
        frozen_at=row.frozen_at,
        terminal_reason_code=row.terminal_reason_code,
        terminal_note=row.terminal_note,
        terminal_by_issuer=row.terminal_by_issuer,
        terminal_by_subject=row.terminal_by_subject,
        terminalized_at=row.terminalized_at,
        semantics=row.semantics,
    )


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# arc_artifacts -- the artifact family
# ---------------------------------------------------------------------------


async def insert_family(
    session: AsyncSession,
    *,
    artifact_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    slug: str,
    kind: str,
    title: str,
    created_at: datetime.datetime,
    created_by_issuer: str,
    created_by_subject: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_artifacts ("
            "  artifact_id, tenant_id, slug, kind, title, created_at, created_by_issuer, created_by_subject"
            ") VALUES ("
            "  :artifact_id, :tenant_id, :slug, :kind, :title, :created_at, :created_by_issuer, :created_by_subject"
            ")"
        ),
        {
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "slug": slug,
            "kind": kind,
            "title": title,
            "created_at": created_at,
            "created_by_issuer": created_by_issuer,
            "created_by_subject": created_by_subject,
        },
    )


async def load_family(session: AsyncSession, artifact_id: uuid.UUID) -> FamilyRow | None:
    row = (
        await session.execute(
            text(
                "SELECT artifact_id, tenant_id, slug, kind, title, active_revision_id, created_at, "
                "       created_by_issuer, created_by_subject "
                "FROM arc_artifacts WHERE artifact_id = :artifact_id"
            ),
            {"artifact_id": artifact_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return FamilyRow(
        artifact_id=row.artifact_id,
        tenant_id=row.tenant_id,
        slug=row.slug,
        kind=row.kind,
        title=row.title,
        active_revision_id=row.active_revision_id,
        created_at=row.created_at,
        created_by_issuer=row.created_by_issuer,
        created_by_subject=row.created_by_subject,
    )


async def load_family_for_update(session: AsyncSession, artifact_id: uuid.UUID) -> FamilyRow | None:
    """Same read as `load_family`, `FOR UPDATE`.

    `open_proposal` holds this lock across the get-or-create-thread step
    below it, matching the ADR 040 lock order's "artifact-family row" class
    preceding "proposal-version row".
    """
    row = (
        await session.execute(
            text(
                "SELECT artifact_id, tenant_id, slug, kind, title, active_revision_id, created_at, "
                "       created_by_issuer, created_by_subject "
                "FROM arc_artifacts WHERE artifact_id = :artifact_id FOR UPDATE"
            ),
            {"artifact_id": artifact_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return FamilyRow(
        artifact_id=row.artifact_id,
        tenant_id=row.tenant_id,
        slug=row.slug,
        kind=row.kind,
        title=row.title,
        active_revision_id=row.active_revision_id,
        created_at=row.created_at,
        created_by_issuer=row.created_by_issuer,
        created_by_subject=row.created_by_subject,
    )


# ---------------------------------------------------------------------------
# arc_authoring_proposals -- the thread
# ---------------------------------------------------------------------------


async def get_or_create_thread(
    session: AsyncSession, *, artifact_id: uuid.UUID, created_at: datetime.datetime
) -> uuid.UUID:
    """Idempotent thread creation: `artifact_id` is `UNIQUE`, so a race
    between two callers opening a proposal on the same brand-new family
    resolves to one thread row regardless of which insert wins.
    """
    proposal_id = uuid.uuid4()
    inserted = (
        await session.execute(
            text(
                "INSERT INTO arc_authoring_proposals (proposal_id, artifact_id, created_at) "
                "VALUES (:proposal_id, :artifact_id, :created_at) "
                "ON CONFLICT (artifact_id) DO NOTHING "
                "RETURNING proposal_id"
            ),
            {"proposal_id": proposal_id, "artifact_id": artifact_id, "created_at": created_at},
        )
    ).one_or_none()
    if inserted is not None:
        return inserted.proposal_id  # type: ignore[no-any-return]
    existing = (
        await session.execute(
            text("SELECT proposal_id FROM arc_authoring_proposals WHERE artifact_id = :artifact_id"),
            {"artifact_id": artifact_id},
        )
    ).one()
    return existing.proposal_id  # type: ignore[no-any-return]


async def lock_thread(session: AsyncSession, proposal_id: uuid.UUID) -> None:
    """Hold the thread row for the rest of this transaction.

    Two concurrent `open_proposal` calls against the same artifact serialize
    here: the second waits for the first's transaction to end, then
    re-reads the latest version under its own lock and correctly sees
    whatever the first committed.
    """
    await session.execute(
        text("SELECT proposal_id FROM arc_authoring_proposals WHERE proposal_id = :proposal_id FOR UPDATE"),
        {"proposal_id": proposal_id},
    )


async def load_thread(session: AsyncSession, proposal_id: uuid.UUID) -> ThreadRow | None:
    row = (
        await session.execute(
            text(
                "SELECT proposal_id, artifact_id, created_at FROM arc_authoring_proposals "
                "WHERE proposal_id = :proposal_id"
            ),
            {"proposal_id": proposal_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return ThreadRow(proposal_id=row.proposal_id, artifact_id=row.artifact_id, created_at=row.created_at)


# ---------------------------------------------------------------------------
# arc_authoring_proposal_versions
# ---------------------------------------------------------------------------


async def load_latest_version(session: AsyncSession, proposal_id: uuid.UUID) -> VersionRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM arc_authoring_proposal_versions "  # noqa: S608 - _VERSION_COLUMNS is a module constant, not caller input
                "WHERE proposal_id = :proposal_id ORDER BY proposal_version DESC LIMIT 1"
            ),
            {"proposal_id": proposal_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return _version_row(row)


async def load_version(session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int) -> VersionRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM arc_authoring_proposal_versions "  # noqa: S608 - _VERSION_COLUMNS is a module constant, not caller input
                "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version"
            ),
            {"proposal_id": proposal_id, "proposal_version": proposal_version},
        )
    ).one_or_none()
    if row is None:
        return None
    return _version_row(row)


async def load_version_by_revision_id(session: AsyncSession, revision_id: uuid.UUID) -> VersionRow | None:
    """The one version row this *materialised* revision bijects to.

    `revision_id` is `UNIQUE` on this table (the ADR 040 bijection), so this
    can never return more than one row. Added for `registry.arc.service.
    integrity`, which is handed a bare `revision_id` (never a `(proposal_id,
    proposal_version)` pair) and needs the proposal identity and
    `source_evidence_id` behind it before it can recompute anything.
    """
    row = (
        await session.execute(
            text(
                f"SELECT {_VERSION_COLUMNS} FROM arc_authoring_proposal_versions "  # noqa: S608 - _VERSION_COLUMNS is a module constant, not caller input
                "WHERE revision_id = :revision_id"
            ),
            {"revision_id": revision_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return _version_row(row)


async def list_versions_for_thread(session: AsyncSession, proposal_id: uuid.UUID) -> list[VersionRow]:
    rows = await session.execute(
        text(
            f"SELECT {_VERSION_COLUMNS} FROM arc_authoring_proposal_versions "  # noqa: S608 - _VERSION_COLUMNS is a module constant, not caller input
            "WHERE proposal_id = :proposal_id ORDER BY proposal_version"
        ),
        {"proposal_id": proposal_id},
    )
    return [_version_row(row) for row in rows]


async def insert_version(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    artifact_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    source_evidence_id: uuid.UUID,
    reviewed_baseline_revision_id: uuid.UUID | None,
    opened_by_issuer: str,
    opened_by_subject: str,
    created_at: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_authoring_proposal_versions ("
            "  proposal_id, proposal_version, artifact_id, tenant_id, state, source_evidence_id,"
            "  reviewed_baseline_revision_id, opened_by_issuer, opened_by_subject, created_at"
            ") VALUES ("
            "  :proposal_id, :proposal_version, :artifact_id, :tenant_id, 'open', :source_evidence_id,"
            "  :reviewed_baseline_revision_id, :opened_by_issuer, :opened_by_subject, :created_at"
            ")"
        ),
        {
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "source_evidence_id": source_evidence_id,
            "reviewed_baseline_revision_id": reviewed_baseline_revision_id,
            "opened_by_issuer": opened_by_issuer,
            "opened_by_subject": opened_by_subject,
            "created_at": created_at,
        },
    )


async def update_semantics(
    session: AsyncSession, *, proposal_id: uuid.UUID, proposal_version: int, semantics: dict[str, Any]
) -> None:
    """Persist the candidate `arc_artifact_semantics_v1` document a `PATCH`
    just validated, in place.

    The caller (`ProvenanceService.edit`) has already validated *semantics*
    and confirmed the version is open before calling this, in the same
    transaction as the `field_provenance` upserts it writes alongside --
    so an invalid candidate, or a version that is not open, never reaches
    this statement at all. No `WHERE state = ...` guard is needed here for
    that reason, matching `upsert_field_provenance`'s own convention of
    trusting the caller's pre-checked state rather than re-deriving it.
    """
    await session.execute(
        text(
            "UPDATE arc_authoring_proposal_versions SET semantics = CAST(:semantics AS JSONB) "
            "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version"
        ),
        {
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "semantics": _json(semantics),
        },
    )


async def transition_version(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    from_states: Sequence[str],
    to_state: str,
    reason_code: str,
    note: str | None,
    actor_issuer: str,
    actor_subject: str,
    now: datetime.datetime,
) -> VersionRow | None:
    """The compare-and-swap: the `WHERE state = ANY(:from_states)` clause is
    the whole mechanism. Two concurrent callers racing the same transition
    both issue this statement; Postgres's row lock during the `UPDATE`
    serializes them, and at most one `RETURNING` row comes back. Returns
    `None` on a lost race or an already-terminal row -- the caller decides
    whether that means "not found" or "conflict" by re-reading.
    """
    row = (
        await session.execute(
            text(
                f"UPDATE arc_authoring_proposal_versions SET "  # noqa: S608 - _VERSION_COLUMNS is a module constant, not caller input
                "  state = :to_state, terminal_reason_code = :reason_code, terminal_note = :note,"
                "  terminal_by_issuer = :actor_issuer, terminal_by_subject = :actor_subject, terminalized_at = :now "
                "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version "
                "  AND state = ANY(:from_states) "
                f"RETURNING {_VERSION_COLUMNS}"
            ),
            {
                "proposal_id": proposal_id,
                "proposal_version": proposal_version,
                "from_states": list(from_states),
                "to_state": to_state,
                "reason_code": reason_code,
                "note": note,
                "actor_issuer": actor_issuer,
                "actor_subject": actor_subject,
                "now": now,
            },
        )
    ).one_or_none()
    if row is None:
        return None
    return _version_row(row)


async def list_versions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    artifact_id: uuid.UUID | None,
    state: str | None,
    cursor_created_at: datetime.datetime | None,
    cursor_proposal_id: uuid.UUID | None,
    cursor_proposal_version: int | None,
    page_size: int,
) -> list[VersionRow]:
    """Keyset pagination on `(created_at DESC, proposal_id DESC, proposal_version DESC)`.

    A `cursor` names the last row the caller already saw; every predicate
    below is optional and independently composable, because `list_proposals`
    callers filter by any combination of tenant/artifact/state.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {"page_size": page_size}

    if tenant_id is None:
        clauses.append("tenant_id IS NULL")
    else:
        clauses.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if artifact_id is not None:
        clauses.append("artifact_id = :artifact_id")
        params["artifact_id"] = artifact_id
    if state is not None:
        clauses.append("state = :state")
        params["state"] = state
    if cursor_created_at is not None and cursor_proposal_id is not None and cursor_proposal_version is not None:
        clauses.append(
            "(created_at, proposal_id, proposal_version) < "
            "(:cursor_created_at, :cursor_proposal_id, :cursor_proposal_version)"
        )
        params["cursor_created_at"] = cursor_created_at
        params["cursor_proposal_id"] = cursor_proposal_id
        params["cursor_proposal_version"] = cursor_proposal_version

    where = " AND ".join(clauses) if clauses else "TRUE"
    rows = await session.execute(
        text(
            f"SELECT {_VERSION_COLUMNS} FROM arc_authoring_proposal_versions "  # noqa: S608 - _VERSION_COLUMNS is a module constant; clauses/where are built from fixed strings above, values are always bound parameters
            f"WHERE {where} "
            "ORDER BY created_at DESC, proposal_id DESC, proposal_version DESC "
            "LIMIT :page_size"
        ),
        params,
    )
    return [_version_row(row) for row in rows]


__all__ = [
    "FamilyRow",
    "ThreadRow",
    "VersionRow",
    "get_or_create_thread",
    "insert_family",
    "insert_version",
    "list_versions",
    "list_versions_for_thread",
    "load_family",
    "load_family_for_update",
    "load_latest_version",
    "load_thread",
    "load_version",
    "load_version_by_revision_id",
    "lock_thread",
    "transition_version",
    "update_semantics",
]
