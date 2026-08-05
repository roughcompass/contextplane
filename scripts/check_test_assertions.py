#!/usr/bin/env python3
"""Lint gate: every test_* function has to actually prove something.

A test whose body runs code and never checks the result passes for the same
reason a `pass` statement would: nothing in it can fail. That shape is easy
to write by accident -- delete the last two lines while trimming a test, or
copy a "happy path" test and forget to add the check the copy was supposed to
add -- and once it exists, it stays green forever, silently promising
coverage it does not have. Auth-critical code is exactly where this is worst:
a token-acceptance test that never looks at what got accepted, or a
signature-verification test that never confirms the signature was actually
checked, both read as passing regression coverage right up until the
regression they were meant to catch.

A `test_*` function or method counts as proven when its own body contains,
directly or through a same-file helper it calls, at least one of:

  1. an `assert` statement;
  2. a call to anything named or attributed `assert*` -- this already covers
     `mock.assert_called_once_with(...)`, `unittest`-style
     `self.assertEqual(...)`, and any locally defined `assert_foo(...)`
     helper called by name, with no separate rule needed for any of them;
  3. a `with pytest.raises(...)` / `pytest.warns(...)` / `pytest.deprecated_call(...)`
     block.

"Through a same-file helper" means: the test calls a plain function defined
at module level in the same file, or -- if the test is itself a method on a
class -- a method on that same class reached via `self.` or `cls.`, and that
helper's own body satisfies one of the three signals above, or itself calls
a further same-file helper that does (checked transitively, never leaving
the file). This is what lets `_expect_malformed`-shaped helpers -- one
`with pytest.raises(...)` block, called from five thin test methods that
each supply different bad input -- count as five proven tests rather than
five vacuous ones. A call to anything outside the file (a fixture, an
imported utility, the function under test itself) does not count: the point
is not "did this test call something," it is "does this test, or something
whose source sits right here for a reviewer to read, check an outcome."

Detection is by AST, not by grepping for the word `assert` -- a bare-word
search cannot tell `self.assertEqual(...)` (an assertion) from a comment or
docstring that happens to contain the word, and cannot follow a call into a
helper defined three lines down in the same file.

The allowlist mechanism is a flat list of exceptions, each naming the file, the
function (class-qualified as `ClassName.test_method` when the test is a
method), and why the entry exists. An entry for a test that has since been
fixed, renamed away, or deleted is a standing permission nobody is using --
this gate treats that as its own failure (see `--explain`), the same way an
unused exemption rots any other allowlist-shaped gate in this repository.

Run locally:
    python scripts/check_test_assertions.py
    python scripts/check_test_assertions.py --explain
    python scripts/check_test_assertions.py --paths tests/unit
    python scripts/check_test_assertions.py --list
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Anchored at the repo root (this checkout), matching every sibling hygiene
# gate under scripts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The two test tiers this gate governs. `tests/conformance` and `tests/perf`
#: are deliberately not in scope: neither tier was part of the measurement
#: this gate's allowlist was seeded from, and both have their own, different
#: conventions (a conformance test's "assertion" is often a snapshot diff; a
#: perf test's is a latency threshold) that were not audited against this
#: criterion. A future sweep that wants either tier governed can pass
#: `--paths tests/conformance` explicitly -- the criterion itself does not
#: change, only what it is pointed at.
_DEFAULT_SCOPE: tuple[str, ...] = ("tests/unit", "tests/integration")

_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git"}
)

#: Names, on a `with` item's call, that make the block itself a proof --
#: whatever is inside is being checked for a raised exception, a warning, or
#: a deprecation notice, regardless of what else the block's body does.
_RAISES_LIKE_ATTRS: frozenset[str] = frozenset({"raises", "warns", "deprecated_call"})


#: One (relative_path, function_name, reason) triple per currently-known
#: vacuous test. `function_name` is the bare function name for a module-level
#: test, or `ClassName.test_method` for a test defined on a class -- the
#: qualification exists because two classes in one file can otherwise name a
#: method the same way, which a bare name could not disambiguate.
#:
#: An entry here is a debt marker, not a license: it exists so this gate can
#: ship without immediately failing on every test that predates it, while
#: still catching every *new* one. Removing an entry is only correct in the
#: same commit that adds the assertion the entry was standing in for --
#: removing it for any other reason reopens exactly the gap this gate exists
#: to close.
ALLOWLIST: tuple[tuple[str, str, str], ...] = ()


# ---------------------------------------------------------------------------
# AST detection
# ---------------------------------------------------------------------------


def _is_assert_like_call(node: ast.Call) -> bool:
    """True for `assert_foo(...)` and `x.assert_foo(...)` alike.

    Deliberately one rule, not two: a bare-name call covers a locally
    defined `assert_something` helper, and an attribute call covers both
    `unittest`-style `self.assertEqual(...)` and mock's
    `m.assert_called_once_with(...)` -- neither needs its own special case.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id.startswith("assert")
    if isinstance(func, ast.Attribute):
        return func.attr.startswith("assert")
    return False


def _is_raises_like_with(node: ast.With) -> bool:
    for item in node.items:
        call = item.context_expr
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in _RAISES_LIKE_ATTRS:
            return True
    return False


def _direct_proof_and_calls(node: ast.AST) -> tuple[bool, list[ast.Call]]:
    """Whether *node*'s own subtree contains a direct proof signal, plus
    every call found in it -- the candidates for helper resolution when it
    does not."""
    has_proof = False
    calls: list[ast.Call] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            has_proof = True
        elif isinstance(sub, ast.Call):
            calls.append(sub)
            if _is_assert_like_call(sub):
                has_proof = True
        elif isinstance(sub, ast.With) and _is_raises_like_with(sub):
            has_proof = True
    return has_proof, calls


@dataclasses.dataclass(frozen=True)
class _FileIndex:
    """One file's module-level functions and per-class methods, keyed by
    name -- the lookup table helper resolution walks against. Built once per
    file and reused across every test target in it."""

    module_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    classes: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]


def _build_index(tree: ast.Module) -> _FileIndex:
    module_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    classes: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            module_functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    methods[sub.name] = sub
            classes[node.name] = methods
    return _FileIndex(module_functions=module_functions, classes=classes)


def _resolve_helper(
    call: ast.Call, current_class: str | None, index: _FileIndex
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, str | None] | None:
    """The (node, class_context) a call resolves to, if it is a same-file
    helper this gate is willing to follow -- a bare-name call to a
    module-level function, or a `self.`/`cls.` call to a method on the
    class *currently being examined*. Anything else (an attribute call on
    some other object, a call to a name not defined in this file) is not a
    helper this gate can verify by reading the same file, so it does not
    count.
    """
    func = call.func
    if isinstance(func, ast.Name):
        target = index.module_functions.get(func.id)
        if target is not None:
            return target, None
        return None
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"}:
        if current_class is None:
            return None
        target = index.classes.get(current_class, {}).get(func.attr)
        if target is not None:
            return target, current_class
    return None


def _proves(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    current_class: str | None,
    index: _FileIndex,
    memo: dict[tuple[int, str | None], bool],
    visiting: set[tuple[int, str | None]],
) -> bool:
    """Whether *node* (a test function, or a helper reached from one) proves
    something -- directly, or by calling a same-file helper that does,
    followed transitively. `visiting` guards against a helper cycle (two
    helpers calling each other) resolving as true by infinite recursion;
    `memo` avoids re-walking a helper called from several tests in the same
    file."""
    key = (id(node), current_class)
    if key in memo:
        return memo[key]
    if key in visiting:
        # A cycle proves nothing on its own -- if it proved something, that
        # proof would have been found on the way in, before the cycle closed.
        return False
    visiting.add(key)
    try:
        has_proof, calls = _direct_proof_and_calls(node)
        if not has_proof:
            for call in calls:
                target = _resolve_helper(call, current_class, index)
                if target is None:
                    continue
                helper_node, helper_class = target
                if _proves(helper_node, helper_class, index, memo, visiting):
                    has_proof = True
                    break
    finally:
        visiting.discard(key)
    memo[key] = has_proof
    return has_proof


def _test_targets(
    tree: ast.Module,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, str | None]]:
    """Every `test_*` function or method in *tree*: (node, qualified_name,
    enclosing_class_name_or_None). Only top-level functions and top-level
    classes' direct methods count -- a `test_*`-named function nested inside
    another function is not something pytest collects, so it is not
    something this gate needs to judge either."""
    targets: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, str | None]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
            targets.append((node, node.name, None))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub.name.startswith("test_"):
                    targets.append((sub, f"{node.name}.{sub.name}", node.name))
    return targets


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    line: int
    function: str
    detail: str


def _parse(path: Path) -> ast.Module | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        # Not this gate's job -- lint and typecheck both catch a broken file.
        return None


def _raw_violations(path: Path, *, rel: str) -> list[Violation]:
    """Every assertion-less test_* in *path*, before the allowlist is applied."""
    tree = _parse(path)
    if tree is None:
        return []
    index = _build_index(tree)
    memo: dict[tuple[int, str | None], bool] = {}
    out: list[Violation] = []
    for node, qualified, current_class in _test_targets(tree):
        if not _proves(node, current_class, index, memo, set()):
            out.append(
                Violation(
                    path=rel,
                    line=node.lineno,
                    function=qualified,
                    detail=(
                        "no assertion, no pytest.raises/warns/deprecated_call, and no same-file "
                        "helper that has one -- this test cannot fail no matter what the code "
                        "under test does."
                    ),
                )
            )
    return out


def _matches_path(rel: str, entry_path: str) -> bool:
    posix = Path(rel).as_posix()
    return posix == entry_path or posix.endswith(f"/{entry_path}")


def _is_allowlisted(rel: str, function: str) -> bool:
    return any(
        _matches_path(rel, entry_path) and entry_function == function for entry_path, entry_function, _ in ALLOWLIST
    )


def check_file(path: Path, *, rel: str) -> list[Violation]:
    """Every un-allowlisted violation in *path*."""
    raw = _raw_violations(path, rel=rel)
    if not raw:
        return []
    return [v for v in raw if not _is_allowlisted(v.path, v.function)]


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


def _stale_allowlist_entries() -> list[str]:
    """An allowlist entry for a test that no longer exists, was renamed, or
    now proves something, is a permission nobody is using -- checked against
    each entry's own file directly, independent of whatever `--paths` scope
    the current run was given, the same way every sibling gate's stale-entry
    check does not let a narrowed `--paths` hide a rotten entry outside it.
    """
    stale: list[str] = []
    by_path: dict[str, list[tuple[str, str]]] = {}
    for entry_path, function, _reason in ALLOWLIST:
        by_path.setdefault(entry_path, []).append((entry_path, function))

    for entry_path, entries in by_path.items():
        candidate = _REPO_ROOT / entry_path
        if not candidate.is_file():
            for _, function in entries:
                stale.append(f"{entry_path}: {function!r} -- file no longer exists")
            continue
        raw = _raw_violations(candidate, rel=entry_path)
        raw_functions = {v.function for v in raw}
        for _, function in entries:
            if function not in raw_functions:
                stale.append(
                    f"{entry_path}: {function!r} no longer matches an assertion-less test "
                    "(fixed, renamed, or removed)"
                )
    return stale


def _duplicate_allowlist_entries() -> list[str]:
    """Two entries naming the same (path, function) is a sign the list was
    hand-edited into an inconsistent state -- worth failing on for the same
    reason a stale entry is: an allowlist nobody can read accurately is one
    nobody is actually enforcing."""
    seen: dict[tuple[str, str], int] = {}
    for entry_path, function, _reason in ALLOWLIST:
        seen[(entry_path, function)] = seen.get((entry_path, function), 0) + 1
    return [f"{path}: {function!r} appears {count} times" for (path, function), count in seen.items() if count > 1]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_explain() -> int:
    print("assertion-less-test gate: what it checks and how to clear a hit.\n")
    print("A test_* function or method is a violation unless its own body contains, directly")
    print("or through a same-file helper it calls (a module-level function, or -- for a test")
    print("that is itself a method -- a same-class method reached via self./cls., resolved")
    print("transitively without ever leaving the file), at least one of:\n")
    print("  1. an `assert` statement")
    print("  2. a call to anything named or attributed assert* (covers mock.assert_called_once_with(...),")
    print("     unittest-style self.assertEqual(...), and any locally defined assert_foo(...) helper)")
    print("  3. a `with pytest.raises(...)` / `pytest.warns(...)` / `pytest.deprecated_call(...)` block\n")
    print("To clear a hit:")
    print("  1. Add the missing assertion, or route the call through a helper that has one.")
    print(
        "  2. If this is a pre-existing gap being tracked for repair rather than fixed in this "
        "change, add a (path, function, reason) triple to ALLOWLIST in "
        "scripts/check_test_assertions.py -- function is class-qualified (ClassName.test_x) "
        "when the test is a method."
    )
    print(f"\nCurrently allowlisted ({len(ALLOWLIST)}):")
    for entry_path, function, reason in ALLOWLIST:
        print(f"  {entry_path} :: {function}")
        print(f"    {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify every test_* function asserts something, directly or through a same-file helper."
    )
    parser.add_argument("--paths", nargs="+", default=list(_DEFAULT_SCOPE), help="Repo-relative paths to scan.")
    parser.add_argument("--explain", action="store_true", help="Print the rule and the current allowlist.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every assertion-less test in scope (allowlisted or not), then exit 0. Diagnostic only.",
    )
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

    if args.list:
        raw: list[Violation] = []
        for path in targets:
            raw.extend(_raw_violations(path, rel=str(path.relative_to(_REPO_ROOT))))
        for v in sorted(raw, key=lambda v: (v.path, v.line)):
            print(f"{v.path}:{v.line}: {v.function}")
        return 0

    violations: list[Violation] = []
    for path in targets:
        violations.extend(check_file(path, rel=str(path.relative_to(_REPO_ROOT))))

    stale = _stale_allowlist_entries()
    duplicates = _duplicate_allowlist_entries()

    if not violations and not stale and not duplicates:
        entry_word = "entry" if len(ALLOWLIST) == 1 else "entries"
        print(f"assertion-less-test gate: {len(targets)} file(s) scanned, {len(ALLOWLIST)} allowlist {entry_word} held")
        return 0

    for v in violations:
        print(f"{v.path}:{v.line}: [{v.function}] {v.detail}")
    for s in stale:
        print(f"stale-allowlist-entry: {s}")
    for d in duplicates:
        print(f"duplicate-allowlist-entry: {d}")

    if violations:
        print(
            f"\n{len(violations)} assertion-less test(s) found. Run with --explain for the criterion "
            "and fix guidance, or --list to see every one in scope regardless of allowlist status.",
            file=sys.stderr,
        )
    if stale:
        print(
            "\nRemove the stale entry from ALLOWLIST in scripts/check_test_assertions.py -- "
            "an allowlist entry nobody needs is one nobody is thinking about.",
            file=sys.stderr,
        )
    if duplicates:
        print(
            "\nCollapse the duplicate entry in ALLOWLIST in scripts/check_test_assertions.py -- "
            "a (path, function) pair should appear at most once.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
