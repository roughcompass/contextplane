"""Closing a claim bi-temporally instead of overwriting it.

A superseded claim is closed, not replaced: `t_invalidated_at` is set and its status
becomes `superseded`, while the surviving claim is a new row. Nothing is overwritten
in place and nothing is physically deleted outside an erasure request.

**The chain is the trust signal.** "What did we believe about this last month, and why
did it change?" is the question a governed store exists to answer, and it is
unanswerable the moment a row is updated in place. Keeping the closed claim is also
what makes a mistaken supersession recoverable -- the previous belief is still there,
with its own score and its own provenance.

**Deliberately further than a workspace revision model.** A revision keeps the latest
version and a history of edits; this keeps every claim that was ever current, each
with the confidence it carried at the time. The difference matters because confidence
is the thing being audited, not the text.

**Consolidation is idempotent, and this is where that is enforced.** Re-running a
sweep over an unchanged neighbourhood must produce no new rows, no duplicate audit
entries, and no confidence drift. A unique index on the surviving-claim relationship
is what makes a second attempt a no-op rather than a second closure.
"""

from __future__ import annotations

from alembic import op

revision = "0038_claim_supersession"
down_revision = "0037_calibration"
branch_labels = None
depends_on = None


_CLAIM_COLUMNS = """
ALTER TABLE lmm_claims
    -- Transaction time: when the store stopped believing this, as distinct from
    -- when the claim stopped holding, which is `asserted_valid_to`. A claim can be
    -- superseded long after the fact it asserted ceased to be true.
    ADD COLUMN t_invalidated_at TIMESTAMPTZ,
    -- Why it was closed. A status of `superseded` says a claim is no longer current
    -- without saying whether it lost a conflict, was collapsed into a duplicate, or
    -- was replaced by a human confirmation -- and those are different histories.
    ADD COLUMN superseded_reason TEXT,

    -- When this claim was last reconciled against its neighbourhood. What makes a
    -- repeated sweep genuinely idempotent rather than merely harmless: without it,
    -- a second pass reaches the same conclusion and writes a second audit row for
    -- an event that happened once, and the log becomes a record of how often the
    -- sweep ran rather than of what it decided.
    --
    -- Compared against the neighbourhood's newest member rather than treated as a
    -- one-shot flag, because a claim arriving later genuinely changes the answer and
    -- must be reconsidered.
    ADD COLUMN consolidated_at TIMESTAMPTZ,

    -- The two travel together. A closed claim with no timestamp cannot be excluded
    -- from a point-in-time query, and a timestamp on a live claim would exclude it
    -- from every one.
    ADD CONSTRAINT ck_lmm_claims_invalidated CHECK (
        (t_invalidated_at IS NULL) = (status <> 'superseded')
    ),
    ADD CONSTRAINT ck_lmm_claims_superseded_reason CHECK (
        superseded_reason IS NULL
        OR superseded_reason IN (
            'lost_conflict', 'cluster_collapsed', 'human_confirmed', 'curator_replaced'
        )
    ),
    -- A closed claim names its successor. Enforced rather than assumed, because a
    -- chain with a gap cannot be walked, and walking it is the whole point.
    ADD CONSTRAINT ck_lmm_claims_superseded_has_successor CHECK (
        status <> 'superseded' OR superseded_by IS NOT NULL
    )
"""

_CLAIM_INDEXES = [
    # A point-in-time query asks which claims were current at an instant, which is
    # every claim created before it and either still live or closed after it.
    "CREATE INDEX ix_lmm_claims_as_of ON lmm_claims " "(subject_entity_id, predicate, created_at, t_invalidated_at)",
    # Walking a supersession chain forward from any point.
    "CREATE INDEX ix_lmm_claims_superseded_by ON lmm_claims (superseded_by) " "WHERE superseded_by IS NOT NULL",
    # One live claim per successor. What makes a repeated sweep a no-op rather than
    # a second closure: a claim already pointing at a survivor cannot be closed
    # again in favour of a different one.
    "CREATE UNIQUE INDEX uq_lmm_claims_one_closure ON lmm_claims (claim_id) " "WHERE status = 'superseded'",
    # The sweep's own claim query: live claims never reconciled, or reconciled
    # before something newer arrived.
    "CREATE INDEX ix_lmm_claims_unconsolidated ON lmm_claims "
    "(subject_entity_id, predicate) WHERE status = 'staged' AND consolidated_at IS NULL",
]

# Which claims were collapsed into which survivor, and what each contributed.
#
# A separate table rather than a column, because a collapse is many-to-one and the
# merged provenance has to remain attributable: after collapsing twenty phrasings,
# "which sessions said this" must still be answerable, and a single successor
# pointer loses the count.
_CLUSTER = """
CREATE TABLE lmm_claim_cluster (
    cluster_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    survivor_claim_id UUID NOT NULL REFERENCES lmm_claims(claim_id) ON DELETE CASCADE,
    collapsed_claim_id UUID NOT NULL REFERENCES lmm_claims(claim_id) ON DELETE CASCADE,

    -- How alike they were, by whatever measure decided it. Recorded so a threshold
    -- change can be evaluated against past decisions instead of guessed at.
    similarity       NUMERIC(4, 3) NOT NULL,
    -- Which arm found it: an exact typed match, or semantic proximity. The first is
    -- decidable without a model and the second is not, and a reviewer checking a
    -- questionable collapse wants to know which one it was.
    matched_by       TEXT NOT NULL,

    collapsed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_lmm_cluster_pair UNIQUE (survivor_claim_id, collapsed_claim_id),
    CONSTRAINT ck_lmm_cluster_distinct CHECK (survivor_claim_id <> collapsed_claim_id),
    CONSTRAINT ck_lmm_cluster_similarity CHECK (similarity >= 0 AND similarity <= 1),
    CONSTRAINT ck_lmm_cluster_matched_by CHECK (
        matched_by IN ('exact_value', 'semantic')
    )
)
"""

_CLUSTER_INDEXES = [
    "CREATE INDEX ix_lmm_cluster_survivor ON lmm_claim_cluster (survivor_claim_id)",
    "CREATE INDEX ix_lmm_cluster_collapsed ON lmm_claim_cluster (collapsed_claim_id)",
]


def upgrade() -> None:
    op.execute(_CLAIM_COLUMNS)
    for statement in _CLAIM_INDEXES:
        op.execute(statement)
    op.execute(_CLUSTER)
    for statement in _CLUSTER_INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_claim_cluster")
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_unconsolidated")
    op.execute("DROP INDEX IF EXISTS uq_lmm_claims_one_closure")
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_superseded_by")
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_as_of")
    op.execute(
        "ALTER TABLE lmm_claims "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_superseded_has_successor, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_superseded_reason, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_invalidated, "
        "  DROP COLUMN IF EXISTS consolidated_at, "
        "  DROP COLUMN IF EXISTS superseded_reason, "
        "  DROP COLUMN IF EXISTS t_invalidated_at"
    )
