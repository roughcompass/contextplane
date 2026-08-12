"""Compare integration lifecycle cost across two exact commits, conservatively.

A lifecycle change that shares an engine, an app, or an auth harness across a
module buys time by removing isolation that used to be free. That trade is only
worth making if the saving is real, so this module exists to make the saving
hard to overstate:

- **The comparison is pessimistic by construction.** Reduction is
  ``min(before) - max(after)``: the fastest run the old code managed against the
  slowest run the new code managed. Averaging two noisy distributions would
  report a win from variance alone, and the direction of that error always
  favours keeping the change.
- **Both sides carry provenance and it must match.** Host, provider, worker
  topology, warm-state inventory, schema fingerprint, canonical schema digest,
  cohort digest and duration-history digest are recorded per run and compared
  field by field. A measured "win" against a different provider is not a win.
- **Both sides must be complete.** Exactly the required number of runs, each
  covering every cohort module. A missing module silently shortens the side it
  is missing from, and shortening the *after* side is indistinguishable from
  succeeding.
- **The checksum covers the raw runs, never the verdict.** A later reader
  re-derives the outcome from the inputs rather than trusting the label, so a
  correct-inputs/wrong-conclusion bundle is catchable.

The critical path is *modeled*, not stopwatch wall time, and that is deliberate.
The bound it is compared against describes parallel execution — first dispatch
until final node outcome — but this measurement runs the cohort serially, so a
stopwatch here would answer a different question than the bound asks. Instead
each run records per-module durations, and the critical path is the longest
per-worker chain under a declared, recorded worker count. At one worker that is
exactly the serial sum, so nothing is invented; above one worker the same raw
runs can be re-derived later without re-measuring.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import SupportsFloat, SupportsInt, cast

#: A change is only worth its complexity if it clears this many seconds.
MINIMUM_REDUCTION_SECONDS = 1.0

#: No absolute ceiling by default. An absolute critical-path budget describes
#: *parallel* execution — first dispatch until final node outcome, with workers
#: overlapping — so it is enforced where overlapping workers actually exist. This
#: comparison runs serially at one worker, where such a budget is unreachable by
#: any optimization at one setting and met by no optimization at another; a gate
#: that swings on a parameter this measurement may not set is measuring the
#: parameter, not the change. Callers that genuinely have a ceiling pass one.
MAX_CRITICAL_PATH_SECONDS = math.inf

#: Both sides need exactly this many complete runs. Two would let one outlier
#: set the reported number; more would cost campaign time without sharpening a
#: min/max comparison.
REQUIRED_RUNS_PER_SIDE = 3

BEFORE = "before"
AFTER = "after"
_SIDES = (BEFORE, AFTER)

#: The only files whose presence in the source delta is expected regardless of
#: outcome. They measure the change; they are not the change.
MEASUREMENT_ONLY_PATHS = frozenset(
    {
        "scripts/lifecycle_measurement.py",
        "scripts/run_integration_lifecycle_comparison.py",
        "scripts/verify_integration_lifecycle_comparison.py",
        "tests/helpers/lifecycle_measurement.py",
        "tests/unit/test_lifecycle_measurement.py",
    }
)

#: Files that already exist and carry lifecycle behavior. A reverted outcome
#: requires every one of these to be byte-identical across the two commits.
LIFECYCLE_OWNED_PATHS = frozenset(
    {
        "tests/conftest.py",
        "tests/integration/conftest.py",
        "tests/helpers/auth_harness.py",
    }
)

#: Files that exist only because the optimization was attempted. A reverted
#: outcome requires all of them to be absent.
IMPLEMENTATION_ADDITION_PATHS = frozenset(
    {
        "tests/helpers/async_db.py",
        "tests/unit/test_async_db.py",
        "tests/unit/test_auth_harness.py",
        "tests/unit/test_integration_async_fixture_scopes.py",
    }
)


class LifecycleError(RuntimeError):
    """Base for every refusal this module raises."""


class ProvenanceMismatch(LifecycleError):
    """Two runs were taken under conditions that make them incomparable."""


class IncompleteEvidence(LifecycleError):
    """A side is missing runs, or a run is missing cohort modules."""


class StaleEvidence(LifecycleError):
    """Evidence was taken on a commit that is not the one being claimed."""


class ChecksumMismatch(LifecycleError):
    """A recorded checksum does not match the bytes it covers."""


class Outcome(Enum):
    RETAINED = "retained"
    REVERTED = "reverted"


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: object) -> bytes:
    """Deterministic bytes for hashing.

    Sorted keys and fixed separators: a checksum that moved when a dictionary
    happened to be built in a different order would be re-derived as a mismatch
    by every later reader.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# Critical path
# ---------------------------------------------------------------------------


def serial_critical_path(durations: Iterable[tuple[str, float]]) -> float:
    """Total of every duration — the one-worker critical path.

    With a single worker every node is on the same chain, so the critical path
    is the sum. Kept separate from the balanced case because the one-worker
    reading has to stay meaningful on its own, and routing it through a balancer
    that cannot split anything would only obscure that.
    """
    return sum(max(0.0, seconds) for _node, seconds in durations)


def balanced_critical_path(durations: Sequence[tuple[str, float]], workers: int) -> float:
    """The longest per-worker chain under longest-processing-time-first.

    Deterministic by construction: nodes are sorted by descending duration with
    the node ID breaking ties, so two runs of the same cohort assign identically
    and their critical paths are comparable. Without the tie-break, two nodes of
    equal duration could swap workers between runs and move the reported number
    without any code changing.
    """
    if workers < 1:
        raise ValueError(f"worker count must be >= 1, got {workers}")
    if not durations:
        return 0.0
    if workers == 1:
        return serial_critical_path(durations)

    ordered = sorted(durations, key=lambda item: (-max(0.0, item[1]), item[0]))
    bins = [0.0] * workers
    for _node, seconds in ordered:
        target = min(range(workers), key=lambda index: (bins[index], index))
        bins[target] += max(0.0, seconds)
    return max(bins)


def assignments(durations: Sequence[tuple[str, float]], workers: int) -> list[list[str]]:
    """Which nodes land on which worker, under the same rule as the balancer."""
    if workers < 1:
        raise ValueError(f"worker count must be >= 1, got {workers}")
    ordered = sorted(durations, key=lambda item: (-max(0.0, item[1]), item[0]))
    bins = [0.0] * workers
    plan: list[list[str]] = [[] for _ in range(workers)]
    for node, seconds in ordered:
        target = min(range(workers), key=lambda index: (bins[index], index))
        bins[target] += max(0.0, seconds)
        plan[target].append(node)
    return plan


# ---------------------------------------------------------------------------
# Evidence records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """The conditions a run was taken under.

    Every field is here because differing on it invalidates the comparison
    rather than merely annotating it. ``provider`` is the sharpest: the two
    providers available on this host differ by a large fraction of total suite
    runtime, so a before/after pair that crossed providers would report a win or
    a loss that has nothing to do with the change under test.
    """

    provider: str
    host_digest: str
    worker_topology: int
    warm_state_digest: str
    schema_fingerprint: str
    canonical_schema_digest: str
    cohort_digest: str
    duration_history_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "hostDigest": self.host_digest,
            "workerTopology": self.worker_topology,
            "warmStateDigest": self.warm_state_digest,
            "schemaFingerprint": self.schema_fingerprint,
            "canonicalSchemaDigest": self.canonical_schema_digest,
            "cohortDigest": self.cohort_digest,
            "durationHistoryDigest": self.duration_history_digest,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> Provenance:
        try:
            return cls(
                provider=str(raw["provider"]),
                host_digest=str(raw["hostDigest"]),
                worker_topology=int(cast("SupportsInt", raw["workerTopology"])),
                warm_state_digest=str(raw["warmStateDigest"]),
                schema_fingerprint=str(raw["schemaFingerprint"]),
                canonical_schema_digest=str(raw["canonicalSchemaDigest"]),
                cohort_digest=str(raw["cohortDigest"]),
                duration_history_digest=str(raw["durationHistoryDigest"]),
            )
        except KeyError as exc:
            raise ProvenanceMismatch(f"run provenance is missing {exc.args[0]}") from exc

    def differences(self, other: Provenance) -> list[str]:
        mine, theirs = self.as_dict(), other.as_dict()
        return [f"{key}: {mine[key]!r} != {theirs[key]!r}" for key in sorted(mine) if mine[key] != theirs[key]]


@dataclass(frozen=True)
class ModuleTiming:
    """One cohort module's measured cost in one run, split by pytest phase.

    Setup and teardown are what a lifecycle change moves; call is carried so the
    critical path describes the work a worker actually performs rather than only
    the part under optimization.
    """

    module_path: str
    setup_seconds: float
    call_seconds: float
    teardown_seconds: float

    @property
    def lifecycle_seconds(self) -> float:
        return self.setup_seconds + self.teardown_seconds

    @property
    def total_seconds(self) -> float:
        return self.setup_seconds + self.call_seconds + self.teardown_seconds

    def as_dict(self) -> dict[str, object]:
        return {
            "modulePath": self.module_path,
            "setupSeconds": round(self.setup_seconds, 4),
            "callSeconds": round(self.call_seconds, 4),
            "teardownSeconds": round(self.teardown_seconds, 4),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ModuleTiming:
        return cls(
            module_path=str(raw["modulePath"]),
            setup_seconds=float(raw["setupSeconds"]),  # type: ignore[arg-type]
            call_seconds=float(raw["callSeconds"]),  # type: ignore[arg-type]
            teardown_seconds=float(raw["teardownSeconds"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class Run:
    """One complete pass over the cohort, on one commit, under one provenance."""

    side: str
    commit: str
    run_index: int
    provenance: Provenance
    timings: tuple[ModuleTiming, ...]
    wall_seconds: float

    @property
    def critical_path_seconds(self) -> float:
        """The figure the gates are applied to.

        Modeled from this run's own per-module durations under the worker count
        recorded in its provenance. At one worker this is the serial sum, which
        is what a serial capture actually performed.
        """
        durations = [(t.module_path, t.total_seconds) for t in self.timings]
        return balanced_critical_path(durations, self.provenance.worker_topology)

    @property
    def lifecycle_seconds(self) -> float:
        return sum(timing.lifecycle_seconds for timing in self.timings)

    def modules(self) -> frozenset[str]:
        return frozenset(timing.module_path for timing in self.timings)

    def as_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "commit": self.commit,
            "runIndex": self.run_index,
            "provenance": self.provenance.as_dict(),
            "wallSeconds": round(self.wall_seconds, 4),
            "timings": [t.as_dict() for t in sorted(self.timings, key=lambda t: t.module_path)],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> Run:
        side = str(raw["side"])
        if side not in _SIDES:
            raise IncompleteEvidence(f"run side must be one of {_SIDES}, got {side!r}")
        return cls(
            side=side,
            commit=str(raw["commit"]),
            run_index=int(cast("SupportsInt", raw["runIndex"])),
            provenance=Provenance.from_dict(cast("Mapping[str, object]", raw["provenance"])),
            timings=tuple(ModuleTiming.from_dict(t) for t in cast("Sequence[Mapping[str, object]]", raw["timings"])),
            wall_seconds=float(cast("SupportsFloat", raw["wallSeconds"])),
        )


# ---------------------------------------------------------------------------
# Cohort
# ---------------------------------------------------------------------------


def cohort_modules(cohort_path: Path) -> tuple[str, ...]:
    """Every module the cohort names, in a stable order."""
    payload = json.loads(cohort_path.read_text())
    return tuple(sorted({str(node["modulePath"]) for node in payload["nodes"]}))


def cohort_digest(cohort_path: Path) -> str:
    """Identity of the cohort a run was taken against.

    Over the module set and each module's node count rather than over the file
    bytes: a cohort re-serialized with different formatting is the same cohort,
    while one that gained or lost a module is not.
    """
    payload = json.loads(cohort_path.read_text())
    shape = sorted((str(n["modulePath"]), int(n["nodeCount"])) for n in payload["nodes"])
    return _sha256_hex(canonical_json(shape))


# ---------------------------------------------------------------------------
# Source delta
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeltaEntry:
    """One path that differs between the two commits."""

    path: str
    status: str
    before_blob: str
    after_blob: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "status": self.status,
            "beforeBlob": self.before_blob,
            "afterBlob": self.after_blob,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> DeltaEntry:
        return cls(
            path=str(raw["path"]),
            status=str(raw["status"]),
            before_blob=str(raw["beforeBlob"]),
            after_blob=str(raw["afterBlob"]),
        )


def source_delta_manifest(runner: Callable[..., str], before_commit: str, after_commit: str) -> tuple[DeltaEntry, ...]:
    """Every path differing between two exact commit objects, sorted.

    ``runner`` is called with Git arguments and returns stdout; the caller owns
    executable resolution and environment scrubbing so this function cannot be
    the place a redirected Git slips in.

    Blob IDs rather than a diff: they make "byte-identical" checkable without
    reading content, which is what a reverted outcome has to prove about files
    it claims not to have touched.
    """
    raw = runner("diff", "--raw", "--no-renames", "-z", f"{before_commit}", f"{after_commit}")
    entries: list[DeltaEntry] = []
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        meta = fields[index]
        if not meta.startswith(":"):
            index += 1
            continue
        parts = meta.split()
        # :<srcmode> <dstmode> <srcblob> <dstblob> <status>
        before_blob, after_blob, status = parts[2], parts[3], parts[4]
        path = fields[index + 1] if index + 1 < len(fields) else ""
        entries.append(DeltaEntry(path=path, status=status, before_blob=before_blob, after_blob=after_blob))
        index += 2
    return tuple(sorted(entries, key=lambda e: e.path))


def manifest_checksum(entries: Sequence[DeltaEntry]) -> str:
    """Checksum over the sorted manifest, so a later reader re-derives it."""
    return _sha256_hex(canonical_json([e.as_dict() for e in entries]))


@dataclass(frozen=True)
class Classification:
    """The delta split into the categories the outcome rules are stated over."""

    measurement_only: tuple[str, ...]
    lifecycle_owned: tuple[str, ...]
    implementation_additions: tuple[str, ...]
    out_of_scope: tuple[str, ...]

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "measurementOnly": list(self.measurement_only),
            "lifecycleOwned": list(self.lifecycle_owned),
            "implementationAdditions": list(self.implementation_additions),
            "outOfScope": list(self.out_of_scope),
        }


def classify_delta(entries: Sequence[DeltaEntry], *, scope: Sequence[str]) -> Classification:
    """Statically sort each changed path into its category.

    Static means by declared path, not by inspecting content: a file's category
    is a property of the contract, so deriving it from what the file happens to
    contain would let a change reclassify itself.
    """
    scope_prefixes = tuple(s for s in scope if s.endswith("/"))
    scope_exact = frozenset(s for s in scope if not s.endswith("/"))

    measurement, lifecycle, additions, outside = [], [], [], []
    for entry in entries:
        path = entry.path
        if path in MEASUREMENT_ONLY_PATHS:
            measurement.append(path)
        elif path in IMPLEMENTATION_ADDITION_PATHS:
            additions.append(path)
        elif path in LIFECYCLE_OWNED_PATHS or path in scope_exact or path.startswith(scope_prefixes):
            lifecycle.append(path)
        else:
            outside.append(path)
    return Classification(
        measurement_only=tuple(sorted(measurement)),
        lifecycle_owned=tuple(sorted(lifecycle)),
        implementation_additions=tuple(sorted(additions)),
        out_of_scope=tuple(sorted(outside)),
    )


def outcome_consistency_failures(outcome: Outcome, classification: Classification) -> list[str]:
    """Whether the delta on disk is consistent with the label being claimed.

    This is the check that makes an outcome label unfalsifiable-by-assertion. A
    run that reports ``reverted`` while its implementation files are still
    present has not reverted anything; a run that reports ``retained`` while
    nothing but the measurement harness changed retained nothing.
    """
    failures: list[str] = []
    if classification.out_of_scope:
        failures.append(
            f"{len(classification.out_of_scope)} changed path(s) fall outside the declared scope: "
            f"{list(classification.out_of_scope)[:5]}"
        )
    if outcome is Outcome.REVERTED:
        if classification.lifecycle_owned:
            failures.append(
                "reverted outcome requires every existing lifecycle file to be byte-identical, but "
                f"{list(classification.lifecycle_owned)} changed"
            )
        if classification.implementation_additions:
            failures.append(
                "reverted outcome requires every implementation addition to be absent, but "
                f"{list(classification.implementation_additions)} are present"
            )
    else:
        if not classification.lifecycle_owned and not classification.implementation_additions:
            failures.append(
                "retained outcome requires a material change, but the only source delta is the "
                "measurement harness itself"
            )
    return failures


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comparison:
    """Both sides, checked, with the outcome the numbers license."""

    before_runs: tuple[Run, ...]
    after_runs: tuple[Run, ...]
    before_commit: str
    after_commit: str
    cohort_digest: str
    minimum_reduction_seconds: float = MINIMUM_REDUCTION_SECONDS
    max_critical_path_seconds: float = MAX_CRITICAL_PATH_SECONDS
    required_runs_per_side: int = REQUIRED_RUNS_PER_SIDE
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def before_best_seconds(self) -> float:
        """The fastest the old code managed — the hardest number to beat."""
        return min(run.critical_path_seconds for run in self.before_runs)

    @property
    def after_worst_seconds(self) -> float:
        """The slowest the new code managed — the easiest number to fail on."""
        return max(run.critical_path_seconds for run in self.after_runs)

    @property
    def reduction_seconds(self) -> float:
        return self.before_best_seconds - self.after_worst_seconds

    @property
    def outcome(self) -> Outcome:
        cleared_floor = self.reduction_seconds >= self.minimum_reduction_seconds
        cleared_ceiling = self.after_worst_seconds <= self.max_critical_path_seconds
        return Outcome.RETAINED if cleared_floor and cleared_ceiling else Outcome.REVERTED

    def refusal_reasons(self) -> list[str]:
        """Why a reverted outcome was reverted, in the gates' own terms."""
        reasons = []
        if self.reduction_seconds < self.minimum_reduction_seconds:
            reasons.append(
                f"reduction {self.reduction_seconds:.2f}s is below the {self.minimum_reduction_seconds:.2f}s floor"
            )
        if self.after_worst_seconds > self.max_critical_path_seconds:
            reasons.append(
                f"after critical path {self.after_worst_seconds:.2f}s exceeds "
                f"the {self.max_critical_path_seconds:.2f}s ceiling"
            )
        return reasons

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "beforeCommit": self.before_commit,
            "afterCommit": self.after_commit,
            "cohortDigest": self.cohort_digest,
            "gates": {
                "minimumReductionSeconds": self.minimum_reduction_seconds,
                "maxCriticalPathSeconds": self.max_critical_path_seconds,
                "requiredRunsPerSide": self.required_runs_per_side,
            },
            "beforeBestSeconds": round(self.before_best_seconds, 4),
            "afterWorstSeconds": round(self.after_worst_seconds, 4),
            "reductionSeconds": round(self.reduction_seconds, 4),
            "outcome": self.outcome.value,
            "refusalReasons": self.refusal_reasons(),
            "runs": [run.as_dict() for run in (*self.before_runs, *self.after_runs)],
            "notes": list(self.notes),
        }

    def checksum(self) -> str:
        """Over the raw runs and the commits, never over the outcome.

        A checksum that covered the label would let a re-derivation agree with a
        wrong conclusion drawn from correct inputs, which is exactly what a later
        reader is supposed to be able to catch.
        """
        return _sha256_hex(
            canonical_json(
                {
                    "beforeCommit": self.before_commit,
                    "afterCommit": self.after_commit,
                    "cohortDigest": self.cohort_digest,
                    "runs": [run.as_dict() for run in (*self.before_runs, *self.after_runs)],
                }
            )
        )


def validate_sides(
    runs: Iterable[Run],
    *,
    before_commit: str,
    after_commit: str,
    expected_modules: Sequence[str],
    required_runs_per_side: int = REQUIRED_RUNS_PER_SIDE,
) -> tuple[tuple[Run, ...], tuple[Run, ...]]:
    """Split runs into sides and refuse anything that cannot be compared.

    Ordered so the cheapest and most specific refusals come first: a run on the
    wrong commit is a different question from a run missing modules, and
    reporting the wrong one sends the reader to the wrong place.
    """
    if before_commit == after_commit:
        raise StaleEvidence(
            f"before and after are the same commit ({before_commit}); "
            "the candidate was never committed, so nothing was measured twice"
        )

    collected = list(runs)
    before = tuple(sorted((r for r in collected if r.side == BEFORE), key=lambda r: r.run_index))
    after = tuple(sorted((r for r in collected if r.side == AFTER), key=lambda r: r.run_index))

    for side_name, side_runs, commit in ((BEFORE, before, before_commit), (AFTER, after, after_commit)):
        if len(side_runs) != required_runs_per_side:
            raise IncompleteEvidence(
                f"{side_name} has {len(side_runs)} run(s); exactly {required_runs_per_side} are required"
            )
        wrong = [r for r in side_runs if r.commit != commit]
        if wrong:
            raise StaleEvidence(
                f"{side_name} run(s) {[r.run_index for r in wrong]} were taken on "
                f"{sorted({r.commit for r in wrong})}, not on the expected {commit}"
            )
        indexes = sorted(r.run_index for r in side_runs)
        if indexes != list(range(1, required_runs_per_side + 1)):
            raise IncompleteEvidence(f"{side_name} run indexes are {indexes}; expected 1..{required_runs_per_side}")

    wanted = frozenset(expected_modules)
    for run in (*before, *after):
        missing = wanted - run.modules()
        if missing:
            raise IncompleteEvidence(
                f"{run.side} run {run.run_index} is missing {len(missing)} cohort module(s): "
                f"{sorted(missing)[:5]}{'...' if len(missing) > 5 else ''}"
            )
        extra = run.modules() - wanted
        if extra:
            raise IncompleteEvidence(
                f"{run.side} run {run.run_index} measured {len(extra)} module(s) outside the cohort: "
                f"{sorted(extra)[:5]}{'...' if len(extra) > 5 else ''}"
            )

    reference = before[0].provenance
    for run in (*before, *after):
        diffs = reference.differences(run.provenance)
        if diffs:
            raise ProvenanceMismatch(
                f"{run.side} run {run.run_index} was taken under different conditions than "
                f"before run 1: {'; '.join(diffs)}"
            )

    return before, after


def build_comparison(
    runs: Iterable[Run],
    *,
    before_commit: str,
    after_commit: str,
    cohort_path: Path,
    minimum_reduction_seconds: float = MINIMUM_REDUCTION_SECONDS,
    max_critical_path_seconds: float = MAX_CRITICAL_PATH_SECONDS,
    required_runs_per_side: int = REQUIRED_RUNS_PER_SIDE,
    notes: Sequence[str] = (),
) -> Comparison:
    """Validate, then compare. Never the other way round."""
    modules = cohort_modules(cohort_path)
    digest = cohort_digest(cohort_path)
    before, after = validate_sides(
        runs,
        before_commit=before_commit,
        after_commit=after_commit,
        expected_modules=modules,
        required_runs_per_side=required_runs_per_side,
    )
    for run in (*before, *after):
        if run.provenance.cohort_digest != digest:
            raise ProvenanceMismatch(
                f"{run.side} run {run.run_index} was taken against cohort "
                f"{run.provenance.cohort_digest} but the comparison uses {digest}"
            )
    return Comparison(
        before_runs=before,
        after_runs=after,
        before_commit=before_commit,
        after_commit=after_commit,
        cohort_digest=digest,
        minimum_reduction_seconds=minimum_reduction_seconds,
        max_critical_path_seconds=max_critical_path_seconds,
        required_runs_per_side=required_runs_per_side,
        notes=tuple(notes),
    )


def load_bundle(path: Path) -> tuple[Comparison, str]:
    """Read a written bundle back and rebuild it from its raw runs.

    Returns the rebuilt comparison and the checksum recorded in the file, so a
    caller compares a value it derived against a value it read rather than
    against one this function chose.
    """
    payload = json.loads(path.read_text())
    runs = [Run.from_dict(raw) for raw in payload["runs"]]
    recorded_checksum = str(payload.get("checksum", ""))
    gates = dict(payload.get("gates", {}))
    comparison = Comparison(
        before_runs=tuple(sorted((r for r in runs if r.side == BEFORE), key=lambda r: r.run_index)),
        after_runs=tuple(sorted((r for r in runs if r.side == AFTER), key=lambda r: r.run_index)),
        before_commit=str(payload["beforeCommit"]),
        after_commit=str(payload["afterCommit"]),
        cohort_digest=str(payload["cohortDigest"]),
        minimum_reduction_seconds=float(gates.get("minimumReductionSeconds", MINIMUM_REDUCTION_SECONDS)),
        max_critical_path_seconds=float(gates.get("maxCriticalPathSeconds", MAX_CRITICAL_PATH_SECONDS)),
        required_runs_per_side=int(gates.get("requiredRunsPerSide", REQUIRED_RUNS_PER_SIDE)),
        notes=tuple(str(n) for n in payload.get("notes", [])),
    )
    return comparison, recorded_checksum


def write_bundle(comparison: Comparison, path: Path, *, extra: Mapping[str, object] | None = None) -> str:
    """Write the bundle and return its checksum."""
    payload = comparison.as_dict()
    if extra:
        payload.update(extra)
    checksum = comparison.checksum()
    payload["checksum"] = checksum
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return checksum


__all__ = [
    "AFTER",
    "BEFORE",
    "IMPLEMENTATION_ADDITION_PATHS",
    "LIFECYCLE_OWNED_PATHS",
    "MAX_CRITICAL_PATH_SECONDS",
    "MEASUREMENT_ONLY_PATHS",
    "MINIMUM_REDUCTION_SECONDS",
    "REQUIRED_RUNS_PER_SIDE",
    "ChecksumMismatch",
    "Classification",
    "Comparison",
    "DeltaEntry",
    "IncompleteEvidence",
    "LifecycleError",
    "ModuleTiming",
    "Outcome",
    "Provenance",
    "ProvenanceMismatch",
    "Run",
    "StaleEvidence",
    "assignments",
    "balanced_critical_path",
    "build_comparison",
    "canonical_json",
    "classify_delta",
    "cohort_digest",
    "cohort_modules",
    "load_bundle",
    "manifest_checksum",
    "outcome_consistency_failures",
    "serial_critical_path",
    "source_delta_manifest",
    "validate_sides",
    "write_bundle",
]
