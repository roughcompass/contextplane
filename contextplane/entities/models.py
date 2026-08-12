"""ORM rows for type-qualified handles, assertion provenance, and attribute revisions.

Declared against the shared storage `Base` so one metadata object still
describes the whole database, and kept in this package rather than in
`storage/models.py`, which is already at its size waiver.

These mirror the handles-and-provenance revision. The migration owns the
constraints and this file owns the Python view of the same rows; a drift between
them is what the parity test catches. Constraints are deliberately not
re-declared here, and three of them matter more than usual for that separation:

- provenance immutability and the two append-only rules are database triggers,
  because migration tooling and operator sessions write these tables too and a
  Python-side rule is one a `psql` session does not have;
- active uniqueness is *partial* — unique among live rows only — which a column
  declaration cannot express, and a restatement here would be a second version
  of the rule that can be relaxed without touching the enforced one;
- `provenance_id` is `NOT NULL` on both sides, but the foreign key is what makes
  a missing source unwritable rather than merely undeclared.

The existing opaque `entity_id` remains the primary identity throughout. A
handle is a record *about* an entity, so nothing here replaces that id or the
references other tables already hold to it.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class EntityHandle(Base):
    """One stable, type-qualified name for an entity, over an interval.

    `qualified_handle` is stored rather than derived on read so a lookup
    compares one column, and the database constrains it to equal the parts it
    was built from — without that it would be a free-text field that merely
    looks structured.

    `lookup_key` is the normalized form every lookup compares against, kept
    apart from the display spelling so normalization happens once at write time
    instead of in each reader, each with its own idea of the rules.

    `valid_to` NULL means live. Retired handles stay in the table so a
    historical reference still resolves, which is why uniqueness among handles
    is partial rather than total.
    """

    __tablename__ = "entity_handles"

    handle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)

    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    handle_name: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_handle: Mapped[str] = mapped_column(Text, nullable=False)
    lookup_key: Mapped[str] = mapped_column(Text, nullable=False)

    #: `primary`, `alias`, `legacy`, or `external_mapping`.
    kind: Mapped[str] = mapped_column(Text, nullable=False)

    valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[str] = mapped_column(Text, nullable=False)
    superseded_by_handle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_handles.handle_id"), nullable=True
    )

    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AssertionProvenance(Base):
    """Where one assertion revision came from, and how far it may be trusted.

    Immutable in the database. Re-stating a source's trust class in place would
    silently re-characterize every assertion already resting on it, including
    ones already read and acted on, so a correction is a new row here and a new
    assertion revision naming it.

    The three times are separate on purpose: when it happened, when it was seen,
    and when it was stored answer different questions, and collapsing them makes
    staleness unmeasurable.

    `confidence` is present only for a derived record. Attaching one to a fact a
    canonical owner stated invites a reader to discount something that was never
    inferred.
    """

    __tablename__ = "assertion_provenance"

    provenance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_namespace: Mapped[str] = mapped_column(Text, nullable=False)
    external_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_revision: Mapped[str | None] = mapped_column(Text, nullable=True)

    event_time: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    derivation_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    derivation_profile: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: `canonical_owner`, `external_authority`, `observed`, or `derived`.
    authority: Mapped[str] = mapped_column(Text, nullable=False)

    #: `fresh`, `stale`, `expired`, or `revoked`.
    freshness_state: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    validating_profile_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_revisions.profile_revision_id"), nullable=True
    )
    extension_set_digest: Mapped[str | None] = mapped_column(Text, nullable=True)

    produced_by: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EntityAttributeAssertion(Base):
    """One revision of one attribute of one entity, with the source it rests on.

    A projection per attribute rather than a column on a mutable entity row:
    two attributes of the same entity routinely come from different sources with
    different trust, and one row per entity hides that difference behind
    whichever write happened last.

    `provenance_id` is `NOT NULL` and a real foreign key. An assertion whose
    source cannot be named is one nobody can re-check or revoke, and once such
    rows exist there is no way to separate them from the rest afterwards.
    """

    __tablename__ = "entity_attribute_assertions"

    assertion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("entities.entity_id"), nullable=False)

    property_name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_assertion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_attribute_assertions.assertion_id"), nullable=True
    )

    provenance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assertion_provenance.provenance_id"), nullable=False
    )

    #: `valid`, `invalid`, or `unchecked`.
    validation_result: Mapped[str] = mapped_column(Text, nullable=False)
    validating_profile_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_revisions.profile_revision_id"), nullable=True
    )

    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
