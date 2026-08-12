"""The predicates that judge a measured run's evidence, and refuse it.

Separated from the modes that select them so that the question "what makes
evidence acceptable?" is answerable without reading the CLI that asks it. Every
function here re-derives what it checks from the machine or from bytes on disk;
none of them reads a conclusion out of the artifact under test.

Two rules hold throughout and are the reason these are predicates rather than
getters:

- **a field is read from evidence only when it is a claim, never when it is a
  fact.** The commit a run says it measured is a claim, checked against a commit
  resolved here. Anything taken on faith is a place the evidence could have been
  written to say whatever would pass;
- **absence fails.** A missing manifest, an empty run list, an unreadable
  checksum sidecar — each is a refusal, never a silent pass. A verifier that
  reports success because it found nothing to disagree with is exactly what a
  suite pointed at the wrong directory does.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))

from integration_evidence import (
    assert_no_secrets,
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

# The only label this corpus may import under. Asserted by the verifier rather
# than read from the corpus: a legacy capture vouching for its own standing is
# exactly the confusion the label exists to prevent.
LEGACY_UNSEALED_LABEL: Final = "legacy-unsealed"

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


def check_sequence_integrity(
    manifest: Mapping[str, Any],
    *,
    require_outer_controller: bool,
    allow_timed_out: bool = False,
) -> list[dict[str, Any]]:
    """One controller, one lease, ordered child sequence, no splicing.

    `allow_timed_out` is set only by scale, where a candidate exceeding the
    ceiling is the measurement rather than a fault. It permits the record to
    exist; it does not decide what a candidate carrying one must look like —
    that is `mode_scale`'s two closed shapes, and keeping the two apart is what
    stops "ineligible" from becoming a way to be exempt from structure.
    """
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
            if not allow_timed_out:
                _fail(f"run {run.get('run_id')!r} timed out; a timed-out child is not a slow measurement")
            if not run.get("abandoned_reason"):
                _fail(
                    f"run {run.get('run_id')!r} timed out but records no abandonment reason; the gap it leaves "
                    "in the candidate's runs has to be explicit or it reads as runs that were never planned"
                )
            if run.get("controller_elapsed_seconds") is None:
                _fail(f"run {run.get('run_id')!r} timed out but records no elapsed time")
            for field in ("external_real_seconds", "external_user_seconds", "external_sys_seconds"):
                if field in run:
                    _fail(
                        f"run {run.get('run_id')!r} timed out yet reports {field}; the timing file was never "
                        "completed, and a controller-measured elapsed under an external-timing key would let "
                        "a killed run be read as a measured one"
                    )
            continue
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
