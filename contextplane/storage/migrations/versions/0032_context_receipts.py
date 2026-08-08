"""Context receipts: what one resolution returned, arm by arm and item by item.

A receipt exists so a reader can point at a line of an answer and ask what
produced it. That only works if the record is as granular as the answer was, so
this is three tables rather than one document: the resolution, the state of each
of its four arms, and the individual items those arms contributed.

**Per-arm state is a row, not a column.** Four columns on the receipt would make
adding an arm a schema change on the receipt itself, and would leave nowhere to
put the reason a single arm degraded. A row per arm also makes the constraint
expressible: an arm that degraded or failed must say why, which is the same rule
the envelope contract enforces in memory, enforced here for rows that arrive by
any other path.

**Receipt items carry the contract's own id, not a position.** A positional id
changes when an unrelated item is added, which would make every line of a
receipt move whenever a block's contents shift — and a receipt whose line
numbers drift is one nobody can cite. The id is the digest of block, source and
item key, computed by the frozen algorithm and written here rather than derived
in SQL.

**Items bind to external references through the existing junction.** A receipt
item that named its own reference would be a second place external identity
lives, and the two would disagree the first time a reference was normalized
differently. The `context_item` subject type in `0031` exists for exactly this.

**Nothing here cascades from a receipt to a reference.** Deleting a resolution
deletes what it returned; it must not delete the external things that resolution
happened to cite, because other receipts and other checkpoints cite them too.
"""

from __future__ import annotations

from alembic import op

revision = "0032_context_receipts"
down_revision: str | None = "0031_external_references"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# The four arms, fixed and ordered by the envelope contract. Closed here so a
# fifth cannot appear by a service passing a new string.
_BLOCK_NAMES = "'canonical', 'arc', 'observed_claims', 'workspace'"

# What one arm did. `empty` and `success` stay distinct: an arm with nothing to
# say is a complete answer, and collapsing them is what makes a broken
# integration read as "nothing exists".
_BLOCK_STATES = "'success', 'empty', 'degraded', 'failed'"

# What the response as a whole was worth. Derived from the arms, never set by
# hand -- the CHECK below cannot enforce the derivation, but the vocabulary is
# closed so a value outside it cannot be stored.
_ENVELOPE_STATES = "'complete', 'degraded', 'blocked'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE context_receipts (
            receipt_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

            -- What was being resolved for. Opaque here: the task family owns
            -- the identifier's meaning, and a foreign key would make a receipt
            -- undeleteable independently of the task it describes.
            task_id         UUID,

            state           TEXT NOT NULL,
            -- Whether the answer was safe to cache. Recorded rather than
            -- recomputed because it was a property of that resolution, and a
            -- later reader recomputing it would use today's rules.
            cacheable       BOOLEAN NOT NULL,

            resolved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- Who asked. Server-derived, like every other attribution in this
            -- schema.
            requested_by    TEXT NOT NULL,

            CONSTRAINT ck_receipt_state CHECK (state IN ({_ENVELOPE_STATES})),
            CONSTRAINT ck_receipt_requested_by_present CHECK (length(requested_by) > 0),
            -- A degraded or blocked answer is not cacheable. Caching it would
            -- outlive the failure that caused it, and the cached copy carries
            -- no sign that it was degraded when it was taken.
            CONSTRAINT ck_receipt_degraded_is_not_cacheable
                CHECK (state = 'complete' OR cacheable = FALSE)
        )
        """
    )
    op.execute("CREATE INDEX ix_receipt_tenant_time ON context_receipts (tenant_id, resolved_at DESC)")
    # "Every resolution for this task, newest first" -- the resume read.
    op.execute(
        """
        CREATE INDEX ix_receipt_task ON context_receipts (tenant_id, task_id, resolved_at DESC)
            WHERE task_id IS NOT NULL
        """
    )

    op.execute(
        f"""
        CREATE TABLE context_receipt_arms (
            arm_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            receipt_id  UUID NOT NULL REFERENCES context_receipts(receipt_id) ON DELETE CASCADE,

            block       TEXT NOT NULL,
            state       TEXT NOT NULL,
            -- Required exactly when the arm did not fully succeed. A degraded
            -- arm with no reason is a dead end for whoever has to explain the
            -- response.
            reason      TEXT,

            CONSTRAINT ck_receipt_arm_block CHECK (block IN ({_BLOCK_NAMES})),
            CONSTRAINT ck_receipt_arm_state CHECK (state IN ({_BLOCK_STATES})),
            CONSTRAINT ck_receipt_arm_reason_when_not_ok
                CHECK (state IN ('success', 'empty') OR (reason IS NOT NULL AND length(reason) > 0))
        )
        """
    )
    # One row per arm per receipt. Two rows for one arm would leave the
    # response's own state underivable from its record.
    op.execute("CREATE UNIQUE INDEX uq_receipt_arm ON context_receipt_arms (receipt_id, block)")

    op.execute(
        f"""
        CREATE TABLE context_receipt_items (
            item_row_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            receipt_id      UUID NOT NULL REFERENCES context_receipts(receipt_id) ON DELETE CASCADE,

            -- The contract's own stable id: the digest of block, source and
            -- item key. Written by the service so the database and the code
            -- cannot disagree about which line of a receipt this is.
            receipt_item_id TEXT NOT NULL,

            block           TEXT NOT NULL,
            -- The system the item came from, as a stable identifier rather than
            -- a display name -- display names get renamed and the provenance
            -- goes with them.
            source          TEXT NOT NULL,
            item_key        TEXT NOT NULL,

            CONSTRAINT ck_receipt_item_block CHECK (block IN ({_BLOCK_NAMES})),
            CONSTRAINT ck_receipt_item_identity_present
                CHECK (length(receipt_item_id) > 0 AND length(source) > 0 AND length(item_key) > 0)
        )
        """
    )
    # One line per item per receipt. The same item twice in one resolution is a
    # duplicate, and a reader counting sources would over-weight it.
    op.execute("CREATE UNIQUE INDEX uq_receipt_item ON context_receipt_items (receipt_id, receipt_item_id)")
    # "Which resolutions returned this item" -- the query that makes a receipt
    # checkable across resolutions rather than only within one.
    op.execute("CREATE INDEX ix_receipt_item_identity ON context_receipt_items (receipt_item_id)")
    # "What did this arm of this receipt contribute" -- the per-arm read.
    op.execute("CREATE INDEX ix_receipt_item_block ON context_receipt_items (receipt_id, block)")


def downgrade() -> None:
    # Children first. Both hold foreign keys into the receipt, and dropping the
    # parent out from under them would fail rather than cascade.
    op.execute("DROP TABLE IF EXISTS context_receipt_items")
    op.execute("DROP TABLE IF EXISTS context_receipt_arms")
    op.execute("DROP TABLE IF EXISTS context_receipts")
