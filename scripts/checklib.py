#!/usr/bin/env python3
"""Shared anchoring and population checks for the `scripts/check_*.py` gates.

Every guard in this directory answers a yes/no question about the tree. The two
failure modes below are dangerous precisely because both of them look like a
pass, and a gate that cannot fail is worse than no gate at all — it is a
checklist item somebody has already ticked.

1. **The vacuous scan.** A guard whose scan population is empty prints a
   cheerful summary and exits 0. The most common cause is a cwd-relative
   anchor: `Path("scripts").rglob("*.py")` matches nothing at all from any
   directory other than the repo root, so the guard reports that its rule holds
   over zero files. `repo_root()` removes the cause and `require_nonempty()`
   removes the symptom, because an anchor can be correct today and wrong after
   the next move.

2. **The stale exemption.** A guard's hard-coded path list keeps naming a file
   that has moved or been deleted. The entry governs nothing, but it still
   reads as a deliberate, reviewed permission. `require_paths_exist()` turns
   that into a loud failure, so a move that invalidates an allowlist entry is
   reported by the guard whose allowlist it is.

Both checks report *the population*, not the verdict. A guard that legitimately
finds no violations still exits 0; only a guard that inspected nothing fails.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Sized
from pathlib import Path

__all__ = [
    "GuardError",
    "VacuousScan",
    "StaleScope",
    "repo_root",
    "require_nonempty",
    "require_paths_exist",
    "resolve_scope",
    "run_guard",
]

# The marker that identifies the repository root. Anchoring on a file that must
# exist at the root is what makes the anchor independent of both the current
# working directory and of how deeply under `scripts/` a guard happens to live:
# `scripts/check_x.py` and `scripts/devstack/y.py` resolve the same root without
# either of them counting `..` hops. A guard that counts hops keeps working
# right up until it is moved one directory, and then fails silently.
_ROOT_MARKER = "pyproject.toml"


class GuardError(RuntimeError):
    """A guard cannot make a trustworthy statement about the tree.

    Raised instead of returning an exit code so that a caller cannot ignore it
    by forgetting to inspect a return value. `run_guard()` converts it into a
    non-zero exit with the message attached.
    """


class VacuousScan(GuardError):
    """A guard's scan population was empty, so its verdict covers nothing."""


class StaleScope(GuardError):
    """A hard-coded path no longer exists, so the entry naming it is dead."""


def repo_root(start: Path | None = None) -> Path:
    """Return the repository root, independent of the current directory.

    Walks upward from this file (or `start`) until it finds the directory
    holding `pyproject.toml`. Raising when there is no such directory is
    deliberate: a guard that guesses a root scans the wrong tree and reports a
    confident verdict about it.
    """
    origin = (start or Path(__file__)).resolve()
    for candidate in (origin, *origin.parents):
        if candidate.is_dir() and (candidate / _ROOT_MARKER).is_file():
            return candidate
    raise GuardError(
        f"no {_ROOT_MARKER} found at or above {origin}, so the repository root "
        "cannot be determined. This guard refuses to guess a root and scan the "
        "wrong tree."
    )


def require_nonempty(
    population: Sized | int,
    what: str,
    *,
    allow_empty: bool = False,
    hint: str | None = None,
) -> None:
    """Fail unless the guard actually has something to inspect.

    Accepts either a collection or an already-computed count, because some
    guards stream their population and only ever hold the tally.

    `allow_empty` exists for one specific, legitimate case: a caller who passed
    an explicit narrow `--paths` scope that genuinely holds no matching file has
    asked a question whose honest answer is "nothing there". That is different
    from a *default* scope resolving to nothing, which means the gate governed
    no file and said so cheerfully. Callers pass
    `allow_empty=(args.paths != DEFAULT_SCOPE)` to keep the distinction.
    """
    size = population if isinstance(population, int) else len(population)
    if size or allow_empty:
        return
    message = (
        f"{what} resolved to nothing under {repo_root()}.\n"
        "Nothing was inspected, so this gate reports a failure rather than a pass."
    )
    if hint:
        message = f"{message}\n{hint}"
    raise VacuousScan(message)


def require_paths_exist(entries: Iterable[str], what: str, *, root: Path | None = None) -> None:
    """Fail when a hard-coded path list names something that is no longer there.

    Guards carry allowlists, exemptions, and default scopes as repo-relative
    strings. When a move invalidates one, the entry stops matching and quietly
    stops governing. Reporting it here means the move is caught by the guard
    that cared, in the same run that the move breaks it.
    """
    base = root or repo_root()
    missing = [entry for entry in entries if not (base / entry).exists()]
    if missing:
        raise StaleScope(
            f"{what} names path(s) that do not exist under {base}: {', '.join(sorted(missing))}.\n"
            "Either the path moved and the entry needs updating, or the entry is "
            "dead and should be deleted. A path that matches nothing governs nothing."
        )


def resolve_scope(
    scope: Iterable[str],
    *,
    suffixes: tuple[str, ...] = (".py",),
    exclude_dirs: frozenset[str] = frozenset(),
    root: Path | None = None,
) -> list[Path]:
    """Expand repo-relative scope entries into concrete files under the root.

    A file entry is taken as-is when its suffix matches; a directory entry is
    walked. Entries are resolved against the anchored root rather than the cwd,
    which is the whole point of the anchor.
    """
    base = root or repo_root()
    out: list[Path] = []
    for entry in scope:
        target = (base / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix in suffixes:
                out.append(target)
            continue
        for path in sorted(target.rglob("*")):
            if path.is_file() and path.suffix in suffixes and not (set(path.parts) & exclude_dirs):
                out.append(path)
    return out


def run_guard(main: Callable[[], int]) -> int:
    """Run a guard's `main`, turning a `GuardError` into a loud non-zero exit.

    Guards use this in their `__main__` block so that the checks above cannot be
    defeated by a caller who forgets to look at a return value.
    """
    try:
        return main()
    except GuardError as exc:
        print(str(exc), file=sys.stderr)
        return 1
