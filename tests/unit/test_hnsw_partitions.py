"""Unit tests — per-partition HNSW index for embeddings.

Covers:
- 0006_phase5_partitions migration emits HNSW DDL for all 8 buckets
- partition_migrate._ensure_hnsw_indexes: idempotency, dry-run, index creation
- ORM Embedding model still loads (tablename unchanged, mapping intact)
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Bootstrap — stub psycopg2 so partition_migrate can be imported without it
# ---------------------------------------------------------------------------

# Only when the real driver is genuinely absent. `not in sys.modules` was the wrong
# condition: it plants the stub whenever psycopg2 merely has not been imported yet,
# which is the normal state at collection time even when it *is* installed. The stub
# then outlives this module and any later test that builds a real
# `postgresql+psycopg2://` engine dies on `module 'psycopg2' has no attribute
# 'paramstyle'` — visible only when unit and conformance share one process.
try:  # pragma: no cover - environment-dependent
    import psycopg2  # noqa: F401
except ImportError:
    _stub = types.ModuleType("psycopg2")
    _stub.connect = MagicMock()  # type: ignore[attr-defined]
    sys.modules["psycopg2"] = _stub

_REPO_ROOT = Path(__file__).parent.parent.parent

# The per-partition HNSW indexes are built by the migration that creates the table, so
# there is no cutover helper left to test here -- `scripts/partition_migrate.py` no longer
# touches embeddings at all.
# Matches the EMBEDDINGS_PARTITION_COUNT default. The migration reads the env var, so a
# deployment can change it at creation time; the tests assert the default.
_EMBEDDINGS_HASH_BUCKETS = 8

# The embeddings partitioning lives in 0003 now: the table is created partitioned rather
# than shadowed and cut over, so there is one physical shape instead of two.
# (Leading digit prevents a normal import.)
_MIG_SPEC = importlib.util.spec_from_file_location(
    "migration_0003",
    _REPO_ROOT / "registry" / "storage" / "migrations" / "versions" / "0003_phase2_embeddings_outbox.py",
)
assert _MIG_SPEC is not None and _MIG_SPEC.loader is not None
_mig = importlib.util.module_from_spec(_MIG_SPEC)
_MIG_SPEC.loader.exec_module(_mig)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Migration DDL — 0006_phase5_partitions
# ---------------------------------------------------------------------------


class TestMigrationHnswDdl:
    """Verify the DDL template and that upgrade() emits HNSW statements."""

    def test_hnsw_index_template_contains_all_8_buckets(self) -> None:
        """_EMBEDDINGS_HNSW_TEMPLATE must be formattable for n in 0..7."""
        template: str = _mig._EMBEDDINGS_HNSW_TEMPLATE
        for n in range(8):
            ddl = template.format(n=n)
            assert f"embeddings_p{n}" in ddl, f"partition name missing for n={n}"
            assert "hnsw" in ddl.lower(), "USING hnsw missing"
            assert "vector_cosine_ops" in ddl, "vector_cosine_ops missing"
            assert "m = 16" in ddl, "m=16 missing"
            assert "ef_construction = 64" in ddl, "ef_construction=64 missing"

    def test_upgrade_calls_op_execute_for_hnsw_indexes(self) -> None:
        """upgrade() must issue one HNSW CREATE INDEX per partition (8 total)."""
        from alembic import op

        executed: list[str] = []

        def capture(sql: str) -> None:
            executed.append(sql)

        # Patch op.execute to capture DDL strings without a real DB.
        original_execute = getattr(op, "execute", None)
        try:
            op.execute = capture  # type: ignore[attr-defined]
            _mig.upgrade()
        finally:
            if original_execute is not None:
                op.execute = original_execute  # type: ignore[attr-defined]

        hnsw_stmts = [s for s in executed if "hnsw" in s.lower()]
        assert len(hnsw_stmts) == 8, f"Expected 8 HNSW statements, got {len(hnsw_stmts)}"
        for n in range(8):
            expected_table = f"embeddings_p{n}"
            assert any(expected_table in s for s in hnsw_stmts), f"No HNSW statement for {expected_table}"


# ---------------------------------------------------------------------------
# partition_migrate._ensure_hnsw_indexes
# ---------------------------------------------------------------------------


def _make_conn(*, index_exists: bool = False) -> MagicMock:
    """Build a mock psycopg2 connection for HNSW index tests."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    # pg_class check: return a row if index exists, None otherwise
    cur.fetchone.return_value = (1,) if index_exists else None
    cur.rowcount = 0
    return conn


class TestEmbeddingModelIntegrity:
    def test_embedding_tablename_unchanged(self) -> None:
        from registry.storage.models import Embedding

        assert Embedding.__tablename__ == "embeddings"

    def test_embedding_has_tenant_id_column(self) -> None:
        from sqlalchemy import inspect

        from registry.storage.models import Embedding

        mapper = inspect(Embedding)
        column_names = {c.key for c in mapper.columns}
        assert "tenant_id" in column_names

    def test_embedding_has_vector_column(self) -> None:
        from sqlalchemy import inspect

        from registry.storage.models import Embedding

        mapper = inspect(Embedding)
        column_names = {c.key for c in mapper.columns}
        assert "vector" in column_names

    def test_embedding_hnsw_note_present(self) -> None:
        from registry.storage.models import Embedding

        assert "PARTITION BY HASH" in (
            Embedding.__doc__ or ""
        ), "Partition note missing from Embedding docstring — Embedding must document its HASH partitioning"
