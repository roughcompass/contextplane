"""Source-admission ORM models: the five tables `0004_arc_source_admission.py`
creates.

Sibling of `registry/arc/models.py`, not a second write surface or a second
metadata registry — every class here maps onto the same `Base` that module
uses, and `models.py` re-exports each one plus folds them into `ARC_MODELS`,
so `from registry.arc.models import ArcSourceConnector` keeps working for
any caller that does not need to know the split exists. The split exists
only because `scripts/check_file_sizes.py`'s 800-line ceiling applies to
`arc/models.py` like every other file in this tree; `registry/storage/
migrations/env.py` still only ever needs to import `registry.arc.models`
for Alembic's autogenerate to see every mapped class, because that module
imports this one.

Global-capable, so `tenant_id` is nullable and paired with an explicit
`owning_scope` column rather than `TenantMixin` — unlike the artifact-side
tables in `models.py` (which infer scope from nullability alone), these
carry both because the wire contract accepts both and a CHECK ties them
together. See `ck_arc_source_connectors_scope_tenant` and its siblings in
the migration.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.storage.models import Base

# Named independently of `models.py`'s own `_TS` rather than imported from
# it: both are the same `DateTime(timezone=True)` literal, and importing a
# private, underscore-prefixed name across sibling modules would make this
# module's own re-export boundary (see the module docstring) less clean than
# the one extra line this costs.
_TIMESTAMPTZ = DateTime(timezone=True)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True)


class ArcSourceConnector(Base):
    """A registered configured-connector authority: `arc_source_connector_v1`.

    A caller admitting a source names one of these by id; it never supplies
    a fetch scheme, host, or credential of its own. Redirect and resolved-
    host checks at fetch time are still against this row's own allowlists,
    not against anything the caller sent.
    """

    __tablename__ = "arc_source_connectors"

    connector_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owning_scope: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    allowed_schemes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    allowed_hosts: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    allowed_media_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    allowed_verifier_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    max_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)


class ArcSourceUploadPolicy(Base):
    """A registered authorized-upload authority: `arc_source_upload_policy_v1`.

    The authenticated caller uploads bytes directly under one of these; it
    supplies no URL, so there is no host or redirect to validate — only the
    scope, media type, byte ceiling, and verifier allowlist below.
    """

    __tablename__ = "arc_source_upload_policies"

    policy_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owning_scope: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    allowed_media_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    allowed_verifier_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    max_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    registered_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)


class ArcSourceBody(Base):
    """Admitted bytes plus the digest this deployment computed over them.

    `source_evidence_id` is minted once in application code, before either
    this row or its evidence sibling is inserted — see the migration's own
    note on why that avoids a circular foreign key.
    """

    __tablename__ = "arc_source_bodies"

    source_evidence_id: Mapped[uuid.UUID] = _uuid_pk()
    content_digest: Mapped[str] = mapped_column(Text, nullable=False)
    content_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)


class ArcSourceApprovalEvidence(Base):
    """The closed `source_approval_evidence_v1` envelope, plus the admission
    and idempotency bookkeeping ADR 039 requires around it.

    `claim` stores the complete `arc_source_approval_claim_v1` object as the
    canonical dict the service validated and hashed — never a caller's raw,
    unvalidated body. `idempotency_scope_digest` carries the UNIQUE
    constraint that is the final race guard behind the service's lock-then-
    recheck.
    """

    __tablename__ = "arc_source_approval_evidence"

    source_evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_source_bodies.source_evidence_id"), primary_key=True
    )
    owning_scope: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision_locator: Mapped[str] = mapped_column(Text, nullable=False)
    source_content_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_content_digest: Mapped[str] = mapped_column(Text, nullable=False)
    claim: Mapped[Any] = mapped_column(JSONB, nullable=False)
    claim_digest: Mapped[str] = mapped_column(Text, nullable=False)
    verification_method: Mapped[str] = mapped_column(Text, nullable=False)
    verifier_id: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_attestation: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    admission_method: Mapped[str] = mapped_column(Text, nullable=False)
    connector_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("arc_source_connectors.connector_id"), nullable=True
    )
    policy_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("arc_source_upload_policies.policy_id"), nullable=True
    )
    admitted_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    admitted_by_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    admitted_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(Text, nullable=False)
    admission_request_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_scope_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)


class ArcSourceApprovalStatus(Base):
    """Local, periodically refreshed source-approval validity.

    ARC never follows a revocation URL from evidence — a configured
    connector or verifier provider's refresh worker updates this row, and
    every trust transition reads it instead. Admission only seeds the
    initial row; a periodic refresh worker keeps it current afterward.
    """

    __tablename__ = "arc_source_approval_status"

    source_evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_source_approval_evidence.source_evidence_id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    checked_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    next_check_at: Mapped[datetime.datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    status_source: Mapped[str] = mapped_column(Text, nullable=False)
    status_evidence_digest: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "ArcSourceApprovalEvidence",
    "ArcSourceApprovalStatus",
    "ArcSourceBody",
    "ArcSourceConnector",
    "ArcSourceUploadPolicy",
]
