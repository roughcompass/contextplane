"""Quarantine: a materialised state on its own column, and the ledger that explains it.

E4-T2, implementing the decision recorded in
`.develop/adr/0016-quarantine-is-a-materialised-state-on-its-own-column.md`.

**Why a dedicated column rather than the bitemporal idiom.** The obvious move is
`t_invalidated_at`, which closes a row without destroying it and is what every
other reversible close in this schema uses. It does not work here, and the
reason is worth stating in the migration rather than only in the ADR, because
the next person to reach for it will be reading this file.

The serving predicate is:

    c.status IN ('staged', 'superseded')                              -- unconditional
    AND c.consolidated_at IS NOT NULL
    AND c.created_at <= :as_of
    AND (c.t_invalidated_at IS NULL OR c.t_invalidated_at > :as_of)   -- as_of-relative

The last term is `as_of`-relative *deliberately* -- "a claim closed after the
instant asked about was still believed then, which is the whole point of
asking". And `as_of` is caller-supplied on both transports: a query parameter on
`GET /v1/memory/claims`, an argument on the `query_claims` MCP tool. So a
quarantine written to `t_invalidated_at` at 14:00 is defeated by asking for
13:00.

`quarantined_at` is therefore read **unconditionally**, the way `status` is --
the shape `discard` already uses when it writes `status='rejected'` and means
"it never serves again".

**The ledger is a separate table, read at apply, revert and audit -- never at
read.** That is the seam the ADR draws. A boolean on the claim would record that
something is quarantined and forget when, by whom, and under which predicate,
and all three are what an incident review asks for first; putting the predicate
itself on the read path would be the read-time-rule design the ADR rejected.

**Revert is a status flip, not a delete**, following the position this schema
already takes in two places: "Revocation is temporal, never a delete" and
"Suspend is a status flip, not a delete... reinstating is one more flip and the
history reads as a suspension rather than a gap." A reverted quarantine leaves
the ledger row with `reverted_at` set, so the fact that content was withheld for
a period survives the withholding.
"""

from __future__ import annotations

from alembic import op

revision = "0071_claim_quarantine"
down_revision: str | None = "0070_grant_temporal_exclusion"
branch_labels: str | None = None
depends_on: str | None = None

#: Nullable, because the overwhelming majority of claims are not quarantined and
#: a NOT NULL sentinel would mean inventing a "not quarantined" instant. NULL is
#: the honest absence here, unlike migration 0069's `hydration_state`, where the
#: absent value had to be the refusing one because a missing hydration claim is
#: not evidence of hydration.
_ADD_COLUMN = "ALTER TABLE memory_claims ADD COLUMN quarantined_at TIMESTAMPTZ"

#: Partial, because the quarantined set is expected to be a small fraction of a
#: large table and every read filters `IS NULL`. Indexing the NULLs would be
#: indexing almost the whole table to answer a question the planner can already
#: answer from the row.
_INDEX = (
    "CREATE INDEX ix_memory_claims_quarantined ON memory_claims (owning_tenant_id, quarantined_at) "
    "WHERE quarantined_at IS NOT NULL"
)

_LEDGER = """
CREATE TABLE claim_quarantines (
    quarantine_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(tenant_id),

    -- The provenance predicate, as the operator stated it. Stored as the
    -- structured selector rather than rendered SQL: a stored fragment of SQL is
    -- a stored decision about how to evaluate it, and revert would then have to
    -- reproduce that evaluation exactly to find the same rows.
    predicate         JSONB NOT NULL,
    -- What the predicate actually matched, at apply time. Not recomputed at
    -- revert: the graph moves, and a revert that re-evaluated the predicate
    -- would restore a different set than it withheld. This is the set.
    matched_count     INTEGER NOT NULL,

    reason            TEXT NOT NULL,
    applied_by        UUID NOT NULL REFERENCES actors(actor_id),
    applied_at        TIMESTAMPTZ NOT NULL,

    -- A reverted quarantine keeps its row. The fact that content was withheld
    -- for a period is what an incident review is asking about, and a deleted
    -- row answers it with silence.
    reverted_by       UUID REFERENCES actors(actor_id),
    reverted_at       TIMESTAMPTZ,

    CONSTRAINT ck_quarantine_reverted_together
        CHECK ((reverted_by IS NULL) = (reverted_at IS NULL)),
    CONSTRAINT ck_quarantine_revert_after_apply
        CHECK (reverted_at IS NULL OR reverted_at > applied_at),
    CONSTRAINT ck_quarantine_matched_nonnegative CHECK (matched_count >= 0),
    CONSTRAINT ck_quarantine_reason_present CHECK (length(btrim(reason)) > 0)
)
"""

#: Which quarantine withheld a given claim. A claim can be matched by more than
#: one predicate over time -- quarantined, reverted, quarantined again by a
#: different rule -- so this is a join table rather than a column, and it is what
#: revert reads to find the rows it must restore.
_MEMBERSHIP = """
CREATE TABLE claim_quarantine_members (
    quarantine_id  UUID NOT NULL REFERENCES claim_quarantines(quarantine_id) ON DELETE CASCADE,
    claim_id       UUID NOT NULL,
    PRIMARY KEY (quarantine_id, claim_id)
)
"""

#: Revert needs every claim in one quarantine; the claim-leading direction
#: answers "why is this claim withheld", which is the question an operator asks
#: from the other end.
_MEMBERSHIP_INDEX = "CREATE INDEX ix_claim_quarantine_members_claim ON claim_quarantine_members (claim_id)"


def upgrade() -> None:
    op.execute(_ADD_COLUMN)
    op.execute(_INDEX)
    op.execute(_LEDGER)
    op.execute(_MEMBERSHIP)
    op.execute(_MEMBERSHIP_INDEX)


def downgrade() -> None:
    # Order matters: membership references the ledger, and the column is
    # independent of both. Dropping the column discards which claims were
    # withheld, which is why the ledger goes with it rather than being left as
    # an orphan record of a state nothing can express any more.
    op.execute("DROP TABLE claim_quarantine_members")
    op.execute("DROP TABLE claim_quarantines")
    op.execute("DROP INDEX ix_memory_claims_quarantined")
    op.execute("ALTER TABLE memory_claims DROP COLUMN quarantined_at")
