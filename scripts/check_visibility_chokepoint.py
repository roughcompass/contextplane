#!/usr/bin/env python3
"""Lint gate: a module that queries the entities table must import the chokepoint.

Cross-tenant isolation is a property of one function, not of any table or column:
`filter_entities()` and `assert_visible()` in `registry/service/governance/visibility.py`
are the only place that turns "does this row belong to a tenant" into "can this
caller see it." Everywhere else that reads `entities` and does its own ad hoc
comparison reimplements a fraction of that decision — usually just same-tenant
equality — and a reviewer has to notice the omission by eye, file by file, forever.

This gate makes the omission visible without banning it outright. A module that
references `entities` and does not import the chokepoint fails unless it is
named here, with a reason. Most reasons on the list say the same thing: the read
is already anchored to the caller's own tenant (a strict subset of what the
chokepoint enforces, so nothing broader ever escapes) or it inherits the
chokepoint through a sibling module in the same package rather than importing it
a second time. Neither is a loophole in the gate — both are decisions a reader
can check against the line the allowlist entry points at.

Two independent signals count as "references entities":

1. A string literal containing `FROM entities` or `JOIN entities` (read SQL).
2. Importing `Entity` from `registry.storage.models` (the ORM row).

Writes are deliberately out of scope. Creating your own tenant's entity is not a
visibility question; only reading rows that might belong to someone else is.

Run locally:
    python scripts/check_visibility_chokepoint.py
    python scripts/check_visibility_chokepoint.py --explain
    python scripts/check_visibility_chokepoint.py --paths registry/service
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Anchored at the repo root, not the workspace above it. A default scope that
# only resolves when the checkout happens to be named `registry` silently
# scans nothing in a git worktree — see check_usage_boundary.py, which hit
# this exact failure mode first.
_REPO_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_SCOPE: tuple[str, ...] = ("registry/service",)

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git"}
)

#: The chokepoint itself. Never flagged for failing to import itself.
_CHOKEPOINT_MODULE = "registry.service.governance.visibility"
_CHOKEPOINT_PATH = "registry/service/governance/visibility.py"

#: Read-only. `FROM entities` / `JOIN entities`, case-insensitive. INSERT/UPDATE
#: against `entities` is a write of the caller's own tenant's row, not a
#: visibility question, so it is deliberately not matched here.
_ENTITIES_READ_RE = re.compile(r"\b(?:FROM|JOIN)\s+entities\b", re.IGNORECASE)

_BYPASS_MARKER = "# visibility-chokepoint: intentional"


@dataclasses.dataclass(frozen=True)
class Exemption:
    """One module that references `entities` without importing the chokepoint, and why."""

    path: str
    reason: str


#: Every module currently exempted, with the property that makes the omission safe.
#: A new entry needs the same thing: point at the line that keeps the read inside
#: one tenant, or the sibling import that reaches the chokepoint for you.
ALLOWLIST: tuple[Exemption, ...] = (
    Exemption(
        path="registry/service/catalog/facts.py",
        reason=(
            "Every Entity read (session.get by primary key) is followed on the next line by "
            "an inline `entity.tenant_id != ctx.tenant_id` check, or delegates to "
            "EntityService._assert_tenant which does the same comparison. No row is ever "
            "returned to a caller outside the tenant that owns it — the chokepoint's "
            "cross-tenant grant logic (adoption, tenant-shared, public) is not needed because "
            "cross-tenant is exactly the case being rejected."
        ),
    ),
    Exemption(
        path="registry/service/catalog/lifecycle.py",
        reason=(
            "Same shape as facts.py: the one Entity read is followed immediately by "
            "`entity.tenant_id != ctx.tenant_id`, and the method returns early (a no-op) "
            "rather than serving the row when it fails. Nothing crosses a tenant boundary."
        ),
    ),
    Exemption(
        path="registry/service/catalog/external_ids.py",
        reason=(
            "The create path resolves an entity's tenant_id and raises TenantIsolationError "
            "on a mismatch before writing the mapping — an ownership guard, not a read that "
            "could return someone else's row. The lookup path joins entities only after "
            "filtering the mapping table by `m.tenant_id = :tid` (the caller's own tenant), so "
            "the join can never reach a row outside it."
        ),
    ),
    Exemption(
        path="registry/service/memory/capability_requests.py",
        reason=(
            "Resolves the owning tenant of one entity_id to route the request and raises "
            "when it is absent or inactive — the same 'absent and invisible are the same "
            "answer' rule the chokepoint itself uses, applied inline because the only "
            "question here is which tenant the request routes to, not which rows a reader "
            "may see."
        ),
    ),
    Exemption(
        path="registry/service/memory/promotion.py",
        reason=(
            "Resolves the edge destination's tenant_id and raises PromotionError when it "
            "differs from the proposal's owner tenant — rejecting a cross-tenant write, not "
            "serving a cross-tenant read. No entity row is returned to a caller."
        ),
    ),
    Exemption(
        path="registry/service/retrieval/listing.py",
        reason=(
            "Inherits _RetrievalState from registry.service.retrieval._query_primitives, the "
            "module that owns this package's chokepoint import (see retrieval/__init__.py's "
            "module docstring on the composition split). Its own SQL is unconditionally "
            "`WHERE e.tenant_id = :tid` — same-tenant listing, never cross-tenant, so "
            "_apply_visibility has nothing to filter."
        ),
    ),
    Exemption(
        path="registry/service/retrieval/search.py",
        reason=(
            "Inherits _RetrievalState from _query_primitives the same way listing.py does, "
            "and calls self._apply_visibility(...) — the chokepoint reached through the "
            "shared base class rather than imported a second time in this module."
        ),
    ),
    Exemption(
        path="registry/service/retrieval/graph_traversal.py",
        reason=(
            "Same composition as search.py: _RetrievalState supplies _apply_visibility, and "
            "this module calls it at every point member entities are resolved."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _imports_chokepoint(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _CHOKEPOINT_MODULE:
            return True
        if isinstance(node, ast.Import) and any(alias.name == _CHOKEPOINT_MODULE for alias in node.names):
            return True
    return False


def _imports_entity_orm_model(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "registry.storage.models"
            and any(alias.name == "Entity" for alias in node.names)
        ):
            return True
    return False


def references_entities(source: str, tree: ast.AST) -> bool:
    """True when this file has either signal that it reads the entities table."""
    if _imports_entity_orm_model(tree):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _ENTITIES_READ_RE.search(node.value):
            return True
    return False


def _is_bypassed(source: str) -> bool:
    return _BYPASS_MARKER in source


def _is_exempt(rel: str) -> bool:
    posix = Path(rel).as_posix()
    return any(posix == e.path or posix.endswith(f"/{e.path}") for e in ALLOWLIST)


def _is_chokepoint_itself(rel: str) -> bool:
    posix = Path(rel).as_posix()
    return posix == _CHOKEPOINT_PATH or posix.endswith(f"/{_CHOKEPOINT_PATH}")


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    detail: str


def check_file(path: Path, *, rel: str) -> Violation | None:
    """None when the file is fine; a Violation when it reads entities ungated."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Not this gate's job — lint and typecheck both catch a broken file.
        return None

    if _is_chokepoint_itself(rel):
        return None
    if not references_entities(source, tree):
        return None
    if _imports_chokepoint(tree):
        return None
    if _is_bypassed(source):
        return None
    if _is_exempt(rel):
        return None

    return Violation(
        path=rel,
        detail=(
            f"references the entities table but does not import {_CHOKEPOINT_MODULE}. "
            "Either call through filter_entities()/assert_visible(), or add an Exemption "
            "to ALLOWLIST in scripts/check_visibility_chokepoint.py naming the line that "
            "keeps the read inside one tenant."
        ),
    )


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
    print("visibility-chokepoint gate: what it checks and how to clear it.\n")
    print("A module fails when it references the entities table (a `FROM entities` /")
    print("`JOIN entities` string, or importing the `Entity` ORM model) without importing")
    print(f"{_CHOKEPOINT_MODULE}.\n")
    print("To clear a hit:")
    print("  1. Prefer calling filter_entities() or assert_visible() directly.")
    print(
        "  2. If the read is provably same-tenant-only (already filtered to ctx.tenant_id, "
        "or a write-side ownership check that rejects rather than serves a cross-tenant row), "
        "add an Exemption to ALLOWLIST stating which line makes it safe."
    )
    print(f"  3. A one-off false positive may carry `{_BYPASS_MARKER}` on its own line.\n")
    print(f"Currently exempted ({len(ALLOWLIST)}):")
    for e in ALLOWLIST:
        print(f"  {e.path}\n    {e.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify every entities-table reader imports the visibility chokepoint."
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

    targets = resolve_targets(args.paths)
    if not targets:
        print("no .py files in scope: " + ", ".join(args.paths), file=sys.stderr)
        return 0

    # A stale allowlist entry — a module that no longer exists, or exists but no
    # longer references entities at all — is a permission nobody is using and
    # nobody is checking. Silently keeping it is how an allowlist rots into a
    # list nobody can explain.
    stale: list[str] = []
    for exemption in ALLOWLIST:
        candidate = _REPO_ROOT / exemption.path
        if not candidate.is_file():
            stale.append(f"{exemption.path}: file no longer exists")
            continue
        source = candidate.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(candidate))
        except SyntaxError:
            continue
        if not references_entities(source, tree):
            stale.append(f"{exemption.path}: no longer references the entities table")

    violations = [v for path in targets if (v := check_file(path, rel=str(path.relative_to(_REPO_ROOT)))) is not None]

    if not violations and not stale:
        print(f"visibility-chokepoint gate: {len(targets)} file(s) scanned, {len(ALLOWLIST)} exemption(s) held")
        return 0

    for v in violations:
        print(f"{v.path}: {v.detail}")
    for s in stale:
        print(f"stale-exemption: {s}")

    if stale:
        print(
            "\nRemove the stale entry from ALLOWLIST in scripts/check_visibility_chokepoint.py — "
            "an exemption nobody needs is one nobody is thinking about.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
