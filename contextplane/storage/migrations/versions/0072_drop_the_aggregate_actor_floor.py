"""The aggregate actor floor is also a CHECK constraint, and it has to go too.

E20-T2. The decision to remove the per-actor floor was recorded and executed in
application code -- `Floors`, `MIN_COHORT_ACTORS`, `MIN_CELL_EVENTS`, the
suppression rule and the partial-total rule all left
`service/memory/learning_reads.py`. **The database was still enforcing it.**

Migration 0043 created `privacy_aggregates` with:

    CONSTRAINT ck_aggregate_meets_the_floor
        CHECK (suppressed OR actor_count >= 5)

which is the same rule, written where no application change can reach it. With
the writer no longer suppressing anything and no longer zeroing `actor_count`, a
window covering four contributors now offers `suppressed = false` and
`actor_count = 4`, and the insert is refused.

That is a half-removal of exactly the kind this plan's supersession rule
forbids, and it would have surfaced as an aggregate worker failing on real data
rather than as a test -- the unit tests drive a fake session, so the constraint
is invisible to them.

**What is deliberately kept.** `ck_aggregate_suppressed_carries_no_value` stays.
It says a suppressed cell carries no value and an unsuppressed one does, and
that is still true and still load-bearing: `suppressed` did not disappear with
the floor, it changed cause. It is now set exclusively by the differencing rule
in the upsert -- a cell whose recomputed value disagrees with one already
published is withheld from then on, because the difference between two figures
for the same cell across an erasure discloses the erased subject's contribution
exactly. A withheld cell must still carry no value, or that defence leaks
through the column it withheld.

So this drops the constraint that encoded the *cardinality* floor and leaves the
one that encodes the *differencing* invariant. They were adjacent in one CREATE
TABLE and they answer different questions.
"""

from __future__ import annotations

from alembic import op

revision = "0072_drop_the_aggregate_actor_floor"
down_revision: str | None = "0071_claim_quarantine"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "ck_aggregate_meets_the_floor"

#: The literal the dropped constraint was generated from. Repeated here rather
#: than imported so the downgrade rebuilds exactly what 0043 built, even after
#: the name it came from has been deleted from the application.
_FLOOR = 5


def upgrade() -> None:
    op.execute(f"ALTER TABLE privacy_aggregates DROP CONSTRAINT {_CONSTRAINT}")


def downgrade() -> None:
    # Can fail, and failing is correct. Once cells have been stored for windows
    # covering fewer than five contributors, restoring the constraint would
    # require deleting them -- which is a disclosure decision, not a schema one,
    # and not this migration's to take silently.
    op.execute(
        f"ALTER TABLE privacy_aggregates ADD CONSTRAINT {_CONSTRAINT} " f"CHECK (suppressed OR actor_count >= {_FLOOR})"
    )
