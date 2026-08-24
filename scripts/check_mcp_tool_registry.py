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

**The two budgets are ratchets, not targets** (E13-T4). A number in a document
drifts and is noticed a year later; a number that can only go down is one
somebody has to argue with to weaken. E13's stated purpose is that simplicity is
subtraction, and subtraction without a ratchet is a one-time cleanup that grows
back.

They are set at what is *achieved*, not at what was wished for. The core tier is
8 and its target was 8. The REST budget is **7 paths** and its target was 6 --
E13-T1 decided that 7 is the floor without dropping a capability, and recorded
why, so the ratchet holds the real number rather than an aspiration nothing can
satisfy.

**Paths, not operations**, and that distinction is the whole of E13-T1.
`record_session_event` and `list_session_events` are POST and GET on one path,
so the core tier is 8 operations over 7 paths. The metric measures what an
integrator has to learn, and a path with two methods is one thing to learn. An
operation ratchet would also pass while somebody added a third method to an
existing path, which is exactly the growth an integrator feels.

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
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checklib import repo_root, run_guard

_REGISTRY = Path("contextplane/api/mcp/tool_registry.json")
#: The most tools a default connection may expose. Achieved, not aspired to:
#: E7-T1 chose eight by a stated rule -- a verb is core if an agent needs it to
#: complete one turn of the two-call loop without reaching for a second surface.
#:
#: **Nine as of E22-T14, and the ninth is `declare_instruction_set`.** Raised as
#: a decision rather than to fit, and the argument is that the alternative is
#: incoherent: `registry_resolve_context` is core and now takes an
#: `instruction_digest`, and the only way to obtain a digest is this tool. Left
#: in `extended`, the default connection would document a parameter no agent on
#: that connection could satisfy -- the same "advertise something the caller
#: cannot reach" failure this repo has already had to fix once.
#:
#: It also passes E7-T1's own rule on its own terms: an agent that has declared
#: cannot complete a turn of the loop without it, because the digest it must
#: send does not exist until this call has been made.
_CORE_TOOL_CEILING = 9

#: The most distinct REST paths that core tier may span. Seven, which E13-T1
#: established is the floor without dropping a capability -- E13's own target of
#: six is unreachable, and the entry says why rather than leaving it unmet.
#:
#: **Eight as of E22-T14.** `/v1/context/instruction-sets` is a new path rather
#: than a method on an existing one, and that was the choice: submission is
#: idempotent-by-content and returns a name derived from the body, which is not
#: what `POST /v1/context/resolve` means. Folding it onto that path to keep this
#: number at seven would have bought the counter and cost the surface its
#: meaning, which is the trade this ratchet exists to make somebody argue for
#: rather than to prevent.
_CORE_PATH_CEILING = 8

_TOOLS_DIR = Path("contextplane/api/mcp/tools")

#: The tiers a tool may be in. `core` reaches a default connection; `extended`
#: requires an envelope that names it. A third tier is a deliberate change here,
#: not a value a branch invents.
_TIERS = frozenset({"core", "extended"})


#: The two `IntentKind` members a tool's act can correspond to. Two rather than
#: seven because the distinction that matters for autonomy is read versus write:
#: the other five name changes to code, dependencies, configuration, security
#: posture and deployments, none of which an MCP tool here performs.
_INTENT_KINDS: Final = frozenset({"read_only", "data_access"})

#: A tool whose name begins with one of these writes something. Derived rather
#: than listed per tool, so adding a verb does not mean remembering to classify
#: it -- and checked against the registry in both directions, so a name that
#: does not fit the rule has to be an exception somebody wrote a reason for.
_WRITE_PREFIXES: Final = (
    "add_",
    "adjudicate_",
    "append_",
    "arc_complete_",
    "arc_issue_",
    "assert_",
    "confirm_",
    "create_",
    "delete_",
    "discard_",
    "grant_",
    "ingest_",
    "link_",
    "open_",
    "raise_",
    "record_",
    "reverse_",
    "review_",
    "revoke_",
    "route_",
    "triage_",
    "update_",
)

#: Tools whose name does not fit the rule, each with the reason. The point of
#: having it is that filling it costs somebody a sentence.
_INTENT_KIND_EXCEPTIONS: Final[dict[str, tuple[str, str]]] = {
    "declare_instruction_set": (
        "data_access",
        "`declare_` is not a write prefix and this writes. The verb was chosen "
        "for what the caller is doing -- stating what it was told -- rather than "
        "for what the service does with it, and the service stores a row: the "
        "instruction set that was in force at every resolution declaring that "
        "digest. Adding `declare_` to the prefix list would be the wrong fix, "
        "because a future `declare_` tool that only reads back a declaration is "
        "a plausible verb and would then be mislabelled by the rule.",
    ),
}


def _expected_intent_kind(name: str) -> str:
    """What the naming rule says this tool's act is."""
    if name in _INTENT_KIND_EXCEPTIONS:
        return _INTENT_KIND_EXCEPTIONS[name][0]
    return "data_access" if name.startswith(_WRITE_PREFIXES) else "read_only"


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
        # The intent kind the envelope selects on when it decides whether this
        # principal sees this verb. Checked against the same rule that produced
        # it, so a tool added later cannot arrive unclassified -- an unclassified
        # extended tool would be listed to everybody or to nobody, and both are
        # decisions nobody made.
        declared = entry.get("intent_kind")
        if declared not in _INTENT_KINDS:
            problems.append(
                f"{name}: intent_kind is {declared!r}, expected one of {sorted(_INTENT_KINDS)}. "
                "It decides whether a principal's envelope lets them see this verb."
            )
        elif (expected := _expected_intent_kind(name)) != declared:
            problems.append(
                f"{name}: intent_kind is {declared!r} and the naming rule derives {expected!r}. "
                "A tool that writes is `data_access`; one that only reads is `read_only`. "
                "If this tool is the exception, add it to _INTENT_KIND_EXCEPTIONS with the reason."
            )

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

    core_entries = [e for e in listed if e.get("tier") == "core"]
    core = len(core_entries)
    # The REST surface an agent integration must learn: distinct *paths* the core
    # tier maps to, with the method dropped. See the module docstring.
    core_paths = {
        str(e.get("rest") or "").split(" ", 1)[-1].strip() for e in core_entries if str(e.get("rest") or "").strip()
    }
    if core > _CORE_TOOL_CEILING:
        problems.append(
            f"the core tier is {core} tools, above the ratchet of {_CORE_TOOL_CEILING}. A default "
            "connection is what an agent meets first; widening it is a decision, not a side effect "
            "of adding a verb. Lower the ratchet when the count drops -- never raise it to fit."
        )
    if len(core_paths) > _CORE_PATH_CEILING:
        problems.append(
            f"the core tier spans {len(core_paths)} REST paths, above the ratchet of "
            f"{_CORE_PATH_CEILING}: {sorted(core_paths)}. That number is what an integrator has to "
            "learn. Adding a method to a path already in the set costs nothing here, which is "
            "deliberate; adding a path does."
        )
    print(
        f"mcp-tool-registry gate: {len(registered)} registered, {len(by_name)} listed, "
        f"{core} core over {len(core_paths)} REST path(s)"
    )

    if problems:
        for line in problems:
            print(line, file=sys.stderr)
        print(f"\nEdit {_REGISTRY}. Every registered tool is listed with a tier.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guard(main))
