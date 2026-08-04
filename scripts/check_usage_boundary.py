"""Gate: usage data is non-authoritative, and nothing may decide anything from it.

Usage rows record who called what. They are deliberately lossy — buffered, dropped
under pressure, and deleted on a retention boundary — and every one of those
properties is fine for answering "is anyone using this" and disqualifying for
answering anything else. The audit log is the record of what happened; usage is a
measurement of traffic.

So the risk is not that someone reads the wrong table. It is that a service reads
usage counts and decides with them: gating a deprecation on a call count that
expired, or answering "who accessed this capability" from rows that were dropped
when a queue filled. Both look like working code and produce a confident wrong
answer. `docs/` says usage is not evidence; this makes the code say it.

Three rules, each in the same shape:

1. **Importing `registry.usage` requires being declared here, with a reason.** An
   inverted allowlist rather than a list of forbidden packages: a forbidden-list
   gate silently permits every package nobody has thought of, which is all of the
   future ones. Adding an importer means writing down why that module needs usage
   and what it does with it.

2. **SQL against the usage tables belongs to `registry/usage/`.** Recording, rolling
   up, erasing, and expiring are the writes; the reads are aggregate. A query
   elsewhere is how a service starts treating usage as a source of truth without
   importing anything.

3. **`registry/usage/` may not import the service or ARC layers.** The other
   direction of the same concern: if usage code cannot reach the decision layer,
   usage cannot become part of a decision. It also keeps the recording path off
   business logic, which matters because that path runs on every request.

Run locally:

    python3 scripts/check_usage_boundary.py
    python3 scripts/check_usage_boundary.py --explain
    python3 scripts/check_usage_boundary.py --paths registry/service
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import re
import sys
from pathlib import Path

# Relative to this repo's root, resolved from this file. Deliberately not the
# workspace above it: a default scope that only resolves when the checkout happens
# to be named `registry` silently scans nothing in a git worktree, which is how a
# gate reports success without having run.
_REPO_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_SCOPE: tuple[str, ...] = ("registry", "sync", "scripts")

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git"}
)

#: The package that owns usage, expressed as a path prefix.
_USAGE_PACKAGE = "registry/usage"

#: Migrations create and alter these tables by definition, and the alembic runner
#: decides when. Excluded from the SQL rule, not from the import rule.
_MIGRATIONS = "registry/storage/migrations"

_BYPASS_MARKER = "# usage-boundary: intentional"


@dataclasses.dataclass(frozen=True)
class Importer:
    """One module permitted to import `registry.usage`, and why."""

    path: str
    reason: str


#: Every module allowed to touch usage, with what it is allowed to do with it.
#:
#: Note what is *absent* and must stay absent: `registry/service/`, `registry/arc/`,
#: and every router other than the aggregate read surface. A service that imported
#: this could gate a decision on a number that expires.
ALLOWED_IMPORTERS: tuple[Importer, ...] = (
    Importer(
        path="registry/main.py",
        reason=(
            "Composition root. Constructs the writer, registers the erasure participant, "
            "and schedules the rollup and expiry workers. Wires the subsystem; reads no "
            "number from it and decides nothing."
        ),
    ),
    Importer(
        path="registry/api/middleware/tenant.py",
        reason=(
            "Stashes the resolved identity on the request so the outcome seam can attach "
            "it later. This is the only place that knows who is calling. Write path only."
        ),
    ),
    Importer(
        path="registry/api/middleware/metrics.py",
        reason=(
            "Emits the REST usage event. The one point that knows the route template and "
            "the outcome together, which is why recording happens here rather than where "
            "identity is resolved. Write path only."
        ),
    ),
    Importer(
        path="registry/api/routers/mcp.py",
        reason=(
            "Stashes MCP identity and emits the MCP usage event from the tool wrapper. "
            "The MCP equivalent of the two middleware entries above. Write path only."
        ),
    ),
    Importer(
        path="registry/api/routers/admin_usage.py",
        reason=(
            "The aggregate read surface, and the only reader. Serves counts to a person "
            "in a console. No service consumes these responses, which is what keeps the "
            "data out of any decision."
        ),
    ),
    Importer(
        path="registry/api/routers/usage.py",
        reason=(
            "The producer-facing owner projection. Reads aggregates scoped by ownership "
            "and returns them to the publisher; like the operator surface, no service "
            "consumes the response, so nothing decides from it."
        ),
    ),
    Importer(
        path="registry/workers/usage_rollup.py",
        reason="Schedules the daily rollup. Aggregates usage into itself and reads nothing out.",
    ),
)

#: Files permitted to write SQL against the usage tables from outside the package.
#: Retention is a worker by the same pattern as every other expiry sweep here, and
#: moving its statement into `registry/usage/` would split the sweep from the
#: batching and logging it shares with its siblings.
ALLOWED_SQL_OUTSIDE_PACKAGE: frozenset[str] = frozenset({"registry/workers/usage_expiry.py"})

#: Table names the SQL rule protects. `usage_rollup_%` is matched by prefix, and the
#: `\b` after the name is what keeps `usage_events_archive` — a different table the
#: rule says nothing about — from matching.
_TABLE_RE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|TABLE|JOIN)\s+(usage_events|usage_rollup_\w+)\b",
    re.IGNORECASE,
)

#: A statement keyword, required *in the same string literal* as the table name.
#:
#: Without it the table pattern fires on English: the sentence "reads nothing from
#: usage_events" contains `from usage_events` and is a promise not to do the thing
#: the gate forbids. A gate that flags the comment explaining why the rule is
#: followed is a gate someone switches off, so it has to want both halves.
_STATEMENT_RE = re.compile(
    r"\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|TRUNCATE)\b",
    re.IGNORECASE,
)

#: Layers `registry/usage` may not import, because reaching them is how a
#: measurement becomes a decision.
_FORBIDDEN_FROM_USAGE = ("registry.service", "registry.arc")


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    line_no: int
    rule: str
    detail: str
    guidance: str


def _imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    """Every dotted module name this file imports, with its line number.

    An AST walk rather than a line regex: `from registry.usage.writer import X` and
    a deferred `import registry.usage.reads` inside a function look nothing alike to
    a regex and identical here. A deferred import is still a dependency.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


def check_file(path: Path) -> list[Violation]:
    """Every boundary violation in one file."""
    rel = path.relative_to(_REPO_ROOT).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Not this gate's job to report; lint and typecheck both will.
        return []

    in_package = rel.startswith(f"{_USAGE_PACKAGE}/")
    declared = {i.path for i in ALLOWED_IMPORTERS}
    found: list[Violation] = []

    def bypassed(line_no: int) -> bool:
        return 0 < line_no <= len(lines) and _BYPASS_MARKER in lines[line_no - 1]

    for module, line_no in _imported_modules(tree):
        if bypassed(line_no):
            continue

        # Rule 1 — importing usage requires a declaration. The dot matters:
        # a bare prefix match also claims `registry.usagelike`, a module this rule
        # says nothing about, and false positives are what get gates switched off.
        imports_usage = module == "registry.usage" or module.startswith("registry.usage.")
        if imports_usage and not in_package and rel not in declared:
            found.append(
                Violation(
                    path=rel,
                    line_no=line_no,
                    rule="undeclared-usage-importer",
                    detail=f"imports {module}",
                    guidance=(
                        "Usage data is non-authoritative: buffered, dropped under pressure, "
                        "and deleted on a retention boundary. Nothing may decide anything "
                        "from it. If this module genuinely needs it, add an Importer entry "
                        "to ALLOWED_IMPORTERS in registry/scripts/check_usage_boundary.py "
                        "stating why and what it does with the data. If it needs a record of "
                        "what happened rather than a measurement of traffic, that is the "
                        "audit log."
                    ),
                )
            )

        # Rule 3 — usage may not reach the decision layers.
        if in_package and any(module == f or module.startswith(f + ".") for f in _FORBIDDEN_FROM_USAGE):
            found.append(
                Violation(
                    path=rel,
                    line_no=line_no,
                    rule="usage-imports-decision-layer",
                    detail=f"imports {module}",
                    guidance=(
                        "The usage package must not reach the service or ARC layers. That is "
                        "the other direction of the same rule: code that cannot reach a "
                        "decision cannot become part of one. It also keeps the recording "
                        "path — which runs on every single request — off business logic."
                    ),
                )
            )

    # Rule 2 — SQL against the usage tables belongs to the package.
    #
    # String literals only, via the AST, and each literal must carry a statement
    # keyword as well as the table name. Scanning raw lines flags the comment that
    # explains why a module does *not* read these tables, because "reads nothing from
    # usage_events" contains `from usage_events`. That is the false positive that
    # gets a gate disabled rather than obeyed.
    sql_allowed = in_package or rel in ALLOWED_SQL_OUTSIDE_PACKAGE or rel.startswith(f"{_MIGRATIONS}/")
    if not sql_allowed:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if bypassed(node.lineno) or bypassed(node.end_lineno or node.lineno):
                continue
            match = _TABLE_RE.search(node.value)
            if match and _STATEMENT_RE.search(node.value):
                found.append(
                    Violation(
                        path=rel,
                        line_no=node.lineno,
                        rule="usage-sql-outside-package",
                        detail=f"queries {match.group(1)}",
                        guidance=(
                            "Reading these tables directly is how a service starts treating "
                            "usage as a source of truth without importing anything. Recording, "
                            "rolling up, erasing, and expiring live in registry/usage/; the "
                            "reads there are aggregate on purpose."
                        ),
                    )
                )

    return found


def resolve_targets(scope: list[str]) -> list[Path]:
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
            if path.is_file() and not any(part in _EXCLUDE_DIRS for part in path.parts):
                out.append(path)
    return out


def _print_explain() -> int:
    print("Usage-boundary rules and what to do if you hit one:\n")
    print("  undeclared-usage-importer")
    print("    Any module importing registry.usage must be declared in ALLOWED_IMPORTERS,")
    print("    with a reason. An inverted allowlist, because a forbidden-list gate permits")
    print("    every package nobody has thought of yet.\n")
    print("  usage-sql-outside-package")
    print("    SQL against usage_events / usage_rollup_* belongs to registry/usage/.")
    print(f"    Permitted elsewhere: {sorted(ALLOWED_SQL_OUTSIDE_PACKAGE)} and migrations.\n")
    print("  usage-imports-decision-layer")
    print(f"    registry/usage/ may not import: {list(_FORBIDDEN_FROM_USAGE)}\n")
    print(f"Lines carrying '{_BYPASS_MARKER}' are exempt.\n")
    print("Currently declared importers:")
    for importer in ALLOWED_IMPORTERS:
        print(f"  {importer.path}\n    {importer.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify usage data stays non-authoritative.")
    parser.add_argument("--paths", nargs="+", default=list(_DEFAULT_SCOPE), help="Repo-relative paths to scan.")
    parser.add_argument("--explain", action="store_true", help="Print each rule and its guidance.")
    args = parser.parse_args(argv)

    if args.explain:
        return _print_explain()

    targets = resolve_targets(args.paths)
    if not targets:
        # A caller's typo'd --paths is reported and survivable; a default scope that
        # resolves to nothing means this gate governed no file and still said so.
        if args.paths != list(_DEFAULT_SCOPE):
            print("no .py files in scope (paths: " + ", ".join(args.paths) + ")", file=sys.stderr)
            return 0
        print(f"the default scope resolved to no files under {_REPO_ROOT}", file=sys.stderr)
        return 1

    # A declared importer that no longer imports usage is stale, and a stale entry
    # is a standing permission nobody is thinking about.
    stale = sorted(
        importer.path
        for importer in ALLOWED_IMPORTERS
        if not (_REPO_ROOT / importer.path).exists()
        or "registry.usage" not in (_REPO_ROOT / importer.path).read_text(encoding="utf-8")
    )

    violations = [v for path in targets for v in check_file(path)]
    if not violations and not stale:
        print(
            f"usage-boundary gate: {len(targets)} file(s) scanned, "
            f"{len(ALLOWED_IMPORTERS)} declared importer(s)"
        )
        return 0

    for v in violations:
        print(f"{v.path}:{v.line_no}: {v.rule}: {v.detail}")
    for path in stale:
        print(f"{path}: stale-declaration: declared as a usage importer but no longer imports it")

    seen: set[str] = set()
    for v in violations:
        if v.rule not in seen:
            seen.add(v.rule)
            print(f"\n{v.rule}: {v.guidance}", file=sys.stderr)
    if stale:
        print(
            "\nstale-declaration: remove the entry from ALLOWED_IMPORTERS. A permission "
            "nobody needs is one nobody is thinking about, and it will be there the day "
            "someone does need it for the wrong reason.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
