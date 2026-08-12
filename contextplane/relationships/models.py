"""ORM row for typed relationship metadata, keyed by the edge it governs.

Declared against the shared storage `Base` so one metadata object still describes
the whole database, and kept here rather than grown into `storage/models.py`,
which is already at its size waiver.

This mirrors `0052_relationship_metadata`. The migration owns the constraints and
this file owns the Python view of the same row; a drift between them is what the
parity test catches. Three constraints are deliberately not restated here:

- the temporal exclusion, which forbids two assertions of one type over one
  ordered pair whose validity intervals overlap. No column declaration can
  express it, and a Python-side approximation would be a second copy of the rule
  that can be relaxed without touching the one the database enforces;
- the readiness and cardinality-scope vocabularies, which are CHECK constraints;
- the aggregate lock key, which is a SQL function precisely so every writer
  derives it identically.

**Why this is a separate row rather than columns on `Edge`.** The edge keeps its
identity and its interfaces unchanged — nothing that reads `edges` today moves.
The governed row shares the edge's id, so the two are joined by identity rather
than kept in step. It also keeps the ORM honest: `Edge` is mapped in the shared
storage models, and adding columns to the table without adding them to that class
is exactly the database/ORM drift the parity test exists to find.

The endpoints appear here as well as on the edge. That duplication is deliberate
and is argued at the migration: every constraint on this table is *about* the
endpoints, and a uniqueness or exclusion rule cannot be expressed over columns
living in another table.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class RelationshipMetadata(Base):
    """The profile-governed facts about one existing edge.

    `relationship_id` is the edge's own id rather than a new key: a governed row
    describes a relationship that already exists and must be reachable by the id
    every current reader holds.

    `relationship_type` and `cardinality_scope` are carried alongside the
    definition reference rather than read through it. They are the definition's
    values *at assertion time*, which is what makes a later definition change show
    up as a difference instead of silently rewriting what this row meant.

    `readiness_state` is stored rather than derived, because readiness gates
    activation and a value recomputed on read would answer with today's rules
    about a row asserted under older ones.
    """

    __tablename__ = "relationship_metadata"

    relationship_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("edges.edge_id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    relationship_type_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("relationship_type_definitions.definition_id"), nullable=False
    )

    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False
    )
    destination_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False
    )

    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    cardinality_scope: Mapped[str] = mapped_column(Text, nullable=False)

    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    effective_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL means still in force, not unknown.
    effective_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    readiness_state: Mapped[str] = mapped_column(Text, nullable=False)

    provenance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assertion_provenance.provenance_id"), nullable=False
    )
    profile_binding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_bindings.binding_id"), nullable=False
    )

    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
