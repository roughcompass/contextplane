"""The embedding pipeline: one store for every kind of thing worth embedding.

Revision ID: 0003_phase2_embeddings_outbox
Revises: 0002_phase1_schema_registry
Create Date: 2026-05-06

* `embeddings`              — one row per embedded chunk, hash-partitioned by tenant
* `embedding_outbox`        — transactional outbox, written in the same transaction as
                              the source row so a rollback removes both atomically
* `embedding_outbox_failed` — dead-letter for rows past max_attempts

Also adds a generated `ts_vector` to `facts` so the lexical retrieval arm can run
full-text search without a separate pass.

**The store is polymorphic, and says so.** A row is identified by `(target_type,
target_id)`: `target_type` names what kind of thing was embedded and `target_id` points at
it. There is deliberately no foreign key on `target_id`, because it addresses more than
one table — the same shape `provenance` already uses. What keeps it honest is the closed
vocabulary on `target_type` plus the fact that exactly one module enqueues each kind.

An earlier draft of this schema called these columns `claim_type` and `claim_id`, with
`claim_id` foreign-keyed to `facts(fact_id)` — so the column named `claim_id` was the one
that did *not* refer to a claim. The names are now what they describe.

**Partitioned from creation.** Hash on `tenant_id`, eight buckets. Doing this here rather
than as a later cutover means there is one physical shape rather than two, and that the
shape tests exercise is the shape that runs.

**The unique key is what makes a re-drain safe.** Without `(tenant_id, target_type,
target_id, model_id, chunk_index)` the drain has no way to tell a retry from a new chunk,
so re-processing a row silently duplicates vectors and a superseded source leaves its old
ones behind. The key lets the drain upsert instead.

Statements are issued one per `op.execute` (asyncpg single-statement requirement).
"""

from __future__ import annotations

from alembic import op

from registry.embedding.targets import EMBEDDING_TARGETS, sql_set

revision = "0003_phase2_embeddings_outbox"
down_revision: str | None = "0002_phase1_schema_registry"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# DDL constants
# ---------------------------------------------------------------------------

_EXT_VECTOR = "CREATE EXTENSION IF NOT EXISTS vector"

# Eight buckets. Changing this is a rebuild rather than a migration, because hash
# partitioning has no way to redistribute rows across a different modulus — which is why
# the number lives here rather than in configuration that looks adjustable.
_HASH_BUCKETS = 8

# The vocabulary lives in code and is rendered into the constraint, so the two cannot
# drift. A conformance test reads the constraint back out of the live schema and asserts
# it enumerates exactly this set.
_TARGET_TYPE_SET = sql_set(EMBEDDING_TARGETS)

_EMBEDDINGS_DDL = f"""
CREATE TABLE embeddings (
    embedding_id  UUID NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,

    -- What was embedded, and which row. No FK on target_id: it addresses more than one
    -- table, so integrity rests on the closed vocabulary below and on there being one
    -- enqueuer per kind.
    target_type   TEXT NOT NULL,
    target_id     UUID NOT NULL,

    chunk_index   INTEGER NOT NULL DEFAULT 0,
    -- No default. A row inserted without a model id would claim to be whatever the
    -- default named, and vectors from two models are not comparable -- so the mistake
    -- would surface as quietly wrong rankings rather than as an error.
    model_id      TEXT NOT NULL,
    vector        VECTOR(384) NOT NULL,
    text_chunk    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Stored rather than computed per query, so the lexical arm reads an index instead
    -- of tokenising every candidate row on every request.
    ts_vector     TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text_chunk)) STORED,

    -- The partition key has to appear in every unique constraint on a partitioned table.
    PRIMARY KEY (embedding_id, tenant_id),

    CONSTRAINT ck_embed_target_type CHECK (target_type IN ({_TARGET_TYPE_SET})),

    -- One vector per (source, model, chunk). This is what lets the drain upsert, so a
    -- retry after a partial failure replaces rather than duplicates, and re-embedding a
    -- changed source overwrites its old chunks instead of leaving them to be retrieved.
    CONSTRAINT uq_embed_target_chunk
        UNIQUE (tenant_id, target_type, target_id, model_id, chunk_index)
) PARTITION BY HASH (tenant_id)
"""

_EMBEDDINGS_PARTITION_TEMPLATE = (
    "CREATE TABLE embeddings_p{n} PARTITION OF embeddings " "FOR VALUES WITH (modulus {modulus}, remainder {n})"
)

# Lookup by source: "which vectors belong to this row", used by the eraser and by the
# coverage metric.
_EMBEDDINGS_SOURCE_IDX = "CREATE INDEX idx_embed_target ON embeddings (tenant_id, target_type, target_id)"

_EMBEDDINGS_MODEL_IDX = "CREATE INDEX idx_embed_model ON embeddings (model_id)"

_EMBEDDINGS_FTS_IDX = "CREATE INDEX idx_embed_fts ON embeddings USING GIN (ts_vector)"

# HNSW per partition. m=16, ef_construction=64 balances build cost against recall.
# Built while the partitions are empty, which costs nothing; the index then fills
# incrementally as the drain inserts.
_EMBEDDINGS_HNSW_TEMPLATE = (
    "CREATE INDEX idx_embed_hnsw_p{n} ON embeddings_p{n} "
    "USING hnsw (vector vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)"
)

_OUTBOX_DDL = f"""
CREATE TABLE embedding_outbox (
    outbox_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(tenant_id),

    target_type     TEXT NOT NULL,
    target_id       UUID NOT NULL,

    -- The text travels on the row. The drain therefore never reads the source table,
    -- which is why adding a second kind of source needs no change to the drain at all.
    text_to_embed   TEXT NOT NULL,
    chunk_plan      JSONB NOT NULL,

    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ,

    CONSTRAINT ck_outbox_target_type CHECK (target_type IN ({_TARGET_TYPE_SET})),

    -- One queued request per target. Five edits to one fact before the drain ticks
    -- should collapse to one row carrying the newest text, not five rows that each
    -- embed a successively staler body. This is also the arbiter that makes an
    -- enqueue upsert possible.
    CONSTRAINT uq_outbox_target UNIQUE (tenant_id, target_type, target_id)
)
"""

# Not partial. The drain claims rows matching `last_error IS NULL OR last_attempt_at <
# cutoff`, so an index restricted to the first half leaves every retry unindexed -- which
# is exactly the population that has already cost something.
_OUTBOX_PENDING_IDX = "CREATE INDEX idx_outbox_pending ON embedding_outbox (enqueued_at)"

_OUTBOX_TENANT_IDX = "CREATE INDEX idx_outbox_tenant ON embedding_outbox (tenant_id, target_type)"

_OUTBOX_FAILED_DDL = f"""
CREATE TABLE embedding_outbox_failed (
    failed_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(tenant_id),

    target_type   TEXT NOT NULL,
    target_id     UUID NOT NULL,

    text_to_embed TEXT NOT NULL,
    chunk_plan    JSONB NOT NULL,
    failed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_text    TEXT NOT NULL,
    attempts      INTEGER NOT NULL,

    CONSTRAINT ck_outbox_failed_target_type CHECK (target_type IN ({_TARGET_TYPE_SET}))
)
"""

_OUTBOX_FAILED_TENANT_IDX = (
    "CREATE INDEX idx_outbox_failed_tenant ON embedding_outbox_failed (tenant_id, target_type, failed_at DESC)"
)

# tsvector on facts.body, for lexical search over raw bodies without joining embeddings.
_FACTS_TSVECTOR_COL = (
    "ALTER TABLE facts " "ADD COLUMN ts_vector TSVECTOR " "GENERATED ALWAYS AS (to_tsvector('english', body)) STORED"
)

_FACTS_FTS_IDX = "CREATE INDEX idx_facts_fts ON facts USING GIN (ts_vector)"


# ---------------------------------------------------------------------------
# Downgrade constants
# ---------------------------------------------------------------------------

_DROP_FACTS_FTS_IDX = "DROP INDEX IF EXISTS idx_facts_fts"
_DROP_FACTS_TSVECTOR_COL = "ALTER TABLE facts DROP COLUMN IF EXISTS ts_vector"
_DROP_OUTBOX_FAILED = "DROP TABLE IF EXISTS embedding_outbox_failed CASCADE"
_DROP_OUTBOX = "DROP TABLE IF EXISTS embedding_outbox CASCADE"
# Dropping the parent drops every partition with it.
_DROP_EMBEDDINGS = "DROP TABLE IF EXISTS embeddings CASCADE"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # pgvector extension — idempotent; must precede any use of the VECTOR type.
    op.execute(_EXT_VECTOR)

    op.execute(_EMBEDDINGS_DDL)
    for bucket in range(_HASH_BUCKETS):
        op.execute(_EMBEDDINGS_PARTITION_TEMPLATE.format(n=bucket, modulus=_HASH_BUCKETS))

    # Indexes on the parent cascade to every partition.
    op.execute(_EMBEDDINGS_SOURCE_IDX)
    op.execute(_EMBEDDINGS_MODEL_IDX)
    op.execute(_EMBEDDINGS_FTS_IDX)

    # HNSW is created per partition rather than on the parent, so each bucket's graph is
    # built and maintained independently.
    for bucket in range(_HASH_BUCKETS):
        op.execute(_EMBEDDINGS_HNSW_TEMPLATE.format(n=bucket))

    op.execute(_OUTBOX_DDL)
    op.execute(_OUTBOX_PENDING_IDX)
    op.execute(_OUTBOX_TENANT_IDX)
    op.execute(_OUTBOX_FAILED_DDL)
    op.execute(_OUTBOX_FAILED_TENANT_IDX)

    op.execute(_FACTS_TSVECTOR_COL)
    op.execute(_FACTS_FTS_IDX)


def downgrade() -> None:
    op.execute(_DROP_FACTS_FTS_IDX)
    op.execute(_DROP_FACTS_TSVECTOR_COL)
    op.execute(_DROP_OUTBOX_FAILED)
    op.execute(_DROP_OUTBOX)
    op.execute(_DROP_EMBEDDINGS)
    # The extension stays — other schemas on the same cluster may be using it.
