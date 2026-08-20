"""Drop the two tuning knobs the corroboration curve they configured no longer has.

`memory_confidence_policy` offered a tenant `corroboration_headroom` and
`corroboration_scale`. Both were parameters of `base + (1 - base) · headroom ·
(1 - e^(-mass/scale))`, and that formula is gone: independent sources now combine
as a noisy OR, `1 - Π(1 - pᵢ)`, which has no headroom fraction and no rate
constant to set.

Columns left behind after their consumer is removed are the worst kind of dead
schema, because they still look configurable. An operator reading this table
would find two numbers with plausible names, defaults, and CHECK bounds, set one,
and get no error and no effect. Nothing in this repository detects that: the
column would be unread rather than unreferenced, and no gate here flags an unread
column.

`ck_memory_confidence_bounds` is dropped and re-added rather than left to fall
away with the columns. PostgreSQL drops a table-level CHECK when any column it
mentions is dropped, so a bare `DROP COLUMN` would silently take the bounds on
`contradiction_penalty`, `confirmed_confidence`, `confirmation_hold_days` and
`decay_multiplier` with it -- losing four live constraints while removing two
dead columns.

No data is at risk. Nothing reads this table: `ConfidencePolicy` is constructed
with its shipped defaults at every call site, so no tenant's configured value has
ever reached a score. The table's own future belongs to the tenant-scoped scoring
work, which moves per-tenant magnitudes onto the profile-binding system; this
revision removes only what the corroboration change superseded.
"""

from __future__ import annotations

from alembic import op

revision = "0057_drop_superseded_corroboration_knobs"
down_revision: str | None = "0056_arc_graph_promoted_source_evidence"
branch_labels: str | None = None
depends_on: str | None = None

#: The reduced form: every term of the original except the two dropped columns.
_BOUNDS_WITHOUT_CORROBORATION = """
ALTER TABLE memory_confidence_policy ADD CONSTRAINT ck_memory_confidence_bounds CHECK (
    contradiction_penalty >= 0 AND contradiction_penalty <= 0.800
    AND confirmed_confidence >= 0.850 AND confirmed_confidence <= 0.980
    AND confirmation_hold_days >= 1 AND confirmation_hold_days <= 730
    AND decay_multiplier >= 0.25 AND decay_multiplier <= 4.00
)
"""

#: Copied verbatim from `0001_baseline_schema`, for `downgrade()` only.
_BOUNDS_WITH_CORROBORATION = """
ALTER TABLE memory_confidence_policy ADD CONSTRAINT ck_memory_confidence_bounds CHECK (
    corroboration_headroom > 0 AND corroboration_headroom <= 0.800
    AND corroboration_scale >= 0.50 AND corroboration_scale <= 10.00
    AND contradiction_penalty >= 0 AND contradiction_penalty <= 0.800
    AND confirmed_confidence >= 0.850 AND confirmed_confidence <= 0.980
    AND confirmation_hold_days >= 1 AND confirmation_hold_days <= 730
    AND decay_multiplier >= 0.25 AND decay_multiplier <= 4.00
)
"""


def upgrade() -> None:
    op.execute("ALTER TABLE memory_confidence_policy DROP CONSTRAINT ck_memory_confidence_bounds")
    op.execute("ALTER TABLE memory_confidence_policy DROP COLUMN corroboration_headroom")
    op.execute("ALTER TABLE memory_confidence_policy DROP COLUMN corroboration_scale")
    op.execute(_BOUNDS_WITHOUT_CORROBORATION)


def downgrade() -> None:
    # Restored with the baseline's defaults, so rows that survived the upgrade come
    # back satisfying the constraint that is about to be re-added. Without the
    # defaults every existing row would have NULLs and the CHECK would be unknown
    # rather than true, which passes -- but the next write would then have to
    # supply values for knobs nothing sets.
    op.execute(
        "ALTER TABLE memory_confidence_policy ADD COLUMN corroboration_headroom NUMERIC(4, 3) NOT NULL DEFAULT 0.600"
    )
    op.execute(
        "ALTER TABLE memory_confidence_policy ADD COLUMN corroboration_scale NUMERIC(4, 2) NOT NULL DEFAULT 2.00"
    )
    op.execute("ALTER TABLE memory_confidence_policy DROP CONSTRAINT ck_memory_confidence_bounds")
    op.execute(_BOUNDS_WITH_CORROBORATION)
