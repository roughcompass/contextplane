"""A receipt can be withheld while an incident is worked, on its own column.

E4-T4. A quarantine that walks its blast radius one row at a time serves the
last unreached receipt right up until it arrives, which is the window an
incident response exists to close. The task that cut this column prescribed
closing it by marking the downstream set *first* and reconciling afterwards.

**That is not what the column is used for, because there is no window.** `apply`
is one transaction: the claims and the receipts that quoted them are withheld at
the same instant, and no reader observes one without the other. Mark-first is a
remedy for an incremental sweep, and this code does not do one -- so the column
records a withholding that is already atomic with its cause, rather than a
provisional mark awaiting reconciliation.

**Why not a fourth `hydration_state`.** That was the real alternative, and the
entry that cut this task named the shape of the objection: a receipt that is
fully hydrated but provisionally withheld is not the same thing as one that was
never hydrated. Three arguments, weakest first.

The states mean different things. `hydration_state` answers "has this receipt
finished recording what it served" -- a fact about the receipt's own
construction. Withholding answers "may this be shown right now" -- a fact about
an incident happening elsewhere. Collapsing them loses the distinction an
operator needs while deciding whether a gap in evidence is a bug or a decision.

`HYDRATION_SERVABLE` exists to make a fourth state expensive. Its docstring says
so: the set is named "rather than written as `== 'complete'` at each read, so a
fourth state cannot be added without every reader being revisited." Adding one
here would be walking through a door somebody deliberately made narrow.

**And decisively: reversibility.** Withholding is reversible by design -- revert
puts back exactly what a quarantine took. Overwriting `hydration_state` destroys
the value to restore *to*, so releasing would have to guess between `complete`
and `failed`. That is the same argument
`claim_quarantine_members` rests on in migration 0071 -- revert restores what
was recorded, never a re-derivation -- and it applies here for the same reason.

This is also the position migration 0071 already took for the identical question
one table over: quarantine is a materialised state on its own column, not a
reuse of a field that already means something else.
"""

from __future__ import annotations

from alembic import op

revision = "0074_receipt_prequarantine"
down_revision: str | None = "0073_agent_accuracy_and_instructions"
branch_labels: str | None = None
depends_on: str | None = None

#: Nullable, for the reason `memory_claims.quarantined_at` is: almost no receipt
#: is withheld, and a NOT NULL sentinel would mean inventing a "not withheld"
#: instant. NULL is the honest absence.
_ADD_COLUMN = "ALTER TABLE context_receipts ADD COLUMN withheld_at TIMESTAMPTZ"

#: Which quarantine withheld it, so reconciliation is exact rather than a
#: re-derivation. Without this, releasing one incident's receipts would have to
#: re-walk the predicate -- and a receipt reached by two open incidents would be
#: released by whichever finished first.
_ADD_CAUSE = (
    "ALTER TABLE context_receipts ADD COLUMN withheld_by UUID "
    "REFERENCES claim_quarantines(quarantine_id) ON DELETE SET NULL"
)

#: Partial: the withheld set is a small fraction of a large table and every read
#: filters `IS NULL`. Indexing the NULLs would index almost the whole table to
#: answer a question the planner can already answer from the row.
_INDEX = (
    "CREATE INDEX ix_context_receipts_withheld ON context_receipts (tenant_id, withheld_by) "
    "WHERE withheld_at IS NOT NULL"
)

#: Both columns move together or neither does. A receipt withheld by nothing
#: cannot be released by anything, and one attributed to a quarantine while
#: servable would make the ledger claim a withholding that is not in effect.
_CHECK = (
    "ALTER TABLE context_receipts ADD CONSTRAINT ck_receipt_withholding_is_attributed "
    "CHECK ((withheld_at IS NULL) = (withheld_by IS NULL))"
)


def upgrade() -> None:
    op.execute(_ADD_COLUMN)
    op.execute(_ADD_CAUSE)
    op.execute(_INDEX)
    op.execute(_CHECK)


def downgrade() -> None:
    op.execute("ALTER TABLE context_receipts DROP CONSTRAINT ck_receipt_withholding_is_attributed")
    op.execute("DROP INDEX ix_context_receipts_withheld")
    op.execute("ALTER TABLE context_receipts DROP COLUMN withheld_by")
    op.execute("ALTER TABLE context_receipts DROP COLUMN withheld_at")
