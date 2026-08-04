"""Adds outcome mix and served payload cost to the capability usage rollup.

Revision ID: 0047_usage_capability_outcomes
Revises: 0046_usage_rollups
Create Date: 2026-08-03

The capability grain shipped with call and distinct-actor counts, which answers "is
anyone using this" and not much else. A capability *owner* needs two more things and
they are the two the operator summary already has at the tenant grain:

- **The outcome mix.** "Two thousand calls" and "two thousand calls, four hundred of
  them errors" are the same number and completely different situations, and the
  second one is the owner's problem to fix rather than the caller's.
- **What it cost to serve.** An owner deciding whether a capability is worth
  maintaining needs the bytes it returned, not only how often it was asked.

Backfilled with zeros rather than left null. `calls` is already `NOT NULL` on this
table, so a null outcome column would be the only nullable count here and every
reader would need to handle it. Zero is also the honest value for rows computed
before these columns existed: no errors were *recorded*, and re-rolling any day
inside the raw retention window recomputes it exactly. Days whose raw rows have
already expired keep their zeros, which is why this is stated here rather than
discovered from a chart showing a suspiciously clean past.

`payload_bytes` stays nullable, matching the tenant grain: it is a sum of a nullable
column, and a null there means "nothing measured it" — MCP calls, and streaming
responses — which is not the same as zero bytes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_usage_capability_outcomes"
down_revision = "0046_usage_rollups"
branch_labels = None
depends_on = None

_ADD = (
    "ALTER TABLE usage_rollup_capability_day ADD COLUMN ok_calls BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE usage_rollup_capability_day ADD COLUMN error_calls BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE usage_rollup_capability_day ADD COLUMN payload_bytes BIGINT",
)

_DROP = (
    "ALTER TABLE usage_rollup_capability_day DROP COLUMN IF EXISTS ok_calls",
    "ALTER TABLE usage_rollup_capability_day DROP COLUMN IF EXISTS error_calls",
    "ALTER TABLE usage_rollup_capability_day DROP COLUMN IF EXISTS payload_bytes",
)


def upgrade() -> None:
    for statement in _ADD:
        op.execute(sa.text(statement))


def downgrade() -> None:
    for statement in _DROP:
        op.execute(sa.text(statement))
