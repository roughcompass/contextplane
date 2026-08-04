"""ARC closed-vocabulary conformance gate.

Two columns are declared closed sets: `arc_revisions.content_classification` and
`arc_receipt_events.event_type`. Closed means three things have to agree — the
constants in `registry.arc.vocabularies`, the `CHECK` constraints in the
database, and the values the code actually writes. Any two of those can drift
apart silently, so each pairing gets a test.

1. **Constants against the live schema.** Reads `pg_get_constraintdef` and parses
   the stored form, so the assertion is against what the database will actually
   enforce rather than against the migration's source text. A migration that was
   written but never applied fails here, which is the point.

2. **Code against the constants.** An AST walk over the ARC package, failing on a
   bare string literal in an `event_type=` keyword argument. Modelled on the audit
   action gate, including its negative fixture: a walker that silently matches
   nothing passes vacuously, and that is the failure mode worth designing against.

The cross-column rule — `regulated` content may not sit in plaintext storage —
is asserted against the live schema too, because it is the constraint most likely
to be dropped by someone adding a column and rewriting the table.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from registry.arc.vocabularies import CONTENT_CLASSIFICATIONS, RECEIPT_EVENT_TYPES

ARC_SOURCE = Path(__file__).resolve().parents[2] / "registry" / "arc"

# Postgres does not store what you wrote. An `IN (...)` is normalised to
# `= ANY (ARRAY[...])` with every element cast, so `pg_get_constraintdef` returns
# `col = ANY (ARRAY['a'::text, 'b'::text])`. Parsing the source form instead of the
# stored one is a test that passes against the migration file and says nothing about
# the database -- which is the opposite of what this gate is for.
_ARRAY_LIST = re.compile(r"ARRAY\[([^\]]*)\]")
_MEMBER = re.compile(r"'([^']*)'")


def _members_from_constraint(definition: str) -> frozenset[str]:
    """Pull the enumerated members out of a stored `= ANY (ARRAY[...])` check."""
    match = _ARRAY_LIST.search(definition)
    if match is None:  # pragma: no cover - only on a malformed constraint
        msg = f"constraint does not enumerate an array: {definition!r}"
        raise AssertionError(msg)
    return frozenset(_MEMBER.findall(match.group(1)))


async def _constraint_def(session, table: str, name: str) -> str:
    result = await session.execute(
        text(
            "SELECT pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = :table AND c.conname = :name"
        ),
        {"table": table, "name": name},
    )
    row = result.scalar_one_or_none()
    assert row is not None, f"{table}.{name} does not exist in the live schema"
    return str(row)


@pytest.mark.asyncio
async def test_content_classification_enumerates_exactly_the_constants(db_session) -> None:
    definition = await _constraint_def(db_session, "arc_revisions", "ck_arc_revisions_content_classification")
    assert _members_from_constraint(definition) == CONTENT_CLASSIFICATIONS


@pytest.mark.asyncio
async def test_receipt_event_type_enumerates_exactly_the_constants(db_session) -> None:
    definition = await _constraint_def(db_session, "arc_receipt_events", "ck_arc_receipt_events_event_type")
    assert _members_from_constraint(definition) == RECEIPT_EVENT_TYPES


@pytest.mark.asyncio
async def test_regulated_content_cannot_be_stored_in_plaintext(db_session) -> None:
    # The cross-column rule exists so no write path can forget it. Asserting the
    # constraint is present is weaker than asserting an insert fails, but it does
    # not require constructing a valid revision row -- and a missing constraint is
    # the realistic regression, not a subtly wrong one.
    definition = await _constraint_def(db_session, "arc_revisions", "ck_arc_revisions_regulated_encrypted")
    assert "regulated" in definition
    assert "encrypted" in definition


def _bare_event_type_literals(tree: ast.AST) -> list[int]:
    """Line numbers where `event_type=` is given a string literal directly."""
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "event_type" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    found.append(keyword.value.lineno)
    return found


def test_no_bare_event_type_literals_in_arc() -> None:
    offenders: list[str] = []
    for path in ARC_SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno in _bare_event_type_literals(tree):
            offenders.append(f"{path}:{lineno}")

    assert not offenders, "event_type must come from registry.arc.vocabularies, not a literal: " + ", ".join(offenders)


def test_the_walker_actually_fires() -> None:
    # Without this, a walker that matches nothing passes the test above for the
    # wrong reason and keeps passing after someone breaks it.
    tree = ast.parse('_insert_event(session=s, event_type="jit_retrieval")')
    assert _bare_event_type_literals(tree) == [1]
