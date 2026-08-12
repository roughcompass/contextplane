#!/usr/bin/env python3
"""The outer owner of hard-gate timing, scale sequences, and provider parity.

The inner runner seals what happens *inside* a measured run. It cannot seal the
run itself. It does not know whether the tree was clean, whether the commit it
reports is the one it ran on, whether another sequence was using the same
database, or how long the whole invocation actually took from outside — its
clock starts after process spawn, Make startup and interpreter import have
already happened, and that excluded prologue is exactly where a regression
hides from an inner clock.

So this layer owns the parts a child cannot vouch for:

- **Provenance.** One realpath'd Git, one verified top level, the expected
  commit and `HEAD` resolved independently before every child and again after
  the sequence, and clean-tree checkpoints throughout.
- **Exclusivity.** One provider lease held from before the first child until
  after the sequence manifest is published, with broker admission closed for
  the duration.
- **Authorization.** A distinct single-use HMAC control per child, consumed
  before collection, so a child that ran is a child this controller authorized.
- **External timing.** `/usr/bin/time -p` writes `real` for the whole canonical
  Make invocation. Only this layer records it.
- **The deadline.** The child's process *group* is terminated at the boundary,
  not just the child. Make spawns pytest which spawns workers; signalling the
  one process the controller can see leaves the rest running past the deadline
  that was supposed to have stopped them.

The command is fixed here and takes nothing from a caller. Hard-gate and
provider-parity permit no command, file, marker, option, or worker override at
all: the number they produce describes the tracked committed default, and a
gate that could be pointed at a subset would produce a smaller number for the
same tree.

Failure is never salvaged. A child that misses a boundary, loses a report, or
finds a dirty tree voids the whole sequence rather than contributing a partial
result — three runs where one was rescheduled are not three runs of one thing.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess  # noqa: S404 - spawning the sealed canonical child is this module's purpose
import sys
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from integration_control import (
    CONTROL_ENVIRONMENT_VARIABLE,
    Broker,
    ControlError,
    Lease,
    LeaseError,
    acquire_lease,
    issue,
    new_sequence_secret,
    reject_inherited_control,
    release_lease,
)
from integration_evidence import (
    EvidenceError,
    ExternalTiming,
    atomic_write,
    create_run_directory,
    parse_time_file,
    read_manifest,
    sha256_file,
    sha256_text,
    verify_manifest,
)
from integration_provenance import (
    PRODUCT_ROOT,
    GitContext,
    ProvenanceError,
    attempted_git_variables,
    bind_commit,
    host_digest,
    open_git,
    sanitized_environment,
)
from integration_scheduler import EXTERNAL_MAX_SECONDS, TERMINATION_GRACE_SECONDS
from run_integration_tests import forbidden_variables

#: The worker counts a scale sequence measures. Fixed internally and not
#: reachable from the command line: a caller who could choose the candidates
#: could choose the one that passes.
SCALE_CANDIDATES: Final = (1, 2, 4, 8)

#: One warm-up, then three measured runs, per candidate. The warm-up exists
#: because the first run of anything pays for a cold page cache and a cold
#: connection pool, and paying that once inside a measured run makes the
#: candidate look worse than the system it represents.
MEASURED_RUNS: Final = 3

#: Provider parity's own budget. This is an operational deadlock timeout, not a
#: performance threshold — a parity run that takes 400 seconds is a complete,
#: usable result, and only a run that has plainly stopped making progress is
#: killed.
PARITY_TIMEOUT_SECONDS: Final = 900.0

_TIME_EXECUTABLE: Final = "/usr/bin/time"
_MAKE_TARGET: Final = "test-integration"

#: Where a tracked worker default lives once a scale sequence has selected one.
_WORKER_SETTING: Final = ("tool", "contextplane", "integration", "workers")


class GateError(RuntimeError):
    """The sequence cannot produce qualifying evidence."""


class SequenceVoid(GateError):
    """A child failed in a way that invalidates every run beside it."""


# ---------------------------------------------------------------------------
# Entry qualification
# ---------------------------------------------------------------------------


def qualify_controller(environ: Mapping[str, str]) -> None:
    """Refuse an invocation that could have changed what the children run.

    Presence is the failure, exactly as in the inner runner. A controller that
    scrubbed `MAKEFLAGS` and continued would emit a clean sequence manifest for
    a sequence somebody tried to redirect, and nothing downstream could tell.
    The attempted names go into the failure message; their values do not.
    """
    attempted = list(forbidden_variables(environ))
    attempted.extend(name for name in attempted_git_variables(environ) if name not in attempted)
    if attempted:
        msg = (
            "refusing to run: forbidden channel(s) present at controller entry: "
            + ", ".join(sorted(attempted))
            + ". Each can change which interpreter, which repository, or which tests the measured "
            "children see. The attempt is the failure, not its effect."
        )
        raise GateError(msg)
    reject_inherited_control(environ)


def committed_worker_count(product_root: Path) -> int:
    """The tracked default every no-override child runs under.

    Read from the committed project file rather than passed in. Hard-gate and
    provider-parity are defined as measurements of what is committed, so a
    worker count that arrived on the command line would let the gate describe a
    configuration the repository does not have.
    """
    settings = tomllib.loads((product_root / "pyproject.toml").read_text(encoding="utf-8"))
    cursor: Any = settings
    for key in _WORKER_SETTING:
        if not isinstance(cursor, Mapping) or key not in cursor:
            msg = (
                f"no committed worker default at [{'.'.join(_WORKER_SETTING[:-1])}] {_WORKER_SETTING[-1]}. "
                "A scale sequence selects it and commits it; the hard gate measures what is committed."
            )
            raise GateError(msg)
        cursor = cursor[key]
    if not isinstance(cursor, int) or cursor < 1:
        msg = f"committed worker default must be a positive integer, got {cursor!r}"
        raise GateError(msg)
    return cursor


# ---------------------------------------------------------------------------
# The canonical child command
# ---------------------------------------------------------------------------


def canonical_command(provider: str) -> tuple[str, ...]:
    """The invariant part of every child's argv.

    Per-child paths — the timing file, the control — are deliberately excluded.
    They differ by construction, so including them would make the digest vary
    between children that are supposed to be identical, and the digest exists
    precisely to prove that they were.
    """
    return ("env", f"CONTEXTPLANE_TEST_PG={provider}", "make", _MAKE_TARGET)


def resolved_command(provider: str, *, time_file: Path, control: Path) -> list[str]:
    """The exact argv, with nothing from a caller anywhere in it."""
    return [
        _TIME_EXECUTABLE,
        "-p",
        "-o",
        str(time_file),
        "env",
        f"CONTEXTPLANE_TEST_PG={provider}",
        f"{CONTROL_ENVIRONMENT_VARIABLE}={control}",
        "make",
        _MAKE_TARGET,
    ]


def command_digest(provider: str) -> str:
    return sha256_text("\x00".join(canonical_command(provider)))


# ---------------------------------------------------------------------------
# One child
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildPlan:
    """What distinguishes one child from its siblings."""

    child_sequence: int
    mode: str
    role: str
    worker_count: int
    provider: str
    deadline_seconds: float

    @property
    def measured(self) -> bool:
        return self.role != "warm-up"


@dataclass
class ChildResult:
    """One completed child, and everything the manifest binds about it."""

    plan: ChildPlan
    run_id: str
    exit_status: int
    timing: ExternalTiming
    control_digest: str
    command: tuple[str, ...]
    checksums: Mapping[str, str]
    inner_summary: Mapping[str, Any]
    timed_out: bool = False

    def as_evidence(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "child_sequence": self.plan.child_sequence,
            "mode": self.plan.mode,
            "role": self.plan.role,
            "worker_count": self.plan.worker_count,
            "provider": self.plan.provider,
            "exit_status": self.exit_status,
            "timed_out": self.timed_out,
            "control_digest": self.control_digest,
            "command": list(self.command),
            "checksums": dict(self.checksums),
            "inner_summary": dict(self.inner_summary),
            **self.timing.as_evidence(),
        }


def _terminate_group(process: subprocess.Popen[bytes], *, grace_seconds: float) -> None:
    """Stop the whole process group, then kill whatever survives.

    The group and not the process. `make` spawns pytest which spawns workers,
    and signalling only the process this controller can see leaves that tree
    running past the deadline that was supposed to have ended it — still
    holding the database the next child is about to take.
    """
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    for signal_number, wait_for in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(group, signal_number)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=max(0.0, wait_for))
        except subprocess.TimeoutExpired:
            continue
        return


def run_child(
    plan: ChildPlan,
    *,
    directory: Path,
    control_path: Path,
    environment: Mapping[str, str],
    product_root: Path,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[int, bool, Path, Path, Path]:
    """Spawn the sealed child, enforce its deadline, and wait for it to exit.

    `start_new_session` puts the child in its own process group so the deadline
    can reach the whole tree. Standard output and error are captured to
    separate files — interleaving them into one stream loses which said what,
    and a failure diagnosis that cannot tell a pytest summary from a Make error
    is not much of a diagnosis.

    Returns before the timing file is read. `/usr/bin/time` writes that file
    when the process it wrapped terminates, so parsing it any earlier reads a
    truncated line, and a truncated `real` that happens to parse is simply a
    wrong number rather than a missing one.
    """
    time_file = directory / "external-time.txt"
    stdout_path = directory / "stdout.log"
    stderr_path = directory / "stderr.log"
    command = resolved_command(plan.provider, time_file=time_file, control=control_path)

    started_at = monotonic()
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        process = subprocess.Popen(  # noqa: S603 - fixed argv built here, no caller input reaches it
            command,
            cwd=str(product_root),
            env=dict(environment),
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=plan.deadline_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            elapsed = monotonic() - started_at
            # "At most the smaller of 500 ms or the remaining allowance." A
            # violation detected at the boundary has no allowance left, and
            # spending a full grace period there would push the run further
            # past the boundary while cleaning up a run that already failed.
            remaining = plan.deadline_seconds + TERMINATION_GRACE_SECONDS - elapsed
            _terminate_group(process, grace_seconds=max(0.0, min(TERMINATION_GRACE_SECONDS, remaining)))
    return process.returncode if process.returncode is not None else -1, timed_out, time_file, stdout_path, stderr_path


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------


@dataclass
class SequenceRun:
    """One uninterrupted run of children under one lease and one controller."""

    mode: str
    provider: str
    evidence_root: Path
    expected_commit: str
    git: GitContext
    controller_id: str = field(default_factory=lambda: f"ctl-{uuid.uuid4().hex[:12]}")
    sequence_id: str = field(default_factory=lambda: f"seq-{uuid.uuid4().hex[:12]}")
    collection_digest: str = ""
    schema_fingerprint: str = ""

    def child_run_id(self, plan: ChildPlan) -> str:
        return f"{self.sequence_id}-{plan.child_sequence:03d}"


def _inner_evidence(directory: Path) -> dict[str, Any]:
    """Validate the child's own sealed bundle and return its summary.

    The manifest is re-checksummed rather than believed. A manifest that
    vouches for itself proves only that whoever edited a file could also edit
    the record of that file.
    """
    inner = directory / "inner"
    if not (inner / "manifest.json").is_file():
        msg = (
            f"child produced no sealed manifest at {inner}. A run with no manifest did not finish, "
            "which is a different fact from a run that finished badly and must not look the same."
        )
        raise SequenceVoid(msg)
    mismatches = verify_manifest(inner)
    if mismatches:
        msg = f"child evidence at {inner} does not match its own checksums: {mismatches}"
        raise SequenceVoid(msg)
    manifest = read_manifest(inner)
    summary: dict[str, Any] = dict(manifest.get("summary", {}))
    summary["inner_manifest_checksum"] = sha256_file(inner / "manifest.json")
    return summary


def execute_sequence(
    sequence: SequenceRun,
    plans: Sequence[ChildPlan],
    *,
    lease: Lease,
    secret: bytes,
    environment: Mapping[str, str],
    product_root: Path,
    now: Callable[[], float] = time.time,
) -> list[ChildResult]:
    """Run every child in order, or void the sequence.

    A clean-tree checkpoint and an independent commit binding run before *and*
    after each child. Checking once at the start proves the tree was right at
    the least interesting moment — before anything had a chance to change it.
    """
    results: list[ChildResult] = []
    for plan in plans:
        sequence.git.assert_clean(checkpoint=f"before-child-{plan.child_sequence}")
        bind_commit(sequence.git, sequence.expected_commit)

        run_id = sequence.child_run_id(plan)
        directory = create_run_directory(sequence.evidence_root, run_id).path
        control = issue(
            secret=secret,
            directory=directory / "control",
            bound={
                "controller_id": sequence.controller_id,
                "lease_id": lease.lease_id,
                "sequence_id": sequence.sequence_id,
                "child_sequence": plan.child_sequence,
                "mode": plan.mode,
                "role": plan.role,
                "worker_count": plan.worker_count,
                "provider": plan.provider,
                "expected_commit": sequence.expected_commit,
                "host_digest": host_digest(),
                "schema_fingerprint": sequence.schema_fingerprint,
                "collection_digest": sequence.collection_digest,
                "command_digest": command_digest(plan.provider),
            },
            now=now,
        )

        exit_status, timed_out, time_file, stdout_path, stderr_path = run_child(
            plan,
            directory=directory,
            control_path=control.path,
            environment=environment,
            product_root=product_root,
        )
        if timed_out:
            msg = (
                f"child {plan.child_sequence} exceeded its {plan.deadline_seconds:.1f}s deadline and its "
                "process group was terminated. The sequence is void; a timed-out child is not "
                "rescheduled, because a sequence with a retry in it is not a sequence of identical runs."
            )
            raise SequenceVoid(msg)
        if exit_status != 0:
            msg = f"child {plan.child_sequence} exited {exit_status}; the sequence is void"
            raise SequenceVoid(msg)

        timing = parse_time_file(time_file)
        summary = _inner_evidence(directory)
        checksums = {
            "external-time.txt": sha256_file(time_file),
            "stdout.log": sha256_file(stdout_path),
            "stderr.log": sha256_file(stderr_path),
        }
        result = ChildResult(
            plan=plan,
            run_id=run_id,
            exit_status=exit_status,
            timing=timing,
            control_digest=control.digest,
            command=tuple(resolved_command(plan.provider, time_file=time_file, control=control.path)),
            checksums=checksums,
            inner_summary=summary,
        )
        # The envelope is written last and atomically, so a reader that finds
        # one knows every file it names is complete.
        atomic_write(
            directory / "envelope.json",
            json.dumps(result.as_evidence(), indent=2, sort_keys=True) + "\n",
        )
        sequence.git.assert_clean(checkpoint=f"after-child-{plan.child_sequence}")
        results.append(result)
    return results


def publish_sequence(
    sequence: SequenceRun,
    results: Sequence[ChildResult],
    *,
    lease: Lease,
    broker: Broker,
    before_checkpoint: tuple[str, ...],
) -> Path:
    """Seal the sequence: one controller, one lease, ordered runs, checksums.

    Written after the final clean-tree checkpoint and the final independent
    commit resolution, while the lease is still held. Publishing before the
    lease is released is what stops a second sequence starting into the window
    where this one's manifest is half-written.
    """
    binding = bind_commit(sequence.git, sequence.expected_commit)
    after_checkpoint = sequence.git.assert_clean(checkpoint="after-sequence")
    # Every child must have presented its control to the broker before it
    # collected. An empty consumed set does not mean "nothing needed
    # authorizing" — it means the authorization path did not run, and a
    # manifest published in that state would assert a property nobody checked.
    if len(broker.consumed_digests) != len(results):
        msg = (
            f"{len(results)} child(ren) ran but {len(broker.consumed_digests)} control(s) were consumed. "
            "A control is consumed by the child presenting it before collection; a count that does not "
            "match means the sequence cannot show that every measured run was authorized."
        )
        raise SequenceVoid(msg)
    manifest = {
        "controller_id": sequence.controller_id,
        "sequence_id": sequence.sequence_id,
        "mode": sequence.mode,
        "provider": sequence.provider,
        "product_root": str(sequence.git.root),
        "git_executable": sequence.git.executable,
        "host_digest": host_digest(),
        "collection_digest": sequence.collection_digest,
        "schema_fingerprint": sequence.schema_fingerprint,
        "canonical_command": list(canonical_command(sequence.provider)),
        "command_digest": command_digest(sequence.provider),
        "lease": lease.as_evidence(),
        "commit": binding.as_evidence(),
        "clean_tree_checkpoints": {
            "before_sequence": list(before_checkpoint),
            "after_sequence": list(after_checkpoint),
        },
        "consumed_control_digests": list(broker.consumed_digests),
        "runs": [result.as_evidence() for result in results],
        "run_ids": [result.run_id for result in results],
    }
    path = sequence.evidence_root / f"{sequence.sequence_id}-manifest.json"
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    atomic_write(path, payload)
    atomic_write(path.with_suffix(".sha256"), f"{sha256_text(payload)}  {path.name}\n")
    return path


# ---------------------------------------------------------------------------
# Mode plans
# ---------------------------------------------------------------------------


def scale_plans(provider: str) -> list[ChildPlan]:
    """Every candidate, one warm-up and three measured runs each, in order."""
    plans: list[ChildPlan] = []
    for count in SCALE_CANDIDATES:
        for index in range(MEASURED_RUNS + 1):
            plans.append(
                ChildPlan(
                    child_sequence=len(plans) + 1,
                    mode="scale",
                    role="warm-up" if index == 0 else f"measured-{index}",
                    worker_count=count,
                    provider=provider,
                    deadline_seconds=EXTERNAL_MAX_SECONDS,
                )
            )
    return plans


def hard_gate_plans(provider: str, workers: int) -> list[ChildPlan]:
    """The committed default, no override anywhere in the argv."""
    return [
        ChildPlan(
            child_sequence=index + 1,
            mode="hard-gate",
            role="warm-up" if index == 0 else f"measured-{index}",
            worker_count=workers,
            provider=provider,
            deadline_seconds=EXTERNAL_MAX_SECONDS,
        )
        for index in range(MEASURED_RUNS + 1)
    ]


def parity_plans(workers: int) -> list[ChildPlan]:
    """Exactly one complete explicit testcontainers child.

    Its deadline is operational rather than a performance threshold, so it is
    900 seconds and its timing is recorded observationally. A parity run over
    60 seconds is a complete result; only one that has stopped progressing is
    killed, and a killed one makes parity incomplete rather than failed-fast.
    """
    return [
        ChildPlan(
            child_sequence=1,
            mode="provider-parity",
            role="parity",
            worker_count=workers,
            provider="testcontainers",
            deadline_seconds=PARITY_TIMEOUT_SECONDS,
        )
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_mode(arguments: argparse.Namespace, environ: Mapping[str, str]) -> Path:
    """Open provenance, take the lease, run the plan, publish, release."""
    qualify_controller(environ)
    product_root = PRODUCT_ROOT
    git = open_git(environ, expected_root=product_root)
    binding = bind_commit(git, arguments.expected_commit)
    before_checkpoint = git.assert_clean(checkpoint="before-sequence")

    evidence_root = (product_root / arguments.evidence_root).resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    provider = "testcontainers" if arguments.mode == "provider-parity" else "devstack"

    if arguments.mode == "scale":
        plans = scale_plans(provider)
    elif arguments.mode == "hard-gate":
        plans = hard_gate_plans(provider, committed_worker_count(product_root))
    else:
        plans = parity_plans(committed_worker_count(product_root))
        if arguments.operational_timeout_seconds != PARITY_TIMEOUT_SECONDS:
            msg = (
                f"--operational-timeout-seconds must be {PARITY_TIMEOUT_SECONDS:.0f}; parity's deadline is "
                "an exact operational bound, not a tunable one"
            )
            raise GateError(msg)

    sequence = SequenceRun(
        mode=arguments.mode,
        provider=provider,
        evidence_root=evidence_root,
        expected_commit=binding.expected,
        git=git,
    )
    secret = new_sequence_secret()
    lease = acquire_lease(evidence_root / ".leases", provider=provider)
    broker = Broker(secret=secret, consumed_root=evidence_root / ".consumed")
    try:
        results = execute_sequence(
            sequence,
            plans,
            lease=lease,
            secret=secret,
            environment=sanitized_environment(environ),
            product_root=product_root,
        )
        # Admission closes before publication and stays closed: nothing outside
        # this sequence may present a control while its manifest is written.
        broker.close_admission()
        return publish_sequence(sequence, results, lease=lease, broker=broker, before_checkpoint=before_checkpoint)
    finally:
        release_lease(lease)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_integration_performance_gate.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("scale", "hard-gate", "provider-parity"):
        # Every mode requires --expected-commit. A sequence that names no
        # commit certifies nothing, and there is no sensible default for it.
        child = subparsers.add_parser(mode)
        child.add_argument("--evidence-root", required=True)
        child.add_argument("--expected-commit", required=True)
        child.add_argument("--print-manifest", action="store_true")
        if mode == "provider-parity":
            child.add_argument("--operational-timeout-seconds", type=float, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not hasattr(arguments, "operational_timeout_seconds"):
        arguments.operational_timeout_seconds = PARITY_TIMEOUT_SECONDS
    try:
        manifest_path = run_mode(arguments, os.environ)  # config: intentional - the ambient environment is the subject
    except (GateError, ControlError, EvidenceError, LeaseError, ProvenanceError) as error:
        print(f"performance gate: {error}", file=sys.stderr)
        return 1
    if arguments.print_manifest:
        print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
