"""ORM rows for discriminated feedback.

Declared against the same `Base` as the rest of the schema so one metadata object
still describes the whole database — a second base would give the parity check two
answers about what exists.

These mirror `0041_discriminated_feedback`, and the mirroring is the contract: the
migration owns the constraints, this file owns the Python view of the same rows,
and a drift between them is what the parity test in this task exists to catch.
Constraints are deliberately *not* re-declared here. Restating the discriminant
CHECK in the ORM would create a second place to change it, and the one that stops
matching is the one nobody reruns — which for a union whose whole purpose is to
say what may be learned from a row is a worse failure than for most tables.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class ContextFeedback(Base):
    """One report about a resolution, in one of three shapes.

    `kind` is the discriminant and the migration enforces what each member may
    cite: item-specific needs a receipt and an exact item on it, receipt-level
    needs a receipt and no item, and a diagnostic observation cites neither and
    can never be learning-eligible.

    The `(receipt_id, receipt_item_id)` pair is a composite foreign key against
    the receipt's own items, so an item belonging to a different receipt cannot
    be cited. No relationship is declared: this table is written and read by
    explicit queries, and a lazy-loading attribute would issue those queries
    somewhere nothing expects a database call.
    """

    __tablename__ = "context_feedback"

    feedback_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    kind: Mapped[str] = mapped_column(Text, nullable=False)

    # Both nullable so the three members are expressible; which may be NULL is
    # decided by the discriminant constraint, not by the writer.
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    receipt_item_id: Mapped[str | None] = mapped_column(Text)

    rating: Mapped[str] = mapped_column(Text, nullable=False)
    learning_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Minimized rather than deleted under the retention policy, and never part
    # of a key.
    note: Mapped[str | None] = mapped_column(Text)

    reporter_id: Mapped[str] = mapped_column(Text, nullable=False)
    reporter_type: Mapped[str] = mapped_column(Text, nullable=False)

    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
