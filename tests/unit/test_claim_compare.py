"""Whether two values disagree — and the cases where the answer is "cannot tell".

Every rule here is a judgement about consequences, not a mathematical fact, so
the tests pin the judgements. A detected disagreement marks both claims contested,
and a contested claim cannot be promoted and always needs review, which no
reviewer can undo when both values were true. So the bias is toward compatible,
and the tests hold that bias in both directions: the knife-edges are asserted on
both sides, because a tolerance nobody has pinned is a tolerance that drifts.
"""

from __future__ import annotations

import datetime

import pytest

from registry.service.claim_compare import (
    COMPATIBLE,
    INCOMPATIBLE,
    UNDECIDABLE,
    intervals_overlap,
    values_compatible,
)

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


def _day(n: int) -> datetime.datetime:
    return _NOW + datetime.timedelta(days=n)


# --- booleans and exact integers ---------------------------------------------


def test_booleans_disagree_when_different() -> None:
    assert values_compatible("boolean", True, False) == INCOMPATIBLE
    assert values_compatible("boolean", True, True) == COMPATIBLE


def test_an_integer_threshold_has_no_tolerance() -> None:
    """One byte of difference is a request one side accepts and the other
    refuses. There is no rounding to absorb."""
    assert values_compatible("bytes", 1048576, 1048577) == INCOMPATIBLE
    assert values_compatible("bytes", 1048576, 1048576) == COMPATIBLE


def test_a_boolean_is_not_an_integer() -> None:
    """`bool` subclasses `int`, so True would otherwise compare as 1."""
    assert values_compatible("integer", True, 1) == UNDECIDABLE


# --- durations: the tolerance and its edges -----------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (900, 900, COMPATIBLE),
        (900, 901, COMPATIBLE),
        # Exactly at the 2% tolerance for 900: floor(18.0) == 18.
        (900, 918, COMPATIBLE),
        # One second past it. This pair is the whole tolerance decision.
        (900, 919, INCOMPATIBLE),
        (900, 1800, INCOMPATIBLE),
        # The floor keeps small durations from having a zero tolerance.
        (2, 3, COMPATIBLE),
        (2, 5, INCOMPATIBLE),
        # A month-scale objective tolerates proportionally more.
        (2_592_000, 2_600_000, COMPATIBLE),
    ],
)
def test_duration_tolerance_boundaries(left: int, right: int, expected: str) -> None:
    assert values_compatible("duration_seconds", left, right) == expected


def test_the_duration_tolerance_is_relative_not_absolute() -> None:
    """A single absolute figure is wrong at both ends of a range spanning two
    seconds to thirty days: it is either the whole of the small value or nothing
    at all against the large one."""
    small_gap_rejected = values_compatible("duration_seconds", 2, 5) == INCOMPATIBLE
    same_gap_accepted_when_large = values_compatible("duration_seconds", 2_592_000, 2_592_003) == COMPATIBLE
    assert small_gap_rejected and same_gap_accepted_when_large


def test_prose_where_a_duration_belongs_is_undecidable_not_a_disagreement() -> None:
    """A malformed value is a validation gap. Calling it a disagreement would
    manufacture a contested claim out of a bug."""
    assert values_compatible("duration_seconds", 900, "about fifteen minutes") == UNDECIDABLE


# --- decimals: exact, deliberately -------------------------------------------


def test_trailing_zeros_are_formatting_not_disagreement() -> None:
    """Stored as a string so nothing is lost on the way in, which means the same
    number can arrive written two ways."""
    assert values_compatible("decimal", "0.999", "0.9990") == COMPATIBLE
    assert values_compatible("decimal", "0.9", "0.90000") == COMPATIBLE


def test_three_nines_against_two_is_a_disagreement() -> None:
    """A tenfold difference in error budget, and the most consequential
    distinction the ontology can express. Any tolerance useful enough to absorb
    rounding would swallow this."""
    assert values_compatible("decimal", "0.999", "0.99") == INCOMPATIBLE


def test_a_small_availability_difference_is_still_a_disagreement() -> None:
    """0.999 against 0.995 is a fivefold budget difference. This is why the type
    has no tolerance at all."""
    assert values_compatible("decimal", "0.999", "0.995") == INCOMPATIBLE


def test_an_unparseable_decimal_is_undecidable() -> None:
    assert values_compatible("decimal", "banana", "0.99") == UNDECIDABLE


# --- timestamps: exact, for a different reason --------------------------------


def test_the_same_instant_written_two_ways_agrees() -> None:
    assert values_compatible("timestamp_utc", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00.000Z") == COMPATIBLE


def test_two_instants_disagree_with_no_tolerance() -> None:
    """Unlike a duration, an instant is not a measurement — it is a boundary
    somebody chose, and two sources choosing differently disagree."""
    assert values_compatible("timestamp_utc", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z") == INCOMPATIBLE


def test_an_unparseable_timestamp_is_undecidable() -> None:
    assert values_compatible("timestamp_utc", "yesterday", "2026-01-01T00:00:00Z") == UNDECIDABLE


# --- folded text --------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Platform", "platform"),
        (" platform ", "platform"),
        ("platform  team", "platform team"),
        ("PLATFORM", "platform"),
    ],
)
def test_case_and_spacing_are_noise_in_a_team_name(left: str, right: str) -> None:
    assert values_compatible("string", left, right) == COMPATIBLE


def test_different_teams_disagree() -> None:
    assert values_compatible("string", "platform", "billing") == INCOMPATIBLE


def test_folding_is_safe_only_because_case_sensitive_predicates_are_set_valued() -> None:
    """`getUser` and `getuser` really are two different operations. This
    comparison would call them equal — which is fine only because the predicate
    naming operations is set-valued and never reaches this test. A single-valued
    case-sensitive predicate would need its own value type."""
    from registry.service.claim_ontology import ONTOLOGY  # noqa: PLC0415
    from registry.service.global_vocabulary import CARDINALITY_MULTI  # noqa: PLC0415

    by_name = {seed.value: seed for seed in ONTOLOGY}
    assert by_name["exposes_operation"].value_cardinality == CARDINALITY_MULTI


# --- URLs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://X.example/a", "https://x.example/a"),
        ("HTTPS://x.example/a", "https://x.example/a"),
        ("https://x.example:443/a", "https://x.example/a"),
        ("http://x.example:80/a", "http://x.example/a"),
        ("https://x.example/a/", "https://x.example/a"),
        ("https://x.example/a#decision", "https://x.example/a"),
    ],
)
def test_url_parts_that_carry_no_meaning_are_folded(left: str, right: str) -> None:
    assert values_compatible("url", left, right) == COMPATIBLE


def test_path_case_is_preserved_because_it_carries_meaning() -> None:
    assert values_compatible("url", "https://x.example/A", "https://x.example/a") == INCOMPATIBLE


def test_a_query_string_is_compared_exactly() -> None:
    assert values_compatible("url", "https://x.example/a?v=1", "https://x.example/a?v=2") == INCOMPATIBLE


def test_a_relative_reference_is_undecidable() -> None:
    assert values_compatible("url", "/runbooks/a", "https://x.example/a") == UNDECIDABLE


# --- version ranges -----------------------------------------------------------


def test_a_tighter_range_does_not_contradict_a_looser_one() -> None:
    """2.1 satisfies both. One source simply knows more, and flagging that would
    punish a source for being precise."""
    assert values_compatible("version_predicate", ">=2.0", ">=2.1") == COMPATIBLE


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (">=2.0,<3.0", ">=3.0,<4.0"),
        ("^1.4", "^2.0"),
        ("1.2.3", "2.0.0"),
        ("<2.0", ">=3.0"),
    ],
)
def test_disjoint_ranges_disagree(left: str, right: str) -> None:
    assert values_compatible("version_predicate", left, right) == INCOMPATIBLE


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (">=2.0,<3.0", ">=2.5,<2.9"),
        ("^1.4", "~1.4.2"),
        ("1.2.3", ">=1.0"),
        ("", ">=3.0"),
    ],
)
def test_overlapping_ranges_agree(left: str, right: str) -> None:
    assert values_compatible("version_predicate", left, right) == COMPATIBLE


def test_an_unconstrained_range_agrees_with_everything() -> None:
    assert values_compatible("version_predicate", "", "^9.9") == COMPATIBLE


def test_a_range_the_grammar_cannot_parse_is_undecidable() -> None:
    """Alternation and hyphen ranges are documented as unsupported. Guessing at
    one would compare a range nobody expanded."""
    assert values_compatible("version_predicate", ">=1.0 || >=2.0", ">=3.0") == UNDECIDABLE


def test_adjacent_exclusive_ranges_are_disjoint() -> None:
    """`<2.0` ends where `>=2.0` begins, and no version is in both."""
    assert values_compatible("version_predicate", "<2.0", ">=2.0") == INCOMPATIBLE


# --- entity references --------------------------------------------------------


def test_the_same_entity_under_two_names_agrees() -> None:
    """One claim names it by identifier, another through an external system. Both
    were resolved on the write path, so this compares identities."""
    assert (
        values_compatible(
            "entity_ref",
            "github:acme/auth",
            "11111111-1111-1111-1111-111111111111",
            left_entity_id="abc",
            right_entity_id="abc",
        )
        == COMPATIBLE
    )


def test_two_resolved_but_different_entities_disagree() -> None:
    assert values_compatible("entity_ref", "a", "b", left_entity_id="x", right_entity_id="y") == INCOMPATIBLE


def test_two_unresolved_references_agree_when_the_text_matches() -> None:
    """So two connectors naming an entity the catalog lacks still count as
    agreeing. Identical text is the only evidence available."""
    assert values_compatible("entity_ref", "github:acme/x", "github:acme/x") == COMPATIBLE


def test_two_unresolved_and_different_references_are_undecidable() -> None:
    """Different text naming nothing the catalog holds could be two names for one
    thing. Not enough to call a disagreement."""
    assert values_compatible("entity_ref", "github:acme/x", "gitlab:acme/x") == UNDECIDABLE


def test_one_resolved_and_one_not_is_undecidable() -> None:
    """The unresolved reference may name the same entity under a name the catalog
    has not learned yet."""
    assert values_compatible("entity_ref", "a", "b", left_entity_id="x") == UNDECIDABLE


# --- prose --------------------------------------------------------------------


def test_prose_is_never_compared() -> None:
    """A paragraph needs a model to compare, and a model's verdict is neither
    reproducible nor auditable. Prose claims cannot be promoted anyway, so a
    disagreement would cost attention and buy nothing."""
    assert values_compatible("prose", "we shipped it", "we did not ship it") == UNDECIDABLE
    assert values_compatible("prose", "same", "same") == UNDECIDABLE


def test_an_unknown_value_type_is_undecidable() -> None:
    """A type this module has never heard of must not be guessed at."""
    assert values_compatible("quantum_flux", 1, 2) == UNDECIDABLE


# --- interval overlap ---------------------------------------------------------


def test_two_open_ended_intervals_overlap() -> None:
    assert intervals_overlap(_day(0), None, _day(5), None)


def test_a_closed_interval_before_another_does_not_overlap() -> None:
    assert not intervals_overlap(_day(0), _day(5), _day(10), _day(15))


def test_a_clean_handover_does_not_overlap() -> None:
    """One claim ending exactly when another begins is a succession, not a
    disagreement. This is what lets an ownership change be recorded without
    contesting either claim."""
    assert not intervals_overlap(_day(0), _day(5), _day(5), None)


def test_overlapping_intervals_overlap() -> None:
    assert intervals_overlap(_day(0), _day(10), _day(5), _day(15))


def test_an_open_ended_interval_overlaps_everything_after_it_starts() -> None:
    assert intervals_overlap(_day(0), None, _day(100), _day(101))
    assert not intervals_overlap(_day(100), None, _day(0), _day(50))
