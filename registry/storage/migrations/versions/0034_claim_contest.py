"""Marking a claim contested, and recording what it disagrees with.

Two claims about one subject, under one single-valued predicate, with values that
cannot both hold over overlapping intervals, are a disagreement. Recording it is
not optional: silent coexistence is how a store ends up serving two answers to one
question with nothing indicating that it is doing so.

**A contested claim is not a wrong claim.** It is a claim the store cannot resolve
by itself. Both may be well-formed, well-provenanced, and sincerely asserted --
one of them is simply out of date, or one source is mistaken, and deciding which
needs either authority-aware resolution or a person. So contesting lowers
confidence and blocks promotion; it never deletes and never picks a winner.

**The pair is recorded, not just the flag.** "This claim is contested" is not
actionable. "This claim is contested by that one, over these values, detected
then" is. Without the pair, a reviewer cannot see what the disagreement was, and
a later resolution cannot tell whether it addressed this disagreement or a
different one.

**Ordered pairs are stored once, not twice.** The lower claim id first, so a
disagreement between two claims has exactly one row however many times the sweep
revisits it. A second row would double the confidence penalty on re-detection.
"""

from __future__ import annotations

from alembic import op

revision = "0034_claim_contest"
down_revision = "0033_arc_closed_vocabularies"
branch_labels = None
depends_on = None


_CONTEST = """
CREATE TABLE lmm_claim_contest (
    contest_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Ordered so one disagreement is one row. Which claim is "first" carries no
    -- meaning beyond making the pair canonical.
    lower_claim_id UUID NOT NULL REFERENCES lmm_claims(claim_id) ON DELETE CASCADE,
    upper_claim_id UUID NOT NULL REFERENCES lmm_claims(claim_id) ON DELETE CASCADE,

    -- Denormalized so a reviewer can see the disagreement without joining, and
    -- so the record survives as evidence of what was compared even if a later
    -- supersession rewrites one of the claims.
    subject_entity_id UUID NOT NULL REFERENCES entities(entity_id),
    predicate      TEXT NOT NULL,
    lower_value    JSONB NOT NULL,
    upper_value    JSONB NOT NULL,

    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when consolidation or a person settles it. Kept rather than deleted:
    -- that a disagreement happened is a fact about the store's history, and a
    -- resolution that erased its own cause could not be reviewed.
    resolved_at    TIMESTAMPTZ,
    resolution     TEXT,

    CONSTRAINT uq_lmm_contest_pair UNIQUE (lower_claim_id, upper_claim_id),
    -- A claim cannot disagree with itself, and an unordered pair would let the
    -- same disagreement be stored twice.
    CONSTRAINT ck_lmm_contest_ordered CHECK (lower_claim_id < upper_claim_id),
    CONSTRAINT ck_lmm_contest_resolution CHECK (
        (resolved_at IS NULL) = (resolution IS NULL)
    ),
    CONSTRAINT ck_lmm_contest_resolution_value CHECK (
        resolution IS NULL
        OR resolution IN ('superseded', 'both_retained', 'dismissed', 'claim_withdrawn')
    )
)
"""

_CONTEST_INDEXES = [
    # Every open disagreement involving a claim, which is what the read path and
    # the review queue both ask for.
    "CREATE INDEX ix_lmm_contest_lower ON lmm_claim_contest (lower_claim_id) " "WHERE resolved_at IS NULL",
    "CREATE INDEX ix_lmm_contest_upper ON lmm_claim_contest (upper_claim_id) " "WHERE resolved_at IS NULL",
    # The review queue, per subject.
    "CREATE INDEX ix_lmm_contest_subject ON lmm_claim_contest "
    "(subject_entity_id, predicate) WHERE resolved_at IS NULL",
]

# `is_contested` is a cached answer to "does an unresolved row exist for this
# claim", carried on the claim so the read path and the promotion gate need no
# join. Denormalized deliberately: promotion eligibility must not depend on a
# query that could be forgotten.
_CLAIM_COLUMN = """
ALTER TABLE lmm_claims
    ADD COLUMN is_contested BOOLEAN NOT NULL DEFAULT FALSE
"""

_CLAIM_INDEX = (
    "CREATE INDEX ix_lmm_claims_contested ON lmm_claims (subject_entity_id, predicate) "
    "WHERE is_contested AND status = 'staged'"
)

# The sweep that looks for disagreements only ever reads single-valued
# predicates, and most of the ontology is set-valued. Partial so it does not walk
# what it will immediately discard.
_SWEEP_INDEX = (
    "CREATE INDEX ix_lmm_claims_single_valued ON lmm_claims "
    "(subject_entity_id, predicate, asserted_valid_from) "
    "WHERE status = 'staged' AND value_cardinality = 'single'"
)

# Copied onto the claim at write time, like the value type and category already
# are, so the sweep never re-reads the vocabulary to learn whether a predicate
# can disagree with itself.
_CARDINALITY_COLUMN = """
ALTER TABLE lmm_claims
    ADD COLUMN value_cardinality TEXT NOT NULL DEFAULT 'multi',
    ADD CONSTRAINT ck_lmm_claims_value_cardinality CHECK (
        value_cardinality IN ('single', 'multi')
    )
"""

# An entity reference resolved once, on the write path, so comparing two
# references stays a function of two rows. Comparing at read time would answer
# "do these agree now" and could never be re-derived.
_VALUE_ENTITY_COLUMN = """
ALTER TABLE lmm_claims
    ADD COLUMN value_entity_id UUID REFERENCES entities(entity_id),
    ADD CONSTRAINT ck_lmm_claims_value_entity CHECK (
        value_entity_id IS NULL OR value_type = 'entity_ref'
    )
"""


def upgrade() -> None:
    op.execute(_CARDINALITY_COLUMN)
    op.execute(_VALUE_ENTITY_COLUMN)
    op.execute(_CLAIM_COLUMN)
    op.execute(_CONTEST)
    for statement in _CONTEST_INDEXES:
        op.execute(statement)
    op.execute(_CLAIM_INDEX)
    op.execute(_SWEEP_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_single_valued")
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_contested")
    op.execute("DROP TABLE IF EXISTS lmm_claim_contest")
    op.execute(
        "ALTER TABLE lmm_claims "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_value_entity, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_value_cardinality, "
        "  DROP COLUMN IF EXISTS is_contested, "
        "  DROP COLUMN IF EXISTS value_entity_id, "
        "  DROP COLUMN IF EXISTS value_cardinality"
    )
