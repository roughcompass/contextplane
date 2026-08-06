"""ORM mirror of the operational event chain (`0007_arc_operational_chain.py`).

Lives beside `models.py` rather than inside it for the same reason
`models_source_admission.py` and `models_proposal.py` do -- `models.py` is
already close to the repo-wide 800-line ceiling `scripts/check_file_sizes.py`
enforces, and this is a cohesive, independently-owned group of three tables
(a chain, its append cursor, and its export outbox), not an arbitrary slice.
`models.py` imports and re-exports these three classes into `ARC_MODELS` so
the schema round-trip test and `registry/storage/migrations/env.py`'s
autogenerate import still only need to look in one place.

`ArcOperationalEvent`/`ArcOperationalEventHead` mirror `ArcReceiptEvent`/
`ArcReceiptEventHead` in shape -- same chain-link discipline, same locked-
head-row append -- but this chain is keyed by `revision_id`, not
`receipt_id`: it is the record of a revision's *operational* lifecycle
(freshness, legal hold, retention), not of one resolution's own lifecycle.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.storage.models import Base

# Named independently of `models.py`'s own `_TS`, matching
# `models_proposal.py`'s stated reason: importing a private,
# underscore-prefixed name across sibling modules would make this module's
# own re-export boundary less clean than the one extra line this costs.
_TS = DateTime(timezone=True)


class ArcOperationalEvent(Base):
    """One signed link in a revision's operational event chain.

    `PRIMARY KEY (revision_id, sequence)`: the natural clustering key for
    "the next event in this chain," which is what an append actually
    contends on. `event_id` is still globally unique (see the migration)
    for callers that only have an event id, but it is not the primary key.
    """

    __tablename__ = "arc_operational_events"

    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    actor_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    actor_subject: Mapped[str] = mapped_column(Text, nullable=False)
    actor_role: Mapped[str] = mapped_column(Text, nullable=False)
    authorization_decision_reference: Mapped[str] = mapped_column(Text, nullable=False)
    authority_evidence_digest: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(Text, nullable=False)
    previous_event_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    signer_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_digest: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    signature_profile: Mapped[str] = mapped_column(Text, nullable=False)
    request_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcOperationalEventHead(Base):
    """Mutable head row per revision; the append concurrency control.

    Locked `FOR UPDATE` on every append past genesis. Verification compares
    against the head's last digest rather than re-reading the whole chain,
    which keeps append cost independent of chain length.
    """

    __tablename__ = "arc_operational_event_heads"

    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), primary_key=True
    )
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    last_event_digest: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcOperationalChainCheckpoint(Base):
    """The durable-export outbox row written alongside every appended event.

    `exported_at`/`sink_receipt_digest`/`sink_receipt_signature` move
    together (see the migration's own CHECK): a checkpoint is pending until
    the sink acknowledges it, and there is no state where a receipt exists
    without an export timestamp or the reverse.
    """

    __tablename__ = "arc_operational_chain_checkpoints"

    checkpoint_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(Text, nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_operational_events.event_id"), nullable=False
    )
    head_digest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    exported_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    sink_receipt_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    sink_receipt_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    export_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_export_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_export_attempt_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)


__all__ = [
    "ArcOperationalChainCheckpoint",
    "ArcOperationalEvent",
    "ArcOperationalEventHead",
]
