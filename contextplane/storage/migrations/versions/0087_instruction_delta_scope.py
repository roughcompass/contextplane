"""A delta names what it corrects, and a broadcast needs a second name on it.

E23-T2, implementing ADR 0021. `0085` shipped one targeting rule — a delta
corrects one submitted instruction set — and named it a placeholder for the
retrieval policy ADR 0020 deferred. This is that policy.

**Three scopes, and `target_digest` becomes nullable.** A delta corrects one
declared set, or whatever a named principal declares at any digest, or every
declaring caller in the tenant. Three rather than a predicate language: a rule
engine over instruction content is the inference ADR 0020 rejected as
unfalsifiable, and an author who wrote a predicate could not say afterwards which
agents it reached.

**A tenant-scoped delta needs an approver who is not its author, and that is a
CHECK.** It reaches every declaring agent, including ones whose instructions
nobody has read, so one person authoring it is the shape ADR 0020's second
dissent warns about with the fleet as the blast radius. The narrower scopes do
not require it: two people to correct one agent is the friction that makes a
channel go unused, and the channel's whole value is that a correction is cheaper
than letting an agent stay wrong.

Enforced here rather than in the service for the reason every rule in this
schema is: a second writer cannot be the one place that forgets.

**Existing rows are `digest`-scoped**, which is what they were. The default is
set for the backfill and then dropped, so a later insert has to say which scope
it means rather than inheriting one nobody chose — the lesson `0084` learned
about `actor_kind` one column over.
"""

from __future__ import annotations

from alembic import op

revision = "0087_instruction_delta_scope"
down_revision: str | None = "0086_evaluation_runs"
branch_labels: str | None = None
depends_on: str | None = None

#: The three scopes, in the order ADR 0021 argues them: narrowest first. Order
#: is not enforced by the type, but the read serves them in it, and writing them
#: this way is what makes a fourth scope obviously a decision.
_SCOPES = "'digest', 'principal', 'tenant'"

_STEPS: tuple[str, ...] = (
    "ALTER TABLE instruction_deltas ADD COLUMN scope TEXT NOT NULL DEFAULT 'digest'",
    "ALTER TABLE instruction_deltas ADD COLUMN target_principal UUID REFERENCES actors(actor_id)",
    "ALTER TABLE instruction_deltas ADD COLUMN approved_by UUID REFERENCES actors(actor_id)",
    "ALTER TABLE instruction_deltas ADD COLUMN approved_at TIMESTAMPTZ",
    # Dropped after the backfill: an insert must say which scope it means.
    "ALTER TABLE instruction_deltas ALTER COLUMN scope DROP DEFAULT",
    # `target_digest` was NOT NULL and is only meaningful under `digest` scope.
    "ALTER TABLE instruction_deltas ALTER COLUMN target_digest DROP NOT NULL",
    f"ALTER TABLE instruction_deltas ADD CONSTRAINT ck_delta_scope CHECK (scope IN ({_SCOPES}))",
    # Each scope carries exactly its own target and no other. A delta with both a
    # digest and a principal would be two statements in one row, and the read
    # would have to choose which one the author meant.
    """
    ALTER TABLE instruction_deltas ADD CONSTRAINT ck_delta_target_matches_scope CHECK (
        (scope = 'digest'    AND target_digest IS NOT NULL AND target_principal IS NULL)
     OR (scope = 'principal' AND target_digest IS NULL     AND target_principal IS NOT NULL)
     OR (scope = 'tenant'    AND target_digest IS NULL     AND target_principal IS NULL)
    )
    """,
    # ADR 0021's second decision. Both columns or neither, and the approver is
    # never the author -- a self-approval is an assertion wearing an approval's
    # shape, which is the rule `ck_grant_not_self` already states one table over.
    """
    ALTER TABLE instruction_deltas ADD CONSTRAINT ck_delta_broadcast_is_approved CHECK (
        scope <> 'tenant'
        OR (approved_by IS NOT NULL AND approved_at IS NOT NULL AND approved_by <> authored_by)
    )
    """,
    """
    ALTER TABLE instruction_deltas ADD CONSTRAINT ck_delta_approval_is_complete CHECK (
        (approved_by IS NULL) = (approved_at IS NULL)
    )
    """,
)

#: The serving reads, one index per scope. Partial on `withdrawn_at IS NULL` for
#: the reason the digest index already is: a withdrawn delta is never served, so
#: indexing it grows what the resolver walks with rows it skips.
_INDEXES: tuple[str, ...] = (
    """
    CREATE INDEX ix_instruction_deltas_by_principal
        ON instruction_deltas (tenant_id, target_principal, authored_at)
        WHERE withdrawn_at IS NULL AND scope = 'principal'
    """,
    """
    CREATE INDEX ix_instruction_deltas_tenant_wide
        ON instruction_deltas (tenant_id, authored_at)
        WHERE withdrawn_at IS NULL AND scope = 'tenant'
    """,
)

_DOWN: tuple[str, ...] = (
    "DROP INDEX ix_instruction_deltas_tenant_wide",
    "DROP INDEX ix_instruction_deltas_by_principal",
    "ALTER TABLE instruction_deltas DROP CONSTRAINT ck_delta_approval_is_complete",
    "ALTER TABLE instruction_deltas DROP CONSTRAINT ck_delta_broadcast_is_approved",
    "ALTER TABLE instruction_deltas DROP CONSTRAINT ck_delta_target_matches_scope",
    "ALTER TABLE instruction_deltas DROP CONSTRAINT ck_delta_scope",
    # Anything not digest-scoped has no digest to restore, so the column cannot
    # go back to NOT NULL while those rows exist. They are the rows this
    # migration made possible, and dropping them is what reverting it means.
    "DELETE FROM instruction_deltas WHERE scope <> 'digest'",
    "ALTER TABLE instruction_deltas ALTER COLUMN target_digest SET NOT NULL",
    "ALTER TABLE instruction_deltas DROP COLUMN approved_at",
    "ALTER TABLE instruction_deltas DROP COLUMN approved_by",
    "ALTER TABLE instruction_deltas DROP COLUMN target_principal",
    "ALTER TABLE instruction_deltas DROP COLUMN scope",
)


def upgrade() -> None:
    for statement in _STEPS:
        op.execute(statement)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWN:
        op.execute(statement)
