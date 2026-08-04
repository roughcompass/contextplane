"""Removes 'private_annotation' from the workspace entry-kind vocabulary.

Revision ID: 0041_drop_private_annotation_entry_kind
Revises: 0040_drop_capability_annotations
Create Date: 2026-08-03

The kind existed to contrast private workspace content with the public
capability annotations that were sent to producers. Those were removed in 0040,
so the name now distinguishes this kind from nothing.

**It never carried behaviour.** It appeared in a frozenset, this CHECK
constraint, and some docstrings — and nowhere else. Nothing validated it
differently from `note`: every entry kind already has `reference_ids`, so "a note
about someone else's capability" was always expressible as a `note` with a
reference. The kind added a vocabulary value whose meaning the schema did not
enforce, which is the kind of value that quietly accumulates divergent
interpretations.

**On the UPDATE below.** It is not defensive theatre. No row in development used
this kind, but a database that has one would fail the constraint being added
here, and the migration would abort halfway. Remapping to `note` loses nothing
precisely because the two were behaviourally identical — the reference, the body,
and the visibility rules are unchanged. A reader who assumes this statement is a
no-op has the reasoning backwards: it is a no-op *in development*, and the only
reason it is safe elsewhere is that the kinds were interchangeable.

Unlike 0040 this migration is genuinely reversible: the six-value constraint is
still satisfiable by any data that satisfies the five-value one, so `downgrade`
restores it. Rows remapped by the upgrade stay `note` — the original kind is not
recoverable, and inventing a way to recover it would mean recording which rows
were remapped, for a distinction that carried no meaning.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_drop_private_annotation_entry_kind"
down_revision = "0040_drop_capability_annotations"
branch_labels = None
depends_on = None

_KINDS_AFTER = "'note','decision','open_question','saved_query','saved_view'"
_KINDS_BEFORE = f"{_KINDS_AFTER},'private_annotation'"

_REMAP = sa.text("UPDATE workspace_entries SET kind = 'note' WHERE kind = 'private_annotation'")


def _swap_constraint(kinds: str) -> None:
    # Dropped and re-added rather than altered: Postgres has no ALTER for a CHECK
    # expression, and naming the constraint keeps this reversible without
    # depending on a generated name.
    op.execute(sa.text("ALTER TABLE workspace_entries DROP CONSTRAINT chk_entry_kind"))
    op.execute(sa.text(f"ALTER TABLE workspace_entries ADD CONSTRAINT chk_entry_kind CHECK (kind IN ({kinds}))"))


def upgrade() -> None:
    op.execute(_REMAP)
    _swap_constraint(_KINDS_AFTER)


def downgrade() -> None:
    # Widening only. Rows the upgrade remapped remain `note`; see the module
    # docstring for why that is not treated as data loss worth reversing.
    _swap_constraint(_KINDS_BEFORE)
