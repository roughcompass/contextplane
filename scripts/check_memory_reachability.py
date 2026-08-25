"""Lint gate: every quarantined memory service has a live production caller.

An earlier stage of the living-memory build shipped eight services that were
fully built and integration-tested but wired to nothing: `promotion.py`,
`curation_queue.py`, `contest.py`, `confirmation.py`, `calibration.py`,
`capability_requests.py`, `source_governance.py`, and `source_ingest.py` under
`contextplane/service/memory/`. Each one's tests construct it directly and drive
it end to end -- which proves the service works, not that anything running in
production ever calls it. A later phase's REST routes, MCP tools, and
scheduled jobs closed that gap for every one of them. This gate is the static
proof: it fails the moment any of the eight goes back to having no reachable
caller, the same way a docstring claim without a gate behind it eventually
does.

**This gate is a named list, not a general audit, and the target name
`reachability-audit` oversells it.** It proves the modules below have callers.
It proves nothing about any service added afterwards, and that gap is not
hypothetical — `service/governance/obligation_evidence.py` shipped as a plan
task's deliverable, wired into the container, reached by no route and no tool,
and passed this gate every time, because it was never in the list.

**It also cannot be widened to cover them, and that is worth knowing before
somebody tries.** Reachability here means *a transport file imports the module*.
The services this repo has added since are reached through the container
(`_services(request).obligation_evidence`) or through a package front door
(`from contextplane.arc import AutonomyEnvelopeService`), and neither leaves an
import in a router file. Adding such a module to the list below makes this gate
fail on a service that is genuinely reachable.

**So the proof for those is a different test, and the convention is:** a service
reached through the container gets a test asserting its route is *mounted* —
against the router's own route table, not by calling it, because a call
exercises authorization too and a 403 would read as a pass. See
`test_obligation_evidence.py` and `test_arc_envelope_surface.py`. When you add a
service a plan entry calls a deliverable, write that test; this gate will not
catch it for you.

A module counts as reachable when at least one of these imports it, outside
`tests/` and outside the module's own file:

  - `contextplane/api/routers/` -- a REST route calls it.
  - `contextplane/api/mcp/tools/` -- an MCP tool calls it.
  - `contextplane/wiring/jobs.py` -- a scheduled job constructs or calls it.

Two named modules do not fit that general rule, and each gets a narrow,
explicit exception rather than a loosened general one:

`source_ingest.py` -- constructed in `wiring/jobs.py`, but its production call
    site is the connector run loop, not a route/tool/job import in the usual
    sense: `contextplane/ingest/runner.py` partitions each artifact's parsed
    facts and routes the claim-shaped subset through this service, after
    `connector.parse()` succeeds and before the existing facts-table write.
    `extra_caller` on its `Rule` names that one file as an additional valid
    location.

`contest.py` -- never imported by a route, tool, or job directly; it exposes
    no route or tool of its own to be called from. It is reachable
    transitively: `claim_writer.py` calls `detect_for_claim` inside
    `stage_claim`'s write path, and `consolidation.py` calls `resolve_contests_for` while
    persisting a consolidation outcome -- and both of *those* modules ARE
    imported directly by the curation router, the curation MCP tools, and the
    promotion-sweep job. `transitive_via` on its `Rule` names both
    intermediates, and the check for it verifies both halves: the
    intermediate imports `contest.py`, AND the intermediate is itself
    reachable through the general rule. This is a second named exception, not
    a general "reachable via another reachable module" rule -- generalizing
    it would let any module hide behind an arbitrarily long, never-checked
    chain. Two exceptions, both spelled out, is the honest shape; a rule that
    quietly accepted transitivity everywhere would not have caught this one
    in the first place, because nothing forced anyone to name the chain.

Purely static, like every other structural gate in this repository: it proves
an import exists, not that the import is ever exercised at runtime.

Run locally:
    python scripts/check_memory_reachability.py
    python scripts/check_memory_reachability.py --explain
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import sys
from pathlib import Path

from checklib import repo_root, require_nonempty, run_guard

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = repo_root()

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git"}
)

#: The three general locations a quarantined module needs at least one import
#: site under. Directories are walked recursively; `wiring/jobs.py` is one file.
_GENERAL_CALLER_DIRS: tuple[str, ...] = ("contextplane/api/routers", "contextplane/api/mcp/tools")
_GENERAL_CALLER_FILES: tuple[str, ...] = ("contextplane/wiring/jobs.py",)

#: Repo-relative default scope for the general-caller search. Overridable with
#: --paths for the same reason every sibling gate accepts it: a checkout laid
#: out differently than this script assumes still needs a way to point it at
#: the right tree.
_DEFAULT_SCOPE: tuple[str, ...] = _GENERAL_CALLER_DIRS + _GENERAL_CALLER_FILES


def _path_to_dotted(rel_path: str) -> str:
    """`contextplane/service/memory/promotion.py` -> `contextplane.service.memory.promotion`."""
    stem = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    return stem.replace("/", ".")


@dataclasses.dataclass(frozen=True)
class Rule:
    """One module from the quarantine list, and how its reachability
    is established: the general rule, or one of the two named exceptions."""

    module_path: str
    reason: str
    #: A single file outside the three general locations that counts as this
    #: module's production entry point. Empty for every module but the one
    #: named special case (`source_ingest.py`).
    extra_caller: str | None = None
    #: Set only for the other named special case (`contest.py`). See the
    #: module docstring for what this does and does not mean.
    transitive_via: frozenset[str] = frozenset()

    @property
    def dotted_module(self) -> str:
        return _path_to_dotted(self.module_path)


QUARANTINE: tuple[Rule, ...] = (
    Rule(
        module_path="contextplane/service/memory/promotion.py",
        reason="Promotion review REST surface (list/get/accept/reject/reverse) and the promotion-sweep job.",
    ),
    Rule(
        module_path="contextplane/service/memory/curation_queue.py",
        reason="The curation-queue REST surface and its MCP twin.",
    ),
    Rule(
        module_path="contextplane/service/memory/contest.py",
        reason=(
            "Exposes no route, tool, or job of its own. Reachable transitively: claim_writer.py's "
            "stage_claim calls detect_for_claim, and consolidation.py's persist step calls "
            "resolve_contests_for -- both call sites sit inside a write path, not behind a "
            "surface this module owns. Both intermediates are themselves imported directly by "
            "the curation router/MCP tools and by wiring/jobs.py, so the chain closes in one hop."
        ),
        transitive_via=frozenset(
            {
                "contextplane/service/memory/claim_writer.py",
                "contextplane/service/memory/consolidation.py",
            }
        ),
    ),
    Rule(
        module_path="contextplane/service/memory/confirmation.py",
        reason="Confirmation REST surface (:confirm/:adjudicate) and its MCP twin.",
    ),
    Rule(
        module_path="contextplane/service/memory/calibration.py",
        reason="Admin calibration routes (active mappings, :refit) and the calibration-refit job.",
    ),
    Rule(
        module_path="contextplane/service/memory/capability_requests.py",
        reason="Capability-requests REST surface and its MCP twin.",
    ),
    Rule(
        module_path="contextplane/service/memory/source_governance.py",
        reason="Admin source-governance routes (declare/policy/:reset-breaker) and the source-ingest job wiring.",
    ),
    Rule(
        module_path="contextplane/service/memory/source_ingest.py",
        reason=(
            "Constructed in wiring/jobs.py, but its production call site is the connector run "
            "loop: runner.py's _execute_sync partitions each artifact's parsed facts and routes "
            "the claim-shaped subset through this service, after connector.parse() succeeds and "
            "before the existing facts-table write."
        ),
        extra_caller="contextplane/ingest/runner.py",
    ),
)

# A rename that quietly drops an entry from this tuple is exactly the failure
# mode this gate exists to prevent one level up -- an exit criterion that
# governs zero modules is not a stricter gate, it is a disabled one.
assert QUARANTINE, "QUARANTINE is empty -- a rename emptied the phase's own exit gate"  # noqa: S101 - self-check on this gate script's own source-level constant at import time, not runtime validation of untrusted input


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CallSite:
    path: str
    line: int


@dataclasses.dataclass(frozen=True)
class Finding:
    rule: Rule
    #: Every satisfying caller site found, empty when the module is unreachable.
    callers: tuple[CallSite, ...]

    @property
    def reachable(self) -> bool:
        return bool(self.callers)


def _imported_dotted_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every dotted module name this file imports, with its line number.

    AST rather than a text search: a deferred `import contextplane.service.memory.x`
    inside a function body is still a dependency, and a regex would either miss
    it or also match the module name inside an unrelated string or comment.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


def _parse(path: Path) -> ast.AST | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        # Not this gate's job to report -- lint and typecheck both catch it.
        return None


def _import_line(path: Path, dotted_module: str) -> int | None:
    """The line number of the first import of *dotted_module* in *path*, if any."""
    tree = _parse(path)
    if tree is None:
        return None
    for module, lineno in _imported_dotted_names(tree):
        if module == dotted_module:
            return lineno
    return None


def resolve_targets(scope: list[str]) -> list[Path]:
    """Every .py file under *scope*, excluding tests/ and noise directories."""
    out: list[Path] = []
    for entry in scope:
        target = (_REPO_ROOT / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix == ".py":
                out.append(target)
            continue
        for path in sorted(target.rglob("*.py")):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            out.append(path)
    return out


def _is_excluded_caller(rel: str, rule_module_path: str) -> bool:
    """True for the module's own file, or anything under a tests/ tree.

    Neither is a legitimate production caller: a module cannot satisfy the
    gate by importing itself, and every quarantined service's own integration
    tests already construct it directly -- that is precisely the "tested but
    unreachable" gap this gate exists to close, not evidence of closing it.
    """
    if rel == rule_module_path:
        return True
    parts = Path(rel).parts
    return "tests" in parts


def _direct_callers(rule: Rule, caller_files: list[Path]) -> tuple[CallSite, ...]:
    """Every general-location file that imports *rule*'s module."""
    found: list[CallSite] = []
    for path in caller_files:
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        if _is_excluded_caller(rel, rule.module_path):
            continue
        line = _import_line(path, rule.dotted_module)
        if line is not None:
            found.append(CallSite(path=rel, line=line))
    return tuple(found)


def _named_extra_caller(rule: Rule) -> CallSite | None:
    if rule.extra_caller is None:
        return None
    path = _REPO_ROOT / rule.extra_caller
    if not path.is_file():
        return None
    line = _import_line(path, rule.dotted_module)
    if line is None:
        return None
    return CallSite(path=rule.extra_caller, line=line)


def _is_directly_reachable(module_path: str, caller_files: list[Path]) -> bool:
    """Whether *module_path* (not necessarily a quarantined module itself)
    has its own general-location caller -- the half of the transitive check
    that keeps it from just pushing unreachability one hop further."""
    probe = Rule(module_path=module_path, reason="")
    return bool(_direct_callers(probe, caller_files))


def _transitive_callers(rule: Rule, caller_files: list[Path]) -> tuple[CallSite, ...]:
    """For contest.py's shape only: an intermediate module that imports this
    module AND is itself reachable through the general rule, checked fresh
    each run rather than assumed from the comment naming it."""
    found: list[CallSite] = []
    for via_rel in sorted(rule.transitive_via):
        via_path = _REPO_ROOT / via_rel
        if not via_path.is_file():
            continue
        line = _import_line(via_path, rule.dotted_module)
        if line is None:
            continue
        if _is_directly_reachable(via_rel, caller_files):
            found.append(CallSite(path=via_rel, line=line))
    return tuple(found)


def evaluate(rule: Rule, caller_files: list[Path]) -> Finding:
    callers = _direct_callers(rule, caller_files)
    extra = _named_extra_caller(rule)
    if extra is not None:
        callers = callers + (extra,)
    if rule.transitive_via:
        callers = callers + _transitive_callers(rule, caller_files)
    return Finding(rule=rule, callers=callers)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_explain() -> int:
    print("memory-reachability gate: what it checks and how to clear a miss.\n")
    print("Each module below needs at least one import site under contextplane/api/routers/,")
    print("contextplane/api/mcp/tools/, or contextplane/wiring/jobs.py -- excluding tests/ and the")
    print("module's own file. Two named exceptions: source_ingest.py's caller is")
    print("contextplane/ingest/runner.py; contest.py is reachable only through claim_writer.py and")
    print("consolidation.py, both independently verified reachable themselves.\n")
    for rule in QUARANTINE:
        print(f"  {rule.module_path}")
        print(f"    {rule.reason}")
        if rule.extra_caller:
            print(f"    named exception -- extra_caller: {rule.extra_caller}")
        if rule.transitive_via:
            print(f"    named exception -- transitive_via: {sorted(rule.transitive_via)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify every Phase-2-quarantined memory service has a production caller.",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=list(_DEFAULT_SCOPE),
        help="Repo-relative general-caller locations to scan (default: the three general locations).",
    )
    parser.add_argument("--explain", action="store_true", help="Print the rule and the current quarantine list.")
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    if not QUARANTINE:
        # Belt-and-suspenders alongside the module-level assert above: the
        # assert protects every other importer of this module (e.g. its own
        # test suite monkeypatching QUARANTINE); this protects `main` being
        # invoked in a context where that assert was somehow bypassed.
        print("QUARANTINE is empty -- nothing was governed, which is a failure, not a pass.", file=sys.stderr)
        return 1

    missing = [entry for entry in args.paths if not (_REPO_ROOT / entry).exists()]
    if missing:
        print(
            f"scope does not exist under {_REPO_ROOT}: {', '.join(missing)}\n"
            "Nothing was checked, so this is a failure rather than a pass.",
            file=sys.stderr,
        )
        return 1

    caller_files = resolve_targets(args.paths)
    # Reachability is decided by who calls the governed modules. With no caller
    # files, every rule would resolve to "unreachable" or "clean" against an
    # empty universe rather than against the tree.
    require_nonempty(
        caller_files,
        "the caller scan population (paths: " + ", ".join(args.paths) + ")",
        allow_empty=args.paths != list(_DEFAULT_SCOPE),
    )

    missing_modules = [rule.module_path for rule in QUARANTINE if not (_REPO_ROOT / rule.module_path).is_file()]
    if missing_modules:
        print(
            "quarantine entry names a file that no longer exists (update the module_path or "
            "remove the entry from QUARANTINE in scripts/check_memory_reachability.py):",
            file=sys.stderr,
        )
        for m in missing_modules:
            print(f"  {m}", file=sys.stderr)
        return 1

    findings = [evaluate(rule, caller_files) for rule in QUARANTINE]
    unreachable = [f for f in findings if not f.reachable]

    for f in findings:
        if f.reachable:
            sites = ", ".join(f"{c.path}:{c.line}" for c in f.callers)
            print(f"{f.rule.module_path}: reachable ({sites})")
        else:
            print(f"{f.rule.module_path}: UNREACHABLE")

    if not unreachable:
        print(f"\nmemory-reachability gate: {len(QUARANTINE)} module(s) governed, all reachable")
        return 0

    print(
        f"\n{len(unreachable)} of {len(QUARANTINE)} quarantined module(s) have no production caller:",
        file=sys.stderr,
    )
    for f in unreachable:
        print(f"\n  {f.rule.module_path}", file=sys.stderr)
        print(f"    {f.rule.reason}", file=sys.stderr)
        print(
            "    needs an import site under contextplane/api/routers/, contextplane/api/mcp/tools/, "
            "or contextplane/wiring/jobs.py (or the named exception this rule declares).",
            file=sys.stderr,
        )
    print(
        "\nDo not weaken the rule to force a pass -- either wire the module to a real route, "
        "tool, or job, or the module still belongs in quarantine and this gate is telling "
        "the truth about it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
