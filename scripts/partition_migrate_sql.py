"""Name and statement construction for the `audit_log` partition cutover.

The pure half of `scripts/partition_migrate.py`: every identifier this cutover
derives and every SQL string it issues, with no database handle in sight. Split
out along the seam the cutover script already drew, so the layer its unit tests
actually exercise is a module boundary rather than a comment.

Nothing here talks to Postgres. Nothing here decides *whether* to run a
statement -- that is the phased logic in `partition_migrate.py`. What lives here
is the part where a wrong string is a silent data-integrity bug rather than an
error, which is why it is the part that is exhaustively unit-tested.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterator

# Suffix carried by the shadow's indexes and constraints while it is being
# built, because an index name is unique per *schema* -- the shadow cannot hold
# `idx_audit_tenant_ts` while the live table still does. Step 5 hands the
# canonical names over inside the cutover transaction.
_BUILD_SUFFIX = "_new"

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_$]*$")

# `CREATE [UNIQUE] INDEX name ON [ONLY] schema.table USING ...`, which is the
# shape pg_get_indexdef emits. The `ONLY` is why this is rewritten rather than
# reused verbatim: on a partitioned parent pg_get_indexdef reports `ON ONLY`,
# and replaying that would create a parent index with no child indexes attached
# -- left permanently invalid instead of covering the partitions.
_INDEXDEF_RE = re.compile(
    r"^(?P<head>CREATE\s+(?:UNIQUE\s+)?INDEX\s+)"
    r"(?P<name>\S+)"
    r"\s+ON\s+(?:ONLY\s+)?"
    r"(?P<table>\S+)"
    r"(?P<tail>\s+USING\s+.*)$",
    re.IGNORECASE | re.DOTALL,
)


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


def shadow_table(table: str) -> str:
    """Name of the table rows are copied into before the rename."""
    return f"{table}_new"


def archive_table(table: str) -> str:
    """Name the live table is renamed to at cutover."""
    return f"{table}_archive"


def ident(name: str) -> str:
    """Return *name* if it is a plain lowercase SQL identifier, else raise.

    Every identifier this script interpolates comes from `pg_catalog` or from
    the `_SOURCE_TABLE` constant, so this always passes. It exists so that if
    that ever stops being true the script refuses to build the statement
    instead of building an injectable one -- a check, not a quoting scheme.
    """
    if not _IDENT_RE.match(name):
        msg = f"refusing to interpolate {name!r}: not a plain lowercase SQL identifier"
        raise ValueError(msg)
    return name


def shadow_child_name(source_child: str, source: str, shadow: str) -> str:
    """Map a source partition name onto its shadow counterpart.

    `audit_log_2025_01` -> `audit_log_new_2025_01`. A child that does not carry
    the parent's prefix is refused rather than guessed at: the forward-headroom
    step below decides what to create by testing these names for existence, so
    a name this function had to invent could silently produce either a
    duplicate or a gap in coverage.
    """
    prefix = f"{source}_"
    if not source_child.startswith(prefix):
        msg = (
            f"partition {source_child!r} does not start with {prefix!r}, so its shadow name cannot be "
            f"derived. Rename it to the {prefix}YYYY_MM convention, or cut this table over by hand."
        )
        raise ValueError(msg)
    return f"{shadow}_{source_child[len(prefix) :]}"


def rewrite_indexdef(indexdef: str, *, source: str, shadow: str, new_name: str) -> str:
    """Retarget a `pg_get_indexdef` string at the shadow table.

    Swaps the index name for *new_name*, the table for *shadow*, and drops any
    `ONLY` so the create recurses into the partitions instead of leaving a
    parent-only index that never becomes valid.
    """
    match = _INDEXDEF_RE.match(indexdef.strip())
    if match is None:
        msg = f"could not parse index definition, refusing to guess at it: {indexdef!r}"
        raise ValueError(msg)
    if source not in match.group("table"):
        msg = f"index definition targets {match.group('table')!r}, not {source!r}: {indexdef!r}"
        raise ValueError(msg)
    return f"{match.group('head')}{ident(new_name)} ON {ident(shadow)}{match.group('tail')}"


def rename_sql(table: str) -> tuple[str, str]:
    """Return (archive_rename_sql, promote_rename_sql) for a table cutover."""
    archive = f"ALTER TABLE {ident(table)} RENAME TO {ident(archive_table(table))}"
    promote = f"ALTER TABLE {ident(shadow_table(table))} RENAME TO {ident(table)}"
    return archive, promote


def child_rename_statements(old_child: str, new_child: str, indexes: list[str]) -> list[str]:
    """Rename one partition and the indexes whose names were derived from it.

    Indexes come first: an index name is unique per schema, so the incoming
    partition's indexes cannot take the canonical names until the outgoing
    ones have let go of them. An index whose name does not start with
    *old_child* is left alone rather than rewritten -- Postgres derives child
    index names from the table, so one that does not is something an operator
    named deliberately.
    """
    statements = [
        f"ALTER INDEX {ident(index)} RENAME TO {ident(new_child + index[len(old_child) :])}"
        for index in indexes
        if index.startswith(old_child)
    ]
    statements.append(f"ALTER TABLE {ident(old_child)} RENAME TO {ident(new_child)}")
    return statements


def copy_sql(columns: list[str], *, source: str, shadow: str) -> str:
    """Build the month-chunk copy statement with an explicit column list."""
    if not columns:
        msg = "refusing to build a copy statement with no columns"
        raise ValueError(msg)
    cols = ", ".join(ident(c) for c in columns)
    return (
        f"INSERT INTO {ident(shadow)} ({cols}) "  # noqa: S608 - `cols` is validated by ident(); source/shadow derive from the _SOURCE_TABLE constant
        f"SELECT {cols} FROM {ident(source)} WHERE ts >= %s AND ts < %s"
    )


def catchup_sql(columns: list[str], *, source: str, shadow: str) -> str:
    """Build the in-lock tail copy.

    Anti-joined on the primary key rather than filtered on `ts > high_water`,
    because two rows can share a timestamp and a `>` would drop the second one
    while a `>=` would duplicate the first. Bounded by `ts >= %s` so the
    anti-join reads the tail rather than the whole table.
    """
    cols = ", ".join(ident(c) for c in columns)
    return (
        f"INSERT INTO {ident(shadow)} ({cols}) "  # noqa: S608 - `cols` is validated by ident(); source/shadow derive from the _SOURCE_TABLE constant
        f"SELECT {cols} FROM {ident(source)} s WHERE s.ts >= %s "
        f"AND NOT EXISTS (SELECT 1 FROM {ident(shadow)} d WHERE d.audit_id = s.audit_id AND d.ts = s.ts)"
    )
