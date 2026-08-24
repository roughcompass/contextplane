"""A source connector and an upload policy can be withdrawn.

E14-T2. Before this, neither table had a revocation column, no `ALTER` added
one, and nothing in the tree issued an `UPDATE` or `DELETE` against them — only
an `INSERT` and a by-id `SELECT`. **Once registered, they were permanent.**

That matters more than it sounds. A connector is not a narrow setting: it names
the schemes, hosts, media types and sizes ARC may fetch, and in
`allowed_verifier_ids` it names *who may approve what it fetches* — for every
future fetch, not the next one. So the widest control in the governance surface
was the one with no off switch, and the failure it invites is the ordinary one:
registered permissively during a migration, then never narrowed because nobody
could.

## What revocation does and does not reach

**It stops new admissions and nothing else.** Material already admitted through
this connector stays admitted, because it *was* validly admitted — the grant was
in force at the time, and rewriting that would make the record describe a
history that did not happen. The revocation is dated so an auditor can place any
admission on one side of it or the other.

This follows the precedent the codebase already set twice: a revoked ARC revision
tombstones rather than disappearing, and a withheld receipt is marked rather than
deleted. Both say "this was fine then and is not now" without editing the past.

**Attributed, and with a reason.** The same shape `reporting_obligations` uses
for its classification and `context_receipts` uses for withholding: the three
columns are set together or not at all, so a row can never record that something
was revoked without recording who decided and why. A revocation nobody is
accountable for is the kind that gets discovered rather than reviewed.
"""

from __future__ import annotations

from alembic import op

revision = "0079_source_grant_revocation"
down_revision: str | None = "0078_claim_trust_transitions"
branch_labels: str | None = None
depends_on: str | None = None

#: Both tables get the identical triple. Written as one loop rather than two
#: blocks so they cannot drift: a connector and an upload policy are the same
#: kind of standing grant, and a revocation that meant something different on
#: one of them would be a distinction nobody chose.
_TABLES = ("arc_source_connectors", "arc_source_upload_policies")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN revoked_at TIMESTAMPTZ")
        op.execute(f"ALTER TABLE {table} ADD COLUMN revoked_by UUID REFERENCES actors(actor_id)")
        op.execute(f"ALTER TABLE {table} ADD COLUMN revocation_reason TEXT")
        # All three or none. A `revoked_at` with no actor is a withdrawal
        # nobody is accountable for, and a reason of "" is the same as none --
        # which is why the length floor is inside the constraint rather than
        # trusted to the service.
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_revocation_is_attributed CHECK ("
            "    (revoked_at IS NULL AND revoked_by IS NULL AND revocation_reason IS NULL)"
            " OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL"
            "     AND revocation_reason IS NOT NULL"
            "     AND char_length(revocation_reason) BETWEEN 20 AND 2000)"
            ")"
        )
        # Partial, on the state the admission path checks. The live grants are
        # the ones read on every fetch; the revoked ones accumulate and are read
        # by an audit.
        op.execute(f"CREATE INDEX ix_{table}_live ON {table} (owning_scope) WHERE revoked_at IS NULL")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP INDEX ix_{table}_live")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_revocation_is_attributed")
        op.execute(f"ALTER TABLE {table} DROP COLUMN revocation_reason")
        op.execute(f"ALTER TABLE {table} DROP COLUMN revoked_by")
        op.execute(f"ALTER TABLE {table} DROP COLUMN revoked_at")
