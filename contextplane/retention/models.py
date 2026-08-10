"""ORM rows for retention policy, tombstones, derivatives, legal holds and privacy-safe aggregates.

Declared against the shared storage `Base` so one metadata object still describes
the whole database, and kept here rather than grown into `storage/models.py`,
which is already at its size waiver.

These mirror `0043_retention_and_derivatives` and, for the three hold tables,
`0046_legal_holds`. The migration owns the constraints
and this file owns the Python view of the same rows; a drift between them is what
the parity test catches. Constraints are deliberately not re-declared here — and
for two of them that separation matters more than usual. The aggregation floor and
the one-version-per-cell uniqueness are the schema's own defences against a
differencing attack; restating either in Python would create a second copy that
can be relaxed without touching the one the database enforces.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class RetentionPolicy(Base):
    """One record class's disposition under one policy version.

    `retention_days` is NULL when the period is event-bounded rather than a
    duration — "life of tenant" is a different statement from "no limit", and
    storing a very large number for it would make the two indistinguishable.
    """

    __tablename__ = "retention_policies"

    policy_version: Mapped[str] = mapped_column(Text, primary_key=True)
    record_class: Mapped[str] = mapped_column(Text, primary_key=True)

    legal_basis: Mapped[str] = mapped_column(Text, nullable=False)
    retention_days: Mapped[int | None] = mapped_column(Integer)
    erasure_mode: Mapped[str] = mapped_column(Text, nullable=False)

    minimization_action: Mapped[str | None] = mapped_column(Text)
    tombstone_behaviour: Mapped[str | None] = mapped_column(Text)
    verifier_disclosure: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceTombstone(Base):
    """The record that something was erased, holding no part of what was erased.

    `proof_hmac` is tenant-keyed, never a bare content digest: erased content is
    often guessable, and a bare hash would let anyone who can guess it confirm the
    guess while equal prefixes revealed equality across erased records.
    """

    __tablename__ = "source_tombstones"

    tombstone_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    record_class: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    policy_version: Mapped[str] = mapped_column(Text, nullable=False)

    request_authority: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    effective_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    proof_hmac: Mapped[str] = mapped_column(Text, nullable=False)

    propagation_state: Mapped[str] = mapped_column(Text, nullable=False)


class DerivativeRegistration(Base):
    """One derived artefact, and everything needed to rebuild, redact or delete it.

    `expires_at` is never NULL and is the minimum across every source link: a
    derivative must not outlive any source it was built from, and an unbounded
    one is exactly the case that does so without anybody noticing.
    """

    __tablename__ = "derivative_registrations"

    derivative_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    derivative_kind: Mapped[str] = mapped_column(Text, nullable=False)
    storage_locator: Mapped[str] = mapped_column(Text, nullable=False)

    audience_partition: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)

    rebuild_handler_version: Mapped[str] = mapped_column(Text, nullable=False)
    delete_handler_version: Mapped[str] = mapped_column(Text, nullable=False)
    redact_handler_version: Mapped[str] = mapped_column(Text, nullable=False)

    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False)

    last_synchronized_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    sync_status: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DerivativeSourceLink(Base):
    """One source a derivative was built from — every one of them, not the triggering one.

    Retention is the minimum across these links, which a single source column on
    the registration could not express.
    """

    __tablename__ = "derivative_source_links"

    link_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    derivative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("derivative_registrations.derivative_id"), nullable=False
    )

    source_record_class: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # NULL means the source is not revisioned, not that the revision is unknown.
    source_revision: Mapped[str | None] = mapped_column(Text)
    source_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class DerivativeWorkItem(Base):
    """One unit of propagation work, idempotent per cause.

    Uniqueness on (derivative, operation, trigger, tombstone) with NULLs treated
    as equal is what makes a repeated sweep enqueue nothing new; without that a
    trigger with no tombstone would re-enqueue on every pass.
    """

    __tablename__ = "derivative_work_outbox"

    work_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    derivative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("derivative_registrations.derivative_id"), nullable=False
    )

    operation: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    tombstone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_tombstones.tombstone_id")
    )

    state: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    available_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class LegalHold(Base):
    """One placed hold: what it covers, who placed it, and when it must be reviewed.

    `review_date` is what keeps the hold alive rather than a flag, because a hold
    that stays active until somebody clears it is how a hold becomes permanent.
    The 180-day ceiling and the one-hold-per-record uniqueness are the migration's
    constraints and are deliberately not restated here: a second copy in Python is
    one that can be relaxed without touching the one the database enforces.

    `renewal_count` is the hold's current position, not its history. The trail is
    two rows per renewal in the tables below, which is what "a renewal is never one
    audit row" means in storage.
    """

    __tablename__ = "legal_holds"

    hold_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    record_class: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    placed_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    placed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review_date: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    renewal_count: Mapped[int] = mapped_column(Integer, nullable=False)


class LegalHoldRenewal(Base):
    """The re-justification half of a renewal: why this hold still has to exist.

    Separate from the hold so a renewal cannot overwrite the reasoning that
    justified the previous one. `previous_review_date` and `new_review_date` are
    both stored because the question a reviewer asks later is how far the clock was
    pushed, which a single date cannot answer.
    """

    __tablename__ = "legal_hold_renewals"

    renewal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    hold_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_holds.hold_id", ondelete="CASCADE"), nullable=False
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)

    previous_review_date: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    new_review_date: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LegalHoldApproval(Base):
    """The approval half of a renewal: who signed it off, and how senior that is.

    `approval_rank` carries the escalation as a number so "higher than the last
    renewal" is a comparison rather than a lookup. The name is stored beside it
    because a rank with no label is unreadable in an audit six months later.
    """

    __tablename__ = "legal_hold_approvals"

    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    renewal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_hold_renewals.renewal_id", ondelete="CASCADE"), nullable=False
    )

    approved_by: Mapped[str] = mapped_column(Text, nullable=False)
    approval_level: Mapped[str] = mapped_column(Text, nullable=False)
    approval_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PrivacyAggregate(Base):
    """One aggregate cell, of which only one version may ever exist.

    That single-version rule is the differencing defence: a reader who saw a cell
    before an erasure and again after would recover the erased subject's exact
    contribution by subtraction, so a recompute must destroy its predecessor. The
    unique key makes that structural rather than a step somebody remembers.
    """

    __tablename__ = "privacy_aggregates"

    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    cohort_key: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    actor_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL exactly when the cell is suppressed.
    value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    suppressed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False)

    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
