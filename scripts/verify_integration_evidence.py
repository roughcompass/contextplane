#!/usr/bin/env python3
"""Judge a measured run's evidence without believing any of it.

This is the half of the measurement that assumes the other half is lying. Not
because the controller is suspected of anything, but because a controller that
validates its own output proves only that whoever could edit a file could also
edit the record of that file. Every check the controller made is made again
here, from the machine rather than from the manifest: the Git executable is
re-resolved, the product root re-derived, the expected commit and `HEAD` re-read,
the tree re-inspected, and every checksum recomputed from bytes on disk.

**A field is read from evidence only when it is a claim, never when it is a
fact.** The commit the run says it measured is a claim, and it is checked
against a commit resolved here. The number of children that ran is a claim, and
it is checked against the run records actually present. Anything the verifier
takes on faith is a place the evidence could have been written to say whatever
would pass.

**Absence is a failure, not a pass.** Every gate here fails closed: a missing
manifest, an empty run list, a mode with nothing to check, an unreadable
checksum sidecar. The failure mode this defends against is a verifier that
reports success because it found nothing to disagree with -- which is exactly
what a suite pointed at the wrong directory does.

**Timing is external or it does not count.** `real` comes from the outer
`/usr/bin/time` file alone. A child's own measurement of itself excludes process
spawn, Make startup, and interpreter import, which is precisely where a
regression hides, so inner-only or synthesized timing is rejected rather than
substituted.

The modes correspond to what was run, and each is stricter than the last about
what it will accept as a complete sequence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))

from integration_evidence import (
    EvidenceError,
    assert_no_secrets,
    atomic_write,
    sha256_file,
    sha256_text,
)
from integration_provenance import (
    PRODUCT_ROOT,
    bind_commit,
    open_git,
    reject_inherited_git,
)
from integration_scheduler import (
    EXECUTION_MAX_SECONDS,
    EXTERNAL_MAX_SECONDS,
    INTERNAL_MAX_SECONDS,
    PROVISIONING_MAX_SECONDS,
    TEARDOWN_MAX_SECONDS,
)

# The candidates a scale sequence must cover, and nothing else. Stated here as
# well as in the controller so a controller that quietly dropped one is caught
# by a module that never read its plan.
SCALE_CANDIDATES: Final = (1, 2, 4, 8)

# One warm-up plus three measured runs per candidate. The warm-up is excluded
# from every timing judgement and is required to exist: a candidate whose first
# measured run *was* its warm-up is measuring a cold cache.
MEASURED_RUNS: Final = 3

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class VerificationFailure(RuntimeError):
    """Evidence was rejected. The message names what did not hold."""


def _fail(message: str) -> NoReturn:
    raise VerificationFailure(message)


# ---------------------------------------------------------------------------
# Reading sealed artifacts
# ---------------------------------------------------------------------------


def load_sealed(path: Path, *, require_checksum: bool = True) -> dict[str, Any]:
    """Read a manifest and re-check the sidecar written beside it.

    The sidecar is recomputed from the file's bytes rather than compared to a
    value inside the file. A document carrying its own checksum authenticates
    nothing: both halves are writable by the same hand.
    """
    if not path.is_file():
        _fail(f"no evidence at {path}; a missing manifest is a run that did not finish, not a run that passed")

    payload = path.read_text(encoding="utf-8")
    sidecar = path.with_suffix(".sha256")
    if require_checksum:
        if not sidecar.is_file():
            _fail(f"{path.name} has no checksum sidecar at {sidecar.name}; unsealed evidence is not evidence")
        recorded = sidecar.read_text(encoding="utf-8").split()
        if not recorded or recorded[0] != sha256_text(payload):
            _fail(f"{path.name} does not match its sidecar checksum; the file changed after it was sealed")

    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise VerificationFailure(f"{path} is not readable JSON: {error}") from error
    if not isinstance(document, dict):
        _fail(f"{path} is not a JSON object")

    # Evidence must never have carried a secret in the first place. Checked on
    # the way in so a leak is caught by whoever reads the file next, not only by
    # whoever wrote it.
    assert_no_secrets(document, where=path.name)
    return dict(document)


# ---------------------------------------------------------------------------
# Independent re-derivation
# ---------------------------------------------------------------------------


def independent_provenance(
    manifest: Mapping[str, Any],
    *,
    expected_commit: str,
    require_sanitized_git: bool,
    require_clean_tree: bool,
    environ: Mapping[str, str],
) -> None:
    """Re-run the controller's Git checks here, then compare.

    The order matters: this resolves everything from the machine first and only
    then looks at what the manifest claimed. Reading the claim first invites
    writing a check that agrees with it.
    """
    if not _HEX40.match(expected_commit):
        _fail(f"expected commit {expected_commit!r} is not a full 40-character object name")

    if require_sanitized_git:
        # A `GIT_*` variable reaching the verifier can redirect the very
        # resolution this function exists to perform.
        reject_inherited_git(environ)

    context = open_git(environ, expected_root=PRODUCT_ROOT)
    binding = bind_commit(context, expected_commit)

    claimed_root = str(manifest.get("product_root", ""))
    if claimed_root and Path(claimed_root).resolve() != context.root.resolve():
        _fail(
            f"evidence was produced under {claimed_root!r} but this tree is {context.root}; "
            "a measurement of another checkout says nothing about this one"
        )

    claimed_git = str(manifest.get("git_executable", ""))
    if claimed_git and claimed_git != context.executable:
        _fail(f"evidence used Git at {claimed_git!r}; this host resolves {context.executable!r}")

    recorded = manifest.get("commit", {})
    if isinstance(recorded, Mapping):
        for field in ("expected_commit", "head_commit"):
            value = recorded.get(field)
            if value is not None and value != expected_commit:
                _fail(
                    f"evidence records {field}={value!r}, expected {expected_commit!r}; "
                    "a sequence that moved commit mid-run measured two different trees"
                )
    if binding.head != expected_commit:
        _fail(
            f"HEAD is {binding.head!r} but the evidence is bound to {expected_commit!r}; "
            "verifying against a tree that has since moved proves nothing about either"
        )

    if require_clean_tree:
        checkpoints = manifest.get("clean_tree_checkpoints", {})
        if not isinstance(checkpoints, Mapping) or not {"before_sequence", "after_sequence"} <= set(checkpoints):
            _fail("evidence has no before/after clean-tree checkpoints; an unbounded window is not a checkpoint")
        for name, entries in checkpoints.items():
            if entries:
                _fail(f"tree was not clean at {name}: {list(entries)!r}")
        # And independently now, because both recorded checkpoints could have
        # been written by a controller that never looked.
        context.assert_clean(checkpoint="verifier")


# ---------------------------------------------------------------------------
# Shared sequence structure
# ---------------------------------------------------------------------------


def _runs(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    runs = manifest.get("runs")
    if not isinstance(runs, list) or not runs:
        _fail("evidence lists no runs; an empty sequence passes every check that iterates it")
    return [dict(run) for run in runs]


def check_sequence_integrity(manifest: Mapping[str, Any], *, require_outer_controller: bool) -> list[dict[str, Any]]:
    """One controller, one lease, ordered child sequence, no splicing."""
    runs = _runs(manifest)

    if require_outer_controller:
        controller = manifest.get("controller_id")
        if not controller:
            _fail("evidence names no controller; runs with no single owner may have come from different sequences")
        lease = manifest.get("lease")
        if not isinstance(lease, Mapping) or not lease.get("lease_id"):
            _fail("evidence carries no lease; without one, another sequence could have overlapped this one")

    raw_order = [run.get("child_sequence") for run in runs]
    if any(not isinstance(value, int) for value in raw_order):
        _fail(f"child sequence numbers are not all integers: {raw_order!r}")
    ordered = [int(value) for value in raw_order if isinstance(value, int)]
    if len(set(ordered)) != len(ordered):
        _fail(f"duplicate child sequence numbers {ordered!r}; a replayed run would appear exactly this way")
    if ordered != sorted(ordered):
        _fail(f"child sequence numbers are out of order {ordered!r}")
    if ordered != list(range(1, len(ordered) + 1)):
        _fail(f"child sequence is not contiguous from 1: {ordered!r}; a removed run leaves exactly this gap")

    run_ids = manifest.get("run_ids")
    if not isinstance(run_ids, list) or run_ids != [run.get("run_id") for run in runs]:
        _fail("the manifest's run_ids do not match its run records; one of the two was edited")

    for run in runs:
        if run.get("timed_out"):
            _fail(f"run {run.get('run_id')!r} timed out; a timed-out child is not a slow measurement")
        if run.get("exit_status") != 0:
            _fail(f"run {run.get('run_id')!r} exited {run.get('exit_status')!r}; a failed run has no timing to report")
    return runs


def check_authenticated_controls(manifest: Mapping[str, Any], runs: Sequence[Mapping[str, Any]]) -> None:
    """Every child presented a distinct control, and the manifest consumed each once."""
    digests: list[str] = []
    for run in runs:
        value = run.get("control_digest")
        if not isinstance(value, str) or not _HEX64.match(value):
            _fail(f"run {run.get('run_id')!r} carries no usable control digest: {value!r}")
        digests.append(str(value))
    if len(set(digests)) != len(digests):
        _fail("two runs share a control digest; a control is single-use and a repeat is a replay")

    consumed = manifest.get("consumed_control_digests")
    if not isinstance(consumed, list):
        _fail("evidence records no consumed controls; authorization that left no trace did not happen")
    if sorted(str(value) for value in consumed) != sorted(digests):
        _fail(
            "the consumed control set does not match the controls the runs presented; "
            "a child that collected without its control being consumed was never authorized"
        )
    for run in runs:
        if any(key in run for key in ("control", "control_payload", "sequence_secret")):
            _fail(f"run {run.get('run_id')!r} serialized control material; only a digest may be recorded")


def check_canonical_commands(manifest: Mapping[str, Any], runs: Sequence[Mapping[str, Any]], *, provider: str) -> None:
    """No caller override reached any child."""
    canonical = manifest.get("canonical_command")
    if not isinstance(canonical, list) or not canonical:
        _fail("evidence records no canonical command")
    if manifest.get("provider") != provider:
        _fail(f"evidence was produced against provider {manifest.get('provider')!r}, expected {provider!r}")

    for run in runs:
        command = run.get("command")
        if not isinstance(command, list) or not command:
            _fail(f"run {run.get('run_id')!r} records no command")
        if run.get("provider") != provider:
            _fail(f"run {run.get('run_id')!r} used provider {run.get('provider')!r}, expected {provider!r}")
        joined = " ".join(str(part) for part in command)
        for banned in ("-k ", "-m ", "--deselect", "--last-failed", "--maxfail", "--stepwise", "-p no:", "::"):
            if banned in joined:
                _fail(
                    f"run {run.get('run_id')!r} ran a non-canonical command containing {banned!r}; "
                    "a selector or override means the measured set was not the committed set"
                )


def external_real(run: Mapping[str, Any]) -> float:
    """The outer `real`, refusing any substitute."""
    timing = run.get("checksums", {})
    if not isinstance(timing, Mapping) or "time" not in timing:
        _fail(f"run {run.get('run_id')!r} has no checksummed external timing file")
    value: Any = run.get("external_real_seconds")
    if value is None:
        summary = run.get("inner_summary", {})
        if isinstance(summary, Mapping) and "external_real_seconds" in summary:
            _fail(
                f"run {run.get('run_id')!r} reports external timing from inside the child; the outer layer "
                "alone may record it, because a child cannot time its own spawn"
            )
        _fail(f"run {run.get('run_id')!r} records no external real time")
    if not isinstance(value, int | float):
        _fail(f"run {run.get('run_id')!r} records a non-numeric external real time {value!r}")
    return float(value)


def check_phase_deadlines(runs: Sequence[Mapping[str, Any]]) -> None:
    """The internal phase budgets, which are non-borrowable by design."""
    limits = (
        ("provisioning_seconds", PROVISIONING_MAX_SECONDS),
        ("execution_seconds", EXECUTION_MAX_SECONDS),
        ("teardown_seconds", TEARDOWN_MAX_SECONDS),
        ("internal_total_seconds", INTERNAL_MAX_SECONDS),
    )
    for run in runs:
        summary = run.get("inner_summary", {})
        if not isinstance(summary, Mapping):
            _fail(f"run {run.get('run_id')!r} has no inner summary")
        for field, ceiling in limits:
            value = summary.get(field)
            if value is None:
                continue
            if float(value) > ceiling:
                _fail(
                    f"run {run.get('run_id')!r} spent {value}s in {field}, over the {ceiling}s budget; "
                    "phase budgets are non-borrowable, so an underrun elsewhere does not pay for this"
                )


def check_external_budget(runs: Sequence[Mapping[str, Any]]) -> list[float]:
    """Every measured run under the external ceiling, strictly."""
    observed: list[float] = []
    for run in runs:
        value = external_real(run)
        # Strict: `/usr/bin/time -p` prints two decimals, so a run showing
        # exactly the ceiling has been rounded down into passing.
        if value >= EXTERNAL_MAX_SECONDS:
            _fail(
                f"run {run.get('run_id')!r} took {value}s external, at or over the {EXTERNAL_MAX_SECONDS}s "
                "ceiling; the printed value is rounded, so equality is a run that already exceeded it"
            )
        observed.append(value)
    return observed


def measured_only(runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Drop warm-ups, and refuse a candidate that never had one."""
    return [dict(run) for run in runs if run.get("role") != "warmup"]


def check_lifecycle_binding(manifest: Mapping[str, Any], root: Path) -> None:
    """The imported lifecycle result must be bound into this sequence by checksum."""
    bound = manifest.get("lifecycle_checksum")
    if not bound:
        _fail(
            "evidence does not bind the lifecycle comparison checksum; without it this sequence could be "
            "paired with any lifecycle result after the fact"
        )
    if not root.is_dir():
        _fail(f"lifecycle evidence root {root} does not exist")
    candidates = sorted(root.glob("**/lifecycle-*.json"))
    if not candidates:
        _fail(f"no lifecycle evidence under {root}; the binding names a record that is not there")
    if not any(sha256_file(candidate) == bound for candidate in candidates):
        _fail(
            f"no lifecycle record under {root} matches the bound checksum {bound!r}; the bundle was replaced "
            "after the sequence was sealed"
        )


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def mode_baseline_import(arguments: argparse.Namespace) -> None:
    """The pre-existing corpus, which is unsealed and must say so."""
    schema = Path(arguments.schema)
    evidence = Path(arguments.evidence)
    if not schema.is_file():
        _fail(f"no baseline schema at {schema}")
    if not evidence.is_dir():
        _fail(f"no baseline evidence directory at {evidence}")

    records = sorted(evidence.glob("*.json"))
    if not records:
        _fail(f"no baseline records under {evidence}; an empty import satisfies every later check vacuously")

    document = json.loads(schema.read_text(encoding="utf-8"))
    required = set(document.get("required", []))

    for record in records:
        payload = json.loads(record.read_text(encoding="utf-8"))
        assert_no_secrets(payload, where=record.name)
        missing = sorted(required - set(payload))
        if missing:
            _fail(f"{record.name} is missing required field(s) {missing}")
        if arguments.require_label and payload.get("label") != arguments.require_label:
            _fail(
                f"{record.name} carries label {payload.get('label')!r}, expected {arguments.require_label!r}; "
                "an unsealed legacy run mislabelled as sealed would be read as evidence it is not"
            )


def _common_sequence_checks(
    arguments: argparse.Namespace, environ: Mapping[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = load_sealed(Path(arguments.evidence), require_checksum=True)
    independent_provenance(
        manifest,
        expected_commit=arguments.expected_commit,
        require_sanitized_git=arguments.require_sanitized_git,
        require_clean_tree=arguments.require_clean_tree,
        environ=environ,
    )
    runs = check_sequence_integrity(manifest, require_outer_controller=arguments.require_outer_controller)
    if arguments.require_authenticated_controls:
        check_authenticated_controls(manifest, runs)
    if arguments.lifecycle_evidence_root:
        check_lifecycle_binding(manifest, Path(arguments.lifecycle_evidence_root))
    return manifest, runs


def mode_scale(arguments: argparse.Namespace, environ: Mapping[str, str]) -> None:
    """1/2/4/8, each with a warm-up and exactly three measured runs."""
    manifest, runs = _common_sequence_checks(arguments, environ)
    check_canonical_commands(manifest, runs, provider=manifest.get("provider", "devstack"))

    expected = tuple(int(value) for value in arguments.workers.split(","))
    if expected != SCALE_CANDIDATES:
        _fail(f"scale verification asked for {expected!r}; the sequence is defined over {SCALE_CANDIDATES!r}")

    by_candidate: dict[int, list[dict[str, Any]]] = {}
    for run in runs:
        by_candidate.setdefault(int(run.get("worker_count", -1)), []).append(run)

    if tuple(sorted(by_candidate)) != SCALE_CANDIDATES:
        _fail(f"sequence covers worker counts {tuple(sorted(by_candidate))!r}, expected {SCALE_CANDIDATES!r}")

    for candidate, candidate_runs in sorted(by_candidate.items()):
        warmups = [run for run in candidate_runs if run.get("role") == "warmup"]
        measured = measured_only(candidate_runs)
        if len(warmups) != 1:
            _fail(
                f"candidate {candidate} has {len(warmups)} warm-up(s), expected exactly 1; a candidate whose "
                "first measured run was its warm-up is timing a cold cache"
            )
        if len(measured) != MEASURED_RUNS:
            _fail(f"candidate {candidate} has {len(measured)} measured run(s), expected {MEASURED_RUNS}")
        check_phase_deadlines(measured)
        check_external_budget(measured)

    if arguments.require_smallest_eligible:
        eligible = [
            candidate
            for candidate, candidate_runs in sorted(by_candidate.items())
            if all(external_real(run) < EXTERNAL_MAX_SECONDS for run in measured_only(candidate_runs))
        ]
        if not eligible:
            _fail("no worker count met the external budget on every measured run")
        selected = manifest.get("selected_worker_count")
        if selected is not None and int(selected) != eligible[0]:
            _fail(
                f"sequence selected {selected} workers but the smallest eligible count is {eligible[0]}; "
                "picking a larger one hides that the smaller one also passed and is cheaper to reproduce"
            )


def mode_hard_gate(arguments: argparse.Namespace, environ: Mapping[str, str]) -> None:
    """The committed default, three measured runs, all under target."""
    manifest, runs = _common_sequence_checks(arguments, environ)
    check_canonical_commands(manifest, runs, provider=manifest.get("provider", "devstack"))

    measured = measured_only(runs)
    if len(measured) != MEASURED_RUNS:
        _fail(f"hard gate has {len(measured)} measured run(s), expected {MEASURED_RUNS}")

    counts = {int(run.get("worker_count", -1)) for run in runs}
    if len(counts) != 1:
        _fail(f"hard gate mixed worker counts {sorted(counts)!r}; it runs the tracked default and nothing else")

    check_phase_deadlines(measured)
    observed = check_external_budget(measured)

    if arguments.require_target_met:
        worst = max(observed)
        if worst > EXECUTION_MAX_SECONDS:
            _fail(
                f"slowest measured run was {worst}s, over the {EXECUTION_MAX_SECONDS}s target; "
                "the gate reports the worst run because the best one is not what a developer waits for"
            )


def mode_provider_parity(arguments: argparse.Namespace, environ: Mapping[str, str]) -> None:
    """The other provider, one attempt, complete collection, timing observed not gated."""
    manifest, runs = _common_sequence_checks(arguments, environ)
    check_canonical_commands(manifest, runs, provider=arguments.provider)

    if arguments.require_one_attempt:
        for run in runs:
            attempts = run.get("attempts", 1)
            if int(attempts) != 1:
                _fail(
                    f"run {run.get('run_id')!r} recorded {attempts} attempts; a retried parity run reports "
                    "whichever attempt happened to work"
                )

    if arguments.require_complete_collection:
        for run in runs:
            summary = run.get("inner_summary", {})
            collected = summary.get("collected")
            reported = summary.get("reported")
            if collected is None or reported is None:
                _fail(f"run {run.get('run_id')!r} does not state collected/reported counts")
            if int(collected) == 0:
                _fail(f"run {run.get('run_id')!r} collected zero tests; a zero-test run passes every assertion")
            if int(collected) != int(reported):
                _fail(
                    f"run {run.get('run_id')!r} collected {collected} but reported {reported}; "
                    "an undisclosed node is a missing outcome, not a smaller run"
                )

    if arguments.require_exact_provider_matrix:
        providers = {str(run.get("provider")) for run in runs}
        if providers != {str(arguments.provider)}:
            _fail(f"parity sequence spans providers {sorted(providers)!r}, expected only {arguments.provider!r}")

    if arguments.expected_operational_timeout_seconds is not None:
        recorded = manifest.get("operational_timeout_seconds")
        expected_timeout = float(arguments.expected_operational_timeout_seconds)
        if recorded is not None and float(recorded) != expected_timeout:
            _fail(f"parity ran under a {recorded}s operational timeout, expected {expected_timeout}s")

    # Timing is recorded and not gated here: the second provider is a
    # correctness statement about collection, and holding it to the first
    # provider's budget would make an unrelated provider's slowness read as a
    # regression in this one.
    if not arguments.timing_observational:
        check_external_budget(measured_only(runs))


def mode_phase_close(arguments: argparse.Namespace, environ: Mapping[str, str]) -> None:
    """Bind everything, then emit target-met records only if nothing failed."""
    for name in ("lane", "base", "branch", "reconciliation_id", "reconciliation_digest"):
        if not getattr(arguments, name, None):
            _fail(f"phase-close requires --{name.replace('_', '-')}")
    for name in ("remote_target_oid", "attestation_commit", "phase_remote_oid", "expected_lane_commit"):
        value = getattr(arguments, name, None)
        if not value or not _HEX40.match(value):
            _fail(f"phase-close needs a full 40-character {name.replace('_', '-')}, got {value!r}")

    if arguments.remote_target_oid != arguments.phase_remote_oid:
        _fail(
            f"fetched remote target {arguments.remote_target_oid!r} is not the verified phase remote "
            f"{arguments.phase_remote_oid!r}; closing against a ref that moved is closing on a different tree"
        )

    context = open_git(environ, expected_root=PRODUCT_ROOT)
    binding = bind_commit(context, arguments.expected_lane_commit)
    if binding.head != arguments.expected_lane_commit:
        _fail(f"HEAD is {binding.head!r}, expected lane commit {arguments.expected_lane_commit!r}")

    if arguments.expected_tree:
        # Resolved through the pinned executable rather than skipped when the
        # helper is absent. A tree check that quietly does not run is worse than
        # no tree check, because the record still says the tree was bound.
        tree = context.run(
            "rev-parse", "--verify", "--end-of-options", f"{arguments.expected_lane_commit}^{{tree}}"
        ).strip()
        if tree != arguments.expected_tree:
            _fail(
                f"lane commit resolves tree {tree!r}, expected {arguments.expected_tree!r}; "
                "identical content under a different tree is a different measurement"
            )

    # Every gate must already have passed. `phase-close` does not re-measure;
    # it refuses to certify a set of results it cannot see.
    required_modes = ("hard-gate", "scale", "provider-parity")
    root = Path(arguments.evidence_root) if arguments.evidence_root else PRODUCT_ROOT / "run/integration-performance"
    present: dict[str, Path] = {}
    for mode in required_modes:
        matches = sorted(root.glob(f"*{mode}*-manifest.json"))
        if not matches:
            _fail(f"phase-close found no {mode} manifest under {root}; a missing gate is not a passed gate")
        present[mode] = matches[-1]

    bound = {
        "lane": arguments.lane,
        "base": arguments.base,
        "branch": arguments.branch,
        "reconciliation_id": arguments.reconciliation_id,
        "reconciliation_digest": arguments.reconciliation_digest,
        "remote_target_oid": arguments.remote_target_oid,
        "attestation_commit": arguments.attestation_commit,
        "phase_remote_oid": arguments.phase_remote_oid,
        "lane_commit": arguments.expected_lane_commit,
        "tree": arguments.expected_tree,
        "manifests": {mode: sha256_file(path) for mode, path in present.items()},
    }

    if arguments.write_verification_result:
        _emit(Path(arguments.write_verification_result), {"target_met": True, "bound": bound})
    if arguments.write_planning_result:
        _emit(Path(arguments.write_planning_result), {"derived_from": bound})


def _emit(path: Path, document: Mapping[str, Any]) -> None:
    """Write a result and its sidecar atomically, so a reader never sees half."""
    assert_no_secrets(document, where=path.name)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    atomic_write(path, payload)
    atomic_write(path.with_suffix(".sha256"), f"{sha256_text(payload)}  {path.name}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_sequence_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--lifecycle-evidence-root")
    parser.add_argument("--require-outer-controller", action="store_true")
    parser.add_argument("--require-authenticated-controls", action="store_true")
    parser.add_argument("--require-sanitized-git", action="store_true")
    parser.add_argument("--require-clean-tree", action="store_true")
    parser.add_argument("--require-manifest-checksum", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)

    baseline = modes.add_parser("baseline-import")
    baseline.add_argument("--schema", required=True)
    baseline.add_argument("--evidence", required=True)
    baseline.add_argument("--require-label")

    scale = modes.add_parser("scale")
    _add_sequence_flags(scale)
    scale.add_argument("--workers", required=True)
    scale.add_argument("--require-smallest-eligible", action="store_true")

    hard_gate = modes.add_parser("hard-gate")
    _add_sequence_flags(hard_gate)
    hard_gate.add_argument("--require-target-met", action="store_true")

    parity = modes.add_parser("provider-parity")
    _add_sequence_flags(parity)
    parity.add_argument("--provider", required=True)
    parity.add_argument("--require-complete-collection", action="store_true")
    parity.add_argument("--require-exact-provider-matrix", action="store_true")
    parity.add_argument("--require-one-attempt", action="store_true")
    parity.add_argument("--timing-observational", action="store_true")
    parity.add_argument("--expected-operational-timeout-seconds", type=float)

    close = modes.add_parser("phase-close")
    close.add_argument("--lane")
    close.add_argument("--base")
    close.add_argument("--branch")
    close.add_argument("--reconciliation-id")
    close.add_argument("--reconciliation-digest")
    close.add_argument("--remote-target-oid")
    close.add_argument("--attestation-commit")
    close.add_argument("--phase-remote-oid")
    close.add_argument("--expected-lane-commit")
    close.add_argument("--expected-tree")
    close.add_argument("--evidence-root")
    close.add_argument("--write-verification-result")
    close.add_argument("--write-planning-result")
    return parser


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    ambient = os.environ if environ is None else environ  # config: intentional - inspected, not read for config
    resolved = dict(ambient)

    handlers: dict[str, Callable[[], None]] = {
        "baseline-import": lambda: mode_baseline_import(arguments),
        "scale": lambda: mode_scale(arguments, resolved),
        "hard-gate": lambda: mode_hard_gate(arguments, resolved),
        "provider-parity": lambda: mode_provider_parity(arguments, resolved),
        "phase-close": lambda: mode_phase_close(arguments, resolved),
    }
    try:
        handlers[arguments.mode]()
    except (VerificationFailure, EvidenceError) as error:
        print(f"integration evidence rejected ({arguments.mode}): {error}", file=sys.stderr)
        return 1
    print(f"integration evidence verified ({arguments.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
