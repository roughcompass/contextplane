"""Event corpus sizing: the denominator the compression ratio needs.

`lmm_claims` records `size_bytes`, `token_count`, and `tokenizer_id` per row --
the numerator of "how much smaller is the claim than the text it came from".
`memory_session_events` records none of them, so the denominator does not exist
and the ratio cannot be computed at all. The asymmetry is easy to miss precisely
because one of the two tables has the columns.

**Why this could not wait.** Retention is not the pressure: the expiry worker
soft-invalidates and never touches `body`, so bytes stay recoverable. Erasure is.
Actor erasure issues an unconditional DELETE, so every request served before
these columns exist permanently removes that actor's contribution to the
denominator. Unlike most measurement gaps this one destroys its own inputs.

**Column names and shapes match `lmm_claims` exactly.** Two tables describing the
same quantity under different names is how a ratio silently ends up comparing
unlike things -- and a ratio that is wrong in a plausible direction is worse than
one that is obviously missing.

**`size_bytes` is added nullable, backfilled, then constrained.** Adding it NOT
NULL would fail on any deployment that has events. The backfill is
`octet_length(body)`, which is the same quantity the ingest path computes, not an
approximation of it.

**`token_count` stays NULL, and NULL means not-yet-counted.** Never zero. An
accounting tokenizer is a separate decision with its own timing; recording a zero
now would make an uncounted event indistinguishable from an empty one, and any
later reader summing the column would under-report the denominator without
anything looking wrong.
"""

from __future__ import annotations

from alembic import op

revision = "0029_session_event_sizing"
down_revision = "0028_claim_source_authority"
branch_labels = None
depends_on = None


_ADD_NULLABLE = """
ALTER TABLE memory_session_events
    ADD COLUMN size_bytes  INTEGER,
    ADD COLUMN token_count INTEGER,
    ADD COLUMN tokenizer_id TEXT
"""

# The same quantity the ingest path computes from the encoded body, not an
# estimate of it. Rows written before this migration are measured exactly.
_BACKFILL = """
UPDATE memory_session_events
   SET size_bytes = octet_length(body)
 WHERE size_bytes IS NULL
"""

_CONSTRAIN = """
ALTER TABLE memory_session_events
    ALTER COLUMN size_bytes SET NOT NULL,
    ADD CONSTRAINT ck_mse_size CHECK (size_bytes >= 0),
    -- Mirrors ck_lmm_claims_tokenizer: a count without a tokenizer identity
    -- cannot be compared to any other count, and a tokenizer identity without
    -- a count says nothing.
    ADD CONSTRAINT ck_mse_tokenizer CHECK (
        (token_count IS NULL) = (tokenizer_id IS NULL)
    )
"""


def upgrade() -> None:
    op.execute(_ADD_NULLABLE)
    op.execute(_BACKFILL)
    op.execute(_CONSTRAIN)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE memory_session_events "
        "  DROP CONSTRAINT IF EXISTS ck_mse_tokenizer, "
        "  DROP CONSTRAINT IF EXISTS ck_mse_size, "
        "  DROP COLUMN IF EXISTS tokenizer_id, "
        "  DROP COLUMN IF EXISTS token_count, "
        "  DROP COLUMN IF EXISTS size_bytes"
    )
