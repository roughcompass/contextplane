"""ORM rows for the external signal ledger.

Declared against the same `Base` as the rest of the schema so one metadata object
still describes the whole database — a second base would give the parity check two
answers about what exists.

These mirror `0040_external_signals`, and the mirroring is the contract: the
migration owns the constraints, this file owns the Python view of the same rows,
and a drift between them is what the parity test in this task exists to catch.
Constraints are deliberately *not* re-declared here. Restating a CHECK in the ORM
would create a second place to change it, and the one that stops matching is the
one nobody reruns.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class ExternalSignal(Base):
    """One observation a producer reported, kept as an observation.

    The two unique keys answer different questions and are both enforced in the
    migration: `source_event_id` identifies the external occurrence, while
    `idempotency_key` identifies the submission that reported it. `content_digest`
    is what makes a replay decidable without keeping the body to compare.
    """

    __tablename__ = "external_signals"

    signal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    # Absent where absence is the meaning: not every producer knows a team or a
    # project, and a placeholder would be grouped as though it did.
    team_key: Mapped[str | None] = mapped_column(Text)
    project_key: Mapped[str | None] = mapped_column(Text)

    # Normalized to lowercase before the write.
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    producer_id: Mapped[str] = mapped_column(Text, nullable=False)
    producer_type: Mapped[str] = mapped_column(Text, nullable=False)

    # Trimmed only: the case belongs to the system that issued it.
    source_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(Text, nullable=False)

    # What the source was entitled to assert, stored per signal rather than per
    # producer so a boundary narrowed later cannot rewrite what backed an old row.
    authority: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)

    # Three different instants. The first two are NULL when the source does not
    # publish them; `ingested_at` is server-assigned and always present.
    event_time: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    observed_time: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # NULL means no expiry was declared, which is not the same as never expires.
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    schema_version: Mapped[str] = mapped_column(Text, nullable=False)

    # Exactly one of these is set; the migration's CHECK is what enforces it.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_handle: Mapped[str | None] = mapped_column(Text)

    # Withdrawn by the source: dependents are invalidated. Distinct from
    # supersession, where both occurrences remain true and only the earlier one
    # stops being the thing to learn from.
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_for_learning: Mapped[bool] = mapped_column(Boolean, nullable=False)
