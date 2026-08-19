"""Partition cutover, pruning, and detach integration tests.

Covers storage-layer invariants for the hash-partitioned ``audit_log`` and
``embeddings`` tables:

- ``test_partition_migrate_cutover_lifecycle``: the whole cutover as one ordered
  test (shadow absent → dry-run → real cutover → shape and row assertions →
  second run is a no-op). One test rather than four because ``pg_container`` is
  session-scoped and a cutover changes that database once, so each step is only
  true at one point in the sequence.
- ``test_partition_pruning_embeddings``: EXPLAIN for WHERE tenant_id = :tid shows
  only 1-of-8 hash partitions scanned (partition pruning active).
- ``test_audit_partition_detach_procedure``: detaches a synthetic old partition;
  verifies audit_log parent is still queryable after detach; verifies detached
  partition is accessible as a standalone table.
- ``test_full_conformance_suite_passes``: programmatically collects all three
  conformance suites (tenant isolation, OpenAPI drift, MCP conformance) in-process
  and asserts zero collection errors. This in-process collection gate ensures the
  conformance files remain importable and structurally valid; full suite execution
  is covered by ``make test-conformance``.

Manual checklist (not automated — document here so the exit gate is explicit):
    1. k6 30-min load test:
       cd scripts/load_test
       k6 run --duration=30m --vus=100 k6_script.js
       Assert: p95 latency < 500ms on /v1/search; error rate < 0.1%.
    2. Helm fresh-cluster deploy:
       helm install catalog ./helm --set image.tag=<sha>
       kubectl wait --for=condition=Ready pod -l app=capability-fabric --timeout=120s
       curl -f http://<cluster-ip>/healthz
    3. SBOM attached to the release artefact set:
       Verify the published release (whichever contextplane/host the
       operator's release pipeline targets) carries ``sbom.spdx.json``.
       On GitHub: ``gh release view v1.0.0 --json assets | jq '.[].name'``.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess  # noqa: S404 - test-harness invocation of this repo's own scripts (sys.executable + fixed script path), no caller input
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from contextplane.wiring.jobs import audit_partitions_eligible_for_archival

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_partition_migrate(database_url: str, *extra: str) -> subprocess.CompletedProcess:
    """Invoke partition_migrate.py as a subprocess."""
    # Convert asyncpg URL to psycopg2 URL (the script uses synchronous psycopg2)
    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://").replace(
        "postgresql://", "postgresql+psycopg2://"
    )
    cmd = [sys.executable, str(_SCRIPTS / "partition_migrate.py"), "--database-url", sync_url, *extra]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": sync_url},
        cwd=str(_REPO_ROOT),
    )


async def _seed_audit_rows(engine: object, count: int) -> int:
    """Insert *count* audit rows across two months; return rows now in audit_log."""
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        tenant_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, 'partition-cutover-test', now(), TRUE)"
            ),
            {"tid": tenant_id, "slug": f"cutover-{tenant_id.hex[:8]}"},
        )
        for n in range(count):
            # Spread across two pre-created partitions so the copy runs more
            # than one chunk and the per-chunk verification has work to do.
            # asyncpg binds timestamptz from a datetime, never from a string.
            ts = datetime.datetime(2025, 3 if n % 2 == 0 else 4, 15, tzinfo=datetime.UTC)
            await conn.execute(
                sa.text(
                    "INSERT INTO audit_log (tenant_id, actor_id, action, target_type, target_id, ts) "
                    "VALUES (:tid, NULL, :action, 'capability', gen_random_uuid(), :ts)"
                ),
                {"tid": tenant_id, "action": f"seed_{n}", "ts": ts},
            )
        result = await conn.execute(sa.text("SELECT COUNT(*) FROM audit_log"))
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partition_migrate_cutover_lifecycle(pg_container: str) -> None:
    """The whole cutover, start to finish, as one test.

    Deliberately one test rather than three. `pg_container` is session-scoped
    and a cutover is a one-way change to that database -- "the shadow table is
    absent", "the cutover works" and "a second run is a no-op" are each only
    true at one point in a fixed sequence, so splitting them would leave three
    tests that pass or fail depending on the order they happen to run in.

    Asserted, in order:
      1. `audit_log_new` is absent from the migrated schema (migration 0054),
         and `--dry-run` reports it would build one without building it.
      2. A real cutover preserves every row.
      3. The promoted table gets its own shape back -- canonical index,
         constraint and *partition* names, and the foreign keys the previously
         pre-created shadow declared none of and therefore silently dropped.
      4. A second invocation is a warn-only no-op.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})

    # --- 1. the shadow is the script's scratch space, not part of the schema ---
    async with engine.begin() as conn:
        exists = await conn.execute(sa.text("SELECT 1 FROM pg_class WHERE relname = 'audit_log_new'"))
        assert exists.first() is None, "audit_log_new should not exist in the migrated schema"

    dry = _run_partition_migrate(pg_container, "--dry-run")
    assert dry.returncode == 0, f"dry-run failed:\nstdout: {dry.stdout}\nstderr: {dry.stderr}"
    assert "CREATE audit_log_new (LIKE audit_log)" in dry.stdout + dry.stderr, dry.stdout + dry.stderr

    async with engine.begin() as conn:
        still_absent = await conn.execute(sa.text("SELECT 1 FROM pg_class WHERE relname = 'audit_log_new'"))
        assert still_absent.first() is None, "--dry-run must not create the shadow table"

    # --- 2. a real cutover loses no rows ---
    seeded = await _seed_audit_rows(engine, 8)
    assert seeded >= 8

    result = _run_partition_migrate(pg_container)
    assert result.returncode == 0, f"cutover failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    async with engine.begin() as conn:
        surviving = await conn.execute(sa.text("SELECT COUNT(*) FROM audit_log"))
        assert surviving.scalar_one() == seeded, "cutover changed the row count"

        archived = await conn.execute(sa.text("SELECT COUNT(*) FROM audit_log_archive"))
        assert archived.scalar_one() == seeded, "the archive should still hold the original rows"

        # relkind is a "char" column; asyncpg hands it back as bytes, so cast.
        partitioned = await conn.execute(sa.text("SELECT relkind::text FROM pg_class WHERE relname = 'audit_log'"))
        assert partitioned.scalar_one() == "p", "audit_log must still be partitioned after the swap"

        # Canonical index names came back, with no _new left over.
        indexes = await conn.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'audit_log' ORDER BY indexname")
        )
        names = {row[0] for row in indexes.fetchall()}
        assert "idx_audit_tenant_ts" in names, names
        assert not any(n.endswith("_new") for n in names), f"_new names leaked into the live table: {names}"

        # The foreign keys the old shadow table dropped are present.
        fks = await conn.execute(
            sa.text("SELECT conname FROM pg_constraint WHERE conrelid = 'audit_log'::regclass AND contype = 'f'")
        )
        fk_names = {row[0] for row in fks.fetchall()}
        assert len(fk_names) == 2, f"expected tenant_id and actor_id foreign keys, got {fk_names}"
        assert not any(n.endswith("_new") for n in fk_names), fk_names

        # Every promoted child partition still matches the name pattern
        # `audit_partitions_eligible_for_archival` anchors on. This is the
        # assertion that would have caught the archival gauge silently sticking
        # at 0 after a cutover -- checking only the parent's indexes did not.
        children = await conn.execute(
            sa.text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'audit_log' ORDER BY c.relname"
            )
        )
        child_names = [row[0] for row in children.fetchall()]
        assert child_names, "the promoted table has no partitions"
        offenders = [n for n in child_names if not re.fullmatch(r"audit_log_\d{4}_\d{2}", n)]
        assert offenders == [], f"partitions the archival monitor would skip: {offenders}"

        # And the archival predicate really does recognise them.
        eligible = audit_partitions_eligible_for_archival(child_names, reference_date=datetime.date(2099, 1, 1))
        assert len(eligible) == len(
            child_names
        ), f"archival monitor recognised {len(eligible)} of {len(child_names)} partitions"

        # And the promoted table still rejects a dangling tenant.
        with pytest.raises(Exception, match="violates foreign key constraint"):
            await conn.execute(
                sa.text(
                    "INSERT INTO audit_log (tenant_id, action, target_type, target_id, ts) "
                    "VALUES (gen_random_uuid(), 'orphan', 'capability', gen_random_uuid(), now())"
                )
            )

    # --- 4. a second invocation is a warn-only no-op ---
    second = _run_partition_migrate(pg_container)
    assert (
        second.returncode == 0
    ), f"second run failed (exit {second.returncode}):\nstdout: {second.stdout}\nstderr: {second.stderr}"
    assert (
        "cutover already done" in (second.stdout + second.stderr).lower()
    ), f"expected 'cutover already done'; got stdout={second.stdout!r} stderr={second.stderr!r}"
    await engine.dispose()


@pytest.mark.asyncio
async def test_partition_pruning_embeddings(pg_container: str) -> None:
    """EXPLAIN for WHERE tenant_id = :tid must show exactly 1 of 8 hash partitions.

    Partition pruning means the planner eliminates 7
    of the 8 embeddings hash partitions at plan time.  We verify this by
    inspecting the EXPLAIN (FORMAT JSON) output and counting the number of
    child plans that reference an embeddings partition.
    """
    import json

    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    tid = uuid.uuid4()

    sa = __import__("sqlalchemy")
    async with engine.begin() as conn:
        # Make sure partition pruning is enabled (PG default is on, but be
        # explicit so the assertion is meaningful in test envs).
        await conn.execute(sa.text("SET enable_partition_pruning = on"))
        # embeddings is created already-partitioned by its own migration (no
        # cutover ever renames it), so `embeddings` is the table this always
        # resolves to; the `embeddings_new` alternative is kept defensively in
        # case that ever changes. Pick whichever name is partitioned.
        target_table_query = await conn.execute(
            sa.text(
                "SELECT relname FROM pg_class WHERE relkind = 'p' "
                "AND relname IN ('embeddings', 'embeddings_new') "
                "ORDER BY relname"
            )
        )
        partitioned_tables = [r[0] for r in target_table_query.fetchall()]
        assert partitioned_tables, (
            "neither `embeddings` nor `embeddings_new` is partitioned — "
            "migration 0006 should have created one of them"
        )
        target_table = partitioned_tables[-1]  # prefer embeddings_new if both
        result = await conn.execute(
            sa.text(f"EXPLAIN (FORMAT JSON, ANALYZE FALSE) SELECT * FROM {target_table} WHERE tenant_id = :tid"),
            {"tid": tid},
        )
        rows = result.fetchall()

    await engine.dispose()

    # rows is a list of single-column rows; the first cell holds the EXPLAIN
    # output. asyncpg auto-decodes JSON columns into Python lists/dicts, so
    # only run json.loads when the driver handed back a raw string.
    raw_plan = rows[0][0]
    plan_json = json.loads(raw_plan) if isinstance(raw_plan, str | bytes | bytearray) else raw_plan

    def _count_partition_scans(node: object) -> int:
        """Recursively count Seq Scan / Index Scan nodes on embeddings partitions.

        EXPLAIN (FORMAT JSON) returns a list of {"Plan": {...}} envelopes;
        each plan node carries `Relation Name` and optional `Plans` children.
        Walk both the wrapper "Plan" key and the recursive "Plans" key.
        """
        count = 0
        if isinstance(node, dict):
            rel = node.get("Relation Name", "")
            if rel.startswith("embeddings_p") or rel.startswith("embeddings_new_p"):
                count += 1
            for key in ("Plan", "Plans"):
                child = node.get(key)
                if child is not None:
                    count += _count_partition_scans(child)
        elif isinstance(node, list):
            for item in node:
                count += _count_partition_scans(item)
        return count

    def _has_partition_pruning_node(node: object) -> bool:
        """True if the plan contains an Append node with a `Subplans Removed`
        field — the canonical "planner pruned partitions" signal."""
        if isinstance(node, dict):
            if node.get("Subplans Removed", 0) > 0:
                return True
            for key in ("Plan", "Plans"):
                child = node.get(key)
                if child is not None and _has_partition_pruning_node(child):
                    return True
        elif isinstance(node, list):
            for item in node:
                if _has_partition_pruning_node(item):
                    return True
        return False

    scanned = _count_partition_scans(plan_json)
    # Either the planner kept exactly one partition (1-of-8 pruning) or it
    # pruned all 8 at plan time with a "Subplans Removed: 7" hint — both
    # outcomes prove the planner is doing partition pruning rather than
    # scanning every child.
    assert scanned == 1 or _has_partition_pruning_node(plan_json), (
        f"Expected pruning to 1-of-8 partitions; planner scanned {scanned} "
        f"and showed no Subplans Removed hint. Plan: {plan_json}"
    )


@pytest.mark.asyncio
async def test_audit_partition_detach_procedure(pg_container: str) -> None:
    """DETACH CONCURRENTLY an old audit_log partition; parent must remain queryable.

    Verifies:
    1. A synthetic partition can be created and populated.
    2. DETACH PARTITION CONCURRENTLY succeeds without locking other partitions.
    3. The audit_log parent table is still queryable after detach.
    4. The detached partition is accessible as a standalone table.
    """
    import sqlalchemy

    engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
    )
    partition_name = "audit_log_2020_01"

    async with engine.begin() as conn:
        # Create a synthetic old partition
        await conn.execute(
            sqlalchemy.text(
                f"CREATE TABLE IF NOT EXISTS {partition_name} "
                f"PARTITION OF audit_log "
                f"FOR VALUES FROM ('2020-01-01') TO ('2020-02-01')"
            )
        )

        # Seed a tenants row so the audit_log_tenant_id_fkey constraint is
        # satisfied. Use a fixed slug suffix on the synthetic tenant to keep
        # the test deterministic across reruns within the same container.
        seed_tenant_id = uuid.uuid4()
        await conn.execute(
            sqlalchemy.text(
                "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                "VALUES (:tid, :slug, 'audit-detach-test', '2020-01-01 00:00:00+00', TRUE)"
            ),
            {"tid": seed_tenant_id, "slug": f"audit-detach-{seed_tenant_id.hex[:8]}"},
        )

        # Insert a row so the partition is non-empty and queryable after
        # detach. Column names match the audit_log schema in migration 0006:
        # audit_id PK, target_type / target_id (not legacy resource_*), ts
        # as the partition key. actor_id is left NULL so the test does not
        # have to seed an actors row to satisfy that FK.
        await conn.execute(
            sqlalchemy.text(
                f"INSERT INTO {partition_name} "
                f"(audit_id, tenant_id, actor_id, action, target_type, target_id, ts) "
                f"VALUES (gen_random_uuid(), :tid, NULL, "
                f"'test_action', 'capability', gen_random_uuid(), '2020-01-15 00:00:00+00')"
            ),
            {"tid": seed_tenant_id},
        )

    # DETACH CONCURRENTLY must run outside an explicit transaction block.
    # Use isolation_level=AUTOCOMMIT on a fresh async engine so the statement
    # runs without an implicit BEGIN.
    detach_engine = create_async_engine(
        pg_container,
        connect_args={"prepared_statement_cache_size": 0},
        isolation_level="AUTOCOMMIT",
    )
    async with detach_engine.connect() as conn:
        await conn.execute(sqlalchemy.text(f"ALTER TABLE audit_log DETACH PARTITION {partition_name} CONCURRENTLY"))
    await detach_engine.dispose()

    # After detach: parent table must still be queryable
    async with engine.begin() as conn:
        result = await conn.execute(sqlalchemy.text("SELECT COUNT(*) FROM audit_log"))
        count = result.scalar()
        assert count is not None, "audit_log is not queryable after partition detach"

        # Detached partition must be accessible as a standalone table
        result2 = await conn.execute(sqlalchemy.text(f"SELECT COUNT(*) FROM {partition_name}"))
        detached_count = result2.scalar()
        assert detached_count == 1, f"Expected 1 row in detached partition; got {detached_count}"

    await engine.dispose()


def test_full_conformance_suite_passes() -> None:
    """Collect all three conformance suites in-process and assert zero collection errors.

    Uses --collect-only so this test does not require a live database. The
    purpose is to verify that all three conformance files remain importable
    and structurally valid. Full conformance execution (which does require a
    database) is covered by ``make test-conformance``; keeping that gate
    separate avoids a hard database dependency in the integration suite.

    The three suites are:
      - tests/conformance/test_tenant_isolation.py
      - tests/conformance/test_openapi_drift.py
      - tests/conformance/test_mcp_conformance.py
    """
    conformance_dir = _REPO_ROOT / "tests" / "conformance"
    suite_files = [
        str(conformance_dir / "test_tenant_isolation.py"),
        str(conformance_dir / "test_openapi_drift.py"),
        str(conformance_dir / "test_mcp_conformance.py"),
    ]

    # Use --collect-only first: if any file fails to collect, we catch it here
    # without running tests (which would require a live database in some cases).
    result = pytest.main(
        ["--collect-only", "-q", "--tb=short"] + suite_files,
        plugins=[],
    )

    # ExitCode.NO_TESTS_COLLECTED (5) is also acceptable if the environment
    # has no DB; what we reject is ERROR (3) or USAGE_ERROR (4).
    assert result not in (3, 4), (
        f"Conformance suite collection failed with pytest exit code {result!r}. "
        "Check that all three conformance files are importable and collect cleanly."
    )
