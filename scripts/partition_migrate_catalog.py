"""Reading `audit_log`'s actual shape back out of `pg_catalog`.

The introspection half of `scripts/partition_migrate.py`. Every function here
answers one question about the live table -- its columns, its partition key, its
child partitions and their bounds, its indexes, its constraints, its row counts
-- and none of them decide anything.

This layer exists so the cutover copies into a table derived from the one it is
copying, rather than from DDL written once in a migration and hand-maintained
afterwards. That is the whole reason the shadow table is no longer part of the
schema: a literal cannot notice a column being added, and a catalog query
cannot miss one.
"""

from __future__ import annotations

import datetime

from partition_migrate_sql import ident


def _table_exists(conn: object, table: str) -> bool:
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = %s AND c.relkind IN ('r', 'p')",
        (table,),
    )
    return cur.fetchone() is not None


def _partition_key(conn: object, table: str) -> str:
    """Return the table's partition strategy clause, e.g. `RANGE (ts)`."""
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute("SELECT pg_get_partkeydef(%s::regclass)", (table,))
    row = cur.fetchone()
    if row is None or not row[0]:
        msg = f"{table} is not partitioned; this script only repartitions an already-partitioned table"
        raise RuntimeError(msg)
    key: str = row[0]
    if not key.upper().startswith("RANGE"):
        msg = f"{table} is partitioned by {key!r}; only RANGE partitioning is supported"
        raise RuntimeError(msg)
    return key


def _columns(conn: object, table: str) -> list[str]:
    """Return the table's columns in attnum order."""
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped "
        "ORDER BY attnum",
        (table,),
    )
    return [row[0] for row in cur.fetchall()]


def _child_partitions(conn: object, parent: str) -> list[tuple[str, str]]:
    """Return [(child_name, bound_expression)] for *parent*, name-ordered."""
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        """
        SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
        FROM   pg_inherits i
        JOIN   pg_class c ON c.oid = i.inhrelid
        JOIN   pg_class p ON p.oid = i.inhparent
        WHERE  p.relname = %s
        ORDER  BY c.relname
        """,
        (parent,),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def _children_with_indexes(conn: object, parent: str) -> dict[str, list[str]]:
    """Return {child_partition_name: [its index names]} for *parent*.

    Both halves are renamed at cutover. The child *tables* have to be, because
    `audit_partitions_eligible_for_archival` matches `^audit_log_(\\d{4})_(\\d{2})$`
    and silently skips anything else: leaving the promoted partitions named
    `audit_log_new_2025_03` would park the archival gauge at 0 permanently, so
    the alert that says "partitions are aging out" would simply never fire
    again. The child *indexes* have to be because Postgres derives their names
    from the table name at creation time and does not revisit them on rename --
    so a second cutover would try to create `audit_log_new_2025_03_pkey` while
    the first cutover's copy still held that name.
    """
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        """
        SELECT c.relname, ci.relname
        FROM   pg_inherits inh
        JOIN   pg_class c ON c.oid = inh.inhrelid
        JOIN   pg_class p ON p.oid = inh.inhparent
        LEFT   JOIN pg_index i ON i.indrelid = c.oid
        LEFT   JOIN pg_class ci ON ci.oid = i.indexrelid
        WHERE  p.relname = %s
        ORDER  BY c.relname, ci.relname
        """,
        (parent,),
    )
    children: dict[str, list[str]] = {}
    for child, index in cur.fetchall():
        children.setdefault(child, [])
        if index is not None:
            children[child].append(index)
    return children


def _secondary_indexes(conn: object, table: str) -> list[tuple[str, str]]:
    """Return [(index_name, indexdef)] excluding constraint-backed indexes.

    Primary-key and unique indexes are created by their constraints in phase 3,
    so replaying them here as well would collide.
    """
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        """
        SELECT i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid)
        FROM   pg_index i
        WHERE  i.indrelid = %s::regclass
          AND  NOT i.indisprimary
          AND  NOT i.indisunique
          AND  NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
        ORDER  BY 1
        """,
        (table,),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def _constraints(conn: object, table: str) -> list[tuple[str, str, bool]]:
    """Return [(name, definition, owns_an_index)] for the PK and foreign keys.

    CHECK constraints are excluded: `LIKE ... INCLUDING CONSTRAINTS` already
    carried them onto the shadow, and adding them twice is an error.

    The third element decides whether the name has to be suffixed while the
    shadow is being built. Only `PRIMARY KEY`/`UNIQUE` create an index, and
    only index names are unique per *schema* -- a constraint name only has to
    be unique per table. So the foreign keys can be added under their final
    names immediately, and their partitions inherit those names rather than
    `..._fkey_new`, which is what a violation on the promoted table would
    otherwise quote back at whoever hit it.
    """
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        """
        SELECT conname, pg_get_constraintdef(oid), contype IN ('p', 'u')
        FROM   pg_constraint
        WHERE  conrelid = %s::regclass AND contype IN ('p', 'f')
        ORDER  BY contype DESC, conname
        """,
        (table,),
    )
    return [(row[0], row[1], bool(row[2])) for row in cur.fetchall()]


def _extent(conn: object, table: str) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(f"SELECT MIN(ts), MAX(ts) FROM {ident(table)}")  # noqa: S608 - `table` is the _SOURCE_TABLE constant, validated by ident()
    row = cur.fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _count_in_range(conn: object, table: str, lo: object, hi: object | None = None) -> int:
    cur = conn.cursor()  # type: ignore[attr-defined]
    if hi is None:
        cur.execute(
            f"SELECT COUNT(*) FROM {ident(table)} WHERE ts >= %s",  # noqa: S608 - `table` is derived from the _SOURCE_TABLE constant and validated by ident(); bounds are bound via %s
            (lo,),
        )
    else:
        cur.execute(
            f"SELECT COUNT(*) FROM {ident(table)} WHERE ts >= %s AND ts < %s",  # noqa: S608 - `table` is derived from the _SOURCE_TABLE constant and validated by ident(); bounds are bound via %s
            (lo, hi),
        )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _count_all(conn: object, table: str) -> int:
    cur = conn.cursor()  # type: ignore[attr-defined]
    cur.execute(f"SELECT COUNT(*) FROM {ident(table)}")  # noqa: S608 - `table` is derived from the _SOURCE_TABLE constant and validated by ident()
    row = cur.fetchone()
    return int(row[0]) if row else 0
