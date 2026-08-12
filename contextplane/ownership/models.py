"""ORM rows for ownership as an auditable assertion, and its transition trail.

Declared against the shared storage `Base` so one metadata object still describes
the whole database, and kept here rather than grown into `storage/models.py`,
which is already at its size waiver.

These mirror `0053_ownership_and_grants`. The migration owns the constraints and
this file owns the Python view of the same rows; a drift between them is what the
parity test catches.

**The absent foreign keys are the point.** `owner_principal`, `owned_target_kind`
and `owned_target_id` are plain columns, not references into any principal, actor
or entitlement table. Ownership records who is *accountable* for something; it is
never consulted to decide who may *change* it. A foreign key here would make the
two joinable, and the distance between "accountable" and "authorized" is exactly
what stops an audit field becoming an access-control decision. An absence cannot
be expressed as a constraint, so a test reads this table's live foreign keys and
refuses any that reach an identity-shaped table.

The lifecycle vocabulary and the legal moves between states are CHECK constraints
in the migration and are deliberately not restated here: a second copy in Python
is one that can be relaxed without touching the one the database enforces.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class OwnershipAssignment(Base):
    """One assertion that somebody is accountable for something.

    `derivation_method` and `confidence` are NULL together: a hand-asserted
    ownership has no inference confidence, and storing 1.0 for it would make a
    human's assertion indistinguishable from a machine that happened to be
    certain.

    `replaced_by_assignment_id` is set only on a superseded assignment, and
    `revocation_reason` only on a revoked one. Both endings stay readable forever
    — that is the reason the row ends here rather than being deleted.
    """

    __tablename__ = "ownership_assignments"

    ownership_assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    # Opaque by design. See the module docstring: a reference into an identity
    # table here would make ownership joinable to authorization.
    owner_principal: Mapped[str] = mapped_column(Text, nullable=False)
    owned_target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    owned_target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    role: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)

    source: Mapped[str] = mapped_column(Text, nullable=False)
    derivation_method: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)

    validation_state: Mapped[str] = mapped_column(Text, nullable=False)

    effective_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL means still in force, not unknown.
    effective_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    provenance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assertion_provenance.provenance_id"), nullable=False
    )

    replaced_by_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ownership_assignments.ownership_assignment_id")
    )
    revocation_reason: Mapped[str | None] = mapped_column(Text)

    recorded_by: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OwnershipAssignmentTransition(Base):
    """One recorded move in an assignment's lifecycle.

    Separate from the assignment so a later move cannot overwrite the reasoning
    that justified the previous one — an assignment in its third state would
    otherwise show one reason, with the trail that made it legitimate overwritten
    twice.

    `sequence` is unique per assignment, so a retry that lost its response cannot
    record the same move twice at the same position.
    """

    __tablename__ = "ownership_assignment_transitions"

    transition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ownership_assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ownership_assignments.ownership_assignment_id", ondelete="CASCADE"),
        nullable=False,
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    from_state: Mapped[str] = mapped_column(Text, nullable=False)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_by: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
