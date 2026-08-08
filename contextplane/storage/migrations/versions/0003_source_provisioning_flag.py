"""Per-source opt-in for provisioning an entity a connector's subject never resolved to.

Revision ID: 0003_source_provisioning_flag
Revises: 0002_unlinked_claim_rejection
Create Date: 2026-08-05

A connector's parsed facts name a subject by a reference the connector derived
on its own -- a path, a manifest key, a deterministic hash of both -- and that
reference frequently does not resolve to any entity the tenant has already
created. Until now the only outcome was `unlinked`: correct, but permanent,
because nothing ever created the missing entity for the claim to attach to.

This adds one column, `may_provision_entities`, defaulting to **false** so no
existing source's behavior changes the moment this migration runs. An operator
opts a specific source into provisioning by declaring it with the flag set;
only then does an unresolved subject get an entity created for it before the
claim is linked. Off by default because auto-creating entities from unverified
connector output is a real decision an operator makes deliberately, not a
convenience the schema should default into.
"""

from __future__ import annotations

from alembic import op

revision = "0003_source_provisioning_flag"
down_revision: str | None = "0002_unlinked_claim_rejection"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE memory_source_governance " "ADD COLUMN may_provision_entities BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE memory_source_governance DROP COLUMN may_provision_entities")
