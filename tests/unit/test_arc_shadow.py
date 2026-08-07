"""Unit tests for `registry/arc/service/shadow.py`: no database.

`overlay_candidate_set`'s own inclusion/exclusion proof lives in `tests/
conformance/test_arc_corpus_lifecycle_filter.py` (it needs to sit beside
the real-Postgres half of that claim). This file covers the rest of the
module: building `Directive`/`ApplicabilityRule` from a candidate's frozen
JSON semantics, the governing-rule tie-break, the delta-code diff, and
envelope-item matching -- all pure, all exercisable without a session.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from registry.arc.service.selection import ScopedDirective, SelectionResult
from registry.arc.service.shadow import (
    DeltaMatch,
    ShadowDelta,
    ShadowError,
    candidate_entries,
    diff_selection,
    match_deltas_to_envelope,
)
from registry.arc.types import (
    ApplicabilityRule,
    AuthorityScope,
    Directive,
    DirectiveType,
    ResolutionStatus,
)

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _directive_dict(directive_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "directive_id": str(directive_id),
        "directive_type": "citation_only",
        "source_anchor": "anchor-1",
        "conflict_subject_digest": None,
        "conflict_key_schema_version": None,
        "conflict_key_namespace": None,
        "conflict_key_subject_selector": None,
        "conflict_key_operation": None,
        "conflict_key_action_class": None,
        "conflict_key_target_selector": None,
        "conflict_key_modality": None,
        "conflict_key_constraint_operator": None,
        "conflict_key_constraint_value": None,
        "delegable_exception": False,
        "satisfaction_mode": None,
        "verification_max_age_seconds": None,
        "accepted_verifier_classes": None,
        "required_evidence_type": None,
    }
    base.update(overrides)
    return base


def _rule_dict(
    rule_id: uuid.UUID, *, scope: str = "global", is_mandatory: bool = False, **overrides: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "rule_id": str(rule_id),
        "scope": scope,
        "is_mandatory": is_mandatory,
        "target_tenant_id": None,
        "capability_ids": None,
        "capability_labels": None,
        "domain_ids": None,
        "task_kinds": None,
        "action_classes": None,
        "environments": None,
        "data_sensitivity_tiers": None,
        "effective_from": None,
        "effective_until": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# candidate_entries: building Directive/ApplicabilityRule from frozen JSON.
# ---------------------------------------------------------------------------


def test_candidate_entries_pairs_every_directive_with_the_governing_rule() -> None:
    """Two rules on the candidate, one mandatory-tenant and one optional-
    global: every directive must pair with the *mandatory* one (mandatory
    beats non-mandatory at any scope, matching `corpus.py`'s own tie-break)."""
    revision_id = uuid.uuid4()
    mandatory_rule_id = uuid.uuid4()
    optional_rule_id = uuid.uuid4()
    directive_id_a = uuid.uuid4()
    directive_id_b = uuid.uuid4()
    semantics = {
        "directives": [_directive_dict(directive_id_a), _directive_dict(directive_id_b)],
        "applicability": [
            _rule_dict(optional_rule_id, scope="global", is_mandatory=False),
            _rule_dict(mandatory_rule_id, scope="tenant", is_mandatory=True, target_tenant_id=str(uuid.uuid4())),
        ],
    }
    entries = candidate_entries(semantics, revision_id=revision_id, effective_from=_NOW)
    assert len(entries) == 2
    for _directive, rule, effective_from in entries:
        assert rule.rule_id == mandatory_rule_id
        assert rule.is_mandatory is True
        assert effective_from == _NOW


def test_candidate_entries_raises_on_zero_rules() -> None:
    """A candidate always carries at least one applicability rule by the
    time it reaches shadow evaluation -- proposal validation rejects an
    empty list before submission. Reaching this with zero rules is a
    defensive backstop, not a normal path."""
    semantics: dict[str, Any] = {"directives": [], "applicability": []}
    with pytest.raises(ShadowError):
        candidate_entries(semantics, revision_id=uuid.uuid4(), effective_from=_NOW)


# ---------------------------------------------------------------------------
# diff_selection: the ADR 041 Sec.4 delta-code reduction.
# ---------------------------------------------------------------------------


def _result(
    *,
    status: ResolutionStatus = ResolutionStatus.READY,
    mandatory: tuple[ScopedDirective, ...] = (),
    optional: tuple[ScopedDirective, ...] = (),
    conflicts: tuple[object, ...] = (),
) -> SelectionResult:
    return SelectionResult(
        status=status,
        mandatory=mandatory,
        optional=optional,
        blocked_reasons=(),
        degraded_reasons=(),
        conflicts=conflicts,  # type: ignore[arg-type]
        applied_exception_ids=(),
        selection_engine_version="test",
    )


def _scoped(directive_id: uuid.UUID) -> ScopedDirective:
    revision_id = uuid.uuid4()
    directive = Directive(
        directive_id=directive_id,
        revision_id=revision_id,
        directive_type=DirectiveType.CITATION_ONLY,
        source_anchor="a",
    )
    rule = ApplicabilityRule(rule_id=uuid.uuid4(), revision_id=revision_id, scope=AuthorityScope.GLOBAL)
    return ScopedDirective(directive=directive, rule=rule, revision_effective_from=_NOW)


def test_diff_selection_reports_newly_selected_for_each_new_directive() -> None:
    new_directive = _scoped(uuid.uuid4())
    baseline = _result()
    overlay = _result(optional=(new_directive,))
    delta = diff_selection(baseline, overlay)
    assert delta.delta_codes == ("newly_selected",)


def test_diff_selection_reports_no_longer_selected_for_each_removed_directive() -> None:
    removed_directive = _scoped(uuid.uuid4())
    baseline = _result(optional=(removed_directive,))
    overlay = _result()
    delta = diff_selection(baseline, overlay)
    assert delta.delta_codes == ("no_longer_selected",)


def test_diff_selection_reports_both_directions_independently() -> None:
    kept = _scoped(uuid.uuid4())
    removed = _scoped(uuid.uuid4())
    added = _scoped(uuid.uuid4())
    baseline = _result(optional=(kept, removed))
    overlay = _result(optional=(kept, added))
    delta = diff_selection(baseline, overlay)
    assert sorted(delta.delta_codes) == sorted(["newly_selected", "no_longer_selected"])


def test_diff_selection_reports_mandatory_block_added_only_on_the_blocked_transition() -> None:
    baseline = _result(status=ResolutionStatus.READY)
    overlay = _result(status=ResolutionStatus.BLOCKED)
    delta = diff_selection(baseline, overlay)
    assert "mandatory_block_added" in delta.delta_codes
    assert "mandatory_block_removed" not in delta.delta_codes


def test_diff_selection_reports_mandatory_block_removed_only_on_the_unblocked_transition() -> None:
    baseline = _result(status=ResolutionStatus.BLOCKED)
    overlay = _result(status=ResolutionStatus.READY)
    delta = diff_selection(baseline, overlay)
    assert "mandatory_block_removed" in delta.delta_codes
    assert "mandatory_block_added" not in delta.delta_codes


def test_diff_selection_reports_no_block_transition_when_both_stay_blocked() -> None:
    baseline = _result(status=ResolutionStatus.BLOCKED)
    overlay = _result(status=ResolutionStatus.BLOCKED)
    delta = diff_selection(baseline, overlay)
    assert "mandatory_block_added" not in delta.delta_codes
    assert "mandatory_block_removed" not in delta.delta_codes


def test_diff_selection_reports_conflict_changed_when_conflict_sets_differ() -> None:
    baseline = _result(conflicts=())
    overlay = _result(conflicts=("some-conflict",))
    delta = diff_selection(baseline, overlay)
    assert "conflict_changed" in delta.delta_codes


def test_diff_selection_reports_nothing_when_both_selections_agree() -> None:
    shared = _scoped(uuid.uuid4())
    baseline = _result(optional=(shared,))
    overlay = _result(optional=(shared,))
    delta = diff_selection(baseline, overlay)
    assert delta.delta_codes == ()


# ---------------------------------------------------------------------------
# match_deltas_to_envelope: per-occurrence explained/unexplained/ambiguous.
# ---------------------------------------------------------------------------


def _item(item_id: str, delta_code: str, **predicate_overrides: Any) -> dict[str, Any]:
    predicate: dict[str, Any] = {
        "task_kind": None,
        "requested_action_classes": None,
        "environment": None,
        "data_sensitivity_tier": None,
        "capability_ids": None,
        "domain_ids": None,
    }
    predicate.update(predicate_overrides)
    return {"item_id": item_id, "delta_code": delta_code, "class_predicate": predicate}


def _manifest_class(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_kind": ["code_change"],
        "requested_action_classes": ["merge"],
        "environment": ["production"],
        "data_sensitivity_tier": ["internal"],
        "capability_ids": None,
        "domain_ids": None,
    }
    base.update(overrides)
    return base


def test_match_explains_a_delta_that_matches_exactly_one_item() -> None:
    items = [_item("item-1", "newly_selected", task_kind=["code_change"])]
    manifest_class = _manifest_class()
    matches = match_deltas_to_envelope(ShadowDelta(delta_codes=("newly_selected",)), manifest_class, items)
    assert matches == (DeltaMatch(delta_code="newly_selected", item_id="item-1"),)


def test_match_leaves_unexplained_when_no_item_matches() -> None:
    items = [_item("item-1", "newly_selected", task_kind=["deployment"])]
    manifest_class = _manifest_class(task_kind=["code_change"])
    matches = match_deltas_to_envelope(ShadowDelta(delta_codes=("newly_selected",)), manifest_class, items)
    assert matches == (DeltaMatch(delta_code="newly_selected", item_id=None),)


def test_match_leaves_unexplained_when_two_items_ambiguously_match() -> None:
    """ADR 041 Sec.4: "matches exactly one envelope item" -- two items
    with non-overlapping delta codes could each independently match the
    same wide-open predicate; when both share this delta code, the
    occurrence is ambiguous and therefore unexplained, never silently
    picked as the first match."""
    items = [_item("item-1", "newly_selected"), _item("item-2", "newly_selected")]
    manifest_class = _manifest_class()
    matches = match_deltas_to_envelope(ShadowDelta(delta_codes=("newly_selected",)), manifest_class, items)
    assert matches == (DeltaMatch(delta_code="newly_selected", item_id=None),)


def test_match_ignores_items_with_a_different_delta_code() -> None:
    items = [_item("item-1", "no_longer_selected")]
    manifest_class = _manifest_class()
    matches = match_deltas_to_envelope(ShadowDelta(delta_codes=("newly_selected",)), manifest_class, items)
    assert matches == (DeltaMatch(delta_code="newly_selected", item_id=None),)
