"""Zero-downtime cutover: copy existing rows into partitioned _new tables and rename.

Covers `audit_log`. `embeddings` is created partitioned by its migration
and therefore has no cutover -- there is one physical shape, which is also the shape the
tests exercise.

Four-step procedure
===================

Step 1 — Discover extents
    SELECT MIN(ts), MAX(ts) FROM audit_log.

Step 2 — Create historical partitions
    For audit_log_new, generate monthly child
    partitions from date_trunc('month', oldest_ts) through now + 12 months
    (forward months were already created by the migration; skip months that
    already exist to make this step idempotent).

Step 3 — Month-chunked copy (resumable)
    INSERT INTO audit_log_new SELECT * FROM audit_log WHERE ts >= :lo AND ts < :hi.
    Before each chunk check SELECT COUNT(*) FROM audit_log_new WHERE ts >= :lo AND ts < :hi;
    if count > 0 the chunk was already copied — skip it.

Step 4 — Transactional rename
    BEGIN;
        ALTER TABLE audit_log  RENAME TO audit_log_archive;
        ALTER TABLE audit_log_new RENAME TO audit_log;
    COMMIT;

Idempotency
    If audit_log_archive already exists, the cutover is complete; exit with
    a warning ("cutover already done").

Downgrade note
    No automatic downgrade. To restore the original tables:
        ALTER TABLE audit_log RENAME TO audit_log_new;
        ALTER TABLE audit_log_archive RENAME TO audit_log;
        -- then DROP TABLE audit_log_new CASCADE;

Usage::

    python scripts/partition_migrate.py --database-url postgresql+psycopg2://...

The script uses synchronous psycopg2 to allow explicit transaction control
(BEGIN/COMMIT without SQLAlchemy ORM overhead).  Pass --dry-run to trace
SQL without executing.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registry.config import get_settings  # noqa: E402

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure helpers (no DB) — tested in isolation
# ---------------------------------------------------------------------------


def month_range(
    start: datetime.date,
    end: datetime.date,
) -> Iterator[tuple[datetime.date, datetime.date]]:
    """Yield (from_date, to_date) for every calendar month in [start, end).

    ``start`` is truncated to the first of its month.  Iteration continues
    until the month beginning is >= ``end``.
    """
    year, month = start.year, start.month
    while True:
        from_d = datetime.date(year, month, 1)
        if from_d >= end:
            break
        if month == 12:
            to_d = datetime.date(year + 1, 1, 1)
        else:
            to_d = datetime.date(year, month + 1, 1)
        yield from_d, to_d
        year, month = to_d.year, to_d.month


def partition_name(table: str, from_d: datetime.date) -> str:
    """Return the child partition name for *table* in the month of *from_d*."""
    return f"{table}_{from_d.year:04d}_{from_d.month:02d}"


def rename_sql(table: str) -> tuple[str, str]:
    """Return (archive_rename_sql, promote_rename_sql) for a table cutover."""
    archive = f"ALTER TABLE {table} RENAME TO {table}_archive"
    promote = f"ALTER TABLE {table}_new RENAME TO {table}"
    return archive, promote


# ---------------------------------------------------------------------------
# DB interaction layer (easily mockable)
# ---------------------------------------------------------------------------


def _existing_partitions(conn: object, parent: str) -> set[str]:
    """Return the set of child partition names already attached to *parent*."""
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        """
        SELECT c.relname
        FROM   pg_inherits i
        JOIN   pg_class c ON c.oid = i.inhrelid
        JOIN   pg_class p ON p.oid = i.inhparent
        WHERE  p.relname = %s
        """,
        (parent,),
    )
    return {row[0] for row in cur.fetchall()}


def _table_exists(conn: object, table: str) -> bool:
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = %s",
        (table,),
    )
    return cur.fetchone() is not None


def _discover_extent(conn: object, table: str) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(f"SELECT MIN(ts), MAX(ts) FROM {table}")  # noqa: S608 - table is always the hardcoded "audit_log"/"audit_log_new" literal from main(), never CLI/operator input
    row = cur.fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _ensure_partition(
    conn: object,
    parent: str,
    child: str,
    from_iso: str,
    to_iso: str,
    existing: set[str],
    dry_run: bool,
) -> None:
    if child in existing:
        _log.debug("partition %s already exists — skipping", child)
        return
    sql = f"CREATE TABLE {child} " f"PARTITION OF {parent} " f"FOR VALUES FROM ('{from_iso}') TO ('{to_iso}')"
    _log.info("CREATE PARTITION %s", child)
    if not dry_run:
        conn.cursor().execute(sql)  # type: ignore[attr-defined]
        conn.commit()  # type: ignore[attr-defined]
    existing.add(child)


def _chunk_row_count(conn: object, table: str, lo: datetime.date, hi: datetime.date) -> int:
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        f"SELECT COUNT(*) FROM {table} WHERE ts >= %s AND ts < %s",  # noqa: S608 - table is always the hardcoded "audit_log"/"audit_log_new" literal from main(), never CLI/operator input; ts bounds are bound via %s
        (lo, hi),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _copy_chunk(
    conn: object,
    src: str,
    dst: str,
    lo: datetime.date,
    hi: datetime.date,
    dry_run: bool,
) -> int:
    """Copy one month chunk from src to dst. Returns row count inserted."""
    # Resume check: if any rows already exist for this range, skip.
    existing = _chunk_row_count(conn, dst, lo, hi)
    if existing > 0:
        _log.info("RESUME: %s [%s, %s) already has %d rows — skipping", dst, lo, hi, existing)
        return 0

    sql = f"INSERT INTO {dst} " f"SELECT * FROM {src} WHERE ts >= %s AND ts < %s"  # noqa: S608 - src/dst are always the hardcoded "audit_log"/"audit_log_new" literals from main(), never CLI/operator input; ts bounds are bound via %s
    _log.info("COPY %s → %s [%s, %s)", src, dst, lo, hi)
    if dry_run:
        return 0
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(sql, (lo, hi))
    count: int = int(cur.rowcount)
    conn.commit()  # type: ignore[attr-defined]
    return count


def _transactional_rename(conn: object, table: str, dry_run: bool) -> None:
    """Atomically rename table → table_archive and table_new → table."""
    archive_sql, promote_sql = rename_sql(table)
    _log.info("RENAME %s → %s_archive, %s_new → %s", table, table, table, table)
    if dry_run:
        _log.info("[dry-run] %s", archive_sql)
        _log.info("[dry-run] %s", promote_sql)
        return
    cur = conn.cursor()  # type: ignore[attr-defined]
    # These two renames must be in the same transaction.
    cur.execute("BEGIN")
    cur.execute(archive_sql)
    cur.execute(promote_sql)
    cur.execute("COMMIT")


def _hnsw_index_name(partition: int) -> str:
    return f"idx_embed_new_hnsw_p{partition}"


# ---------------------------------------------------------------------------
# High-level migration logic
# ---------------------------------------------------------------------------


def _migrate_range_table(
    conn: object,
    table: str,
    now: datetime.date,
    dry_run: bool,
) -> None:
    """Steps 1–4 for a RANGE-partitioned table (audit_log)."""
    new_table = f"{table}_new"
    archive_table = f"{table}_archive"

    # Step 5 — idempotency check
    if _table_exists(conn, archive_table):
        _log.warning("cutover already done: %s exists — skipping %s", archive_table, table)
        return

    # Step 1 — discover extent
    oldest_ts, _newest_ts = _discover_extent(conn, table)
    if oldest_ts is None:
        _log.info("%s is empty — no historical partitions needed", table)
        oldest_dt: datetime.date = now
    else:
        if hasattr(oldest_ts, "date"):
            oldest_dt = oldest_ts.date()
        else:
            oldest_dt = oldest_ts

    # Step 2 — create historical + forward partitions
    range_end = datetime.date(now.year + (1 if now.month + 11 > 12 else 0), ((now.month + 11) % 12) + 1, 1)
    # Simpler: iterate 12 months forward from start of current month.
    forward_end_year = now.year + (now.month - 1 + 12) // 12
    forward_end_month = (now.month - 1 + 12) % 12 + 1
    range_end = datetime.date(forward_end_year, forward_end_month, 1)

    hist_start = datetime.date(oldest_dt.year, oldest_dt.month, 1)
    existing = _existing_partitions(conn, new_table)

    for from_d, to_d in month_range(hist_start, range_end):
        child = partition_name(new_table, from_d)
        _ensure_partition(conn, new_table, child, from_d.isoformat(), to_d.isoformat(), existing, dry_run)

    # Step 3 — month-chunked copy
    total_copied = 0
    if oldest_ts is not None:
        for from_d, to_d in month_range(hist_start, range_end):
            total_copied += _copy_chunk(conn, table, new_table, from_d, to_d, dry_run)
    _log.info("copied %d rows from %s → %s", total_copied, table, new_table)

    # Step 4 — transactional rename
    _transactional_rename(conn, table, dry_run)


def run_migration(conn: object, dry_run: bool = False) -> None:
    """Execute the full partition cutover.

    Operates on: audit_log.
    """
    now = datetime.date.today()

    _migrate_range_table(conn, "audit_log", now, dry_run)

    # `embeddings` is absent on purpose. Its migration creates it already partitioned, so
    # there is nothing to copy and nothing to rename -- and one physical shape means the
    # shape the tests exercise is the shape that runs.

    _log.info("partition migration complete")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Zero-downtime cutover to partitioned tables.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="psycopg2 database URL (overrides DATABASE_URL env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print SQL without executing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)

    # Settings is the single env-var reader.
    if not args.database_url:
        args.database_url = get_settings().database_url

    try:
        import psycopg2  # type: ignore[import-untyped]  # noqa: PLC0415 - deferred so a missing install surfaces this friendly message, not a raw traceback
    except ModuleNotFoundError:
        sys.exit("ERROR: psycopg2 not installed — pip install psycopg2-binary")

    # SQLAlchemy-style URLs ("postgresql+asyncpg://...", "postgresql+psycopg2://...")
    # are accepted everywhere else in the codebase but psycopg2.connect rejects
    # the "+driver" suffix. Strip it so DATABASE_URL works without modification.
    dsn = args.database_url
    if dsn.startswith("postgresql+"):
        dsn = "postgresql://" + dsn.split("://", 1)[1]

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        run_migration(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
