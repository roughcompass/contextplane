#!/usr/bin/env python
"""Re-derive a lifecycle comparison from its raw inputs and refuse a bad one.

This verifier exists because the controller that produced the bundle is not a
trustworthy witness to its own output. It therefore re-reads the raw runs,
recomputes the reduction, recomputes the source-delta manifest from the two
commit objects, and compares every derived value against what the bundle
recorded. Agreement is the result; the bundle's own ``outcome`` field is never an
input to that decision.

Four things it will not let past:

- **An incomplete or mismatched pair.** Exactly the required runs per side, each
  covering every cohort module, all under one provenance.
- **Stale or redirected inputs.** Runs attributed to a commit they were not taken
  on, or a bundle checksum that does not cover the runs beside it.
- **A label the delta contradicts.** ``reverted`` with implementation files still
  present, or ``retained`` whose only source change is the measurement harness.
- **A win measured somewhere else.** Provenance is compared field by field, so a
  before side taken under one provider and an after side under another is a
  refusal rather than a headline.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess  # noqa: S404 - runs git only, fixed argv, scrubbed env
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lifecycle_measurement import (
    LifecycleError,
    build_comparison,
    classify_delta,
    load_bundle,
    manifest_checksum,
    outcome_consistency_failures,
    source_delta_manifest,
)

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

#: Paths this change is allowed to touch. Anything outside is a refusal rather
#: than a warning: a lifecycle measurement that also edited unrelated product
#: code is not measuring what it claims to measure.
DECLARED_SCOPE = (
    "scripts/check_config_consolidation.py",
    "scripts/lifecycle_measurement.py",
    "scripts/run_integration_lifecycle_comparison.py",
    "scripts/verify_integration_lifecycle_comparison.py",
    "tests/conftest.py",
    "tests/integration/conftest.py",
    "tests/helpers/async_db.py",
    "tests/helpers/auth_harness.py",
    "tests/helpers/lifecycle_measurement.py",
    "tests/unit/test_async_db.py",
    "tests/unit/test_auth_harness.py",
    "tests/unit/test_lifecycle_measurement.py",
    "tests/unit/test_integration_async_fixture_scopes.py",
    "tests/integration/test_admin_progression.py",
    "tests/integration/test_sync_ingest.py",
    "tests/integration/test_closure_cache.py",
    "tests/integration/test_memory_curation_queue.py",
    "tests/integration/test_external_ids_rest.py",
    "tests/integration/test_api_ergonomics.py",
    "tests/integration/test_memory_promotion_surface.py",
    "tests/integration/test_memory_curation_mcp_tools.py",
    "tests/integration/test_memory_confirmation_surface.py",
    "tests/integration/test_memory_claim_history_surface.py",
    "tests/integration/test_memory_capability_requests_surface.py",
    "tests/integration/test_memory_claim_assertion.py",
    "tests/integration/test_pii_block.py",
    "run/integration-performance/",
)


class VerificationFailure(RuntimeError):
    """Raised with every finding at once, so one run reports the whole story."""


def resolve_git() -> str:
    found = shutil.which("git")
    if not found:
        raise VerificationFailure("no git executable on PATH")
    return os.path.realpath(found)


def scrubbed_env() -> dict[str, str]:
    redirecting = sorted(k for k in os.environ if k in REDIRECTING_GIT_VARS)  # config: intentional
    if redirecting:
        raise VerificationFailure(
            "refusing to run with inherited Git environment: "
            + ", ".join(redirecting)
            + ". Each can make `git -C <path>` answer about a different repository, which would let "
            "this verifier confirm a comparison against a tree nobody measured. Unset them and re-run."
        )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # config: intentional
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def make_runner(product_root: Path) -> Callable[..., str]:
    """One realpath'd Git, anchored at a verified top level, for every call."""
    git_bin = resolve_git()
    env = scrubbed_env()
    root = product_root.resolve()

    def run(*args: str) -> str:
        result = subprocess.run(  # noqa: S603 - realpath'd git, fixed argv, scrubbed env
            [git_bin, "-C", str(root), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise VerificationFailure(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    top = Path(run("rev-parse", "--show-toplevel").strip()).resolve()
    if top != root:
        raise VerificationFailure(f"--product-root {product_root} is not a worktree top level; git reports {top}")
    return run


def verify(args: argparse.Namespace) -> list[str]:
    findings: list[str] = []
    run_git = make_runner(Path(args.product_root))

    # Both commits must actually exist before anything is derived between them.
    for label, commit in (("before", args.expected_before_commit), ("after", args.expected_after_commit)):
        try:
            run_git("cat-file", "-e", f"{commit}^{{commit}}")
        except VerificationFailure:
            findings.append(f"{label} commit {commit[:12]} names no object in this repository")
    if findings:
        return findings

    # The prefix names the *design*, and the two are not interchangeable: `cmp-`
    # is the superseded blocked capture (all befores, then all afters), `paired-`
    # is the interleaved one. Preferring `paired-` reads the better evidence when
    # both exist, and keeping the names distinct is what stops a reader mistaking
    # a contaminated blocked attempt for a paired result.
    stem = f"{args.expected_before_commit[:12]}-{args.expected_after_commit[:12]}"
    evidence_root = Path(args.evidence_root)
    for prefix in ("paired", "cmp"):
        run_id = f"{prefix}-{stem}"
        bundle_path = evidence_root / run_id / "lifecycle-comparison.json"
        if bundle_path.is_file():
            break
    else:
        return [
            f"no bundle at {evidence_root}/(paired|cmp)-{stem}/lifecycle-comparison.json; "
            "the capture step has not run for this commit pair"
        ]

    recorded, recorded_checksum = load_bundle(bundle_path)

    # Re-derive from the raw runs rather than trusting the recorded summary.
    try:
        derived = build_comparison(
            [*recorded.before_runs, *recorded.after_runs],
            before_commit=args.expected_before_commit,
            after_commit=args.expected_after_commit,
            cohort_path=Path(args.cohort),
            minimum_reduction_seconds=args.minimum_reduction,
            max_critical_path_seconds=args.max_critical_path,
            required_runs_per_side=args.before_runs,
        )
    except LifecycleError as exc:
        return [f"raw runs do not support a comparison: {exc}"]

    if args.before_runs != args.after_runs:
        findings.append(
            f"--before-runs {args.before_runs} and --after-runs {args.after_runs} differ; "
            "a matched comparison requires the same count on both sides"
        )

    if derived.checksum() != recorded_checksum:
        findings.append(
            f"bundle checksum {recorded_checksum[:12]} does not match the runs it sits beside "
            f"({derived.checksum()[:12]}); the evidence was edited after it was written"
        )

    for label, mine, theirs in (
        ("reduction", derived.reduction_seconds, recorded.reduction_seconds),
        ("before best", derived.before_best_seconds, recorded.before_best_seconds),
        ("after worst", derived.after_worst_seconds, recorded.after_worst_seconds),
    ):
        if abs(mine - theirs) > 0.001:
            findings.append(f"recorded {label} {theirs:.4f}s does not match the re-derived {mine:.4f}s")

    if derived.outcome is not recorded.outcome:
        findings.append(
            f"recorded outcome {recorded.outcome.value!r} does not match the re-derived "
            f"{derived.outcome.value!r}; the label was not produced by these numbers"
        )

    # The source delta, recomputed between the two exact commit objects.
    entries = source_delta_manifest(run_git, args.expected_before_commit, args.expected_after_commit)
    classification = classify_delta(entries, scope=DECLARED_SCOPE)
    checksum = manifest_checksum(entries)

    if args.require_source_delta_manifest and not entries:
        findings.append(
            "the two commits are identical in content; there is no source delta to classify, so "
            "nothing was measured across a change"
        )
    if args.require_source_delta_checksum:
        print(f"source-delta checksum   {checksum}")
    if args.require_static_source_classification:
        for name, paths in classification.as_dict().items():
            if paths:
                print(f"  {name}: {len(paths)}")
                for path in paths:
                    print(f"    {path}")
    if args.require_material_or_reverted:
        findings.extend(outcome_consistency_failures(derived.outcome, classification))

    print(f"run id                  {run_id}")
    print(f"bundle                  {bundle_path.resolve()}")
    print(f"bundle checksum         {recorded_checksum}")
    print(f"before best             {derived.before_best_seconds:.2f}s")
    print(f"after worst             {derived.after_worst_seconds:.2f}s")
    print(f"reduction               {derived.reduction_seconds:.2f}s " f"(floor {args.minimum_reduction:.2f}s)")
    print(f"after critical path     {derived.after_worst_seconds:.2f}s " f"(ceiling {args.max_critical_path:.2f}s)")
    print(f"outcome                 {derived.outcome.value}")
    for reason in derived.refusal_reasons():
        print(f"  - {reason}")
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--product-root", default=".")
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--expected-before-commit", required=True)
    parser.add_argument("--expected-after-commit", required=True)
    parser.add_argument("--before-runs", type=int, default=3)
    parser.add_argument("--after-runs", type=int, default=3)
    parser.add_argument("--minimum-reduction", type=float, default=1.0)
    parser.add_argument("--max-critical-path", type=float, default=math.inf)
    parser.add_argument("--require-source-delta-manifest", action="store_true")
    parser.add_argument("--require-source-delta-checksum", action="store_true")
    parser.add_argument("--require-static-source-classification", action="store_true")
    parser.add_argument("--require-material-or-reverted", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        findings = verify(args)
    except (VerificationFailure, LifecycleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if findings:
        print("\nREFUSED", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("\nverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
