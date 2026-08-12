"""ORM rows for cross-organization grants and migration dispositions.

Declared against the shared storage `Base` so one metadata object still describes
the whole database, and kept here rather than grown into `storage/models.py`,
which is already at its size waiver.

These mirror `0053_ownership_and_grants`. The migration owns the constraints and
this file owns the Python view of the same rows; a drift between them is what the
parity test catches. Three constraint groups are deliberately not restated here,
and for each the reason is the same — a second copy in Python is one that can be
relaxed without touching the one the database enforces:

- revocation is when, why and by whom, all three or none, and the grant state must
  agree with whether a revocation exists;
- an active grant has at least one approving authority;
- a grandfather disposition carries owner, reason, warning, expiry and enforced
  action together, and its expiry may not exceed the approved ceiling.

`policy_version` is NOT NULL because an omitted grant or policy is a denial: a
grant that cannot name the policy it was evaluated under cannot be evaluated at
all, and must not be storable.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class CrossOrgGrant(Base):
    """One grant of cross-organization reach, and everything needed to revoke it.

    The selector fields are documents rather than columns because their shape is
    the profile's to define; a column per selector kind would need a migration
    every time the profile grew one.

    `classification_ceiling` is a ceiling rather than a filter: content above it is
    not shared at all, rather than shared in redacted form.
    """

    __tablename__ = "cross_org_grants"

    grant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    source_tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    destination_tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )

    grant_kind: Mapped[str] = mapped_column(Text, nullable=False)
    grant_state: Mapped[str] = mapped_column(Text, nullable=False)

    profile_types: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    relationship_types: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    instance_selectors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    audience: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    applicability: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    allowed_operations: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)

    classification_ceiling: Mapped[str] = mapped_column(Text, nullable=False)

    effective_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    approving_authorities: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    approval_evidence: Mapped[str | None] = mapped_column(Text)

    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    revoked_by: Mapped[str | None] = mapped_column(Text)

    policy_version: Mapped[str] = mapped_column(Text, nullable=False)

    recorded_by: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProfileMigrationDisposition(Base):
    """What happens to one pre-profile record, and the justification if it stays.

    The five grandfather fields are NULL together or present together. They are
    what separate a deliberate temporary exemption from an indefinite one nobody
    revisits, and a `migrate` row carrying an expiry would be an exemption in
    disguise.
    """

    __tablename__ = "profile_migration_dispositions"

    disposition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    record_class: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    disposition: Mapped[str] = mapped_column(Text, nullable=False)

    grandfather_owner: Mapped[str | None] = mapped_column(Text)
    grandfather_reason: Mapped[str | None] = mapped_column(Text)
    grandfather_warning: Mapped[str | None] = mapped_column(Text)
    grandfather_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    enforced_action: Mapped[str | None] = mapped_column(Text)

    recorded_by: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
