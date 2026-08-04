"""Creates the three usage rollup tables — actor-free, and kept forever.

Revision ID: 0044_usage_rollups
Revises: 0043_usage_events
Create Date: 2026-08-03

These are the tables the aggregate API reads. Raw usage events expire; these do
not, and the reason is the whole design: **an aggregate with no actor identifier is
not personal data.** So it carries no erasure obligation, no retention boundary,
and erasing an actor never rewrites one — which is what keeps a report for a closed
month reproducible after its raw rows are gone, and what stops a
right-to-be-forgotten request from silently changing a number someone already
quoted.

**The actor dimension survives as a count.** `distinct_actors` is
`COUNT(DISTINCT actor_id)` computed at rollup time and then discarded. That answers
"how many people used this" without storing which people, which is the only way
those two properties coexist.

**What a per-day distinct count cannot answer**, and this is recorded rather than
discovered later: the repeat-actor rate — "an actor seen in two or more distinct
windows" — cannot be reconstructed from daily counts once the raw rows expire. The
alternative was a salted presence sketch per day, which is a new dependency and a
new thing to be wrong about. The decision taken instead is to scope that one metric
to the raw retention window and label it as scoped. A bounded honest metric beats an
unbounded approximate one.

Not partitioned. Row count is bounded by tenant × day × dimension, which grows
linearly and slowly — unlike the raw table, where partitioning exists so whole
months can be dropped.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_usage_rollups"
down_revision = "0043_usage_events"
branch_labels = None
depends_on = None


# Latency percentiles are stored rather than recomputed, because the raw rows they
# came from will not exist later. `percentile_disc` rather than `_cont`: a real
# observed latency is more defensible than an interpolated one that no request
# actually experienced.
_TENANT_DAY = """
CREATE TABLE usage_rollup_tenant_day (
    tenant_id       UUID        NOT NULL,
    day             DATE        NOT NULL,
    surface         TEXT        NOT NULL,
    calls           BIGINT      NOT NULL,
    ok_calls        BIGINT      NOT NULL,
    error_calls     BIGINT      NOT NULL,
    -- A count, never a list. This is the field that keeps the table non-personal.
    distinct_actors INTEGER     NOT NULL,
    p50_ms          INTEGER,
    p95_ms          INTEGER,
    p99_ms          INTEGER,
    payload_bytes   BIGINT,
    payload_tokens  BIGINT,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, day, surface)
)
"""

_CAPABILITY_DAY = """
CREATE TABLE usage_rollup_capability_day (
    tenant_id       UUID        NOT NULL,
    day             DATE        NOT NULL,
    -- Taken from the resolved path params of the call, not from the operation
    -- column, which deliberately holds the route template.
    capability_id   UUID        NOT NULL,
    calls           BIGINT      NOT NULL,
    distinct_actors INTEGER     NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, day, capability_id)
)
"""

_TOOL_DAY = """
CREATE TABLE usage_rollup_tool_day (
    tenant_id       UUID        NOT NULL,
    day             DATE        NOT NULL,
    -- The MCP tool name. A closed set by construction: it comes from the
    -- registered catalog and changes only when an engineer adds a decorator.
    tool            TEXT        NOT NULL,
    calls           BIGINT      NOT NULL,
    ok_calls        BIGINT      NOT NULL,
    error_calls     BIGINT      NOT NULL,
    distinct_actors INTEGER     NOT NULL,
    p50_ms          INTEGER,
    p95_ms          INTEGER,
    p99_ms          INTEGER,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, day, tool)
)
"""

# Every read is "this tenant, this range", so the day belongs in the index with the
# tenant. The primary keys already cover the point lookups the upsert does.
_INDEXES = (
    "CREATE INDEX idx_urtd_tenant_day ON usage_rollup_tenant_day (tenant_id, day DESC)",
    "CREATE INDEX idx_urcd_tenant_day ON usage_rollup_capability_day (tenant_id, day DESC)",
    "CREATE INDEX idx_urtld_tenant_day ON usage_rollup_tool_day (tenant_id, day DESC)",
)

_TABLES = ("usage_rollup_tenant_day", "usage_rollup_capability_day", "usage_rollup_tool_day")


def upgrade() -> None:
    for ddl in (_TENANT_DAY, _CAPABILITY_DAY, _TOOL_DAY):
        op.execute(sa.text(ddl))
    for statement in _INDEXES:
        op.execute(sa.text(statement))


def downgrade() -> None:
    for table in _TABLES:
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
