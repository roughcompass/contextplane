"""Drops capability_annotations and its seeded vocabulary.

Revision ID: 0040_drop_capability_annotations
Revises: 0039_claim_promotion
Create Date: 2026-08-03

The annotations feature is withdrawn. Two independent reasons, either
sufficient on its own.

**Most of it duplicated tools the organisation already runs.** Four of the five
categories — `feedback`, `bug`, `suggestion`, `question` — are chat, issue
tracker, and service-desk territory. A note filed against a catalogue row that
nobody has open is a note nobody answers, and this shipped without an interface,
so nobody could have.

**The part that was genuinely ours already exists in this codebase.** Structured
contradiction of a record — "this row is wrong, here is my evidence" — is what
the claims pipeline does. `service/contest.py` detects disagreement mechanically
from typed values; authority weighting, corroboration, and authority-ordered
resolution landed with the consolidation work. Keeping a second, weaker
mechanism beside it would mean two subsystems in one process answering "who wins
when two sources disagree" differently.

Consumer-to-provider feedback does not disappear with this table: the claim
request lifecycle covers it, and was written to mirror this triage flow rather
than invent a competing one.

**On dropping rather than deprecating.** The table held no production data — the
feature had no interface, and the three MCP tools that could write to it were
reachable only by an agent that went looking. Leaving an unreachable table of
free-text user content would keep a right-to-be-forgotten obligation alive for a
feature with no users, and no erasure participant was ever registered for it, so
that obligation was already unmet.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_drop_capability_annotations"
down_revision = "0039_claim_promotion"
branch_labels = None
depends_on = None


# Both vocabularies seeded by 0018. `is_system = TRUE` scopes this to the rows
# that migration inserted, leaving any tenant-added value alone — those are
# orphaned either way, but deleting another party's rows is not this migration's
# business.
_DELETE_VOCAB = sa.text(
    "DELETE FROM vocabulary_values " "WHERE is_system = TRUE AND kind IN ('annotation_category', 'annotation_status')"
)

# Indexes and CHECK constraints go with the table. Naming them separately would
# only create a second place to keep in sync with 0018.
_DROP_TABLE = sa.text("DROP TABLE IF EXISTS capability_annotations")


def upgrade() -> None:
    op.execute(_DELETE_VOCAB)
    op.execute(_DROP_TABLE)


def downgrade() -> None:
    """Deliberately empty.

    Recreating the table would produce a schema no code reads or writes. An
    operator who genuinely needs the old shape wants the revision before this
    one, where it still had a service attached.
    """
