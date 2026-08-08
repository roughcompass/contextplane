"""Submission identity gets a durable column, not just a durable timestamp.

Revision ID: 0012_arc_submission_identity
Revises: 0011_arc_observation
Create Date: 2026-08-07

`arc_authoring_proposal_versions` has recorded *when* a version was submitted
(`frozen_at`) since it was created, but never *who* submitted it. The only
durable record of the submitter was the same-transaction `arc.proposal.
submitted` audit-outbox event, and the review-package assembly path read it
back out of `arc_audit_outbox` by scanning `event_payload` for a matching
`proposal_id`/`proposal_version` -- an unindexed JSONB scan over a table
whose stated purpose is audit, not authoritative state, sitting in a signed
artifact's read path. If that table is ever pruned, archived, or partitioned
by age, an already-approved revision's submitter identity becomes
unreadable, and the artifact that identity is a required field of fails
closed at activation rather than degrading quietly -- the right failure
mode, reached through the wrong dependency.

This migration adds the two columns the write path already has the values
for at submission time: `submitted_by_issuer`/`submitted_by_subject`, written
by the same compare-and-swap that already sets `frozen_at` and `revision_id`
when a version is submitted, so the review-package assembly path can read
them from the row it already loads instead of scanning the outbox.

Nullable, both, matching `frozen_at`'s own nullability and for the same
reason: a version in the `open` state predates any submission and correctly
has no submitter yet, so a `NOT NULL` column would have no legitimate value
to hold before that compare-and-swap ever runs. Once a version is submitted,
both are always set together with `frozen_at` and `revision_id` in that one
statement -- there is no code path that sets one without the other three.

Existing rows: any version already in a post-open state before this
migration lands gets `NULL` for both new columns, because there is no
backfill source for them that is not the exact audit-outbox scan this
migration exists to stop depending on -- backfilling from the thing being
removed would just move the same dependency one migration earlier. This has
no operational consequence today: nothing built on this schema has shipped
anywhere yet, so no such row exists outside of a local or CI database that
predates this change, and re-running the seeding/submission flow there
produces rows with both columns populated going forward.
"""

from __future__ import annotations

from alembic import op

revision = "0012_arc_submission_identity"
down_revision: str | None = "0011_arc_observation"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE arc_authoring_proposal_versions ADD COLUMN submitted_by_issuer TEXT")
    op.execute("ALTER TABLE arc_authoring_proposal_versions ADD COLUMN submitted_by_subject TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE arc_authoring_proposal_versions DROP COLUMN submitted_by_subject")
    op.execute("ALTER TABLE arc_authoring_proposal_versions DROP COLUMN submitted_by_issuer")
