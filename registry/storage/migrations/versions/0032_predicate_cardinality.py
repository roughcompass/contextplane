"""How many values of a predicate may hold at once.

Without this, a claim store cannot tell a disagreement from a second fact. A
capability depends on many things, so two `depends_on` values are two
dependencies; a capability has one accountable team, so two `owned_by_team`
values mean one of them is wrong. Both pairs look identical in the schema.

**The consequence of getting it wrong is not noise, it is permanence.** A
detected disagreement marks both claims contested, and a contested claim is
ineligible for promotion and always needs review. So treating a set-valued
predicate as single-valued does not merely add queue entries: it makes every
such claim permanently unpromotable, and no reviewer can resolve it, because
both values are true and neither can supersede the other.

**Declared, never inferred.** Neither the value type nor the category determines
it, and both inferences are provably wrong against the shipped vocabulary:
`steward_entity` and `depends_on` are both entity references with opposite
cardinality, and `owned_by_team` and `exposes_operation` are both strings with
opposite cardinality.

**A column rather than a constant, because the schema already permits
tenant-local predicates.** A table of the shipped terms cannot answer for a row
it has never seen, which would leave the comparison guessing for exactly the
terms it knows least about. The cardinality has to live where those rows do.

**The backfill and the conservative direction are both `multi`.** A predicate
whose cardinality nobody declared must not be treated as single-valued: that
manufactures disagreements, while treating it as a set only misses them. Those
errors are not symmetric, and only one of them is recoverable.
"""

from __future__ import annotations

from alembic import op

revision = "0032_predicate_cardinality"
down_revision = "0031_extraction_strategy_config"
branch_labels = None
depends_on = None


_COLUMN = "ALTER TABLE vocabulary_values ADD COLUMN value_cardinality TEXT"

_BACKFILL = """
UPDATE vocabulary_values
   SET value_cardinality = COALESCE(value_cardinality, 'multi')
 WHERE kind = 'claim_predicate'
"""

# Same shape as the metadata CHECK already on this table: a claim predicate
# missing a declared property cannot be reasoned with, so it must not exist.
_CONSTRAINT = """
ALTER TABLE vocabulary_values
    ADD CONSTRAINT ck_vocab_claim_predicate_cardinality CHECK (
        kind <> 'claim_predicate'
        OR value_cardinality IN ('single', 'multi')
    )
"""


def upgrade() -> None:
    op.execute(_COLUMN)
    op.execute(_BACKFILL)
    op.execute(_CONSTRAINT)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE vocabulary_values "
        "  DROP CONSTRAINT IF EXISTS ck_vocab_claim_predicate_cardinality, "
        "  DROP COLUMN IF EXISTS value_cardinality"
    )
