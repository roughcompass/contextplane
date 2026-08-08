#!/usr/bin/env python3
"""Release gate: every recall and pilot-write surface is inventoried and reasoned.

The inventory is **generated**, not maintained. It is derived by walking the
directories that hold surfaces, so a router, tool or worker added tomorrow is in
the inventory the moment it exists rather than the moment somebody remembers to
list it. A hand-maintained list of 80-odd modules would be wrong within a week,
and wrong in the direction that matters: the missing entry is always the new
surface nobody thought about.

**What a reason is required for.** Not for being in scope -- that is the default
and needs no defence. A reason is required for every family (why this directory
holds surfaces at all) and for every *exclusion* (why this module writes or
returns pilot material and is nonetheless not inventoried). Exclusions are where
the risk lives: an unreasoned exclusion is indistinguishable from switching the
gate off for that file, so `Exclusion.reason` is a constructor argument with no
default rather than a comment beside the path.

**Both directions are checked, which is what keeps the list honest.**

1. A module inside a family directory that is neither inventoried nor excluded
   fails, naming itself. This is the new-surface case.
2. Every exclusion is re-checked against the tree, independent of any `--paths`
   narrowing. An exclusion naming a file that no longer exists is stale and
   fails until removed. Without this the exclusion list only ever grows and
   rots into a set of claims nobody re-reads -- a waiver nobody needs is a
   waiver nobody is thinking about.

The same shape the file-size and test-assertion gates in this directory already
use, for the same reason.

Usage:
    python scripts/check_surface_inventory.py            # gate the whole tree
    python scripts/check_surface_inventory.py --json     # emit the inventory
    python scripts/check_surface_inventory.py --explain  # families and reasons
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from checklib import repo_root, require_nonempty, run_guard

_REPO_ROOT = repo_root()

#: A surface that returns stored material to a caller.
RECALL = "recall"
#: A surface through which caller-supplied material enters storage.
PILOT_WRITE = "pilot_write"


@dataclasses.dataclass(frozen=True)
class SurfaceFamily:
    """A directory whose modules are surfaces, and why.

    `members` narrows a family to named modules, for a package that holds
    surfaces alongside plain support code. A family with no `members` takes
    every module in its directory, which is the safer default: it fails loudly
    on an unrecognised addition rather than quietly ignoring it.
    """

    root: str
    kind: str
    reason: str
    members: tuple[str, ...] = ()

    def paths(self, repo_root: Path) -> list[Path]:
        directory = repo_root / self.root
        if not directory.is_dir():
            return []
        if self.members:
            return [directory / name for name in self.members if (directory / name).is_file()]
        return sorted(p for p in directory.glob("*.py") if p.name != "__init__.py")


@dataclasses.dataclass(frozen=True)
class Exclusion:
    """A module inside a family that is deliberately not inventoried.

    The reason is required. An excluded path with no stated reason is the same
    as an unwatched one, and the point of writing exclusions down is that the
    next reader can disagree with them.
    """

    path: str
    reason: str


FAMILIES: tuple[SurfaceFamily, ...] = (
    SurfaceFamily(
        root="contextplane/api/routers",
        kind=PILOT_WRITE,
        reason=(
            "The HTTP surface. Every caller-supplied body that reaches storage arrives through "
            "one of these, so the family is in scope wholesale rather than per-router."
        ),
    ),
    SurfaceFamily(
        root="contextplane/api/mcp/tools",
        kind=PILOT_WRITE,
        reason=(
            "The agent-facing twin of the routers. Each re-resolves the caller's tenant the way "
            "the REST middleware does, and each can write, so neither transport may be "
            "inventoried without the other."
        ),
    ),
    SurfaceFamily(
        root="contextplane/workers",
        kind=PILOT_WRITE,
        reason=(
            "Background jobs write material no request is waiting on. They are the surfaces "
            "least likely to be noticed by hand, which is why they are enumerated by walking."
        ),
    ),
    SurfaceFamily(
        root="contextplane/arc/workers",
        kind=PILOT_WRITE,
        reason=(
            "The governance-side jobs, registered as scheduler jobs like the rest. A separate "
            "family because they live in a separate package, and a family-per-directory rule "
            "is what stops a whole package being missed for living somewhere else."
        ),
    ),
    SurfaceFamily(
        root="contextplane/ingest",
        kind=PILOT_WRITE,
        reason=(
            "External material entering the system: connector runs and inbound webhooks. The "
            "only family whose input is written by somebody who is not a caller of this API."
        ),
    ),
    SurfaceFamily(
        root="contextplane/context",
        kind=RECALL,
        reason=(
            "The recall path: the arms that read material, the assembler that labels it, and "
            "the surfaces that return receipts and resume state. Narrowed to named modules "
            "because this package also holds models and queries, which store nothing and "
            "return nothing on their own."
        ),
        members=(
            "arms.py",
            "assembler.py",
            "resolve.py",
            "receipts.py",
            "references.py",
            "resume.py",
        ),
    ),
)


#: Every exclusion is a claim that a module inside a surface family neither
#: returns stored material nor admits any, and each is written to be disagreed
#: with. Support code is the whole of this list today: the moment an exclusion
#: is needed for something that *does* touch material, that is the interesting
#: entry and it should be hard to write without noticing.
EXCLUSIONS: tuple[Exclusion, ...] = (
    Exclusion(
        path="contextplane/ingest/connector.py",
        reason=(
            "Resolves connector credentials by dynamic ref string. Plumbing the other ingest "
            "modules call; holds no material of its own."
        ),
    ),
    Exclusion(
        path="contextplane/ingest/connector_registry.py",
        reason="Maps connector type names to implementations. Reads nothing, writes nothing.",
    ),
    Exclusion(
        path="contextplane/ingest/queries.py",
        reason=(
            "SQL the ingest surfaces execute. Inventorying it as well as its callers would "
            "count the same admission twice and make the total read larger than the surface is."
        ),
    ),
    Exclusion(
        path="contextplane/context/admission.py",
        reason=(
            "Decides whether content may be stored. It performs no write and returns no stored "
            "material -- the surfaces it guards are inventoried in their own families, and "
            "counting the check as well would double every admission."
        ),
    ),
    Exclusion(
        path="contextplane/context/intent.py",
        reason=(
            "Decides which of the four agent write paths a request is, and refuses the "
            "crossings. It routes writes rather than performing one -- the surfaces it routes "
            "to are inventoried in their own families."
        ),
    ),
    Exclusion(
        path="contextplane/context/models.py",
        reason="Row definitions. Declares shape, performs no read or write.",
    ),
    Exclusion(
        path="contextplane/context/models_receipt.py",
        reason="Row definitions for the receipt tables. Same as the above.",
    ),
    Exclusion(
        path="contextplane/context/quality.py",
        reason=(
            "Derives a quality summary from arms that have already returned. It reads the "
            "assembler's own output, never storage, so it can neither miss a label nor add one."
        ),
    ),
    Exclusion(
        path="contextplane/context/queries.py",
        reason=(
            "SQL the recall arms execute. Excluded for the same reason as the ingest queries: "
            "the arm is the surface, and counting both would double it."
        ),
    ),
)


@dataclasses.dataclass(frozen=True)
class Surface:
    """One inventoried surface, as the generated inventory reports it."""

    path: str
    kind: str
    family: str

    def as_json(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "family": self.family}


def build_inventory(repo_root: Path = _REPO_ROOT) -> list[Surface]:
    """Walk every family and return the surfaces, sorted for a stable diff."""
    excluded = {exclusion.path for exclusion in EXCLUSIONS}
    surfaces: list[Surface] = []
    for family in FAMILIES:
        for path in family.paths(repo_root):
            relative = path.relative_to(repo_root).as_posix()
            if relative in excluded:
                continue
            surfaces.append(Surface(path=relative, kind=family.kind, family=family.root))
    return sorted(surfaces, key=lambda surface: surface.path)


def _candidates(repo_root: Path) -> dict[str, str]:
    """Every module a family directory holds, mapped to the family that holds it.

    The shared domain of both checks below, and they must share it: when they
    were computed separately, a module inside a family narrowed by `members`
    was simultaneously required to carry an exclusion and rejected for carrying
    one, because "what the family takes" and "what the family contains" are
    different sets and only the second is the question being asked here.
    """
    candidates: dict[str, str] = {}
    for family in FAMILIES:
        directory = repo_root / family.root
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name == "__init__.py":
                continue
            candidates.setdefault(path.relative_to(repo_root).as_posix(), family.root)
    return candidates


def stale_exclusions(repo_root: Path = _REPO_ROOT) -> list[str]:
    """Exclusions naming a file that is gone, or that sits in no family at all.

    The second case matters as much as the first: an exclusion for a path
    outside every family excludes nothing, and reads to the next person as
    though some surface is being deliberately skipped when none is.
    """
    candidates = _candidates(repo_root)

    stale: list[str] = []
    for exclusion in EXCLUSIONS:
        if not (repo_root / exclusion.path).is_file():
            stale.append(f"{exclusion.path}: excluded, but no such file")
        elif exclusion.path not in candidates:
            stale.append(f"{exclusion.path}: excluded, but it is in no surface family, so it excludes nothing")
    return stale


def unregistered(repo_root: Path = _REPO_ROOT) -> list[str]:
    """Modules inside a family directory that are neither inventoried nor excluded.

    Only reachable for a family that names its `members`: a family without them
    takes its whole directory, so nothing in it can be unregistered. Narrowing a
    family is therefore the one way to acquire this debt, and this is what makes
    that narrowing visible instead of silent.
    """
    inventoried = {surface.path for surface in build_inventory(repo_root)}
    excluded = {exclusion.path for exclusion in EXCLUSIONS}

    return [
        f"{relative}: inside {family_root}, which is a surface family, but the family "
        "does not name it and no exclusion explains why"
        for relative, family_root in sorted(_candidates(repo_root).items())
        if relative not in inventoried and relative not in excluded
    ]


def _explain() -> int:
    for family in FAMILIES:
        scope = f"{len(family.members)} named module(s)" if family.members else "every module"
        print(f"{family.root} [{family.kind}] — {scope}")
        print(f"    {family.reason}")
    if EXCLUSIONS:
        print("\nExclusions:")
        for exclusion in EXCLUSIONS:
            print(f"  {exclusion.path}\n      {exclusion.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit the generated inventory and exit")
    parser.add_argument("--explain", action="store_true", help="print families, scopes and reasons")
    args = parser.parse_args(argv)

    if args.explain:
        return _explain()

    inventory = build_inventory()
    # Both findings below are computed against this inventory. If discovery
    # returns nothing, "no unregistered surfaces" is a statement about an empty
    # set rather than about the API.
    require_nonempty(inventory, "the discovered surface inventory")

    if args.json:
        print(json.dumps([surface.as_json() for surface in inventory], indent=2))
        return 0

    findings = unregistered() + stale_exclusions()
    if findings:
        print("surface-inventory gate: FAILED", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nInventory a new surface by letting its family take it, or write an Exclusion "
            "with a reason in scripts/check_surface_inventory.py.",
            file=sys.stderr,
        )
        return 1

    recall = sum(1 for surface in inventory if surface.kind == RECALL)
    writes = len(inventory) - recall
    print(
        f"surface-inventory gate: {len(inventory)} surface(s) inventoried "
        f"({recall} recall, {writes} pilot write), {len(EXCLUSIONS)} reasoned exclusion(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
