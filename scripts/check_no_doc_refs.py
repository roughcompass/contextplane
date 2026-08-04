"""Lint that shipped code contains no internal-doc references.

The full rule lives in `CLAUDE.md` at the repo root. This script is the
programmatic gate that enforces it. Run it locally or wire into CI:

    python registry/scripts/check_no_doc_refs.py
    python registry/scripts/check_no_doc_refs.py --explain
    python registry/scripts/check_no_doc_refs.py --paths registry/registry/service

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


# Default scope when --paths is not given. Paths are relative to the *workspace*
# — the directory holding this repo — not to the repo root, which is why every
# default entry below starts with `registry/`. Named accordingly: calling it the
# repo root is what makes the `registry/registry` paths look like a typo.
_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_SCOPE: tuple[str, ...] = (
    "registry/registry",
    "registry/sync",
    "registry/tests",
    "registry/scripts",
    "registry/eval",
    "registry/CONTRIBUTING.md",
    "registry/README.md",
    "registry/.env.example",
    "registry/packaging/helm",
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
        name="CAP-PN-TNN",
        regex=re.compile(r"\bCAP-P\d+R?-T\d+[a-z]?\b"),
        explain=(
            "Development-plan task ID. Allowed only in eval/EVAL.md as a "
            "commit-history anchor (`git log --grep=...`). Elsewhere, anyone "
            "can `git blame` to find the introducing commit — task IDs in "
            "comments are noise."
        ),
    ),
    Pattern(
        name="CC-TNN",
        regex=re.compile(r"\bCC-T\d+\b"),
        explain="Config-consolidation task ID. Same rule as CAP-PN-TNN.",
    ),
    Pattern(
        name="DRC-TNN",
        regex=re.compile(r"\bDRC-T\d+\b"),
        explain="Doc-reference-cleanup task ID. Same rule as CAP-PN-TNN.",
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
    return [entry for entry in scope if not (_WORKSPACE_ROOT / entry).exists()]


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


def _resolve_targets(scope: list[str]) -> list[Path]:
    """Expand the scope list into concrete files to scan."""
    out: list[Path] = []
    for entry in scope:
        target = (_WORKSPACE_ROOT / entry).resolve()
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

    A relative path inside the EVAL.md commit-anchor column is also
    exempted for the CAP-PN-TNN / CC-TNN / DRC-TNN patterns (per the rule
    in `CLAUDE.md`).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    is_eval_md = path.name == "EVAL.md"
    task_id_patterns = {"CAP-PN-TNN", "CC-TNN", "DRC-TNN"}

    hits: list[Hit] = []
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        if _BYPASS_MARKER in raw_line:
            continue
        for pattern in _PATTERNS:
            m = pattern.regex.search(raw_line)
            if m is None:
                continue
            if is_eval_md and pattern.name in task_id_patterns:
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
    print("EVAL.md (in eval/) is allowed to reference CAP-/CC-/DRC- task IDs " "as commit-history anchors.")
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
                f"the default scope resolved to no files under {_WORKSPACE_ROOT}.\n"
                "This gate assumes the repository is checked out at <workspace>/registry/. "
                "It is not, so nothing was scanned — pass --paths explicitly, e.g.\n"
                f"  python3 {Path(__file__).name} --paths "
                f"{Path.cwd().name}/registry {Path.cwd().name}/tests",
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
        return 0

    for hit in all_hits:
        rel = hit.path.relative_to(_WORKSPACE_ROOT)
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
