#!/usr/bin/env python
"""Capture matched before/after lifecycle evidence for the integration cohort.

The controller's whole job is to make a measured claim hard to fake. Three
things follow from that and explain most of the code here:

**One Git, resolved once, anchored explicitly.** Every Git call goes through a
single realpath'd executable with ``-C`` at the product root, under an
environment with the whole ``GIT_*`` namespace removed. ``GIT_DIR`` alone is
enough to make ``git -C <path>`` answer about a different repository, which
would let this controller certify a commit nobody measured. The redirecting
variables are *rejected* rather than silently dropped, because a caller who
exported one deserves to be told it was ignored.

**The before side is captured on a tree that contains none of this.** Bootstrap
the controller into the ignored staging area, capture there, and only then check
the generator in. Measurement scaffolding living in ``tests/`` during the before
capture adds collection cost to the very baseline the change is scored against,
and that error always flatters the change.

**Nothing is derived from a label.** Raw per-run records are written first and
checksummed; the comparison is rebuilt from those raw inputs at finalize time.
A reader who distrusts the verdict can recompute it from what is on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess  # noqa: S404 - runs this repo's own pytest and git, fixed argv, no caller input
import sys
import time
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle_measurement import (
    AFTER,
    BEFORE,
    ModuleTiming,
    Provenance,
    Run,
    build_comparison,
    canonical_json,
    cohort_digest,
    cohort_modules,
    write_bundle,
)

#: Each of these can redirect a Git command at another repository. Rejected
#: loudly; the rest of the GIT_* namespace is scrubbed silently because
#: GIT_EDITOR or GIT_PAGER cannot change which repository answers.
REDIRECTING_GIT_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
)

#: The cohort is captured serially, so the recorded topology is one worker and
#: the critical path is the serial sum. Recorded rather than assumed so the
#: same raw runs can be re-derived at other worker counts later without
#: re-measuring, and so a run taken under a different topology cannot be
#: compared against these by accident.
WORKER_TOPOLOGY = 1

#: Contamination rule, fixed before any result exists so it cannot be applied
#: selectively afterwards.
#:
#: A pair is compared against itself, so what invalidates it is *asymmetry*
#: between its halves rather than absolute load. Pairing already absorbs slow
#: drift by construction; what it cannot absorb is a burst landing on one half.
#: An absolute floor is actively harmful here — set below the host's own idle
#: baseline it can never be satisfied, which produces a stall that looks like a
#: measurement failure. The delta is derived from this host's ambient swing
#: (roughly +/-0.6 around its baseline), not chosen.
MAX_PAIR_LOAD_DELTA = 1.0

#: Backstop for a machine loud enough that both halves are unreliable together.
MAX_PAIR_LOAD = 8.0

#: Re-takes count only pairs actually measured. Waiting is not an attempt.
MAX_PAIR_ATTEMPTS = 3

_DURATION_LINE = re.compile(r"^(\d+\.\d+)s\s+(setup|call|teardown)\s+(\S+)", re.M)


class ControllerError(RuntimeError):
    """Any refusal that should stop the campaign with a readable reason."""


# ---------------------------------------------------------------------------
# Git, resolved and anchored
# ---------------------------------------------------------------------------


def resolve_git() -> str:
    """One absolute Git for every call.

    The ``shutil.which`` result is realpath'd so a shimmed or symlinked git
    cannot resolve differently between two calls in the same campaign.
    """
    found = shutil.which("git")
    if not found:
        raise ControllerError("no git executable on PATH")
    return os.path.realpath(found)


def scrubbed_env() -> dict[str, str]:
    """Reject, then remove, every inherited ``GIT_*`` variable."""
    redirecting = sorted(k for k in os.environ if k in REDIRECTING_GIT_VARS)  # config: intentional
    if redirecting:
        raise ControllerError(
            "refusing to run with inherited Git environment: "
            + ", ".join(redirecting)
            + ". Each can make `git -C <path>` answer about a different repository, which would let "
            "this controller certify evidence against a tree nobody measured. Unset them and re-run."
        )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # config: intentional
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class Git:
    """Every Git call in this process, anchored at one verified product root."""

    def __init__(self, product_root: Path) -> None:
        self._bin = resolve_git()
        self._env = scrubbed_env()
        self._root = product_root.resolve()
        top = Path(self("rev-parse", "--show-toplevel")).resolve()
        if top != self._root:
            raise ControllerError(f"{product_root} is not a worktree top level; git reports {top}")

    def __call__(self, *args: str) -> str:
        result = subprocess.run(  # noqa: S603 - realpath'd git, fixed argv, scrubbed env
            [self._bin, "-C", str(self._root), *args],
            capture_output=True,
            text=True,
            env=self._env,
            check=False,
        )
        if result.returncode != 0:
            raise ControllerError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    @property
    def root(self) -> Path:
        return self._root

    def head(self) -> str:
        return self("rev-parse", "HEAD")

    def require_commit(self, expected: str, *, require_clean: bool) -> None:
        """Refuse unless this tree *is* the commit being claimed.

        ``cat-file -e`` first: a caller can hand over a well-formed SHA that
        names no object, and comparing two strings would accept it.
        """
        self("cat-file", "-e", f"{expected}^{{commit}}")
        head = self.head()
        if head != expected:
            raise ControllerError(
                f"worktree HEAD is {head}, expected {expected}. Evidence captured here would be "
                "attributed to a commit it was not taken on."
            )
        if require_clean and self("status", "--porcelain"):
            raise ControllerError(
                "worktree has uncommitted changes; measured evidence would describe a tree that "
                "no commit records. Commit or stash, then re-run."
            )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def load1() -> float:
    """One-minute load average, sampled around every run in both arms."""
    return os.getloadavg()[0]


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def host_digest() -> str:
    """Identity of the machine, over the attributes that move timings."""
    return _digest(
        {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpuCount": os.cpu_count(),
            "python": platform.python_version(),
        }
    )


def schema_fingerprint(root: Path) -> tuple[str, str]:
    """(fingerprint, canonical digest) of the migration set.

    Derived from the migration files rather than from a live database: the
    fingerprint has to be computable before anything is provisioned, and a
    database can be at a different revision than the tree that describes it.
    """
    versions = root / "contextplane" / "storage" / "migrations" / "versions"
    files = sorted(p for p in versions.glob("*.py") if p.name != "__init__.py")
    if not files:
        raise ControllerError(f"no migration versions found under {versions}")
    fingerprint = _digest([p.name for p in files])
    canonical = _digest([hashlib.sha256(p.read_bytes()).hexdigest() for p in files])
    return fingerprint, canonical


def warm_state_digest(root: Path, provider: str) -> str:
    """What warm state the run could have reused.

    Recorded because a run against an already-migrated warm cluster and a run
    that had to build one are not the same measurement, and the difference lands
    entirely in setup — the phase this task is trying to move.
    """
    pgdata = root / ".devstack" / "pgdata-test"
    return _digest(
        {
            "provider": provider,
            "pgdataPresent": pgdata.is_dir(),
            "pgdataEntries": sorted(p.name for p in pgdata.iterdir())[:20] if pgdata.is_dir() else [],
        }
    )


#: The cache provider is disabled for every measured run, so no previous run's
#: recorded durations can influence this one's ordering or selection. The digest
#: records that policy rather than a mutable file, because the point is that
#: there is no history in play.
DURATION_HISTORY_POLICY = "cacheprovider-disabled; no duration history consulted or written"


def build_provenance(root: Path, provider: str, cohort_path: Path) -> Provenance:
    fingerprint, canonical = schema_fingerprint(root)
    return Provenance(
        provider=provider,
        host_digest=host_digest(),
        worker_topology=WORKER_TOPOLOGY,
        warm_state_digest=warm_state_digest(root, provider),
        schema_fingerprint=fingerprint,
        canonical_schema_digest=canonical,
        cohort_digest=cohort_digest(cohort_path),
        duration_history_digest=_digest(DURATION_HISTORY_POLICY),
    )


# ---------------------------------------------------------------------------
# One measured run
# ---------------------------------------------------------------------------


def parse_durations(stdout: str, wanted: Sequence[str]) -> tuple[ModuleTiming, ...]:
    """Fold pytest's per-phase duration lines into per-module totals.

    Only cohort modules are kept. A module that produced no duration line at all
    is left out entirely rather than recorded as zero — zero is a measurement,
    absence is a gap, and the completeness check downstream is the thing that
    should notice the difference.
    """
    totals: dict[str, dict[str, float]] = {}
    wanted_set = frozenset(wanted)
    for seconds, phase, nodeid in _DURATION_LINE.findall(stdout):
        module = nodeid.split("::")[0]
        if module not in wanted_set:
            continue
        totals.setdefault(module, {"setup": 0.0, "call": 0.0, "teardown": 0.0})[phase] += float(seconds)
    return tuple(
        ModuleTiming(
            module_path=module,
            setup_seconds=phases["setup"],
            call_seconds=phases["call"],
            teardown_seconds=phases["teardown"],
        )
        for module, phases in sorted(totals.items())
    )


def execute_run(
    root: Path,
    modules: Sequence[str],
    provider: str,
    *,
    log_path: Path,
) -> tuple[tuple[ModuleTiming, ...], float]:
    """Run the cohort once and return its per-module timings and wall time."""
    env = dict(os.environ)  # config: intentional
    env["CONTEXTPLANE_TEST_PG"] = provider
    # Force `import contextplane` to resolve to the tree being measured. The
    # virtualenv is an editable install pointing at the primary checkout, and
    # its finder answers for any linked worktree that lacks its own venv — so
    # without this a worktree run measures the primary checkout's code while
    # reporting the worktree's commit. PYTHONPATH wins because the editable
    # finder is appended to sys.meta_path after PathFinder.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else str(root)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *modules,
        "-q",
        "--durations=0",
        "--durations-min=0",
        "-p",
        "no:cacheprovider",
    ]
    started = time.monotonic()
    result = subprocess.run(  # noqa: S603 - sys.executable + this repo's pytest, fixed argv
        command, cwd=root, env=env, capture_output=True, text=True, check=False
    )
    wall = time.monotonic() - started
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(result.stdout + "\n----- stderr -----\n" + result.stderr)

    timings = parse_durations(result.stdout, modules)
    if not timings:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-25:])
        raise ControllerError(f"no durations parsed from the run; pytest produced:\n{tail}")
    return timings, wall


def capture_side(
    git: Git,
    *,
    side: str,
    commit: str,
    runs: int,
    cohort_path: Path,
    evidence_root: Path,
    provider: str,
    require_clean: bool,
) -> list[Run]:
    """Capture one side's complete set of runs, writing each raw record."""
    git.require_commit(commit, require_clean=require_clean)
    modules = cohort_modules(cohort_path)
    provenance = build_provenance(git.root, provider, cohort_path)
    side_dir = evidence_root / f"{side}-{commit[:12]}"
    side_dir.mkdir(parents=True, exist_ok=True)

    captured: list[Run] = []
    for index in range(1, runs + 1):
        print(f"[{side}] run {index}/{runs} over {len(modules)} modules ...", flush=True)
        timings, wall = execute_run(git.root, modules, provider, log_path=side_dir / f"run-{index}.log")
        # Re-check identity after the run: a long campaign is exactly when
        # something else moves the tree underneath it.
        git.require_commit(commit, require_clean=require_clean)
        run = Run(
            side=side,
            commit=commit,
            run_index=index,
            provenance=provenance,
            timings=timings,
            wall_seconds=wall,
        )
        record = side_dir / f"run-{index}.json"
        payload = run.as_dict()
        payload["recordChecksum"] = hashlib.sha256(canonical_json(run.as_dict())).hexdigest()
        record.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        captured.append(run)
        print(
            f"[{side}] run {index}: {len(timings)} modules, critical path "
            f"{run.critical_path_seconds:.2f}s (wall {wall:.1f}s)",
            flush=True,
        )
    return captured


def load_side(evidence_root: Path, side: str, commit: str, expected_runs: int) -> list[Run]:
    """Read a side's raw records back, verifying each record's own checksum."""
    side_dir = evidence_root / f"{side}-{commit[:12]}"
    if not side_dir.is_dir():
        raise ControllerError(f"no {side} evidence at {side_dir}. Capture the {side} side on {commit[:12]} first.")
    runs: list[Run] = []
    for index in range(1, expected_runs + 1):
        record = side_dir / f"run-{index}.json"
        if not record.is_file():
            raise ControllerError(f"{side} run {index} is missing at {record}")
        payload = json.loads(record.read_text())
        recorded = str(payload.pop("recordChecksum", ""))
        run = Run.from_dict(payload)
        derived = hashlib.sha256(canonical_json(run.as_dict())).hexdigest()
        if recorded != derived:
            raise ControllerError(
                f"{side} run {index} record checksum {recorded[:12]} does not match its contents "
                f"({derived[:12]}); the raw input was edited after capture"
            )
        runs.append(run)
    return runs


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def take_run(
    git: Git,
    *,
    side: str,
    commit: str,
    index: int,
    modules: Sequence[str],
    provenance: Provenance,
    provider: str,
    side_dir: Path,
    require_clean: bool,
) -> Run:
    """One run, with the load sampled either side of it.

    Deliberately ungated. A single run cannot be judged on its own load, because
    what invalidates this comparison is asymmetry *between* the halves of a pair
    — a judgement only the caller, which can see both, is in a position to make.
    """
    before_load = load1()
    git.require_commit(commit, require_clean=require_clean)
    timings, wall = execute_run(git.root, modules, provider, log_path=side_dir / f"run-{index}.log")
    after_load = load1()
    git.require_commit(commit, require_clean=require_clean)
    run = Run(
        side=side,
        commit=commit,
        run_index=index,
        provenance=provenance,
        timings=timings,
        wall_seconds=wall,
        load_before=before_load,
        load_after=after_load,
    )
    print(
        f"[{side} {index}] critical path {run.critical_path_seconds:.2f}s "
        f"(wall {wall:.1f}s, load {before_load:.2f}->{after_load:.2f}, mean {run.mean_load:.2f})",
        flush=True,
    )
    return run


def cmd_capture_paired(args: argparse.Namespace) -> int:
    """Interleave the arms: before, after, before, after, ...

    Paired rather than blocked because drift and contention on a shared machine
    are not constant across an hour. Interleaving makes both arms absorb the same
    drift, so the difference survives a machine that got busier partway through;
    two separated blocks silently attribute that drift to whichever arm ran later.
    """
    git = Git(Path(args.product_root))
    evidence_root = Path(args.evidence_root)
    cohort_path = Path(args.cohort)
    modules = cohort_modules(cohort_path)
    provenance = build_provenance(git.root, args.provider, cohort_path)

    baseline = load1()
    if baseline > MAX_PAIR_LOAD:
        raise ControllerError(
            f"host load is {baseline:.2f}, already above the {MAX_PAIR_LOAD} backstop before any run "
            "started. Retrying cannot fix a floor set under the machine's own idle baseline — quiesce "
            "the host or raise the backstop deliberately, but do not measure through it."
        )
    print(f"host baseline load {baseline:.2f}", flush=True)

    sides = ((BEFORE, args.expected_before_commit), (AFTER, args.expected_after_commit))
    collected: list[Run] = []
    for index in range(1, args.before_runs + 1):
        for attempt in range(1, MAX_PAIR_ATTEMPTS + 1):
            pair: list[Run] = []
            for side, commit in sides:
                git("checkout", "--quiet", commit)
                side_dir = evidence_root / f"{side}-{commit[:12]}"
                side_dir.mkdir(parents=True, exist_ok=True)
                pair.append(
                    take_run(
                        git,
                        side=side,
                        commit=commit,
                        index=index,
                        modules=modules,
                        provenance=provenance,
                        provider=args.provider,
                        side_dir=side_dir,
                        require_clean=args.require_clean,
                    )
                )
            delta = abs(pair[0].mean_load - pair[1].mean_load)
            loudest = max(run.mean_load for run in pair)
            if delta > MAX_PAIR_LOAD_DELTA:
                print(
                    f"[pair {index}] load delta {delta:.2f} > {MAX_PAIR_LOAD_DELTA}: one half saw a burst "
                    f"the other did not, re-taking (attempt {attempt})",
                    flush=True,
                )
                continue
            if loudest > MAX_PAIR_LOAD:
                print(
                    f"[pair {index}] loudest half at {loudest:.2f} > {MAX_PAIR_LOAD}, re-taking "
                    f"(attempt {attempt})",
                    flush=True,
                )
                continue
            print(f"[pair {index}] valid — delta {delta:.2f}, loudest {loudest:.2f}", flush=True)
            for run in pair:
                side_dir = evidence_root / f"{run.side}-{run.commit[:12]}"
                payload = run.as_dict()
                payload["recordChecksum"] = hashlib.sha256(canonical_json(run.as_dict())).hexdigest()
                (side_dir / f"run-{index}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                collected.append(run)
            break
        else:
            raise ControllerError(f"pair {index} never came back symmetric enough to compare")

    comparison = build_comparison(
        collected,
        before_commit=args.expected_before_commit,
        after_commit=args.expected_after_commit,
        cohort_path=cohort_path,
        minimum_reduction_seconds=args.minimum_reduction,
        max_critical_path_seconds=args.max_critical_path,
        required_runs_per_side=args.before_runs,
    )
    run_id = f"paired-{args.expected_before_commit[:12]}-{args.expected_after_commit[:12]}"
    bundle = evidence_root / run_id / "lifecycle-comparison.json"
    checksum = write_bundle(comparison, bundle, extra={"runId": run_id, "design": "interleaved-paired"})
    print(f"before best   {comparison.before_best_seconds:.2f}s")
    print(f"after worst   {comparison.after_worst_seconds:.2f}s")
    print(f"reduction     {comparison.reduction_seconds:.2f}s")
    print(f"outcome       {comparison.outcome.value}")
    for reason in comparison.refusal_reasons():
        print(f"  - {reason}")
    print(f"run id        {run_id}")
    print(f"bundle        {bundle.resolve()}")
    print(f"checksum      {checksum}")
    return 0


def cmd_capture_before(args: argparse.Namespace) -> int:
    git = Git(Path(args.product_root))
    capture_side(
        git,
        side=BEFORE,
        commit=args.expected_before_commit,
        runs=args.before_runs,
        cohort_path=Path(args.cohort),
        evidence_root=Path(args.evidence_root),
        provider=args.provider,
        require_clean=args.require_clean,
    )
    print("before side captured")
    return 0


def cmd_measure_after_and_finalize(args: argparse.Namespace) -> int:
    git = Git(Path(args.product_root))
    evidence_root = Path(args.evidence_root)
    cohort_path = Path(args.cohort)

    before = load_side(evidence_root, BEFORE, args.expected_before_commit, args.before_runs)
    after = capture_side(
        git,
        side=AFTER,
        commit=args.expected_after_commit,
        runs=args.after_runs,
        cohort_path=cohort_path,
        evidence_root=evidence_root,
        provider=args.provider,
        require_clean=args.require_clean,
    )

    comparison = build_comparison(
        [*before, *after],
        before_commit=args.expected_before_commit,
        after_commit=args.expected_after_commit,
        cohort_path=cohort_path,
        minimum_reduction_seconds=args.minimum_reduction,
        max_critical_path_seconds=args.max_critical_path,
        required_runs_per_side=args.before_runs,
    )

    run_id = f"cmp-{args.expected_before_commit[:12]}-{args.expected_after_commit[:12]}"
    bundle = evidence_root / run_id / "lifecycle-comparison.json"
    checksum = write_bundle(comparison, bundle, extra={"runId": run_id})

    print(f"before best   {comparison.before_best_seconds:.2f}s")
    print(f"after worst   {comparison.after_worst_seconds:.2f}s")
    print(f"reduction     {comparison.reduction_seconds:.2f}s")
    print(f"outcome       {comparison.outcome.value}")
    for reason in comparison.refusal_reasons():
        print(f"  - {reason}")
    print(f"run id        {run_id}")
    print(f"bundle        {bundle.resolve()}")
    print(f"checksum      {checksum}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Every shared option lives on a parent parser rather than the top-level one,
    # so they are accepted *after* the subcommand. argparse binds top-level
    # options only before the subcommand, which would reject the documented
    # invocation order -- the shape a caller actually types.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--product-root", default=".", help="worktree top level to measure")
    common.add_argument("--cohort", required=True, help="machine-derived cohort JSON")
    common.add_argument("--evidence-root", required=True, help="ignored staging root for raw runs and bundles")
    common.add_argument("--provider", default="devstack", choices=("devstack", "testcontainers", "external"))
    common.add_argument("--before-runs", type=int, default=3)
    common.add_argument("--after-runs", type=int, default=3)
    common.add_argument("--expected-before-commit", required=True)
    common.add_argument("--minimum-reduction", type=float, default=1.0)
    common.add_argument("--max-critical-path", type=float, default=math.inf)
    common.add_argument("--require-clean", action="store_true")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "capture-before",
        parents=[common],
        help="capture the before side on the frozen dependency commit",
    )
    after = sub.add_parser(
        "measure-after-and-finalize-handoff",
        parents=[common],
        help="capture the after side on the committed candidate and finalize the bundle",
    )
    after.add_argument("--expected-after-commit", required=True)
    paired = sub.add_parser(
        "capture-paired",
        parents=[common],
        help="interleave both arms run-by-run so drift lands on both equally",
    )
    paired.add_argument("--expected-after-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capture-before":
            return cmd_capture_before(args)
        if args.command == "capture-paired":
            return cmd_capture_paired(args)
        return cmd_measure_after_and_finalize(args)
    except (ControllerError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
