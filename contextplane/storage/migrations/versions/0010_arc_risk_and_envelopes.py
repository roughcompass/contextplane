"""Risk classification and expected-impact envelopes -- the last prerequisite
`ArtifactMaterialisationService.submit` needs before it can enable submission.

Revision ID: 0010_arc_risk_and_envelopes
Revises: 0009_arc_approval_challenges
Create Date: 2026-08-06

`arc_risk_classifications` is the sticky, immutable record of one proposal
version's computed risk classification and the algorithm version that
produced it -- distinct from the two summary columns `0005_arc_authoring_
proposals.py` already added to `arc_authoring_proposal_versions`
(`risk_classification`, `risk_algorithm_version`), which are a read-path
cache the same write also populates. Later recomputation (approval,
qualification, activation) compares its own fresh result against this row,
never against the cache columns, so a future rewrite of the cache can never
also rewrite the sticky record it is supposed to be compared against.

`arc_expected_impact_envelopes`/`arc_expected_impact_envelope_items` freeze
the proposer's declared `arc_expected_impact_envelope_v1` object exactly
once per proposal version (`UNIQUE (proposal_id, proposal_version)`).
Predicate-key allowlisting, empty-set rejection, and item non-overlap are
service-enforced in `envelope.py` before this table is ever written -- a
closed field-set schema cannot express "no two items overlap," and the
service's own refusal code (`arc_envelope_invalid`) is more specific than
anything a raw CHECK violation could report. The CHECK constraints below
are the defense-in-depth layer, not the primary enforcement point: they
close the classification and delta-code vocabularies and the count-range
invariant at the row level, matching every other closed-vocabulary table
this phase has added.
"""

from __future__ import annotations

from alembic import op

revision = "0010_arc_risk_and_envelopes"
down_revision: str | None = "0009_arc_approval_challenges"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# arc_risk_classifications -- the sticky result + algorithm version.
# ---------------------------------------------------------------------------

_RISK_CLASSIFICATIONS_DDL = """
CREATE TABLE arc_risk_classifications (
    proposal_id        UUID NOT NULL,
    proposal_version   INTEGER NOT NULL,
    classification      TEXT NOT NULL,
    algorithm_version    TEXT NOT NULL,
    computed_at           TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (proposal_id, proposal_version),
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version),
    CONSTRAINT ck_arc_risk_classifications_classification CHECK (
        classification IN (
            'global_mandatory', 'global_non_mandatory',
            'tenant_mandatory', 'tenant_non_mandatory',
            'domain_mandatory', 'domain_non_mandatory',
            'capability_mandatory', 'capability_non_mandatory',
            'task_mandatory', 'task_non_mandatory'
        )
    )
)
"""

# ---------------------------------------------------------------------------
# arc_expected_impact_envelopes -- one frozen envelope per proposal version.
# ---------------------------------------------------------------------------

_ENVELOPES_DDL = """
CREATE TABLE arc_expected_impact_envelopes (
    envelope_id        UUID PRIMARY KEY,
    proposal_id        UUID NOT NULL,
    proposal_version    INTEGER NOT NULL,
    envelope_digest      TEXT NOT NULL,
    author_issuer         TEXT NOT NULL,
    author_subject         TEXT NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (proposal_id, proposal_version)
        REFERENCES arc_authoring_proposal_versions (proposal_id, proposal_version),
    CONSTRAINT uq_arc_expected_impact_envelopes_version UNIQUE (proposal_id, proposal_version)
)
"""

# ---------------------------------------------------------------------------
# arc_expected_impact_envelope_items -- items keyed (envelope_id, item_id)
# per Appendix B.3.
# ---------------------------------------------------------------------------

_ENVELOPE_ITEMS_DDL = """
CREATE TABLE arc_expected_impact_envelope_items (
    envelope_id        UUID NOT NULL REFERENCES arc_expected_impact_envelopes(envelope_id),
    item_id             TEXT NOT NULL,
    delta_code           TEXT NOT NULL,
    class_predicate        JSONB NOT NULL,
    minimum_count           INTEGER NOT NULL,
    maximum_count             INTEGER,
    rationale_code              TEXT NOT NULL,
    PRIMARY KEY (envelope_id, item_id),
    CONSTRAINT ck_arc_expected_impact_envelope_items_delta_code CHECK (
        delta_code IN (
            'newly_selected', 'no_longer_selected', 'conflict_changed',
            'mandatory_block_added', 'mandatory_block_removed'
        )
    ),
    CONSTRAINT ck_arc_expected_impact_envelope_items_minimum_nonneg CHECK (minimum_count >= 0),
    CONSTRAINT ck_arc_expected_impact_envelope_items_range CHECK (
        maximum_count IS NULL OR maximum_count >= minimum_count
    )
)
"""


def upgrade() -> None:
    # Statements are issued one per op.execute -- asyncpg requires single
    # statements at the prepare layer; multi-statement scripts fail.
    op.execute(_RISK_CLASSIFICATIONS_DDL)
    op.execute(_ENVELOPES_DDL)
    op.execute(_ENVELOPE_ITEMS_DDL)


def downgrade() -> None:
    # Reverse dependency order: items reference envelopes; risk
    # classifications and envelopes both reference proposal_versions only.
    op.execute("DROP TABLE arc_expected_impact_envelope_items")
    op.execute("DROP TABLE arc_expected_impact_envelopes")
    op.execute("DROP TABLE arc_risk_classifications")
