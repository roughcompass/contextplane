"""Close the source-authority vocabulary, and record what set each claim's floor.

`source_authority` shipped as bare `TEXT NOT NULL`, which is not what an ordered
closed vocabulary means. Closed means constrained: an unrecognized value would
compare as equal to nothing and sort by string accident, and consolidation
resolves conflicts by this column.

**Two axes, flattened ownership-major.** Authority answers whether the asserting
tenant owns the subject (`owner` / `observer`) and how reproducible the step from
artefact to typed triple was (`human` / `extraction` / `inference`). The flattening
is lossless only because a claim from a non-owning tenant never outranks one from
the owner at any derivation tier, so no owner/observer inversion is expressible.

Cross-tenant supersession must still be gated on
`author_tenant_id <> owning_tenant_id`, never on rank. "Different tenant" routes a
proposal; "lower rank" contests and can supersede. One ordinal cannot say both,
which is why both tenant columns stay side by side rather than collapsing into it.

**`unattributed` is not a tier.** A claim whose subject did not resolve has no
owner to compare its author against, so its standing is undefined rather than low.
Naming an observer tier there would assert a determination nobody made, and nothing
would mark it stale once a curator links the claim to an entity the author does own.
The biconditional CHECK ties it to the same condition as the two null-pair checks
already on this table.

**Provenance records its own derivation tier.** Authority is a minimum over a
claim's evidence -- the weakest link, because a claim is only as checkable as the
least checkable step needed to produce it. Without the per-row tier, an auditor can
see the authority but not which piece of evidence set it.
"""

from __future__ import annotations

from alembic import op

revision = "0028_claim_source_authority"
down_revision = "0027_lmm_claims"
branch_labels = None
depends_on = None

_AUTHORITY_VALUES = (
    "owner_human",
    "owner_extraction",
    "owner_inference",
    "observer_human",
    "observer_extraction",
    "observer_inference",
    "unattributed",
)

_AUTHORITY_LIST = ", ".join(f"'{v}'" for v in _AUTHORITY_VALUES)

_CLAIMS_CONSTRAINTS = f"""
ALTER TABLE lmm_claims
    ADD CONSTRAINT ck_lmm_claims_authority CHECK (
        source_authority IN ({_AUTHORITY_LIST})
    ),
    -- Same shape as the two null-pair checks already on this table: an
    -- authority with no owner to compare against exists exactly when there is
    -- no subject to derive an owner from.
    ADD CONSTRAINT ck_lmm_claims_unattributed CHECK (
        (source_authority = 'unattributed') = (subject_entity_id IS NULL)
    )
"""

# `inference` is the floor, so it is the safe default for any row written before
# this column existed: a backfill can only ever understate authority, never
# invent it.
_PROVENANCE_COLUMN = """
ALTER TABLE lmm_claim_provenance
    ADD COLUMN derivation TEXT NOT NULL DEFAULT 'inference',
    ADD CONSTRAINT ck_lmm_prov_derivation CHECK (
        derivation IN ('human', 'extraction', 'inference')
    )
"""

# Consolidation reads claims about a subject in authority order. Without the
# column in the index that is a sort over every claim on the subject.
_INDEX = (
    "CREATE INDEX ix_lmm_claims_subject_authority ON lmm_claims "
    "(subject_entity_id, predicate, source_authority) WHERE status = 'staged'"
)


def upgrade() -> None:
    op.execute(_CLAIMS_CONSTRAINTS)
    op.execute(_PROVENANCE_COLUMN)
    op.execute(_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_lmm_claims_subject_authority")
    op.execute(
        "ALTER TABLE lmm_claim_provenance "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_prov_derivation, "
        "  DROP COLUMN IF EXISTS derivation"
    )
    op.execute(
        "ALTER TABLE lmm_claims "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_unattributed, "
        "  DROP CONSTRAINT IF EXISTS ck_lmm_claims_authority"
    )
