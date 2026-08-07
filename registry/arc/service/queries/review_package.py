"""Parametrized SQL for `ReviewPackageService.assemble`'s read-only inputs:
the sticky risk classification, the frozen expected-impact envelope and its
items, the one-time submission-identity record, the baseline version's own
frozen semantics (for the diff), and per-field reach confirmations.

Every function here takes an already-open `AsyncSession` and writes nothing
-- `assemble` never persists, it only recomputes from what earlier tasks
already froze. Matching every other queries module in this package: no
transaction is opened or committed here, the caller controls that.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RiskClassificationRow:
    classification: str
    algorithm_version: str
    computed_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class EnvelopeItemRow:
    item_id: str
    delta_code: str
    class_predicate: dict[str, Any]
    minimum_count: int
    maximum_count: int | None
    rationale_code: str


@dataclasses.dataclass(frozen=True)
class EnvelopeRow:
    envelope_id: uuid.UUID
    envelope_digest: str
    author_issuer: str
    author_subject: str
    created_at: datetime.datetime
    items: tuple[EnvelopeItemRow, ...]


@dataclasses.dataclass(frozen=True)
class SubmissionIdentityRow:
    submitted_by_issuer: str
    submitted_by_subject: str


@dataclasses.dataclass(frozen=True)
class ReachConfirmationRow:
    field_path: str
    confirmed: bool
    confirmed_at: datetime.datetime | None
    confirmed_by_issuer: str | None
    confirmed_by_subject: str | None


# ---------------------------------------------------------------------------
# arc_risk_classifications -- the sticky, immutable result later
# recomputation compares its own fresh classification against.
# ---------------------------------------------------------------------------


async def load_risk_classification(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int
) -> RiskClassificationRow | None:
    row = (
        await session.execute(
            text(
                "SELECT classification, algorithm_version, computed_at FROM arc_risk_classifications "
                "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version"
            ),
            {"proposal_id": proposal_id, "proposal_version": proposal_version},
        )
    ).one_or_none()
    if row is None:
        return None
    return RiskClassificationRow(
        classification=row.classification, algorithm_version=row.algorithm_version, computed_at=row.computed_at
    )


# ---------------------------------------------------------------------------
# arc_expected_impact_envelopes / arc_expected_impact_envelope_items -- the
# frozen envelope plus its items, read back so the review package can
# reconstruct and recompute the canonical digest rather than trust the
# persisted `envelope_digest` column.
# ---------------------------------------------------------------------------


async def load_envelope(session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int) -> EnvelopeRow | None:
    envelope = (
        await session.execute(
            text(
                "SELECT envelope_id, envelope_digest, author_issuer, author_subject, created_at "
                "FROM arc_expected_impact_envelopes WHERE proposal_id = :proposal_id AND proposal_version = :pv"
            ),
            {"proposal_id": proposal_id, "pv": proposal_version},
        )
    ).one_or_none()
    if envelope is None:
        return None
    item_rows = await session.execute(
        text(
            "SELECT item_id, delta_code, class_predicate, minimum_count, maximum_count, rationale_code "
            "FROM arc_expected_impact_envelope_items WHERE envelope_id = :envelope_id ORDER BY item_id"
        ),
        {"envelope_id": envelope.envelope_id},
    )
    items = tuple(
        EnvelopeItemRow(
            item_id=item.item_id,
            delta_code=item.delta_code,
            class_predicate=item.class_predicate,
            minimum_count=item.minimum_count,
            maximum_count=item.maximum_count,
            rationale_code=item.rationale_code,
        )
        for item in item_rows
    )
    return EnvelopeRow(
        envelope_id=envelope.envelope_id,
        envelope_digest=envelope.envelope_digest,
        author_issuer=envelope.author_issuer,
        author_subject=envelope.author_subject,
        created_at=envelope.created_at,
        items=items,
    )


# ---------------------------------------------------------------------------
# Submission identity. `arc_authoring_proposal_versions` has no column for
# who called `submit` -- only `frozen_at` (when). `ArtifactMaterialisation
# Service.submit` writes the authenticated submitter's issuer/subject into
# the same-transaction `arc.proposal.submitted` audit-outbox event and
# nowhere else durable. See `review_package.py`'s own module docstring for
# why this is a deliberate, reported compromise rather than a silent
# workaround: the outbox is documented elsewhere in this codebase as
# drain-worker-only, and this is the one read path that reaches back into it.
# ---------------------------------------------------------------------------


async def load_submission_identity(
    session: AsyncSession, *, event_type: str, proposal_id: uuid.UUID, proposal_version: int
) -> SubmissionIdentityRow | None:
    """The one `arc.proposal.submitted` outbox row for this exact version.

    Matched on the payload's own `proposal_id`/`proposal_version` fields
    rather than any indexed column -- there is no index over `event_payload`
    for this lookup, which is exactly the tradeoff this module's docstring
    names. `proposal_id` is a UUID and `event_type` narrows the scan to one
    event class, so this returns at most one row regardless of tenant.
    """
    row = (
        await session.execute(
            text(
                "SELECT event_payload->>'submitted_by_issuer' AS submitted_by_issuer,"
                "       event_payload->>'submitted_by_subject' AS submitted_by_subject "
                "FROM arc_audit_outbox "
                "WHERE event_type = :event_type "
                "  AND event_payload->>'proposal_id' = :proposal_id "
                "  AND (event_payload->>'proposal_version')::int = :proposal_version "
                "ORDER BY created_at ASC LIMIT 1"
            ),
            {"event_type": event_type, "proposal_id": str(proposal_id), "proposal_version": proposal_version},
        )
    ).one_or_none()
    if row is None or row.submitted_by_issuer is None or row.submitted_by_subject is None:
        return None
    return SubmissionIdentityRow(
        submitted_by_issuer=row.submitted_by_issuer, submitted_by_subject=row.submitted_by_subject
    )


# ---------------------------------------------------------------------------
# Baseline diff support: the baseline is named by revision_id (the reviewed
# revision), not by (proposal_id, proposal_version) -- read back whichever
# proposal version's bijection points at it, and hand back its own frozen
# candidate document.
# ---------------------------------------------------------------------------


async def load_semantics_by_revision_id(session: AsyncSession, revision_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT semantics FROM arc_authoring_proposal_versions WHERE revision_id = :revision_id"),
            {"revision_id": revision_id},
        )
    ).one_or_none()
    if row is None or row.semantics is None:
        return None
    return dict(row.semantics)


# ---------------------------------------------------------------------------
# arc_authoring_reach_confirmations -- no writer exists yet (a later task's
# job); read back whatever is there so the review package never hides a
# confirmation a future writer records.
# ---------------------------------------------------------------------------


async def load_reach_confirmations(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int
) -> list[ReachConfirmationRow]:
    rows = await session.execute(
        text(
            "SELECT field_path, confirmed, confirmed_at, confirmed_by_issuer, confirmed_by_subject "
            "FROM arc_authoring_reach_confirmations WHERE proposal_id = :proposal_id AND proposal_version = :pv "
            "ORDER BY field_path"
        ),
        {"proposal_id": proposal_id, "pv": proposal_version},
    )
    return [
        ReachConfirmationRow(
            field_path=row.field_path,
            confirmed=row.confirmed,
            confirmed_at=row.confirmed_at,
            confirmed_by_issuer=row.confirmed_by_issuer,
            confirmed_by_subject=row.confirmed_by_subject,
        )
        for row in rows
    ]


__all__ = [
    "EnvelopeItemRow",
    "EnvelopeRow",
    "ReachConfirmationRow",
    "RiskClassificationRow",
    "SubmissionIdentityRow",
    "load_envelope",
    "load_reach_confirmations",
    "load_risk_classification",
    "load_semantics_by_revision_id",
    "load_submission_identity",
]
