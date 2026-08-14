"""Deterministic node scheduling, outcome reconciliation, and the interval watchdog.

Three concerns that look separable and are not. Balancing decides which worker
owns which node; reconciliation decides whether every node it dispatched came
back exactly once; the watchdog decides whether any of it happened inside its
budget. A run is valid only if all three agree, and each one of them is a way
for a measurement to be wrong while still producing a number.

The scheduler is deliberately free of I/O. Everything here takes its clock and
its duration history as arguments, because the boundaries this module enforces
are measured in tenths of a second and a test that cannot control the clock
cannot prove a boundary. The process-group signalling that a violation triggers
lives with the caller that owns those processes; this module decides *that* a
boundary was crossed and which one, never how to kill anything.

Balancing is longest-processing-time-first over frozen duration history. LPT is
not chosen for optimality — it is chosen because it is deterministic given a
fixed input, and a schedule that changes between two runs of the same commit
makes the two runs incomparable. The history is snapshotted once per sequence
for the same reason: a history that absorbs run 1's timings would hand run 2 a
different schedule, and the three measured runs would no longer be measuring
one thing.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

# The five boundaries. Every measured run must independently meet all of them;
# the three intervals are ordered, non-overlapping, and sum exactly to the
# internal total, which is why 8.0 + 47.0 + 4.0 and 59.0 are the same number
# stated two ways rather than two independent budgets.
PROVISIONING_MAX_SECONDS: Final = 8.0
EXECUTION_MAX_SECONDS: Final = 47.0
TEARDOWN_MAX_SECONDS: Final = 4.0
INTERNAL_MAX_SECONDS: Final = 59.0
EXTERNAL_MAX_SECONDS: Final = 60.0

# How long a parent waits between SIGTERM and SIGKILL. "At most the smaller of
# 500 ms or the remaining external allowance" — the second term matters because
# a violation detected at 58.9 s internal has less than 500 ms of external
# budget left, and spending the full grace there would push the run past the
# external boundary while trying to clean up a run that has already failed.
TERMINATION_GRACE_SECONDS: Final = 0.5

# What a node costs when history has never seen it. The median of the known
# durations is the honest prior — it neither front-loads an unknown node onto
# one worker nor pretends it is free. With no history at all there is nothing
# to take a median of, so every node is equal and LPT degenerates to round
# robin, which is the correct behaviour for a first run.
_NO_HISTORY_FALLBACK_SECONDS: Final = 1.0


class SchedulerError(RuntimeError):
    """A scheduling, reconciliation, or deadline fault. Always fail-closed."""


class RunInvalid(SchedulerError):
    """The run cannot produce a measurement and must exit nonzero.

    Separate from `SchedulerError` so callers can distinguish "this run is
    void" from "this module was called wrongly". Both are fatal; only one of
    them is a bug.
    """


class Phase(StrEnum):
    """The three ordered intervals, in the only order they may occur."""

    PROVISIONING = "provisioning"
    EXECUTION = "execution"
    TEARDOWN = "teardown"


_PHASE_ORDER: Final = (Phase.PROVISIONING, Phase.EXECUTION, Phase.TEARDOWN)
_PHASE_MAXIMUM: Final = {
    Phase.PROVISIONING: PROVISIONING_MAX_SECONDS,
    Phase.EXECUTION: EXECUTION_MAX_SECONDS,
    Phase.TEARDOWN: TEARDOWN_MAX_SECONDS,
}


@dataclass(frozen=True)
class HistoryKey:
    """What makes two recorded durations comparable.

    A duration measured under a different provider, schema, host, or worker
    topology is a number about a different system. Keying history on all of
    them means a devstack history cannot silently schedule a testcontainers
    run, which would produce a valid-looking schedule built from inapplicable
    data.
    """

    source_collection_digest: str
    provider: str
    schema_fingerprint: str
    host_digest: str
    topology: str

    def as_evidence(self) -> dict[str, str]:
        return {
            "source_collection_digest": self.source_collection_digest,
            "provider": self.provider,
            "schema_fingerprint": self.schema_fingerprint,
            "host_digest": self.host_digest,
            "topology": self.topology,
        }


@dataclass(frozen=True)
class FrozenHistory:
    """A duration snapshot that cannot move while a sequence is running.

    Frozen at sequence start and passed to every run in that sequence. The
    alternative — a live history — makes run 2 schedule differently from run 1
    because run 1 happened, and three runs that scheduled differently are three
    measurements of three different systems.
    """

    key: HistoryKey
    durations: Mapping[str, float]

    def __post_init__(self) -> None:
        for node, seconds in self.durations.items():
            if seconds < 0.0:
                msg = f"negative duration for {node!r}: {seconds}"
                raise SchedulerError(msg)

    @property
    def fallback_seconds(self) -> float:
        """The cost assumed for a node history has not seen."""
        known = [value for value in self.durations.values() if value > 0.0]
        if not known:
            return _NO_HISTORY_FALLBACK_SECONDS
        return float(statistics.median(known))

    def cost(self, node: str) -> float:
        recorded = self.durations.get(node)
        if recorded is None or recorded <= 0.0:
            return self.fallback_seconds
        return float(recorded)

    def as_evidence(self) -> dict[str, object]:
        return {
            "key": self.key.as_evidence(),
            "node_count": len(self.durations),
            "fallback_seconds": round(self.fallback_seconds, 6),
        }


@dataclass(frozen=True)
class Assignment:
    """One worker's frozen share of the collection."""

    worker_id: str
    nodes: tuple[str, ...]
    predicted_seconds: float

    def as_evidence(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "nodes": list(self.nodes),
            "node_count": len(self.nodes),
            "predicted_seconds": round(self.predicted_seconds, 6),
        }


@dataclass(frozen=True)
class Schedule:
    """The exact aggregation a run must reproduce to be reconcilable."""

    assignments: tuple[Assignment, ...]
    history: FrozenHistory

    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(node for assignment in self.assignments for node in assignment.nodes)

    def worker_for(self, node: str) -> str:
        for assignment in self.assignments:
            if node in assignment.nodes:
                return assignment.worker_id
        msg = f"node not scheduled: {node!r}"
        raise SchedulerError(msg)

    def as_evidence(self) -> dict[str, object]:
        return {
            "history": self.history.as_evidence(),
            "assignments": [assignment.as_evidence() for assignment in self.assignments],
            "node_count": len(self.nodes),
        }


def balance(
    nodes: Iterable[str],
    *,
    workers: int,
    history: FrozenHistory,
) -> Schedule:
    """Assign every node exactly once, deterministically.

    Longest-processing-time-first: sort by predicted cost descending, then by
    node ID ascending, and hand each node to the least-loaded worker with the
    lowest index. Both tiebreaks are load-bearing — without them two runs of
    the same commit can produce different schedules, and the whole point of
    freezing history is that they must not.
    """
    if workers < 1:
        msg = f"worker count must be positive, got {workers}"
        raise SchedulerError(msg)

    ordered = tuple(nodes)
    duplicates = sorted({node for node in ordered if ordered.count(node) > 1})
    if duplicates:
        msg = f"collection contains duplicate nodes: {duplicates}"
        raise SchedulerError(msg)
    if not ordered:
        # Zero collection is a qualification failure, not an empty success. A
        # run that collected nothing and passed is the exact evidence this
        # phase exists to make impossible.
        msg = "collection is empty; a zero-node run cannot qualify"
        raise RunInvalid(msg)

    # Descending cost, ascending node ID. Sorting the key rather than the list
    # twice keeps the tiebreak explicit instead of relying on sort stability.
    ranked = sorted(ordered, key=lambda node: (-history.cost(node), node))

    loads = [0.0] * workers
    buckets: list[list[str]] = [[] for _ in range(workers)]
    for node in ranked:
        target = min(range(workers), key=lambda index: (loads[index], index))
        buckets[target].append(node)
        loads[target] += history.cost(node)

    assignments = tuple(
        Assignment(
            worker_id=_worker_id(index),
            # Sorted for a stable recorded assignment: dispatch order within a
            # worker is the worker's business, but the *evidence* must be
            # byte-identical for identical inputs.
            nodes=tuple(sorted(bucket)),
            predicted_seconds=loads[index],
        )
        for index, bucket in enumerate(buckets)
    )
    return Schedule(assignments=assignments, history=history)


def _worker_id(index: int) -> str:
    return f"w{index}"


class NodeOutcome(StrEnum):
    """Every terminal state a dispatched node may reach.

    `MISSING` is not something a worker reports — it is what the parent
    records for a node that was dispatched and never disclosed. It exists so
    that a lost shard report is a named outcome in the evidence rather than a
    node that quietly vanishes from the aggregation.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    MISSING = "missing"


_TERMINAL_REPORTED: Final = frozenset({NodeOutcome.PASSED, NodeOutcome.FAILED, NodeOutcome.ERROR, NodeOutcome.SKIPPED})


@dataclass(frozen=True)
class NodeEvent:
    """One disclosure about one node, in one worker's event stream."""

    worker_id: str
    sequence: int
    node: str
    outcome: NodeOutcome | None
    started: bool

    def as_evidence(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "sequence": self.sequence,
            "node": self.node,
            "outcome": self.outcome.value if self.outcome else None,
            "started": self.started,
        }


@dataclass
class Reconciler:
    """Proves every scheduled node was dispatched once and disclosed once.

    Fail-closed on every axis the TDD names: a duplicate event, a gap in a
    worker's sequence numbers, an outcome for a node that worker was never
    assigned, a second outcome for a node already resolved, an outcome with no
    start, or any node left undisclosed when the stream ends. None of these is
    recoverable by rescheduling — the run is void and exits nonzero.
    """

    schedule: Schedule
    _started: dict[str, NodeEvent] = field(default_factory=dict, init=False)
    _resolved: dict[str, NodeOutcome] = field(default_factory=dict, init=False)
    _last_sequence: dict[str, int] = field(default_factory=dict, init=False)
    _events: list[NodeEvent] = field(default_factory=list, init=False)

    def record(self, event: NodeEvent) -> None:
        expected_owner = self._owner_or_fail(event)
        self._check_sequence(event)

        if event.started:
            if event.node in self._started:
                msg = f"duplicate start for node {event.node!r}"
                raise RunInvalid(msg)
            self._started[event.node] = event
        else:
            if event.outcome is None:
                msg = f"malformed event for node {event.node!r}: terminal event with no outcome"
                raise RunInvalid(msg)
            if event.outcome not in _TERMINAL_REPORTED:
                msg = f"worker reported non-reportable outcome {event.outcome.value!r} for {event.node!r}"
                raise RunInvalid(msg)
            if event.node not in self._started:
                msg = f"outcome for node {event.node!r} that never started"
                raise RunInvalid(msg)
            if event.node in self._resolved:
                msg = f"duplicate outcome for node {event.node!r}"
                raise RunInvalid(msg)
            self._resolved[event.node] = event.outcome

        self._last_sequence[expected_owner] = event.sequence
        self._events.append(event)

    def _owner_or_fail(self, event: NodeEvent) -> str:
        owner = self.schedule.worker_for(event.node)
        if owner != event.worker_id:
            msg = f"worker {event.worker_id!r} disclosed node {event.node!r} assigned to {owner!r}"
            raise RunInvalid(msg)
        return owner

    def _check_sequence(self, event: NodeEvent) -> None:
        # Per-worker contiguous numbering from 1. A gap means an event was
        # lost in transit, and a lost event is indistinguishable from a node
        # that silently failed — so the gap itself is the failure.
        previous = self._last_sequence.get(event.worker_id, 0)
        if event.sequence == previous:
            msg = f"duplicate event sequence {event.sequence} from worker {event.worker_id!r}"
            raise RunInvalid(msg)
        if event.sequence != previous + 1:
            msg = (
                f"event sequence gap for worker {event.worker_id!r}: " f"expected {previous + 1}, got {event.sequence}"
            )
            raise RunInvalid(msg)

    def finalize(self) -> dict[str, NodeOutcome]:
        """Close the stream, or raise with every undisclosed node named."""
        undisclosed = sorted(set(self.schedule.nodes) - set(self._resolved))
        if undisclosed:
            msg = f"run invalid: {len(undisclosed)} node(s) never disclosed an outcome: {undisclosed}"
            raise RunInvalid(msg)
        return dict(self._resolved)

    def outcomes_with_missing(self) -> dict[str, NodeOutcome]:
        """Every node's outcome, with undisclosed nodes marked `MISSING`.

        For the failure path only. The parent still exits nonzero; this exists
        so the sealed artifact says which nodes were lost rather than leaving
        the aggregation short and unexplained.
        """
        resolved = dict(self._resolved)
        for node in self.schedule.nodes:
            resolved.setdefault(node, NodeOutcome.MISSING)
        return resolved

    @property
    def events(self) -> tuple[NodeEvent, ...]:
        return tuple(self._events)


@dataclass(frozen=True)
class IntervalRecord:
    """One completed interval, as the evidence records it."""

    phase: Phase
    started_at: float
    ended_at: float

    @property
    def duration(self) -> float:
        return self.ended_at - self.started_at

    def as_evidence(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "started_at": round(self.started_at, 6),
            "ended_at": round(self.ended_at, 6),
            "duration_seconds": round(self.duration, 6),
            "maximum_seconds": _PHASE_MAXIMUM[self.phase],
        }


@dataclass(frozen=True)
class DeadlineViolation:
    """Which boundary was crossed, and by how much."""

    phase: Phase | None
    boundary: str
    limit_seconds: float
    observed_seconds: float

    def as_evidence(self) -> dict[str, object]:
        return {
            "phase": self.phase.value if self.phase else None,
            "boundary": self.boundary,
            "limit_seconds": self.limit_seconds,
            "observed_seconds": round(self.observed_seconds, 6),
            "overrun_seconds": round(self.observed_seconds - self.limit_seconds, 6),
        }


class DeadlineExceeded(RunInvalid):
    """A boundary was crossed. Carries the violation for the artifact."""

    def __init__(self, violation: DeadlineViolation) -> None:
        super().__init__(
            f"{violation.boundary} boundary exceeded: "
            f"{violation.observed_seconds:.6f}s against {violation.limit_seconds}s"
        )
        self.violation = violation


class IntervalWatchdog:
    """Enforces the three non-borrowable intervals and the internal total.

    Non-borrowable is the whole design. A provisioning phase that finishes in
    3.0 s does not hand 5.0 s to execution — execution gets 47.0 s and not one
    tick more. Allowing the borrow would let a run bank slack from a cheap
    phase and hide an execution regression inside it, which is precisely the
    regression the 47.0 s boundary exists to catch.

    The clock is injected and must be monotonic. Wall-clock time is wrong here
    for the ordinary reason (it can step backwards) and for a specific one: the
    fingerprint date check elsewhere in this run keys on `TZ=UTC` dates, and a
    watchdog that disagreed with it about elapsed time would be unexplainable.
    """

    def __init__(self, *, monotonic: Callable[[], float], enforcing: bool = True) -> None:
        self._monotonic = monotonic
        # Measuring and enforcing are separate on purpose. The intervals are
        # recorded on every run because the evidence wants the numbers whatever
        # the run was for; the boundaries are *enforced* only for the sequences
        # they were set for. A developer running the tier to see whether it
        # passes is not making a performance claim, and failing that run on a
        # ceiling meant for a measured candidate would make the ordinary command
        # unusable while telling nobody anything true.
        self._enforcing = enforcing
        self._started_at = monotonic()
        self._records: list[IntervalRecord] = []
        self._current: Phase | None = None
        self._current_started_at: float | None = None
        self._violation: DeadlineViolation | None = None

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def enforcing(self) -> bool:
        return self._enforcing

    @enforcing.setter
    def enforcing(self, value: bool) -> None:
        """Settable because the decision arrives after timing has to start.

        Provisioning is measured from the first instruction, and whether this
        run is a measured candidate is only known once its control has been
        authenticated -- which happens inside provisioning.
        """
        self._enforcing = value

    @property
    def violation(self) -> DeadlineViolation | None:
        return self._violation

    @property
    def records(self) -> tuple[IntervalRecord, ...]:
        return tuple(self._records)

    def elapsed(self) -> float:
        return self._monotonic() - self._started_at

    def enter(self, phase: Phase) -> None:
        """Begin an interval, in order, closing the previous one."""
        expected_index = len(self._records)
        if expected_index >= len(_PHASE_ORDER):
            msg = f"cannot enter {phase.value!r}: all three intervals are complete"
            raise SchedulerError(msg)
        expected = _PHASE_ORDER[expected_index]
        if phase is not expected:
            msg = f"phases must run in order: expected {expected.value!r}, got {phase.value!r}"
            raise SchedulerError(msg)
        if self._current is not None:
            msg = f"cannot enter {phase.value!r} while {self._current.value!r} is open"
            raise SchedulerError(msg)

        self._current = phase
        self._current_started_at = self._monotonic()

    def check(self) -> None:
        """Raise if the open interval or the internal total is already over.

        Called by the parent's watchdog tick. It reports the *first* boundary
        crossed: an interval overrun is recorded as that interval's violation
        even when the internal total has also passed, because the interval is
        the one that identifies where the time went.
        """
        now = self._monotonic()
        if self._current is not None and self._current_started_at is not None:
            limit = _PHASE_MAXIMUM[self._current]
            observed = now - self._current_started_at
            if observed > limit:
                self._fail(
                    DeadlineViolation(
                        phase=self._current,
                        boundary=self._current.value,
                        limit_seconds=limit,
                        observed_seconds=observed,
                    )
                )
        internal = now - self._started_at
        if internal > INTERNAL_MAX_SECONDS:
            self._fail(
                DeadlineViolation(
                    phase=self._current,
                    boundary="internal_total",
                    limit_seconds=INTERNAL_MAX_SECONDS,
                    observed_seconds=internal,
                )
            )

    def leave(self, phase: Phase) -> IntervalRecord:
        """Close an interval, failing if it overran its own maximum."""
        if self._current is not phase or self._current_started_at is None:
            open_phase = self._current.value if self._current else None
            msg = f"cannot leave {phase.value!r}: open interval is {open_phase!r}"
            raise SchedulerError(msg)

        ended_at = self._monotonic()
        record = IntervalRecord(phase=phase, started_at=self._current_started_at, ended_at=ended_at)
        self._current = None
        self._current_started_at = None
        self._records.append(record)

        limit = _PHASE_MAXIMUM[phase]
        if record.duration > limit:
            self._fail(
                DeadlineViolation(
                    phase=phase,
                    boundary=phase.value,
                    limit_seconds=limit,
                    observed_seconds=record.duration,
                )
            )
        self.check()
        return record

    def grace_seconds(self) -> float:
        """How long to wait between SIGTERM and SIGKILL, right now.

        The smaller of 500 ms and whatever external allowance remains. Never
        negative: a run already past the external boundary gets no grace at
        all, which is the correct reading of "at most".
        """
        remaining_external = EXTERNAL_MAX_SECONDS - self.elapsed()
        return max(0.0, min(TERMINATION_GRACE_SECONDS, remaining_external))

    def internal_total(self) -> float:
        """Parent monotonic wall time — and the exact interval sum.

        The TDD requires these to be the same number. They are computed
        separately and reconciled here rather than one being derived from the
        other, because a parent that reports the sum of its own intervals can
        report 58.9 s for a run that took 70 s of wall time with a gap between
        two intervals nobody accounted for.
        """
        if len(self._records) != len(_PHASE_ORDER):
            msg = f"internal total requires all three intervals, have {len(self._records)}"
            raise SchedulerError(msg)
        interval_sum = sum(record.duration for record in self._records)
        wall = self._records[-1].ended_at - self._started_at
        # Tolerance is one clock tick, not a fudge factor: the two values are
        # the same quantity measured through the same monotonic source, so any
        # real difference is unaccounted parent time.
        if abs(wall - interval_sum) > 1e-6:
            msg = (
                f"unaccounted parent time: wall {wall:.6f}s against interval sum "
                f"{interval_sum:.6f}s; intervals must be contiguous"
            )
            raise RunInvalid(msg)
        return interval_sum

    def _fail(self, violation: DeadlineViolation) -> None:
        # The violation is recorded either way. A non-enforcing run that crossed
        # a boundary is a fact worth having in the artifact -- it is how a
        # candidate is shown to be too slow -- and only whether it *stops* the
        # run depends on what the run was for.
        if self._violation is None:
            self._violation = violation
        if self._enforcing:
            raise DeadlineExceeded(violation)


def eligible(
    *,
    intervals: Sequence[IntervalRecord],
    internal_total: float,
    external_real: float,
) -> tuple[bool, tuple[str, ...]]:
    """Does this run independently meet all five conditions?

    Returns the verdict and every boundary it missed rather than the first —
    a run that blows provisioning *and* execution should say so, because
    "which boundary" is the finding and stopping at the first hides half of it.

    Deliberately not an exception. Ineligibility is an ordinary outcome for a
    scale candidate: the contract requires complete evidence for all four
    counts and only one of them to be eligible.
    """
    misses: list[str] = []
    for record in intervals:
        limit = _PHASE_MAXIMUM[record.phase]
        if record.duration > limit:
            misses.append(f"{record.phase.value} {record.duration:.6f}s > {limit}s")
    if internal_total > INTERNAL_MAX_SECONDS:
        misses.append(f"internal_total {internal_total:.6f}s > {INTERNAL_MAX_SECONDS}s")
    # Strictly less than, unlike every other boundary. `/usr/bin/time` reports
    # `real` to two decimals, so a run printing exactly 60.00 has been rounded
    # from something at or above the boundary and cannot be shown to be under
    # it. The TDD writes this one as `< 60.0` for that reason.
    if not external_real < EXTERNAL_MAX_SECONDS:
        misses.append(f"external_real {external_real:.6f}s not < {EXTERNAL_MAX_SECONDS}s")
    return (not misses), tuple(misses)


def smallest_eligible(candidates: Mapping[int, bool]) -> int | None:
    """The committed default: the smallest worker count that qualifies.

    `None` when nothing through 8 qualifies, which is a block rather than a
    fallback to the largest count. A phase that silently shipped 8 workers
    because 1, 2, and 4 missed would be reporting a passing gate for a suite
    that does not fit its budget.
    """
    eligible_counts = sorted(count for count, ok in candidates.items() if ok)
    return eligible_counts[0] if eligible_counts else None
