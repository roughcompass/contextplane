"""Task memory: explicit participant grants, an immutable checkpoint chain, and a mutable head.

Three tables with three different mutability rules, which is the whole point of
separating them:

**Grants are explicit and never self-issued.** `granted_by` must differ from
`actor_id`, enforced by a CHECK rather than left to the service: an actor who
names themselves a participant has asserted it, not been granted it, and once
stored the row is indistinguishable from a real grant. Temporal evidence
(`granted_at`, `expires_at`) is on the row because a grant that was valid last
month is not evidence today, and `resolver_version` is recorded because a grant
made under one resolution rule is not evidence under a later one.

**Checkpoints are immutable.** Enforced by a trigger, not by convention: resume
walks this chain backwards and a rewritten checkpoint would change what a past
agent is recorded as having decided. `(task_id, sequence)` is unique so two
writers cannot both claim step 4, and `predecessor_id` is required from sequence
2 onward so a hole in the chain is a constraint violation rather than a silently
short history.

**The head is a projection and is meant to be overwritten.** It carries no
history of its own; the checkpoint chain is the history, and duplicating it here
would create a second answer to what happened.

Existing workspace rows and their APIs are untouched. Nothing here upgrades an
existing actor/tenant workspace into a task: participation is granted, and
inventing grants for rows that predate the concept would manufacture evidence.
"""

from __future__ import annotations

from alembic import op

revision = "0030_task_memory"
down_revision: str | None = "0012_arc_submission_identity"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_PARTICIPANT_ROLES = "'reader', 'contributor', 'owner', 'auditor'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE task_participant_grants (
            grant_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),
            task_id         UUID NOT NULL,
            actor_id        TEXT NOT NULL,
            role            TEXT NOT NULL,
            granted_by      TEXT NOT NULL,
            granted_at      TIMESTAMPTZ NOT NULL,
            expires_at      TIMESTAMPTZ,
            resolver_version TEXT NOT NULL,

            CONSTRAINT ck_grant_role CHECK (role IN ({_PARTICIPANT_ROLES})),
            -- A self-grant is an assertion wearing a grant's shape. Refused in
            -- the schema so no service can be the one place that forgets.
            CONSTRAINT ck_grant_not_self CHECK (actor_id <> granted_by),
            -- An expiry before the grant existed describes no window at all.
            CONSTRAINT ck_grant_window CHECK (expires_at IS NULL OR expires_at > granted_at)
        )
        """
    )
    # One live grant per actor per task: two rows would make "what is this actor
    # allowed to do" ambiguous exactly when it is being checked.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_task_participant_grant
            ON task_participant_grants (tenant_id, task_id, actor_id)
        """
    )

    op.execute(
        """
        CREATE TABLE task_checkpoints (
            checkpoint_id    UUID PRIMARY KEY,
            tenant_id        UUID NOT NULL REFERENCES tenants(tenant_id),
            task_id          UUID NOT NULL,
            sequence         INTEGER NOT NULL,
            predecessor_id   UUID REFERENCES task_checkpoints(checkpoint_id),
            goal             TEXT NOT NULL,
            decisions        JSONB NOT NULL DEFAULT '[]'::jsonb,
            assumptions      JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence         JSONB NOT NULL DEFAULT '[]'::jsonb,
            completed_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
            open_questions   JSONB NOT NULL DEFAULT '[]'::jsonb,
            next_action      TEXT,
            author           TEXT NOT NULL,
            recorded_at      TIMESTAMPTZ NOT NULL,
            retention_policy TEXT NOT NULL,
            digest           TEXT NOT NULL,

            CONSTRAINT ck_checkpoint_sequence_positive CHECK (sequence >= 1),
            -- Only the first step may have nothing before it. Anywhere else, a
            -- missing predecessor is a hole in the chain resume walks.
            CONSTRAINT ck_checkpoint_predecessor CHECK (
                (sequence = 1 AND predecessor_id IS NULL)
                OR (sequence > 1 AND predecessor_id IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_task_checkpoint_sequence
            ON task_checkpoints (tenant_id, task_id, sequence)
        """
    )
    op.execute("CREATE INDEX ix_task_checkpoint_task ON task_checkpoints (tenant_id, task_id, sequence DESC)")

    # Immutability as a trigger rather than a convention. Resume reads this chain
    # to reconstruct what a past agent decided; an UPDATE would rewrite history
    # that later checkpoints were built on, and a DELETE would break the chain
    # every successor's predecessor_id points at.
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
    op.execute(
        """
        CREATE TRIGGER trg_task_checkpoints_immutable
            BEFORE UPDATE OR DELETE ON task_checkpoints
            FOR EACH ROW EXECUTE FUNCTION task_checkpoints_are_immutable()
        """
    )

    op.execute(
        """
        CREATE TABLE task_heads (
            tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
            task_id             UUID NOT NULL,
            head_checkpoint_id  UUID NOT NULL REFERENCES task_checkpoints(checkpoint_id),
            head_sequence       INTEGER NOT NULL,
            summary             TEXT NOT NULL,
            updated_at          TIMESTAMPTZ NOT NULL,

            PRIMARY KEY (tenant_id, task_id),
            CONSTRAINT ck_head_sequence_positive CHECK (head_sequence >= 1)
        )
        """
    )


def downgrade() -> None:
    # Dropped in dependency order: the head references checkpoints, and the
    # trigger's function outlives the table it was attached to unless named.
    op.execute("DROP TABLE IF EXISTS task_heads")
    op.execute("DROP TRIGGER IF EXISTS trg_task_checkpoints_immutable ON task_checkpoints")
    op.execute("DROP FUNCTION IF EXISTS task_checkpoints_are_immutable()")
    op.execute("DROP TABLE IF EXISTS task_checkpoints")
    op.execute("DROP TABLE IF EXISTS task_participant_grants")
