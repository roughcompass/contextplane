"""ORM rows for derivation attempts, the evidence they read, and the cases they raise.

The Living Memory domain's mapped models, declared against the shared storage
`Base` so one metadata object still describes the whole database — the same
domain-local idiom `context/models.py` and `workspaces/models.py` already use.

These mirror `0042_derivation_and_curation`. The migration owns the constraints
and this file owns the Python view of the same rows; a drift between them is what
the parity test catches. Constraints are deliberately not re-declared here.
Restating the evidence discriminant in the ORM would create a second place to
change it, and for a table whose whole job is to record what a claim was entitled
to be derived from, the copy that stops matching is the dangerous one.

`env.py` imports only `arc.models` and `storage.models`, so autogenerate does not
see these tables and the migration is hand-written on purpose. Parity is proven by
the migration test importing these classes directly rather than by a diff nobody
runs.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base


class ClaimDerivation(Base):
    """One attempt to derive an assertion from evidence, kept whether or not it produced a claim.

    `source_authority` is the ceiling this attempt claimed for itself. It is not
    checkable in SQL — authority is a source-issued string with no ordering — so
    it is stored on both this row and every evidence link precisely so the
    comparison can be made by a reviewer rather than assumed.
    """

    __tablename__ = "claim_derivations"

    derivation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    profile: Mapped[str] = mapped_column(Text, nullable=False)
    profile_version: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False)

    applicability: Mapped[str] = mapped_column(Text, nullable=False)
    assertion_digest: Mapped[str] = mapped_column(Text, nullable=False)

    source_authority: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)

    # Present only when the attempt produced a claim; a pending or rejected
    # attempt created nothing and the migration refuses one that says otherwise.
    created_claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("memory_claims.claim_id"))

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DerivationEvidenceLink(Base):
    """One thing a derivation read, and the authority it carried when read.

    A discriminated union: `evidence_kind` says which pointer is populated, and
    the migration enforces that the others are not. The
    `(receipt_id, receipt_item_id)` pair is a composite foreign key against the
    receipt's own items, so an item belonging to a different receipt cannot be
    cited as evidence.
    """

    __tablename__ = "derivation_evidence_links"

    link_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    derivation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claim_derivations.derivation_id"), nullable=False
    )

    evidence_kind: Mapped[str] = mapped_column(Text, nullable=False)

    signal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("external_signals.signal_id"))
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    receipt_item_id: Mapped[str | None] = mapped_column(Text)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("context_external_references.reference_id")
    )
    checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    checkpoint_digest: Mapped[str | None] = mapped_column(Text)

    source_authority: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)

    # The smallest quotation that makes the assertion checkable; never a
    # workspace copy.
    excerpt: Mapped[str | None] = mapped_column(Text)


class CurationCase(Base):
    """A contradiction routed to an owner, and what was eventually decided.

    Nothing here points at a canonical target. Dispositions are proposals, and
    the surfaces that act on them have their own approval paths — a column that
    named a write target would make "decided" and "written" the same event.
    """

    __tablename__ = "curation_cases"

    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False)

    subject_reference: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)

    raised_by_derivation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claim_derivations.derivation_id")
    )

    status: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[str | None] = mapped_column(Text)
    routed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    disposition: Mapped[str | None] = mapped_column(Text)
    approval_authority: Mapped[str | None] = mapped_column(Text)
    evidence_threshold: Mapped[str | None] = mapped_column(Text)

    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
