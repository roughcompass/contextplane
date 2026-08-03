"""Living memory — the observation substrate: an actor's session event log.

The vision's loop is knowledge in, curated, served to agents, and back as
observations from their sessions. Everything except the return leg exists. This
table is the return leg: one immutable row per thing an agent did, scoped to
`(tenant_id, actor_id, session_id)`.

Deliberately LLM-free. Nothing here extracts, scores, or infers -- later phases
do that and need this to exist first.

**Scoped by actor, not by entity visibility.** Every other read path in this
schema is tenant-scoped, and tenant scoping is not sufficient here: a session is
readable by exactly one actor and by nobody else, including colleagues in the
same tenant. `VisibilityService` cannot express that -- its same-tenant branch
returns visible for any actor in the owning tenant, which is right for a catalog
entity and exactly wrong for a private conversation. The pattern followed
instead is `workspace`'s, which already scopes personal content by owning actor.

**Ordering is by an allocated sequence, not a timestamp.** A burst of events can
share a `created_at` to the microsecond, and a random v4 `event_id` tie-break
would impose an order that does not reflect what happened. Replay exists so an
agent can resume its own conversation; "the last five events" has to mean the
last five that occurred, which makes a per-session sequence the only honest key.
It is also what makes cursor pagination coherent, since an offset over an
append-only table re-reads shifting windows.
"""

from __future__ import annotations

from alembic import op

revision = "0025_lmm_session_events"
down_revision = "0024_arc_child_tenant_agreement"
branch_labels = None
depends_on = None


# Retention is a tenant setting because the requirement makes it one: 30 days by
# default, configurable to 180. Held on `tenants` rather than in application
# config so a deployment serving several tenants can honour different
# obligations without a redeploy.
_TENANT_RETENTION_DDL = """
ALTER TABLE tenants
    ADD COLUMN memory_retention_days INTEGER NOT NULL DEFAULT 30,
    ADD CONSTRAINT ck_tenants_memory_retention
        CHECK (memory_retention_days BETWEEN 1 AND 180)
"""

_EVENTS_DDL = """
CREATE TABLE memory_session_events (
    event_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(tenant_id),
    actor_id            UUID NOT NULL REFERENCES actors(actor_id),
    session_id          TEXT NOT NULL,
    seq                 BIGINT NOT NULL,
    kind                TEXT NOT NULL,
    body                TEXT NOT NULL,
    tool_name           TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    invalidated_at      TIMESTAMPTZ,
    invalidated_reason  TEXT,

    CONSTRAINT ck_mse_kind CHECK (
        kind IN ('user_message', 'agent_action', 'tool_invocation')
    ),
    CONSTRAINT ck_mse_session_len CHECK (char_length(session_id) BETWEEN 1 AND 200),

    -- Bytes, not characters. The requirement caps bodies at 16 KB, and a
    -- character count would admit roughly four times that in multi-byte text --
    -- which is the text most likely to arrive from a real conversation.
    CONSTRAINT ck_mse_body_bytes CHECK (octet_length(body) <= 16384),

    -- A tool invocation with no tool name is unattributable; any other kind
    -- carrying one is a caller confusing the vocabulary. Both directions,
    -- because only checking the first lets the second through silently.
    CONSTRAINT ck_mse_tool_name CHECK (
        (kind = 'tool_invocation') = (tool_name IS NOT NULL)
    ),

    -- An invalidated row must say why. Retention and an actor's own deletion
    -- both remove an event from replay, and an auditor reading the row later
    -- needs to tell those apart.
    CONSTRAINT ck_mse_invalidation CHECK (
        (invalidated_at IS NULL) = (invalidated_reason IS NULL)
    ),
    CONSTRAINT ck_mse_reason_len CHECK (
        invalidated_reason IS NULL OR char_length(invalidated_reason) BETWEEN 1 AND 64
    ),

    -- Ordering depends on this being unique, and two concurrent appends to one
    -- session serialise here rather than both taking the same position.
    CONSTRAINT uq_mse_session_seq UNIQUE (tenant_id, actor_id, session_id, seq)
)
"""

_INDEXES = [
    # Replay, and the read that allocates the next seq. Partial because the
    # default read path excludes invalidated rows, so they need not be traversed.
    "CREATE INDEX ix_mse_replay ON memory_session_events "
    "(tenant_id, actor_id, session_id, seq) WHERE invalidated_at IS NULL",
    # Session listing: an actor's own sessions, most recently active first.
    "CREATE INDEX ix_mse_listing ON memory_session_events "
    "(tenant_id, actor_id, created_at DESC)",
    # The retention worker's claim scan. A plain b-tree on one column is why
    # `expires_at` is materialised at write rather than derived at scan time.
    "CREATE INDEX ix_mse_expiry ON memory_session_events "
    "(expires_at) WHERE invalidated_at IS NULL",
    # Metadata equality filters, which are the whole reason metadata is
    # structure-region and unscanned.
    "CREATE INDEX ix_mse_metadata ON memory_session_events USING GIN (metadata)",
]


def upgrade() -> None:
    op.execute(_TENANT_RETENTION_DDL)
    op.execute(_EVENTS_DDL)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS memory_session_events")
    op.execute("ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_memory_retention")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS memory_retention_days")
