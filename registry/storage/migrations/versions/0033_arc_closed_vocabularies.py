"""Enumerate two ARC columns that were declared closed and shipped open.

`arc_revisions.content_classification` and `arc_receipt_events.event_type` were
specified as `CHECK`-constrained closed sets and landed with a length bound only.
Length-bounded is not closed: any string up to 64 characters was storable, so
neither column could be relied on for filtering, alerting, or a retention rule,
and a typo produced a value that compared equal to nothing.

The member lists come from `registry.arc.vocabularies`, and a conformance test
asserts the constants there and the constraints here describe the same set.

**Additive only.** The existing `NOT NULL` and `char_length` checks are correct as
far as they go and stay. No backfill is implied: `event_type` has only ever been
written from three constants, and `content_classification` rows that predate this
are checked by the constraint's own validation when it is added -- a row outside
the set fails the migration, which is the correct outcome for a value nothing can
interpret.

**`regulated` implies encrypted storage, and that is enforced here rather than in
the service layer.** A revision whose content is legally controlled must not sit in
a plaintext row. Expressing it as a cross-column CHECK means no write path can
forget it -- including one written later by someone who never reads this file.
"""

from __future__ import annotations

from alembic import op

from registry.arc.vocabularies import CONTENT_CLASSIFICATIONS, RECEIPT_EVENT_TYPES

revision = "0033_arc_closed_vocabularies"
down_revision = "0032_predicate_cardinality"
branch_labels = None
depends_on = None


def _sql_set(values: frozenset[str]) -> str:
    # Sorted so the emitted DDL is stable across runs; a set's iteration order is
    # not, and an unstable constraint definition makes every schema diff noisy.
    return ", ".join(f"'{value}'" for value in sorted(values))


def upgrade() -> None:
    op.execute(
        "ALTER TABLE arc_revisions "
        "ADD CONSTRAINT ck_arc_revisions_content_classification "
        f"CHECK (content_classification IN ({_sql_set(CONTENT_CLASSIFICATIONS)}))"
    )

    op.execute(
        "ALTER TABLE arc_revisions "
        "ADD CONSTRAINT ck_arc_revisions_regulated_encrypted "
        "CHECK (content_classification <> 'regulated' "
        "       OR content_storage_mode = 'encrypted')"
    )

    op.execute(
        "ALTER TABLE arc_receipt_events "
        "ADD CONSTRAINT ck_arc_receipt_events_event_type "
        f"CHECK (event_type IN ({_sql_set(RECEIPT_EVENT_TYPES)}))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE arc_receipt_events DROP CONSTRAINT ck_arc_receipt_events_event_type"
    )
    op.execute(
        "ALTER TABLE arc_revisions DROP CONSTRAINT ck_arc_revisions_regulated_encrypted"
    )
    op.execute(
        "ALTER TABLE arc_revisions DROP CONSTRAINT ck_arc_revisions_content_classification"
    )
