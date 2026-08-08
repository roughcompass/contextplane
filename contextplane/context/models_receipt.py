"""ORM rows for context receipts, their per-arm state, and their items.

Separate module from `models.py` so the reference tables and the receipt tables
can be imported independently — the assembler needs references without receipts,
and the receipt reader needs receipts without a reason to pull in the reference
mapper. Both declare against the same `Base`, so one metadata object still
describes the whole database.

Mirrors `0032_context_receipts`. The migration owns the constraints and this
file owns the Python view of the same rows; restating a CHECK here would create
a second place to change it, and the one that stops matching is the one nobody
reruns.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class ContextReceipt(Base):
    """One resolution, and what it was worth as a whole."""

    __tablename__ = "context_receipts"

    receipt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    # Opaque here on purpose: no foreign key, so a receipt can outlive or be
    # deleted independently of the task it describes.
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    state: Mapped[str] = mapped_column(Text, nullable=False)
    cacheable: Mapped[bool] = mapped_column(Boolean, nullable=False)

    resolved_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)


class ContextReceiptArm(Base):
    """What one of the four arms did in one resolution.

    A row rather than four columns on the receipt: a column layout has nowhere
    to put the reason a single arm degraded, and adding an arm would be a schema
    change on the receipt itself.
    """

    __tablename__ = "context_receipt_arms"

    arm_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("context_receipts.receipt_id", ondelete="CASCADE"), nullable=False
    )

    block: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    # Absent exactly when the arm succeeded or was truthfully empty.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContextReceiptItem(Base):
    """One line of a receipt: an item an arm contributed.

    `receipt_item_id` is the contract's stable digest of block, source and item
    key, not a position — a positional id would move every time an unrelated
    item was added, and a citation into the receipt would rot.
    """

    __tablename__ = "context_receipt_items"

    item_row_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("context_receipts.receipt_id", ondelete="CASCADE"), nullable=False
    )

    receipt_item_id: Mapped[str] = mapped_column(Text, nullable=False)

    block: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    item_key: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = ["ContextReceipt", "ContextReceiptArm", "ContextReceiptItem"]
