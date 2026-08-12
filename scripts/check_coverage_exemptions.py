"""Verify every coverage `omit` entry carries a reason and is still necessary.

`make test-coverage` measures `contextplane/` against a floor. `omit` removes a
file from that measurement entirely, which makes it the one setting in this
repository that can raise the reported percentage without a single new test. A
bare path in `omit` is therefore indistinguishable from turning the ratchet off
for that file, and standing policy forbids lowering the floor quietly.

So an entry has to say which of two things it is, and prove it:

- `TierExemption` -- the module is exercised by a tier the ratchet does not run
  (integration, perf, airgap). The entry names both the tier and the test file
  that covers it, and this gate fails if that file stops existing or stops
  referencing the module. An exemption whose evidence has gone is a claim
  nobody is checking.
- `DrainableDebt` -- nothing tests the module yet. A reason is required, and the
  entry is meant to leave when the tests arrive.

`reason` is a required constructor field in both, not an optional comment:
there is no way to build an entry without one. This is the same two-category
split `check_file_sizes.py` already proved out here -- a permanent category that
never goes stale beside a drainable one that must.

**The eligibility bound: a file is omit-eligible only at 0.00% under the tiers
the ratchet runs.** `omit` is file-granular, so omitting a partly-covered file
discards its *covered* statements too. When such a file measures below the
project total, removing it raises the reported percentage while testing nothing
more -- a floor drop wearing an honesty improvement's clothes. At 0.00% there
are no covered statements to lose, and the omission only stops the project
claiming credit for a file no measured tier touches.

**How the bound is enforced without running coverage.** A file that any measured
tier imports has its module-level statements executed, so it cannot be at 0.00%.
This gate walks the import graph from every test under the measured tiers
through `contextplane/`, and refuses an entry for any module it reaches. That
also makes the entry self-invalidating in the direction that matters: the moment
a unit or conformance test starts importing the module, the entry fails instead
of lingering.

The walk is deliberately conservative -- it counts an import inside a
`TYPE_CHECKING` block, which does not execute at runtime. The two error
directions are not symmetric: refusing a legitimate omission costs an argument,
while permitting an illegitimate one silently moves a floor that exists to stop
exactly that. A gate protecting a ratchet errs toward refusing.

Run locally:
    python scripts/check_coverage_exemptions.py
    python scripts/check_coverage_exemptions.py --explain
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import sys
import tomllib
from collections.abc import Iterable, Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent

#: The tiers `make test-coverage` actually runs. A module reachable from either
#: has covered statements and cannot be omitted. Kept here rather than read from
#: the Makefile because it is the definition the bound rests on: if the measured
#: tiers ever change, this list is the thing that has to be updated deliberately.
MEASURED_TIERS: tuple[str, ...] = ("tests/unit", "tests/conformance")

#: Tiers a `TierExemption` may name. A tier outside this set is either measured
#: already (so the exemption is wrong) or does not exist (so the evidence cannot
#: be checked).
UNMEASURED_TIERS: frozenset[str] = frozenset({"integration", "perf", "airgap"})

_PACKAGE = "contextplane"


@dataclasses.dataclass(frozen=True)
class TierExemption:
    """A module covered by a tier the ratchet does not run.

    `test_file` and `tier` are what make this checkable. Without them the entry
    asserts "something else covers it" and nothing can ever disagree -- which is
    the shape of every waiver that outlives its reason.
    """

    path: str
    tier: str
    test_file: str
    reason: str


@dataclasses.dataclass(frozen=True)
class DrainableDebt:
    """A module nothing tests yet, omitted so the floor is not held hostage.

    Meant to leave. An entry here is a debt marker, not a verdict about the
    module, and the reason says what would have to happen for it to go.
    """

    path: str
    reason: str


#: Either kind of entry. Both carry `path` and `reason`; only the checks that
#: need a tier narrow to `TierExemption`.
Entry = TierExemption | DrainableDebt


#: Modules exempted because an unmeasured tier covers them.
#:
#: Empty, and deliberately so. The single entry this gate was written to
#: regularise -- `contextplane/extraction/contract_suite.py` -- turned out to
#: measure 92.93% under `tests/unit` alone, because `ExtractionProviderContract`
#: is a base class that both first-party adapters' unit tests subclass. It failed
#: the 0.00% bound by the widest possible margin and was removed from `omit`
#: rather than recategorised.
TIER_EXEMPTIONS: tuple[TierExemption, ...] = ()

#: Modules nothing tests yet. Also empty; see above.
DRAINABLE_DEBT: tuple[DrainableDebt, ...] = ()


def configured_omit(repo_root: Path) -> list[str]:
    """The `omit` list as coverage.py will read it."""
    config = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return list(config.get("tool", {}).get("coverage", {}).get("run", {}).get("omit", []))


def _module_name(rel_path: str) -> str:
    """`contextplane/a/b.py` -> `contextplane.a.b`; `__init__.py` -> the package."""
    parts = Path(rel_path).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_modules(source: str) -> set[str]:
    """Every `contextplane...` module named by an import in `source`.

    Relative imports are not resolved: nothing under `tests/` uses them, and a
    production module's relative import is reached anyway through the absolute
    import that brought its package in.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.split(".")[0] == _PACKAGE)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == _PACKAGE:
            found.add(node.module)
            # `from contextplane.pkg import module` names a module, not an
            # attribute, whenever `pkg` is a package -- and a submodule import
            # executes that submodule. Both readings are added; a name that is
            # really an attribute resolves to no file and is dropped below.
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _module_file(module: str, repo_root: Path) -> str | None:
    """The repo-relative file a module name resolves to, if one exists."""
    base = repo_root / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return str(candidate.relative_to(repo_root))
    return None


def _iter_py_files(root: Path) -> Iterator[Path]:
    yield from sorted(p for p in root.rglob("*.py") if p.is_file())


def reachable_from_measured_tiers(repo_root: Path, tiers: Iterable[str] = MEASURED_TIERS) -> set[str]:
    """Every `contextplane/` file the measured tiers can import, transitively.

    Breadth-first from the tests rather than a whole-package scan: the question
    is not "does this file exist" but "does running the ratchet execute it".
    """
    frontier: set[str] = set()
    for tier in tiers:
        tier_root = repo_root / tier
        if not tier_root.is_dir():
            continue
        for test_file in _iter_py_files(tier_root):
            frontier |= _imported_modules(test_file.read_text(encoding="utf-8", errors="replace"))

    reached: set[str] = set()
    seen_modules: set[str] = set()
    while frontier:
        module = frontier.pop()
        if module in seen_modules:
            continue
        seen_modules.add(module)
        rel = _module_file(module, repo_root)
        if rel is None:
            continue
        reached.add(rel)
        frontier |= _imported_modules((repo_root / rel).read_text(encoding="utf-8", errors="replace")) - seen_modules
    return reached


def violations(
    repo_root: Path,
    tier_exemptions: tuple[TierExemption, ...] = TIER_EXEMPTIONS,
    drainable_debt: tuple[DrainableDebt, ...] = DRAINABLE_DEBT,
) -> list[str]:
    """Every reason the omit list does not hold up, as reader-facing sentences.

    The registries are parameters rather than module reads so each failure mode
    can be proved against a scratch tree. A gate whose failure paths have never
    been watched fire is a gate nobody knows the shape of, and this one starts
    with both registries empty -- so its own tests are the only thing that ever
    exercises them.
    """
    problems: list[str] = []
    omit = configured_omit(repo_root)
    entries: tuple[Entry, ...] = (*tier_exemptions, *drainable_debt)
    registered = [entry.path for entry in entries]

    # 1. The registry and the config must name the same files. An omit entry
    #    with no registered reason is the bare path this gate exists to refuse;
    #    a registered entry absent from omit is a reason for nothing.
    for path in sorted(set(omit) - set(registered)):
        problems.append(
            f"{path}: omitted in pyproject.toml with no registered reason. Add a TierExemption or "
            f"DrainableDebt entry in {Path(__file__).name}, or remove it from `omit` -- a bare path is "
            "indistinguishable from switching the ratchet off for that file."
        )
    for path in sorted(set(registered) - set(omit)):
        problems.append(
            f"{path}: registered here but not in pyproject.toml's `omit`. The entry justifies an exemption "
            "that is not in force; remove it."
        )

    duplicates = sorted({path for path in registered if registered.count(path) > 1})
    for path in duplicates:
        problems.append(f"{path}: registered more than once; two reasons for one file will drift apart.")

    # 2. Every registered file must exist. A stale path silently exempts nothing
    #    while reading as a live waiver.
    for path in sorted(set(registered)):
        if not (repo_root / path).is_file():
            problems.append(f"{path}: registered but no such file. Remove the entry.")

    # 3. The eligibility bound, enforced mechanically.
    reachable = reachable_from_measured_tiers(repo_root)
    for path in sorted(set(registered) & reachable):
        problems.append(
            f"{path}: reachable from {' or '.join(MEASURED_TIERS)}, so it has covered statements and cannot "
            "be at 0.00%. Omitting it discards those covered statements too, which moves the reported "
            "percentage without testing anything more. Remove the entry."
        )

    # 4. A tier exemption's evidence must still be there and still point at it.
    for entry in tier_exemptions:
        if entry.tier not in UNMEASURED_TIERS:
            problems.append(
                f"{entry.path}: names tier {entry.tier!r}, which is not one of {sorted(UNMEASURED_TIERS)}. "
                "A measured tier cannot be the reason a file is unmeasured."
            )
        evidence = repo_root / entry.test_file
        if not evidence.is_file():
            problems.append(
                f"{entry.path}: names {entry.test_file} as the {entry.tier} coverage, and that file does not "
                "exist. The exemption's evidence is gone, so the exemption is a claim nobody is checking."
            )
            continue
        module = _module_name(entry.path)
        if module not in evidence.read_text(encoding="utf-8", errors="replace"):
            problems.append(
                f"{entry.path}: {entry.test_file} no longer references {module}. Whatever that file tests "
                "today, it is not the evidence this exemption rests on."
            )

    # 5. A reason is required by construction, but an empty string still parses.
    for registered_entry in entries:
        if not registered_entry.reason.strip():
            problems.append(f"{registered_entry.path}: carries an empty reason, which is a bare path with extra steps.")

    return problems


def _print_explain() -> int:
    print(__doc__)
    print(f"measured tiers: {', '.join(MEASURED_TIERS)}")
    print(f"tier exemptions: {len(TIER_EXEMPTIONS)}")
    for entry in TIER_EXEMPTIONS:
        print(f"  {entry.path} -- {entry.tier} via {entry.test_file}")
    print(f"drainable debt: {len(DRAINABLE_DEBT)}")
    for debt in DRAINABLE_DEBT:
        print(f"  {debt.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify every coverage omit entry is reasoned and still needed.")
    parser.add_argument("--explain", action="store_true", help="Print the rule and the current registry.")
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    problems = violations(_REPO_ROOT)
    if problems:
        print("coverage-exemptions gate: the omit list does not hold up", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    omit = configured_omit(_REPO_ROOT)
    print(
        f"coverage-exemptions gate: {len(omit)} omitted file(s), "
        f"{len(TIER_EXEMPTIONS)} tier-exempt, {len(DRAINABLE_DEBT)} drainable"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
