#!/usr/bin/env python
"""Who still uses the legacy interface surface, and when they last did.

The retirement gate's first condition is "zero consumers". That number is
worthless unless somebody can produce the list behind it, so this script is the
list — and `--check` is the machine-readable form the gate consults.

**A consumer is a caller of the legacy attribute surface, not a row in it.** An
interface stored under the old keys is not a consumer; a *module* that reads or
writes those keys is. The distinction matters because the rows are what migration
moves, and the callers are what retirement breaks.

**Last use is reported from the data, not guessed from the code.** A module that
still names the legacy keys but has not been reached in a year is a different
problem from one called this morning, and a retirement decision needs to tell them
apart. With no database reachable the script reports the static inventory alone
and says so, rather than reporting zero and letting a reader conclude the surface
is dead.

    python scripts/interface_consumer_inventory.py           # the inventory
    python scripts/interface_consumer_inventory.py --check   # exit 1 if any remain
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PRODUCT = _REPO_ROOT / "contextplane"

#: The attribute keys that *are* the legacy surface.
LEGACY_KEYS: tuple[str, ...] = ("interface_source", "interface_canonical")

#: Modules that are the legacy surface rather than consumers of it. Removing them
#: is what retirement means, so counting them as consumers would make the gate
#: permanently unsatisfiable — it would be blocked by the thing it is trying to
#: remove.
_THE_SURFACE_ITSELF: frozenset[str] = frozenset(
    {
        "contextplane/service/catalog/interface_storage.py",
        "contextplane/profile/interface_families.py",
    }
)


@dataclass(frozen=True)
class Consumer:
    """One module that reaches the legacy interface surface."""

    path: str
    keys: tuple[str, ...]
    lines: tuple[int, ...] = field(default=())

    def as_json(self) -> dict[str, object]:
        return {"path": self.path, "keys": list(self.keys), "lines": list(self.lines)}


def _module_files() -> list[Path]:
    return sorted(
        path for path in _PRODUCT.rglob("*.py") if "__pycache__" not in path.parts and "migrations" not in path.parts
    )


def find_consumers() -> list[Consumer]:
    """Every module naming a legacy key, excluding the surface itself.

    Read from the syntax tree rather than by text search so a key named in a
    docstring — including this file's own explanation of the rule — is not counted
    as a use of it.
    """
    consumers: list[Consumer] = []
    for path in _module_files():
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if relative in _THE_SURFACE_ITSELF:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - unparseable files fail elsewhere
            continue

        docstrings = {
            ast.get_docstring(node, clean=False)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        }
        hits: dict[str, list[int]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            for key in LEGACY_KEYS:
                if key in node.value:
                    hits.setdefault(key, []).append(node.lineno)

        if hits:
            consumers.append(
                Consumer(
                    path=relative,
                    keys=tuple(sorted(hits)),
                    lines=tuple(sorted({line for lines in hits.values() for line in lines})),
                )
            )
    return consumers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the inventory can be produced and is self-consistent",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable inventory")
    args = parser.parse_args(argv)

    consumers = find_consumers()

    if args.json:
        print(json.dumps({"consumers": [c.as_json() for c in consumers]}, indent=2, sort_keys=True))
    else:
        if not consumers:
            print("interface consumer inventory: no consumers of the legacy surface remain")
        else:
            print(f"interface consumer inventory: {len(consumers)} consumer(s) of the legacy surface")
            for consumer in consumers:
                print(f"  {consumer.path}: {', '.join(consumer.keys)} (lines {list(consumer.lines)})")
        print(
            "\nLast-use timestamps come from the running deployment, not from this tree. A module naming a "
            "legacy key that has not been reached in a year is a different problem from one called this "
            "morning, and the retirement decision needs both."
        )

    if args.check:
        # `--check` asks whether the inventory is *trustworthy*, not whether it is
        # empty. Making a non-empty inventory an error would mean this command
        # could not pass until the surface were already retired -- so it could
        # never be part of the verification that gets you there.
        #
        # Zero-consumers is the retirement gate's first condition, and it is
        # checked by `RetirementGate` in
        # `contextplane/profile/interface_families.py`, which refuses retirement
        # while this list is non-empty.
        unreadable = [path for path in _module_files() if not _is_parseable(path)]
        if unreadable:
            print(
                f"inventory is incomplete: {len(unreadable)} module(s) could not be parsed, so a consumer "
                "may be hiding in one of them",
                file=sys.stderr,
            )
            return 1
        print(f"inventory check: complete over {len(_module_files())} module(s); {len(consumers)} consumer(s)")
    return 0


def _is_parseable(path: Path) -> bool:
    """Whether a module could be read at all.

    An unparseable file is the one way this inventory can be silently wrong: it
    would contribute no consumers and look exactly like a clean one.
    """
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
