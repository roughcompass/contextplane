"""Risk-classification and expected-impact-envelope ORM models: the three
tables `0010_arc_risk_and_envelopes.py` creates.

Sibling of `contextplane/arc/models.py`, same reason `models_proposal.py` and
`models_operational_chain.py` are: `models.py` is already close to the
repo-wide 800-line ceiling `scripts/check_file_sizes.py` enforces, and this
phase's convention (set by those two modules) is to split up front rather
than retrofit a split once the file is already over the line.
`models.py` imports and re-exports these three classes into `ARC_MODELS` so
the schema round-trip test and `contextplane/storage/migrations/env.py`'s
autogenerate import still only need to look in one place.

Global-capable, like the proposal aggregate in `models_proposal.py`: none of
the three carries its own `tenant_id` column or `TenantMixin`. Ownership
already lives on the `(proposal_id, proposal_version)` row every one of
these tables keys off of (or, for items, off the envelope that keys off of
it) -- a duplicate tenant column here would be a second place that could
disagree with the proposal version's own.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base

# Named independently of `models.py`'s own `_TS`, matching every other
# sibling module's stated reason: importing a private, underscore-prefixed
# name across sibling modules would make this module's own re-export
# boundary less clean than the one extra line this costs.
_TS = DateTime(timezone=True)


class ArcRiskClassification(Base):
    """The sticky, immutable risk-classification result and algorithm
    version a submission binds -- the record later recomputation (approval,
    qualification, activation) compares its own fresh result against,
    distinct from the read-path cache columns on `arc_authoring_proposal_
    versions` (`risk_classification`/`risk_algorithm_version`), which this
    same write also populates via a separate statement.
    """

    __tablename__ = "arc_risk_classifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    proposal_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcExpectedImpactEnvelope(Base):
    """One frozen `arc_expected_impact_envelope_v1` object per proposal
    version -- `UNIQUE (proposal_id, proposal_version)` is what makes a
    second submission attempt against an already-submitted version collide
    here rather than silently freezing a second envelope for it."""

    __tablename__ = "arc_expected_impact_envelopes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    envelope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(Text, nullable=False)
    author_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    author_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcExpectedImpactEnvelopeItem(Base):
    """One envelope item: one delta code, one class predicate, and the
    count boundaries/rationale that justify it. `class_predicate` stores
    the closed `arc_observation_class_predicate_v1` object verbatim -- the
    six approved selector dimensions only, already validated and
    canonicalized by `ExpectedImpactEnvelopeService` before this row is
    ever written."""

    __tablename__ = "arc_expected_impact_envelope_items"
    __table_args__ = (ForeignKeyConstraint(["envelope_id"], ["arc_expected_impact_envelopes.envelope_id"]),)

    envelope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    delta_code: Mapped[str] = mapped_column(Text, nullable=False)
    class_predicate: Mapped[Any] = mapped_column(JSONB, nullable=False)
    minimum_count: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rationale_code: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "ArcExpectedImpactEnvelope",
    "ArcExpectedImpactEnvelopeItem",
    "ArcRiskClassification",
]
