"""Store how much an episode is worth keeping, beside how likely it is to be true.

`memory_claims` records confidence — the estimated probability a claim is correct
— and nothing about whether it is worth remembering. Those are different
quantities on incomparable scales, and the reason for a second set of columns
rather than one more number folded into the first is that averaging them would
produce a figure that answers neither question.

Four columns, additive and all nullable:

- `salience` NUMERIC(4,3): the weighted sum over the write-time signals. Null
  means "not scored", which is the honest state for every row written before
  this revision and for any claim whose originating events are gone.
- `salience_signals` JSONB: the per-signal values the sum was computed from, so
  a reader asking why a claim scored as it did gets the answer that was true at
  the time rather than one recomputed against a window that has since moved.
  Same discipline as `confidence_inputs`.
- `salience_weights_id` TEXT: which governed magnitude produced it. Without it a
  weighting change makes every historical salience unreproducible, in exactly
  the way `scorer_version` exists to prevent for confidence.
- `salience_novelty` NUMERIC(4,3): the sixth signal, filled by the embedding
  consumer when the vector lands. Deliberately its own column rather than a key
  inside `salience_signals`, because it arrives later than the others and a
  JSONB key that is sometimes absent cannot be distinguished from one that is
  absent because nobody computed it.

No CHECK tying the four together. The obvious constraint — salience non-null iff
the other three are — would be wrong for the state this design is built around: a
row scored synchronously has three of the four, and gets the fourth minutes later
when embedding completes. A constraint admitting that state admits almost every
state, so the invariant lives in the writer and its tests rather than being
half-expressed here.

Bounds are checked. `[0,1]` for both scores, because a weighted sum of signals
that are each in `[0,1]` under weights that sum to one cannot leave that range,
and a value outside it means the weights artifact and the writer disagree.
"""

from __future__ import annotations

from alembic import op

revision = "0058_claim_salience"
down_revision: str | None = "0057_drop_superseded_corroboration_knobs"
branch_labels: str | None = None
depends_on: str | None = None

_COLUMNS = (
    "salience NUMERIC(4, 3)",
    "salience_signals JSONB",
    "salience_weights_id TEXT",
    "salience_novelty NUMERIC(4, 3)",
)

_BOUNDS = """
ALTER TABLE memory_claims ADD CONSTRAINT ck_memory_claims_salience_range CHECK (
    (salience IS NULL OR (salience >= 0 AND salience <= 1))
    AND (salience_novelty IS NULL OR (salience_novelty >= 0 AND salience_novelty <= 1))
)
"""

#: Partial, because the rows worth indexing are the scored ones and a retention
#: sweep asks for the least salient. An unfiltered index would carry every
#: pre-existing NULL for no read.
#:
#: Keyed on `owning_tenant_id`, which is what this table has -- there is no bare
#: `tenant_id` column on `memory_claims`, and a retention sweep is the owner's
#: decision rather than the author's.
_INDEX = """
CREATE INDEX ix_memory_claims_salience
    ON memory_claims (owning_tenant_id, salience)
    WHERE salience IS NOT NULL
"""


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE memory_claims ADD COLUMN {column}")
    op.execute(_BOUNDS)
    op.execute(_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_claims_salience")
    op.execute("ALTER TABLE memory_claims DROP CONSTRAINT ck_memory_claims_salience_range")
    for column in ("salience_novelty", "salience_weights_id", "salience_signals", "salience"):
        op.execute(f"ALTER TABLE memory_claims DROP COLUMN {column}")
