"""Intent memory, renamed in place: three tables, their key column, and the values rows carry.

The code above this migration already calls a checkpoint's owner an *intent*.
This revision moves the database to the same vocabulary in one transaction, so
there is no interval in which a query written against the new names reads a
database that still holds the old ones.

**Renamed in place, never recreated.** `ALTER TABLE ... RENAME` keeps the rows,
the indexes, the constraints and the grants exactly as they were. Creating new
tables and copying into them would give every checkpoint a new physical identity
for a change that is supposed to be a spelling correction, and the chain digests
that make a checkpoint sequence verifiable are computed over content that a copy
would have to reproduce byte-perfectly to stay valid.

**The indexes and constraints keep their old names, deliberately.** The ORM still
declares `uq_task_participant_grant`, `uq_task_checkpoint_sequence` and
`ix_task_checkpoint_task`. The database and the mapping have to agree, and the
mapping is the frozen postimage this cutover is measured against — so renaming
them here would break the agreement in the direction nothing checks until a
migration test compares the two column for column.

**Stored values, not only schema.** Renaming the tables and stopping there is the
failure mode this revision exists to avoid. Rows carry `task_checkpoint` as a
retention record class, an audit target type, a receipt item's source and its
trust source; audit rows carry `task.checkpoint.appended` and
`task.head.summary_set` as actions and a `task_id` key inside their JSONB
payload. Every one of those is read by an equality filter that the renamed code
now spells `intent_checkpoint`. A migration that moved the schema and left the
values would leave rows no query matches, and it would run green — nothing
raises when a `WHERE` clause simply fails to find anything.

**One receipt-item id is derived, so it is recomputed rather than rewritten.**
`context_receipt_items.receipt_item_id` is a SHA-256 over the item's block,
source and key, length-prefixed per part. Renaming the source without
recomputing the digest would leave every checkpoint item holding an id that no
longer equals the digest of its own columns — an invariant break that reads as
data corruption to anything that re-derives the id to compare two receipts. The
three inputs are all stored columns, so the digest is reproducible here in SQL:
`int4send` emits the same four-byte big-endian length the application's
`len(part.encode()).to_bytes(4, "big")` does.

**What this revision cannot repair, and does not pretend to.**
`context_receipts.request_digest` is a digest over a request record that is
never itself stored, and the key naming the intent ids inside that record
changed with the rename. Pre-cutover receipts therefore keep a digest that the
renamed code cannot reproduce from the same request. There is no input to
recompute from, so the column is left exactly as written: a digest that records
what was actually hashed at the time is still true evidence, while one rewritten
to a guess would not be. The column is read-only reporting — nothing queries or
joins on it — so the consequence is confined to comparing a pre-cutover receipt
with a post-cutover one by digest, which now reads as different requests.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_intent_memory_nomenclature"
down_revision: str | None = "0046_legal_holds"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: The one goal value a minimized checkpoint may carry. Repeated from the
#: revision that introduced the erasure exception because the trigger body has to
#: be restated in full to change the column it names, and the literal it admits
#: must not drift while it is being restated.
_ERASED_GOAL = "erased:checkpoint"

#: The three tables, old name to new. The column rename is the same word in all
#: three, so it is applied per table rather than spelled three times.
_TABLES: tuple[tuple[str, str], ...] = (
    ("task_participant_grants", "intent_participant_grants"),
    ("task_checkpoints", "intent_checkpoints"),
    ("task_heads", "intent_heads"),
)

#: The CHECK that closes the set of things a reference may be bound to, spelled
#: under the name both directions re-create it with because a test matches a
#: refusal against that name.
_BINDING_CHECK = "ck_reference_binding_subject_type"

#: The closed subject set, after and before this revision. Postgres cannot alter
#: a CHECK expression in place, so the constraint is dropped and re-added -- and
#: the rows move while it is off, because the new set refuses the old spelling
#: and the old set refuses the new one.
_SUBJECT_TYPES = "'intent_checkpoint', 'context_item', 'external_signal'"
_PRIOR_SUBJECT_TYPES = "'task_checkpoint', 'context_item', 'external_signal'"


def _rebind_subjects(*, subject_types: str, old_value: str, new_value: str) -> None:
    """Move the binding subject to its new spelling, constraint off for the move.

    The order is forced: the CHECK admits one spelling or the other and never
    both, so an UPDATE with either constraint in place refuses every row it
    touches. Dropping and re-adding inside this transaction keeps the window
    where nothing is enforced from being observable outside it.
    """
    op.execute(f"ALTER TABLE context_reference_bindings DROP CONSTRAINT {_BINDING_CHECK}")
    op.execute(
        sa.text("UPDATE context_reference_bindings SET subject_type = :new WHERE subject_type = :old").bindparams(
            new=new_value, old=old_value
        )
    )
    op.execute(
        f"ALTER TABLE context_reference_bindings ADD CONSTRAINT {_BINDING_CHECK} "
        f"CHECK (subject_type IN ({subject_types}))"
    )


def _rename_immutability_trigger(*, table: str, function: str, trigger: str, id_column: str) -> None:
    """Restate the append-only trigger against the renamed table and column.

    `CREATE OR REPLACE` rather than a drop and re-add, so the table is never
    left without the rule attached. The body has to be restated in full rather
    than renamed alone: plpgsql resolves `NEW.<column>` at execution time, so a
    body still naming the old column would keep validating until the first write
    after the migration and then fail there instead of here.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               -- Identity, position and provenance: untouched, all of them. This
               -- is the list a post-erasure verifier reads, so an UPDATE that
               -- moved any of it would be a rewrite wearing an erasure's clothes.
               AND NEW.checkpoint_id    =  OLD.checkpoint_id
               AND NEW.tenant_id        =  OLD.tenant_id
               AND NEW.{id_column}      =  OLD.{id_column}
               AND NEW.sequence         =  OLD.sequence
               AND NEW.predecessor_id   IS NOT DISTINCT FROM OLD.predecessor_id
               AND NEW.author           =  OLD.author
               AND NEW.recorded_at      =  OLD.recorded_at
               AND NEW.retention_policy =  OLD.retention_policy
               AND NEW.digest           =  OLD.digest
               -- The body, at its erased value and no other. Pinning the goal to
               -- one literal is what stops "blank the arrays and write whatever
               -- you like into the goal" from being an admitted shape.
               AND NEW.goal             =  '{_ERASED_GOAL}'
               AND NEW.decisions        =  '[]'::jsonb
               AND NEW.assumptions      =  '[]'::jsonb
               AND NEW.evidence         =  '[]'::jsonb
               AND NEW.completed_checks =  '[]'::jsonb
               AND NEW.open_questions   =  '[]'::jsonb
               AND NEW.next_action      IS NULL
            THEN
                RETURN NEW;
            END IF;

            RAISE EXCEPTION
                '{table} is append-only apart from erasure minimization: % on checkpoint_id=% is refused',
                TG_OP, OLD.checkpoint_id
                USING HINT = 'record a new checkpoint whose predecessor_id is this one; '
                             'erasure clears the body to its erased values and changes nothing else';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(f"ALTER TRIGGER {trigger} ON {table} RENAME TO trg_{table}_immutable")


def upgrade() -> None:
    for old, new in _TABLES:
        op.execute(f"ALTER TABLE {old} RENAME TO {new}")
        op.execute(f"ALTER TABLE {new} RENAME COLUMN task_id TO intent_id")

    # The fourth live `task_id`, and the one outside the renamed tables: a
    # receipt names the intent it describes. The partial index over it keeps its
    # own name for the same reason the others do -- the mapping declares it.
    op.execute("ALTER TABLE context_receipts RENAME COLUMN task_id TO intent_id")

    _rebind_subjects(
        subject_types=_SUBJECT_TYPES,
        old_value="task_checkpoint",
        new_value="intent_checkpoint",
    )

    op.execute("ALTER FUNCTION task_checkpoints_are_immutable() RENAME TO intent_checkpoints_are_immutable")
    _rename_immutability_trigger(
        table="intent_checkpoints",
        function="intent_checkpoints_are_immutable",
        trigger="trg_task_checkpoints_immutable",
        id_column="intent_id",
    )

    # The retention policy row and every tombstone that points at it move
    # together. The foreign key is on `(policy_version, record_class)` and is not
    # deferrable, so it comes off for the length of the pair of updates rather
    # than the parent row being deleted and reinserted -- which would destroy the
    # approval record a tombstone cites.
    op.execute("ALTER TABLE source_tombstones DROP CONSTRAINT fk_tombstone_policy")
    op.execute(
        "UPDATE retention_policies SET record_class = 'intent_checkpoint' WHERE record_class = 'task_checkpoint'"
    )
    op.execute("UPDATE source_tombstones SET record_class = 'intent_checkpoint' WHERE record_class = 'task_checkpoint'")
    op.execute(
        """
        ALTER TABLE source_tombstones
            ADD CONSTRAINT fk_tombstone_policy
            FOREIGN KEY (policy_version, record_class)
            REFERENCES retention_policies (policy_version, record_class)
        """
    )

    op.execute(
        "UPDATE derivative_source_links SET source_record_class = 'intent_checkpoint' "
        "WHERE source_record_class = 'task_checkpoint'"
    )
    op.execute("UPDATE legal_holds SET record_class = 'intent_checkpoint' WHERE record_class = 'task_checkpoint'")

    op.execute("UPDATE audit_log SET target_type = 'intent_checkpoint' WHERE target_type = 'task_checkpoint'")
    op.execute("UPDATE audit_log SET action = 'intent.checkpoint.appended' WHERE action = 'task.checkpoint.appended'")
    op.execute("UPDATE audit_log SET action = 'intent.head.summary_set' WHERE action = 'task.head.summary_set'")
    # The payload key, not only the column. An audit row whose `after` still says
    # `task_id` describes the append in a vocabulary the reader no longer uses,
    # and the key is what a reader indexes into rather than something it scans.
    op.execute(
        """
        UPDATE audit_log
           SET after_jsonb = (after_jsonb - 'task_id') || jsonb_build_object('intent_id', after_jsonb -> 'task_id')
         WHERE after_jsonb ? 'task_id'
           AND target_type = 'intent_checkpoint'
        """
    )

    # Source and derived id in one statement: the digest is computed from the
    # value the row is about to hold, so splitting this in two would leave a
    # window where the id is the digest of neither spelling.
    op.execute(
        """
        UPDATE context_receipt_items
           SET receipt_item_id = encode(sha256(
                   int4send(octet_length(convert_to(block, 'UTF8'))) || convert_to(block, 'UTF8') ||
                   int4send(octet_length(convert_to('intent_checkpoint', 'UTF8'))) ||
                       convert_to('intent_checkpoint', 'UTF8') ||
                   int4send(octet_length(convert_to(item_key, 'UTF8'))) || convert_to(item_key, 'UTF8')
               ), 'hex'),
               source = 'intent_checkpoint',
               trust_source = CASE WHEN trust_source = 'task_checkpoint' THEN 'intent_checkpoint' ELSE trust_source END
         WHERE source = 'task_checkpoint'
        """
    )
    # A checkpoint's trust source on an item sourced from something else: the id
    # does not depend on it, so the value moves on its own.
    op.execute(
        "UPDATE context_receipt_items SET trust_source = 'intent_checkpoint' "
        "WHERE trust_source = 'task_checkpoint' AND source <> 'intent_checkpoint'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE context_receipt_items SET trust_source = 'task_checkpoint' "
        "WHERE trust_source = 'intent_checkpoint' AND source <> 'intent_checkpoint'"
    )
    op.execute(
        """
        UPDATE context_receipt_items
           SET receipt_item_id = encode(sha256(
                   int4send(octet_length(convert_to(block, 'UTF8'))) || convert_to(block, 'UTF8') ||
                   int4send(octet_length(convert_to('task_checkpoint', 'UTF8'))) ||
                       convert_to('task_checkpoint', 'UTF8') ||
                   int4send(octet_length(convert_to(item_key, 'UTF8'))) || convert_to(item_key, 'UTF8')
               ), 'hex'),
               source = 'task_checkpoint',
               trust_source = CASE WHEN trust_source = 'intent_checkpoint' THEN 'task_checkpoint' ELSE trust_source END
         WHERE source = 'intent_checkpoint'
        """
    )

    op.execute(
        """
        UPDATE audit_log
           SET after_jsonb = (after_jsonb - 'intent_id') || jsonb_build_object('task_id', after_jsonb -> 'intent_id')
         WHERE after_jsonb ? 'intent_id'
           AND target_type = 'intent_checkpoint'
        """
    )
    op.execute("UPDATE audit_log SET action = 'task.checkpoint.appended' WHERE action = 'intent.checkpoint.appended'")
    op.execute("UPDATE audit_log SET action = 'task.head.summary_set' WHERE action = 'intent.head.summary_set'")
    op.execute("UPDATE audit_log SET target_type = 'task_checkpoint' WHERE target_type = 'intent_checkpoint'")

    op.execute(
        "UPDATE derivative_source_links SET source_record_class = 'task_checkpoint' "
        "WHERE source_record_class = 'intent_checkpoint'"
    )
    op.execute("UPDATE legal_holds SET record_class = 'task_checkpoint' WHERE record_class = 'intent_checkpoint'")

    op.execute("ALTER TABLE source_tombstones DROP CONSTRAINT fk_tombstone_policy")
    op.execute(
        "UPDATE retention_policies SET record_class = 'task_checkpoint' WHERE record_class = 'intent_checkpoint'"
    )
    op.execute("UPDATE source_tombstones SET record_class = 'task_checkpoint' WHERE record_class = 'intent_checkpoint'")
    op.execute(
        """
        ALTER TABLE source_tombstones
            ADD CONSTRAINT fk_tombstone_policy
            FOREIGN KEY (policy_version, record_class)
            REFERENCES retention_policies (policy_version, record_class)
        """
    )

    _rebind_subjects(
        subject_types=_PRIOR_SUBJECT_TYPES,
        old_value="intent_checkpoint",
        new_value="task_checkpoint",
    )

    op.execute("ALTER TABLE context_receipts RENAME COLUMN intent_id TO task_id")

    # The tables come back first. `ALTER TRIGGER ... ON <table>` has to name a
    # table that exists under that name, and the restated body names the column,
    # so both renames precede the trigger rather than following it.
    for old, new in reversed(_TABLES):
        op.execute(f"ALTER TABLE {new} RENAME COLUMN intent_id TO task_id")
        op.execute(f"ALTER TABLE {new} RENAME TO {old}")

    op.execute("ALTER FUNCTION intent_checkpoints_are_immutable() RENAME TO task_checkpoints_are_immutable")
    _rename_immutability_trigger(
        table="task_checkpoints",
        function="task_checkpoints_are_immutable",
        trigger="trg_intent_checkpoints_immutable",
        id_column="task_id",
    )
