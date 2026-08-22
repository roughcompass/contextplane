"""A receipt says whether it is finished being written.

E3 splits the receipt write into a synchronous intent row plus asynchronous
hydration of arms, items and exclusions. Before any of that, the receipt needs a
way to say which it is -- and this migration is deliberately the whole of that,
landing while it is still uninteresting.

**Today every receipt is complete by construction, which is exactly why this can
be reviewed on its own.** `receipts.record` writes the receipt, its arms, its
items and its exclusions in one transaction, and `resolve.py` fails the
resolution if that write fails: "an answer nobody can later show they were given
is the thing receipts exist to prevent". So every row this migration backfills is
`complete`, and the column changes no behaviour.

The task that makes it interesting is the one that relaxes that guarantee, and it
is a separate change on purpose. A diff that both removes a guarantee and adds
the discriminator making the removal safe is a diff where a reviewer cannot see
the removal.

**The counts matter as much as the state, and are the part easy to leave out.** A
`complete` receipt with zero exclusions and a `pending` one with zero exclusions
are the same row without them. With them a reader can tell "nothing was withheld"
from "what was withheld has not been written down yet" -- and the receipts module
already holds that a receipt reading as complete while withholding something is
worse than no receipt.

Counts rather than a flag, because they are also the check: once hydration lands,
a `complete` receipt whose stored exclusion rows do not number
`exclusion_count` is a receipt that finished wrongly, and nothing else would
notice.
"""

from __future__ import annotations

from alembic import op

revision = "0069_receipt_hydration_state"
down_revision: str | None = "0068_source_namespace_registrations"
branch_labels: str | None = None
depends_on: str | None = None

#: Nullable first, backfilled, then made NOT NULL with a default. The three-step
#: is what a populated table needs, and writing it once here means the next
#: person copies the safe shape rather than the convenient one.
#:
#: **The default is `pending`, which is the answer that refuses.** A writer that
#: inserts a receipt without saying whether it is hydrated has not made a claim,
#: and the honest reading of no claim is "not yet evidence" -- the reads below
#: refuse it. Defaulting to `complete` would mean a forgotten column asserts a
#: receipt is finished, which is the same shape as a validation status defaulting
#: to validated: the permissive direction, taken by omission.
#:
#: The one production writer states the value explicitly and the ORM column has
#: no Python-side default, so omitting it there is a type error rather than a
#: quiet `pending`.
_ADD = """
ALTER TABLE context_receipts
    ADD COLUMN hydration_state TEXT,
    ADD COLUMN item_count      INTEGER,
    ADD COLUMN exclusion_count INTEGER
"""

#: Every existing receipt was written whole in one transaction, so `complete` is
#: not an assumption -- it is what the write path guaranteed. The counts come
#: from the rows themselves rather than from a constant, so a receipt that was
#: somehow short is backfilled as what it actually is.
_BACKFILL = """
UPDATE context_receipts r
   SET hydration_state = 'complete',
       item_count = (
           SELECT count(*) FROM context_receipt_items i WHERE i.receipt_id = r.receipt_id
       ),
       exclusion_count = (
           SELECT count(*) FROM context_receipt_exclusions e WHERE e.receipt_id = r.receipt_id
       )
"""

_ENFORCE = """
ALTER TABLE context_receipts
    ALTER COLUMN hydration_state SET NOT NULL,
    ALTER COLUMN hydration_state SET DEFAULT 'pending',
    ALTER COLUMN item_count      SET NOT NULL,
    ALTER COLUMN item_count      SET DEFAULT 0,
    ALTER COLUMN exclusion_count SET NOT NULL,
    ALTER COLUMN exclusion_count SET DEFAULT 0
"""

#: `failed` is a state a receipt can rest in, not a transient. Hydration that
#: gave up leaves evidence that it gave up; a row that stayed `pending` forever
#: would be indistinguishable from one still in flight, and the read surfaces
#: have to refuse the two differently -- one is "wait", the other is "this
#: receipt will never be evidence".
_STATE_CHECK = """
ALTER TABLE context_receipts
    ADD CONSTRAINT ck_context_receipts_hydration_state
    CHECK (hydration_state IN ('pending', 'complete', 'failed'))
"""

#: Counts are counts. Negative is not a state anything should be able to write,
#: and a CHECK is cheaper than the test that would otherwise have to notice.
_COUNT_CHECK = """
ALTER TABLE context_receipts
    ADD CONSTRAINT ck_context_receipts_counts
    CHECK (item_count >= 0 AND exclusion_count >= 0)
"""

#: The read surfaces filter on this, and a receipt that is not `complete` is the
#: minority forever, so the index is partial: it answers "what is still pending
#: or failed" without carrying a row for every finished receipt.
_INDEX = """
CREATE INDEX ix_context_receipts_unhydrated
    ON context_receipts (tenant_id, resolved_at)
    WHERE hydration_state <> 'complete'
"""


def upgrade() -> None:
    op.execute(_ADD)
    op.execute(_BACKFILL)
    op.execute(_ENFORCE)
    op.execute(_STATE_CHECK)
    op.execute(_COUNT_CHECK)
    op.execute(_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX ix_context_receipts_unhydrated")
    op.execute("ALTER TABLE context_receipts DROP CONSTRAINT ck_context_receipts_counts")
    op.execute("ALTER TABLE context_receipts DROP CONSTRAINT ck_context_receipts_hydration_state")
    op.execute(
        "ALTER TABLE context_receipts "
        "DROP COLUMN exclusion_count, DROP COLUMN item_count, DROP COLUMN hydration_state"
    )
