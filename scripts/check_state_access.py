#!/usr/bin/env python3
"""Lint gate: services live on the typed container, not on raw app/request state.

`contextplane.api.container.Services` is the one typed source of truth for
every service `create_app` wires -- a frozen dataclass, one field per
service, assembled once in `lifespan` (see `contextplane/wiring/services.py`).
Before it existed, every constructed service hung off `app.state` under a
bare attribute name, and two anti-patterns grew around that:

1. `getattr(request.app.state, "some_service", None)` -- a stringly-typed
   read. `app.state` is Starlette's `State`, whose `__getattr__` is typed to
   return `Any` no matter what the string names, so a typo in the name, a
   service read before it is constructed, or a caller that assumes a service
   is always present all look identical to mypy: fine, until the `None`
   surfaces three call frames deep in a request handler.
2. `app.state.some_service = ...` -- a second, uncoordinated place a service
   gets attached, invisible to `Services` and therefore invisible to anyone
   reading the container to see what the app actually wires.

This gate flags both, everywhere except the places they are the designated
mechanism rather than a bypass of it:

- `contextplane/wiring/` -- where every service is constructed. The `getattr`
  rule is exempt here entirely: a wiring function is allowed to read
  something optional off `app.state` as it builds a service (e.g. the
  not-yet-real `pii_scanner` deployment hook the ARC/memory stage reads
  defensively).
  The *assign* rule is narrower: `app.state.<x> = ...` inside `contextplane/wiring/`
  is only exempt when `<x>` is one of the named keys in
  `_WIRING_ASSIGNABLE_KEYS` below. Every other service an area's `wiring.py`
  constructs flows into `Services` as a plain return value threaded through
  `contextplane.main.create_app` (see `CoreServices` / `PostAppServices` /
  `AuthContext` in `contextplane/wiring/stages.py`, `ArcServices` in
  `contextplane/arc/wiring.py`, and `RouteServices` in
  `contextplane/wiring/routes.py`) -- it never touches `app.state` at all, so a
  new service some future wiring function bolts onto `app.state` without
  adding it to `_WIRING_ASSIGNABLE_KEYS` (and naming its reader) is still
  caught here, the same as anywhere else in the codebase.
- `contextplane/main.py` -- the one place `app.state.services` itself is
  assigned, one call above `contextplane.wiring.services.build_services_container`.
- A short, named list of functions in specific files, each with a reason
  tied to a concrete failure mode below (`ALLOWLIST`). Two shapes recur:
    * `contextplane/api/mcp/context.py`'s `_services` helper -- the MCP
      transport threads `app` through a ContextVar rather than FastAPI's
      `Depends` machinery, so it needs the one seam that reaches from that
      `app` into its typed container. Every other accessor in that module
      calls `_services()` and then reads a field off the *container*, which
      this gate does not match at all (see "What is not flagged" below).
    * `contextplane/api/middleware/tenant.py` and
      `contextplane/api/middleware/idempotency.py` -- `settings`,
      `claim_resolver`, `oidc_cache`, and `session_factory` are read live
      and deliberately not through the container in these two files.
      `wire_auth_context` builds the auth trio inside `lifespan`, after the
      container is already assembled, and several test harnesses (and, in
      principle, an operator rotating a credential) replace
      `app.state.claim_resolver` on an already-running app. The container is
      a frozen snapshot taken once at startup; routing these reads through
      it would silently keep serving whatever existed at that instant. This
      was not a hypothetical when this gate was written -- routing
      `contextplane.api.mcp.context._resolve_tenant`'s identical trio through
      the container broke exactly this in `tests/conformance/test_mcp_conformance.py`
      before the read was reverted to `app.state` directly.

What is not flagged: a `getattr` call whose object is the *container*
itself, e.g. `getattr(services, "arc_preflight", None)` where `services`
came from `app.state.services`. The container is a plain object; walking it
by field name still type-checks the container's own construction, and nothing
about it models a service as silently possibly-absent the way raw `app.state`
does. Only the read *into* `app.state` is the anti-pattern this gate exists
to catch. `request.state.<x>` (no `.app` in the chain) is also not flagged --
that is Starlette's per-request scratch pad (e.g. `oidc_claims`, stashed by
middleware for the lifetime of one request), a different and legitimate
mechanism unrelated to the service container.

Run locally:
    python scripts/check_state_access.py
    python scripts/check_state_access.py --explain
    python scripts/check_state_access.py --paths contextplane/api
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

# Anchored at the repo root (this checkout), not the workspace above it --
# matches check_visibility_chokepoint.py, the sibling this gate follows most
# closely.
_REPO_ROOT = repo_root()

_DEFAULT_SCOPE: tuple[str, ...] = ("contextplane",)

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git"}
)

#: Every file under here is exempt from the `getattr` rule entirely -- a
#: wiring function reading something optional off `app.state` as it builds a
#: service (e.g. the ARC/memory stage's defensive `pii_scanner` read) is construction,
#: not a bypass. The `assign` rule is narrower: see `_WIRING_ASSIGNABLE_KEYS`.
_WIRING_PREFIX = "contextplane/wiring/"

#: The only `app.state.<x> = ...` assignments `contextplane/wiring/` may make.
#: Every one of these has a reader outside `contextplane.wiring` that does not go
#: through the typed `Services` container -- a router that has not migrated,
#: a middleware that deliberately bypasses the container's frozen snapshot,
#: or a test harness that replaces the attribute on an already-running app --
#: and that reader is named beside the key below. The assignments themselves
#: live in `attach_core_services`, `attach_arc_state`, and `attach_auth_state`
#: in `contextplane/wiring/stages.py`, plus `register` in
#: `contextplane/wiring/routes.py`. Every other service an area's `wiring.py`
#: constructs flows into `Services` only as a plain return value
#: (`CoreServices` / `PostAppServices` / `ArcServices` / `AuthContext` /
#: `RouteServices`) -- it has no reason to ever touch `app.state`, so a new
#: key appearing here without a named reader, or an `app.state.<x> = ...`
#: for a key *not* in this set, is still a violation this gate catches.
_WIRING_ASSIGNABLE_KEYS: frozenset[str] = frozenset(
    {
        # attach_core_services -- pre-`app` services with a real reader
        # outside the typed container. Who reads each one live:
        #   api.middleware.tenant, api.middleware.idempotency, api.mcp.context,
        #   and unit tests that build a bare FastAPI() app with no container
        "settings",
        "session_factory",
        #   usage.recording._writer -- durability/overhead tests install a
        #   different writer on an already-running app
        "usage_writer",
        #   api.routers.admin_lifecycle, api.routers.admin_extraction
        "clock",
        #   an integration test drives promotion off app.state on an app whose
        #   lifespan never ran; the attaching stage runs in create_app's body
        "lifecycle",
        #   api.routers._common, retrieval, admin_sync; ingest.webhook
        "catalog",
        #   api.routers.retrieval, api.routers.graph
        "retrieval",
        #   api.routers.external_ids
        "external_ids",
        #   ingest.webhook, api.routers.admin_sync
        "scheduler",
        #   api.routers.capabilities
        "visibility",
        #   api.routers.adoptions
        "adoption",
        #   api.routers.graph
        "projections",
        #   api.routers.subscriptions
        "subscriptions",
        #   api.routers.notifications
        "notifications",
        #   api.routers.breaking_change
        "breaking_change",
        #   api.routers.integrations
        "integrations",
        #   api.routers.interface
        "interface_storage",
        #   api.routers.capabilities
        "includes",
        # attach_arc_state -- the ARC fields contextplane.api.routers.arc (or
        # a test exercising an app whose lifespan never ran) still reads live.
        "arc_signing",
        "arc_clock",
        "arc_challenges",
        "arc_jit",
        "arc_receipt_reader",
        "arc_preflight",
        # attach_auth_state -- the two auth-trio members middleware and test
        # harnesses read or replace live. `entitlement_client` is not here:
        # it flows to `contextplane.main`'s lifespan as a return value only.
        "oidc_cache",
        "claim_resolver",
        # routes.register -- the workspace singleton and the erasure registry.
        "workspace_service",
        "erasure",
    }
)

_BYPASS_MARKER = "# state-access: intentional"


@dataclasses.dataclass(frozen=True)
class Exemption:
    """One file, and which of its functions may read `app`/`request` state
    directly -- for `getattr` reads (rule "getattr") or a direct assignment
    (rule "assign") -- and why.

    `functions` is never empty: an allowlist entry names the functions it
    covers rather than exempting a whole file, so a *new* function added
    later to an exempted file is not silently covered by someone else's
    reason.
    """

    path: str
    rule: str  # "getattr" | "assign"
    functions: frozenset[str]
    reason: str


#: Every exemption currently held, each naming the property that makes the
#: bypass safe. A new entry needs the same thing: point at what makes this
#: read/write immune to the anti-pattern the gate exists to catch, not just
#: "this one is fine."
ALLOWLIST: tuple[Exemption, ...] = (
    Exemption(
        path="contextplane/main.py",
        rule="assign",
        functions=frozenset({"lifespan"}),
        reason=(
            "The one place `app.state.services` itself is assigned -- inside `create_app`'s "
            "`lifespan`, one call above `contextplane.wiring.services.build_services_container`, "
            "which is where every other field has to already be readable off `app.state` by "
            "name. Mirrors the wiring/ exemption one layer up the composition root."
        ),
    ),
    Exemption(
        path="contextplane/api/mcp/context.py",
        rule="getattr",
        functions=frozenset({"_services", "_resolve_tenant"}),
        reason=(
            "`_services` is the one seam that reaches from a ContextVar-carried `app` into "
            "its typed container -- the MCP transport threads `app` through a ContextVar "
            "rather than FastAPI's Depends machinery, so nothing hands this module a request "
            "to read the container off. Every other accessor in this module calls "
            "`_services()` and then reads a named field off the *container* it returns, which "
            "this gate does not match at all (see the module docstring's 'What is not "
            "flagged'). `_resolve_tenant` reads `settings`, `claim_resolver`, and `oidc_cache` "
            "live and deliberately not through `_services()` -- see the reason on the "
            "`tenant.py` entry below; the same failure mode broke this exact function when it "
            "was tried, in tests/conformance/test_mcp_conformance.py, before the read was "
            "reverted."
        ),
    ),
    Exemption(
        path="contextplane/api/middleware/tenant.py",
        rule="getattr",
        functions=frozenset({"_resolve_entitlements", "get_tenant_context", "get_authenticated_context"}),
        reason=(
            "`settings`, `claim_resolver`, and `oidc_cache` are read live, deliberately not "
            "through the container: `wire_auth_context` builds this trio inside `lifespan`, "
            "after the container has already been assembled, and several test harnesses (and, "
            "in principle, an operator rotating a credential) replace `app.state.claim_resolver` "
            "on an already-running app. Routing this read through the container -- a frozen "
            "snapshot taken once at startup -- would silently keep serving whatever resolver "
            "existed at that instant. Proven, not hypothetical: doing exactly this to the "
            "identical trio in `contextplane.api.mcp.context._resolve_tenant` broke "
            "tests/conformance/test_mcp_conformance.py before the read was reverted here."
        ),
    ),
    Exemption(
        path="contextplane/api/middleware/idempotency.py",
        rule="getattr",
        functions=frozenset({"get_idempotency_context"}),
        reason=(
            "`session_factory` is read defensively ahead of a header check that decides "
            "whether the rest of the function runs at all -- the sibling branch two lines "
            "below already reads the same attribute directly (no getattr), and several unit "
            "tests build a bare FastAPI() app that sets `app.state.session_factory` directly "
            "without ever constructing `app.state.services`."
        ),
    ),
    Exemption(
        path="contextplane/usage/recording.py",
        rule="getattr",
        functions=frozenset({"_writer"}),
        reason=(
            "`usage_writer` is read live, deliberately not through the container: several "
            "durability/overhead tests install a different `UsageWriter` instance onto an "
            "already-running app after startup (e.g. tests/integration/test_usage_durability.py, "
            "tests/perf/test_usage_overhead.py) to exercise a crippled or differently-tuned "
            "writer. A frozen container snapshot taken at startup would not see the swap."
        ),
    ),
)


# ---------------------------------------------------------------------------
# AST detection
# ---------------------------------------------------------------------------


def _is_getattr_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"


def _is_bare_request_state(node: ast.AST) -> bool:
    """True for exactly `request.state` (not `request.app.state`).

    Starlette's per-request scratch pad -- middleware stashes things like
    `oidc_claims` there for the lifetime of one request. A different,
    legitimate mechanism from the app-wide service container this gate
    protects, so it is deliberately not in scope.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "state"
        and isinstance(node.value, ast.Name)
        and node.value.id == "request"
    )


def _is_bare_request_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "request"


def _is_state_expr(node: ast.AST) -> bool:
    """True when *node* evaluates to `app.state` (however it is spelled).

    Two shapes count: plain attribute access ending in `.state` --
    `app.state`, `request.app.state`, `app_ref.state`, ... -- and a
    `getattr(..., "state", ...)` call standing in for that same access,
    which is exactly the pattern one layer of this gate exists to catch.
    Bare `request.state` (no `.app` in the chain) is excluded either way --
    see `_is_bare_request_state`.
    """
    if isinstance(node, ast.Attribute) and node.attr == "state" and not _is_bare_request_state(node):
        return True
    if _is_getattr_call(node):
        call = node
        assert isinstance(call, ast.Call)  # noqa: S101 - narrows a real, already-enforced invariant: _is_getattr_call() above only returns True when isinstance(node, ast.Call) already holds
        if call.args:
            key = call.args[1] if len(call.args) >= 2 else None
            obj = call.args[0]
            if isinstance(key, ast.Constant) and key.value == "state" and not _is_bare_request_name(obj):
                return True
    return False


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    line: int
    function: str | None
    rule: str  # "getattr" | "assign"
    detail: str
    #: The attribute name assigned, for rule "assign" only -- lets
    #: `check_file` clear a `contextplane/wiring/` hit against
    #: `_WIRING_ASSIGNABLE_KEYS` without re-parsing `detail`.
    key: str | None = None


class _StateAccessVisitor(ast.NodeVisitor):
    """Walks one module, recording every getattr-on-state read and every
    direct `app.state.<x> = ...` assignment, tagged with the innermost
    function it occurred in (so the allowlist can be scoped per-function)."""

    def __init__(self) -> None:
        self.violations: list[Violation] = []
        self._func_stack: list[str] = []

    def _current_function(self) -> str | None:
        return self._func_stack[-1] if self._func_stack else None

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_getattr_call(node) and node.args:
            obj = node.args[0]
            key = node.args[1] if len(node.args) >= 2 else None
            key_is_state = isinstance(key, ast.Constant) and key.value == "state" and not _is_bare_request_name(obj)
            if key_is_state or _is_state_expr(obj):
                key_desc = repr(key.value) if isinstance(key, ast.Constant) else "<dynamic>"
                self.violations.append(
                    Violation(
                        path="",
                        line=node.lineno,
                        function=self._current_function(),
                        rule="getattr",
                        detail=f"getattr(...) reads app/request state directly (key={key_desc})",
                    )
                )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "state"
                and not _is_bare_request_state(target.value)
            ):
                self.violations.append(
                    Violation(
                        path="",
                        line=node.lineno,
                        function=self._current_function(),
                        rule="assign",
                        detail=f"assigns app.state.{target.attr} directly",
                        key=target.attr,
                    )
                )
        self.generic_visit(node)


def _raw_violations(source: str, path: Path) -> list[Violation]:
    """Every violation in *source*, before the allowlist is applied."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Not this gate's job -- lint and typecheck both catch a broken file.
        return []
    visitor = _StateAccessVisitor()
    visitor.visit(tree)
    return visitor.violations


def _bypassed_lines(source: str) -> set[int]:
    return {idx for idx, line in enumerate(source.splitlines(), start=1) if _BYPASS_MARKER in line}


def _is_wiring_path(rel: str) -> bool:
    posix = Path(rel).as_posix()
    return posix.startswith(_WIRING_PREFIX) or f"/{_WIRING_PREFIX}" in posix


def _matching_exemptions(rel: str, rule: str) -> list[Exemption]:
    posix = Path(rel).as_posix()
    return [e for e in ALLOWLIST if e.rule == rule and (posix == e.path or posix.endswith(f"/{e.path}"))]


def check_file(path: Path, *, rel: str) -> list[Violation]:
    """Every un-exempted violation in *path*, each carrying its file path."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    raw = _raw_violations(source, path)
    if not raw:
        return []

    bypassed = _bypassed_lines(source)
    wiring = _is_wiring_path(rel)
    out: list[Violation] = []
    for v in raw:
        if v.line in bypassed:
            continue
        if wiring:
            # The getattr rule stays exempt everywhere in contextplane/wiring/ --
            # see _WIRING_PREFIX's own comment. The assign rule is not: only
            # an assignment to a name in _WIRING_ASSIGNABLE_KEYS clears here:
            # anything else falls through to the same named-exemption check
            # every other file gets, which -- absent an ALLOWLIST entry --
            # means it is still a violation.
            if v.rule == "getattr":
                continue
            if v.rule == "assign" and v.key in _WIRING_ASSIGNABLE_KEYS:
                continue
        exemptions = _matching_exemptions(rel, v.rule)
        if any(v.function is not None and v.function in e.functions for e in exemptions):
            continue
        out.append(dataclasses.replace(v, path=rel))
    return out


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


def _stale_exemptions() -> list[str]:
    """An allowlist entry naming a function that no longer exists, or no
    longer contains a raw violation, is a permission nobody is using."""
    stale: list[str] = []
    by_path: dict[str, list[Exemption]] = {}
    for e in ALLOWLIST:
        by_path.setdefault(e.path, []).append(e)

    for rel, exemptions in by_path.items():
        candidate = _REPO_ROOT / rel
        if not candidate.is_file():
            stale.append(f"{rel}: file no longer exists")
            continue
        source = candidate.read_text(encoding="utf-8")
        raw = _raw_violations(source, candidate)
        raw_by_rule_and_function: dict[str, set[str | None]] = {}
        for v in raw:
            raw_by_rule_and_function.setdefault(v.rule, set()).add(v.function)
        for e in exemptions:
            covered = raw_by_rule_and_function.get(e.rule, set())
            unused = [fn for fn in sorted(e.functions) if fn not in covered]
            for fn in unused:
                stale.append(f"{rel}: {e.rule} exemption for {fn!r} no longer matches any violation")
    return stale


def _stale_wiring_keys() -> list[str]:
    """A key in `_WIRING_ASSIGNABLE_KEYS` that no `contextplane/wiring/` file
    actually assigns is a standing permission nobody is using -- same
    principle as a stale `ALLOWLIST` entry, applied to the keyed exemption.

    No `contextplane/wiring/` directory at all (a scratch tree built by this
    gate's own unit tests, not a real checkout) means there is nothing to
    judge staleness against -- report none rather than flagging every key.
    """
    if not (_REPO_ROOT / "contextplane" / "wiring").is_dir():
        return []
    used: set[str] = set()
    for path in resolve_targets(["contextplane/wiring"]):
        rel = str(path.relative_to(_REPO_ROOT))
        if not _is_wiring_path(rel):
            continue
        source = path.read_text(encoding="utf-8")
        for v in _raw_violations(source, path):
            if v.rule == "assign" and v.key is not None:
                used.add(v.key)
    return sorted(key for key in _WIRING_ASSIGNABLE_KEYS if key not in used)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_explain() -> int:
    print("state-access gate: what it checks and how to clear it.\n")
    print("Two rules. Everywhere outside contextplane/wiring/, both apply in full,")
    print("except for the named functions in ALLOWLIST below (whichever rule each")
    print("entry names). Inside contextplane/wiring/, rule (a) is exempt everywhere and")
    print("rule (b) is exempt only for the keys in _WIRING_ASSIGNABLE_KEYS:\n")
    print("  (a) getattr(...) reads of app/request state directly, e.g.")
    print('      getattr(request.app.state, "some_service", None)')
    print("  (b) app.state.<x> = ... assignments, where <x> is not one of the")
    print("      _WIRING_ASSIGNABLE_KEYS when the file is under contextplane/wiring/\n")
    print("To clear a hit:")
    print("  1. Prefer request.app.state.services.<field> (the typed Services container).")
    print("  2. If the read genuinely cannot go through the container -- see the reasons")
    print("     already on ALLOWLIST for the shape this takes -- add an Exemption naming")
    print("     the specific function(s) and why.")
    print(f"  3. A one-off false positive may carry `{_BYPASS_MARKER}` on its own line.\n")
    print(f"Currently exempted ({len(ALLOWLIST)}):")
    for e in ALLOWLIST:
        print(f"  {e.path} [{e.rule}] {sorted(e.functions)}")
        print(f"    {e.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify services are read from the typed Services container, not raw app/request state."
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
    # An explicit narrow --paths that holds no matching file is a fair question
    # with the answer "nothing there". A *default* scope that resolves to
    # nothing means this gate governed no file, which is a failure, not a pass.
    require_nonempty(
        targets,
        "the .py scan population (paths: " + ", ".join(args.paths) + ")",
        allow_empty=args.paths != list(_DEFAULT_SCOPE),
    )
    if not targets:
        print("nothing to scan in " + ", ".join(args.paths), file=sys.stderr)
        return 0

    violations: list[Violation] = []
    for path in targets:
        violations.extend(check_file(path, rel=str(path.relative_to(_REPO_ROOT))))

    stale = _stale_exemptions()
    stale_keys = _stale_wiring_keys()

    if not violations and not stale and not stale_keys:
        print(f"state-access gate: {len(targets)} file(s) scanned, {len(ALLOWLIST)} exemption(s) held")
        return 0

    for v in violations:
        fn = v.function or "<module scope>"
        print(f"{v.path}:{v.line}: [{fn}] {v.detail}")
    for s in stale:
        print(f"stale-exemption: {s}")
    for key in stale_keys:
        print(f"stale-wiring-key: {key!r} in _WIRING_ASSIGNABLE_KEYS no longer matches any assignment")

    if violations:
        print(
            f"\n{len(violations)} state-access violation(s) found. Run with --explain for fix guidance.",
            file=sys.stderr,
        )
    if stale:
        print(
            "\nRemove the stale entry from ALLOWLIST in scripts/check_state_access.py -- "
            "an exemption nobody needs is one nobody is thinking about.",
            file=sys.stderr,
        )
    if stale_keys:
        print(
            "\nRemove the stale key from _WIRING_ASSIGNABLE_KEYS in scripts/check_state_access.py -- "
            "a key nobody assigns is a permission nobody is using.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
