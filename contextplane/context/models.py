"""ORM rows for normalized external references and their bindings.

Declared against the same `Base` as the rest of the schema so one metadata
object still describes the whole database — a second base would give the parity
check two answers about what exists.

These mirror `0031_external_references`, and the mirroring is the contract: the
migration owns the constraints, this file owns the Python view of the same rows,
and a drift between them is what the parity test in this task exists to catch.
Constraints are deliberately *not* re-declared here. Restating a CHECK in the
ORM would create a second place to change it, and the one that stops matching is
the one nobody reruns.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class ContextExternalReference(Base):
    """One external thing, however many times it is cited.

    `collision_key` is written by the service from the contract's own digest
    rather than derived in SQL, so the database and the code cannot disagree
    about which two references are the same one.
    """

    __tablename__ = "context_external_references"

    reference_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    # Normalized to lowercase before the write.
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_namespace: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Trimmed only: the case belongs to the system that issued it.
    external_id: Mapped[str] = mapped_column(Text, nullable=False)

    classification: Mapped[str] = mapped_column(Text, nullable=False)
    external_authority: Mapped[str] = mapped_column(Text, nullable=False)

    # Absent where absence is the meaning: no revision recorded, no authorized
    # URI to hand a reader, no observation time the source could report.
    revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorized_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    collision_key: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextReferenceBinding(Base):
    """One subject citing one reference.

    Carries no payload of its own. Anything recorded here about *why* the
    subject cited it would be a second place to look for provenance the
    reference and the subject already carry between them.
    """

    __tablename__ = "context_reference_bindings"

    binding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    reference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("context_external_references.reference_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Closed set in the migration's CHECK. Kept a plain string here rather than
    # an enum so the database stays the one place the set is declared.
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    bound_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["ContextExternalReference", "ContextReferenceBinding"]
