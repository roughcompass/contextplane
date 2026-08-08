"""Unit tests for the claim-predicate -> canonical-target mapping.

Pure functions over the static `ONTOLOGY` table -- no session, no DB, no
mocking. Coverage:
- `target_for`: attribute-valued predicates, entity_ref (edge) predicates,
  the barred prose predicate (no target), and a predicate absent from the
  ontology entirely.
- `multi_valued` reflects each seed's own declared cardinality, not a
  blanket default -- a single-valued predicate and a multi-valued one of
  the same target kind must disagree.
- `unmapped_reason`: the two distinct "why not" reasons (barred value type
  vs. not in the ontology at all), and `None` for a predicate that *does*
  have a target -- a caller must never be told a reason for something
  promotable.
- The barred value type never appears in `TARGETS` at all, not merely as a
  `target_for` miss (proves `_build()` actually excludes it, not that some
  other check happens to intercept it later).
"""

from __future__ import annotations

from contextplane.service.memory import promotion_targets as pt

# ---------------------------------------------------------------------------
# target_for
# ---------------------------------------------------------------------------


def test_target_for_a_single_valued_attribute_predicate() -> None:
    """`owned_by_team` is a single-valued (`value_cardinality="single"`)
    string attribute in the ontology."""
    target = pt.target_for("owned_by_team")
    assert target is not None
    assert target.kind == pt.TARGET_ATTRIBUTE
    assert target.key == "owned_by_team"
    assert target.multi_valued is False


def test_target_for_a_multi_valued_attribute_predicate() -> None:
    """`deployment_environment` has no cardinality override, so it keeps
    the seed default (multi) -- a capability is in staging and production
    at once."""
    target = pt.target_for("deployment_environment")
    assert target is not None
    assert target.kind == pt.TARGET_ATTRIBUTE
    assert target.multi_valued is True


def test_target_for_an_entity_ref_predicate_is_an_edge() -> None:
    """`depends_on` is `entity_ref`-typed, the one value type that maps to
    an edge rather than an attribute."""
    target = pt.target_for("depends_on")
    assert target is not None
    assert target.kind == pt.TARGET_EDGE
    assert target.key == "depends_on"
    # entity_ref predicates carry no cardinality override in the ontology
    # today, so they keep the seed default (multi).
    assert target.multi_valued is True


def test_target_for_the_barred_prose_predicate_is_none() -> None:
    """`session_summary` is the one prose-valued predicate. Prose has no
    typed canonical target, so it must never resolve to one."""
    assert pt.target_for("session_summary") is None


def test_target_for_a_predicate_outside_the_ontology_is_none() -> None:
    assert pt.target_for("not_a_real_predicate") is None


def test_barred_predicate_is_absent_from_targets_not_merely_unmatched() -> None:
    """Proves `_build()` itself excludes the barred value type, rather than
    some downstream check happening to intercept a `target_for` miss that
    was actually present in the table."""
    assert "session_summary" not in pt.TARGETS


# ---------------------------------------------------------------------------
# unmapped_reason
# ---------------------------------------------------------------------------


def test_unmapped_reason_is_none_for_a_predicate_that_has_a_target() -> None:
    """A caller must never be handed a "why not" reason for something that
    is, in fact, promotable."""
    assert pt.unmapped_reason("owned_by_team") is None


def test_unmapped_reason_names_prose_as_the_specific_barred_reason() -> None:
    assert pt.unmapped_reason("session_summary") == pt.UNMAPPED_PROSE


def test_unmapped_reason_for_a_predicate_outside_the_ontology_names_that_distinctly() -> None:
    """Distinct from the barred-prose reason: "never mapped" and "can never
    be mapped" call for different responses from a curator looking at an
    unpromotable claim."""
    reason = pt.unmapped_reason("not_a_real_predicate")
    assert reason is not None
    assert reason != pt.UNMAPPED_PROSE
    assert "not_a_real_predicate" in reason
    assert "not in the ontology" in reason
