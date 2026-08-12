"""ORM rows for context receipts, their per-arm state, and their items.

Separate module from `models.py` so the reference tables and the receipt tables
can be imported independently — the assembler needs references without receipts,
and the receipt reader needs receipts without a reason to pull in the reference
mapper. Both declare against the same `Base`, so one metadata object still
describes the whole database.

Mirrors `0032_context_receipts` and `0033_receipt_evidence`. The migration owns the constraints and this
file owns the Python view of the same rows; restating a CHECK here would create
a second place to change it, and the one that stops matching is the one nobody
reruns.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
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
    intent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    state: Mapped[str] = mapped_column(Text, nullable=False)
    cacheable: Mapped[bool] = mapped_column(Boolean, nullable=False)

    resolved_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)

    # The request this answered, digested. Without it a reader can only compare
    # what came back, which differs for reasons unrelated to the question asked.
    request_digest: Mapped[str | None] = mapped_column(Text, nullable=True)


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

    # What the arm cost. `considered` and `returned` are both kept because their
    # difference is the story: three of three and three of nine hundred are not
    # the same answer, and the state alone cannot tell them apart.
    considered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    returned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Two truncations, kept apart: the arm stopped at its own limit, or the
    # assembler cut it at the cap. Tuning one needs to know which happened.
    truncated_by_arm: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    truncated_by_cap: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # NULL freshness means the arm does not track it, which is not the same as
    # fresh -- so staleness is stored rather than derived from a NULL here.
    fresh_as_of: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


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

    # The frozen trust contract's eight fields, one column each. Typed rather
    # than a JSONB blob because the release gate has to assert label coverage
    # across every surface, and a gate cannot assert coverage over a blob
    # without reimplementing the schema inside the gate.
    #
    # `trust_source` rather than `source`: this table already has a `source`,
    # carrying the receipt item id's own component, and two columns of that name
    # in one row would be read wrong exactly once.
    trust: Mapped[str | None] = mapped_column(Text, nullable=True)
    trust_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    assertion_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    authority: Mapped[str | None] = mapped_column(Text, nullable=True)
    freshness: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mutability: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which exact record this came from. Without these a receipt names a
    # document, and the document has since changed.
    source_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_digest: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContextReceiptExclusion(Base):
    """One item an arm found and deliberately did not return.

    A child table because an arm has zero or many, so no column can hold them,
    and because "what did this block withhold" has to be one indexed read -- it
    is the query a reader runs when an answer looks thinner than expected.

    This is the row that distinguishes "there was nothing" from "there was
    something you may not see". Only the second tells a reader to go and ask
    somebody, and it cannot be reconstructed afterwards: the envelope carries
    what survived.
    """

    __tablename__ = "context_receipt_exclusions"

    exclusion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("context_receipts.receipt_id", ondelete="CASCADE"), nullable=False
    )

    block: Mapped[str] = mapped_column(Text, nullable=False)
    item_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Required. A withheld item with no reason tells a reader something was kept
    # back and gives them no way to know whether to ask for access or report a bug.
    reason: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "ContextReceipt",
    "ContextReceiptArm",
    "ContextReceiptExclusion",
    "ContextReceiptItem",
]
