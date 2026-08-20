#!/usr/bin/env python3
"""The only implementation behind the canonical integration-test target.

A worker flag cannot do this job. Sealing collection, owning one server,
proving every node reported exactly once, and refusing a zero-test run are
properties of a runner, and a flag on somebody else's runner can be overridden
by the caller who sets it. So the target invokes this, this invokes pytest as
`sys.executable -m pytest`, and nothing in between accepts an argument from a
caller.

Qualification is the security boundary and it fails *closed on presence*, not
on effect. If a caller exports `PYTEST=true` or preloads a Makefile that
replaces the interpreter, scrubbing that variable out of the child would
produce a clean run and a passing gate — which is precisely the outcome that
makes the evidence worthless, because the attempt succeeded at hiding itself.
The attempt is therefore the failure. Evidence records which variable names
were attempted and never their values, since the values are exactly the sort
of thing that should not be written down.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess  # noqa: S404 - the whole point of this runner is to spawn a sealed child
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from integration_control import (
    BROKER_ENDPOINT_VARIABLE,
    CONTROL_ENVIRONMENT_VARIABLE,
    ControlRejected,
    present_control,
    reject_inherited_control,
)
from integration_failure_report import report_outcomes, worker_output
from integration_reporter import EVENTS_PATH_VARIABLE, WORKER_ID_VARIABLE
from integration_schedule_inputs import frozen_history, workers_supported
from integration_scheduler import (
    DeadlineExceeded,
    IntervalWatchdog,
    NodeEvent,
    NodeOutcome,
    Phase,
    Reconciler,
    RunInvalid,
    Schedule,
    balance,
)

if TYPE_CHECKING:
    # Annotation only. Importing it at runtime would pull the provisioning
    # module into every invocation, including the sealed-runner fixture trees
    # that carry no provider modules at all.
    from integration_assignment import Assignments

REPOSITORY_ROOT: Final = Path(__file__).resolve().parent.parent
INTEGRATION_ROOT: Final = "tests/integration"

# Channels through which a caller could change what runs, what is reported, or
# which interpreter does the running. Each is listed by exact name rather than
# by prefix where the name is fixed, because a prefix match invites a later
# reader to assume the set is approximate. `PYTEST_*` is the one genuine
# family: pytest reads option channels from it that no fixed list can enumerate
# across versions.
_FORBIDDEN_EXACT: Final = frozenset(
    {
        # Replace the runner or the interpreter outright.
        "PYTEST",
        "PYTHON",
        # Inject pytest options without touching argv.
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_DEBUG",
        # Make-level command, flag, and file overrides. `MAKEFILES` is the
        # subtle one: it preloads a makefile before the target's own, so it
        # can redefine PYTEST or PYTHON without ever appearing in the
        # environment as those names. That is the only one of these channels
        # with no second detector behind it, so it keeps the blanket rule.
        "GNUMAKEFLAGS",
        "MAKEOVERRIDES",
        "MAKEFILES",
        # An inherited control would let one sequence's authentication be
        # replayed into another's child.
        "CONTEXTPLANE_INTEGRATION_CONTROL_INHERITED",
    }
)

_FORBIDDEN_PREFIXES: Final = ("PYTEST_", "GIT_")

# GNU make exports these two to every recipe unconditionally, empty when
# nobody has asked for anything. Rejecting them on presence would refuse every
# invocation made through a Makefile -- a target that can never run, which is
# the exact mirror of the zero-test pass this runner exists to replace.
#
# This is not the presence-not-effect rule bending. That rule forbids
# inspecting a value to decide whether its contents are harmless; parsing
# `MAKEFLAGS` for which overrides it carries is precisely the reasoning it
# rules out. Asking whether the variable is empty is a different question: it
# asks whether there is a message at all, and interprets no part of one. An
# empty `MAKEFLAGS` is not a channel someone scrubbed, it is a channel make
# opens whether or not anyone speaks into it.
#
# Nothing is surrendered by the narrowing. A command-line override is caught
# three independent ways -- `MAKEOVERRIDES` presence, non-empty `MAKEFLAGS`,
# and make exporting the overridden name itself into `_FORBIDDEN_EXACT`'s
# reach -- and `make -j` yields non-empty values in both, so a parallel
# invocation stays refused, as it must: a jobserver perturbs the very timing
# this runner measures.
_FORBIDDEN_WHEN_NON_EMPTY: Final = frozenset({"MAKEFLAGS", "MFLAGS"})

# Variables the child genuinely needs. Everything else is dropped rather than
# forwarded: an allowlist that grows by accident is not an allowlist.
_CHILD_ALLOWLIST: Final = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TZ",
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "VIRTUAL_ENV",
        "CONTEXTPLANE_TEST_PG",
        "CONTEXTPLANE_TEST_DATABASE_URL",
        # The manifest the assigned URL came from. Forwarded so a worker can
        # refuse a database handed to it by anything other than the broker
        # holding this sequence's lease.
        "CONTEXTPLANE_INTEGRATION_BROKER_MANIFEST_DIGEST",
        # Marks a worker as belonging to a sealed sequence. The worker side
        # keys its fail-closed requirement on this rather than on worker
        # identity, which every dispatched worker carries for the reporter.
        "CONTEXTPLANE_INTEGRATION_SEALED_RUN",
        "CONTEXTPLANE_INTEGRATION_CONTROL",
        "CONTEXTPLANE_PG_BINDIR",
        "DOCKER_HOST",
    }
)

# Only these plugins load. Autoload is off, so a plugin installed in the
# environment cannot join a measured run and change its timing or its
# reporting without appearing here first.
_REQUIRED_PLUGINS: Final = ("pytest_asyncio.plugin", "pytest_timeout")

# Carried over from the recipe this runner replaces, which passed the same
# value. A per-test ceiling, not a per-run one: the run's own boundaries are
# the watchdog's, and they bind only on a measured sequence.
_WORKER_TEST_TIMEOUT_SECONDS: Final = 180

# This module, imported by each worker so its reporter hooks are registered.
_REPORTER_MODULE: Final = "integration_reporter"

# The only modes whose contracts set the interval boundaries. provider-parity
# is deliberately absent: its timing is observational, and a parity run over
# 60 seconds is a complete result rather than a failed one.
_ENFORCING_MODES: Final = frozenset({"scale", "hard-gate"})

# Argv shapes that reselect, reorder, or re-run the suite. A measured run whose
# selection differs from the collection digest is measuring a different suite.
_FORBIDDEN_ARGV_FLAGS: Final = (
    "-k",
    "-m",
    "-x",
    "--deselect",
    "--last-failed",
    "--lf",
    "--failed-first",
    "--ff",
    "--stepwise",
    "--maxfail",
    "--reruns",
    "--flaky",
    "--only-rerun",
    "-n",
    "--numprocesses",
    "--dist",
    "--shard-id",
    "--num-shards",
)


class QualificationError(RuntimeError):
    """The invocation cannot produce qualifying evidence.

    Raised before collection and before any provider mutation, so a rejected
    attempt costs nothing and changes nothing.
    """


@dataclass(frozen=True)
class QualificationFailure:
    """What was attempted, by name only."""

    attempted_variables: tuple[str, ...]
    attempted_arguments: tuple[str, ...]
    reason: str

    def as_evidence(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            # Names, never values. A rejected attempt is still an attempt to
            # smuggle something in, and writing the payload into the artifact
            # would carry it to every reader of the bundle.
            "attempted_variables": list(self.attempted_variables),
            "attempted_arguments": list(self.attempted_arguments),
        }


def forbidden_variables(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Every forbidden channel actually present, sorted for stable evidence."""
    found = {name for name in environ if name in _FORBIDDEN_EXACT or name.startswith(_FORBIDDEN_PREFIXES)}
    found |= {name for name in _FORBIDDEN_WHEN_NON_EMPTY if environ.get(name, "")}
    return tuple(sorted(found))


def forbidden_arguments(argv: Sequence[str]) -> tuple[str, ...]:
    """Selector-shaped argv, including the `--flag=value` spelling."""
    found: list[str] = []
    for argument in argv:
        head = argument.split("=", 1)[0]
        if head in _FORBIDDEN_ARGV_FLAGS:
            found.append(head)
    return tuple(sorted(set(found)))


def qualify(environ: Mapping[str, str], argv: Sequence[str]) -> None:
    """Refuse an invocation that could have changed what runs.

    Presence is the failure, not effect. Scrubbing a forbidden variable and
    continuing would turn a tampered invocation into a passing gate, which is
    the one outcome that makes the whole sealed-evidence scheme pointless.
    """
    variables = forbidden_variables(environ)
    arguments = forbidden_arguments(argv)
    if not variables and not arguments:
        return

    reasons = []
    if variables:
        reasons.append(f"forbidden environment channel(s) present: {', '.join(variables)}")
    if arguments:
        reasons.append(f"forbidden argument(s): {', '.join(arguments)}")
    failure = QualificationFailure(
        attempted_variables=variables,
        attempted_arguments=arguments,
        reason="; ".join(reasons),
    )
    raise QualificationError(failure.reason)


def build_child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """A fresh allowlisted environment, built up rather than filtered down.

    Constructed from nothing so a variable that nobody thought about is absent
    by default. Filtering an inherited environment has the opposite default,
    and the difference shows up the first time somebody invents a new channel.
    """
    child = {name: environ[name] for name in sorted(_CHILD_ALLOWLIST) if name in environ}
    # Autoload off is not a preference. With it on, any plugin present in the
    # environment joins a measured run and can change both its timing and what
    # it reports, so two runs of one commit on two machines are not comparable.
    child["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Hash randomization changes dict and set iteration order, which changes
    # collection order, which changes the collection digest. Pinning it makes
    # the digest a property of the tree rather than of the process.
    child["PYTHONHASHSEED"] = "0"
    return child


def collection_command() -> list[str]:
    """The exact argv used to enumerate the suite. No caller input reaches it."""
    return [
        sys.executable,
        "-m",
        "pytest",
        INTEGRATION_ROOT,
        "--collect-only",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        *[argument for plugin in _REQUIRED_PLUGINS for argument in ("-p", plugin)],
    ]


def worker_command(node_ids: Sequence[str]) -> list[str]:
    """One worker's argv. Node IDs come from our own collection, not a caller.

    The reporter is loaded explicitly rather than through a conftest so that a
    worker's disclosure path is part of the argv this runner builds, not
    something the tree under test could redefine.
    """
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        *[argument for plugin in _REQUIRED_PLUGINS for argument in ("-p", plugin)],
        # The per-test backstop the timeout plugin is loaded for. Without it
        # the plugin loads and does nothing, and a single wedged test hangs a
        # run forever on any invocation the interval watchdog is not enforcing
        # -- which is every ordinary one. Far above the measured ceilings, so
        # it never fires ahead of a boundary during a measured sequence; it is
        # the floor under runs that have no boundaries at all.
        f"--timeout={_WORKER_TEST_TIMEOUT_SECONDS}",
        "-p",
        _REPORTER_MODULE,
        *node_ids,
    ]


def collection_digest(node_ids: Sequence[str]) -> str:
    """A digest over the sorted node list.

    Sorted so that collection order — which pytest does not promise across
    filesystems — cannot change the digest, while adding or removing a single
    test always does.
    """
    payload = "\n".join(sorted(node_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Collection:
    """What the suite contains, and the digest that pins it."""

    node_ids: tuple[str, ...]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "digest", collection_digest(self.node_ids))

    def as_evidence(self) -> dict[str, object]:
        return {"node_count": len(self.node_ids), "collection_digest": self.digest}


def parse_collection(stdout: str) -> tuple[str, ...]:
    """Read node IDs out of `--collect-only -q` output.

    Everything after the first blank line is pytest's summary, and lines
    without `::` are directory headers or warnings. Both are excluded by shape
    rather than by pattern-matching pytest's prose, which changes between
    versions.
    """
    node_ids: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            break
        if "::" not in line:
            continue
        node_ids.append(line)
    return tuple(node_ids)


def collect(environ: Mapping[str, str], *, cwd: Path | None = None) -> Collection:
    """Enumerate the whole integration root. An empty result is fatal.

    A zero-node run that exits 0 is the failure mode this whole phase exists
    to make impossible: it looks exactly like a fast, healthy suite.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no caller input
        collection_command(),
        env=build_child_environment(environ),
        cwd=str(cwd or REPOSITORY_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        msg = f"collection failed with exit {completed.returncode}: {completed.stderr.strip()[:400]}"
        raise QualificationError(msg)

    node_ids = parse_collection(completed.stdout)
    if not node_ids:
        msg = "collection produced zero nodes; a zero-test run cannot qualify"
        raise QualificationError(msg)
    return Collection(node_ids=node_ids)


#: Where the committed worker default lives once a scale sequence has selected
#: it. Absent until then, which is why this reads as "not yet measured" rather
#: than defaulting to a number nobody chose.
_WORKER_COUNT_TABLE: Final = ("tool", "contextplane", "integration")
_WORKER_COUNT_FIELD: Final = "workers"


def committed_worker_count(*, pyproject: Path | None = None) -> int:
    """The tracked default, or 1 when no scale sequence has committed one.

    Serial is the honest fallback: it is the only count whose correctness does
    not depend on a measurement that has not been taken. Defaulting to a
    parallel count would let an unmeasured topology run under a number that
    looks selected.
    """
    path = pyproject or (REPOSITORY_ROOT / "pyproject.toml")
    if not path.is_file():
        return 1
    table: Any = tomllib.loads(path.read_text(encoding="utf-8"))
    for key in _WORKER_COUNT_TABLE:
        table = table.get(key) if isinstance(table, Mapping) else None
        if table is None:
            return 1
    committed = table.get(_WORKER_COUNT_FIELD) if isinstance(table, Mapping) else None
    if committed is None:
        return 1
    if not isinstance(committed, int) or isinstance(committed, bool) or committed < 1:
        msg = f"committed worker count must be a positive integer, got {committed!r}"
        raise QualificationError(msg)
    return committed


# --- the worker's half of the contract ---------------------------------------
#
# Lives in `integration_reporter`, which is what a worker loads by name. The
# disclosure format belongs to the side that writes it; this side only needs
# to know how to address a worker and how to read back what it wrote.

# --- the parent's half ---------------------------------------------------------


@dataclass(frozen=True)
class WorkerResult:
    """One worker process, after it stopped."""

    worker_id: str
    returncode: int
    stdout: str
    stderr: str


def parse_events(payload: str) -> tuple[NodeEvent, ...]:
    """Read one worker's stream. A malformed line voids the run.

    Tolerating an unreadable record would mean the aggregation silently
    describes fewer nodes than ran, which is the shape of every failure this
    runner exists to refuse.
    """
    events: list[NodeEvent] = []
    for number, raw in enumerate(payload.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            event = NodeEvent(
                worker_id=str(record["worker_id"]),
                sequence=int(record["sequence"]),
                node=str(record["node"]),
                outcome=NodeOutcome(record["outcome"]) if record["outcome"] is not None else None,
                started=bool(record["started"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            msg = f"malformed worker event on line {number}: {error}"
            raise RunInvalid(msg) from error
        events.append(event)
    return tuple(events)


def _terminate_group(process: subprocess.Popen[str], grace_seconds: float) -> None:
    """TERM the worker's own process group, then KILL whatever survived.

    The group rather than the process: pytest's own children -- a container, a
    server -- outlive a bare kill of the interpreter, and a leaked child is
    both a resource leak and a contaminant for the next measured run.
    """
    for send, wait in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, None)):
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), send)
        except (ProcessLookupError, PermissionError):
            return
        if wait is None:
            return
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue


def dispatch(
    schedule: Schedule,
    environ: Mapping[str, str],
    *,
    events_root: Path,
    cwd: Path | None = None,
    watchdog: IntervalWatchdog | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    poll_seconds: float = 0.05,
    databases: Assignments | None = None,
) -> tuple[dict[str, NodeOutcome], tuple[WorkerResult, ...]]:
    """Run every assignment once and reconcile what came back.

    One attempt per node, as the contract requires: nothing here reschedules,
    retries, or salvages. A worker that dies takes the run with it, because a
    partial aggregation that exits zero is worse than no measurement at all.

    `databases` carries each worker's assigned URL when a sequence provisioned
    them. It is optional because a developer running the tier by hand has no
    broker and needs none; in that case each worker falls back to the ordinary
    provider path. Under a sequence it is required for every dispatched
    worker, and a worker with no assignment is refused rather than allowed to
    provision its own server -- a self-provisioned worker would be green with
    timing indistinguishable from an assigned one.
    """
    events_root.mkdir(parents=True, exist_ok=True)
    base_environment = build_child_environment(environ)

    processes: list[tuple[str, subprocess.Popen[str], Path]] = []
    for assignment in schedule.assignments:
        events_path = events_root / f"events-{assignment.worker_id}.jsonl"
        events_path.touch()
        worker_environment = dict(base_environment)
        # The worker imports this module by name to load the reporter, so the
        # directory holding it has to be importable in the child regardless of
        # where the child is rooted.
        existing = worker_environment.get("PYTHONPATH", "")
        scripts_directory = str(Path(__file__).resolve().parent)
        worker_environment["PYTHONPATH"] = (
            f"{scripts_directory}{os.pathsep}{existing}" if existing else scripts_directory
        )
        worker_environment[EVENTS_PATH_VARIABLE] = str(events_path)
        worker_environment[WORKER_ID_VARIABLE] = assignment.worker_id
        if databases is not None:
            # Raises for an unassigned worker rather than dispatching it. The
            # worker side fails closed on a missing URL, so this would be
            # caught either way -- but it is caught here naming the worker,
            # instead of inside a child as a missing-variable error.
            worker_environment.update(databases.environment(assignment.worker_id))
        process = subprocess.Popen(  # noqa: S603 - argv is built from our own collection
            worker_command(assignment.nodes),
            env=worker_environment,
            cwd=str(cwd or REPOSITORY_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Its own session, so the whole group can be signalled at once.
            start_new_session=True,
        )
        processes.append((assignment.worker_id, process, events_path))

    violation: DeadlineExceeded | None = None
    while any(process.poll() is None for _, process, _ in processes):
        if watchdog is not None:
            try:
                watchdog.check()
            except DeadlineExceeded as exceeded:
                violation = exceeded
                break
        time.sleep(poll_seconds)

    if violation is not None:
        grace = watchdog.grace_seconds() if watchdog is not None else 0.5
        deadline = monotonic() + grace
        for _, process, _ in processes:
            _terminate_group(process, max(0.0, deadline - monotonic()))

    results: list[WorkerResult] = []
    for worker_id, process, _ in processes:
        stdout, stderr = process.communicate()
        results.append(
            WorkerResult(worker_id=worker_id, returncode=process.returncode, stdout=stdout or "", stderr=stderr or "")
        )

    reconciler = Reconciler(schedule=schedule)
    for _, _, events_path in processes:
        for event in parse_events(events_path.read_text(encoding="utf-8")):
            reconciler.record(event)

    if violation is not None:
        raise violation

    # Reconciliation before exit codes, deliberately. A worker exits nonzero
    # both when tests failed and when it died holding results; only the event
    # stream distinguishes those, and the second must not be reported as the
    # first. `finalize` raises if any node went undisclosed.
    return reconciler.finalize(), tuple(results)


def authorize(environ: Mapping[str, str]) -> Mapping[str, Any] | None:
    """Present this child's control to the broker, before anything is collected.

    Returns the authenticated bound fields, or `None` when the runner is being
    invoked outside a sealed sequence -- a developer running the suite by hand
    has no controller and needs none. What must never happen is a child that
    was *given* a control or a broker and proceeds without an affirmative
    answer, so a half-configured invocation is refused rather than downgraded.
    """
    reject_inherited_control(environ)
    control = environ.get(CONTROL_ENVIRONMENT_VARIABLE)
    endpoint = environ.get(BROKER_ENDPOINT_VARIABLE)
    if not control and not endpoint:
        return None
    if not control or not endpoint:
        present, missing = (
            (CONTROL_ENVIRONMENT_VARIABLE, BROKER_ENDPOINT_VARIABLE)
            if control
            else (BROKER_ENDPOINT_VARIABLE, CONTROL_ENVIRONMENT_VARIABLE)
        )
        msg = f"{present} is set but {missing} is not; a control with nobody to authenticate it is not authorization"
        raise ControlRejected(msg)
    return present_control(Path(endpoint), Path(control))


def resolve_worker_count(authorized: Mapping[str, Any] | None, *, requested: int | None) -> int:
    """The count the controller authorized, or the tracked default.

    Under a sealed sequence the control wins outright and `--workers` is
    refused rather than ignored. The canonical command is byte-identical across
    candidates, so the control is the only channel that distinguishes them; a
    child that let argv override it would run one configuration while its
    evidence claimed four.
    """
    if authorized is None:
        return requested if requested is not None else committed_worker_count()
    if requested is not None:
        msg = "--workers cannot be given to an authorized child; its worker count is bound into the control"
        raise ControlRejected(msg)
    bound = authorized.get("worker_count")
    if not isinstance(bound, int) or isinstance(bound, bool) or bound < 1:
        msg = f"control binds an unusable worker count: {bound!r}"
        raise ControlRejected(msg)
    return bound


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Deliberately tiny.

    The runner takes a worker count and nothing else. There is no passthrough
    for pytest arguments, and adding one would reopen every channel
    qualification closes.
    """
    parser = argparse.ArgumentParser(
        prog="run_integration_tests.py",
        description="Run the integration tier under sealed collection and scheduling.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Worker count. Defaults to the tracked committed default.",
    )
    return parser.parse_args(argv)


@contextmanager
def provisioned_databases(
    authorized: Mapping[str, Any] | None,
    schedule: Schedule,
    *,
    provider: str,
) -> Iterator[Assignments | None]:
    """One database per worker for the length of a sealed sequence.

    Yields `None` outside a sequence. A developer running the tier by hand has
    no controller and no broker, and each worker resolves a database the
    ordinary way -- provisioning a server for them here would make the
    canonical command unusable without establishing anything.

    The provisioning itself lives in the assignment module and is imported
    only on the branch that provisions. At module scope it would pull the
    provider modules into every invocation, including the sealed-runner
    fixture trees that carry none, where the child dies with
    `ModuleNotFoundError` -- which reads as a broken runner rather than a
    missing optional dependency.
    """
    if authorized is None:
        yield None
        return

    from integration_assignment import (  # noqa: PLC0415 - deferred: reaches a real server
        AssignmentError,
        provision_for_sequence,
    )

    try:
        with provision_for_sequence(
            authorized,
            [assignment.worker_id for assignment in schedule.assignments],
            provider=provider,
        ) as databases:
            yield databases
    except AssignmentError as failure:
        # Translated because the symbol only exists behind the deferred
        # import: a worker dispatched with no database is a run that cannot
        # produce a measurement, which is what RunInvalid already means here.
        raise RunInvalid(str(failure)) from failure


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        qualify(os.environ, arguments)  # config: intentional - the ambient environment is the thing under inspection
    except QualificationError as error:
        print(f"integration runner: refusing to run: {error}", file=sys.stderr)
        return 2

    options = _parse_args(arguments)
    # Timing starts before anything else, including before the control that says
    # whether these boundaries bind. Enforcement is switched on below once the
    # control has been read; the intervals are measured either way.
    watchdog = IntervalWatchdog(monotonic=time.monotonic, enforcing=False)

    watchdog.enter(Phase.PROVISIONING)
    try:
        authorized = authorize(os.environ)  # config: intentional - the controller addresses its child by environment
    except ControlRejected as rejected:
        print(f"integration runner: refusing to collect: {rejected}", file=sys.stderr)
        return 2
    # The boundaries belong to the sequences they were set for. A developer
    # running the tier is not making a performance claim, and an ordinary
    # invocation failed by a candidate's ceiling would make the canonical
    # command unusable without establishing anything.
    watchdog.enforcing = authorized is not None and authorized.get("mode") in _ENFORCING_MODES
    try:
        collection = collect(os.environ)  # config: intentional - the child environment is built from the ambient one
    except QualificationError as error:
        print(f"integration runner: {error}", file=sys.stderr)
        return 2
    provider = os.environ.get("CONTEXTPLANE_TEST_PG", "")  # config: intentional - the provider keys duration history
    try:
        workers = workers_supported(resolve_worker_count(authorized, requested=options.workers), provider=provider)
    except ControlRejected as rejected:
        print(f"integration runner: refusing to run: {rejected}", file=sys.stderr)
        return 2
    history = frozen_history(collection.digest, provider=provider, workers=workers)
    schedule = balance(collection.node_ids, workers=workers, history=history)
    try:
        watchdog.leave(Phase.PROVISIONING)
    except DeadlineExceeded as exceeded:
        print(f"integration runner: run invalid: {exceeded}", file=sys.stderr)
        return 1

    print(
        f"integration runner: collected {len(collection.node_ids)} nodes ({collection.digest[:12]}), "
        f"{workers} worker(s)"
    )

    events_root = Path(tempfile.mkdtemp(prefix="contextplane-integration-events-"))
    watchdog.enter(Phase.EXECUTION)
    try:
        with provisioned_databases(authorized, schedule, provider=provider) as databases:
            outcomes, results = dispatch(
                schedule,
                os.environ,  # config: intentional - the child environment is built from the ambient one
                events_root=events_root,
                watchdog=watchdog,
                databases=databases,
            )
    except DeadlineExceeded as exceeded:
        print(f"integration runner: run invalid: {exceeded}", file=sys.stderr)
        return 1
    except RunInvalid as invalid:
        print(f"integration runner: run invalid: {invalid}", file=sys.stderr)
        return 1
    try:
        watchdog.leave(Phase.EXECUTION)
        watchdog.enter(Phase.TEARDOWN)
        # Worker stderr, plus the failure section of stdout -- see
        # `integration_failure_report` for why the second half is not optional.
        for line in worker_output((r.stderr, r.stdout) for r in results):
            print(line, file=sys.stderr, end="")
        watchdog.leave(Phase.TEARDOWN)
    except DeadlineExceeded as exceeded:
        # Closing an interval is where an overrun is detected, so these calls
        # need the same handler the dispatch does. Without it the run reports a
        # traceback instead of naming the boundary it crossed.
        print(f"integration runner: run invalid: {exceeded}", file=sys.stderr)
        return 1

    return report_outcomes({node: outcome.value for node, outcome in outcomes.items()})


if __name__ == "__main__":
    raise SystemExit(main())
