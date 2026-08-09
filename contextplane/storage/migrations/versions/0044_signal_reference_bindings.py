"""A signal may cite external work, through the same junction everything else cites it through.

The bindings table already answers "what does this subject cite" for checkpoints
and context items. A signal carries references too -- they are folded into its
content digest and echoed back to the producer -- but nothing could bind them,
because `subject_type` is a closed set and `external_signal` was not in it. So
"which references did signal X carry" was answerable only by re-reading the
producer's payload in whatever shape that producer happened to use, which is the
thing a normalized reference exists to avoid.

**Widened, not opened.** The CHECK stays a closed set and keeps its name. Closed
because a polymorphic subject column that accepts any string is one where a typo
creates a binding nobody can find; the name because the constraint is what
refuses an unknown subject type, and a refusal identified by a different name is
a refusal no existing reader recognises. Re-creating it under the same name is
therefore the whole change: no new table, no new column, no second junction for
signals alone -- a parallel table would mean two answers to "what cites this
reference" and a reader would have to know which subjects live in which.

**The downgrade deletes before it narrows.** Bindings written under the widened
set are unstorable under the narrow one, so re-adding the old CHECK against a
table holding them fails and leaves the database on neither revision. Deleting
them first is honest about what a downgrade costs: the record of which
references a signal carried is lost, and it has to be, because the schema being
restored has nowhere to keep it. The references themselves survive -- they are
shared rows that other subjects may still cite.
"""

from __future__ import annotations

from alembic import op

revision = "0044_signal_reference_bindings"
down_revision: str | None = "0043_retention_and_derivatives"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: The constraint's own name, spelled once. Both directions re-create it under
#: this name, and a test matches a refusal against it.
_CHECK = "ck_reference_binding_subject_type"

#: What may cite a reference, after this revision.
_SUBJECT_TYPES = "'task_checkpoint', 'context_item', 'external_signal'"

#: What could before it, and what the downgrade restores.
_PRIOR_SUBJECT_TYPES = "'task_checkpoint', 'context_item'"


def _recreate_check(subject_types: str) -> None:
    """Swap the closed set the CHECK admits, keeping its name.

    Dropped and re-added rather than altered because Postgres has no way to
    change a CHECK's expression in place; doing it in one migration step keeps
    the window where no constraint is enforced inside this transaction.
    """
    op.execute(f"ALTER TABLE context_reference_bindings DROP CONSTRAINT {_CHECK}")
    op.execute(
        f"ALTER TABLE context_reference_bindings ADD CONSTRAINT {_CHECK} CHECK (subject_type IN ({subject_types}))"
    )


def upgrade() -> None:
    _recreate_check(_SUBJECT_TYPES)


def downgrade() -> None:
    # Before the narrowing, not after: rows the restored set cannot hold would
    # make the ADD CONSTRAINT fail and abort the whole downgrade.
    op.execute("DELETE FROM context_reference_bindings WHERE subject_type = 'external_signal'")
    _recreate_check(_PRIOR_SUBJECT_TYPES)
