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


# The HNSW build parameters the partitions were created with, so a rebuild produces the
# same index rather than a differently-tuned one.
_HNSW_M = 16
_HNSW_EF_CONSTRUCTION = 64


def _embedding_partitions() -> list[str]:
    """Every child partition of `embeddings`, from the catalog.

    Read rather than constructed. An earlier version of this file built the names by
    interpolation and got them wrong, and because this whole path only runs behind an
    explicit opt-in the mistake never surfaced. Asking the database cannot be wrong
    about what the database contains.
    """
    rows = (
        op.get_bind()
        .exec_driver_sql(
            """
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'embeddings'::regclass
        ORDER BY c.relname
        """
        )
        .fetchall()
    )
    return [str(row[0]) for row in rows]


def _hnsw_indexes_on(table: str) -> list[str]:
    """Names of the HNSW indexes on one table, from the catalog."""
    rows = (
        op.get_bind()
        .exec_driver_sql(
            f"""
        SELECT i.relname
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class t ON t.oid = x.indrelid
        JOIN pg_am am ON am.oid = i.relam
        WHERE t.relname = '{table}' AND am.amname = 'hnsw'
        """
        )
        .fetchall()
    )
    return [str(row[0]) for row in rows]


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

    # Drop the ANN indexes first. Rebuilding them over empty partitions afterwards is
    # far cheaper than letting ALTER rewrite them in place.
    partitions = _embedding_partitions()
    dropped: list[tuple[str, str]] = []
    for partition in partitions:
        for index_name in _hnsw_indexes_on(partition):
            dropped.append((index_name, partition))
            op.execute(f"DROP INDEX IF EXISTS {index_name}")

    # Existing vectors are the wrong width and cannot be cast, so they go. The outbox
    # re-queues the work and the drain recomputes them at the new width.
    op.execute("TRUNCATE TABLE embeddings")
    op.execute(f"ALTER TABLE embeddings ALTER COLUMN vector TYPE vector({target})")

    for index_name, partition in dropped:
        op.execute(_hnsw_create(index_name, partition))

    # Re-enqueue everything that was truncated -- both kinds. A fact-only re-enqueue
    # would leave the claim half of the index permanently empty after a width change,
    # which is the failure this whole path exists to avoid.
    #
    # `ON CONFLICT DO NOTHING` is load-bearing now that the outbox has a unique key on
    # (tenant_id, target_type, target_id): an undrained row for the same target is
    # already carrying the newest text, so leaving it alone is correct.
    op.execute(
        """
        INSERT INTO embedding_outbox
            (outbox_id, tenant_id, target_type, target_id, text_to_embed,
             chunk_plan, attempts, enqueued_at)
        SELECT gen_random_uuid(), f.tenant_id, 'fact', f.fact_id, f.body,
               '[]'::jsonb, 0, NOW()
        FROM facts f
        ON CONFLICT (tenant_id, target_type, target_id) DO NOTHING
        """
    )

    # Claims, but only if the claim table exists yet.
    #
    # This migration sits ahead of the one that creates `lmm_claims`, so on a fresh chain
    # -- an operator setting a non-default width on an empty database -- the table is not
    # there and there are no claims to re-enqueue either. On a database already at head it
    # does exist and the refill matters, because the truncate above removed claim vectors
    # as well as fact ones.
    if _table_exists("lmm_claims"):
        _reenqueue_claims()


def _table_exists(name: str) -> bool:
    return op.get_bind().exec_driver_sql(f"SELECT to_regclass('{name}') IS NOT NULL").scalar() is True


def _reenqueue_claims() -> None:
    # The claim text has to be rendered the same way the application renders it. The rule
    # lives in `registry.service.embedding_index.index_text`; a conformance test asserts
    # the two agree, because the same rule now exists in two languages.
    op.execute(
        """
        INSERT INTO embedding_outbox
            (outbox_id, tenant_id, target_type, target_id, text_to_embed,
             chunk_plan, attempts, enqueued_at)
        SELECT gen_random_uuid(),
               c.owning_tenant_id,
               'claim',
               c.claim_id,
               replace(c.predicate, '_', ' ') || ': ' ||
                   CASE WHEN jsonb_typeof(c.value_jsonb) = 'string'
                        THEN c.value_jsonb #>> '{}'
                        ELSE c.value_jsonb::text
                   END,
               '[]'::jsonb,
               0,
               NOW()
        FROM lmm_claims c
        WHERE c.owning_tenant_id IS NOT NULL
          AND c.consolidated_at IS NOT NULL
          AND c.status IN ('staged', 'superseded')
          AND c.t_invalidated_at IS NULL
        ON CONFLICT (tenant_id, target_type, target_id) DO NOTHING
        """
    )


def downgrade() -> None:
    """No-op — see the module docstring."""
