"""Per-author accuracy: the index that makes it a lookup, and the two tables the loop needs.

E20-T3. Renumbered from the 0071 the task entry named -- that revision and 0072
were taken while E20 was being decomposed, by E4-T2's quarantine and by E20-T2's
drop of the aggregate actor floor.

**The index is the whole performance story.** `AgentAccuracyService` joins
`memory_claim_adjudication` to `memory_claims` and filters on
`(author_actor_id, window)`. `ix_memory_claims_author` exists and is
actor-only, so a windowed read scans every claim that actor ever wrote and
discards most of them. Leading with the actor and completing with `created_at`
lets the join drive from `memory_claims` and finish through the existing
`ix_memory_adjudication_claim`, rather than probing the adjudication table once
per candidate row.

`ix_memory_claims_author` stays. Other readers key on author with no time bound,
and a two-column index answers those only by prefix -- which it does, but the
existing one is smaller and this migration is not the place to decide whether
that matters.

**Partial on `author_actor_id IS NOT NULL`**, because the column is nullable: a
claim can be authored by a system path with no actor, and indexing those rows
would be indexing a value nobody can query for.

**`agent_failure_pattern_report.groups` is a JSONB snapshot rather than a child
table**, following `memory_calibration_mapping.bins`. A report is a *fitted
aggregate over a window* -- it is read whole, never joined into, and never
updated in place. A normalized child would add a join and a second write path to
something that only ever exists as one immutable blob.

**The activation gate is a CHECK, not only a service rule.** An instruction may
not be `active` without naming the report that motivated it. That mirrors
`ck_memory_calibration_error`, which puts the identical kind of gate in the
schema so no writer can activate a mapping whose measured error is too high. The
point in both cases is that the rule survives a writer nobody has read yet.

The partial unique index on `(author_actor_id) WHERE status = 'active'` is
`uq_memory_calibration_active`'s pattern: at most one live version per subject,
enforced where two concurrent activations would otherwise both succeed.

**No change to `actors.actor_kind`**, deliberately, per E20's scope decision:
there is no reliable signal to populate it from, and this epic tracks agents by
`author_actor_id` instead.
"""

from __future__ import annotations

from alembic import op

revision = "0073_agent_accuracy_and_instructions"
down_revision: str | None = "0072_drop_the_aggregate_actor_floor"
branch_labels: str | None = None
depends_on: str | None = None

_ACCURACY_INDEX = (
    "CREATE INDEX ix_memory_claims_author_created ON memory_claims (author_actor_id, created_at) "
    "WHERE author_actor_id IS NOT NULL"
)

_REPORT = """
CREATE TABLE agent_failure_pattern_report (
    report_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenants(tenant_id),
    author_actor_id          UUID NOT NULL REFERENCES actors(actor_id),

    window_start             TIMESTAMPTZ NOT NULL,
    window_end               TIMESTAMPTZ NOT NULL,

    -- The header figures, stored rather than recomputed. A report is evidence
    -- about a window, and a window's contents change: recomputing later would
    -- answer a different question under the same report id.
    n_adjudicated            INTEGER NOT NULL,
    n_incorrect              INTEGER NOT NULL,
    -- The autonomy dimension, independent of correctness. An agent can be
    -- accurate but need constant steering, or fast and wrong, and those are
    -- different problems needing different instruction changes.
    n_intervention_sessions  INTEGER NOT NULL,
    n_sessions               INTEGER NOT NULL,

    -- [{claim_category, predicate, incorrect_count, total_count, rate,
    --   example_claim_ids}] -- read whole, never joined into.
    groups                   JSONB NOT NULL,

    generated_at             TIMESTAMPTZ NOT NULL,
    generated_by             UUID REFERENCES actors(actor_id),

    CONSTRAINT ck_failure_report_window_ordered CHECK (window_end > window_start),
    CONSTRAINT ck_failure_report_incorrect_within_adjudicated CHECK (n_incorrect <= n_adjudicated),
    CONSTRAINT ck_failure_report_counts_nonneg CHECK (
        n_adjudicated >= 0 AND n_incorrect >= 0 AND n_intervention_sessions >= 0 AND n_sessions >= 0
    ),
    -- An intervention happens *in* a session, so there cannot be more of them
    -- than there were sessions. Cheap, and it catches the join that counted
    -- interventions across the wrong window.
    CONSTRAINT ck_failure_report_interventions_within_sessions
        CHECK (n_intervention_sessions <= n_sessions)
)
"""

_REPORT_INDEX = (
    "CREATE INDEX ix_failure_report_actor_window " "ON agent_failure_pattern_report (author_actor_id, window_end DESC)"
)

_INSTRUCTION = """
CREATE TABLE agent_instruction (
    instruction_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(tenant_id),
    author_actor_id       UUID NOT NULL REFERENCES actors(actor_id),

    version               INTEGER NOT NULL,
    content               TEXT NOT NULL,

    -- The evidence this version answers. Nullable so a draft or a rejected
    -- proposal can exist without one; required to activate, by the CHECK below.
    motivated_by_report_id UUID REFERENCES agent_failure_pattern_report(report_id),

    status                TEXT NOT NULL,
    activated_at          TIMESTAMPTZ,
    superseded_at         TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL,
    created_by            UUID REFERENCES actors(actor_id),

    CONSTRAINT uq_agent_instruction_version UNIQUE (author_actor_id, version),
    CONSTRAINT ck_agent_instruction_status CHECK (status IN ('active', 'superseded', 'rejected')),
    CONSTRAINT ck_agent_instruction_version_positive CHECK (version >= 1),
    CONSTRAINT ck_agent_instruction_content_present CHECK (length(btrim(content)) > 0),

    -- An instruction cannot activate without citing the report that motivated
    -- it. The gate is here rather than only in the service for the reason
    -- `ck_memory_calibration_error` gives: a rule enforced in one writer is a
    -- rule the next writer skips without noticing.
    CONSTRAINT ck_agent_instruction_active_cites_evidence
        CHECK (status <> 'active' OR motivated_by_report_id IS NOT NULL),

    -- The three status values and the two timestamps say the same thing, so
    -- they must not be able to disagree.
    CONSTRAINT ck_agent_instruction_active_timestamps
        CHECK ((status = 'active') = (activated_at IS NOT NULL AND superseded_at IS NULL))
)
"""

#: At most one live version per agent, enforced where two concurrent
#: activations would otherwise both succeed. `uq_memory_calibration_active`'s
#: pattern, for the same reason: "which instruction is in force" must have one
#: answer at every instant.
_INSTRUCTION_ACTIVE = (
    "CREATE UNIQUE INDEX uq_agent_instruction_active ON agent_instruction (author_actor_id) WHERE status = 'active'"
)


def upgrade() -> None:
    op.execute(_ACCURACY_INDEX)
    op.execute(_REPORT)
    op.execute(_REPORT_INDEX)
    op.execute(_INSTRUCTION)
    op.execute(_INSTRUCTION_ACTIVE)


def downgrade() -> None:
    # Instruction first: it references the report table.
    op.execute("DROP TABLE agent_instruction")
    op.execute("DROP TABLE agent_failure_pattern_report")
    op.execute("DROP INDEX ix_memory_claims_author_created")
