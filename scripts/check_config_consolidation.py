#!/usr/bin/env python3
"""Lint gate: every env-var read outside `Settings` is marked and registered.

`contextplane/config.py::Settings` is the one place the app's environment-to-field
mapping is supposed to live (see that module's own docstring). CLAUDE.md's
"Secrets and config" section states the rule that follows from that: a read of
`os.environ`/`os.getenv` anywhere else "triggers the consolidation gate and
must justify the bypass with a same-line `# config: intentional` marker."
Until this gate existed, that sentence named an enforcement mechanism that did
not exist -- nothing walked the tree checking it, so the marker was a comment
convention nobody could rely on staying true, and the doc's own bypass count
had already drifted (it named two bypasses; measurement found five more).

**Two things are checked, not one.** A marker alone is not enough:

1. **Unmarked.** An env-touching expression outside `Settings` with no
   `# config: intentional` on its own source line (or, for a multi-line
   expression, any line the expression spans) is a violation. Fix it by
   routing the value through a `Settings` field, or by adding the marker.
2. **Unregistered.** A marker with no matching entry in `ALLOWLIST` below is
   *also* a violation. The marker alone lets anyone bypass `Settings` and
   defend it with an unverifiable "the design forces it" comment; requiring a
   named, reasoned `ALLOWLIST` entry per file is what makes the bypass set
   small and auditable rather than a growing pile of unreviewed comments.

**What counts as an env-touching expression:** `os.environ.get(...)`,
`os.getenv(...)`, a subscript *read* (`os.environ[NAME]`), and a containment
check (`NAME in os.environ` / `NAME not in os.environ`). A subscript
*assignment* (`os.environ[NAME] = value`) or `os.environ.setdefault(...)` is
deliberately not matched -- writing a value into the process environment (so
a later read, by this process or a subprocess, sees it) is not "reading
config outside Settings," and every real instance of the presence-check +
default-write shape in this codebase already marks the *read* half, not the
write. Any other bare appearance of `os.environ` (`dict(os.environ)`,
`**os.environ`, `for k in os.environ`, `.copy()`, `.keys()`, ...) is caught by
a catch-all: reading the whole environment is the least targeted form of the
exact thing this rule exists to prevent.

**Scope.** `contextplane/` (recursively) and the top-level scripts directly under
`scripts/` -- not `scripts/devstack/` or `scripts/load_test/`. Those two
subtrees are local tooling that manages its *own* process environment (ports,
mock-server settings, a Postgres binary directory) to stand up dependencies
for a developer's machine; they are not the shipped app reading its own
configuration, and `scripts/check_doc_env_mentions.py`'s own `ALLOWLIST`
already carves out `CONTEXTPLANE_PG_BINDIR` on exactly this reasoning. Excluding
them here is a scope decision this module states rather than leaves
implicit -- see CLAUDE.md's "Secrets and config" section for the same
boundary in prose.

Run locally:
    python scripts/check_config_consolidation.py
    python scripts/check_config_consolidation.py --explain
    python scripts/check_config_consolidation.py --paths contextplane/api
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

_BYPASS_MARKER = "# config: intentional"


@dataclasses.dataclass(frozen=True)
class Exemption:
    """One file allowed to carry `# config: intentional` markers, and why
    every env-touching site in it cannot go through `Settings`."""

    path: str
    reason: str


#: Every file currently holding a registered bypass. A new marked site needs
#: an entry here naming the same property every existing one does: a concrete
#: reason `Settings` cannot cover it, not "this one is fine."
ALLOWLIST: tuple[Exemption, ...] = (
    Exemption(
        path="scripts/run_integration_lifecycle_comparison.py",
        reason=(
            "Manages the process environment it hands to child processes rather than reading "
            "its own configuration. It rejects and then scrubs the whole GIT_* namespace before "
            "any git call, because GIT_DIR alone can make `git -C <path>` answer about another "
            "repository and certify measured evidence against a tree nobody ran; enumerating the "
            "inherited environment is the only way to scrub it. It also pins CONTEXTPLANE_TEST_PG "
            "and PYTHONPATH for the pytest child so the tree being measured is the one under "
            "test. Same role as scripts/devstack/: local tooling plumbing an environment, not the "
            "shipped app reading configuration Settings should own."
        ),
    ),
    Exemption(
        path="scripts/run_integration_tests.py",
        reason=(
            "The ambient environment is this runner's subject, not its configuration. It refuses "
            "an invocation carrying PYTEST, PYTHON, any PYTEST_*/GIT_* channel, or a Make-level "
            "override, and it can only find those by enumerating what it inherited -- a Settings "
            "field would describe the values it is supposed to reject. It then builds the sealed "
            "child environment allowlist-first from that same inherited map, so the interpreter "
            "and provider the measured suite runs under are the ones this process chose. Settings "
            "is the shipped app's configuration and is never constructed here."
        ),
    ),
    Exemption(
        path="scripts/verify_integration_lifecycle_comparison.py",
        reason=(
            "Same GIT_* reject-then-scrub as the controller it verifies, for the same reason: a "
            "verifier that honoured an inherited GIT_DIR would confirm a comparison against a "
            "different repository than the one named on its command line. It reads no "
            "configuration of its own."
        ),
    ),
    Exemption(
        path="scripts/run_workspace_evaluation.py",
        reason=(
            "Reads the evaluation signing key from the environment before it constructs "
            "Settings, and deliberately before it does anything else at all. The key decides "
            "whether a run may start: an unsigned result cannot be attributed to whoever took "
            "it, so the runner refuses rather than producing one, and that refusal has to "
            "happen ahead of the database connection Settings exists to describe. It is also "
            "not deployment configuration -- it is a per-invocation operator secret that must "
            "have no committed default, whereas a Settings field invites one and would carry "
            "the key into every process that constructs Settings for unrelated reasons."
        ),
    ),
    Exemption(
        path="contextplane/arc/service/drafter.py",
        reason=(
            "_sandbox_env() builds the *complete* environment handed to the two sandbox "
            "subprocesses, and reads PATH/HOME from the parent only so that everything else "
            "-- DATABASE_URL, OIDC secrets, admin tokens -- is deliberately left behind. "
            "These two are process-execution context rather than configuration: routing them "
            "through Settings would add two fields whose sole purpose is to be forwarded "
            "verbatim to a child process, and would not change one byte of what that child "
            "receives. The conformance suite already proves this exact pair is sufficient "
            "(the parser runs with only PATH and HOME plus a poisoned DATABASE_URL), and a "
            "unit test asserts the child's environment equals this allowlist exactly, so a "
            "future secret added to Settings cannot silently reach the sandbox."
        ),
    ),
    Exemption(
        path="contextplane/ingest/webhook.py",
        reason=(
            "Reads GITHUB_WEBHOOK_SECRET / GITLAB_WEBHOOK_SECRET directly on every delivery "
            "to support per-instance secret rotation without an app restart. Settings also "
            "carries these fields (webhook_secret_github/gitlab) for validation and "
            "documentation purposes, but Settings is a frozen snapshot taken once at startup; "
            "routing the live read through it would keep serving a rotated-out secret until "
            "the next restart, which is the failure mode this bypass exists to avoid."
        ),
    ),
    Exemption(
        path="contextplane/ingest/connector.py",
        reason=(
            "resolve_credential() reads a connector credential by a dynamic ref string an "
            "operator supplies per connector definition -- the set of names is not fixed at "
            "code-writing time, so it cannot be enumerated as Settings fields."
        ),
    ),
    Exemption(
        path="contextplane/api/middleware/http_methods.py",
        reason=(
            "get_mode_settings() reads CONTEXTPLANE_HTTP_METHODS_MODE / "
            "CONTEXTPLANE_HTTP_METHOD_ALIAS_SEPARATOR directly because routers register their "
            "routes at import time, before any Settings instance exists to read from. The "
            "module's own docstring names the resulting drift hazard (the defaults are "
            "duplicated in Settings and here) and accepts it rather than papering over it; "
            "moving route registration inside create_app(settings) would remove the need for "
            "this bypass entirely but is a larger change than a doc/gate task should make."
        ),
    ),
    Exemption(
        path="contextplane/storage/migrations/versions/0001_baseline_schema.py",
        reason=(
            "_embedding_vector_dim() and _embedding_hash_buckets() read EMBEDDING_DIM and "
            "EMBEDDINGS_PARTITION_COUNT directly at CREATE TABLE time. Both need integer and "
            "positivity validation with an error message actionable from a bare `alembic "
            "upgrade head` failure, which a generic Settings ValidationError does not give; "
            "EMBEDDINGS_PARTITION_COUNT additionally has no Settings field at all, because "
            "hash-partition fan-out is fixed at table creation and nothing at application "
            "runtime ever needs to know it afterwards."
        ),
    ),
    Exemption(
        path="scripts/bootstrap_dev_tenant.py",
        reason=(
            "Local-dev-only bootstrap script that never constructs Settings at all -- it "
            "opens its own SQLAlchemy engine and talks to the mock OIDC/entitlement services "
            "directly. The DATABASE_URL presence-check-and-default runs before Settings could "
            "exist regardless; the OIDC_DISCOVERY_URL / ENTITLEMENT_SERVICE_URL reads compute "
            "argparse defaults for --mock-oidc-url/--mock-entitlement-url, evaluated even "
            "earlier, before that same DATABASE_URL default has been applied."
        ),
    ),
    Exemption(
        path="scripts/seed.py",
        reason=(
            "Same DATABASE_URL presence-check-and-default shape as bootstrap_dev_tenant.py: "
            "if the variable is absent, a docker-compose-matching default is written into the "
            "process environment, and the very next line calls get_settings() normally, which "
            "picks it up like any operator-supplied value. The direct read only covers the "
            "presence check itself, not the DB URL Settings goes on to resolve and use."
        ),
    ),
    Exemption(
        path="scripts/prove_quickstart.py",
        reason=(
            "baseline_env() builds a deliberately minimal, sanitized subprocess environment for "
            "the clean-clone proof's child processes (git, docker, the cloned checkout's own "
            "tooling) -- forwarding only HOME/USER/TMPDIR/DOCKER_HOST from this process so that "
            "nothing ambient (an activated venv, a stray DATABASE_URL) leaks into a run that "
            "exists to prove the docs work with none of that. Same role as scripts/devstack/: "
            "process-environment plumbing for a child process, not the app reading its own "
            "configuration through Settings."
        ),
    ),
)

# scripts/export_openapi.py writes CONTEXTPLANE_HTTP_METHODS_MODE into the process
# environment before importing the app (so the import-time route registration
# in http_methods.py sees the value it pins), but never *reads* configuration
# itself -- there is nothing here for Settings to own, and no ALLOWLIST entry
# to register. See that script's own comment for why the write exists.


def _allowlisted_paths() -> frozenset[str]:
    return frozenset(e.path for e in ALLOWLIST)


# ---------------------------------------------------------------------------
# AST detection
# ---------------------------------------------------------------------------


def _is_os_environ(node: ast.AST) -> bool:
    """True for exactly the expression `os.environ`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_os_getenv_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getenv"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    )


def _is_environ_get_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _is_os_environ(node.func.value)
    )


def _is_environ_setdefault_call(node: ast.AST) -> bool:
    """`os.environ.setdefault(...)` -- a conditional *write*, not a read; see
    the module docstring's "What counts" section for why this is excluded."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setdefault"
        and _is_os_environ(node.func.value)
    )


@dataclasses.dataclass(frozen=True)
class Site:
    """One env-touching expression found in one file, before the marker and
    ALLOWLIST are consulted."""

    lineno: int
    end_lineno: int
    detail: str


class _EnvironAccessVisitor(ast.NodeVisitor):
    """Walks one module, recording every env-touching expression and which
    `os.environ` Attribute nodes it already accounted for -- the remainder,
    found by a second pass, are the whole-environment catch-all.

    Each recorded `Site` carries the *innermost enclosing statement's* line
    range, not just the triggering expression's own -- a marker placed on the
    statement that wraps a multi-line call (`raw_sep = (  # config: intentional`
    on one line, the actual `os.environ.get(...)` on the next) is the real,
    existing shape in this codebase, and anchoring only to the expression's
    own span would flag already-marked code as unmarked.
    """

    def __init__(self) -> None:
        self.sites: list[Site] = []
        self._consumed: set[int] = set()
        self._stmt_stack: list[tuple[int, int]] = []

    def _range(self, node: ast.AST) -> tuple[int, int]:
        if self._stmt_stack:
            return self._stmt_stack[-1]
        end = getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]
        return (node.lineno, end)  # type: ignore[attr-defined]

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.stmt):
            end = node.end_lineno or node.lineno
            self._stmt_stack.append((node.lineno, end))
            super().generic_visit(node)
            self._stmt_stack.pop()
        else:
            super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        lineno, end_lineno = self._range(node)
        if _is_os_getenv_call(node):
            self.sites.append(Site(lineno, end_lineno, "os.getenv(...) call"))
        elif _is_environ_get_call(node):
            self._consumed.add(id(node.func.value))  # type: ignore[attr-defined]
            self.sites.append(Site(lineno, end_lineno, "os.environ.get(...) call"))
        elif _is_environ_setdefault_call(node):
            self._consumed.add(id(node.func.value))  # type: ignore[attr-defined]
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_os_environ(node.value):
            self._consumed.add(id(node.value))
            if isinstance(node.ctx, ast.Load):
                lineno, end_lineno = self._range(node)
                self.sites.append(Site(lineno, end_lineno, "os.environ[...] read"))
            # ast.Store (an assignment target) is a write -- not matched, see docstring.
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        lineno, end_lineno = self._range(node)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, ast.In | ast.NotIn) and _is_os_environ(comparator):
                self._consumed.add(id(comparator))
                word = "in" if isinstance(op, ast.In) else "not in"
                self.sites.append(Site(lineno, end_lineno, f"NAME {word} os.environ check"))
        self.generic_visit(node)

    def finish(self, tree: ast.AST) -> list[Site]:
        """Second pass: any `os.environ` Attribute node not already
        consumed by a specific rule above is a whole-environment access
        (`dict(os.environ)`, `**os.environ`, `for k in os.environ`, ...)."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _is_os_environ(node) and id(node) not in self._consumed:
                self.sites.append(
                    Site(node.lineno, node.end_lineno or node.lineno, "whole-environment access (os.environ itself)")
                )
        return self.sites


def _raw_sites(source: str, path: Path) -> list[Site]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Not this gate's job -- lint and typecheck both catch a broken file.
        return []
    visitor = _EnvironAccessVisitor()
    visitor.visit(tree)
    return visitor.finish(tree)


def _marked_lines(source: str) -> set[int]:
    return {idx for idx, line in enumerate(source.splitlines(), start=1) if _BYPASS_MARKER in line}


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    line: int
    kind: str  # "unmarked" | "unregistered"
    detail: str


def check_file(path: Path, *, rel: str, allowlisted: frozenset[str]) -> list[Violation]:
    """Every violation in *path*: unmarked env-touching sites, plus marked
    sites in a file with no matching ALLOWLIST entry."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    sites = _raw_sites(source, path)
    if not sites:
        return []

    marked = _marked_lines(source)
    out: list[Violation] = []
    any_marked = False
    for site in sites:
        site_lines = range(site.lineno, site.end_lineno + 1)
        if any(ln in marked for ln in site_lines):
            any_marked = True
            continue
        out.append(
            Violation(
                path=rel,
                line=site.lineno,
                kind="unmarked",
                detail=(
                    f"{site.detail} outside Settings with no `{_BYPASS_MARKER}` marker. "
                    "Route the value through a Settings field, or add the marker and an "
                    "Exemption in scripts/check_config_consolidation.py naming why."
                ),
            )
        )

    if any_marked and rel not in allowlisted:
        first_marked_line = min(ln for ln in marked if any(ln in range(s.lineno, s.end_lineno + 1) for s in sites))
        out.append(
            Violation(
                path=rel,
                line=first_marked_line,
                kind="unregistered",
                detail=(
                    f"carries `{_BYPASS_MARKER}` but is not named in ALLOWLIST in "
                    "scripts/check_config_consolidation.py. Add an Exemption stating why this "
                    "bypass cannot use Settings."
                ),
            )
        )

    return out


def resolve_targets(scope: list[str]) -> list[Path]:
    """`contextplane/` recursively; `scripts/` non-recursively -- deliberately:
    see the module docstring's "Scope" section for why scripts/devstack/ and
    scripts/load_test/ are not walked."""
    out: list[Path] = []
    for entry in scope:
        target = (_REPO_ROOT / entry).resolve()
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix == ".py":
                out.append(target)
            continue
        rel = Path(entry).as_posix()
        globber = target.glob("*.py") if rel == "scripts" else target.rglob("*.py")
        for path in sorted(globber):
            if path.is_file() and not any(part in _EXCLUDE_DIRS for part in path.parts):
                out.append(path)
    return out


_DEFAULT_SCOPE: tuple[str, ...] = ("contextplane", "scripts")


def _stale_exemptions(targets: list[Path]) -> list[str]:
    """An ALLOWLIST entry naming a file that no longer exists, or that no
    longer holds any marked env-touching site, is a permission nobody needs."""
    by_rel = {str(p.relative_to(_REPO_ROOT)): p for p in targets}
    stale: list[str] = []
    for exemption in ALLOWLIST:
        candidate = _REPO_ROOT / exemption.path
        if not candidate.is_file():
            stale.append(f"{exemption.path}: file no longer exists")
            continue
        source = candidate.read_text(encoding="utf-8")
        sites = _raw_sites(source, candidate)
        marked = _marked_lines(source)
        still_marked = any(any(ln in marked for ln in range(s.lineno, s.end_lineno + 1)) for s in sites)
        if not still_marked:
            stale.append(f"{exemption.path}: no marked env-touching site remains")
        if exemption.path not in by_rel:
            stale.append(f"{exemption.path}: outside the scanned scope ({', '.join(_DEFAULT_SCOPE)})")
    return stale


def _print_explain() -> int:
    print("config-consolidation gate: what it checks and how to clear a hit.\n")
    print("Every os.environ.get(...)/os.getenv(...)/os.environ[...] read/`in os.environ`")
    print(f"check outside Settings needs `{_BYPASS_MARKER}` on its own line. A marked site")
    print("also needs its file named in ALLOWLIST below, with a reason.\n")
    print("To clear a hit:")
    print("  1. Prefer a Settings field -- get_settings().<field> -- over a direct read.")
    print("  2. If the read genuinely cannot go through Settings (see the reasons already on")
    print("     ALLOWLIST for the shapes this takes), add the marker and an Exemption.")
    print(f"\nCurrently registered ({len(ALLOWLIST)} file(s)):")
    for e in ALLOWLIST:
        print(f"  {e.path}")
        print(f"    {e.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify every env-var read outside Settings is marked and registered.")
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

    allowlisted = _allowlisted_paths()
    violations: list[Violation] = []
    for path in targets:
        violations.extend(check_file(path, rel=str(path.relative_to(_REPO_ROOT)), allowlisted=allowlisted))

    stale = _stale_exemptions(targets) if set(args.paths) == set(_DEFAULT_SCOPE) else []

    if not violations and not stale:
        print(f"config-consolidation gate: {len(targets)} file(s) scanned, {len(ALLOWLIST)} exemption(s) held")
        return 0

    for v in violations:
        print(f"{v.path}:{v.line}: [{v.kind}] {v.detail}")
    for s in stale:
        print(f"stale-exemption: {s}")

    if violations:
        print(
            f"\n{len(violations)} config-consolidation violation(s) found. Run with --explain for fix guidance.",
            file=sys.stderr,
        )
    if stale:
        print(
            "\nRemove the stale entry from ALLOWLIST in scripts/check_config_consolidation.py -- "
            "an exemption nobody needs is one nobody is thinking about.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
