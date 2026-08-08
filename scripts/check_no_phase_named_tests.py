"""Gate that prevents phase-named test files and stale phase-marker comments.

Test files must describe present-tense system behavior, not delivery history.
A file named `test_phase3.py` tells a reader when something was built, not
what invariant is being protected. This script detects:

  1. Filenames matching the phase-naming pattern (e.g. `test_phase3.py`).
  2. Inline comments that anchor a block to a delivery phase
     (e.g. `# phase 3 setup`).

Run locally or wire into CI:

    python scripts/check_no_phase_named_tests.py
    python scripts/check_no_phase_named_tests.py --explain
    python scripts/check_no_phase_named_tests.py --paths tests/unit

Lines ending with `# test-hygiene: intentional` are exempt. Use the marker
only when "phase" appears as a genuine domain term unrelated to delivery
milestones.

The Alembic migration versions subtree is excluded from the walk entirely —
filenames like `0005_phase4_rbac_oidc.py` are framework-generated revision
keys, not test files.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from checklib import repo_root, require_nonempty, run_guard

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Resolve from the repo root, not the workspace above it. Going up one extra
# level and back down through a literal directory name breaks in any checkout
# not named that -- a git worktree, most often -- and the gate then scans
# nothing while still exiting non-zero.
_REPO_ROOT = repo_root()

# Default scope when --paths is not given, relative to the repo root.
_DEFAULT_SCOPE: tuple[str, ...] = ("tests",)

# Subtrees that are never walked, even when a parent directory is in scope.
# The Alembic versions directory contains framework-generated filenames that
# embed phase tokens as revision identifiers — they are not test artifacts.
_EXCLUDE_SUBTREES: frozenset[str] = frozenset(
    {
        "contextplane/storage/migrations/versions",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".git",
    }
)

# Lines ending with this marker are exempt from all checks.
_BYPASS_MARKER = "# test-hygiene: intentional"


@dataclass(frozen=True)
class DetectionRule:
    """One detection rule and the guidance emitted on a hit."""

    name: str
    explain: str


_FILENAME_RULE = DetectionRule(
    name="phase-named-file",
    explain=(
        "Test file name encodes a delivery milestone. Rename the file so its "
        "name describes the behavioral contract it protects (e.g. "
        "`test_phase3.py` → `test_sync_ingest.py`). If the 'phase' token is "
        "a genuine domain term, append `# test-hygiene: intentional` to a "
        "comment at the top of the file and re-run."
    ),
)

_COMMENT_RULE = DetectionRule(
    name="phase-marker-comment",
    explain=(
        "Comment anchors the block to a delivery milestone rather than "
        "describing the behavior being tested. Replace the comment with a "
        "behavioral statement (e.g. `# phase 3 setup` → `# seed a sync "
        "source so ingest tests have a connector to pull from`). If the "
        "'phase' token is a genuine domain term, end the line with "
        "`# test-hygiene: intentional`."
    ),
)

# Filename pattern: matches `test_phase3.py`, `test_phase3_foo.py`,
# `foo_phase3.py`, etc. Applied to the basename only.
_FILENAME_RE = re.compile(r"(?:test_phase\d+\w*|(?:\w+_)phase\d+)\.py$", re.IGNORECASE)

# Comment pattern: matches lines whose first non-whitespace token is `#`
# and that contain `phase <digits>` anywhere in the comment text.
_COMMENT_RE = re.compile(r"^\s*#[^\n]*\bphase\s+\d+\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """One detection match."""

    path: Path
    # line_no is 0 for filename hits (not a content line).
    line_no: int
    rule: DetectionRule
    matched: str


def _is_excluded(path: Path) -> bool:
    """Return True if *path* falls inside any excluded subtree."""
    parts_str = str(path)
    for subtree in _EXCLUDE_SUBTREES:
        # Match any path component sequence — works whether the subtree
        # token appears at the repo root or nested inside one.
        if subtree in parts_str:
            return True
    return False


def _unresolved_scope_message(missing: list[str], scope: list[str]) -> str:
    """Explain that the scope could not be found, and why that is a failure.

    A gate that cannot find what it was asked to check has established nothing.
    Reporting that as success used to be this script's behaviour, which made a
    mistyped path — and the entire default scope, whenever resolved from a
    checkout shaped differently from the one this script assumes it lives in —
    read as a clean run. A directory that exists and holds no Python files is a
    different thing and still passes.
    """
    return (
        f"scope does not exist: {', '.join(missing)}\n"
        f"(full scope: {', '.join(scope)})\n"
        "\n"
        "Nothing was checked, so this is a failure rather than a pass. Either a\n"
        "path is wrong, or the working directory is not shaped the way the\n"
        "default scope is resolved against."
    )


def _resolve_targets(scope: list[str]) -> list[Path]:
    """Expand the scope list into concrete .py files to scan."""
    out: list[Path] = []
    for entry in scope:
        target = (_REPO_ROOT / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix == ".py" and not _is_excluded(target):
                out.append(target)
            continue
        for path in sorted(target.rglob("*.py")):
            if not path.is_file():
                continue
            if _is_excluded(path):
                continue
            out.append(path)
    return out


def _scan_file(path: Path) -> list[Hit]:
    """Return every detection hit in *path*.

    Two classes of hit:
    - Filename hit: the basename matches the phase-naming pattern.
    - Comment hit: a content line contains a phase-marker comment.

    Lines ending with the bypass marker are exempt from comment scanning.
    Filename bypass is not supported per line — annotate with a top-level
    comment and use the bypass marker on that comment.
    """
    hits: list[Hit] = []

    # --- Filename check ---
    if _FILENAME_RE.search(path.name):
        hits.append(
            Hit(
                path=path,
                line_no=0,
                rule=_FILENAME_RULE,
                matched=path.name,
            )
        )

    # --- Content check ---
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return hits

    for idx, raw_line in enumerate(text.splitlines(), start=1):
        if _BYPASS_MARKER in raw_line:
            continue
        if _COMMENT_RE.search(raw_line):
            hits.append(
                Hit(
                    path=path,
                    line_no=idx,
                    rule=_COMMENT_RULE,
                    matched=raw_line.strip(),
                )
            )

    return hits


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_explain() -> int:
    print("Detection rules and what to do if you hit one:\n")
    rules = [
        (_FILENAME_RULE, f"Pattern: {_FILENAME_RE.pattern}"),
        (_COMMENT_RULE, f"Pattern: {_COMMENT_RE.pattern}"),
    ]
    for rule, pattern_desc in rules:
        print(f"  {rule.name}")
        print(f"    {pattern_desc}")
        print(f"    Fix: {rule.explain}")
        print()
    print(f"Lines ending with '{_BYPASS_MARKER}' are exempt from comment checks.")
    print(
        "The Alembic migrations versions subtree "
        "(`contextplane/storage/migrations/versions/`) is excluded from the walk."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Verify no phase-named test files or stale phase-marker " "comments exist in the test tree."),
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=list(_DEFAULT_SCOPE),
        help="Repo-relative paths to scan (default: tests).",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print each detection rule with fix guidance, then exit.",
    )
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    # See the note in check_no_doc_refs.py: strict polarity, better message.
    missing = [entry for entry in args.paths if not (_REPO_ROOT / entry).exists()]
    if missing:
        if args.paths == list(_DEFAULT_SCOPE):
            print(
                f"the default scope resolved to no .py files under {_REPO_ROOT}: "
                f"{', '.join(missing)}.\n"
                "Nothing was scanned, which this gate treats as a failure rather than a pass.\n"
                "Pass --paths explicitly if the tree lives elsewhere, e.g.\n"
                f"  python3 {Path(__file__).name} --paths tests/unit",
                file=sys.stderr,
            )
        else:
            print(_unresolved_scope_message(missing, args.paths), file=sys.stderr)
        return 1

    targets = _resolve_targets(args.paths)
    # Scope entries all exist. If the *default* scope still resolves to no file,
    # the gate governed nothing and must say so; an explicit narrow --paths that
    # holds nothing is a fair question with a "nothing there" answer.
    require_nonempty(
        targets,
        "the .py scan population (paths: " + ", ".join(args.paths) + ")",
        allow_empty=args.paths != list(_DEFAULT_SCOPE),
    )
    if not targets:
        # Every scope entry exists and none holds a .py file — a real
        # "checked, found nothing".
        print("no .py files to scan in " + ", ".join(args.paths), file=sys.stderr)
        return 0

    all_hits: list[Hit] = []
    for path in targets:
        all_hits.extend(_scan_file(path))

    if not all_hits:
        # Report the count, not just the exit code: a gate that passes because
        # it resolved an empty scope is the failure mode this line makes visible.
        print(f"test-hygiene gate: {len(targets)} file(s) scanned, 0 violation(s)")
        return 0

    for hit in all_hits:
        try:
            display = hit.path.relative_to(_REPO_ROOT)
        except ValueError:
            display = hit.path
        if hit.line_no == 0:
            print(f"{display}: {hit.rule.name}: {hit.matched}")
        else:
            print(f"{display}:{hit.line_no}: {hit.rule.name}: {hit.matched}")

    print(
        f"\n{len(all_hits)} phase-naming violation(s) found. " "Run with --explain for fix guidance.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(run_guard(main))
