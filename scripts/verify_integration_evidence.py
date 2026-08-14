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
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from integration_evidence import (
    EvidenceError,
    assert_no_secrets,
    atomic_write,
    sha256_file,
    sha256_text,
)
from integration_evidence_checks import (  # noqa: F401 - re-exported surface
    _HEX40,
    _HEX64,
    LEGACY_UNSEALED_LABEL,
    MEASURED_RUNS,
    SCALE_CANDIDATES,
    VerificationFailure,
    _fail,
    check_authenticated_controls,
    check_canonical_commands,
    check_external_budget,
    check_lifecycle_binding,
    check_phase_deadlines,
    check_sequence_integrity,
    external_real,
    independent_provenance,
    load_sealed,
    measured_only,
)
from integration_provenance import (
    PRODUCT_ROOT,
    bind_commit,
    open_git,
)

# The check surface stays importable from this module even though the predicates
# live in a sibling for size reasons. Callers -- the tests among them -- reach
# for `verify_integration_evidence.check_*` and the thresholds, and a split that
# moved that surface would be a rename disguised as a refactor.
from integration_scheduler import (  # noqa: F401 - re-exported surface; the thresholds' owner
    EXECUTION_MAX_SECONDS,
    EXTERNAL_MAX_SECONDS,
    PROVISIONING_MAX_SECONDS,
)

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def mode_baseline_import(arguments: argparse.Namespace) -> None:
    """Re-check the pre-sealing corpus, and refuse to let it pass as sealed.

    This corpus was captured before any of the sealing machinery existed. It has
    a checksum sidecar and nothing else: no manifest, no lease, no control, no
    external timing owned by an outer controller. That makes it usable as a
    historical reference and unusable as evidence for a gate, and the whole job
    here is to keep those two apart.

    So the import does three things. It recomputes every raw capture's digest
    from bytes against the sidecar, because a corpus nobody re-checks is a
    corpus that quietly rots. It holds the capture record to the schema's
    required fields, since an optional field is one a capture can omit and a
    reader can then assume. And it refuses the import outright if the corpus
    has grown anything that would let it be mistaken for sealed evidence --
    which is what the label means, and why the label is asserted rather than
    read out of the record. Nothing in a legacy capture is authorized to call
    itself sealed, so a label found *inside* the corpus would be the corpus
    vouching for its own standing.
    """
    schema = Path(arguments.schema)
    evidence = Path(arguments.evidence)
    if not schema.is_file():
        _fail(f"no baseline schema at {schema}")
    if not evidence.is_dir():
        _fail(f"no baseline evidence directory at {evidence}")

    # The corpus root is where the schema lives; the raw directory must sit
    # inside it, so a caller cannot pair one corpus's schema with another's
    # captures.
    corpus = schema.resolve().parent
    if corpus not in evidence.resolve().parents:
        _fail(f"evidence {evidence} is not inside the corpus rooted at {corpus}")

    if arguments.require_label and arguments.require_label != LEGACY_UNSEALED_LABEL:
        _fail(
            f"baseline import was asked for label {arguments.require_label!r}; this corpus imports only as "
            f"{LEGACY_UNSEALED_LABEL!r}, because it predates every sealing guarantee a other label would imply"
        )

    sidecar = corpus / "checksums.txt"
    if not sidecar.is_file():
        _fail(f"no checksums.txt at {sidecar}; an unchecked legacy corpus is not a reference, it is a guess")

    recorded: dict[str, str] = {}
    for line in sidecar.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if not _HEX64.match(digest.strip()) or not name.strip():
            _fail(f"unreadable checksum line in {sidecar.name}: {line!r}")
        recorded[name.strip()] = digest.strip()
    if not recorded:
        _fail(f"{sidecar.name} lists no captures; an empty sidecar checks nothing")

    present = {path.name for path in evidence.iterdir() if path.is_file()}
    missing = sorted(set(recorded) - present)
    if missing:
        _fail(f"capture(s) named in {sidecar.name} are absent from {evidence}: {missing}")
    unlisted = sorted(present - set(recorded))
    if unlisted:
        _fail(
            f"capture(s) present in {evidence} but not in {sidecar.name}: {unlisted}; an unlisted file is one "
            "nothing vouches for sitting where a vouched-for one is expected"
        )
    for name, digest in sorted(recorded.items()):
        actual = sha256_file(evidence / name)
        if actual != digest:
            _fail(f"{name} digests {actual} but {sidecar.name} records {digest}; the capture changed after capture")

    capture = corpus / "baseline.json"
    if not capture.is_file():
        _fail(f"no capture record at {capture}")
    document = json.loads(schema.read_text(encoding="utf-8"))
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert_no_secrets(payload, where=capture.name)

    for field in sorted(document.get("required", [])):
        if field not in payload:
            _fail(f"{capture.name} is missing schema-required field {field!r}")

    run_schema = document.get("properties", {}).get("runs", {}).get("items", {})
    for index, run in enumerate(payload.get("runs", []), start=1):
        for field in sorted(run_schema.get("required", [])):
            if field not in run:
                _fail(f"{capture.name} run {index} is missing schema-required field {field!r}")

    # The label is a refusal, not a decoration: anything here that looks sealed
    # could later be cited as sealed.
    sealed_shapes = sorted(
        path.name
        for path in corpus.rglob("*")
        if path.is_file() and (path.name == "manifest.json" or path.name.endswith("-manifest.json"))
    )
    if sealed_shapes:
        _fail(
            f"corpus carries sealed-evidence artifact(s) {sealed_shapes}; it imports as "
            f"{LEGACY_UNSEALED_LABEL!r} precisely because it has none, and one appearing here would let a "
            "pre-sealing capture be cited as though a controller had vouched for it"
        )


def _common_sequence_checks(
    arguments: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    allow_timed_out: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = load_sealed(Path(arguments.evidence), require_checksum=True)
    independent_provenance(
        manifest,
        expected_commit=arguments.expected_commit,
        require_sanitized_git=arguments.require_sanitized_git,
        require_clean_tree=arguments.require_clean_tree,
        environ=environ,
    )
    runs = check_sequence_integrity(
        manifest,
        require_outer_controller=arguments.require_outer_controller,
        allow_timed_out=allow_timed_out,
    )
    if arguments.require_authenticated_controls:
        check_authenticated_controls(manifest, runs)
    if arguments.lifecycle_evidence_root:
        check_lifecycle_binding(manifest, Path(arguments.lifecycle_evidence_root))
    return manifest, runs


def mode_scale(arguments: argparse.Namespace, environ: Mapping[str, str]) -> None:
    """1/2/4/8, each candidate matching exactly one of two permitted shapes.

    The shapes are a closed set, not a relaxation:

    - **eligible** -- exactly one warm-up and exactly three measured runs, every
      one of them inside the budget;
    - **ineligible** -- at least one timed-out child and no measured runs at all.

    Written the other way round -- "fewer runs are allowed when a candidate is
    ineligible" -- the loosening would itself be the hole: a candidate that
    genuinely broke could be relabelled ineligible and stop having to account
    for its missing runs. Here an ineligible candidate is not exempt from
    structure, it has a different and equally checked structure, and a candidate
    matching neither is refused without either label being available to it.
    """
    manifest, runs = _common_sequence_checks(arguments, environ, allow_timed_out=True)
    check_canonical_commands(manifest, runs, provider=manifest.get("provider", "devstack"))

    expected = tuple(int(value) for value in arguments.workers.split(","))
    if expected != SCALE_CANDIDATES:
        _fail(f"scale verification asked for {expected!r}; the sequence is defined over {SCALE_CANDIDATES!r}")

    by_candidate: dict[int, list[dict[str, Any]]] = {}
    for run in runs:
        by_candidate.setdefault(int(run.get("worker_count", -1)), []).append(run)

    if tuple(sorted(by_candidate)) != SCALE_CANDIDATES:
        _fail(
            f"sequence covers worker counts {tuple(sorted(by_candidate))!r}, expected {SCALE_CANDIDATES!r}; "
            "every count is measured, including the ones expected to miss, because the evidence has to say "
            "which of them missed rather than that some were not tried"
        )

    eligible_counts: list[int] = []
    for candidate, candidate_runs in sorted(by_candidate.items()):
        warmups = [run for run in candidate_runs if run.get("role") == "warm-up"]
        measured = [run for run in candidate_runs if run.get("role") != "warm-up" and not run.get("timed_out")]
        timed_out = [run for run in candidate_runs if run.get("timed_out")]

        if timed_out:
            # Ineligible shape. Checked as strictly as the eligible one.
            if measured:
                _fail(
                    f"candidate {candidate} has both a timed-out child and {len(measured)} completed measured "
                    "run(s); a candidate is measured or abandoned, and one that is both leaves no answer to "
                    "whether its number describes the budget it blew"
                )
            continue

        if len(warmups) != 1:
            _fail(
                f"candidate {candidate} has {len(warmups)} warm-up(s), expected exactly 1; a candidate whose "
                "first measured run was its warm-up is timing a cold cache"
            )
        if len(measured) != MEASURED_RUNS:
            _fail(
                f"candidate {candidate} completed {len(measured)} measured run(s), expected {MEASURED_RUNS} "
                "and recorded no timeout to explain the difference"
            )
        check_phase_deadlines(measured)
        check_external_budget(measured)
        eligible_counts.append(candidate)

    if arguments.require_smallest_eligible:
        recorded = manifest.get("selected_worker_count")
        if not eligible_counts:
            if recorded is not None:
                _fail(f"no candidate qualified, yet the sequence selected {recorded!r}")
            _fail(
                "no worker count through 8 met the external budget. This is a block rather than a fallback to "
                "the largest count: shipping 8 because 1, 2 and 4 missed would report a passing gate for a "
                "suite that does not fit its budget"
            )
        if recorded is None or int(recorded) != eligible_counts[0]:
            _fail(
                f"sequence selected {recorded!r} but the smallest eligible count is {eligible_counts[0]}; "
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
