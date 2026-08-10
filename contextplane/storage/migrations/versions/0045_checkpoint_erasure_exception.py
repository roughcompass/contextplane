"""Checkpoints stay append-only, with one write shape admitted: erasure minimization.

The chain was absolutely immutable, enforced by a trigger that raised on every
UPDATE and every DELETE. That is the right default and it stays the default --
resume walks the chain backwards, and a rewritten checkpoint changes what a past
agent is recorded as having decided.

It also made erasure impossible. The approved disposition for a checkpoint is
minimize-and-tombstone: clear the body, keep the position, keep the digest, and
record that the erasure happened. Every one of those is an UPDATE, so a trigger
that refuses all of them refuses the erasure too -- and the alternatives are both
worse than an exception. Deleting the row breaks the chain every successor's
`predecessor_id` points at. Dropping the trigger for the erasure and re-adding it
after leaves a window in which anything may rewrite anything.

**So the exception is a shape, not a caller.** The trigger admits exactly one
UPDATE: every identity, position and provenance column unchanged, and every body
column at its erased value -- the goal replaced by a content-free marker, the five
JSONB arrays empty, `next_action` NULL. Anything else, including a DELETE and
including an UPDATE that blanks the body but rewrites the goal to new text, still
raises. A privilege check would have been the other way to write this, and it is
weaker: the database has one application role, so "who is asking" is a question it
cannot answer, while "what is being written" is one it can.

**The digest is deliberately preserved and deliberately no longer matches.** It is
the record's internally-held commitment to what it held, which is what a
post-erasure verifier checks structure against and what the tombstone's keyed proof
commits to. A digest recomputed over the minimized body would prove that the body
was blanked and nothing else -- the erasure would erase its own evidence.

**The downgrade restores the unconditional refusal and leaves minimized rows
alone.** They are ordinary rows under the restored function; nothing about them is
unstorable. What the downgrade costs is the ability to minimize the next one.
"""

from __future__ import annotations

from alembic import op

revision = "0045_checkpoint_erasure_exception"
down_revision: str | None = "0044_signal_reference_bindings"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: The one goal value a minimized checkpoint may carry, spelled here because the
#: trigger admits this literal and nothing else. The application holds the same
#: constant; an integration test performs the real minimization against a live
#: database, so the two cannot drift apart without a red test rather than a
#: silently refused erasure.
_ERASED_GOAL = "erased:checkpoint"


def upgrade() -> None:
    # `CREATE OR REPLACE` rather than a drop and re-add: the trigger already
    # names this function, so replacing the body swaps the rule without a window
    # in which the table has no trigger attached at all.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION task_checkpoints_are_immutable() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               -- Identity, position and provenance: untouched, all of them. This
               -- is the list a post-erasure verifier reads, so an UPDATE that
               -- moved any of it would be a rewrite wearing an erasure's clothes.
               AND NEW.checkpoint_id    =  OLD.checkpoint_id
               AND NEW.tenant_id        =  OLD.tenant_id
               AND NEW.task_id          =  OLD.task_id
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
                'task_checkpoints is append-only apart from erasure minimization: % on checkpoint_id=% is refused',
                TG_OP, OLD.checkpoint_id
                USING HINT = 'record a new checkpoint whose predecessor_id is this one; '
                             'erasure clears the body to its erased values and changes nothing else';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    # Byte-for-byte the function this revision replaced. Restoring an
    # equivalent-but-reworded one would change the message two integration tests
    # match a refusal against, which is the kind of drift a downgrade is least
    # expected to introduce.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION task_checkpoints_are_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'task_checkpoints is append-only: % on checkpoint_id=% is refused',
                TG_OP, OLD.checkpoint_id
                USING HINT = 'record a new checkpoint whose predecessor_id is this one';
        END;
        $$ LANGUAGE plpgsql
        """
    )
