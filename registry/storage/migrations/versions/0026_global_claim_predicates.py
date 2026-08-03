"""Organization-level claim predicates: one shared vocabulary across tenants.

Living memory needs a predicate to mean the same thing everywhere. Two tenants
each defining `depends_on` with their own value type cannot corroborate or
contradict each other -- the claims are not comparable, and the whole point of a
shared graph is that they are.

`vocabulary_values.tenant_id` was `NOT NULL`, so a global row could not be
expressed at all. This migration opens exactly that door and no wider.

**Global scope is limited to one kind.** A row may have `tenant_id IS NULL` only
when `kind = 'claim_predicate'`. Every other vocabulary kind stays tenant-scoped
with the semantics it has today. The nullable column is a keyhole, not a general
cross-tenant write surface, and the CHECK is what keeps it that way -- without it
the column's nullability would silently apply to every kind.

**Predicate metadata is required and immutable.** A claim predicate declares its
`value_type`, `claim_category`, and `definition`. Requiring them at the schema
level is what stops a predicate existing without a declared type, which is the
failure that makes claims incomparable in the first place. Immutability is
enforced in the service layer rather than here: retyping an existing term
silently reinterprets every claim already stored against it, so a predicate
changes by deprecation and a successor, never in place.

**Uniqueness splits in two.** The single `(tenant_id, kind, value)` index cannot
express "one global row per name" because NULL never equals NULL in a unique
index -- every global predicate could reuse the same name. Two partial indexes
replace it: one for tenant rows, one for global rows.

**Downgrade is conditional.** Once a deployment has seeded a global predicate,
claims reference it and dropping it would orphan them. The downgrade refuses
rather than cascading, and says so.
"""

from __future__ import annotations

from alembic import op

revision = "0026_global_claim_predicates"
down_revision = "0025_lmm_session_events"
branch_labels = None
depends_on = None

_CLAIM_PREDICATE = "claim_predicate"


_COLUMNS = """
ALTER TABLE vocabulary_values
    ALTER COLUMN tenant_id DROP NOT NULL,
    ADD COLUMN value_type     TEXT,
    ADD COLUMN claim_category TEXT,
    ADD COLUMN definition     TEXT
"""

# Any pre-existing local claim predicate must carry metadata before the CHECK
# is added, or the constraint would fail to validate on a deployment that has
# some. Seeded with a marker rather than a guess: a predicate whose meaning
# nobody recorded should be visibly unreconciled, not quietly given a plausible
# type that later claims are validated against.
_BACKFILL = f"""
UPDATE vocabulary_values
   SET value_type     = COALESCE(value_type, 'string'),
       claim_category = COALESCE(claim_category, 'unclassified'),
       definition     = COALESCE(definition, 'Pre-existing predicate; metadata not recorded at creation.')
 WHERE kind = '{_CLAIM_PREDICATE}'
"""

_CONSTRAINTS = f"""
ALTER TABLE vocabulary_values
    -- The keyhole: only a claim predicate may be global.
    ADD CONSTRAINT ck_vocab_global_is_claim_predicate CHECK (
        tenant_id IS NOT NULL OR kind = '{_CLAIM_PREDICATE}'
    ),
    -- A predicate without a declared type cannot validate anything written
    -- against it, which is the failure this whole requirement exists to fix.
    ADD CONSTRAINT ck_vocab_claim_predicate_metadata CHECK (
        kind <> '{_CLAIM_PREDICATE}'
        OR (value_type IS NOT NULL AND char_length(value_type) > 0
            AND claim_category IS NOT NULL AND char_length(claim_category) > 0
            AND definition IS NOT NULL AND char_length(definition) > 0)
    )
"""

# NULL is never equal to NULL in a unique index, so the existing composite index
# would let every global predicate reuse one name. Split by scope instead.
_INDEXES = [
    "DROP INDEX IF EXISTS idx_vocab_kind_value",
    "CREATE UNIQUE INDEX uq_vocab_tenant_kind_value ON vocabulary_values "
    "(tenant_id, kind, value) WHERE tenant_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_vocab_global_kind_value ON vocabulary_values "
    "(kind, value) WHERE tenant_id IS NULL",
    # Resolution reads global predicates before local ones, so that lookup gets
    # its own index rather than scanning the tenant-oriented one.
    "CREATE INDEX ix_vocab_global_predicates ON vocabulary_values (kind, value) "
    "WHERE tenant_id IS NULL",
]

# Refusing rather than cascading. A seeded global predicate has claims written
# against it; dropping the column that holds it would orphan them, and doing so
# inside a downgrade is the least visible possible moment for that to happen.
_DOWNGRADE_GUARD = """
DO $$
DECLARE
    global_count INTEGER;
BEGIN
    SELECT count(*) INTO global_count
      FROM vocabulary_values
     WHERE tenant_id IS NULL;

    IF global_count > 0 THEN
        RAISE EXCEPTION
            'refusing to downgrade: % global claim predicate(s) exist. They are the shared '
            'vocabulary claims are written against, and removing them would orphan those claims. '
            'Deprecate and remove the global predicates first, or restore a pre-migration backup.',
            global_count;
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(_COLUMNS)
    op.execute(_BACKFILL)
    op.execute(_CONSTRAINTS)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute(_DOWNGRADE_GUARD)
    op.execute("DROP INDEX IF EXISTS ix_vocab_global_predicates")
    op.execute("DROP INDEX IF EXISTS uq_vocab_global_kind_value")
    op.execute("DROP INDEX IF EXISTS uq_vocab_tenant_kind_value")
    op.execute(
        "CREATE UNIQUE INDEX idx_vocab_kind_value ON vocabulary_values (tenant_id, kind, value)"
    )
    op.execute(
        "ALTER TABLE vocabulary_values "
        "  DROP CONSTRAINT IF EXISTS ck_vocab_claim_predicate_metadata, "
        "  DROP CONSTRAINT IF EXISTS ck_vocab_global_is_claim_predicate, "
        "  DROP COLUMN IF EXISTS definition, "
        "  DROP COLUMN IF EXISTS claim_category, "
        "  DROP COLUMN IF EXISTS value_type, "
        "  ALTER COLUMN tenant_id SET NOT NULL"
    )
