"""Unit tests — partition_migrate.py (pure helpers + mock DB).

Covers:
- month_range generation (boundary conditions)
- partition_name / shadow_table / archive_table naming
- ident() refusing anything that is not a plain identifier
- shadow_child_name mapping and its refusal on a non-conforming name
- rewrite_indexdef: ONLY stripped, name and table retargeted, refusals
- copy_sql / catchup_sql: explicit column lists, PK anti-join
- rename_sql composition
- idempotency (archive table present -> early exit)
- resume detection (chunk already in the shadow -> skip copy)
- shadow reuse refused when its columns have drifted from the source
- verification refusing a shadow that holds more rows than its source
- the cutover transaction: lock timeout retries, and rollback on tail mismatch
"""

from __future__ import annotations

import datetime
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import the module under test (no psycopg2 required at import time)
# ---------------------------------------------------------------------------

_MOD_PATH = "scripts.partition_migrate"

# Provide a stub psycopg2 so the module can be imported in environments
# where psycopg2-binary is not installed.
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

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    _MOD_PATH,
    Path(__file__).parent.parent.parent / "scripts" / "partition_migrate.py",
)
assert _spec is not None and _spec.loader is not None
_pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pm)  # type: ignore[union-attr]

month_range = _pm.month_range
partition_name = _pm.partition_name
shadow_table = _pm.shadow_table
archive_table = _pm.archive_table
ident = _pm.ident
shadow_child_name = _pm.shadow_child_name
rewrite_indexdef = _pm.rewrite_indexdef
rename_sql = _pm.rename_sql
child_rename_statements = _pm.child_rename_statements
copy_sql = _pm.copy_sql
catchup_sql = _pm.catchup_sql
run_migration = _pm.run_migration
_migrate_range_table = _pm._migrate_range_table
_build_shadow = _pm._build_shadow
_copy = _pm._copy
_verify = _pm._verify
_cutover = _pm._cutover

_COLUMNS = ["audit_id", "tenant_id", "actor_id", "action", "target_type", "target_id", "ts"]


# ---------------------------------------------------------------------------
# month_range
# ---------------------------------------------------------------------------


class TestMonthRange:
    def test_single_month(self) -> None:
        result = list(month_range(datetime.date(2026, 3, 15), datetime.date(2026, 4, 1)))
        assert result == [(datetime.date(2026, 3, 1), datetime.date(2026, 4, 1))]

    def test_truncates_start_to_first_of_month(self) -> None:
        result = list(month_range(datetime.date(2026, 3, 20), datetime.date(2026, 5, 1)))
        assert result[0] == (datetime.date(2026, 3, 1), datetime.date(2026, 4, 1))
        assert len(result) == 2

    def test_year_boundary(self) -> None:
        result = list(month_range(datetime.date(2025, 11, 1), datetime.date(2026, 2, 1)))
        assert len(result) == 3
        assert result[0] == (datetime.date(2025, 11, 1), datetime.date(2025, 12, 1))
        assert result[1] == (datetime.date(2025, 12, 1), datetime.date(2026, 1, 1))
        assert result[2] == (datetime.date(2026, 1, 1), datetime.date(2026, 2, 1))

    def test_empty_when_start_gte_end(self) -> None:
        result = list(month_range(datetime.date(2026, 5, 1), datetime.date(2026, 5, 1)))
        assert result == []

    def test_twelve_months_forward(self) -> None:
        start = datetime.date(2026, 5, 1)
        end = datetime.date(2027, 5, 1)
        result = list(month_range(start, end))
        assert len(result) == 12
        assert result[-1] == (datetime.date(2027, 4, 1), datetime.date(2027, 5, 1))


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------


class TestNaming:
    def test_partition_name_basic(self) -> None:
        assert partition_name("audit_log_new", datetime.date(2026, 5, 1)) == "audit_log_new_2026_05"

    def test_partition_name_zero_pads_month(self) -> None:
        assert partition_name("audit_log_new", datetime.date(2025, 1, 15)) == "audit_log_new_2025_01"

    def test_partition_name_december(self) -> None:
        assert partition_name("audit_log_new", datetime.date(2025, 12, 1)) == "audit_log_new_2025_12"

    def test_shadow_and_archive(self) -> None:
        assert shadow_table("audit_log") == "audit_log_new"
        assert archive_table("audit_log") == "audit_log_archive"


class TestIdent:
    @pytest.mark.parametrize("name", ["audit_log", "_x", "audit_log_2025_01", "idx_audit_tenant_ts"])
    def test_accepts_plain_identifiers(self, name: str) -> None:
        assert ident(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            'audit_log"; DROP TABLE tenants; --',
            "audit log",
            "AuditLog",
            "1_audit",
            "",
            "audit_log;",
        ],
    )
    def test_refuses_anything_else(self, name: str) -> None:
        with pytest.raises(ValueError, match="not a plain lowercase SQL identifier"):
            ident(name)


class TestShadowChildName:
    def test_maps_source_child_onto_shadow(self) -> None:
        assert shadow_child_name("audit_log_2025_01", "audit_log", "audit_log_new") == "audit_log_new_2025_01"

    def test_refuses_a_child_without_the_parent_prefix(self) -> None:
        """A name the function had to invent could duplicate or leave a gap."""
        with pytest.raises(ValueError, match="does not start with"):
            shadow_child_name("legacy_audit_2025_01", "audit_log", "audit_log_new")


# ---------------------------------------------------------------------------
# rewrite_indexdef
# ---------------------------------------------------------------------------


class TestRewriteIndexdef:
    _SRC = "CREATE INDEX idx_audit_tenant_ts ON ONLY public.audit_log USING btree (tenant_id, ts DESC)"

    def test_strips_only_so_the_create_recurses_into_partitions(self) -> None:
        """`ON ONLY` would leave a parent index that never becomes valid."""
        out = rewrite_indexdef(self._SRC, source="audit_log", shadow="audit_log_new", new_name="idx_new")
        assert " ONLY " not in out
        assert out == "CREATE INDEX idx_new ON audit_log_new USING btree (tenant_id, ts DESC)"

    def test_retargets_name_and_table(self) -> None:
        out = rewrite_indexdef(
            self._SRC, source="audit_log", shadow="audit_log_new", new_name="idx_audit_tenant_ts_new"
        )
        assert out.startswith("CREATE INDEX idx_audit_tenant_ts_new ON audit_log_new ")
        assert "idx_audit_tenant_ts " not in out

    def test_preserves_unique(self) -> None:
        src = "CREATE UNIQUE INDEX u_audit ON ONLY public.audit_log USING btree (audit_id, ts)"
        out = rewrite_indexdef(src, source="audit_log", shadow="audit_log_new", new_name="u_audit_new")
        assert out == "CREATE UNIQUE INDEX u_audit_new ON audit_log_new USING btree (audit_id, ts)"

    def test_preserves_a_partial_index_predicate(self) -> None:
        src = "CREATE INDEX p_audit ON ONLY public.audit_log USING btree (ts) WHERE (error_code IS NOT NULL)"
        out = rewrite_indexdef(src, source="audit_log", shadow="audit_log_new", new_name="p_audit_new")
        assert out.endswith("WHERE (error_code IS NOT NULL)")

    def test_handles_a_non_only_definition(self) -> None:
        src = "CREATE INDEX idx_a ON public.audit_log USING btree (ts)"
        out = rewrite_indexdef(src, source="audit_log", shadow="audit_log_new", new_name="idx_a_new")
        assert out == "CREATE INDEX idx_a_new ON audit_log_new USING btree (ts)"

    def test_refuses_an_unparseable_definition(self) -> None:
        with pytest.raises(ValueError, match="could not parse index definition"):
            rewrite_indexdef("SELECT 1", source="audit_log", shadow="audit_log_new", new_name="x")

    def test_refuses_a_definition_for_another_table(self) -> None:
        src = "CREATE INDEX idx_x ON ONLY public.usage_events USING btree (ts)"
        with pytest.raises(ValueError, match="targets"):
            rewrite_indexdef(src, source="audit_log", shadow="audit_log_new", new_name="idx_x_new")


# ---------------------------------------------------------------------------
# Statement builders
# ---------------------------------------------------------------------------


class TestRenameSql:
    def test_audit_log(self) -> None:
        archive, promote = rename_sql("audit_log")
        assert archive == "ALTER TABLE audit_log RENAME TO audit_log_archive"
        assert promote == "ALTER TABLE audit_log_new RENAME TO audit_log"


class TestChildRenameStatements:
    def test_renames_indexes_before_the_table(self) -> None:
        """An index name is schema-unique, so it must be freed before reuse."""
        out = child_rename_statements(
            "audit_log_new_2025_03",
            "audit_log_2025_03",
            ["audit_log_new_2025_03_pkey", "audit_log_new_2025_03_tenant_id_ts_idx"],
        )
        assert out == [
            "ALTER INDEX audit_log_new_2025_03_pkey RENAME TO audit_log_2025_03_pkey",
            "ALTER INDEX audit_log_new_2025_03_tenant_id_ts_idx RENAME TO audit_log_2025_03_tenant_id_ts_idx",
            "ALTER TABLE audit_log_new_2025_03 RENAME TO audit_log_2025_03",
        ]

    def test_leaves_an_index_not_named_after_the_table_alone(self) -> None:
        """Postgres derives child index names from the table; one that isn't was deliberate."""
        out = child_rename_statements("audit_log_new_2025_03", "audit_log_2025_03", ["operator_named_index"])
        assert out == ["ALTER TABLE audit_log_new_2025_03 RENAME TO audit_log_2025_03"]

    def test_handles_a_child_with_no_indexes(self) -> None:
        out = child_rename_statements("audit_log_2025_03", "audit_log_archive_2025_03", [])
        assert out == ["ALTER TABLE audit_log_2025_03 RENAME TO audit_log_archive_2025_03"]


class TestCopySql:
    def test_names_every_column_explicitly_on_both_sides(self) -> None:
        """`SELECT *` would silently depend on the two tables agreeing on order."""
        out = copy_sql(_COLUMNS, source="audit_log", shadow="audit_log_new")
        cols = ", ".join(_COLUMNS)
        assert out == (f"INSERT INTO audit_log_new ({cols}) SELECT {cols} FROM audit_log WHERE ts >= %s AND ts < %s")
        assert "*" not in out

    def test_refuses_an_empty_column_list(self) -> None:
        with pytest.raises(ValueError, match="no columns"):
            copy_sql([], source="audit_log", shadow="audit_log_new")

    def test_refuses_a_non_identifier_column(self) -> None:
        with pytest.raises(ValueError, match="not a plain lowercase SQL identifier"):
            copy_sql(["ts", 'x"; DROP TABLE tenants; --'], source="audit_log", shadow="audit_log_new")


class TestCatchupSql:
    def test_anti_joins_on_the_primary_key_not_a_timestamp_comparison(self) -> None:
        """Two rows can share a ts: `>` would drop one, `>=` would duplicate one."""
        out = catchup_sql(_COLUMNS, source="audit_log", shadow="audit_log_new")
        assert "NOT EXISTS" in out
        assert "d.audit_id = s.audit_id AND d.ts = s.ts" in out

    def test_bounds_the_anti_join_by_ts_so_it_reads_the_tail(self) -> None:
        out = catchup_sql(_COLUMNS, source="audit_log", shadow="audit_log_new")
        assert "s.ts >= %s" in out


# ---------------------------------------------------------------------------
# Mock-DB helpers
# ---------------------------------------------------------------------------


def _conn_with(fetchone_values: list[object]) -> MagicMock:
    """A mock connection whose fetchone() walks *fetchone_values* in order."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.fetchone.side_effect = list(fetchone_values)
    cur.fetchall.return_value = []
    cur.rowcount = 0
    return conn


def _executed(conn: MagicMock) -> list[str]:
    return [str(c.args[0]) for c in conn.cursor.return_value.execute.call_args_list if c.args]


# ---------------------------------------------------------------------------
# Idempotency — archive table already present
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_skips_when_archive_exists(self, caplog: pytest.LogCaptureFixture) -> None:
        """If audit_log_archive exists, _migrate_range_table must exit early."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # _table_exists checks pg_class; return a row (truthy) for the archive
        cur.fetchone.return_value = (1,)

        with caplog.at_level(logging.WARNING):
            _migrate_range_table(conn, "audit_log", datetime.date(2026, 5, 7), dry_run=True)

        assert "cutover already done" in caplog.text
        assert [s for s in _executed(conn) if "RENAME" in s] == []

    def test_run_migration_skips_when_archive_exists(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = (1,)

        with caplog.at_level(logging.WARNING):
            run_migration(conn, dry_run=True)

        assert "cutover already done" in caplog.text


# ---------------------------------------------------------------------------
# Step 1 — shadow reuse
# ---------------------------------------------------------------------------


class TestBuildShadow:
    def test_reuses_an_existing_shadow_whose_columns_match(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = (1,)  # shadow exists
        # _columns() for source then shadow, then _child_partitions() calls
        cur.fetchall.side_effect = [
            [(c,) for c in _COLUMNS],  # source columns
            [(c,) for c in _COLUMNS],  # shadow columns
            [],  # shadow children
            [],  # source children
        ]

        with caplog.at_level(logging.INFO):
            _build_shadow(
                conn,
                source="audit_log",
                shadow="audit_log_new",
                forward_months=0,
                now=datetime.date(2026, 5, 1),
                dry_run=True,
            )

        assert "reusing existing audit_log_new" in caplog.text
        assert not any("CREATE TABLE audit_log_new (LIKE" in s for s in _executed(conn))

    def test_refuses_a_shadow_whose_columns_have_drifted(self) -> None:
        """A copy into a stale shape is the one failure no statement would raise on."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = (1,)  # shadow exists
        cur.fetchall.side_effect = [
            [(c,) for c in _COLUMNS],  # source columns
            [(c,) for c in _COLUMNS[:-1]],  # shadow is missing `ts`
        ]

        with pytest.raises(RuntimeError, match="columns no longer match"):
            _build_shadow(
                conn,
                source="audit_log",
                shadow="audit_log_new",
                forward_months=0,
                now=datetime.date(2026, 5, 1),
                dry_run=True,
            )

    def test_replicates_source_bounds_verbatim(self) -> None:
        """Coverage must never narrow: reuse the source's own bound expression.

        Recomputing month boundaries here would only be equivalent to the
        source's by coincidence; a month the live table accepts inserts for and
        the shadow does not becomes a failed audit write after the rename.
        """
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        bound = "FOR VALUES FROM ('2025-01-01 00:00:00+00') TO ('2025-02-01 00:00:00+00')"
        # _table_exists(shadow) -> absent, then _partition_key(source)
        cur.fetchone.side_effect = [None, ("RANGE (ts)",)]
        cur.fetchall.side_effect = [
            [(c,) for c in _COLUMNS],  # source columns
            [],  # shadow children (none yet)
            [("audit_log_2025_01", bound)],  # source children
        ]

        _build_shadow(
            conn,
            source="audit_log",
            shadow="audit_log_new",
            forward_months=0,
            now=datetime.date(2026, 5, 1),
            dry_run=False,
        )

        executed = _executed(conn)
        assert f"CREATE TABLE audit_log_new_2025_01 PARTITION OF audit_log_new {bound}" in executed
        assert any("(LIKE audit_log INCLUDING DEFAULTS" in s and "PARTITION BY RANGE (ts)" in s for s in executed)

    def test_creates_forward_headroom_beyond_the_source_partitions(self) -> None:
        """A cutover that cannot accept next month's writes is a live hazard."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [None, ("RANGE (ts)",)]
        cur.fetchall.side_effect = [
            [(c,) for c in _COLUMNS],  # source columns
            [],  # shadow children
            [],  # source children (none)
        ]

        _build_shadow(
            conn,
            source="audit_log",
            shadow="audit_log_new",
            forward_months=2,
            now=datetime.date(2026, 5, 15),
            dry_run=False,
        )

        executed = _executed(conn)
        assert any("audit_log_new_2026_05" in s for s in executed)
        assert any("audit_log_new_2026_06" in s for s in executed)
        assert not any("audit_log_new_2026_07" in s for s in executed)

    def test_does_not_recreate_a_month_a_source_partition_already_covered(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        bound = "FOR VALUES FROM ('2026-05-01 00:00:00+00') TO ('2026-06-01 00:00:00+00')"
        cur.fetchone.side_effect = [None, ("RANGE (ts)",)]
        cur.fetchall.side_effect = [
            [(c,) for c in _COLUMNS],
            [],  # shadow children
            [("audit_log_2026_05", bound)],  # source already covers 2026-05
        ]

        _build_shadow(
            conn,
            source="audit_log",
            shadow="audit_log_new",
            forward_months=1,
            now=datetime.date(2026, 5, 15),
            dry_run=False,
        )

        creates = [s for s in _executed(conn) if "audit_log_new_2026_05" in s]
        assert len(creates) == 1, f"2026-05 created more than once: {creates}"


# ---------------------------------------------------------------------------
# Step 2 — resume detection
# ---------------------------------------------------------------------------


class TestResumeDetection:
    def test_chunk_skipped_when_destination_already_populated(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = (42,)  # COUNT(*) > 0 → already copied

        with caplog.at_level(logging.INFO):
            copied = _copy(
                conn,
                source="audit_log",
                shadow="audit_log_new",
                columns=_COLUMNS,
                chunks=[(datetime.date(2026, 4, 1), datetime.date(2026, 5, 1))],
                dry_run=False,
            )

        assert copied == 0
        assert "RESUME" in caplog.text
        assert [s for s in _executed(conn) if "INSERT" in s] == []

    def test_chunk_copied_when_destination_is_empty(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = (0,)  # COUNT(*) = 0 → needs copy
        cur.rowcount = 5

        copied = _copy(
            conn,
            source="audit_log",
            shadow="audit_log_new",
            columns=_COLUMNS,
            chunks=[(datetime.date(2026, 4, 1), datetime.date(2026, 5, 1))],
            dry_run=False,
        )

        assert copied == 5
        assert len([s for s in _executed(conn) if "INSERT" in s]) == 1


# ---------------------------------------------------------------------------
# Step 4 — verification
# ---------------------------------------------------------------------------


class TestVerify:
    def test_passes_when_counts_agree(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # chunk src, chunk dst, total src, total dst
        cur.fetchone.side_effect = [(10,), (10,), (10,), (10,)]

        _verify(
            conn,
            source="audit_log",
            shadow="audit_log_new",
            chunks=[(datetime.date(2026, 4, 1), datetime.date(2026, 5, 1))],
            dry_run=False,
        )

        # It counted both sides of the chunk and both whole tables, and issued
        # nothing that changes state -- verification is strictly a read.
        executed = _executed(conn)
        assert len([s for s in executed if "COUNT(*)" in s]) == 4, executed
        assert not any(v in s.upper() for s in executed for v in ("ALTER", "INSERT", "DROP", "CREATE"))

    def test_refuses_when_a_chunk_is_short(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(10,), (7,)]

        with pytest.raises(RuntimeError, match="row count mismatch"):
            _verify(
                conn,
                source="audit_log",
                shadow="audit_log_new",
                chunks=[(datetime.date(2026, 4, 1), datetime.date(2026, 5, 1))],
                dry_run=False,
            )

    def test_tolerates_a_shadow_behind_its_source(self) -> None:
        """The source is still taking writes; the in-lock catch-up closes that."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        # source total 12, shadow total 10 -- behind, which must not raise
        cur.fetchone.side_effect = [(10,), (10,), (12,), (10,)]

        _verify(
            conn,
            source="audit_log",
            shadow="audit_log_new",
            chunks=[(datetime.date(2026, 4, 1), datetime.date(2026, 5, 1))],
            dry_run=False,
        )

        # It read all four counts and still permitted the cutover to proceed.
        assert len([s for s in _executed(conn) if "COUNT(*)" in s]) == 4

    def test_refuses_a_shadow_ahead_of_its_source(self) -> None:
        """Extra rows cannot be explained by concurrent writes."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(10,), (10,), (10,), (11,)]

        with pytest.raises(RuntimeError, match="cannot be explained by concurrent writes"):
            _verify(
                conn,
                source="audit_log",
                shadow="audit_log_new",
                chunks=[(datetime.date(2026, 4, 1), datetime.date(2026, 5, 1))],
                dry_run=False,
            )


# ---------------------------------------------------------------------------
# Step 5 — the cutover transaction
# ---------------------------------------------------------------------------

_INDEXES = [("idx_audit_tenant_ts", "CREATE INDEX idx_audit_tenant_ts ON ONLY public.audit_log USING btree (ts)")]
# (name, definition, owns_an_index). The primary key owns one, so its name has to
# be suffixed while the shadow is built and handed over at cutover; the foreign
# key owns none, so it is built under its final name and never renamed.
_CONSTRAINTS = [
    ("audit_log_pkey", "PRIMARY KEY (audit_id, ts)", True),
    ("audit_log_tenant_id_fkey", "FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)", False),
]


def _cutover_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": "audit_log",
        "shadow": "audit_log_new",
        "columns": _COLUMNS,
        "indexes": _INDEXES,
        "constraints": _CONSTRAINTS,
        "tail_window": "2 hours",
        "lock_timeout": "3s",
        "lock_retries": 3,
        "retry_wait_s": 0.0,
        "dry_run": False,
    }
    base.update(overrides)
    return base


class TestCutover:
    def test_dry_run_mutates_nothing(self) -> None:
        """Dry-run still reads the catalog to project the child renames.

        What it must not do is issue anything that changes state, so the
        assertion is on mutating verbs rather than on execute() being unused.
        """
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []

        _cutover(conn, **_cutover_kwargs(dry_run=True))  # type: ignore[arg-type]

        mutating = [
            s
            for s in _executed(conn)
            if any(verb in s.upper() for verb in ("CREATE", "ALTER", "INSERT", "DROP", "LOCK", "BEGIN", "COMMIT"))
        ]
        assert mutating == [], mutating

    def test_sets_lock_timeout_before_taking_the_lock(self) -> None:
        """An ACCESS EXCLUSIVE request that queues blocks every reader behind it."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(None,)]  # no tail floor
        cur.rowcount = 0

        _cutover(conn, **_cutover_kwargs())  # type: ignore[arg-type]

        executed = _executed(conn)
        timeout_idx = next(i for i, s in enumerate(executed) if "lock_timeout" in s)
        lock_idx = next(i for i, s in enumerate(executed) if "ACCESS EXCLUSIVE" in s)
        assert timeout_idx < lock_idx

    def test_renames_inside_one_transaction_in_order(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(None,)]
        cur.rowcount = 0

        _cutover(conn, **_cutover_kwargs())  # type: ignore[arg-type]

        executed = _executed(conn)
        begin_idx = next(i for i, s in enumerate(executed) if s == "BEGIN")
        archive_idx = next(i for i, s in enumerate(executed) if "audit_log RENAME TO audit_log_archive" in s)
        promote_idx = next(i for i, s in enumerate(executed) if "audit_log_new RENAME TO audit_log" in s)
        commit_idx = next(i for i, s in enumerate(executed) if s == "COMMIT")
        assert begin_idx < archive_idx < promote_idx < commit_idx

    def test_hands_canonical_index_and_constraint_names_over(self) -> None:
        """Post-cutover shape must match a fresh install, or the next run collides."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(None,)]
        cur.rowcount = 0

        _cutover(conn, **_cutover_kwargs())  # type: ignore[arg-type]

        executed = _executed(conn)
        assert any("RENAME CONSTRAINT audit_log_pkey TO audit_log_pkey_archive" in s for s in executed)
        assert any("RENAME CONSTRAINT audit_log_pkey_new TO audit_log_pkey" in s for s in executed)
        assert any("ALTER INDEX idx_audit_tenant_ts RENAME TO idx_audit_tenant_ts_archive" in s for s in executed)
        assert any("ALTER INDEX idx_audit_tenant_ts_new RENAME TO idx_audit_tenant_ts" in s for s in executed)

    def test_does_not_rename_a_constraint_that_owns_no_index(self) -> None:
        """A foreign key was built under its final name; renaming it would fail."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(None,)]
        cur.rowcount = 0

        _cutover(conn, **_cutover_kwargs())  # type: ignore[arg-type]

        assert not any("audit_log_tenant_id_fkey" in s for s in _executed(conn))

    def test_renames_child_partitions_so_the_archival_monitor_still_matches(self) -> None:
        """`audit_partitions_eligible_for_archival` anchors on audit_log_YYYY_MM.

        Promoted children left as `audit_log_new_2025_03` are silently skipped by
        that predicate, which parks the archival gauge at 0 for good.
        """
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.side_effect = [(None,)]
        cur.rowcount = 0
        # _children_with_indexes: source children, then shadow children
        cur.fetchall.side_effect = [
            [("audit_log_2025_03", "audit_log_2025_03_pkey")],
            [("audit_log_new_2025_03", "audit_log_new_2025_03_pkey")],
        ]

        _cutover(conn, **_cutover_kwargs())  # type: ignore[arg-type]

        executed = _executed(conn)
        # outgoing child steps aside, incoming child takes the canonical name
        assert "ALTER TABLE audit_log_2025_03 RENAME TO audit_log_archive_2025_03" in executed
        assert "ALTER TABLE audit_log_new_2025_03 RENAME TO audit_log_2025_03" in executed
        # and the indexes Postgres named after those tables follow
        assert "ALTER INDEX audit_log_2025_03_pkey RENAME TO audit_log_archive_2025_03_pkey" in executed
        assert "ALTER INDEX audit_log_new_2025_03_pkey RENAME TO audit_log_2025_03_pkey" in executed
        # ordering: the outgoing name must be freed before the incoming one takes it
        assert executed.index("ALTER TABLE audit_log_2025_03 RENAME TO audit_log_archive_2025_03") < executed.index(
            "ALTER TABLE audit_log_new_2025_03 RENAME TO audit_log_2025_03"
        )

    def test_retries_then_fails_when_the_lock_never_comes(self, caplog: pytest.LogCaptureFixture) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur

        def execute(sql: str, params: object = None) -> None:
            if "ACCESS EXCLUSIVE" in str(sql):
                raise RuntimeError("canceling statement due to lock timeout")

        cur.execute.side_effect = execute

        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError, match="could not acquire ACCESS EXCLUSIVE"):
                _cutover(conn, **_cutover_kwargs(lock_retries=3))  # type: ignore[arg-type]

        assert _executed(conn).count("ROLLBACK") == 3
        assert "lock attempt 1/3" in caplog.text
        assert not any("RENAME TO" in s for s in _executed(conn))

    def test_rolls_back_when_the_tail_count_disagrees(self, caplog: pytest.LogCaptureFixture) -> None:
        """A mismatch must abort the rename rather than lose the rows."""
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        floor = datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC)
        # tail floor, then source tail count, then shadow tail count
        cur.fetchone.side_effect = [(floor,), (9,), (7,)]
        cur.rowcount = 2

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError, match="tail count mismatch"):
                _cutover(conn, **_cutover_kwargs())  # type: ignore[arg-type]

        executed = _executed(conn)
        assert "ROLLBACK" in executed
        assert not any("RENAME TO" in s for s in executed)
        assert "still live" in caplog.text

    def test_copies_the_tail_before_renaming(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        floor = datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC)
        cur.fetchone.side_effect = [(floor,), (9,), (9,)]
        cur.rowcount = 3

        _cutover(conn, **_cutover_kwargs())  # type: ignore[arg-type]

        executed = _executed(conn)
        insert_idx = next(i for i, s in enumerate(executed) if "INSERT INTO audit_log_new" in s)
        promote_idx = next(i for i, s in enumerate(executed) if "audit_log_new RENAME TO audit_log" in s)
        assert insert_idx < promote_idx
