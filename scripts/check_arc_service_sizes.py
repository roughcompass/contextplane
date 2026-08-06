#!/usr/bin/env python3
"""Lint gate: no module under `registry/arc/service/` exceeds an 800-line ceiling.

`artifact.py` grew to 962 lines before the split this gate accompanies --
one file doing lifecycle transitions, revision creation, and digest/integrity
checking at once, which is exactly the kind of file where "just add one more
method" always looks cheap in isolation and never looks cheap in aggregate.
Splitting it once and trusting reviewers to notice the next file creeping
past 800 is how the same growth recurs; this gate makes the ceiling something
every commit checks instead of something only a phase-boundary audit catches
after the fact.

The whole `registry/arc/service/` tree is in scope, not just the modules one
task happens to be touching -- a gate scoped to today's change would pass
forever while an unrelated file in the same package drifted past the limit
unnoticed.

A file may exceed 800 lines only with an explicit, reviewed entry in
`ALLOWLIST` below, naming the waived ceiling and the cohesion argument for
why splitting further would cost more than it buys. There is no bare
"disable this check" escape hatch -- a waiver names a number, not "no limit."

Below the ceiling, a file at or above 85% of it (680 lines) is reported as a
warning. This does not fail the build; it exists so the task that is about
to push a file over 800 sees the wall approaching in its own gate output,
rather than hitting it cold in a later task that has to stop and split.

Run locally:
    python scripts/check_arc_service_sizes.py
    python scripts/check_arc_service_sizes.py --explain
    python scripts/check_arc_service_sizes.py --paths registry/arc/service
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Anchored at the workspace root two levels above this checkout, matching
# check_privileged_writes.py's convention -- the default scope resolves
# correctly whether this is invoked from the workspace root or from `cd
# registry && ...`, because it is computed from `__file__`, not the cwd.
_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_SCOPE: tuple[str, ...] = ("registry/registry/arc/service",)

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
)

#: A file at or above this many lines fails the gate, unless waived.
_CEILING = 800

#: A file at or above this many lines (and still under its ceiling) is
#: reported as approaching it. 85% of 800.
_WARN_AT = 680


@dataclasses.dataclass(frozen=True)
class Waiver:
    """One file allowed to exceed the default ceiling, and why.

    `ceiling` is the waived limit for this file specifically -- not "no
    limit" -- so a waived file can still grow without bound being silently
    accepted. Landing an entry here requires deliberate reviewer approval
    and a recorded cohesion argument: a file that cannot be split further
    without harming cohesion, not a file nobody got around to splitting.
    """

    path: str
    ceiling: int
    reason: str


#: No waivers exist yet. Adding one is a deliberate, reviewed act -- see
#: the module and `Waiver` docstrings.
ALLOWLIST: tuple[Waiver, ...] = ()


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    lines: int
    ceiling: int


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file() and not any(part in _EXCLUDE_DIRS for part in p.parts))


def _line_count(path: Path) -> int:
    # Counts newline characters, matching `wc -l` exactly, so this gate and
    # a one-off shell check using `wc -l` against the same file never
    # disagree at the boundary.
    content = path.read_text(encoding="utf-8", errors="replace")
    return content.count("\n")


def _waiver_for(rel: str) -> Waiver | None:
    for w in ALLOWLIST:
        if rel == w.path or rel.endswith(f"/{w.path}"):
            return w
    return None


def _print_explain() -> int:
    print("arc-service-sizes gate: what it checks and how to clear it.\n")
    print(f"Every .py file under {_DEFAULT_SCOPE[0]} must be under {_CEILING} lines.")
    print(f"A file at or above {_WARN_AT} lines (85% of the ceiling) is reported as a")
    print("warning even when it still passes, so the wall is visible before it is hit.\n")
    print("To clear a failure:")
    print("  1. Split the file along a seam it already has -- cohesion, not an arbitrary")
    print("     line-count cut. Record why that boundary was chosen in the new modules'")
    print("     docstrings.")
    print(
        "  2. If no split preserves cohesion, add a Waiver to ALLOWLIST in "
        "scripts/check_arc_service_sizes.py naming the waived ceiling and the cohesion "
        "argument, with phase-boundary reviewer approval."
    )
    print(f"\nCurrently waived ({len(ALLOWLIST)}):")
    for w in ALLOWLIST:
        print(f"  {w.path} (ceiling {w.ceiling})\n    {w.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify no registry/arc/service/ module exceeds the line ceiling.")
    parser.add_argument("--paths", nargs="+", default=list(_DEFAULT_SCOPE), help="Workspace-relative paths to scan.")
    parser.add_argument("--explain", action="store_true", help="Print the rule and the current waiver list.")
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    missing = [entry for entry in args.paths if not (_WORKSPACE_ROOT / entry).exists()]
    if missing:
        print(
            f"scope does not exist under {_WORKSPACE_ROOT}: {', '.join(missing)}\n"
            "Nothing was checked, so this is a failure rather than a pass.",
            file=sys.stderr,
        )
        return 1

    sizes: list[tuple[str, int]] = []
    for entry in args.paths:
        target = (_WORKSPACE_ROOT / entry).resolve()
        files = [target] if target.is_file() else _iter_py_files(target)
        for f in files:
            try:
                rel = str(f.relative_to(_WORKSPACE_ROOT))
            except ValueError:
                # Scanned via an absolute path outside the assumed root (a
                # test's tmp_path, for instance). The report is cosmetic;
                # the ceiling check below still holds regardless of what the
                # path displays as.
                rel = f.as_posix()
            sizes.append((rel, _line_count(f)))

    sizes.sort(key=lambda item: item[1], reverse=True)

    violations: list[Violation] = []
    warnings: list[tuple[str, int]] = []
    seen_waivers: set[str] = set()

    for rel, lines in sizes:
        waiver = _waiver_for(rel)
        ceiling = waiver.ceiling if waiver is not None else _CEILING
        if waiver is not None:
            seen_waivers.add(waiver.path)
        if lines >= ceiling:
            violations.append(Violation(path=rel, lines=lines, ceiling=ceiling))
        elif lines >= _WARN_AT:
            warnings.append((rel, lines))

    # A waiver for a file that no longer exists in scope, or that no longer
    # needs it, is a permission nobody is using -- the same "stale entry"
    # failure mode check_visibility_chokepoint.py and check_privileged_writes.py
    # guard against.
    stale = [
        f"{w.path}: not found in scanned scope, or no longer needs a waiver"
        for w in ALLOWLIST
        if w.path not in seen_waivers
    ]

    if sizes:
        print(f"arc-service-sizes gate: {len(sizes)} file(s) scanned, ceiling {_CEILING} lines")
        for rel, lines in sizes[:5]:
            print(f"  {lines:>4}  {rel}")
    else:
        print("arc-service-sizes gate: no .py files in scope: " + ", ".join(args.paths))

    for rel, lines in warnings:
        print(f"warning: {rel} is {lines} lines, {_CEILING - lines} below the {_CEILING}-line ceiling")

    if not violations and not stale:
        return 0

    for v in violations:
        waived = " (waived ceiling, still exceeded)" if v.ceiling != _CEILING else ""
        print(f"{v.path}: {v.lines} lines meets or exceeds the {v.ceiling}-line ceiling{waived}", file=sys.stderr)
    for s in stale:
        print(f"stale-waiver: {s}", file=sys.stderr)

    if stale:
        print(
            "\nRemove the stale entry from ALLOWLIST in scripts/check_arc_service_sizes.py -- "
            "a waiver nobody needs is one nobody is thinking about.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
