"""Lint that shipped code contains no internal-doc references.

The full rule lives in `CLAUDE.md` at the repo root. This script is the
programmatic gate that enforces it. Run it locally or wire into CI:

    python scripts/check_no_doc_refs.py
    python scripts/check_no_doc_refs.py --explain
    python scripts/check_no_doc_refs.py --paths registry/service

The script walks the in-scope paths, applies the forbidden-pattern regex
set, ignores lines tagged `# doc-ref: intentional`, and exits non-zero
with a `file:line` list on any hit. The `--explain` flag lists each
pattern and what to do if you hit one.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


# Resolve from the repo root, not the workspace above it. Going up one extra
# level and back down through a literal directory name breaks in any checkout
# not named that -- a git worktree, most often -- and the gate then scans
# nothing while still exiting non-zero.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Default scope when --paths is not given, relative to the repo root.
_DEFAULT_SCOPE: tuple[str, ...] = (
    "registry",
    "tests",
    "scripts",
    "eval",
    "CONTRIBUTING.md",
    "README.md",
    ".env.example",
    "deploy/helm",
)

# Paths that are *never* checked even if a parent dir is in scope.
_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".git",
        ".context",
    }
)

# File extensions to scan.
_SCAN_SUFFIXES: frozenset[str] = frozenset({".py", ".md", ".yaml", ".yml", ".sql", ".txt", ".example"})

# Marker that excludes a line from the gate.
_BYPASS_MARKER = "# doc-ref: intentional"


@dataclass(frozen=True)
class Pattern:
    """One forbidden pattern + the rewrite guidance the gate emits on hit."""

    name: str
    regex: re.Pattern[str]
    explain: str


_PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        name="ADR-NNN",
        regex=re.compile(r"\bADR-\d+\b"),
        explain=(
            "Architecture decision record reference. State the rule the ADR "
            "encodes directly in the code (one short sentence), not the ADR id."
        ),
    ),
    Pattern(
        # `N?` because half of every PRD's requirement ids are non-functional ones,
        # and a leading `\b` cannot match the `F` of an `NF`-prefixed id — `N` is a
        # word character, so there is no boundary in front of the `F`. Without it the
        # gate blocked functional ids and waved every non-functional one through.
        name="F<n>.<n> / NF<n>.<n>",
        regex=re.compile(r"\bN?F\d+\.\d+\b"),
        explain=(
            "PRD requirement number. Describe what the code does (or the user-"
            "visible capability it implements), not its PRD entry."
        ),
    ),
    Pattern(
        name="OQ-…",
        regex=re.compile(r"\bOQ-[A-Za-z0-9-]+"),
        explain=(
            "Open-question label. Write the resolved behaviour directly. The "
            "fact that it was once an open question is git-blame trivia."
        ),
    ),
    Pattern(
        # One pattern for every development-plan task ID shape this repo's
        # planning workspace has ever used, instead of one hardcoded prefix
        # per project (`CC-TNN`, `DRC-TNN`, `CSS-TNN`, ...). That enumeration
        # only ever covered prefixes someone remembered to add on the day a
        # project started — a project whose prefix nobody registered slipped
        # through this gate on every commit for the life of the project, no
        # matter how many references piled up in shipped files. Matching the
        # *shape* of a task ID (2-5 uppercase letters, an optional
        # `-P<phase>` segment for the original phase-numbered scheme, then
        # `-T<number>` with an optional lowercase-letter suffix) catches any
        # prefix on first use, including ones that don't exist yet.
        #
        # Checked against the whole repository for false positives before
        # this replaced the enumeration: no ISO-8601 timestamp collides
        # (`2026-08-07T12:00` has no hyphen immediately before the `T`, since
        # the date and time components are joined directly), and nothing
        # else in any tracked file matches the shape by coincidence.
        name="<PREFIX>-T<NN> task ID",
        regex=re.compile(r"\b[A-Z]{2,5}(?:-P\d+R?)?-T\d+[a-z]?\b"),
        explain=(
            "Development-plan task ID. Allowed only in eval/EVAL.md as a "
            "commit-history anchor (`git log --grep=...`). Elsewhere, anyone "
            "can `git blame` to find the introducing commit — task IDs in "
            "comments are noise."
        ),
    ),
    Pattern(
        name="AQ<n>",
        regex=re.compile(r"\bAQ\d+\b"),
        explain="Architecture-quality label. Describe the quality constraint in plain terms.",
    ),
    Pattern(
        name="PRD \N{SECTION SIGN}",
        regex=re.compile(r"\bPRD §"),
        explain="PRD section citation. Inline the rule the section encodes.",
    ),
    Pattern(
        name="TDD \N{SECTION SIGN}",
        regex=re.compile(r"\bTDD §"),
        explain="TDD section citation. Inline the design choice the section encodes.",
    ),
    Pattern(
        name="<doc>.md §",
        regex=re.compile(r"\b(interfaces|flows|data-model)\.md §"),
        explain="Architecture-doc citation. Inline the relevant content.",
    ),
    Pattern(
        name="Phase <n>",
        regex=re.compile(r"\bPhase \d+\b"),
        explain=("Bare phase label. Say *what* the change is, not which internal " "milestone it shipped under."),
    ),
    Pattern(
        name="<acronym>-phase",
        regex=re.compile(r"\b[A-Z]{2,}-phase\b"),
        explain=(
            "Delivery-milestone label written as a compound word — an "
            "acronym hyphenated straight onto the word 'phase' — instead "
            "of a sentence. Same fix as the bare Phase <n> pattern above: "
            "say what the constraint IS (e.g. content encryption is a "
            "retrofit layer that does not exist yet; plaintext at rest is "
            "the current, deliberate state), never which internal "
            "milestone decides it."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """One forbidden pattern match in a shipped file."""

    path: Path
    line_no: int
    pattern: Pattern
    matched: str
    line_text: str


def _missing_scope(scope: list[str]) -> list[str]:
    """Scope entries that do not exist, which makes the run meaningless."""
    return [entry for entry in scope if not (_REPO_ROOT / entry).exists()]


def _unresolved_scope_message(missing: list[str], scope: list[str]) -> str:
    """Explain that the scope could not be found, and why that is a failure.

    A gate that cannot find what it was asked to check has established nothing,
    and reporting that as success is the failure this gate exists to prevent,
    turned on itself. It used to print a note and exit 0, so a mistyped path
    read as a clean run — and so did the whole default scope whenever it was
    resolved from a checkout shaped differently from the one this script assumes
    it lives in. A git worktree is named for its branch rather than the project,
    which made this gate pass vacuously from inside every one of them, and a
    worktree is where work that needs isolating happens.

    A directory that exists and holds nothing to scan is a different thing and
    still passes: there, the answer really is "checked, found nothing".
    """
    return (
        f"scope does not exist: {', '.join(missing)}\n"
        f"(full scope: {', '.join(scope)})\n"
        "\n"
        "Nothing was checked, so this is a failure rather than a pass. Either a\n"
        "path is wrong, or the working directory is not shaped the way the\n"
        "default scope is resolved against."
    )


def _relative_for_report(path: Path) -> str:
    """*path* relative to the repo root, for a human-readable report.

    Falls back to the absolute path when *path* is not under the workspace
    root at all -- an explicit `--paths` pointing outside it (a test's
    `tmp_path`, or a caller scanning some other checkout). The report is
    cosmetic either way; nothing downstream keys off this string.
    """
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _resolve_targets(scope: list[str]) -> list[Path]:
    """Expand the scope list into concrete files to scan."""
    out: list[Path] = []
    for entry in scope:
        target = (_REPO_ROOT / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix in _SCAN_SUFFIXES or target.name in {".env.example"}:
                out.append(target)
            continue
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix in _SCAN_SUFFIXES or path.name in {".env.example"}:
                out.append(path)
    return out


def _scan_file(path: Path) -> list[Hit]:
    """Return every forbidden-pattern hit in *path*, excluding bypassed lines.

    `eval/EVAL.md` is also exempted from the task-ID pattern specifically
    (per the rule in `CLAUDE.md`): its per-phase tables use task IDs as
    commit-history anchors (`git log --grep=...`), which is the one place
    that usage is the point rather than noise.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    is_eval_md = path.name == "EVAL.md"
    task_id_pattern_names = {"<PREFIX>-T<NN> task ID"}

    hits: list[Hit] = []
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        if _BYPASS_MARKER in raw_line:
            continue
        for pattern in _PATTERNS:
            m = pattern.regex.search(raw_line)
            if m is None:
                continue
            if is_eval_md and pattern.name in task_id_pattern_names:
                # EVAL.md is allowed to use task IDs as commit-history anchors.
                continue
            hits.append(
                Hit(
                    path=path,
                    line_no=idx,
                    pattern=pattern,
                    matched=m.group(0),
                    line_text=raw_line.rstrip(),
                )
            )
    return hits


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_explain() -> int:
    print("Forbidden patterns and what to do if you hit one:\n")
    for pattern in _PATTERNS:
        print(f"  {pattern.name}")
        print(f"    regex:  {pattern.regex.pattern}")
        print(f"    fix:    {pattern.explain}")
        print()
    print(f"Lines ending in '{_BYPASS_MARKER}' are exempt.")
    print("EVAL.md (in eval/) is allowed to reference task IDs of any prefix as commit-history anchors.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint shipped code for internal-doc references.",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=list(_DEFAULT_SCOPE),
        help="Repo-relative paths to scan (default: shipped code).",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print one line per forbidden pattern with fix guidance, then exit.",
    )
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    # Two branches caught this gate reporting success after reading nothing, and
    # settled it differently. The stricter polarity wins: a scope that does not
    # exist fails whoever supplied it. "The caller mistyped --paths" is not a
    # reason to answer clean — a narrowed scope with a typo in it is exactly the
    # silent skip being guarded against, and an annoying failure beats a false
    # pass. The message is the other branch's, because it says where it looked
    # and offers a command that would have worked.
    missing = _missing_scope(args.paths)
    if missing:
        if args.paths == list(_DEFAULT_SCOPE):
            print(
                f"the default scope resolved to no files under {_REPO_ROOT}: "
                f"{', '.join(missing)}.\n"
                "Nothing was scanned, which this gate treats as a failure rather than a pass.\n"
                "Pass --paths explicitly if the tree lives elsewhere, e.g.\n"
                f"  python3 {Path(__file__).name} --paths registry tests",
                file=sys.stderr,
            )
        else:
            print(_unresolved_scope_message(missing, args.paths), file=sys.stderr)
        return 1

    targets = _resolve_targets(args.paths)
    if not targets:
        # Every scope entry exists and none of them holds a scannable file. That
        # is a real "checked, found nothing", unlike the case above.
        print("nothing to scan in " + ", ".join(args.paths), file=sys.stderr)
        return 0

    all_hits: list[Hit] = []
    for path in targets:
        all_hits.extend(_scan_file(path))

    if not all_hits:
        # Report the count, not just the exit code: a gate that passes because
        # it resolved an empty scope is the failure mode this line makes visible.
        print(f"doc-refs gate: {len(targets)} file(s) scanned, 0 violation(s)")
        return 0

    for hit in all_hits:
        rel = _relative_for_report(hit.path)
        print(
            f"{rel}:{hit.line_no}: {hit.pattern.name}: {hit.matched}\n" f"    {hit.line_text}",
        )
    print(
        f"\n{len(all_hits)} forbidden reference(s) found. " f"Run with --explain for fix guidance.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
