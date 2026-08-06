"""Conformance gate for the drafter model decision artifact and the
evaluation fixture corpus behind it.

`registry/arc/drafter/model_decision.json` is a committed decision, not a
runtime toggle: no model-backed endpoint may serve until it records
`outcome == "accepted"` with every evaluation gate passed. This module checks
three things, in the order a defect in any one of them should be caught:

1. The artifact itself is the closed shape the decision gate requires
   (`load_drafter_model_decision`, shared with the startup guard in
   `registry.wiring.services` so the two can never validate different
   shapes of the same file).
2. Every one of the seven minimum non-negotiable gates is named -- not a
   subset, not a superset, and not zero because nothing ran.
3. The evaluation-fixture corpus each gate result cites is provably live:
   the manifest's declared case count for each gate equals the count of
   case files actually on disk, *before* anything about a case's content is
   trusted. A truncated or emptied fixture directory fails here rather than
   letting a `fixture_case_count` in the decision artifact quietly go stale
   against nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from registry.wiring.services import (
    _DRAFTER_DECISION_PATH,
    load_drafter_model_decision,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVAL_FIXTURE_ROOT = _REPO_ROOT / "tests" / "fixtures" / "arc_drafter_eval"
_EVAL_MANIFEST = _EVAL_FIXTURE_ROOT / "manifest.json"

# The seven minimum non-negotiable gates named in the decision-gate contract.
# Hardcoded rather than read off the decision artifact or the fixture
# manifest -- either of those losing a gate would otherwise validate itself
# by definition, the same reasoning `test_arc_authoring_fixtures.py` applies
# to its sixteen profile directories.
_REQUIRED_GATE_IDS = frozenset(
    {
        "source_identity_preservation",
        "mandatory_directive_recall",
        "source_backed_citation_integrity",
        "closed_vocabulary_fidelity",
        "lifecycle_authority_containment",
        "prompt_injection_containment",
        "deterministic_output_shape",
    }
)

_CASE_KIND_VOCABULARY = frozenset({"benign", "hostile"})


def _load_eval_manifest() -> dict[str, Any]:
    return json.loads(_EVAL_MANIFEST.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The decision artifact's own shape.
# ---------------------------------------------------------------------------


def test_decision_artifact_exists_and_is_the_closed_shape() -> None:
    decision = load_drafter_model_decision()
    assert decision["outcome"] in ("accepted", "human_only")


def test_decision_artifact_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        load_drafter_model_decision(tmp_path / "does_not_exist.json")


def test_decision_artifact_rejects_an_unclosed_shape(tmp_path: Path) -> None:
    bad = tmp_path / "model_decision.json"
    bad.write_text(json.dumps({"outcome": "human_only", "gate_results": [{"gate_id": "x", "passed": False}]}))
    with pytest.raises(ValueError, match="not the closed shape"):
        load_drafter_model_decision(bad)


def test_decision_artifact_rejects_an_unknown_outcome(tmp_path: Path) -> None:
    decision = load_drafter_model_decision()
    mutated = {**decision, "outcome": "provisionally_accepted"}
    bad = tmp_path / "model_decision.json"
    bad.write_text(json.dumps(mutated))
    with pytest.raises(ValueError, match="not one of"):
        load_drafter_model_decision(bad)


def test_decision_artifact_rejects_a_gate_result_missing_passed(tmp_path: Path) -> None:
    decision = load_drafter_model_decision()
    mutated = {**decision, "gate_results": [{"gate_id": "source_identity_preservation"}]}
    bad = tmp_path / "model_decision.json"
    bad.write_text(json.dumps(mutated))
    with pytest.raises(ValueError, match="boolean 'passed'"):
        load_drafter_model_decision(bad)


# ---------------------------------------------------------------------------
# 2. Every one of the seven gates is named, and consistently with an
#    accepted-vs-human_only outcome.
# ---------------------------------------------------------------------------


def test_decision_artifact_names_all_seven_gates_exactly_once() -> None:
    decision = load_drafter_model_decision()
    gate_ids = [g["gate_id"] for g in decision["gate_results"]]
    assert set(gate_ids) == _REQUIRED_GATE_IDS, f"decision artifact does not name exactly the seven gates: {gate_ids}"
    assert len(gate_ids) == len(set(gate_ids)), "a gate_id is repeated in gate_results"


def test_human_only_outcome_requires_no_gate_marked_passed() -> None:
    """An `accepted` outcome asserted alongside a failed gate is a
    self-contradictory artifact -- and the reverse is the honest shape a
    `human_only` verdict must carry when no candidate was ever evaluated:
    nothing here is claimed to have passed."""
    decision = load_drafter_model_decision()
    if decision["outcome"] != "human_only":
        pytest.skip("committed decision is not human_only; the accepted-path invariant is checked elsewhere")
    passed = [g["gate_id"] for g in decision["gate_results"] if g["passed"]]
    assert not passed, f"outcome is human_only but gate_results claims a pass for: {passed}"


def test_accepted_outcome_requires_every_gate_passed() -> None:
    decision = load_drafter_model_decision()
    if decision["outcome"] != "accepted":
        pytest.skip("committed decision is not accepted; the human_only invariant is checked elsewhere")
    failed = [g["gate_id"] for g in decision["gate_results"] if not g["passed"]]
    assert not failed, f"outcome is accepted but gate_results records a failing gate: {failed}"


def test_every_gate_result_carries_a_nonempty_detail() -> None:
    """A `passed: false` entry with no explanation is unreviewable -- the
    same reasoning as every other refusal report in this codebase: an
    operator (or reviewer) must be able to act on what they are shown."""
    decision = load_drafter_model_decision()
    for entry in decision["gate_results"]:
        assert (
            isinstance(entry.get("detail"), str) and entry["detail"].strip()
        ), f"gate_results entry {entry.get('gate_id')!r} has no detail explaining its passed={entry.get('passed')}"


# ---------------------------------------------------------------------------
# 3. The evaluation fixture corpus is provably live -- cardinality first.
# ---------------------------------------------------------------------------


def test_eval_manifest_declares_all_seven_gates_with_a_nonzero_case_each() -> None:
    manifest = _load_eval_manifest()
    gate_ids = {g["gate_id"] for g in manifest["gates"]}
    assert gate_ids == _REQUIRED_GATE_IDS, f"eval manifest does not name exactly the seven gates: {gate_ids}"
    for gate in manifest["gates"]:
        cases = gate["cases"]
        assert len(cases) > 0, f"{gate['gate_id']}: eval manifest declares zero cases"
        kinds = {c["kind"] for c in cases}
        assert kinds <= _CASE_KIND_VOCABULARY, f"{gate['gate_id']}: case kind outside {_CASE_KIND_VOCABULARY}: {kinds}"
        assert "benign" in kinds, f"{gate['gate_id']}: no benign case"
        assert "hostile" in kinds, f"{gate['gate_id']}: no hostile case"


def test_eval_case_files_on_disk_match_the_manifest_count_exactly() -> None:
    """The count the manifest claims must equal the count that exists on
    disk, checked before any case file's content is read. An emptied or
    truncated fixture directory fails here, not by silently scoring a
    (fabricated) perfect result over nothing."""
    manifest = _load_eval_manifest()
    for gate in manifest["gates"]:
        gate_dir = _EVAL_FIXTURE_ROOT / gate["gate_id"]
        on_disk = sorted(gate_dir.glob("*.json"))
        assert len(on_disk) == len(gate["cases"]), (
            f"{gate['gate_id']}: manifest declares {len(gate['cases'])} case(s) but "
            f"{len(on_disk)} case file(s) exist under {gate_dir}"
        )
        declared_paths = {c["case_path"] for c in gate["cases"]}
        on_disk_paths = {f"{gate['gate_id']}/{f.name}" for f in on_disk}
        assert declared_paths == on_disk_paths, f"{gate['gate_id']}: manifest case_path set does not match disk"


def test_eval_case_files_are_well_formed_and_self_consistent() -> None:
    manifest = _load_eval_manifest()
    for gate in manifest["gates"]:
        for case in gate["cases"]:
            case_file = _EVAL_FIXTURE_ROOT / case["case_path"]
            payload = json.loads(case_file.read_text(encoding="utf-8"))
            assert payload["case_id"] == case["case_id"], f"{case_file}: case_id does not match manifest"
            assert payload["gate_id"] == gate["gate_id"], f"{case_file}: gate_id does not match manifest"
            assert payload["kind"] == case["kind"], f"{case_file}: kind does not match manifest"
            assert payload["source_evidence_id"], f"{case_file}: missing source_evidence_id"
            assert payload["source_anchor"], f"{case_file}: missing source_anchor"
            assert (
                isinstance(payload.get("expected"), dict) and payload["expected"]
            ), f"{case_file}: missing a nonempty 'expected' block"
            assert (
                isinstance(payload.get("rationale"), str) and payload["rationale"].strip()
            ), f"{case_file}: missing a nonempty 'rationale'"


def test_decision_gate_results_fixture_counts_are_reproducible_from_disk() -> None:
    """The decision artifact's `fixture_case_count` per gate is not an
    assertion the artifact makes about itself -- it must equal what the
    fixture manifest and the files on disk actually contain right now."""
    decision = load_drafter_model_decision()
    manifest = _load_eval_manifest()
    case_counts = {gate["gate_id"]: len(gate["cases"]) for gate in manifest["gates"]}

    for entry in decision["gate_results"]:
        gate_id = entry["gate_id"]
        assert gate_id in case_counts, f"decision artifact references gate {gate_id!r} with no eval-manifest entry"
        assert entry["fixture_case_count"] == case_counts[gate_id], (
            f"{gate_id}: decision artifact records fixture_case_count={entry['fixture_case_count']} but the "
            f"eval manifest + files on disk currently show {case_counts[gate_id]}"
        )


def test_decision_artifact_path_matches_the_documented_repo_layout() -> None:
    assert _DRAFTER_DECISION_PATH.relative_to(_REPO_ROOT) == Path("registry/arc/drafter/model_decision.json")
