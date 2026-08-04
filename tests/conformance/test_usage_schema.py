"""The usage table cannot hold content, and the vocabularies cannot drift.

Two properties, both structural, and neither provable by reading the migration.

**No free text.** A usage row is written on every authenticated call, kept for
ninety days, and covered by a right-to-be-forgotten obligation. A text column in
that table is where a customer email eventually lands — not through malice, through
someone adding a `notes` field for a good reason. Scanning for content afterwards
is a losing game; having nowhere to put it is not. So the assertion is about the
*shape of the schema*, not about what any caller currently writes.

**The vocabularies agree.** The closed sets live in `registry/usage/vocabularies.py`
and are enforced by CHECK constraints written as SQL literals in the migration. Two
lists that must match and live in different files eventually stop matching, and the
symptom is a value the application accepts and the database rejects at 3am.

Read from the parsed migration rather than a live database, so this runs in CI with
no container — the ARC content-minimisation gate is the closest precedent and its
fixtures currently error on a missing sync driver, which is exactly the dependency
this avoids inheriting.
"""

from __future__ import annotations

import importlib
import re

import pytest

from registry.usage import vocabularies

# Imported rather than read as text, so the assertions run against the SQL that
# will actually execute. The migration builds its DDL with f-strings, and reading
# the file would test the placeholders instead of the interpolated vocabularies —
# which is precisely the drift this file exists to catch.
_MIGRATION_MODULE = importlib.import_module("registry.storage.migrations.versions.0043_usage_events")


def _create_table_sql() -> str:
    match = re.search(r"CREATE TABLE usage_events \((.*?)\n\) PARTITION BY", _MIGRATION_MODULE._TABLE, re.S)
    assert match, "could not locate the CREATE TABLE body — did the migration change shape?"
    return match.group(1)


def _declared_columns() -> dict[str, str]:
    """Column name → declared type, from the migration's own DDL."""
    columns: dict[str, str] = {}
    for line in _create_table_sql().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--") or stripped.startswith(("CONSTRAINT", "PRIMARY KEY")):
            continue
        parts = stripped.rstrip(",").split()
        if len(parts) >= 2:
            columns[parts[0]] = parts[1].upper()
    return columns


# ---------------------------------------------------------------------------
# No free text
# ---------------------------------------------------------------------------

# The three TEXT columns the table is allowed to have, each with the reason it is
# not free text. Anything else must justify itself here first, which is the whole
# mechanism: adding a column means editing this list and explaining yourself.
_BOUNDED_TEXT_COLUMNS = {
    "surface": "closed vocabulary, CHECK-constrained",
    "operation": "route template or tool name — bounded by the code, not by input",
    "outcome": "closed vocabulary, CHECK-constrained",
    "status_class": "closed vocabulary, CHECK-constrained",
    "request_id": "generated correlation id, sanitised and length-capped upstream",
    "query_digest": "fixed-width sha256 hex, CHECK-constrained to 64 characters",
}


def test_every_text_column_is_declared_and_justified() -> None:
    """The gate. A new text column fails here until someone explains it.

    Deliberately not "no TEXT columns" — several are legitimate and bounded by
    something other than their type. What is banned is an *undeclared* one, so the
    check is a diff against an allowlist rather than a ban on a keyword.
    """
    text_columns = {name for name, kind in _declared_columns().items() if kind.startswith("TEXT")}
    undeclared = text_columns - set(_BOUNDED_TEXT_COLUMNS)
    assert not undeclared, (
        f"undeclared text column(s) in usage_events: {sorted(undeclared)}. "
        "Every usage row is written on an authenticated call, retained for 90 days, "
        "and covered by RTBF — a free-text column here is where personal data lands. "
        "If the column is genuinely bounded, add it to _BOUNDED_TEXT_COLUMNS with the reason."
    )


def test_the_query_terms_have_nowhere_to_go() -> None:
    """The specific column someone will want, and its absence.

    "What are people searching for" is a real and valuable question. It is
    deliberately unanswerable from this table: a digest, a length and a result
    count support "how often did a search return nothing" without recording what
    anyone asked. Recording the terms was raised and deferred to an amendment.
    """
    columns = _declared_columns()
    assert "query_digest" in columns
    assert "query_length" in columns
    for forbidden in ("query", "query_text", "search_text", "query_terms", "notes", "detail", "message"):
        assert forbidden not in columns, f"usage_events must not have a '{forbidden}' column"


def test_the_digest_column_is_length_bounded() -> None:
    # Without the CHECK, `query_digest` is a TEXT column with a reassuring name —
    # which is a worse free-text field than an honestly-named one, because it
    # passes review.
    body = _create_table_sql()
    assert "chk_usage_query_digest" in body
    assert "char_length(query_digest) = 64" in body


# ---------------------------------------------------------------------------
# The vocabularies and the constraints describe the same sets
# ---------------------------------------------------------------------------


def _check_values(constraint: str) -> set[str]:
    body = _create_table_sql()
    match = re.search(rf"{constraint}\s+CHECK \([a-z_]+ IN \(([^)]*)\)\)", body)
    assert match, f"could not find {constraint} in the migration"
    return {v.strip().strip("'") for v in match.group(1).split(",")}


@pytest.mark.parametrize(
    ("constraint", "declared"),
    [
        ("chk_usage_surface", vocabularies.SURFACES),
        ("chk_usage_outcome", vocabularies.OUTCOMES),
        ("chk_usage_status_class", vocabularies.STATUS_CLASSES),
    ],
)
def test_the_constraint_and_the_module_agree(constraint: str, declared: frozenset[str]) -> None:
    assert _check_values(constraint) == set(declared)


def test_status_classes_are_shared_with_the_operational_tier() -> None:
    """Not redefined here.

    A second copy would let the two tiers disagree about what a 429 is called, and
    then two dashboards built from one service tell different stories about the
    same request.
    """
    from registry.metrics import STATUS_CLASSES

    assert vocabularies.STATUS_CLASSES is STATUS_CLASSES


def test_the_surface_split_is_exactly_rest_and_mcp() -> None:
    # No third member for browser traffic: interaction analytics stays with a
    # third-party tool, so nothing in this table is ever submitted by a browser.
    # The rest/mcp split is the one that makes agent adoption visible at all.
    assert vocabularies.SURFACES == {"rest", "mcp"}


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------


def test_the_table_is_partitioned_on_the_timestamp() -> None:
    assert "PARTITION BY RANGE (occurred_at)" in _MIGRATION_MODULE._TABLE
    # Same treatment as audit_log, episodes, notifications and the PII log, so the
    # existing archival runbook covers this table without change. 24 children are
    # pre-created; nothing creates more, which is recorded as a known limit.
    names = [n for n, _, _ in _MIGRATION_MODULE._monthly_partition_bounds(
        _MIGRATION_MODULE._PARTITION_START, _MIGRATION_MODULE._PARTITION_COUNT)]
    assert len(names) == 24
    assert names[0] == "usage_events_2025_01"


def test_actor_id_is_nullable() -> None:
    """An unauthenticated call has no actor, and the row is still recorded.

    Skipping those rows would silently change the denominator of every rate
    computed from this table, and "how many callers could not authenticate" is one
    of the more useful things it can answer.
    """
    columns = _declared_columns()
    assert columns["actor_id"] == "UUID"
    body = _create_table_sql()
    actor_line = next(ln for ln in body.splitlines() if ln.strip().startswith("actor_id"))
    assert "NOT NULL" not in actor_line
