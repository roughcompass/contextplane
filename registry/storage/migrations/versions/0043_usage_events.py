"""Creates usage_events — who used which surface, partitioned monthly.

Revision ID: 0043_usage_events
Revises: 0042_claim_embedding
Create Date: 2026-08-03

The operational metrics tier cannot answer any adoption question, and the reason
is structural rather than a gap to fill: every Prometheus label value must be
enumerable when the metric is written, and tenant, actor and capability are
exactly the dimensions that grow as the product is adopted. So that tier carries
no identity at all. This table is where identity lives.

**Structurally incapable of holding content.** Every column is an identifier, a
timestamp, a number, or a term from a set fixed in ``registry/usage/vocabularies.py``.
There is no text column. That is the point: a free-text field in a high-volume
table carrying a retention window and a right-to-be-forgotten obligation is where
someone eventually pastes a customer email, and scanning for that afterwards is a
losing game while making it unrepresentable is not. A conformance test asserts a
text column cannot be added.

Query text is where the pull is strongest and is answered as ARC answers it — a
digest, a length, and a result count. That supports "how often did a search return
nothing" without recording what anyone asked for.

**``actor_id`` is nullable, and that is a decision rather than laxity.** A request
that fails to authenticate has no actor. Recording it with a null keeps "how many
callers could not authenticate" answerable; dropping the row instead would quietly
change the denominator of every rate computed from this table. A null means *no
identity was resolved*, never *not recorded*.

**Partitioning matches the audit log**: monthly range partitions on the timestamp,
24 pre-created. That is the treatment already given to ``audit_log``, ``episodes``,
``notifications`` and ``pii_detection_log``, and the existing archival runbook
covers the resulting shape without change.

Known limit, inherited rather than introduced: nothing creates partitions beyond
the pre-created window. The hourly partition job only counts children eligible for
archival. This table reaches that wall with considerably more data behind it than
the audit log will, and the follow-up belongs in a requirement for on-demand
creation rather than in a hand-rolled job here.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

import sqlalchemy as sa
from alembic import op

revision = "0043_usage_events"
down_revision = "0042_claim_embedding"
branch_labels = None
depends_on = None


# The vocabularies are duplicated here as SQL literals on purpose: a migration
# must be readable and runnable at the revision it was written, without importing
# application code that may have moved on. `tests/conformance/test_usage_schema.py`
# asserts these and `registry/usage/vocabularies.py` describe the same sets, so the
# duplication is checked rather than trusted.
_SURFACES = "'rest','mcp'"
_OUTCOMES = "'ok','error'"
_STATUS_CLASSES = "'2xx','3xx','4xx','5xx','other'"

_TABLE = f"""
CREATE TABLE usage_events (
    event_id           UUID        NOT NULL,
    occurred_at        TIMESTAMPTZ NOT NULL,
    tenant_id          UUID        NOT NULL,
    -- Nullable by design; see the module docstring.
    actor_id           UUID,
    surface            TEXT        NOT NULL,
    -- A route template or an MCP tool name. Never a resolved path: substituting a
    -- real id here would put an entity reference in a column meant to be a
    -- bounded operation label, and the top-operations query would fragment into
    -- one row per entity.
    operation          TEXT        NOT NULL,
    outcome            TEXT        NOT NULL,
    status_class       TEXT        NOT NULL,
    latency_ms         INTEGER     NOT NULL,
    -- How many rows the caller got back. Null where the operation does not
    -- return a collection; zero is a real and interesting answer.
    result_count       INTEGER,
    payload_bytes      INTEGER,
    payload_tokens     INTEGER,
    -- Correlates a row to the request's log lines and audit entries.
    request_id         TEXT,
    -- Bounded set of entities the call concerned. An array rather than a join
    -- table because it is read as a whole and never joined against.
    subject_entity_ids UUID[]      NOT NULL DEFAULT '{{}}',
    -- Digest, length and result count only. There is no column for the terms.
    query_digest       TEXT,
    query_length       INTEGER,
    CONSTRAINT chk_usage_surface      CHECK (surface IN ({_SURFACES})),
    CONSTRAINT chk_usage_outcome      CHECK (outcome IN ({_OUTCOMES})),
    CONSTRAINT chk_usage_status_class CHECK (status_class IN ({_STATUS_CLASSES})),
    -- A digest is a fixed-width hex sha256 or absent. Bounding the length is what
    -- stops this column becoming the free-text field the table refuses to have.
    CONSTRAINT chk_usage_query_digest CHECK (query_digest IS NULL OR char_length(query_digest) = 64),
    CONSTRAINT chk_usage_query_length CHECK (query_length IS NULL OR query_length >= 0),
    CONSTRAINT chk_usage_latency      CHECK (latency_ms >= 0),
    -- The partition key has to be in the key for a partitioned table.
    PRIMARY KEY (event_id, occurred_at)
) PARTITION BY RANGE (occurred_at)
"""

# Indexes carry `occurred_at` because every read is time-bounded — the rollup job
# sweeps a day, the expiry worker sweeps a boundary, and no query wants the whole
# table. A tenant-only index would be scanned for a range it cannot restrict.
_INDEXES = (
    "CREATE INDEX idx_usage_tenant_time ON usage_events (tenant_id, occurred_at DESC)",
    "CREATE INDEX idx_usage_operation ON usage_events (tenant_id, surface, operation, occurred_at DESC)",
    # Partial: the RTBF purge and the actor-distinct count are the only readers,
    # and both ignore rows with no resolved identity.
    "CREATE INDEX idx_usage_actor ON usage_events (actor_id, occurred_at DESC) WHERE actor_id IS NOT NULL",
)


def _monthly_partition_bounds(start: datetime.date, count: int) -> Iterator[tuple[str, str, str]]:
    """Yield (partition_name, from_iso, to_iso) for monthly partitions.

    A local copy rather than an import from 0001: that helper hardcodes the
    ``audit_log_`` prefix, and a migration that reaches into another migration for
    a helper breaks the moment either is edited.
    """
    year, month = start.year, start.month
    for _ in range(count):
        from_d = datetime.date(year, month, 1)
        next_year = year + (1 if month == 12 else 0)
        next_month = 1 if month == 12 else month + 1
        to_d = datetime.date(next_year, next_month, 1)
        yield f"usage_events_{from_d.year:04d}_{from_d.month:02d}", from_d.isoformat(), to_d.isoformat()
        year, month = next_year, next_month


# Same window as 0001's audit_log partitions, and for the same reason: the test
# suite runs on fixed clocks inside it, so a narrower range makes tests fail for
# reasons unrelated to what they assert.
_PARTITION_START = datetime.date(2025, 1, 1)
_PARTITION_COUNT = 24


def upgrade() -> None:
    op.execute(sa.text(_TABLE))
    for name, from_iso, to_iso in _monthly_partition_bounds(_PARTITION_START, _PARTITION_COUNT):
        op.execute(
            sa.text(f"CREATE TABLE {name} PARTITION OF usage_events FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')")
        )
    for statement in _INDEXES:
        op.execute(sa.text(statement))


def downgrade() -> None:
    # Dropping the parent takes every child with it.
    op.execute(sa.text("DROP TABLE IF EXISTS usage_events"))
