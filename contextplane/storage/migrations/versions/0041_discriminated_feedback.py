"""Feedback as a closed union, with the linkage each member is allowed to have.

Feedback about a resolution comes in exactly three shapes, and the difference
between them is what may be learned from them. One row type with optional columns
would let all three be spelled the same way and leave "is this item-specific?" to
be answered by inspecting which columns happen to be filled -- which is a question
about the data rather than a statement of the contract. The discriminant is a
column, and what it permits is a constraint.

- **item-specific** cites a receipt *and* an exact item on it;
- **receipt-level** cites a receipt and must not cite an item, because feedback on
  a whole answer is not evidence about any one line of it;
- **diagnostic observation** cites neither and can never be learning-eligible.

The third exists so a reporter can say "something was wrong here" without that
statement becoming training evidence about a specific retrieved item. Collapsing
it into receipt-level feedback is what turns an unattributable complaint into a
citation the derivation path will happily consume.

**The exact-item rule is a composite foreign key, not an id column.** A
`receipt_item_id` on its own would let feedback cite item X while naming receipt
Y, and the pair would look valid in every individual column check. The reference
is `(receipt_id, receipt_item_id)` against the unique key `0032` already declares
on `context_receipt_items`, so an item that does not belong to the cited receipt
is unstorable rather than merely discouraged. Nullability does the rest of the
work: SQL does not enforce a composite foreign key when any of its columns is
NULL, which is exactly the behaviour the other two members need.

**Deleting a receipt does not delete what people said about it.** The reference
is deliberately not `ON DELETE CASCADE`, unlike the receipt's own items: a
retention path that removes a receipt must decide explicitly what happens to the
feedback citing it, and the default here fails that delete rather than silently
destroying the record that a human reported a problem. Redaction and tombstoning
are policy questions, and this constraint's job is to stop them being answered by
accident.

**Implicit outcomes are not ratings, and the schema says so by omission.** An
external system reporting that a run failed is an observation and belongs in the
signal ledger; it is not a reporter asserting that an answer was wrong. Nothing
here accepts a signal id, so the two cannot be conflated by a write path that
finds it convenient.
"""

from __future__ import annotations

from alembic import op

revision = "0041_discriminated_feedback"
down_revision: str | None = "0040_external_signals"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# The three members of the union. Closed here because every downstream rule about
# what may be learned is written against exactly these.
_FEEDBACK_KINDS = "'item_specific', 'receipt_level', 'diagnostic_observation'"

# The bounded vocabulary. Closed for the same reason the classification list is:
# a verdict nobody declared is one no learning or evaluation rule accounts for.
_RATINGS = (
    "'relevant', 'irrelevant', 'missing', 'stale', 'incorrect', 'contradicted', "
    "'unsafe', 'selected', 'ignored', 'succeeded', 'failed', 'rolled_back', "
    "'needs_human_review'"
)

# Who reported it. The same three kinds the signal ledger closes, and the same
# reason: the privacy and learning-eligibility rules are written against them.
_REPORTER_TYPES = "'human', 'agent', 'external'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE context_feedback (
            feedback_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

            -- Which member of the union this row is. Everything below about
            -- what it may cite is enforced against this column.
            kind            TEXT NOT NULL,

            -- The linkage. Both nullable so the three members can be expressed;
            -- which of them may be NULL is not left to the writer, see the
            -- discriminant constraint below.
            receipt_id      UUID,
            receipt_item_id TEXT,

            rating          TEXT NOT NULL,
            -- Whether this row may be used as learning or evaluation evidence.
            -- Stored rather than derived from `kind`: a reporter may withhold an
            -- otherwise-eligible rating from learning, and a later reader must
            -- see the decision that was made rather than recompute today's.
            learning_eligible BOOLEAN NOT NULL,

            -- Free text, and the field most likely to carry something personal.
            -- Minimized rather than deleted under the retention policy, which is
            -- why it is a nullable column of its own and never part of a key.
            note            TEXT,

            reporter_id     TEXT NOT NULL,
            reporter_type   TEXT NOT NULL,

            -- Exact replay converges; changed replay under a reused key is a
            -- conflict the unique index below refuses. Same shape as the signal
            -- ledger, for the same reason: the digest makes replay decidable
            -- without keeping a body to compare.
            idempotency_key TEXT NOT NULL,
            content_digest  TEXT NOT NULL,

            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT ck_feedback_kind CHECK (kind IN ({_FEEDBACK_KINDS})),
            CONSTRAINT ck_feedback_rating CHECK (rating IN ({_RATINGS})),
            CONSTRAINT ck_feedback_reporter_type CHECK (reporter_type IN ({_REPORTER_TYPES})),
            CONSTRAINT ck_feedback_identity_present
                CHECK (
                    length(reporter_id) > 0
                    AND length(idempotency_key) > 0
                    AND length(content_digest) > 0
                    AND idempotency_key = btrim(idempotency_key)
                ),

            -- The union itself. Each member names exactly what it must and must
            -- not cite; a row matching none of the three has no shape and is
            -- refused rather than stored as a fourth kind nobody designed.
            CONSTRAINT ck_feedback_discriminant
                CHECK (
                    (kind = 'item_specific'
                        AND receipt_id IS NOT NULL AND receipt_item_id IS NOT NULL)
                    OR (kind = 'receipt_level'
                        AND receipt_id IS NOT NULL AND receipt_item_id IS NULL)
                    OR (kind = 'diagnostic_observation'
                        AND receipt_id IS NULL AND receipt_item_id IS NULL)
                ),

            -- A diagnostic observation cites nothing, so nothing can check what
            -- it refers to; letting one be learning-eligible would admit
            -- unattributable evidence to the derivation path.
            CONSTRAINT ck_feedback_diagnostic_never_learns
                CHECK (kind <> 'diagnostic_observation' OR learning_eligible = FALSE),

            -- The exact-item rule. Not enforced when either column is NULL,
            -- which is what lets the other two members exist; enforced as a pair
            -- when both are present, which is what stops feedback citing an item
            -- belonging to some other receipt.
            CONSTRAINT fk_feedback_exact_receipt_item
                FOREIGN KEY (receipt_id, receipt_item_id)
                REFERENCES context_receipt_items (receipt_id, receipt_item_id),

            -- Receipt-level feedback still has to name a receipt that exists.
            -- The composite key above cannot carry this: it is not enforced when
            -- `receipt_item_id` is NULL, which is precisely that member's shape.
            CONSTRAINT fk_feedback_receipt
                FOREIGN KEY (receipt_id) REFERENCES context_receipts (receipt_id)
        )
        """
    )

    # Exact replay is one row per reporter per key.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_feedback_idempotency
            ON context_feedback (tenant_id, reporter_id, idempotency_key)
        """
    )
    # "What was said about this resolution" -- the read every review path starts
    # from, and the one a receipt-deletion check has to run.
    op.execute("CREATE INDEX ix_feedback_by_receipt ON context_feedback (receipt_id) WHERE receipt_id IS NOT NULL")
    # "What may this tenant learn from" -- the derivation path's own selection,
    # partial because ineligible rows are never in its candidate set.
    op.execute(
        """
        CREATE INDEX ix_feedback_learning_candidates
            ON context_feedback (tenant_id, created_at DESC)
            WHERE learning_eligible
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS context_feedback")
