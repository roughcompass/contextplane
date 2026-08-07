"""ORM mirror of the D2 projection-approval tables (`0009_arc_approval_
challenges.py`): the two-call challenge protocol's own state, and the
verified evidence it produces.

Sibling of `models.py` for the same reason `models_verifier_enrollment.py`
is: `models.py` is already close to the repo-wide 800-line ceiling `scripts/
check_file_sizes.py` enforces, and these are two cohesive, independently-
owned tables. `models.py` imports and re-exports both classes into
`ARC_MODELS` so the schema round-trip test and `registry/storage/
migrations/env.py`'s autogenerate import still only need to look in one
place.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from registry.storage.models import Base

# Named independently of `models.py`'s own `_TS`, matching every other
# sibling's stated reason: importing a private, underscore-prefixed name
# across sibling modules would make this module's own re-export boundary
# less clean than the one extra line this costs.
_TS = DateTime(timezone=True)


class ArcApprovalChallenge(Base):
    """One D2 two-call approval round trip's own state.

    `canonical_evidence_bytes` and `approved_payload_digest` (the recomputed
    `A` node) are both committed here at creation, before any evidence row
    exists -- the named verifier signs (or attests over) exactly these
    bytes. `attempt_count` and `state` are mutated under this row's own
    `SELECT ... FOR UPDATE` lock; `CHECK attempt_count <= 3` (in the
    migration) is what makes the third invalid signature terminal rather
    than merely conventional.
    """

    __tablename__ = "arc_approval_challenges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    approval_challenge_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_artifacts.artifact_id"), nullable=False
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=False
    )
    approval_verifier_id: Mapped[str] = mapped_column(
        Text, ForeignKey("arc_approval_verifiers.approval_verifier_id"), nullable=False
    )
    nonce: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    canonical_evidence_bytes: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    signing_domain: Mapped[str] = mapped_column(Text, nullable=False)
    approved_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_scope_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="issued")
    issued_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    terminalized_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)


class ArcProjectionApprovalEvidence(Base):
    """The verified D2 output -- the row activation predicate 8 revalidates.

    `credential_fingerprint_at_approval` is a snapshot, not a live join: it
    is compared against the verifier's *current* fingerprint at activation
    to detect drift (a key rotated or a verifier re-enrolled since this
    evidence was accepted), which only works if this column freezes the
    value as it was at verification time.
    """

    __tablename__ = "arc_projection_approval_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "proposal_version"],
            ["arc_authoring_proposal_versions.proposal_id", "arc_authoring_proposal_versions.proposal_version"],
        ),
    )

    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    approval_challenge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_approval_challenges.approval_challenge_id"), nullable=False, unique=True
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("arc_revisions.revision_id"), nullable=False
    )
    approved_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    approval_verifier_id: Mapped[str] = mapped_column(
        Text, ForeignKey("arc_approval_verifiers.approval_verifier_id"), nullable=False
    )
    approving_principal_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    approving_principal_subject: Mapped[str] = mapped_column(Text, nullable=False)
    credential_fingerprint_at_approval: Mapped[str] = mapped_column(Text, nullable=False)
    verification_method: Mapped[str] = mapped_column(Text, nullable=False)
    signature_algorithm: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_bytes: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    signing_domain: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    revocation_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["ArcApprovalChallenge", "ArcProjectionApprovalEvidence"]
