"""Gate: the MCP tool registry and the registered tools agree, both directions.

`contextplane/api/mcp/tool_registry.json` names every tool the server exposes and
which surface tier it belongs to. It is a committed artifact rather than a list
computed at import, for the reason `ranking_registry.json` is: a generated list
agrees with the code by construction and therefore cannot catch the code being
wrong. The artifact is a claim; this check is what makes the claim falsifiable.

**Both directions, because they fail differently.** A tool registered and not
listed is a verb an agent can call that no reviewer decided the tier of -- it
reaches a default connection by default, which is the state E7 exists to end. A
tool listed and not registered is a promise the server does not keep, and the
registry is what E7-T2's parity gate and the docs will be generated from.

**Read from the source, not from a built server.** Building one needs a database
and a dozen services, and a check that could only run against a live app would not
run in the lint job. Each `tools/*.py` exposes a `register()` that calls
`_bind_tool(<function>, ...)` once per tool, so the registered set is a syntactic
fact about that function's body -- which is exactly the kind of fact a gate can
hold to.

Anti-vacuity: an empty registry, or a source tree that appears to register
nothing, fails rather than passing. A check that scans zero tools and prints a
tick is the failure mode every script in this directory is written against.

Run locally:

    python3 scripts/check_mcp_tool_registry.py
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checklib import repo_root, run_guard

_REGISTRY = Path("contextplane/api/mcp/tool_registry.json")
_TOOLS_DIR = Path("contextplane/api/mcp/tools")

#: The tiers a tool may be in. `core` reaches a default connection; `extended`
#: requires an envelope that names it. A third tier is a deliberate change here,
#: not a value a branch invents.
_TIERS = frozenset({"core", "extended"})


def _registered(source: str) -> set[str]:
    """The tools one module's `register()` binds onto the server.

    Matched on the `_bind_tool(<name>, ...)` call rather than on the module's
    function definitions: a module may define a helper coroutine that is not a
    tool, and it may define a tool it never registers. What reaches an agent is
    what `register()` binds.
    """
    found: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "register"):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr == "_bind_tool" and call.args:
                first = call.args[0]
                if isinstance(first, ast.Name):
                    found.add(first.id)
                elif isinstance(first, ast.Attribute):
                    found.add(first.attr)
    return found


def main() -> int:
    root = repo_root()
    document = json.loads((root / _REGISTRY).read_text(encoding="utf-8"))
    listed = document.get("tools")
    if not isinstance(listed, list) or not listed:
        print(f"{_REGISTRY.name}: `tools` is missing, not a list, or empty", file=sys.stderr)
        return 1

    problems: list[str] = []

    by_name: dict[str, dict[str, object]] = {}
    for entry in listed:
        name = str(entry.get("name", "<unnamed>"))
        if name in by_name:
            problems.append(f"{name}: listed twice")
        by_name[name] = entry
        if entry.get("tier") not in _TIERS:
            problems.append(f"{name}: tier is {entry.get('tier')!r}, expected one of {sorted(_TIERS)}")
        # A core tool is one a default connection exposes, so a reader has to be
        # able to find the REST operation it corresponds to without grepping.
        if entry.get("tier") == "core" and not str(entry.get("rest") or "").strip():
            problems.append(f"{name}: core tools name the REST operation they mirror")

    registered: dict[str, str] = {}
    for path in sorted((root / _TOOLS_DIR).glob("*.py")):
        if path.name == "__init__.py":
            continue
        for name in _registered(path.read_text(encoding="utf-8")):
            registered[name] = f"{_TOOLS_DIR}/{path.name}"

    if not registered:
        print(
            f"no tool registrations found under {_TOOLS_DIR}. Either the registration shape changed "
            "or this check is looking for a call that no longer exists -- both make a clean result "
            "here meaningless.",
            file=sys.stderr,
        )
        return 1

    for name, module in sorted(registered.items()):
        if name not in by_name:
            problems.append(
                f"{name}: registered in {module} and not in the registry. An unlisted tool reaches a "
                "default connection with nobody having decided its tier."
            )
        elif by_name[name].get("module") != module:
            problems.append(f"{name}: registry says {by_name[name].get('module')}, registered in {module}")

    for name in sorted(by_name):
        if name not in registered:
            problems.append(f"{name}: in the registry and registered nowhere -- a promise the server does not keep")

    core = sum(1 for e in listed if e.get("tier") == "core")
    print(f"mcp-tool-registry gate: {len(registered)} registered, {len(by_name)} listed, {core} core")

    if problems:
        for line in problems:
            print(line, file=sys.stderr)
        print(f"\nEdit {_REGISTRY}. Every registered tool is listed with a tier.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
