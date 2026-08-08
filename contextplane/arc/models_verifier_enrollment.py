"""ORM mirror of the verifier-enrollment challenge table
(`0008_arc_verifier_principal_binding.py`).

Lives beside `models.py` rather than inside it for the same reason
`models_operational_chain.py` does -- `models.py` is already close to the
repo-wide 800-line ceiling `scripts/check_file_sizes.py` enforces, and this
is one cohesive, independently-owned table. `models.py` imports and
re-exports this class into `ARC_MODELS` so the schema round-trip test and
`contextplane/storage/migrations/env.py`'s autogenerate import still only need
to look in one place.

The principal-binding columns the same migration adds to `arc_approval_
verifiers` are declared directly on `ArcApprovalVerifier` in `models.py`
itself -- that class already lives there and nothing outside `models.py`
imports it, so extending it in place is the smaller change; only the new
table gets a sibling.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base

# Named independently of `models.py`'s own `_TS`, matching every other
# sibling's stated reason: importing a private, underscore-prefixed name
# across sibling modules would make this module's own re-export boundary
# less clean than the one extra line this costs.
_TS = DateTime(timezone=True)


class ArcApprovalVerifierEnrollmentChallenge(Base):
    """One D1 proof-of-possession round trip's own state.

    `verifier_id` and `canonical_enrollment_bytes` are both committed here
    at creation, before any `arc_approval_verifiers` row exists: the whole
    point of the challenge is that the caller (or a configured provider)
    proves possession of the credential *before* the row that will trust it
    is written. `consumed_at` starts `NULL` and is set exactly once, by a
    `WHERE consumed_at IS NULL` compare-and-swap in the service -- the same
    single-use shape `ChallengeService.consume_challenge` uses for the
    unrelated host-attestation challenge table.
    """

    __tablename__ = "arc_approval_verifier_enrollment_challenges"

    enrollment_challenge_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    verifier_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    nonce: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    binding_kind: Mapped[str] = mapped_column(Text, nullable=False)
    principal_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    principal_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_allowed_principal_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    owning_scope: Mapped[str] = mapped_column(Text, nullable=False)
    target_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=True
    )
    allowed_evidence_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    signature_algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    credential_material: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    canonical_enrollment_bytes: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    valid_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    valid_to: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    issued_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    consumed_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    created_by_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


__all__ = ["ArcApprovalVerifierEnrollmentChallenge"]
