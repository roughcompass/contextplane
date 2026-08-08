"""Normalized external references, and the bindings that point at them.

**One row per external thing, not one per mention.** The collision key is stored
on the row and unique per tenant, so two producers naming the same issue with
different spellings converge on one reference rather than accumulating rows that
never meet. Normalization happens before the write -- system, namespace and kind
are folded to lowercase, the opaque id is only trimmed -- and the key is the
digest of exactly those four, computed by the one algorithm the context contract
froze. Recomputing it here in SQL would be a second answer to whether two
references are the same.

**Revision is deliberately outside the key.** Two revisions of one document are
one document; scoping by revision would make an edit look like a new reference,
and a reader counting distinct sources would over-count every time somebody saved.

**Bindings are a junction, not a generic entity table.** `subject_type` is a
closed CHECK set rather than an open string: a polymorphic table that accepts any
subject name is one where a typo creates a binding nobody can find, and where
nothing stops a later writer inventing a subject kind the readers do not know.
The pair is unique because a subject referencing one thing twice is a duplicate,
not corroboration -- the same rule the checkpoint contract enforces in memory,
enforced here for rows that arrive by any other path.

**Deleting a reference deletes its bindings.** The binding has no meaning without
the thing it points at, and leaving orphans would let a join silently return
fewer rows than the subject actually cited.
"""

from __future__ import annotations

from alembic import op

revision = "0031_external_references"
down_revision: str | None = "0030_task_memory"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# The handling classes a reference may inherit. Closed here for the same reason
# the schema closes it: a classification nobody declared is one no policy covers.
_CLASSIFICATIONS = "'public', 'internal', 'confidential', 'restricted'"

# What may bind a reference. Closed deliberately -- see the module docstring.
_SUBJECT_TYPES = "'task_checkpoint', 'context_item'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE context_external_references (
            reference_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),

            -- Normalized before the write: folded to lowercase, so two
            -- spellings of one source cannot occupy two rows.
            source_system       TEXT NOT NULL,
            source_namespace    TEXT NOT NULL,
            kind                TEXT NOT NULL,
            -- The other system's id, trimmed but not folded: its case is its
            -- own, and folding would merge two things it considers distinct.
            external_id         TEXT NOT NULL,

            classification      TEXT NOT NULL,
            -- The authority in the external system, not this one's.
            external_authority  TEXT NOT NULL,

            revision            TEXT,
            authorized_uri      TEXT,
            observed_at         TIMESTAMPTZ,

            -- The digest of (source_system, source_namespace, kind,
            -- external_id), computed by the contract's own algorithm and stored
            -- so uniqueness is one index on one column rather than a
            -- four-column comparison that has to agree with the code.
            collision_key       TEXT NOT NULL,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT ck_external_reference_classification
                CHECK (classification IN ({_CLASSIFICATIONS})),
            -- The four parts of the collision scope must be present and
            -- non-empty: a reference missing one of them collides with
            -- everything else missing it.
            CONSTRAINT ck_external_reference_scope_present
                CHECK (
                    length(source_system) > 0
                    AND length(source_namespace) > 0
                    AND length(kind) > 0
                    AND length(external_id) > 0
                ),
            -- Normalization is enforced, not assumed. A row written around the
            -- service would otherwise carry an unfolded spelling and never
            -- collide with the folded one.
            CONSTRAINT ck_external_reference_normalized
                CHECK (
                    source_system = lower(source_system)
                    AND source_namespace = lower(source_namespace)
                    AND kind = lower(kind)
                    AND external_id = btrim(external_id)
                )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_external_reference_collision
            ON context_external_references (tenant_id, collision_key)
        """
    )
    # The read path that resolves a producer's payload to an existing row.
    op.execute(
        """
        CREATE INDEX ix_external_reference_lookup
            ON context_external_references (tenant_id, source_system, kind, external_id)
        """
    )

    op.execute(
        f"""
        CREATE TABLE context_reference_bindings (
            binding_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

            reference_id  UUID NOT NULL
                REFERENCES context_external_references(reference_id) ON DELETE CASCADE,

            subject_type  TEXT NOT NULL,
            subject_id    UUID NOT NULL,

            bound_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT ck_reference_binding_subject_type
                CHECK (subject_type IN ({_SUBJECT_TYPES}))
        )
        """
    )
    # A subject cites one external thing once. Twice is a duplicate that reads
    # as two independent sources supporting one claim.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_reference_binding
            ON context_reference_bindings (tenant_id, subject_type, subject_id, reference_id)
        """
    )
    # "What does this subject cite" -- the assembler's direction.
    op.execute(
        """
        CREATE INDEX ix_reference_binding_subject
            ON context_reference_bindings (tenant_id, subject_type, subject_id)
        """
    )
    # "What cites this reference" -- the direction recall reads, and the one the
    # unique index above cannot serve because reference_id is its last column.
    op.execute(
        """
        CREATE INDEX ix_reference_binding_reference
            ON context_reference_bindings (tenant_id, reference_id)
        """
    )


def downgrade() -> None:
    # Bindings first: they hold the foreign key, and dropping the referenced
    # table out from under them would fail rather than cascade.
    op.execute("DROP TABLE IF EXISTS context_reference_bindings")
    op.execute("DROP TABLE IF EXISTS context_external_references")
