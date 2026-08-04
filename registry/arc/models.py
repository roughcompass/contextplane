"""SQLAlchemy 2.0 mapped classes for the ARC schema.

Mirrors migration ``0023_arc_phase1``. The migration is the authoritative DDL
source — CHECK constraints, partial indexes, deferrable foreign keys, and the
challenge-consumption constraint trigger live there and are deliberately not
duplicated here. What this module provides is a typed Python surface for service
code, and the round-trip test in ``tests/integration/test_arc_models_schema.py``
is what keeps the two from drifting.

Scope columns follow the architecture's rule, which is easy to get subtly wrong:

- Artifact-side tables (artifacts, revisions, directives, applicability rules,
  identities, obligations) are **global-capable**, so ``tenant_id`` is nullable
  and ``NULL`` is the only global marker. They must not use ``TenantMixin``.
- Request-side tables (challenges, receipts, events, selected rows, audit
  outbox) always carry the concrete requesting tenant, even when the receipt
  selected global artifacts, so they do use ``TenantMixin``.

Getting that backwards in either direction is a tenant-isolation bug rather than
a style question: a global artifact with a concrete tenant becomes invisible, and
a receipt with a NULL tenant escapes every tenant-scoped read.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.storage.models import Base, TenantMixin

# The reserved deployment-scope tenant. `audit_log.tenant_id` is NOT NULL and
# ARC's deployment-global operations have no tenant, so those events attribute
# here. Created by migration 0023 with `disabled_at` set — that column, not
# `is_active`, is what makes the row unusable as a real tenant.
DEPLOYMENT_TENANT_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

_TS = DateTime(timezone=True)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True)


# ---------------------------------------------------------------------------
# Artifact family — global-capable, no TenantMixin
# ---------------------------------------------------------------------------


class ArcArtifact(Base):
    """Stable artifact family identity. ``tenant_id IS NULL`` means global."""

    __tablename__ = "arc_artifacts"

    artifact_id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    created_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class ArcRevision(Base):
    """Immutable approved projection of an artifact at a point in time.

    Content fields never change after creation; only lifecycle transitions do.
    Supersession creates a new revision and links back, rather than editing.
    """

    __tablename__ = "arc_revisions"

    revision_id: Mapped[uuid.UUID] = _uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_canonical_locator: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision_locator: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    effective_until: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    # FK added by the migration after the table exists (self-referential).
    superseded_by_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # DEFERRABLE INITIALLY DEFERRED in the migration: a revision names the
    # evidence that approved it and the evidence names the revision, so one
    # transaction has to be able to insert both in either order.
    approval_evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    review_expires_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    detail_audience: Mapped[str] = mapped_column(Text, nullable=False)
    freshness_basis: Mapped[str] = mapped_column(Text, nullable=False)
    content_classification: Mapped[str] = mapped_column(Text, nullable=False)
    content_retention_until: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_storage_mode: Mapped[str] = mapped_column(Text, nullable=False)
    source_body_ciphertext: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    source_body_plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_body_nonce: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    source_body_wrapped_dek: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    content_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_encryption_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    activated_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    created_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


class ArcDirectiveIdentity(Base):
    """Stable directive identity, reusable only by approved successor revisions."""

    __tablename__ = "arc_directive_identities"

    directive_id: Mapped[uuid.UUID] = _uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcConflictDomain(Base):
    """Serialization row per conflict subject. Holds no directive content.

    The digest indexes the canonical subject key; it does not define identity.
    Activations sharing a subject domain lock the same row and therefore
    serialize, while disjoint domains stay concurrent.
    """

    __tablename__ = "arc_conflict_domains"

    conflict_subject_digest: Mapped[str] = mapped_column(Text, primary_key=True)
    conflict_subject_key: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcDirective(Base):
    """Revision-local projection of a stable directive identity.

    Composite primary key: one stable identity has at most one projection per
    revision.
    """

    __tablename__ = "arc_directives"

    directive_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("arc_directive_identities.directive_id"),
        primary_key=True,
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    directive_type: Mapped[str] = mapped_column(Text, nullable=False)
    conflict_key_schema_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_subject_digest: Mapped[str | None] = mapped_column(
        Text, ForeignKey("arc_conflict_domains.conflict_subject_digest"), nullable=True
    )
    compact_statement_ciphertext: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    compact_statement_plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    compact_statement_nonce: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    compact_statement_wrapped_dek: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    source_anchor: Mapped[str] = mapped_column(Text, nullable=False)
    conflict_key_namespace: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_key_subject_selector: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_key_operation: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_key_action_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_key_target_selector: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_key_modality: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_key_constraint_operator: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_key_constraint_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    satisfaction_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_max_age_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted_verifier_classes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    accepted_verifier_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)
    required_evidence_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    delegable_exception: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcApplicabilityRule(Base):
    """Structured applicability predicate over a task manifest."""

    __tablename__ = "arc_applicability_rules"

    rule_id: Mapped[uuid.UUID] = _uuid_pk()
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    target_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    capability_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)
    capability_labels: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    domain_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    task_kinds: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    action_classes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    environments: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    data_sensitivity_tiers: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    effective_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    effective_until: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcMandatoryObligation(Base):
    """Family-level tombstone so a vanished mandatory projection still blocks.

    Without this, a revoked or review-expired mandatory directive would simply
    stop appearing in selection, and a bundle missing an obligation would look
    identical to one that never had it.
    """

    __tablename__ = "arc_mandatory_obligations"

    obligation_id: Mapped[uuid.UUID] = _uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False
    )
    directive_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_directive_identities.directive_id"), nullable=False
    )
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=True
    )
    applicability_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    applicability_digest: Mapped[str] = mapped_column(Text, nullable=False)
    obligation_state: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    effective_until: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


# ---------------------------------------------------------------------------
# Key and trust registries
# ---------------------------------------------------------------------------


class ArcHostAttestationKey(Base):
    """Registered host attestation signing keys.

    Resolution holds this row ``FOR SHARE`` until receipt commit and revocation
    holds it ``FOR UPDATE``, which is what totally orders revocation against
    every new receipt.
    """

    __tablename__ = "arc_host_attestation_keys"

    signer_key_id: Mapped[str] = mapped_column(Text, primary_key=True)
    host_id: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    attestation_profile: Mapped[str] = mapped_column(Text, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    valid_until: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    replacement_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    created_by_operator: Mapped[str] = mapped_column(Text, nullable=False)


class ArcReceiptSigningKey(Base):
    """Public verification history for receipt-event signing keys.

    Private key material stays in the configured ``ReceiptSigningProvider`` and
    is never stored here. Retirement never deletes a row: a receipt signed years
    ago must stay verifiable.
    """

    __tablename__ = "arc_receipt_signing_keys"

    signer_key_id: Mapped[str] = mapped_column(Text, primary_key=True)
    algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    valid_until: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    compromised_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    replacement_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_digest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcApprovalVerifier(Base):
    """Deployment-trusted verification identities for approval evidence.

    Public material or an approved provider reference only — never a signing
    secret.
    """

    __tablename__ = "arc_approval_verifiers"

    approval_verifier_id: Mapped[str] = mapped_column(Text, primary_key=True)
    verifier_kind: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_evidence_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    algorithm: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_key: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    valid_to: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcApprovalEvidence(Base):
    """Signed or verifier-attested approval backing an activation or exception."""

    __tablename__ = "arc_approval_evidence"

    evidence_id: Mapped[uuid.UUID] = _uuid_pk()
    evidence_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    approved_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=True
    )
    # Both of these close cycles and are DEFERRABLE INITIALLY DEFERRED in the
    # migration; no ORM-level ForeignKey here.
    approved_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_exception_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    approving_principal: Mapped[str] = mapped_column(Text, nullable=False)
    approving_role: Mapped[str] = mapped_column(Text, nullable=False)
    source_system_approval_locator: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_timestamp: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_method: Mapped[str] = mapped_column(Text, nullable=False)
    signer_key_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("arc_approval_verifiers.approval_verifier_id"), nullable=True
    )
    approval_verifier_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("arc_approval_verifiers.approval_verifier_id"), nullable=True
    )
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_attestation: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    verifier_identity: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_log_reference: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcApprovalEvidenceRevocation(Base):
    """Append-only approval withdrawal. Presence makes the evidence unusable."""

    __tablename__ = "arc_approval_evidence_revocations"

    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_approval_evidence.evidence_id"), primary_key=True
    )
    revoked_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    reason_digest: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcApprovedException(Base):
    """Approved lower-scope weakening of a delegable higher-scope directive."""

    __tablename__ = "arc_approved_exceptions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["higher_scope_revision_id", "higher_scope_directive_id"],
            ["arc_directives.revision_id", "arc_directives.directive_id"],
        ),
    )

    exception_id: Mapped[uuid.UUID] = _uuid_pk()
    higher_scope_directive_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    higher_scope_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lower_scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    lower_scope_tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    lower_scope_domain_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lower_scope_capability_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lower_scope_task_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    lower_scope_action_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    lower_scope_environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    lower_scope_data_sensitivity: Mapped[str | None] = mapped_column(Text, nullable=True)
    replacement_conflict_descriptor: Mapped[Any] = mapped_column(JSONB, nullable=False)
    exception_statement_ciphertext: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    exception_statement_plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    exception_statement_nonce: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    justification_ciphertext: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    justification_plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    justification_nonce: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    content_wrapped_dek: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    content_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    effective_until: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    # Deferrable in the migration; closes a cycle with arc_approval_evidence.
    approval_evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    created_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True
    )


# ---------------------------------------------------------------------------
# Request side — always a concrete requesting tenant, so TenantMixin applies
# ---------------------------------------------------------------------------


class ArcContextChallenge(Base, TenantMixin):
    """Single-use challenge binding a host, session, and claims digest.

    Only the nonce *digest* is stored. The raw nonce is reproducible for an
    exact unexpired retry through the versioned deriver, so keeping a
    recoverable copy would add risk for nothing.
    """

    __tablename__ = "arc_context_challenges"

    challenge_id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    host_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_claims_digest: Mapped[str] = mapped_column(Text, nullable=False)
    arc_nonce_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    nonce_derivation_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    idempotency_key_digest: Mapped[str] = mapped_column(Text, nullable=False)


class ArcReceipt(Base, TenantMixin):
    """Immutable context-resolution receipt.

    ``challenge_id`` is NOT NULL and UNIQUE: every receipt consumes exactly one
    challenge and no challenge backs two receipts. That pair, plus the deferred
    constraint trigger in the migration, is the single-use invariant.
    """

    __tablename__ = "arc_receipts"

    receipt_id: Mapped[uuid.UUID] = _uuid_pk()
    challenge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("arc_context_challenges.challenge_id"),
        nullable=False,
        unique=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=False)
    host_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    attestation_id: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_status: Mapped[str] = mapped_column(Text, nullable=False)
    selection_engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    registry_build_revision: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_profile_versions: Mapped[Any] = mapped_column(JSONB, nullable=False)
    selection_config_digest: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    freshness_basis: Mapped[str] = mapped_column(Text, nullable=False)
    freshness_deadline: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    blocked_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    degraded_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    mandatory_directive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rendered_content_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_limit_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    integrity_state: Mapped[str] = mapped_column(Text, nullable=False, default="valid")
    response_replay_ciphertext: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    response_replay_nonce: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    response_replay_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcReceiptEvent(Base, TenantMixin):
    """Append-only receipt lifecycle event.

    Sequences are **0-indexed**: the creation event is sequence 0 with a NULL
    predecessor. A 1-indexed assumption here rejects the first event of every
    receipt, which is not an edge case — it is the only path through receipt
    creation.
    """

    __tablename__ = "arc_receipt_events"

    event_id: Mapped[uuid.UUID] = _uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_receipts.receipt_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_source: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("actors.actor_id"), nullable=True)
    gateway_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    signer_key_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("arc_receipt_signing_keys.signer_key_id"), nullable=True
    )
    signature_profile: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    previous_event_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    consumed_continuation_token_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_replay_ciphertext: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    response_replay_nonce: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    response_replay_key_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_digest: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcReceiptEventHead(Base):
    """Mutable head row per receipt; the append concurrency control.

    Locked ``FOR UPDATE`` on every append. Verification compares against the
    head's last digest rather than re-reading the chain, so append stays O(1).
    """

    __tablename__ = "arc_receipt_event_heads"

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_receipts.receipt_id"), primary_key=True
    )
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    last_event_digest: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcReceiptSelectedRevision(Base, TenantMixin):
    """Which revisions a receipt selected."""

    __tablename__ = "arc_receipt_selected_revisions"

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_receipts.receipt_id"), primary_key=True
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False
    )
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False)
    was_omitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    omission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArcReceiptSelectedDirective(Base, TenantMixin):
    """Per-receipt snapshot of an exact selected directive.

    This is what JIT authorizes against. The locator and digest columns are
    access-controlled rather than encrypted — they are redacted by artifact
    audience before they reach a caller.
    """

    __tablename__ = "arc_receipt_selected_directives"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "directive_id"],
            ["arc_directives.revision_id", "arc_directives.directive_id"],
        ),
    )

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_receipts.receipt_id"), primary_key=True
    )
    directive_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False
    )
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False)
    was_omitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    omission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility_decision_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_locator: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision_locator: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(Text, nullable=False)
    obligation_fields: Mapped[Any] = mapped_column(JSONB, nullable=False)
    context_handle_digest: Mapped[str] = mapped_column(Text, nullable=False)


class ArcAuditOutbox(Base, TenantMixin):
    """Durable audit outbox, written in the same transaction as domain state.

    ARC does not write ``audit_log`` inline the way the rest of the codebase
    does; the drain worker is ARC's only writer to it. Failure state lives on
    the row — a row past the attempt ceiling is never deleted and never silently
    skipped, only excluded from the active drain query and counted by a gauge.
    """

    __tablename__ = "arc_audit_outbox"

    outbox_id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    drained_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)


# `ArcContentDeletionVerification` (`arc_content_deletion_verifications`) is
# not modeled here. Nothing in this codebase ever wrote a row to it — no
# INSERT anywhere in registry/, scripts/, or tests/ — so the table was
# excluded when the migration chain was squashed into one baseline revision.
# An ORM model with no table behind it would only mislead the next reader who
# went looking for its writer.

# Every ARC table, for the schema round-trip test and for service code that
# needs to enumerate them.
ARC_MODELS: tuple[type[Base], ...] = (
    ArcArtifact,
    ArcRevision,
    ArcDirectiveIdentity,
    ArcConflictDomain,
    ArcDirective,
    ArcApplicabilityRule,
    ArcMandatoryObligation,
    ArcHostAttestationKey,
    ArcReceiptSigningKey,
    ArcApprovalVerifier,
    ArcApprovalEvidence,
    ArcApprovalEvidenceRevocation,
    ArcApprovedException,
    ArcContextChallenge,
    ArcReceipt,
    ArcReceiptEvent,
    ArcReceiptEventHead,
    ArcReceiptSelectedRevision,
    ArcReceiptSelectedDirective,
    ArcAuditOutbox,
)
