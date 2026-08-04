"""A claim-scoped semantic index, deliberately separate from the shared one.

The shared embedding pipeline is bound to facts at three points: `embeddings.claim_id`
carries a foreign key to `facts.fact_id`, and both the outbox and its dead-letter table
key on `fact_id` with the same reference. All three already carry a `claim_type`
discriminator, so polymorphism was anticipated and never used.

Unifying claims into that pipeline means relaxing a foreign key and adding a
polymorphic identifier on two hash-partitioned tables that a worker is actively
draining. That is a migration with its own risk profile and its own review, and it is
gated on a decision that has not been taken. Building it opportunistically here --
inside a phase about serving -- would be doing the risky part quietly as a side effect
of something else.

So this table stands alone. It is not the end state, and it is named and shaped so the
eventual merge is a data migration rather than an archaeology exercise: one row per
claim, the model version that produced it, and the namespace it was indexed under.

**No foreign key to a partitioned table, and a hard link to the claim.** The claim
reference is a real foreign key with a cascade, because an index entry outliving its
claim would serve a vector that resolves to nothing -- and the resolution happens after
the visibility check, so a dangling row is a hole in the governed path rather than a
tidy-up problem.

**The model version is on the row, not in configuration.** Vectors from two models are
not comparable, and a deployment that changes models has rows of both kinds until it
finishes re-indexing. Reading the version from configuration would silently rank them
against each other; reading it from the row lets a query exclude what it cannot
compare.
"""

from __future__ import annotations

from alembic import op

revision = "0042_claim_embedding"
down_revision = "0041_drop_private_annotation_entry_kind"
branch_labels = None
depends_on = None


_TABLE = """
CREATE TABLE lmm_claim_embedding (
    claim_id      UUID PRIMARY KEY REFERENCES lmm_claims(claim_id) ON DELETE CASCADE,
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

    -- Copied from the claim so the index can be filtered without joining. A
    -- namespace filter that had to join would be applied after ranking, and
    -- filtering after ranking returns a short page of a long list and calls it the
    -- top ten.
    namespace     TEXT,

    -- What was actually embedded. Kept so a re-index can tell whether the text
    -- changed or only the model did, and so a human can see what the vector
    -- represents rather than inferring it.
    indexed_text  TEXT NOT NULL,

    embedding     VECTOR(384) NOT NULL,
    model_version TEXT NOT NULL,

    indexed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_lmm_claim_embedding_text CHECK (char_length(trim(indexed_text)) > 0),
    CONSTRAINT ck_lmm_claim_embedding_model CHECK (char_length(trim(model_version)) > 0)
)
"""

_INDEXES = [
    # Cosine, matching the shared index, so a later merge does not have to re-tune
    # the distance function as well as move the rows.
    "CREATE INDEX lmm_claim_embedding_hnsw ON lmm_claim_embedding " "  USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX ix_lmm_claim_embedding_tenant ON lmm_claim_embedding (tenant_id)",
    "CREATE INDEX ix_lmm_claim_embedding_namespace ON lmm_claim_embedding (namespace) " "  WHERE namespace IS NOT NULL",
    # The re-index sweep asks which model produced a row.
    "CREATE INDEX ix_lmm_claim_embedding_model ON lmm_claim_embedding (model_version)",
]


def upgrade() -> None:
    op.execute(_TABLE)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lmm_claim_embedding")
