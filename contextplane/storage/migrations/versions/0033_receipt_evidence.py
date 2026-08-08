"""What a receipt needs to be evidence rather than a summary.

`0032` recorded that a resolution happened, which arms answered, and which items
came back. That is enough to reconstruct the shape of an answer and not enough
to weigh it. This adds the three things a reader actually needs to judge one.

**Trust is eight typed columns, not a JSONB blob.** The release gate has to
assert trust-label coverage across every surface and report which arms were
stale, excluded, truncated or timed out. A gate cannot assert coverage over a
blob without reimplementing the schema inside the gate, and the copy inside the
gate is the one that stops matching. Eight columns cost eight `ALTER`s once;
a blob costs a schema nobody can query and a gate nobody trusts.

**Exclusions are a child table, because an arm has zero or many.** A column
cannot hold them, and a JSON array in one would make "which items were withheld
from this block" a scan. Keyed `(receipt_id, block, item_key)` so it is one
indexed read -- which matters because the exclusion list is the part a reader
consults when an answer looks thin.

**Withholding is the fact this whole table exists to keep.** An item dropped for
authorization is the difference between "there was nothing" and "there was
something you may not see", and only the second tells a reader to go and ask
somebody. It is also unreconstructable after the fact: the envelope carries what
survived, so if the write path does not record what it dropped, nothing later
can.
"""

from __future__ import annotations

from alembic import op

revision = "0033_receipt_evidence"
down_revision: str | None = "0032_context_receipts"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # --- The request itself -------------------------------------------------
    #
    # Two resolutions of the same request should be comparable, and without the
    # request's own digest a reader can only compare what came back -- which
    # differs for reasons that have nothing to do with the question asked.
    op.execute("ALTER TABLE context_receipts ADD COLUMN request_digest TEXT")

    # --- What each arm cost, and what it dropped ----------------------------
    #
    # `considered` and `returned` are both recorded because their difference is
    # the whole story: an arm that returned three of three is not the same
    # answer as one that returned three of nine hundred, and the block state
    # alone cannot tell them apart.
    #
    # Two truncations, kept apart: the arm stopped at its own limit, or the
    # assembler cut it at the cap. An operator tuning one needs to know which.
    #
    # `fresh_as_of` NULL means the arm does not track freshness, which is not
    # the same as fresh and must not be read as such -- hence a separate `stale`
    # column rather than deriving staleness from a NULL timestamp at read time.
    op.execute(
        """
        ALTER TABLE context_receipt_arms
            ADD COLUMN considered       INTEGER,
            ADD COLUMN returned         INTEGER,
            ADD COLUMN truncated_by_arm BOOLEAN,
            ADD COLUMN truncated_by_cap BOOLEAN,
            ADD COLUMN fresh_as_of      TIMESTAMPTZ,
            ADD COLUMN stale            BOOLEAN,
            ADD COLUMN duration_ms      INTEGER
        """
    )
    op.execute(
        """
        ALTER TABLE context_receipt_arms
            ADD CONSTRAINT ck_receipt_arms_counts_nonneg CHECK (
                (considered IS NULL OR considered >= 0)
                AND (returned IS NULL OR returned >= 0)
                AND (duration_ms IS NULL OR duration_ms >= 0)
            )
        """
    )
    # An arm cannot return more than it considered. A row that does is a writer
    # bug, and catching it here means the receipt never records an impossible
    # selection rather than a reader discovering one much later.
    op.execute(
        """
        ALTER TABLE context_receipt_arms
            ADD CONSTRAINT ck_receipt_arms_returned_within_considered CHECK (
                considered IS NULL OR returned IS NULL OR returned <= considered
            )
        """
    )

    # --- Trust, per item ----------------------------------------------------
    #
    # The eight fields of the frozen trust contract, one column each, plus the
    # exact source record this item came from. `source_revision` and
    # `source_digest` are what make a citation checkable: without them a receipt
    # names a document, and the document has since changed.
    #
    # `trust_source` rather than `source`: the items table already has a
    # `source` column carrying the receipt item id's own source component, and
    # two columns of that name in one row would be read wrong exactly once.
    op.execute(
        """
        ALTER TABLE context_receipt_items
            ADD COLUMN trust           TEXT,
            ADD COLUMN trust_source    TEXT,
            ADD COLUMN assertion_kind  TEXT,
            ADD COLUMN authority       TEXT,
            ADD COLUMN freshness       TIMESTAMPTZ,
            ADD COLUMN mutability      TEXT,
            ADD COLUMN attribution     TEXT,
            ADD COLUMN classification  TEXT,
            ADD COLUMN source_revision TEXT,
            ADD COLUMN source_digest   TEXT
        """
    )
    # Canonical items carry no trust by contract; every other block's items
    # must. Enforced here as well as in memory because a row can arrive by a
    # path the contract object never touched, and an untrusted item in an ARC or
    # workspace block is invalid rather than merely unlabelled.
    op.execute(
        """
        ALTER TABLE context_receipt_items
            ADD CONSTRAINT ck_receipt_items_trust_outside_canonical CHECK (
                block = 'canonical' OR trust IS NOT NULL
            )
        """
    )

    # --- What was withheld --------------------------------------------------
    op.execute(
        """
        CREATE TABLE context_receipt_exclusions (
            exclusion_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            receipt_id   UUID NOT NULL REFERENCES context_receipts(receipt_id) ON DELETE CASCADE,
            block        TEXT NOT NULL,
            item_key     TEXT NOT NULL,
            -- Required, not optional. A withheld item with no reason tells a
            -- reader something was kept back and gives them no way to find out
            -- whether to ask for access or report a bug.
            reason       TEXT NOT NULL,
            CONSTRAINT ck_receipt_exclusions_reason_present CHECK (length(trim(reason)) > 0),
            CONSTRAINT uq_receipt_exclusions_identity UNIQUE (receipt_id, block, item_key)
        )
        """
    )
    # The read this table exists for: everything one block withheld, in one
    # indexed lookup rather than a scan of the receipt's exclusions.
    op.execute("CREATE INDEX ix_receipt_exclusions_receipt_block ON context_receipt_exclusions (receipt_id, block)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS context_receipt_exclusions")

    op.execute("ALTER TABLE context_receipt_items DROP CONSTRAINT IF EXISTS ck_receipt_items_trust_outside_canonical")
    op.execute(
        """
        ALTER TABLE context_receipt_items
            DROP COLUMN source_digest,
            DROP COLUMN source_revision,
            DROP COLUMN classification,
            DROP COLUMN attribution,
            DROP COLUMN mutability,
            DROP COLUMN freshness,
            DROP COLUMN authority,
            DROP COLUMN assertion_kind,
            DROP COLUMN trust_source,
            DROP COLUMN trust
        """
    )

    op.execute("ALTER TABLE context_receipt_arms DROP CONSTRAINT IF EXISTS ck_receipt_arms_returned_within_considered")
    op.execute("ALTER TABLE context_receipt_arms DROP CONSTRAINT IF EXISTS ck_receipt_arms_counts_nonneg")
    op.execute(
        """
        ALTER TABLE context_receipt_arms
            DROP COLUMN duration_ms,
            DROP COLUMN stale,
            DROP COLUMN fresh_as_of,
            DROP COLUMN truncated_by_cap,
            DROP COLUMN truncated_by_arm,
            DROP COLUMN returned,
            DROP COLUMN considered
        """
    )

    op.execute("ALTER TABLE context_receipts DROP COLUMN request_digest")
