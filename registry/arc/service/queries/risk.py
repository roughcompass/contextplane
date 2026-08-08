"""Parametrized SQL for the risk-classification and expected-impact-envelope
tables `RiskEnvelopeValidator.assess_and_persist` writes, in the caller's own
transaction, once at submission.

Both tables live in one queries module, unlike most sibling queries files
that own a single aggregate: `arc_risk_classifications` and
`arc_expected_impact_envelopes`/`arc_expected_impact_envelope_items` are
written together, exactly once, by exactly one caller
(`RiskEnvelopeValidator`), and never independently -- there is no future
caller that writes one without the other, so a split would only be a second
file with no cohesion boundary behind it. Every function here takes an
already-open `AsyncSession` and commits nothing itself, matching
`queries/materialisation.py`'s own convention: the caller controls the
transaction boundary.
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


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# arc_risk_classifications -- the sticky result + algorithm version.
# ---------------------------------------------------------------------------


async def insert_risk_classification(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    classification: str,
    algorithm_version: str,
    computed_at: datetime.datetime,
) -> None:
    """Insert the one durable, sticky risk-classification row a submission
    ever writes for this proposal version.

    Deliberately separate from `set_proposal_version_risk` below rather
    than trusting the two summary columns on `arc_authoring_proposal_
    versions` alone: those columns are a read-path cache
    (`ProposalVersionResponse`/`ProposalSummary` read them directly, no
    join), while this row is the immutable record later recomputation
    (approval, qualification, activation) compares its own fresh result
    against. A future rewrite of the summary columns must never rewrite
    this one.
    """
    await session.execute(
        text(
            "INSERT INTO arc_risk_classifications ("
            "  proposal_id, proposal_version, classification, algorithm_version, computed_at"
            ") VALUES (:proposal_id, :proposal_version, :classification, :algorithm_version, :computed_at)"
        ),
        {
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "classification": classification,
            "algorithm_version": algorithm_version,
            "computed_at": computed_at,
        },
    )


async def set_proposal_version_risk(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    classification: str,
    algorithm_version: str,
) -> None:
    """The read-path cache: `arc_authoring_proposal_versions.risk_
    classification`/`.risk_algorithm_version`, columns added by migration
    `0005_arc_authoring_proposals.py` and already read by
    `ProposalVersionResponse`/`ProposalSummary` -- this is the one write
    that ever populates them.
    No `WHERE state = ...` guard: the caller already holds this row inside
    the same won compare-and-swap `freeze_and_link` performed moments
    earlier in the same transaction.
    """
    await session.execute(
        text(
            "UPDATE arc_authoring_proposal_versions SET "
            "  risk_classification = :classification, risk_algorithm_version = :algorithm_version "
            "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version"
        ),
        {
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "classification": classification,
            "algorithm_version": algorithm_version,
        },
    )


# ---------------------------------------------------------------------------
# arc_expected_impact_envelopes / arc_expected_impact_envelope_items
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class EnvelopeItemRow:
    """One `arc_expected_impact_envelope_items` row, named once so the
    service module building them and the SQL consuming them cannot drift
    apart silently through a growing keyword-argument list."""

    item_id: str
    delta_code: str
    class_predicate: dict[str, Any]
    minimum_count: int
    maximum_count: int | None
    rationale_code: str


async def insert_envelope(
    session: AsyncSession,
    *,
    envelope_id: uuid.UUID,
    proposal_id: uuid.UUID,
    proposal_version: int,
    envelope_digest: str,
    author_issuer: str,
    author_subject: str,
    created_at: datetime.datetime,
    items: Sequence[EnvelopeItemRow],
) -> None:
    """Insert the one envelope row and every one of its items.

    The envelope row first: the items' FK references it. `UNIQUE
    (proposal_id, proposal_version)` on the envelope table is what makes a
    second submission attempt against the same version collide here rather
    than silently freezing a second envelope for one version -- the same
    "N+1 opens only after N is terminal" invariant the bijection itself
    already enforces.
    """
    await session.execute(
        text(
            "INSERT INTO arc_expected_impact_envelopes ("
            "  envelope_id, proposal_id, proposal_version, envelope_digest, author_issuer, author_subject,"
            "  created_at"
            ") VALUES ("
            "  :envelope_id, :proposal_id, :proposal_version, :envelope_digest, :author_issuer, :author_subject,"
            "  :created_at"
            ")"
        ),
        {
            "envelope_id": envelope_id,
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "envelope_digest": envelope_digest,
            "author_issuer": author_issuer,
            "author_subject": author_subject,
            "created_at": created_at,
        },
    )
    for item in items:
        await session.execute(
            text(
                "INSERT INTO arc_expected_impact_envelope_items ("
                "  envelope_id, item_id, delta_code, class_predicate, minimum_count, maximum_count,"
                "  rationale_code"
                ") VALUES ("
                "  :envelope_id, :item_id, :delta_code, CAST(:class_predicate AS JSONB), :minimum_count,"
                "  :maximum_count, :rationale_code"
                ")"
            ),
            {
                "envelope_id": envelope_id,
                "item_id": item.item_id,
                "delta_code": item.delta_code,
                "class_predicate": _json(item.class_predicate),
                "minimum_count": item.minimum_count,
                "maximum_count": item.maximum_count,
                "rationale_code": item.rationale_code,
            },
        )


__all__ = [
    "EnvelopeItemRow",
    "insert_envelope",
    "insert_risk_classification",
    "set_proposal_version_risk",
]
