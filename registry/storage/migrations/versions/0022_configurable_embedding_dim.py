"""Align the embeddings.vector width with EMBEDDING_DIM.

Revision ID: 0022_configurable_embedding_dim
Revises: 0021_entitlement_auth_consolidation
Create Date: 2026-07-30

The vector width used to be a literal 384 in the DDL, in the ORM, and in the
stub embedder, which meant no embedding model of any other width could ever be
configured. It now follows ``EMBEDDING_DIM``.

**For every deployment on the shipped default this migration does nothing.**
384 is still the default, the model still produces 384-d vectors, and the
migration sees a match and returns.

It only acts when an operator has changed ``EMBEDDING_DIM``, and then the change
is destructive by nature: a stored vector cannot be converted to a different
width, only recomputed. So the resized path drops every embeddings row, widens
the column, rebuilds the HNSW indexes, and re-enqueues every fact into
``embedding_outbox`` for the drain to re-embed. Search recall is degraded from
the moment this runs until the drain catches up.

Because ``alembic upgrade`` typically runs unattended as part of a deploy, that
path will not run on its own. It requires a second, explicit opt-in:

    EMBEDDING_DIM=1536 EMBEDDING_DIM_ALLOW_REBUILD=true alembic upgrade head

Without it the migration raises and changes nothing, so a mistyped
``EMBEDDING_DIM`` fails the deploy instead of erasing the index.

The downgrade is deliberately a no-op. Reversing means another destructive
rebuild, and inferring the previous width is not possible from the schema alone
— to go back, set ``EMBEDDING_DIM`` to the old value and upgrade again.
"""

from __future__ import annotations

import os

from alembic import op

revision: str = "0022_configurable_embedding_dim"
down_revision: str | None = "0021_entitlement_auth_consolidation"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


# Must match 0006_phase5_partitions.py — the hash-partition count and the HNSW
# build parameters the per-partition indexes were originally created with.
_EMBEDDINGS_HASH_BUCKETS = 8
_HNSW_M = 16
_HNSW_EF_CONSTRUCTION = 64

# Two index-naming eras coexist: `embeddings_hnsw` on the single table created
# by 0003, and `idx_embed_new_hnsw_p{n}` on the hash partitions introduced by
# 0006. Which one is present depends on whether the partition cutover has run,
# so both are handled.
_LEGACY_HNSW_INDEX = "embeddings_hnsw"


def _partition_hnsw_index(partition: int) -> str:
    return f"idx_embed_new_hnsw_p{partition}"


def _hnsw_create(index_name: str, table: str) -> str:
    return (
        f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} "
        f"USING hnsw (vector vector_cosine_ops) "
        f"WITH (m = {_HNSW_M}, ef_construction = {_HNSW_EF_CONSTRUCTION})"
    )


def _configured_dim() -> int:
    raw = os.environ.get("EMBEDDING_DIM", "384")  # config: intentional
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"EMBEDDING_DIM must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"EMBEDDING_DIM must be positive, got {value}")
    return value


def _current_dim(connection: object) -> int | None:
    """Width of the live embeddings.vector column, or None if undeclared.

    pgvector stores the declared width in ``atttypmod``. A column declared as
    bare ``vector`` with no width reports -1.
    """
    result = (
        op.get_bind()
        .exec_driver_sql(
            """
        SELECT a.atttypmod
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'embeddings' AND a.attname = 'vector' AND a.attnum > 0
        """
        )
        .fetchone()
    )
    if result is None:
        return None
    typmod = int(result[0])
    return None if typmod < 0 else typmod


def _is_partitioned() -> bool:
    result = op.get_bind().exec_driver_sql("SELECT relkind FROM pg_class WHERE relname = 'embeddings'").fetchone()
    return result is not None and str(result[0]) == "p"


def upgrade() -> None:
    target = _configured_dim()
    current = _current_dim(op.get_bind())

    if current == target:
        return

    if os.environ.get("EMBEDDING_DIM_ALLOW_REBUILD", "").lower() not in ("1", "true", "yes"):  # config: intentional
        raise RuntimeError(
            f"EMBEDDING_DIM is {target} but the embeddings.vector column is "
            f"{current if current is not None else 'unconstrained'}. Changing the width "
            f"requires deleting and recomputing every embedding — stored vectors cannot be "
            f"converted. Re-run with EMBEDDING_DIM_ALLOW_REBUILD=true to accept that, or set "
            f"EMBEDDING_DIM back to {current} to leave the column alone."
        )

    partitioned = _is_partitioned()

    # Drop the ANN indexes first. Rebuilding them afterwards over an empty table
    # is far cheaper than letting ALTER rewrite them in place.
    if partitioned:
        for bucket in range(_EMBEDDINGS_HASH_BUCKETS):
            op.execute(f"DROP INDEX IF EXISTS {_partition_hnsw_index(bucket)}")
    op.execute(f"DROP INDEX IF EXISTS {_LEGACY_HNSW_INDEX}")

    # Existing vectors are the wrong width and cannot be cast. They go, and the
    # outbox re-queues the work so the drain recomputes them at the new width.
    op.execute("TRUNCATE TABLE embeddings")
    op.execute(f"ALTER TABLE embeddings ALTER COLUMN vector TYPE vector({target})")

    if partitioned:
        for bucket in range(_EMBEDDINGS_HASH_BUCKETS):
            op.execute(_hnsw_create(_partition_hnsw_index(bucket), f"embeddings_p{bucket}"))
    else:
        op.execute(_hnsw_create(_LEGACY_HNSW_INDEX, "embeddings"))

    # Re-enqueue every fact. ON CONFLICT DO NOTHING keeps this safe when the
    # outbox already holds undrained rows for some of them.
    op.execute(
        """
        INSERT INTO embedding_outbox
            (outbox_id, tenant_id, claim_type, claim_id, text_to_embed,
             chunk_plan, attempts, created_at)
        SELECT gen_random_uuid(), f.tenant_id, 'fact', f.fact_id, f.body,
               '[]'::jsonb, 0, NOW()
        FROM facts f
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    """No-op — see the module docstring."""
