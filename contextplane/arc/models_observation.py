"""Shadow-observation, qualification, and replay ORM models: the five
tables `0011_arc_observation.py` creates.

Sibling of `contextplane/arc/models.py`, same reason `models_risk_envelope.py`
and every other sibling in this package is: `models.py` is already at the
repo-wide 800-line ceiling `scripts/check_file_sizes.py` enforces, and this
phase's convention is to split up front rather than retrofit a split once
the file is already over the line. `models.py` imports and re-exports these
five classes into `ARC_MODELS` so the schema round-trip test and
`contextplane/storage/migrations/env.py`'s autogenerate import still only need
to look in one place.

See the migration's own module docstring for why `arc_observation_cohort_
members`'s `PRIMARY KEY (cohort_id, tenant_id)` is the leak-prevention
control rather than an incidental key choice, and why the qualifications
table's binding-tuple constraint is `UNIQUE NULLS NOT DISTINCT` rather than
a plain `UNIQUE`.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base

# Named independently of `models.py`'s own `_TS`, matching every other
# sibling module's stated reason: importing a private, underscore-prefixed
# name across sibling modules would make this module's own re-export
# boundary less clean than the one extra line this costs.
_TS = DateTime(timezone=True)


class ArcObservationCohort(Base):
    """One frozen `arc_observation_cohort_v1` record per candidate proposal
    version, plus the two closing-boundary bookkeeping columns the wire
    profile does not carry -- see the migration's own docstring for what
    `closed_at`/`window_ended_at` mean and why they move together.
    """

    __tablename__ = "arc_observation_cohorts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    cohort_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=False
    )
    risk_classification: Mapped[str] = mapped_column(Text, nullable=False)
    scope_predicate_digest: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_membership_digest: Mapped[str] = mapped_column(Text, nullable=False)
    eligibility_predicate_digest: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    window_started_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    window_deadline: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    window_ended_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    closed_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)


class ArcObservationCohortMember(Base):
    """Tenant membership for one cohort. `PRIMARY KEY (cohort_id,
    tenant_id)` -- see the migration's own module docstring for why this
    key is the leak-prevention control: no other table or query surface
    ever holds or projects a member tenant id back out of a global read.
    """

    __tablename__ = "arc_observation_cohort_members"

    cohort_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_observation_cohorts.cohort_id"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), primary_key=True)
    added_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcObservationResult(Base):
    """Bounded counters and digests only -- no manifest, repository
    identity, session id, or task summary column exists on this table, per
    ADR 041's "fingerprints, never manifests" rule. One row per `(cohort_id,
    tenant_id)`, even for a global cohort: a global qualification's
    aggregate view is a `SUM()` over these rows (see `queries/
    observation.py::load_aggregate_counters`), never a second, untenanted
    row that would otherwise be the one place unaggregated detail could
    hide.
    """

    __tablename__ = "arc_observation_results"

    cohort_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_observation_cohorts.cohort_id"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), primary_key=True)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unexplained_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    out_of_envelope_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    counters_by_delta_code: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    # Per-observation-class-digest granularity, never a manifest -- the one
    # column `observation_fingerprint_reaper.py` clears 30 days after the
    # owning cohort closes. `legal_hold_at` suspends that clearing.
    fingerprint_digests: Mapped[Any] = mapped_column(JSONB, nullable=False, default=list)
    legal_hold_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    fingerprints_reaped_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcObservationReplayCorpus(Base):
    """One approved `arc_observation_replay_corpus_v1` record. Created
    before `ArcObservationQualification` in the migration because that
    table's `replay_corpus_digest` column carries a FK to this table's own
    `canonical_corpus_digest`.
    """

    __tablename__ = "arc_observation_replay_corpora"

    corpus_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    generator_version: Mapped[str] = mapped_column(Text, nullable=False)
    generator_input_digest: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_corpus_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    fixture_class_count: Mapped[int] = mapped_column(Integer, nullable=False)
    owning_scope: Mapped[str] = mapped_column(Text, nullable=False)
    target_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    approving_authority_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    approving_authority_subject: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcObservationQualification(Base):
    """The durable, signed `arc_observation_qualification_v1` record.

    `qualification_id` is the primary key *and* carries its own explicit
    `unique=True` -- the migration restates it as a named constraint for the
    same reason `0009_arc_approval_challenges.py` restates `approval_
    challenge_id`: the TDD names it as a discrete property to prove, not an
    incidental consequence of the key choice. The eight-column binding
    tuple's `UNIQUE NULLS NOT DISTINCT` constraint is declared in the
    migration only -- SQLAlchemy's declarative layer has no `nulls_not_
    distinct` argument on `UniqueConstraint` as of this codebase's pinned
    SQLAlchemy version, matching every other migration-only constraint this
    package already accepts (partial indexes, CHECKs spanning several
    columns).
    """

    __tablename__ = "arc_observation_qualifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    qualification_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, unique=True)
    idempotency_key_digest: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_review_package_digest: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=False
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_classification: Mapped[str] = mapped_column(Text, nullable=False)
    risk_algorithm_version: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=True
    )
    selection_engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    engine_configuration_version: Mapped[str] = mapped_column(Text, nullable=False)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_observation_cohorts.cohort_id"), nullable=False
    )
    cohort_digest: Mapped[str] = mapped_column(Text, nullable=False)
    window_started_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    window_ended_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_impact_envelope_digest: Mapped[str] = mapped_column(Text, nullable=False)
    counters_by_delta_code: Mapped[Any] = mapped_column(JSONB, nullable=False, default=list)
    unexplained_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    out_of_envelope_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replay_corpus_digest: Mapped[str | None] = mapped_column(
        Text, ForeignKey("arc_observation_replay_corpora.canonical_corpus_digest"), nullable=True
    )
    replay_result_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualification_algorithm_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_decision: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    accepted_by_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_by_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    acceptance_audit_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)


__all__ = [
    "ArcObservationCohort",
    "ArcObservationCohortMember",
    "ArcObservationQualification",
    "ArcObservationReplayCorpus",
    "ArcObservationResult",
]
