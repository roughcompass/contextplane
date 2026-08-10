"""Legal holds get somewhere to live, so a hold can be placed instead of refused.

Scheduled deletion runs now, and until this revision the hold seam every expiry
path consults had no storage behind it: the shipped store answered "nothing is
held" truthfully, because with no table no hold could exist, and refused every
attempt to place one. That is the litigation case the approved retention policy
exists for, so the table is the whole of the change and the seam above it does
not move.

**Three tables, because a renewal is never one audit row.** The policy admits a
hold past its first review only with recorded legal-necessity re-justification
*and* escalating approval. Those are two separate facts asserted by two
different parties, and folding them into a column on the hold would keep only
the latest of each -- a hold in its third year would show one justification and
one approver, with the trail that made it legitimate overwritten twice. So a
renewal writes a `legal_hold_renewals` row for the re-justification and a
`legal_hold_approvals` row for who approved it at what level, and the hold row
carries only the current position.

**The 180-day ceiling is a CHECK, not a convention.** An unbounded hold is how a
hold becomes a way of never deleting anything, and the review date is what keeps
it alive. Enforced in the database because the application is not the only thing
that will ever write this table -- operator tooling places holds too, and a rule
that lives only in Python is one a psql session does not have.

**One active hold per record.** The seam answers "is this record held?" with a
mapping keyed by subject id, so a second hold on the same record would be a
second answer to a question that has one. The uniqueness is partial on nothing:
a lapsed hold stays in the table as the audit trail of a hold that once existed,
so uniqueness covers every row and re-holding a record after a lapse is a new
`hold_id` -- which is the honest shape, because it is a new decision.

**The downgrade drops all three tables.** Every hold is lost, which is why the
downgrade is not a routine operation: it is the one change in this chain that
can turn a record the law requires kept into a record the next sweep deletes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_legal_holds"
down_revision: str | None = "0045_checkpoint_erasure_exception"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: The approved ceiling on a single hold, in days. The application holds the same
#: constant; an integration test places a hold against a live database, so the two
#: cannot drift apart without a red test rather than a silently accepted hold.
_MAX_HOLD_DAYS = 180


def upgrade() -> None:
    op.create_table(
        "legal_holds",
        sa.Column("hold_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id"),
            nullable=False,
        ),
        # Scope: which record class, and which record within it. Both, because a
        # subject id is only unique inside its class.
        sa.Column("record_class", sa.Text(), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Audit: who placed it and why. Free text on purpose -- a reason drawn
        # from a fixed vocabulary is a reason nobody reads.
        sa.Column("placed_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_date", sa.DateTime(timezone=True), nullable=False),
        # Position, not history. The trail lives in the two tables below.
        sa.Column("renewal_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_unique_constraint(
        "uq_legal_holds_record",
        "legal_holds",
        ["tenant_id", "record_class", "subject_id"],
    )
    op.create_check_constraint(
        "ck_legal_holds_review_within_ceiling",
        "legal_holds",
        f"review_date > placed_at AND review_date <= placed_at + INTERVAL '{_MAX_HOLD_DAYS} days'",
    )
    op.create_check_constraint(
        "ck_legal_holds_renewal_count_non_negative",
        "legal_holds",
        "renewal_count >= 0",
    )
    # The sweep's question, asked once per batch per record class: which of these
    # subject ids is held right now. Leading with the columns it filters on.
    op.create_index(
        "ix_legal_holds_lookup",
        "legal_holds",
        ["tenant_id", "record_class", "subject_id", "review_date"],
    )

    op.create_table(
        "legal_hold_renewals",
        sa.Column("renewal_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "hold_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_holds.hold_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Which renewal this is, counted from one. Unique per hold so a renewal
        # cannot be recorded twice at the same position, which is what a retry
        # that lost its response would otherwise do.
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("previous_review_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("new_review_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_legal_hold_renewals_sequence",
        "legal_hold_renewals",
        ["hold_id", "sequence"],
    )
    op.create_check_constraint(
        "ck_legal_hold_renewals_sequence_positive",
        "legal_hold_renewals",
        "sequence >= 1",
    )
    # A blank justification is the case the policy forbids, spelled so the
    # database refuses it rather than storing a renewal that recorded nothing.
    op.create_check_constraint(
        "ck_legal_hold_renewals_justification_present",
        "legal_hold_renewals",
        "length(btrim(justification)) > 0",
    )

    op.create_table(
        "legal_hold_approvals",
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "renewal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_hold_renewals.renewal_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("approved_by", sa.Text(), nullable=False),
        # Where this approver sits in the escalation. Stored as the rank rather
        # than only the name so "higher than last time" is a comparison the
        # database can express and a reader can check without a lookup table.
        sa.Column("approval_level", sa.Text(), nullable=False),
        sa.Column("approval_rank", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
    )
    # One approval per renewal. A second one would make "who approved this" a
    # question with more than one answer, and the escalation rule compares ranks
    # between renewals, not within one.
    op.create_unique_constraint(
        "uq_legal_hold_approvals_renewal",
        "legal_hold_approvals",
        ["renewal_id"],
    )
    op.create_check_constraint(
        "ck_legal_hold_approvals_rank_non_negative",
        "legal_hold_approvals",
        "approval_rank >= 0",
    )


def downgrade() -> None:
    # Approvals first, then renewals, then holds: each points at the one after it.
    op.drop_table("legal_hold_approvals")
    op.drop_table("legal_hold_renewals")
    op.drop_index("ix_legal_holds_lookup", table_name="legal_holds")
    op.drop_table("legal_holds")
