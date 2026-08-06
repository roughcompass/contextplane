"""The candidate document gains a column to live in.

Revision ID: 0006_arc_candidate_semantics
Revises: 0005_arc_authoring_proposals
Create Date: 2026-08-06

`ProposalPatchRequest.semantics` -- the full candidate `arc_artifact_semantics_v1`
document -- was frozen into the wire contract before this migration existed.
`arc_authoring_proposal_versions` carried only state and identity columns,
so a `PATCH` had nowhere durable to write the candidate it validated, and
`POST {PV}/semantic-tests` fell back to evaluating the reviewed baseline
revision's already-materialized rules instead of the document a caller had
just edited. This migration adds exactly the one column that gap needed.

Nullable, no `DEFAULT`: an open version predates any `PATCH` and correctly
has no candidate yet, and every row `0005_arc_authoring_proposals` has ever
created is one of those -- a `NOT NULL` here would need a same-migration
backfill this table has no legitimate placeholder value for. The three-way
conditional-requiredness and ambiguous-selector rules a candidate document
must satisfy are service-enforced (`arc/service/provenance.py`), the same
reasoning the parent migration already gives for not expressing
`arc_authoring_field_provenance`'s own conditional rule as a CHECK.
"""

from __future__ import annotations

from alembic import op

revision = "0006_arc_candidate_semantics"
down_revision: str | None = "0005_arc_authoring_proposals"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE arc_authoring_proposal_versions ADD COLUMN semantics JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE arc_authoring_proposal_versions DROP COLUMN semantics")
