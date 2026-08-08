#!/usr/bin/env python3
"""Lint gate: only an allowlisted module may write `artifact_activation` evidence.

Two tables are governed, because ARC has -- deliberately -- two of them.
`arc_approval_evidence` is the pre-existing table, now restricted to
`exception_approval` writes only after its direct `artifact_activation`
write path was removed: a row of type `artifact_activation` there today is
a `check_privileged_writes.py`-style violation of trust, since it is what a
revision's activation used to check directly, before the D2 challenge/proof
protocol existed. `arc_projection_approval_evidence` is that protocol's own
table, added alongside `arc_approval_challenges`: every row in it *is*
`artifact_activation`-class evidence by construction -- there is no
`evidence_type` column to check a literal value against, because the table
itself carries no other meaning. Both tables trade on the same fact: that
trust only holds while there is exactly one place that can produce such a
row under the invariants a real writer has to enforce -- challenge/nonce
consumption, actor binding, digest recomputation. A second writer produces
rows that look identical while satisfying none of them.

This gate is deliberately AST-based rather than a text search. A text
search for `artifact_activation` would fail on this file's own docstring,
on every other module's comments and tests that discuss the value without
writing it, and on the profile-literal tables that enumerate every evidence
type without ever calling `INSERT`. What actually matters is a narrower,
structural question: is a `sqlalchemy.text(...)` call anywhere in this
file's call sites -- not its prose -- passed a string that writes one of the
two governed tables (and, for the legacy table only, also names
`artifact_activation` as a value -- the new table needs no such check, since
every row in it already means that). Parsing the AST and inspecting call
arguments answers that question directly; grepping the raw text cannot tell
a call site from a comment about one.

The allowlist starts empty and gains exactly one entry, added deliberately
and reviewed, the day a first-party challenge/proof writer exists for the
new table (`approval_challenge.py`). Every other writer named in it is the
same kind of deliberate addition -- see above for why a wide-open default is
the failure mode this gate exists to prevent. Tests and migrations are out
of scope: tests legitimately seed rows directly to exercise the read side of
this exact restriction, and a migration's own schema-bootstrapping inserts
run under the migration runner's control, not this deployment's request
path.

Run locally:
    python scripts/check_arc_approval_writers.py
    python scripts/check_arc_approval_writers.py --explain
    python scripts/check_arc_approval_writers.py --paths registry/arc/service
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Resolve from the repo root, not the workspace above it. Going up one extra
# level and back down through a literal directory name breaks in any checkout
# not named that -- a git worktree, most often -- and the gate then scans
# nothing while still exiting non-zero.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# The shipped application only. Tests seed `artifact_activation` rows
# directly to exercise the read/refusal side of this exact restriction, and
# migrations run under the migration runner's control -- neither is a
# production request path this gate needs to police.
_DEFAULT_SCOPE: tuple[str, ...] = ("registry",)

_EXCLUDE_SUBTREE_SUFFIXES: tuple[str, ...] = ("storage/migrations",)

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
)

# The evidence_type value this gate exists to constrain, for the legacy table.
_TARGET_VALUE = "artifact_activation"

# The legacy table: a write only violates this gate when it also names
# `_TARGET_VALUE` as a value -- `exception_approval` writes to the same
# table remain legitimate and unrestricted.
_LEGACY_TABLE_PATTERN = re.compile(r"\bINSERT\s+INTO\s+arc_approval_evidence\b", re.IGNORECASE)

# The D2 protocol's own table: any write at all is governed, because the
# table carries no `evidence_type` column to check a literal value
# against -- every row in it already means `artifact_activation`.
_PROJECTION_EVIDENCE_TABLE_PATTERN = re.compile(r"\bINSERT\s+INTO\s+arc_projection_approval_evidence\b", re.IGNORECASE)

#: Modules permitted to write `artifact_activation` evidence. Empty until a
#: reviewed first-party writer exists -- see the module docstring.
ALLOWLIST: frozenset[str] = frozenset({"registry/arc/service/approval_challenge.py"})


# ---------------------------------------------------------------------------
# AST-based detection
# ---------------------------------------------------------------------------


def _is_text_call(node: ast.Call) -> bool:
    """True for a call shaped like `text(...)` or `sqlalchemy.text(...)`.

    This is the call-site anchor: every raw-SQL write in this codebase goes
    through SQLAlchemy's `text()`, so restricting detection to calls of that
    exact shape ties a violation to an actual SQL-execution primitive,
    rather than to any string literal that happens to appear as some call's
    argument.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "text"
    if isinstance(func, ast.Attribute):
        return func.attr == "text"
    return False


def _string_literal(node: ast.AST) -> str | None:
    """The literal string value of a call argument, if it is a plain constant.

    Adjacent string literals (`"a" "b"`) are already merged into one
    `ast.Constant` by the parser -- the multi-line style every raw-SQL call
    in this codebase uses -- so this resolves the whole statement without
    needing to chase concatenation. An f-string, `.format()` call, or `+`
    join is not chased; a writer built that way would also defeat a text
    grep, and this gate's job is to catch the call shape every writer here
    actually uses, not to prove no obfuscation is possible.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _violations_in_file(path: Path) -> list[int]:
    """Line numbers of `text(...)` call sites in `path` that write
    `artifact_activation` evidence -- into `arc_approval_evidence` (legacy
    table, literal-value gated) or `arc_projection_approval_evidence` (D2's
    own table, gated on any write at all)."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_text_call(node):
            continue
        for arg in node.args:
            sql = _string_literal(arg)
            if sql is None:
                continue
            if _LEGACY_TABLE_PATTERN.search(sql) and _TARGET_VALUE in sql:
                lines.append(node.lineno)
            elif _PROJECTION_EVIDENCE_TABLE_PATTERN.search(sql):
                lines.append(node.lineno)
    return lines


def _iter_py_files(root: Path) -> list[Path]:
    excluded_suffixes = tuple(f"/{s}" for s in _EXCLUDE_SUBTREE_SUFFIXES)
    return sorted(
        p
        for p in root.rglob("*.py")
        if p.is_file()
        and not any(part in _EXCLUDE_DIRS for part in p.parts)
        and not any(p.as_posix().endswith(s) or f"{s}/" in p.as_posix() for s in excluded_suffixes)
    )


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        # An absolute path outside the assumed root (a test's tmp_path).
        # The report is cosmetic; the allowlist match below is keyed off the
        # same string, so this stays self-consistent regardless.
        return path.as_posix()


def _is_allowlisted(rel: str) -> bool:
    return any(rel == entry or rel.endswith(f"/{entry}") for entry in ALLOWLIST)


def _print_explain() -> int:
    print("arc-approval-writers gate: what it checks and how to clear it.\n")
    print(f"No module under {_DEFAULT_SCOPE[0]} may INSERT arc_approval_evidence rows naming")
    print(f"evidence_type = {_TARGET_VALUE!r}, or INSERT into arc_projection_approval_evidence at")
    print("all, unless the module's path is in ALLOWLIST below.\n")
    print("To clear a failure:")
    print("  1. The write almost certainly belongs behind the existing first-party writer instead")
    print("     of alongside it -- a second writer is exactly what this gate exists to catch.")
    print(
        "  2. If a new writer is genuinely intended and reviewed, add its path to ALLOWLIST in "
        "scripts/check_arc_approval_writers.py."
    )
    print(f"\nCurrently allowlisted ({len(ALLOWLIST)}):")
    for entry in sorted(ALLOWLIST):
        print(f"  {entry}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify only an allowlisted module writes artifact_activation approval evidence."
    )
    parser.add_argument("--paths", nargs="+", default=list(_DEFAULT_SCOPE), help="Repo-relative paths to scan.")
    parser.add_argument("--explain", action="store_true", help="Print the rule and the current allowlist.")
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    missing = [entry for entry in args.paths if not (_REPO_ROOT / entry).exists()]
    if missing:
        print(
            f"scope does not exist under {_REPO_ROOT}: {', '.join(missing)}\n"
            "Nothing was checked, so this is a failure rather than a pass.",
            file=sys.stderr,
        )
        return 1

    scanned = 0
    violations: list[tuple[str, int]] = []
    for entry in args.paths:
        target = (_REPO_ROOT / entry).resolve()
        files = [target] if target.is_file() else _iter_py_files(target)
        for f in files:
            scanned += 1
            rel = _relative(f)
            if _is_allowlisted(rel):
                continue
            for lineno in _violations_in_file(f):
                violations.append((rel, lineno))

    print(f"arc-approval-writers gate: {scanned} file(s) scanned, {len(ALLOWLIST)} allowlisted writer(s)")

    if not violations:
        return 0

    for rel, lineno in violations:
        print(
            f"{rel}:{lineno}: writes evidence_type = {_TARGET_VALUE!r} into arc_approval_evidence; "
            "no allowlisted first-party writer covers this file",
            file=sys.stderr,
        )
    print(
        "\nAdd the writer to ALLOWLIST in scripts/check_arc_approval_writers.py only after a "
        "deliberate review -- see the module docstring for why the default is empty.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
