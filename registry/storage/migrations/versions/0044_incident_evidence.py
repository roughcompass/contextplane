"""An incident is its own kind of evidence, and its claims are historical facts.

Two small changes with one idea behind them: an incident is not a work item and does
not behave like a contract claim.

**A new evidence kind.** The provenance vocabulary already had `work_item`, and
labelling an incident as one would have avoided this migration. It would also have made
"which of these claims came from something that broke" unanswerable, and that is the
question anybody investigating a service asks first. The kinds are a closed set on
purpose, so extending it is a migration rather than a convention.

**A new claim category, because decay is keyed on category.** Every other category
describes current state and loses value as time passes since anybody checked. An
incident does not: it happened, it is still true that it happened, and a claim recording
it should not fade toward the floor. Reusing an existing category would have given
incident claims the wrong decay curve, and the curve is the whole reason categories
exist.
"""

from __future__ import annotations

from alembic import op

revision = "0044_incident_evidence"
down_revision = "0043_capability_requests"
branch_labels = None
depends_on = None


# The kinds are a closed set, so widening it means rewriting the constraint rather
# than adding to a list somewhere.
_ADD_INCIDENT_KIND = """
ALTER TABLE lmm_claim_provenance
    DROP CONSTRAINT ck_lmm_prov_kind,
    ADD CONSTRAINT ck_lmm_prov_kind CHECK (
        evidence_kind IN (
            'session_event', 'document_revision', 'commit', 'work_item',
            'connector_run', 'curator', 'incident'
        )
    )
"""

_DROP_INCIDENT_KIND = """
ALTER TABLE lmm_claim_provenance
    DROP CONSTRAINT ck_lmm_prov_kind,
    ADD CONSTRAINT ck_lmm_prov_kind CHECK (
        evidence_kind IN (
            'session_event', 'document_revision', 'commit', 'work_item',
            'connector_run', 'curator'
        )
    )
"""


def upgrade() -> None:
    op.execute(_ADD_INCIDENT_KIND)


def downgrade() -> None:
    # Rows using the new kind have to go first, or the narrower constraint cannot be
    # validated. Deleting them is correct on a downgrade: the claims they support are
    # about a kind of evidence this schema version cannot express.
    op.execute("DELETE FROM lmm_claim_provenance WHERE evidence_kind = 'incident'")
    op.execute(_DROP_INCIDENT_KIND)
