"""The audit cutover shadow table stops being schema and becomes a tool's scratch space.

`audit_log_new` was created by `0001_baseline_schema` as a permanent, empty
25-object fixture: a parent plus 24 monthly partitions, present in every
deployment, holding nothing. Its only consumer is `scripts/partition_migrate.py`,
which copies `audit_log` into it ahead of a transactional rename when an operator
repartitions the audit log. Nothing else reads it, nothing writes it, and no
deployment this project tracks has ever run that cutover.

**Why it does not belong in the schema.** Its DDL was a hand-maintained copy of
`audit_log`'s, and nothing forced the two to stay in step -- a column added to
`audit_log` by a later revision would not appear here, and the mismatch would
surface as a failed cutover at the exact moment an operator was mid-procedure on
a live system. It also declared no foreign keys on `tenant_id`/`actor_id`, for a
defensible reason (per-row FK checks would slow the bulk copy) with an
indefensible consequence: completing the cutover silently and permanently
dropped `audit_log`'s referential integrity, and nothing in the schema recorded
that the promoted table was weaker than the one it replaced.

The script now builds the shadow itself at run time from `pg_catalog` --
columns, partition bounds, indexes and constraints read off the live table, with
the foreign keys added after the copy rather than skipped -- so the shape it
copies into is derived from the table it is copying, not from a literal written
once in a migration. A tool's intermediate scratch table is the tool's business.

`downgrade()` recreates the table, its indexes, and its fixed 2025-01-origin
24-month partition window exactly as `0001_baseline_schema` first created them.
The DDL is copied inline rather than imported, so this revision keeps producing
the same bytes after the baseline module it once matched has moved on.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

from alembic import op

revision = "0054_drop_precreated_audit_shadow"
down_revision: str | None = "0049_arc_intent_nomenclature"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Copied from `0001_baseline_schema`'s `_FIXED_PARTITION_START` /
# `_FIXED_PARTITION_COUNT`, for `downgrade()` only.
_FIXED_PARTITION_START = datetime.date(2025, 1, 1)
_FIXED_PARTITION_COUNT = 24

_AUDIT_LOG_NEW_DDL = """
CREATE TABLE audit_log_new (
    audit_id     UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL,
    actor_id     UUID,
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    UUID NOT NULL,
    before_jsonb JSONB,
    after_jsonb  JSONB,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id   TEXT,
    error_code   TEXT,
    PRIMARY KEY (audit_id, ts)
) PARTITION BY RANGE (ts)
"""

_AUDIT_LOG_NEW_INDEXES = [
    "CREATE INDEX idx_audit_new_tenant_ts ON audit_log_new (tenant_id, ts DESC)",
    "CREATE INDEX idx_audit_new_target    ON audit_log_new (tenant_id, target_type, target_id, ts DESC)",
    "CREATE INDEX idx_audit_new_actor     ON audit_log_new (tenant_id, actor_id, ts DESC)",
]


def _monthly_partition_bounds(start: datetime.date, count: int) -> Iterator[tuple[str, str, str]]:
    """Yield (suffix, from_iso, to_iso) for *count* consecutive months from *start*."""
    year, month = start.year, start.month
    for _ in range(count):
        from_d = datetime.date(year, month, 1)
        next_year = year + (1 if month == 12 else 0)
        next_month = 1 if month == 12 else month + 1
        to_d = datetime.date(next_year, next_month, 1)
        suffix = f"{from_d.year:04d}_{from_d.month:02d}"
        yield suffix, from_d.isoformat(), to_d.isoformat()
        year, month = next_year, next_month


def upgrade() -> None:
    # CASCADE takes the 24 attached partitions with the parent. A partitioned
    # table's children are referenced by nothing outside it, so this drops
    # exactly this table's own objects.
    #
    # IF EXISTS rather than a bare DROP: an operator who has already completed
    # a cutover on this deployment has `audit_log_new` promoted to `audit_log`
    # and no table under the old name, and this revision must not fail on the
    # deployment that actually used the mechanism.
    op.execute("DROP TABLE IF EXISTS audit_log_new CASCADE")


def downgrade() -> None:
    op.execute(_AUDIT_LOG_NEW_DDL)
    for stmt in _AUDIT_LOG_NEW_INDEXES:
        op.execute(stmt)
    for suffix, from_iso, to_iso in _monthly_partition_bounds(_FIXED_PARTITION_START, _FIXED_PARTITION_COUNT):
        op.execute(
            f"CREATE TABLE audit_log_new_{suffix} PARTITION OF audit_log_new "
            f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
        )
