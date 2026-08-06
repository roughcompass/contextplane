"""Artifact-family and proposal-aggregate ORM models: the five tables
`0005_arc_authoring_proposals.py` creates, plus the four columns that same
migration adds to the pre-existing `arc_artifacts` table.

Sibling of `registry/arc/models.py`, same reason `models_source_admission.py`
is: `models.py` was already near the 800-line ceiling `scripts/
check_file_sizes.py` enforces repo-wide, so new mapped classes live here and
`models.py` imports and re-exports each one, folding them into `ARC_MODELS`.
`from registry.arc.models import ArcAuthoringProposal` keeps working for any
caller that does not need to know the split exists.

Two tables carry the real service surface this phase implements
(`ArcAuthoringProposal`, the thread; `ArcAuthoringProposalVersion`, where all
state lives). The other three (`ArcAuthoringFieldProvenance`,
`ArcAuthoringSemanticTest`, `ArcAuthoringReachConfirmation`) are declared here
so the migration's tables are never invisible to the ORM/schema round-trip
test the moment they exist -- their own service and query modules belong to
the tasks that populate them, not this one.

Global-capable, like `arc_artifacts`/`arc_revisions` in the sibling module:
`tenant_id` is nullable with no `TenantMixin`, because a proposal on a global
artifact has no owning tenant either.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.storage.models import Base

# Named independently of `models.py`'s own `_TS`, matching
# `models_source_admission.py`'s stated reason: importing a private,
# underscore-prefixed name across sibling modules would make this module's
# own re-export boundary less clean than the one extra line this costs.
_TS = DateTime(timezone=True)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True)


class ArcAuthoringProposal(Base):
    """A proposal thread: stable identity and sequence coordination only.

    Exactly one thread per artifact family (`artifact_id` is `UNIQUE`) --
    state never lives here. `open_proposal` gets-or-creates this row, locks
    it, and only then decides whether a new version may open, which is what
    serializes concurrent opens against the same family into one winner and
    one `arc_proposal_state_conflict`.
    """

    __tablename__ = "arc_authoring_proposals"

    proposal_id: Mapped[uuid.UUID] = _uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False, unique=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcAuthoringProposalVersion(Base):
    """Where all proposal state lives -- immutable content, mutable state.

    `revision_id` is the bijection column (`UNIQUE`, nullable until
    submission materialises exactly one draft revision). The partial unique
    index enforcing "one nonterminal candidate per thread" is declared in
    the migration, not here -- SQLAlchemy's declarative layer cannot express
    a `WHERE` clause on a plain `unique=True`.
    """

    __tablename__ = "arc_authoring_proposal_versions"

    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_authoring_proposals.proposal_id"), primary_key=True
    )
    proposal_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    source_evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_source_approval_evidence.source_evidence_id"), nullable=False
    )
    reviewed_baseline_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=True
    )
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=True, unique=True
    )
    risk_classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_algorithm_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_by_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    opened_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    frozen_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    terminal_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_by_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminalized_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)


class ArcAuthoringFieldProvenance(Base):
    """One `field_provenance_v1` record per semantic field instance.

    Declared here because the migration that creates it is this task's;
    `provenance.py`'s conditional-requiredness rule (which three-column
    group a `provenance_class` requires and forbids) is service-enforced,
    not a DDL CHECK -- see the migration's own comment on why.
    """

    __tablename__ = "arc_authoring_field_provenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    proposal_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_path: Mapped[str] = mapped_column(Text, primary_key=True)
    provenance_class: Mapped[str] = mapped_column(Text, nullable=False)
    source_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_source_approval_evidence.source_evidence_id"), nullable=True
    )
    source_anchor: Mapped[str | None] = mapped_column(Text, nullable=True)
    excerpt_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    derivation_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcAuthoringSemanticTest(Base):
    """One frozen semantic-test input/result pair per `test_id`."""

    __tablename__ = "arc_authoring_semantic_tests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    proposal_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest: Mapped[Any] = mapped_column(JSONB, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected: Mapped[Any] = mapped_column(JSONB, nullable=False)
    actual: Mapped[Any] = mapped_column(JSONB, nullable=False)
    executed_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcAuthoringReachConfirmation(Base):
    """Per-field confirmation state -- has this field's reach been reviewed."""

    __tablename__ = "arc_authoring_reach_confirmations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    proposal_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_path: Mapped[str] = mapped_column(Text, primary_key=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confirmed_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    confirmed_by_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "ArcAuthoringFieldProvenance",
    "ArcAuthoringProposal",
    "ArcAuthoringProposalVersion",
    "ArcAuthoringReachConfirmation",
    "ArcAuthoringSemanticTest",
]
