"""ORM rows for published profile revisions, tenant bindings, extensions and compiled definitions.

Declared against the shared storage `Base` so one metadata object still describes
the whole database, and kept in this package rather than in `storage/models.py`,
which is already at its size waiver.

These mirror `0050_profile_revisions`. The migration owns the constraints and this
file owns the Python view of the same rows; a drift between them is what the
parity test catches. Constraints are deliberately not re-declared here, and for
three of them that separation matters more than usual:

- publication immutability is a database trigger, because migration tooling and
  operator sessions write these tables too and a Python-side rule is one a psql
  session does not have;
- the one-active-binding-per-tenant-per-instant rule is an exclusion constraint
  over a time range, which no column declaration can express;
- the `NULLS NOT DISTINCT` uniqueness on the compiled definitions is what stops a
  repeated compile of a core revision inserting a second copy of every type, and
  a restatement here would be a second copy that can be relaxed without touching
  the one the database enforces.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class ProfileRevision(Base):
    """One published profile document, which may never change afterwards.

    `document_digest` is stored rather than derived on read: the canonical form
    is the compiler's output, so a reader that recomputes it is trusting today's
    canonicalizer to agree with the one that published this row years ago.

    `predecessor_revision_id` is NULL exactly for a profile's first revision.
    That is a different statement from "predecessor unknown", which is why no
    sentinel id is used for it.
    """

    __tablename__ = "profile_revisions"

    profile_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    profile_family: Mapped[str] = mapped_column(Text, nullable=False)
    profile_name: Mapped[str] = mapped_column(Text, nullable=False)
    semantic_version: Mapped[str] = mapped_column(Text, nullable=False)

    canonical_document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    document_digest: Mapped[str] = mapped_column(Text, nullable=False)

    compatibility: Mapped[str] = mapped_column(Text, nullable=False)

    predecessor_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_revisions.profile_revision_id")
    )
    migration_plan_ref: Mapped[str | None] = mapped_column(Text)

    published_by: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProfileExtension(Base):
    """One tenant's published extension of a core revision, equally immutable.

    `target_core_revision_id` is not nullable because an extension is only
    meaningful relative to the core it extends — an extension with no declared
    target cannot be checked for collisions against anything.

    `extension_points` is kept beside the document rather than only inside it so
    publication can check what the extension claims to extend without re-parsing
    the document it is validating.
    """

    __tablename__ = "profile_extensions"

    extension_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    target_core_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_revisions.profile_revision_id"), nullable=False
    )

    canonical_document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    document_digest: Mapped[str] = mapped_column(Text, nullable=False)

    extension_points: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    compatibility_result: Mapped[str] = mapped_column(Text, nullable=False)

    published_by: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProfileBinding(Base):
    """Which profile a tenant is governed by, over which interval.

    The one row in this module that is meant to move: `planned` becomes
    `validating` becomes `active`, and a rollback walks it back. It carries no
    immutability trigger for that reason — freezing it would make `state` a lie.

    `effective_to` NULL means the interval is still open, which is what an active
    binding looks like until something replaces it. `rollback_target_binding_id`
    and `rollback_ready` are both present because a target with no readiness is a
    plan nobody has checked.
    """

    __tablename__ = "profile_bindings"

    binding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    profile_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_revisions.profile_revision_id"), nullable=False
    )

    extension_set_digest: Mapped[str] = mapped_column(Text, nullable=False)

    state: Mapped[str] = mapped_column(Text, nullable=False)

    effective_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    rollback_target_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_bindings.binding_id")
    )
    rollback_ready: Mapped[bool] = mapped_column(Boolean, nullable=False)

    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    audit_reference: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EntityTypeDefinition(Base):
    """One compiled entity type, as it stood in one compile of one revision.

    `extension_revision_id` NULL means the type came from core rather than from a
    tenant extension. `readiness_rules` is separate from the property lists
    because a draft is allowed to fail readiness while still being a well-formed
    entity — collapsing the two would make every incomplete draft invalid.
    """

    __tablename__ = "entity_type_definitions"

    definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    profile_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_revisions.profile_revision_id"), nullable=False
    )
    extension_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_extensions.extension_revision_id")
    )

    type_name: Mapped[str] = mapped_column(Text, nullable=False)

    required_properties: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    optional_properties: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    value_schemas: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    authority: Mapped[str] = mapped_column(Text, nullable=False)
    default_provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    readiness_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    compiled_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RelationshipTypeDefinition(Base):
    """One compiled relationship type: its endpoints, its shape, and who may assert it.

    Endpoints are type *names* rather than definition ids: an extension may
    declare a relationship whose endpoint is a core type compiled under a
    different definition row, and a foreign key here would make that
    unexpressible.

    `cross_org_policy` is not nullable because an omitted policy is a denial, and
    a NULL would leave the decision to whatever a reader assumes.
    """

    __tablename__ = "relationship_type_definitions"

    definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    profile_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_revisions.profile_revision_id"), nullable=False
    )
    extension_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_extensions.extension_revision_id")
    )

    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)

    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    destination_type: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)

    property_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    duplicate_policy: Mapped[str] = mapped_column(Text, nullable=False)
    symmetry: Mapped[str] = mapped_column(Text, nullable=False)
    inverse_view_policy: Mapped[str] = mapped_column(Text, nullable=False)

    min_cardinality: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL means unbounded above, not "not yet decided".
    max_cardinality: Mapped[int | None] = mapped_column(Integer)
    cardinality_scope: Mapped[str] = mapped_column(Text, nullable=False)

    authority: Mapped[str] = mapped_column(Text, nullable=False)
    cross_org_policy: Mapped[str] = mapped_column(Text, nullable=False)

    compiled_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProfileCompileResult(Base):
    """What one compile consumed, which compiler ran it, and what it produced.

    All three are stored because the same inputs through a different compiler
    version may legitimately produce a different output digest; without the
    version recorded, that difference is indistinguishable from corruption.

    `conflicts` and `warnings` are ordered sequences, which is why they are
    arrays rather than sets — the order is how a reader works through them.
    """

    __tablename__ = "profile_compile_results"

    compile_result_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    profile_revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_revisions.profile_revision_id"), nullable=False
    )
    extension_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profile_extensions.extension_revision_id")
    )

    input_digests: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    compiler_version: Mapped[str] = mapped_column(Text, nullable=False)
    output_digest: Mapped[str] = mapped_column(Text, nullable=False)

    conflicts: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)

    compiled_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
