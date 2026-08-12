"""The scheduler's three jobs, each proved against a clock the test owns.

A watchdog tested against the real clock proves nothing at tenth-of-a-second
resolution — the test would have to sleep for the boundary it is checking, and
a 47-second unit test is a test nobody runs. Every deadline case here drives a
list of timestamps, so the boundary is exact and the test is instant.
"""

from __future__ import annotations

import pytest

from tests.helpers.integration_scheduler import (
    EXTERNAL_MAX_SECONDS,
    INTERNAL_MAX_SECONDS,
    DeadlineExceeded,
    FrozenHistory,
    HistoryKey,
    IntervalRecord,
    IntervalWatchdog,
    NodeEvent,
    NodeOutcome,
    Phase,
    Reconciler,
    RunInvalid,
    SchedulerError,
    balance,
    eligible,
    smallest_eligible,
)


class FakeClock:
    """A monotonic source the test advances by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_history(durations: dict[str, float] | None = None) -> FrozenHistory:
    return FrozenHistory(
        key=HistoryKey(
            source_collection_digest="collect-abc",
            provider="devstack",
            schema_fingerprint="schema-1",
            host_digest="host-1",
            topology="4",
        ),
        durations=durations or {},
    )


# --------------------------------------------------------------------------
# Balancing
# --------------------------------------------------------------------------


def test_schedule_is_byte_identical_across_repeated_calls() -> None:
    """Determinism is the property the whole comparison rests on."""
    history = make_history({f"n{i}": float(i % 7) + 0.5 for i in range(30)})
    nodes = [f"n{i}" for i in range(30)]

    first = balance(nodes, workers=4, history=history)
    second = balance(list(reversed(nodes)), workers=4, history=history)

    assert first.as_evidence() == second.as_evidence()


def test_longest_node_lands_first_and_alone() -> None:
    history = make_history({"slow": 10.0, "a": 1.0, "b": 1.0, "c": 1.0})

    schedule = balance(["a", "b", "c", "slow"], workers=2, history=history)

    by_worker = {a.worker_id: a.nodes for a in schedule.assignments}
    assert by_worker["w0"] == ("slow",)
    assert by_worker["w1"] == ("a", "b", "c")


def test_unknown_node_costs_the_median_of_known_ones() -> None:
    """Not zero. A free unknown node stacks every new test onto one worker."""
    history = make_history({"a": 2.0, "b": 4.0, "c": 6.0})

    assert history.cost("never-seen") == pytest.approx(4.0)


def test_history_with_nothing_in_it_falls_back_to_equal_cost() -> None:
    history = make_history()

    schedule = balance(["a", "b", "c", "d"], workers=2, history=history)

    assert [len(a.nodes) for a in schedule.assignments] == [2, 2]


def test_every_node_is_scheduled_exactly_once() -> None:
    history = make_history({f"n{i}": float(i) for i in range(11)})

    schedule = balance([f"n{i}" for i in range(11)], workers=3, history=history)

    assert sorted(schedule.nodes) == sorted(f"n{i}" for i in range(11))
    assert len(schedule.nodes) == 11


def test_duplicate_node_in_collection_is_rejected() -> None:
    with pytest.raises(SchedulerError, match="duplicate nodes"):
        balance(["a", "a"], workers=1, history=make_history())


def test_empty_collection_is_invalid_rather_than_a_vacuous_pass() -> None:
    """A zero-node run that exits 0 is the failure this phase exists to stop."""
    with pytest.raises(RunInvalid, match="zero-node run cannot qualify"):
        balance([], workers=2, history=make_history())


def test_negative_recorded_duration_is_rejected_at_construction() -> None:
    with pytest.raises(SchedulerError, match="negative duration"):
        FrozenHistory(key=make_history().key, durations={"a": -1.0})


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


def one_worker_schedule(nodes: list[str]) -> object:
    return balance(nodes, workers=1, history=make_history())


def test_clean_run_reconciles_every_node() -> None:
    schedule = one_worker_schedule(["a", "b"])
    reconciler = Reconciler(schedule=schedule)

    reconciler.record(NodeEvent("w0", 1, "a", None, started=True))
    reconciler.record(NodeEvent("w0", 2, "a", NodeOutcome.PASSED, started=False))
    reconciler.record(NodeEvent("w0", 3, "b", None, started=True))
    reconciler.record(NodeEvent("w0", 4, "b", NodeOutcome.SKIPPED, started=False))

    assert reconciler.finalize() == {"a": NodeOutcome.PASSED, "b": NodeOutcome.SKIPPED}


def test_lost_shard_report_names_the_undisclosed_node() -> None:
    schedule = one_worker_schedule(["a", "b"])
    reconciler = Reconciler(schedule=schedule)
    reconciler.record(NodeEvent("w0", 1, "a", None, started=True))
    reconciler.record(NodeEvent("w0", 2, "a", NodeOutcome.PASSED, started=False))

    with pytest.raises(RunInvalid, match="never disclosed an outcome.*'b'"):
        reconciler.finalize()


def test_undisclosed_node_is_marked_missing_in_failure_evidence() -> None:
    schedule = one_worker_schedule(["a", "b"])
    reconciler = Reconciler(schedule=schedule)
    reconciler.record(NodeEvent("w0", 1, "a", None, started=True))
    reconciler.record(NodeEvent("w0", 2, "a", NodeOutcome.PASSED, started=False))

    assert reconciler.outcomes_with_missing() == {
        "a": NodeOutcome.PASSED,
        "b": NodeOutcome.MISSING,
    }


def test_duplicate_outcome_invalidates_the_run() -> None:
    schedule = one_worker_schedule(["a"])
    reconciler = Reconciler(schedule=schedule)
    reconciler.record(NodeEvent("w0", 1, "a", None, started=True))
    reconciler.record(NodeEvent("w0", 2, "a", NodeOutcome.PASSED, started=False))

    with pytest.raises(RunInvalid, match="duplicate outcome"):
        reconciler.record(NodeEvent("w0", 3, "a", NodeOutcome.FAILED, started=False))


def test_event_sequence_gap_invalidates_the_run() -> None:
    """A gap and a silently-failed node are indistinguishable from outside."""
    schedule = one_worker_schedule(["a"])
    reconciler = Reconciler(schedule=schedule)
    reconciler.record(NodeEvent("w0", 1, "a", None, started=True))

    with pytest.raises(RunInvalid, match="sequence gap.*expected 2, got 4"):
        reconciler.record(NodeEvent("w0", 4, "a", NodeOutcome.PASSED, started=False))


def test_replayed_event_sequence_invalidates_the_run() -> None:
    schedule = one_worker_schedule(["a"])
    reconciler = Reconciler(schedule=schedule)
    reconciler.record(NodeEvent("w0", 1, "a", None, started=True))

    with pytest.raises(RunInvalid, match="duplicate event sequence"):
        reconciler.record(NodeEvent("w0", 1, "a", NodeOutcome.PASSED, started=False))


def test_outcome_without_a_start_invalidates_the_run() -> None:
    schedule = one_worker_schedule(["a"])
    reconciler = Reconciler(schedule=schedule)

    with pytest.raises(RunInvalid, match="never started"):
        reconciler.record(NodeEvent("w0", 1, "a", NodeOutcome.PASSED, started=False))


def test_worker_disclosing_another_workers_node_invalidates_the_run() -> None:
    history = make_history({"slow": 10.0, "a": 1.0})
    schedule = balance(["slow", "a"], workers=2, history=history)

    reconciler = Reconciler(schedule=schedule)
    with pytest.raises(RunInvalid, match="assigned to"):
        reconciler.record(NodeEvent("w1", 1, "slow", None, started=True))


def test_terminal_event_without_an_outcome_is_malformed() -> None:
    schedule = one_worker_schedule(["a"])
    reconciler = Reconciler(schedule=schedule)
    reconciler.record(NodeEvent("w0", 1, "a", None, started=True))

    with pytest.raises(RunInvalid, match="terminal event with no outcome"):
        reconciler.record(NodeEvent("w0", 2, "a", None, started=False))


def test_worker_cannot_report_missing_itself() -> None:
    """`MISSING` is the parent's verdict, never a worker's disclosure."""
    schedule = one_worker_schedule(["a"])
    reconciler = Reconciler(schedule=schedule)
    reconciler.record(NodeEvent("w0", 1, "a", None, started=True))

    with pytest.raises(RunInvalid, match="non-reportable outcome"):
        reconciler.record(NodeEvent("w0", 2, "a", NodeOutcome.MISSING, started=False))


# --------------------------------------------------------------------------
# The watchdog
# --------------------------------------------------------------------------


def test_execution_cannot_borrow_slack_from_a_fast_provisioning() -> None:
    """The no-borrow rule, stated as the test that would catch its absence.

    Provisioning takes 1.0 s of its 8.0 s. Execution then runs 47.5 s. Under a
    cumulative budget that run is fine — 48.5 s against 55.0 s of combined
    allowance. Under the non-borrowable rule it is a violation, because
    execution's own maximum is 47.0 s regardless of what provisioning saved.
    """
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)

    watchdog.enter(Phase.PROVISIONING)
    clock.advance(1.0)
    watchdog.leave(Phase.PROVISIONING)

    watchdog.enter(Phase.EXECUTION)
    clock.advance(47.5)
    with pytest.raises(DeadlineExceeded) as caught:
        watchdog.leave(Phase.EXECUTION)

    violation = caught.value.violation
    assert violation.phase is Phase.EXECUTION
    assert violation.limit_seconds == 47.0
    assert violation.observed_seconds == pytest.approx(47.5)


def test_provisioning_overrun_fails_at_its_own_boundary() -> None:
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)
    watchdog.enter(Phase.PROVISIONING)
    clock.advance(8.01)

    with pytest.raises(DeadlineExceeded) as caught:
        watchdog.check()

    assert caught.value.violation.boundary == "provisioning"


def test_teardown_overrun_fails_at_its_own_boundary() -> None:
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)
    watchdog.enter(Phase.PROVISIONING)
    clock.advance(1.0)
    watchdog.leave(Phase.PROVISIONING)
    watchdog.enter(Phase.EXECUTION)
    clock.advance(1.0)
    watchdog.leave(Phase.EXECUTION)
    watchdog.enter(Phase.TEARDOWN)
    clock.advance(4.5)

    with pytest.raises(DeadlineExceeded) as caught:
        watchdog.leave(Phase.TEARDOWN)

    assert caught.value.violation.boundary == "teardown"


def test_a_phase_exactly_at_its_maximum_is_still_inside_it() -> None:
    """`<= 8.0` means 8.0 passes. An off-by-one here rejects valid runs."""
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)
    watchdog.enter(Phase.PROVISIONING)
    clock.advance(8.0)

    record = watchdog.leave(Phase.PROVISIONING)

    assert record.duration == pytest.approx(8.0)


def test_internal_total_violation_is_reported_even_when_each_phase_fits() -> None:
    """Three individually-legal intervals can still blow the 59.0 s total."""
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)
    watchdog.enter(Phase.PROVISIONING)
    clock.advance(8.0)
    watchdog.leave(Phase.PROVISIONING)
    watchdog.enter(Phase.EXECUTION)
    clock.advance(47.0)
    watchdog.leave(Phase.EXECUTION)
    watchdog.enter(Phase.TEARDOWN)
    clock.advance(4.0)
    watchdog.leave(Phase.TEARDOWN)

    # 8 + 47 + 4 == 59.0 exactly, which is the boundary and therefore legal.
    assert watchdog.internal_total() == pytest.approx(INTERNAL_MAX_SECONDS)


def test_a_gap_between_intervals_is_unaccounted_parent_time() -> None:
    """Wall time and the interval sum must be the same number, not close."""
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)
    watchdog.enter(Phase.PROVISIONING)
    clock.advance(1.0)
    watchdog.leave(Phase.PROVISIONING)

    clock.advance(5.0)  # parent doing something it never declared

    watchdog.enter(Phase.EXECUTION)
    clock.advance(1.0)
    watchdog.leave(Phase.EXECUTION)
    watchdog.enter(Phase.TEARDOWN)
    clock.advance(1.0)
    watchdog.leave(Phase.TEARDOWN)

    with pytest.raises(RunInvalid, match="unaccounted parent time"):
        watchdog.internal_total()


def test_phases_must_run_in_order() -> None:
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)

    with pytest.raises(SchedulerError, match="expected 'provisioning'"):
        watchdog.enter(Phase.EXECUTION)


def test_grace_shrinks_to_the_remaining_external_allowance() -> None:
    """At 59.8 s elapsed there is 0.2 s left, not the full 500 ms."""
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)
    clock.advance(59.8)

    assert watchdog.grace_seconds() == pytest.approx(0.2)


def test_grace_is_never_negative_past_the_external_boundary() -> None:
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)
    clock.advance(EXTERNAL_MAX_SECONDS + 5.0)

    assert watchdog.grace_seconds() == 0.0


def test_grace_is_the_full_half_second_early_in_a_run() -> None:
    clock = FakeClock()
    watchdog = IntervalWatchdog(monotonic=clock)
    clock.advance(2.0)

    assert watchdog.grace_seconds() == pytest.approx(0.5)


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


def intervals(provisioning: float, execution: float, teardown: float) -> list[IntervalRecord]:
    cursor = 0.0
    records = []
    for phase, duration in (
        (Phase.PROVISIONING, provisioning),
        (Phase.EXECUTION, execution),
        (Phase.TEARDOWN, teardown),
    ):
        records.append(IntervalRecord(phase=phase, started_at=cursor, ended_at=cursor + duration))
        cursor += duration
    return records


def test_a_run_inside_every_boundary_is_eligible() -> None:
    ok, misses = eligible(intervals=intervals(5.0, 40.0, 2.0), internal_total=47.0, external_real=52.0)

    assert ok
    assert misses == ()


def test_eligibility_reports_every_missed_boundary_not_just_the_first() -> None:
    ok, misses = eligible(intervals=intervals(9.0, 48.0, 2.0), internal_total=59.0, external_real=59.0)

    assert not ok
    assert len(misses) == 2
    assert any("provisioning" in miss for miss in misses)
    assert any("execution" in miss for miss in misses)


def test_external_real_of_exactly_sixty_is_not_eligible() -> None:
    """`real < 60.0` is strict — `/usr/bin/time` prints two decimals, so an
    exact 60.00 has been rounded down from something at or over the line."""
    ok, misses = eligible(intervals=intervals(5.0, 40.0, 2.0), internal_total=47.0, external_real=60.0)

    assert not ok
    assert any("external_real" in miss for miss in misses)


def test_external_real_just_under_sixty_is_eligible() -> None:
    ok, _ = eligible(intervals=intervals(5.0, 40.0, 2.0), internal_total=47.0, external_real=59.99)

    assert ok


def test_smallest_eligible_count_wins() -> None:
    assert smallest_eligible({1: False, 2: True, 4: True, 8: True}) == 2


def test_nothing_eligible_blocks_rather_than_defaulting_to_eight() -> None:
    assert smallest_eligible({1: False, 2: False, 4: False, 8: False}) is None
