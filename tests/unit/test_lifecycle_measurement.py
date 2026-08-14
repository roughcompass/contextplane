"""Failure-mode tests for the lifecycle comparison.

Every test here builds a complete synthetic six-run comparison and then breaks
exactly one thing about it. That shape is deliberate: a comparison that is
already invalid for three reasons cannot demonstrate that the fourth check
works, and a test that asserts on a partially-built comparison tends to pass for
the wrong reason. Each case below is a way a wrong conclusion could survive to a
reader, and the assertion is that it does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers.lifecycle_measurement import (
    AFTER,
    BEFORE,
    Classification,
    DeltaEntry,
    IncompleteEvidence,
    ModuleTiming,
    Outcome,
    Provenance,
    ProvenanceMismatch,
    Run,
    StaleEvidence,
    balanced_critical_path,
    build_comparison,
    classify_delta,
    cohort_digest,
    load_bundle,
    manifest_checksum,
    outcome_consistency_failures,
    serial_critical_path,
    write_bundle,
)

BEFORE_COMMIT = "a" * 40
AFTER_COMMIT = "b" * 40
MODULES = ("tests/integration/test_one.py", "tests/integration/test_two.py")


def make_provenance(**overrides: object) -> Provenance:
    base: dict[str, object] = {
        "provider": "devstack",
        "host_digest": "host-1",
        "worker_topology": 1,
        "warm_state_digest": "warm-1",
        "schema_fingerprint": "schema-1",
        "canonical_schema_digest": "canonical-1",
        "cohort_digest": "",
        "duration_history_digest": "history-1",
    }
    base.update(overrides)
    return Provenance(**base)  # type: ignore[arg-type]


def make_run(side: str, commit: str, index: int, seconds: float, provenance: Provenance) -> Run:
    """One run whose two modules sum to ``seconds`` of measured work."""
    half = seconds / 2
    return Run(
        side=side,
        commit=commit,
        run_index=index,
        provenance=provenance,
        timings=tuple(
            ModuleTiming(
                module_path=m,
                setup_seconds=half * 0.8,
                call_seconds=half * 0.15,
                teardown_seconds=half * 0.05,
            )
            for m in MODULES
        ),
        wall_seconds=seconds,
    )


@pytest.fixture
def cohort_path(tmp_path: Path) -> Path:
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps({"nodes": [{"modulePath": m, "nodeCount": 3} for m in MODULES]}))
    return path


@pytest.fixture
def provenance(cohort_path: Path) -> Provenance:
    return make_provenance(cohort_digest=cohort_digest(cohort_path))


@pytest.fixture
def six_runs(provenance: Provenance) -> list[Run]:
    """Three before runs at 60/62/64s and three after runs at 40/42/44s.

    The pessimistic formula reads 60 (fastest before) against 44 (slowest
    after), so the reduction is 16s — deliberately different from the 20s an
    average-to-average comparison would report, so a test cannot pass under both
    readings.
    """
    return [
        *(make_run(BEFORE, BEFORE_COMMIT, i, s, provenance) for i, s in enumerate((60.0, 62.0, 64.0), start=1)),
        *(make_run(AFTER, AFTER_COMMIT, i, s, provenance) for i, s in enumerate((40.0, 42.0, 44.0), start=1)),
    ]


def build(runs: list[Run], cohort_path: Path, **kwargs: object):
    return build_comparison(
        runs,
        before_commit=BEFORE_COMMIT,
        after_commit=AFTER_COMMIT,
        cohort_path=cohort_path,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# formula
# ---------------------------------------------------------------------------


def test_reduction_is_fastest_before_against_slowest_after(six_runs, cohort_path):
    comparison = build(six_runs, cohort_path)
    assert comparison.before_best_seconds == pytest.approx(60.0)
    assert comparison.after_worst_seconds == pytest.approx(44.0)
    assert comparison.reduction_seconds == pytest.approx(16.0)


def test_averaging_would_have_reported_a_larger_win(six_runs, cohort_path):
    """The pessimistic reading is strictly smaller than the mean-to-mean one."""
    comparison = build(six_runs, cohort_path)
    before_mean = sum(r.critical_path_seconds for r in comparison.before_runs) / 3
    after_mean = sum(r.critical_path_seconds for r in comparison.after_runs) / 3
    assert comparison.reduction_seconds < before_mean - after_mean


def test_serial_critical_path_is_the_sum_and_balancing_splits_it():
    durations = [("a", 10.0), ("b", 6.0), ("c", 4.0)]
    assert serial_critical_path(durations) == pytest.approx(20.0)
    assert balanced_critical_path(durations, 1) == pytest.approx(20.0)
    assert balanced_critical_path(durations, 2) == pytest.approx(10.0)


def test_balancer_is_deterministic_for_equal_durations():
    """Equal durations must not swap workers between runs and move the number."""
    durations = [("b", 5.0), ("a", 5.0), ("d", 5.0), ("c", 5.0)]
    assert balanced_critical_path(durations, 2) == balanced_critical_path(list(reversed(durations)), 2)


def test_outcome_needs_both_the_floor_and_the_ceiling(six_runs, cohort_path):
    cleared = build(six_runs, cohort_path, minimum_reduction_seconds=1.0, max_critical_path_seconds=47.0)
    assert cleared.outcome is Outcome.RETAINED

    ceiling_missed = build(six_runs, cohort_path, minimum_reduction_seconds=1.0, max_critical_path_seconds=40.0)
    assert ceiling_missed.outcome is Outcome.REVERTED
    assert any("ceiling" in reason for reason in ceiling_missed.refusal_reasons())

    floor_missed = build(six_runs, cohort_path, minimum_reduction_seconds=20.0, max_critical_path_seconds=47.0)
    assert floor_missed.outcome is Outcome.REVERTED
    assert any("floor" in reason for reason in floor_missed.refusal_reasons())


# ---------------------------------------------------------------------------
# mutation and checksum
# ---------------------------------------------------------------------------


def test_editing_a_raw_timing_moves_the_checksum(six_runs, cohort_path, provenance):
    original = build(six_runs, cohort_path).checksum()
    tampered = list(six_runs)
    tampered[-1] = make_run(AFTER, AFTER_COMMIT, 3, 30.0, provenance)
    assert build(tampered, cohort_path).checksum() != original


def test_checksum_ignores_the_outcome_label(six_runs, cohort_path):
    """Two comparisons over identical runs agree even when their gates differ.

    The checksum covers the inputs so a later reader can re-derive the verdict.
    If it covered the verdict, a bundle whose label was wrong for its numbers
    would still self-certify.
    """
    lenient = build(six_runs, cohort_path, minimum_reduction_seconds=1.0)
    strict = build(six_runs, cohort_path, minimum_reduction_seconds=99.0)
    assert lenient.outcome is not strict.outcome
    assert lenient.checksum() == strict.checksum()


def test_round_trip_through_a_bundle_preserves_the_checksum(six_runs, cohort_path, tmp_path):
    comparison = build(six_runs, cohort_path)
    path = tmp_path / "bundle.json"
    written = write_bundle(comparison, path)
    reloaded, recorded = load_bundle(path)
    assert recorded == written
    assert reloaded.checksum() == written
    assert reloaded.reduction_seconds == pytest.approx(comparison.reduction_seconds)


def test_a_hand_edited_bundle_no_longer_matches_its_checksum(six_runs, cohort_path, tmp_path):
    path = tmp_path / "bundle.json"
    write_bundle(build(six_runs, cohort_path), path)
    payload = json.loads(path.read_text())
    for run in payload["runs"]:
        if run["side"] == AFTER:
            run["timings"][0]["setupSeconds"] = 0.0
    path.write_text(json.dumps(payload))
    reloaded, recorded = load_bundle(path)
    assert reloaded.checksum() != recorded


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_a_side_measured_under_another_provider_is_refused(six_runs, cohort_path, provenance):
    crossed = list(six_runs)
    other = make_provenance(provider="testcontainers", cohort_digest=provenance.cohort_digest)
    crossed[3] = make_run(AFTER, AFTER_COMMIT, 1, 40.0, other)
    with pytest.raises(ProvenanceMismatch, match="provider"):
        build(crossed, cohort_path)


def test_a_side_measured_under_another_worker_topology_is_refused(six_runs, cohort_path, provenance):
    crossed = list(six_runs)
    other = make_provenance(worker_topology=4, cohort_digest=provenance.cohort_digest)
    crossed[3] = make_run(AFTER, AFTER_COMMIT, 1, 40.0, other)
    with pytest.raises(ProvenanceMismatch, match="workerTopology"):
        build(crossed, cohort_path)


def test_a_run_taken_against_another_cohort_is_refused(six_runs, cohort_path):
    crossed = list(six_runs)
    crossed[0] = make_run(BEFORE, BEFORE_COMMIT, 1, 60.0, make_provenance(cohort_digest="some-other-cohort"))
    with pytest.raises(ProvenanceMismatch):
        build(crossed, cohort_path)


# ---------------------------------------------------------------------------
# wrong-before and completeness
# ---------------------------------------------------------------------------


def test_a_before_run_taken_on_the_wrong_commit_is_refused(six_runs, cohort_path, provenance):
    wrong = list(six_runs)
    wrong[0] = make_run(BEFORE, "c" * 40, 1, 60.0, provenance)
    with pytest.raises(StaleEvidence, match="not on the expected"):
        build(wrong, cohort_path)


def test_an_uncommitted_candidate_cannot_be_compared_against_itself(six_runs, cohort_path):
    with pytest.raises(StaleEvidence, match="same commit"):
        build_comparison(
            six_runs,
            before_commit=BEFORE_COMMIT,
            after_commit=BEFORE_COMMIT,
            cohort_path=cohort_path,
        )


def test_a_short_side_is_refused(six_runs, cohort_path):
    with pytest.raises(IncompleteEvidence):
        build(six_runs[:-1], cohort_path)


def test_a_run_missing_a_cohort_module_is_refused(six_runs, cohort_path, provenance):
    short = list(six_runs)
    full = make_run(AFTER, AFTER_COMMIT, 3, 44.0, provenance)
    short[-1] = Run(
        side=AFTER,
        commit=AFTER_COMMIT,
        run_index=3,
        provenance=provenance,
        timings=full.timings[:1],
        wall_seconds=44.0,
    )
    with pytest.raises(IncompleteEvidence, match="missing"):
        build(short, cohort_path)


# ---------------------------------------------------------------------------
# false-reverted and classification
# ---------------------------------------------------------------------------


def entry(path: str, status: str = "M") -> DeltaEntry:
    return DeltaEntry(path=path, status=status, before_blob="0" * 40, after_blob="1" * 40)


SCOPE = ("tests/conftest.py", "tests/integration/conftest.py", "tests/helpers/auth_harness.py")


def test_a_reverted_label_with_implementation_files_present_is_refused():
    classification = classify_delta([entry("tests/helpers/async_db.py", "A")], scope=SCOPE)
    assert any("absent" in f for f in outcome_consistency_failures(Outcome.REVERTED, classification))


def test_a_reverted_label_with_a_touched_lifecycle_file_is_refused():
    classification = classify_delta([entry("tests/conftest.py")], scope=SCOPE)
    assert any("byte-identical" in f for f in outcome_consistency_failures(Outcome.REVERTED, classification))


def test_a_genuine_revert_touching_only_measurement_files_passes():
    classification = classify_delta(
        [
            entry("tests/helpers/lifecycle_measurement.py"),
            entry("scripts/verify_integration_lifecycle_comparison.py"),
        ],
        scope=SCOPE,
    )
    assert outcome_consistency_failures(Outcome.REVERTED, classification) == []


def test_a_retained_label_with_no_material_change_is_refused():
    """Retaining complexity requires that some complexity was actually added."""
    classification = classify_delta([entry("tests/helpers/lifecycle_measurement.py")], scope=SCOPE)
    assert any("material" in f for f in outcome_consistency_failures(Outcome.RETAINED, classification))


def test_a_change_outside_the_declared_scope_is_refused_under_either_label():
    classification = classify_delta([entry("contextplane/api/routers/capabilities.py")], scope=SCOPE)
    assert classification.out_of_scope == ("contextplane/api/routers/capabilities.py",)
    for outcome in (Outcome.RETAINED, Outcome.REVERTED):
        assert any("outside the declared scope" in f for f in outcome_consistency_failures(outcome, classification))


def test_classification_is_static_rather_than_content_derived():
    """A path's category comes from the contract, not from what it contains."""
    classification = classify_delta(
        [entry("tests/helpers/lifecycle_measurement.py"), entry("tests/helpers/async_db.py", "A")],
        scope=SCOPE,
    )
    assert classification.measurement_only == ("tests/helpers/lifecycle_measurement.py",)
    assert classification.implementation_additions == ("tests/helpers/async_db.py",)


def test_manifest_checksum_moves_when_a_blob_changes():
    first = manifest_checksum([entry("tests/conftest.py")])
    second = manifest_checksum(
        [DeltaEntry(path="tests/conftest.py", status="M", before_blob="0" * 40, after_blob="2" * 40)]
    )
    assert first != second


def test_manifest_checksum_is_order_independent():
    """The manifest is sorted before hashing, so discovery order cannot move it."""
    a, b = entry("tests/conftest.py"), entry("tests/integration/conftest.py")
    assert manifest_checksum(sorted([a, b], key=lambda e: e.path)) == manifest_checksum(
        sorted([b, a], key=lambda e: e.path)
    )


def test_empty_classification_reports_nothing_in_any_bucket():
    empty = Classification((), (), (), ())
    assert empty.as_dict() == {
        "measurementOnly": [],
        "lifecycleOwned": [],
        "implementationAdditions": [],
        "outOfScope": [],
    }
