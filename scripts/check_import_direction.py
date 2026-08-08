#!/usr/bin/env python3
"""Tests may import scripts; scripts must never import tests.

The dev stack's mock servers and JWT factory live under scripts/devstack —
they are runtime components of the local stack, not test code — and the test
suite reaches down into them. The reverse direction is the one that rots:
production-adjacent tooling that imports test modules stops working the
moment the tests refactor, and the failure surfaces on a developer's machine
mid-`make dev-up` instead of in CI. This gate keeps the arrow one-way.
"""

from __future__ import annotations

import re
import sys

from checklib import repo_root, require_nonempty, run_guard

_FORBIDDEN = re.compile(r"^\s*(from tests[.\s]|import tests\b)", re.M)

_SCOPE = "scripts"


def main() -> int:
    root = repo_root()
    # Anchored on the repo root rather than the cwd. The previous spelling,
    # `Path("scripts").rglob(...)`, matched nothing from any other directory and
    # then announced that the arrow held over zero files.
    targets = sorted((root / _SCOPE).rglob("*.py"))
    require_nonempty(
        targets,
        f"the {_SCOPE}/ scan population",
        hint=f"Expected Python files under {root / _SCOPE}; the tree may have moved.",
    )

    offenders: list[str] = []
    for path in targets:
        if _FORBIDDEN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(root)))
    if offenders:
        print("scripts/ must not import from tests/ — move the shared code under scripts/:")
        for offender in sorted(offenders):
            print(f"  {offender}")
        return 1
    print(f"import-direction gate: {len(targets)} script file(s), arrow holds")
    return 0


if __name__ == "__main__":
    sys.exit(run_guard(main))
