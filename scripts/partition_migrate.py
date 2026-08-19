"""Zero-downtime repartitioning cutover for `audit_log`, on a live system.

Covers `audit_log`. `embeddings` is created already-partitioned by its migration
and therefore has no cutover -- there is one physical shape, which is also the
shape the tests exercise.

Why the shadow table is built here and not by a migration
========================================================

An earlier design pre-created the shadow (`audit_log_new`) in the baseline
migration, so every deployment carried an empty 25-object shadow table forever
whether or not it ever cut over, and the DDL was a hand-maintained copy of
`audit_log`'s that nothing forced to stay in step. Migration
`0054_drop_precreated_audit_shadow` removes it. This script now derives the
shadow from the live table at run time -- columns, partition bounds, indexes and
foreign keys all read out of `pg_catalog` -- so it cannot drift from whatever
shape `audit_log` actually has when an operator runs it.

Procedure
=========

Step 1 — Build the shadow
    `CREATE TABLE audit_log_new (LIKE audit_log ...) PARTITION BY <source key>`,
    then one child per source child, reusing the source's own
    `pg_get_expr(relpartbound)` verbatim. Copying the bound expression rather
    than recomputing month boundaries is what guarantees the new table's
    coverage is never narrower than the old one's: a month the source could
    accept an insert for and the shadow could not would turn into a failed
    audit write the first time a row landed there. `--forward-months` adds
    headroom beyond that.

Step 2 — Month-chunked copy (resumable)
    One `INSERT INTO ... SELECT` per month, committed per chunk, skipped when
    the destination range is already non-empty. The column list is read from
    the catalog and written out explicitly -- `SELECT *` silently depends on
    two tables agreeing about column *order*, which `LIKE` happens to give us
    today and no constraint enforces tomorrow.

Step 3 — Indexes and constraints, after the load
    The primary key, the secondary indexes and the foreign keys are added once
    the rows are in, which is both faster and less bloating than maintaining
    them per row during the copy. All three take heavy locks and the FK add
    scans the whole table -- all free here, because nothing reads the shadow
    yet. This is also how the foreign keys survive the cutover: the previous
    shadow table declared none, so cutting over silently and permanently
    dropped `audit_log`'s `tenant_id`/`actor_id` referential integrity.

Step 4 — Verify outside the lock
    Per-chunk and whole-table counts must agree before anything is renamed.

Step 5 — Cutover (one short transaction)
    `lock_timeout` is set and `ACCESS EXCLUSIVE` is taken explicitly, so a
    cutover that cannot get the lock promptly fails and retries with backoff
    instead of parking a lock request that every subsequent reader queues
    behind -- the usual way a "zero-downtime" rename takes an application down.
    Holding that lock, the script copies the tail written during steps 2-4,
    re-verifies the tail count, hands the canonical names over from old to new
    -- parent constraints and indexes, then every child partition and the
    indexes Postgres named after it -- and renames both tables. Any mismatch
    rolls the whole transaction back: no cutover, no lost rows, safe to re-run.

    Renaming the children is not cosmetic. `audit_partitions_eligible_for_archival`
    matches `^audit_log_(\\d{4})_(\\d{2})$` and skips whatever does not, so
    partitions promoted as `audit_log_new_2025_03` would hold the archival gauge
    at 0 for good: the alert saying "partitions are aging out" would never fire
    again, and nothing would look broken.

Step 6 — Report
    `audit_log_archive` is left in place. Dropping it is an operator decision,
    made after their own verification, never this script's.

Idempotency and resumability
    An existing `audit_log_archive` means the cutover already happened; the
    script warns and exits 0. A partially-built shadow from an interrupted run
    is reused after its column list is checked against the source.

Downgrade
    No automatic downgrade. To restore the original tables::

        ALTER TABLE audit_log RENAME TO audit_log_new;
        ALTER TABLE audit_log_archive RENAME TO audit_log;
        -- then, once satisfied: DROP TABLE audit_log_new CASCADE;

    The index and constraint names follow the tables; see `--help` output for
    the exact statement list a given run would issue (`--dry-run`).

Usage::

    python scripts/partition_migrate.py --database-url postgresql+psycopg2://...
    python scripts/partition_migrate.py --dry-run

The script uses synchronous psycopg2 to allow explicit transaction and lock
control (BEGIN/LOCK/COMMIT without SQLAlchemy ORM overhead).
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# This script's own directory, so the two sibling modules it was split into
# import as the top-level modules every file in scripts/ already is. Making
# scripts/ a package instead would change how the whole directory resolves
# for every other entry point, mypy and pre-commit included.
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from contextplane.config import get_settings  # noqa: E402

_log = logging.getLogger(__name__)

# The one table this script cuts over. A module constant rather than a CLI
# argument on purpose: every identifier below is either this literal or a name
# read back out of pg_catalog, which is what makes the interpolated DDL safe.
_SOURCE_TABLE = "audit_log"

# The pure name/statement layer. Re-exported rather than referenced through the
# module so the phased logic below reads the same as before the split, and so
# `partition_migrate.<helper>` stays a valid entry point for the unit tests.
from partition_migrate_sql import (  # noqa: E402
    _BUILD_SUFFIX,
    archive_table,
    catchup_sql,
    child_rename_statements,
    copy_sql,
    ident,
    month_range,
    partition_name,
    rename_sql,
    rewrite_indexdef,
    shadow_child_name,
    shadow_table,
)

__all__ = [
    "archive_table",
    "catchup_sql",
    "child_rename_statements",
    "copy_sql",
    "ident",
    "month_range",
    "partition_name",
    "rename_sql",
    "rewrite_indexdef",
    "run_migration",
    "shadow_child_name",
    "shadow_table",
]


# The catalog-introspection layer: what shape the live table actually has.
from partition_migrate_catalog import (  # noqa: E402
    _child_partitions,
    _children_with_indexes,
    _columns,
    _constraints,
    _count_all,
    _count_in_range,
    _extent,
    _partition_key,
    _secondary_indexes,
    _table_exists,
)

# ---------------------------------------------------------------------------
# Cutover steps
# ---------------------------------------------------------------------------


def _execute(conn: object, sql: str, params: tuple[object, ...] | None = None, *, dry_run: bool) -> int:
    """Run one statement, or trace it under --dry-run. Returns affected rows."""
    if dry_run:
        _log.info("[dry-run] %s", sql if params is None else f"{sql} -- params={params!r}")
        return 0
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(sql, params)
    return int(cur.rowcount)


def _build_shadow(
    conn: object,
    *,
    source: str,
    shadow: str,
    forward_months: int,
    now: datetime.date,
    dry_run: bool,
) -> None:
    """Step 1 — create the shadow table and every partition it needs."""
    source_columns = _columns(conn, source)

    if _table_exists(conn, shadow):
        # An interrupted earlier run. Reuse it -- that is what makes the copy
        # resumable -- but only after confirming it still has the source's
        # columns, because a copy into a stale shape is the one failure this
        # script could commit without any statement erroring.
        shadow_columns = _columns(conn, shadow)
        if shadow_columns != source_columns:
            msg = (
                f"{shadow} exists from an earlier run but its columns no longer match {source}\n"
                f"  {source}: {source_columns}\n"
                f"  {shadow}: {shadow_columns}\n"
                f"Drop {shadow} and re-run to rebuild it against the current schema."
            )
            raise RuntimeError(msg)
        _log.info("reusing existing %s (columns match %s)", shadow, source)
    else:
        key = _partition_key(conn, source)
        _log.info("CREATE %s (LIKE %s) PARTITION BY %s", shadow, source, key)
        _execute(
            conn,
            f"CREATE TABLE {ident(shadow)} "
            f"(LIKE {ident(source)} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING GENERATED) "
            f"PARTITION BY {key}",
            dry_run=dry_run,
        )
        if not dry_run:
            conn.commit()  # type: ignore[attr-defined]

    existing = {name for name, _ in _child_partitions(conn, shadow)}

    # Every source partition, with the source's own bound expression. This is
    # what keeps the shadow's coverage from being narrower than the live
    # table's; recomputing month boundaries here would only be equivalent by
    # coincidence.
    for child, bound in _child_partitions(conn, source):
        target = shadow_child_name(child, source, shadow)
        if target in existing:
            _log.debug("partition %s already exists — skipping", target)
            continue
        _log.info("CREATE PARTITION %s %s", target, bound)
        _execute(conn, f"CREATE TABLE {ident(target)} PARTITION OF {ident(shadow)} {bound}", dry_run=dry_run)
        if not dry_run:
            conn.commit()  # type: ignore[attr-defined]
        existing.add(target)

    # Forward headroom, so the table can still accept writes after the cutover
    # without an operator racing the calendar. Skipped by name where a source
    # partition already covered the month.
    horizon_year = now.year + (now.month - 1 + forward_months) // 12
    horizon_month = (now.month - 1 + forward_months) % 12 + 1
    horizon = datetime.date(horizon_year, horizon_month, 1)
    for from_d, to_d in month_range(datetime.date(now.year, now.month, 1), horizon):
        target = partition_name(shadow, from_d)
        if target in existing:
            continue
        _log.info("CREATE PARTITION %s (forward headroom)", target)
        _execute(
            conn,
            f"CREATE TABLE {ident(target)} PARTITION OF {ident(shadow)} "
            f"FOR VALUES FROM ('{from_d.isoformat()}') TO ('{to_d.isoformat()}')",
            dry_run=dry_run,
        )
        if not dry_run:
            conn.commit()  # type: ignore[attr-defined]
        existing.add(target)


def _copy(
    conn: object,
    *,
    source: str,
    shadow: str,
    columns: list[str],
    chunks: list[tuple[datetime.date, datetime.date]],
    dry_run: bool,
) -> int:
    """Step 2 — month-chunked, per-chunk-committed, resumable copy."""
    statement = copy_sql(columns, source=source, shadow=shadow)
    total = 0
    for lo, hi in chunks:
        if not dry_run and _count_in_range(conn, shadow, lo, hi) > 0:
            _log.info("RESUME: %s [%s, %s) already populated — skipping", shadow, lo, hi)
            continue
        _log.info("COPY %s → %s [%s, %s)", source, shadow, lo, hi)
        total += _execute(conn, statement, (lo, hi), dry_run=dry_run)
        if not dry_run:
            conn.commit()  # type: ignore[attr-defined]
    return total


def _build_indexes_and_constraints(
    conn: object,
    *,
    source: str,
    shadow: str,
    indexes: list[tuple[str, str]],
    constraints: list[tuple[str, str, bool]],
    dry_run: bool,
) -> None:
    """Step 3 — add the primary key, indexes and foreign keys after the load.

    Every statement here takes a heavy lock on the shadow and the foreign-key
    adds scan it end to end. All of that is free: nothing reads this table
    until phase 5 renames it.
    """
    for name, definition, owns_index in constraints:
        target = f"{name}{_BUILD_SUFFIX}" if owns_index else name
        _log.info("ADD CONSTRAINT %s ON %s — %s", target, shadow, definition)
        _execute(
            conn,
            f"ALTER TABLE {ident(shadow)} ADD CONSTRAINT {ident(target)} {definition}",
            dry_run=dry_run,
        )
        if not dry_run:
            conn.commit()  # type: ignore[attr-defined]

    for name, definition in indexes:
        target = f"{name}{_BUILD_SUFFIX}"
        statement = rewrite_indexdef(definition, source=source, shadow=shadow, new_name=target)
        _log.info("CREATE INDEX %s ON %s", target, shadow)
        _execute(conn, statement, dry_run=dry_run)
        if not dry_run:
            conn.commit()  # type: ignore[attr-defined]


def _verify(
    conn: object,
    *,
    source: str,
    shadow: str,
    chunks: list[tuple[datetime.date, datetime.date]],
    dry_run: bool,
) -> None:
    """Step 4 — counts must agree per chunk and overall before any rename."""
    if dry_run:
        _log.info("[dry-run] would verify per-chunk and total row counts")
        return

    for lo, hi in chunks:
        src = _count_in_range(conn, source, lo, hi)
        dst = _count_in_range(conn, shadow, lo, hi)
        if src != dst:
            msg = (
                f"row count mismatch for [{lo}, {hi}): {source} has {src}, {shadow} has {dst}. "
                f"Nothing has been renamed. Re-run to copy the difference, or drop {shadow} to start over."
            )
            raise RuntimeError(msg)
        _log.debug("verified [%s, %s): %d rows", lo, hi, src)

    src_total = _count_all(conn, source)
    dst_total = _count_all(conn, shadow)
    # The source may still be taking writes, so the shadow being *behind* is
    # expected here and is what the in-lock catch-up exists to close. Being
    # *ahead* is not: it means this table holds rows the source does not.
    if dst_total > src_total:
        msg = (
            f"{shadow} holds {dst_total} rows but {source} holds {src_total}. "
            f"A shadow ahead of its source cannot be explained by concurrent writes; refusing to cut over."
        )
        raise RuntimeError(msg)
    _log.info("verified %d chunk(s); %s=%d rows, %s=%d rows", len(chunks), source, src_total, shadow, dst_total)


def _cutover(
    conn: object,
    *,
    source: str,
    shadow: str,
    columns: list[str],
    indexes: list[tuple[str, str]],
    constraints: list[tuple[str, str, bool]],
    tail_window: str,
    lock_timeout: str,
    lock_retries: int,
    retry_wait_s: float,
    dry_run: bool,
) -> None:
    """Step 5 — one short transaction: lock, catch up, verify, rename."""
    archive = archive_table(source)
    archive_rename, promote_rename = rename_sql(source)

    if dry_run:
        _log.info("[dry-run] SET lock_timeout = %s", lock_timeout)
        _log.info("[dry-run] LOCK TABLE %s IN ACCESS EXCLUSIVE MODE", source)
        _log.info("[dry-run] %s", catchup_sql(columns, source=source, shadow=shadow))
        _log.info("[dry-run] verify tail counts, then roll back on mismatch")
        for name, _, owns_index in constraints:
            if not owns_index:
                continue  # already carries its final name; nothing to hand over
            _log.info("[dry-run] ALTER TABLE %s RENAME CONSTRAINT %s TO %s_archive", source, name, name)
            _log.info("[dry-run] ALTER TABLE %s RENAME CONSTRAINT %s%s TO %s", shadow, name, _BUILD_SUFFIX, name)
        for name, _ in indexes:
            _log.info("[dry-run] ALTER INDEX %s RENAME TO %s_archive", name, name)
            _log.info("[dry-run] ALTER INDEX %s%s RENAME TO %s", name, _BUILD_SUFFIX, name)
        # The shadow does not exist under --dry-run, so its children are
        # projected from the source's rather than read back.
        source_children = _children_with_indexes(conn, source)
        for child in sorted(source_children):
            _log.info(
                "[dry-run] ALTER TABLE %s RENAME TO %s (and %d index(es))",
                child,
                child.replace(source, archive, 1),
                len(source_children[child]),
            )
            _log.info(
                "[dry-run] ALTER TABLE %s RENAME TO %s (and its indexes)",
                shadow_child_name(child, source, shadow),
                child,
            )
        _log.info("[dry-run] %s", archive_rename)
        _log.info("[dry-run] %s", promote_rename)
        return

    cur = conn.cursor()  # type: ignore[attr-defined]
    for attempt in range(1, lock_retries + 1):
        try:
            cur.execute("BEGIN")
            # Fail fast rather than queue. An ACCESS EXCLUSIVE request that
            # waits also blocks every reader that arrives behind it, which is
            # how a rename that "takes milliseconds" becomes an outage.
            cur.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
            cur.execute(f"LOCK TABLE {ident(source)} IN ACCESS EXCLUSIVE MODE")
            break
        except Exception as exc:
            cur.execute("ROLLBACK")
            if attempt == lock_retries:
                msg = (
                    f"could not acquire ACCESS EXCLUSIVE on {source} within {lock_timeout} "
                    f"after {lock_retries} attempt(s): {exc}. Nothing has been renamed; re-run when the "
                    f"table is quieter."
                )
                raise RuntimeError(msg) from exc
            _log.warning(
                "lock attempt %d/%d on %s timed out after %s — retrying in %.1fs",
                attempt,
                lock_retries,
                source,
                lock_timeout,
                retry_wait_s,
            )
            time.sleep(retry_wait_s)

    try:
        # Writers have drained, so the source is static from here on. The tail
        # floor is deliberately earlier than the last copied row: a transaction
        # that started before its month was copied and committed afterwards
        # carries a `now()` from before the copy, so a floor at the high-water
        # mark alone would step over it.
        cur.execute(
            f"SELECT COALESCE(MAX(ts), '-infinity'::timestamptz) - INTERVAL %s FROM {ident(shadow)}",  # noqa: S608 - `shadow` derives from the _SOURCE_TABLE constant and is validated by ident()
            (tail_window,),
        )
        row = cur.fetchone()
        floor = row[0] if row else None

        copied = 0
        if floor is not None:
            cur.execute(catchup_sql(columns, source=source, shadow=shadow), (floor,))
            copied = int(cur.rowcount)
            _log.info("caught up %d row(s) written during the copy (tail floor %s)", copied, floor)

            # Verify the tail, not the whole table: this runs with the lock
            # held, so a full COUNT(*) here would extend the outage in
            # proportion to table size. The bulk was already verified in
            # phase 4, outside the lock.
            src_tail = _count_in_range(conn, source, floor)
            dst_tail = _count_in_range(conn, shadow, floor)
            if src_tail != dst_tail:
                msg = (
                    f"tail count mismatch after catch-up: {source} has {src_tail} row(s) at or after "
                    f"{floor}, {shadow} has {dst_tail}. Rolling back — no rename, no rows lost."
                )
                raise RuntimeError(msg)

        # Hand the canonical names over: parent constraints and indexes, then
        # every child partition and the indexes Postgres named after it. All
        # catalog updates, under a lock we already hold -- a few hundred of them
        # on a 24-partition table, still microseconds each. Paying that inside
        # the lock window is what makes the end state indistinguishable from a
        # fresh install, which is what both the archival monitor's name pattern
        # and any later re-run of this script depend on.
        for name, _, owns_index in constraints:
            if not owns_index:
                continue  # foreign keys were built under their final names
            cur.execute(f"ALTER TABLE {ident(source)} RENAME CONSTRAINT {ident(name)} TO {ident(name + '_archive')}")
            cur.execute(f"ALTER TABLE {ident(shadow)} RENAME CONSTRAINT {ident(name + _BUILD_SUFFIX)} TO {ident(name)}")
        for name, _ in indexes:
            cur.execute(f"ALTER INDEX {ident(name)} RENAME TO {ident(name + '_archive')}")
            cur.execute(f"ALTER INDEX {ident(name + _BUILD_SUFFIX)} RENAME TO {ident(name)}")

        # Read both child sets before renaming either: the outgoing partitions
        # have to give up the canonical names before the incoming ones take them.
        source_children = _children_with_indexes(conn, source)
        shadow_children = _children_with_indexes(conn, shadow)
        for child, child_indexes in sorted(source_children.items()):
            for statement in child_rename_statements(child, child.replace(source, archive, 1), child_indexes):
                cur.execute(statement)
        for child, child_indexes in sorted(shadow_children.items()):
            canonical = f"{source}_{child[len(shadow) + 1 :]}"
            for statement in child_rename_statements(child, canonical, child_indexes):
                cur.execute(statement)
        _log.info(
            "renamed %d outgoing and %d incoming partition(s) to keep the canonical naming",
            len(source_children),
            len(shadow_children),
        )

        cur.execute(archive_rename)
        cur.execute(promote_rename)
        cur.execute("COMMIT")
        _log.info("cutover committed: %s → %s, %s → %s", source, archive, shadow, source)
    except Exception:
        cur.execute("ROLLBACK")
        _log.error("cutover rolled back; %s is untouched and still live", source)
        raise


# ---------------------------------------------------------------------------
# High-level migration logic
# ---------------------------------------------------------------------------


def _migrate_range_table(
    conn: object,
    table: str,
    now: datetime.date,
    dry_run: bool,
    *,
    forward_months: int = 12,
    tail_window: str = "2 hours",
    lock_timeout: str = "3s",
    lock_retries: int = 5,
    retry_wait_s: float = 5.0,
) -> None:
    """Run every phase for one RANGE-partitioned table."""
    shadow = shadow_table(table)
    archive = archive_table(table)

    if _table_exists(conn, archive):
        _log.warning("cutover already done: %s exists — skipping %s", archive, table)
        return

    if not _table_exists(conn, table):
        msg = f"{table} does not exist; nothing to cut over"
        raise RuntimeError(msg)

    columns = _columns(conn, table)
    indexes = _secondary_indexes(conn, table)
    constraints = _constraints(conn, table)
    _log.info(
        "%s: %d column(s), %d secondary index(es), %d constraint(s) to reproduce",
        table,
        len(columns),
        len(indexes),
        len(constraints),
    )

    # Step 1
    _build_shadow(
        conn,
        source=table,
        shadow=shadow,
        forward_months=forward_months,
        now=now,
        dry_run=dry_run,
    )

    # Chunks cover exactly the data that exists; empty forward partitions have
    # nothing to copy.
    oldest, newest = _extent(conn, table)
    if oldest is None or newest is None:
        _log.info("%s is empty — no rows to copy", table)
        chunks: list[tuple[datetime.date, datetime.date]] = []
    else:
        oldest_d = oldest.date() if hasattr(oldest, "date") else oldest
        newest_d = newest.date() if hasattr(newest, "date") else newest
        hist_start = datetime.date(oldest_d.year, oldest_d.month, 1)
        end_year = newest_d.year + (1 if newest_d.month == 12 else 0)
        end_month = 1 if newest_d.month == 12 else newest_d.month + 1
        chunks = list(month_range(hist_start, datetime.date(end_year, end_month, 1)))

    # Step 2
    copied = _copy(conn, source=table, shadow=shadow, columns=columns, chunks=chunks, dry_run=dry_run)
    _log.info("copied %d row(s) from %s → %s across %d chunk(s)", copied, table, shadow, len(chunks))

    # Step 3
    _build_indexes_and_constraints(
        conn,
        source=table,
        shadow=shadow,
        indexes=indexes,
        constraints=constraints,
        dry_run=dry_run,
    )

    # Step 4
    _verify(conn, source=table, shadow=shadow, chunks=chunks, dry_run=dry_run)

    # Step 5
    _cutover(
        conn,
        source=table,
        shadow=shadow,
        columns=columns,
        indexes=indexes,
        constraints=constraints,
        tail_window=tail_window,
        lock_timeout=lock_timeout,
        lock_retries=lock_retries,
        retry_wait_s=retry_wait_s,
        dry_run=dry_run,
    )

    # Step 6
    _log.info(
        "%s now holds the previous table. Verify it, then drop it deliberately: DROP TABLE %s CASCADE",
        archive,
        archive,
    )


def run_migration(
    conn: object,
    dry_run: bool = False,
    *,
    forward_months: int = 12,
    tail_window: str = "2 hours",
    lock_timeout: str = "3s",
    lock_retries: int = 5,
    retry_wait_s: float = 5.0,
) -> None:
    """Execute the full partition cutover.

    Operates on: audit_log.
    """
    now = datetime.date.today()

    _migrate_range_table(
        conn,
        _SOURCE_TABLE,
        now,
        dry_run,
        forward_months=forward_months,
        tail_window=tail_window,
        lock_timeout=lock_timeout,
        lock_retries=lock_retries,
        retry_wait_s=retry_wait_s,
    )

    # `embeddings` is absent on purpose. Its migration creates it already partitioned, so
    # there is nothing to copy and nothing to rename -- and one physical shape means the
    # shape the tests exercise is the shape that runs.

    _log.info("partition migration complete")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Zero-downtime cutover to a rebuilt partitioned table.")
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
    parser.add_argument(
        "--forward-months",
        type=int,
        default=12,
        help="Months of empty partition headroom to create beyond the current month (default: 12)",
    )
    parser.add_argument(
        "--tail-window",
        default="2 hours",
        help=(
            "How far back the in-lock catch-up re-checks for rows. Must exceed your longest write "
            "transaction, or a row committed late could be missed (default: 2 hours)"
        ),
    )
    parser.add_argument(
        "--lock-timeout",
        default="3s",
        help="How long to wait for ACCESS EXCLUSIVE before backing off (default: 3s)",
    )
    parser.add_argument(
        "--lock-retries",
        type=int,
        default=5,
        help="Lock acquisition attempts before giving up (default: 5)",
    )
    parser.add_argument(
        "--retry-wait",
        type=float,
        default=5.0,
        help="Seconds between lock attempts (default: 5.0)",
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
        run_migration(
            conn,
            dry_run=args.dry_run,
            forward_months=args.forward_months,
            tail_window=args.tail_window,
            lock_timeout=args.lock_timeout,
            lock_retries=args.lock_retries,
            retry_wait_s=args.retry_wait,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
