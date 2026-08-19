"""Make the legal-hold ceiling an absolute duration, matching what places holds.

The ceiling was written as `placed_at + INTERVAL '180 days'`. For `timestamptz`,
PostgreSQL treats a day interval as a **calendar** duration: it preserves local
wall-clock time across a daylight-saving transition, so the same expression spans
4319 or 4321 hours when one falls inside the window, and 4320 when none does.

Nothing that places a hold agrees with that. `retention/holds.py` computes
`review_date = placed_at + timedelta(days=review_in_days)`, an absolute duration,
and gates `review_in_days` against `MAX_HOLD_DAYS = 180`. So the product means
4320 hours exactly, and the database was measuring something else whenever the
server's `TimeZone` observed DST.

Both halves of the disagreement are wrong in a way that matters:

- **A valid hold is refused.** `place_hold()` with its own default of 180 days
  produces a `review_date` 4320 hours out. Across a spring-forward window the
  constraint's bound is 4319 hours, so the product's default call path is rejected
  by the product's own constraint.
- **An over-ceiling hold is accepted.** Across a fall-back window the bound is
  4321 hours, so a review date an hour past what the product calls the ceiling
  satisfies the check. That is the compliance-relevant direction: data held longer
  than the approved maximum.

Only the server's zone decided which, so the same schema behaved differently on a
UTC server and a regional one. Both PostgreSQL 16.13 and 18.4 were measured and
agree; this was never a version difference, and an earlier report that filed it as
one has been corrected.

Stated as `MAX_HOLD_DAYS * 24` hours rather than a bare `4320` so the constant and
the constraint cannot drift apart silently, and so the arithmetic is legible to
whoever changes the ceiling next.

The bound moves by at most one hour, and only for holds whose window crosses a
transition. No existing row can be invalidated by tightening it: any row that
satisfied a 4321-hour bound and not a 4320-hour one would have to have been placed
by something computing calendar days, and nothing does.
"""

from __future__ import annotations

from alembic import op

revision = "0055_legal_hold_ceiling_absolute"
down_revision: str | None = "0054_drop_precreated_audit_shadow"
branch_labels: str | None = None
depends_on: str | None = None

#: Parented on `0054_drop_precreated_audit_shadow`, which is the actual head.
#: Filenames do not sort into revision order in this tree -- 0049 was added after
#: 0053 -- so the parent is taken from walking `down_revision`, not from the
#: highest number. This revision was authored against 0049 when that was the head;
#: 0054 landed on the same parent first, so re-parenting here is what keeps the
#: chain single-headed rather than forking it into "Multiple head revisions".

#: Duplicated from `retention/holds.py` deliberately. A migration cannot import
#: application code -- it has to keep running against a tree whose Python has moved
#: on -- so the number is restated here and the integration tier is what holds the
#: two together.
_MAX_HOLD_DAYS = 180

_CONSTRAINT = "ck_legal_holds_review_within_ceiling"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "legal_holds", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "legal_holds",
        f"review_date > placed_at AND review_date <= placed_at + INTERVAL '{_MAX_HOLD_DAYS * 24} hours'",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "legal_holds", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "legal_holds",
        f"review_date > placed_at AND review_date <= placed_at + INTERVAL '{_MAX_HOLD_DAYS} days'",
    )
