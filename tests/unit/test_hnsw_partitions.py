"""Unit tests — per-partition HNSW index for embeddings.

Covers:
- the baseline migration's embeddings DDL emits HNSW indexes for all 8 buckets
- ORM Embedding model still loads (tablename unchanged, mapping intact)
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Bootstrap — stub psycopg2 in case it's genuinely absent from the environment
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

# The per-partition HNSW indexes are built by the migration that creates the table --
# embeddings has no cutover script, so there is no runtime helper to test here.
# Matches the EMBEDDINGS_PARTITION_COUNT default. The migration reads the env var, so a
# deployment can change it at creation time; the tests assert the default.
_EMBEDDINGS_HASH_BUCKETS = 8

# The embeddings table is created partitioned from the start (one physical
# shape, not a shadow-and-cutover pair), so the baseline's upgrade() emits
# every partition's HNSW index directly.
_MIG_SPEC = importlib.util.spec_from_file_location(
    "baseline_schema",
    _REPO_ROOT / "contextplane" / "storage" / "migrations" / "versions" / "0001_baseline_schema.py",
)
assert _MIG_SPEC is not None and _MIG_SPEC.loader is not None
_mig = importlib.util.module_from_spec(_MIG_SPEC)
_MIG_SPEC.loader.exec_module(_mig)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Migration DDL — the baseline's embeddings section
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
        """upgrade() must issue one HNSW CREATE INDEX per partition (8 total).

        Runs the *whole* baseline upgrade() against a mocked `op` — there is
        no narrower "just the embeddings section" entry point anymore, since
        every table lives in one migration. Mocking op.execute is enough to
        run it end to end without a database; only the HNSW statements are
        asserted on.
        """
        from unittest.mock import MagicMock

        from alembic import op

        executed: list[str] = []

        def capture(sql: str, *_a: object, **_k: object) -> None:
            executed.append(str(sql))

        original_execute = getattr(op, "execute", None)
        original_get_bind = getattr(op, "get_bind", None)
        try:
            op.execute = capture  # type: ignore[attr-defined]
            op.get_bind = MagicMock()  # type: ignore[attr-defined]
            _mig.upgrade()
        finally:
            if original_execute is not None:
                op.execute = original_execute  # type: ignore[attr-defined]
            if original_get_bind is not None:
                op.get_bind = original_get_bind  # type: ignore[attr-defined]

        hnsw_stmts = [s for s in executed if "hnsw" in s.lower()]
        assert len(hnsw_stmts) == 8, f"Expected 8 HNSW statements, got {len(hnsw_stmts)}"
        for n in range(8):
            expected_table = f"embeddings_p{n}"
            assert any(expected_table in s for s in hnsw_stmts), f"No HNSW statement for {expected_table}"


class TestEmbeddingModelIntegrity:
    def test_embedding_tablename_unchanged(self) -> None:
        from contextplane.storage.models import Embedding

        assert Embedding.__tablename__ == "embeddings"

    def test_embedding_has_tenant_id_column(self) -> None:
        from sqlalchemy import inspect

        from contextplane.storage.models import Embedding

        mapper = inspect(Embedding)
        column_names = {c.key for c in mapper.columns}
        assert "tenant_id" in column_names

    def test_embedding_has_vector_column(self) -> None:
        from sqlalchemy import inspect

        from contextplane.storage.models import Embedding

        mapper = inspect(Embedding)
        column_names = {c.key for c in mapper.columns}
        assert "vector" in column_names

    def test_embedding_hnsw_note_present(self) -> None:
        from contextplane.storage.models import Embedding

        assert "PARTITION BY HASH" in (
            Embedding.__doc__ or ""
        ), "Partition note missing from Embedding docstring — Embedding must document its HASH partitioning"
