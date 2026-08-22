"""Autonomy-envelope binding ORM model: the one table
`0062_autonomy_envelope_bindings.py` creates.

Sibling of `contextplane/arc/models.py`, same reason `models_proposal.py` and
`models_risk_envelope.py` are: `models.py` sits just under the 800-line ceiling
`scripts/check_file_sizes.py` enforces, so new mapped classes live beside it and
`models.py` imports and re-exports each one into `ARC_MODELS`, which is what the
schema round-trip test and `contextplane/storage/migrations/env.py` read.

**Not `models_risk_envelope.py`, despite the word.** That module's
`ArcExpectedImpactEnvelope` is a blast-radius forecast attached to a proposal
version -- how many selections change if a revision activates. This one binds an
agent principal to the `policy` revision that governs it. The two share a noun
and nothing else, which is why they are kept apart rather than filed together on
the strength of a name.

Tenant-scoped, unlike the artifact it points at: `arc_artifacts` and
`arc_revisions` are global-capable, but a principal belongs to exactly one
tenant, and a global envelope revision is bound per tenant rather than bound
once for everyone.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKeyConstraint, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from contextplane.storage.models import Base

# Named independently of `models.py`'s own `_TS`, matching every other sibling
# module's stated reason: importing a private, underscore-prefixed name across
# sibling modules would make this module's own re-export boundary less clean
# than the one extra line this costs.
_TS = DateTime(timezone=True)


class ArcAutonomyEnvelopeBinding(Base):
    """Which `policy` revision governs which agent principal, and since when.

    The binding names a revision rather than an artifact so that a governed
    widen is a recorded act -- close this binding, open another -- instead of
    something that happens to a principal when somebody publishes.

    `artifact_id` and `artifact_kind` are carried, not derived. They exist so
    the composite foreign keys can state in SQL that the bound revision belongs
    to an artifact of kind `policy`; the CHECK pinning `artifact_kind` and the
    exclusion constraint keeping one active binding per principal are both
    database-side and have no ORM expression here.
    """

    __tablename__ = "arc_autonomy_envelope_bindings"
    __table_args__ = (
        ForeignKeyConstraint(["artifact_id", "artifact_kind"], ["arc_artifacts.artifact_id", "arc_artifacts.kind"]),
        ForeignKeyConstraint(
            ["revision_id", "artifact_id"], ["arc_revisions.revision_id", "arc_revisions.artifact_id"]
        ),
    )

    binding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(Text, nullable=False)

    principal_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    principal_subject: Mapped[str] = mapped_column(Text, nullable=False)

    state: Mapped[str] = mapped_column(Text, nullable=False)

    effective_from: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)
    effective_to: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)

    suspended_at: Mapped[datetime.datetime | None] = mapped_column(_TS, nullable=True)
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    audit_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


class ArcEnvelopeAdvisoryRecord(Base):
    """One refusal the advisory stage recorded instead of enforcing.

    The graduation scan's substrate: it asks which principals acted with no
    envelope inside an observation window, and that question is why the table
    is keyed on `(tenant_id, principal_issuer, principal_subject)` rather than
    on anything about the act.

    Written only in the advisory stage and only for refusals -- a permit leaves
    no row, because a principal acting inside its envelope is not an offender.
    The two CHECKs that make the scan's assumptions structural live in
    `0065_envelope_enforcement_stage`: `permitted` is not an admissible verdict,
    and `no_envelope` is the one verdict with no binding to name.
    """

    __tablename__ = "arc_envelope_advisory_records"

    record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    principal_issuer: Mapped[str] = mapped_column(Text, nullable=False)
    principal_subject: Mapped[str] = mapped_column(Text, nullable=False)

    verdict: Mapped[str] = mapped_column(Text, nullable=False)

    binding_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    intent_kind: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)

    #: The handling tier the matrix judged this act at. Nullable, and null means
    #: the manifest carried none -- which the selection engine reads as the most
    #: restrictive. Stored as null rather than as `restricted` so a reader can
    #: tell a stream somebody classified from one nobody has: the same verdict,
    #: and only one of them is an omission to fix.
    data_sensitivity: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_at: Mapped[datetime.datetime] = mapped_column(_TS, nullable=False)


__all__ = ["ArcAutonomyEnvelopeBinding", "ArcEnvelopeAdvisoryRecord"]
