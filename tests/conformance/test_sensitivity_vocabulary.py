"""The handling scale is one closed set, and the database agrees with it.

`contextplane.sensitivity` replaced six Python spellings of the same four names
and the three separate tuples that carried their order. **Nothing in `tests/`
asserted that any of them agreed** — which is why this gate exists. It is the
reason the consolidation is worth doing at all: without it, one module replaces
six copies and nothing stops a seventh.

Five `CHECK` constraints enumerate this scale in SQL. Each is read from the live
schema through `pg_get_constraintdef` rather than from the migration's source
text, because a migration that was written and never applied passes a source
comparison and says nothing about what the database will enforce.

The `Literal` is checked against the tuple too. It is the one form that cannot be
derived — a `Literal` will not take a runtime value — so it is the one place a
fifth tier could be added to the scale and forgotten on the type.
"""

from __future__ import annotations

import re
import typing

import pytest
from sqlalchemy import text

from contextplane.sensitivity import (
    MOST_RESTRICTIVE,
    TIER_SET,
    TIERS,
    Tier,
    UnknownSensitivityTier,
    at_most,
    is_tier,
    rank,
)

#: Postgres normalises `IN (...)` to `= ANY (ARRAY[...])` with every element cast,
#: so the stored form is parsed rather than the written one.
_ARRAY_LIST = re.compile(r"ARRAY\[([^\]]*)\]")
_MEMBER = re.compile(r"'([^']*)'")

#: Every column that enumerates this scale in SQL, and the constraint that does it.
_CONSTRAINED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("context_external_references", "ck_external_reference_classification"),
    ("claim_derivations", "ck_derivation_classification"),
    ("derivation_evidence_links", "ck_evidence_classification"),
    ("external_signals", "ck_external_signal_classification"),
    ("derivative_registrations", "ck_derivative_classification"),
)


def _members(definition: str) -> frozenset[str]:
    match = _ARRAY_LIST.search(definition)
    if match is None:  # pragma: no cover - only on a malformed constraint
        raise AssertionError(f"constraint does not enumerate an array: {definition!r}")
    return frozenset(_MEMBER.findall(match.group(1)))


async def _constraint_def(session, table: str, name: str) -> str:
    row = (
        await session.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = :table AND c.conname = :name"
            ),
            {"table": table, "name": name},
        )
    ).scalar_one_or_none()
    assert row is not None, f"{table}.{name} does not exist in the live schema"
    return str(row)


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "constraint"), _CONSTRAINED_COLUMNS)
async def test_the_database_enumerates_exactly_this_scale(db_session, table: str, constraint: str) -> None:
    assert _members(await _constraint_def(db_session, table, constraint)) == TIER_SET


@pytest.mark.asyncio
async def test_every_sql_enumeration_of_the_scale_is_covered(db_session) -> None:
    """The gate is only worth having if it knows about every column.

    A sixth constrained column added without a line here would leave the scale
    free to drift in the one place this test is not looking, and nothing else
    would notice.
    """
    found = (
        await db_session.execute(
            text(
                "SELECT t.relname, c.conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE '%classification%'"
                "  AND pg_get_constraintdef(c.oid) LIKE '%restricted%'"
            )
        )
    ).all()

    assert {(row[0], row[1]) for row in found} == set(_CONSTRAINED_COLUMNS)


def test_the_literal_agrees_with_the_tuple() -> None:
    """The one form that cannot be derived, so the one that can be forgotten."""
    assert set(typing.get_args(Tier)) == set(TIERS)


def test_membership_derives_from_the_order() -> None:
    assert TIER_SET == frozenset(TIERS)
    assert len(TIERS) == len(TIER_SET), "a tier is listed twice"


def test_the_scale_is_ordered_least_to_most_sensitive() -> None:
    assert [rank(tier) for tier in TIERS] == sorted(rank(tier) for tier in TIERS)
    assert MOST_RESTRICTIVE == TIERS[-1]
    assert rank(MOST_RESTRICTIVE) == len(TIERS) - 1


def test_ranking_an_unknown_name_raises_rather_than_guessing() -> None:
    """The module does not decide what an unreadable label means.

    Two call sites treat one as maximally sensitive and one refuses to compare;
    both are right about the question they ask, so the vocabulary answers
    neither.
    """
    with pytest.raises(UnknownSensitivityTier) as caught:
        rank("regulated")

    assert "regulated" in str(caught.value)
    assert "public" in str(caught.value), "the refusal names the scale it does have"


def test_is_tier_is_total_where_rank_is_not() -> None:
    assert is_tier("internal") is True
    assert is_tier("regulated") is False
    assert is_tier(None) is False
    assert is_tier(3) is False


def test_at_most_compares_within_the_scale() -> None:
    assert at_most("public", "restricted") is True
    assert at_most("restricted", "restricted") is True
    assert at_most("restricted", "public") is False


def test_at_most_refuses_a_ceiling_it_cannot_rank() -> None:
    """A comparison against a ceiling nobody can rank was never posed."""
    with pytest.raises(UnknownSensitivityTier):
        at_most("public", "regulated")


def test_arcs_scale_is_a_different_scale() -> None:
    """`regulated` is not a spelling of `restricted`.

    They are not merged: ARC's carries a cross-column CHECK requiring encrypted
    storage that this one does not, so merging would give the encryption rule
    reach beyond ARC.
    """
    from contextplane.arc.vocabularies import CONTENT_CLASSIFICATIONS

    assert CONTENT_CLASSIFICATIONS != TIER_SET
    assert "regulated" in CONTENT_CLASSIFICATIONS
    assert "regulated" not in TIER_SET
    assert "restricted" in TIER_SET
    assert "restricted" not in CONTENT_CLASSIFICATIONS
