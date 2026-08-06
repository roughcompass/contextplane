"""Parametrized SQL for the one write submission's materialisation
transaction makes across two tables: inserting the draft `arc_revisions`
row a proposal version submits into, and the compare-and-swap that freezes
the version and writes the bijection link to it.

Sibling of `queries/proposal.py`, deliberately not added to it: that module
owns the proposal aggregate (`arc_authoring_proposals`, `arc_authoring_
proposal_versions`) and is under active development for candidate-semantics
storage. This module owns the one write that crosses from a proposal
version into `arc_revisions` -- the table the bijection column points at --
so the two files never need to change for the same reason. Every function
here takes an already-open `AsyncSession` and commits nothing itself,
matching `queries/proposal.py`'s own convention: the caller controls the
transaction boundary.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class DraftRevision:
    """The columns `insert_draft_revision` writes, named once so the
    service module building them and the SQL consuming them cannot drift
    apart silently through a growing keyword-argument list."""

    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    tenant_id: uuid.UUID | None
    source_system: str
    source_canonical_locator: str
    source_revision_locator: str
    content_digest: str
    effective_from: datetime.datetime
    review_expires_at: datetime.datetime
    detail_audience: str
    freshness_basis: str
    content_classification: str
    content_retention_until: datetime.datetime
    created_at: datetime.datetime


async def insert_draft_revision(session: AsyncSession, draft: DraftRevision) -> None:
    """Insert the one `arc_revisions` row a submission materialises.

    `lifecycle_state` is not a parameter: every row this function writes is
    a fresh draft, and the column's own DEFAULT already says so -- naming it
    explicitly here would just be a second place that could someday say
    something else. `content_storage_mode` is always `'none'`: the
    candidate this row was materialised from lives on `arc_authoring_
    proposal_versions.semantics`, read through the bijection this same
    transaction writes, not duplicated into a body column here.
    """
    await session.execute(
        text(
            "INSERT INTO arc_revisions ("
            "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
            "  source_revision_locator, content_digest, effective_from, review_expires_at,"
            "  detail_audience, freshness_basis, content_classification, content_retention_until,"
            "  content_storage_mode, created_at"
            ") VALUES ("
            "  :revision_id, :artifact_id, :tenant_id, :source_system, :source_canonical_locator,"
            "  :source_revision_locator, :content_digest, :effective_from, :review_expires_at,"
            "  :detail_audience, :freshness_basis, :content_classification, :content_retention_until,"
            "  'none', :created_at"
            ")"
        ),
        {
            "revision_id": draft.revision_id,
            "artifact_id": draft.artifact_id,
            "tenant_id": draft.tenant_id,
            "source_system": draft.source_system,
            "source_canonical_locator": draft.source_canonical_locator,
            "source_revision_locator": draft.source_revision_locator,
            "content_digest": draft.content_digest,
            "effective_from": draft.effective_from,
            "review_expires_at": draft.review_expires_at,
            "detail_audience": draft.detail_audience,
            "freshness_basis": draft.freshness_basis,
            "content_classification": draft.content_classification,
            "content_retention_until": draft.content_retention_until,
            "created_at": draft.created_at,
        },
    )


@dataclasses.dataclass(frozen=True)
class FrozenVersion:
    """What `freeze_and_link` hands back on a won compare-and-swap."""

    proposal_id: uuid.UUID
    proposal_version: int
    state: str
    revision_id: uuid.UUID
    frozen_at: datetime.datetime


async def freeze_and_link(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    revision_id: uuid.UUID,
    now: datetime.datetime,
) -> FrozenVersion | None:
    """The compare-and-swap: `open` with no prior freeze, to `submitted`
    with the bijection link set, in the same statement that decides it.

    Mirrors `queries.proposal.transition_version`'s own shape -- a bare
    `UPDATE ... WHERE ... RETURNING`, no separate `SELECT ... FOR UPDATE` --
    for the same reason: the `WHERE` clause's row lock at execution time is
    the whole mechanism, and a caller racing this statement against another
    submit always resolves to exactly one winner. Returns `None` on a lost
    race or an already-frozen row; the caller decides what that means.
    """
    row = (
        await session.execute(
            text(
                "UPDATE arc_authoring_proposal_versions SET "
                "  state = 'submitted', frozen_at = :now, revision_id = :revision_id "
                "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version "
                "  AND state = 'open' AND frozen_at IS NULL "
                "RETURNING proposal_id, proposal_version, state, revision_id, frozen_at"
            ),
            {
                "proposal_id": proposal_id,
                "proposal_version": proposal_version,
                "revision_id": revision_id,
                "now": now,
            },
        )
    ).one_or_none()
    if row is None:
        return None
    return FrozenVersion(
        proposal_id=row.proposal_id,
        proposal_version=row.proposal_version,
        state=row.state,
        revision_id=row.revision_id,
        frozen_at=row.frozen_at,
    )


__all__ = ["DraftRevision", "FrozenVersion", "freeze_and_link", "insert_draft_revision"]
