"""A guard that governs one transport governs nothing.

E7-T3a. Every service here has two transports, and twice now a guard has been
built on the HTTP one and left off the other:

- The PII scan. `tools/memory.py` still carries the note: this path *"called
  `record_event` directly and scanned nothing, while this tool's own docstring
  told agents it did — so a tenant that configured blocking was bypassed here
  and told otherwise."* Fixed by moving `admit_or_refuse` below the transports.
- The autonomy envelope. `enforce_envelope` lived in `api/envelope_guard.py`,
  was called from exactly **one** route, and reached no tool at all. A tenant
  graduated to `enforcing` was governed on REST and permitted over MCP.

The second one is why this file exists. Fixing the instance is not enough — the
same mistake is available to the next act somebody governs, and the way it
presents is a guard that looks present because the route has it.

So the rule is checked over the *pair*: an act the envelope governs is governed
on both transports, and the registry below has to list every call site on each.
Adding a guard to one transport and not the other fails here, because the sweep
finds a call site the registry does not name.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Final

import pytest

from contextplane import arc
from contextplane.api import envelope_guard
from contextplane.api.mcp import context as mcp_context

_PACKAGE: Final = pathlib.Path(arc.__file__).resolve().parent.parent

#: Every act the envelope governs, and where it is enforced on each transport.
#:
#: A registry rather than a pair of greps, because the property is that the two
#: sides *correspond* — a route guarded with no tool beside it is exactly the
#: defect this file was written after, and a sweep of either side alone reads as
#: healthy while it is happening.
GOVERNED_ACTS: Final[dict[str, dict[str, str]]] = {
    "record a session event": {
        "rest": "api/routers/memory.py",
        "mcp": "api/mcp/tools/memory.py",
    },
}

#: The two adapter functions. A module calling one of these is enforcing the
#: envelope on its transport; a module calling neither is not.
_REST_GUARD: Final = "enforce_envelope"
_MCP_GUARD: Final = "enforce_envelope_for_tool"

#: Where each adapter is defined, so the definitions are not mistaken for uses.
_ADAPTER_MODULES: Final = frozenset(
    {"api/envelope_guard.py", "api/mcp/context.py", "arc/service/autonomy_enforcement.py"}
)


def _modules_calling(name: str) -> set[str]:
    """Every module under `contextplane/` that calls *name*, by relative path."""
    found: set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(_PACKAGE))
        if relative in _ADAPTER_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                found.add(relative)
    return found


def test_every_governed_act_is_governed_on_both_transports() -> None:
    """The pair, not the route.

    A route with the guard and no tool beside it is what happened, and it read
    as healthy from the REST side for as long as it lasted.
    """
    rest_sites = _modules_calling(_REST_GUARD)
    mcp_sites = _modules_calling(_MCP_GUARD)

    for act, sites in GOVERNED_ACTS.items():
        assert sites["rest"] in rest_sites, f"{act}: the REST transport no longer enforces the envelope"
        assert sites["mcp"] in mcp_sites, f"{act}: the MCP transport no longer enforces the envelope"


def test_no_transport_enforces_the_envelope_for_an_unregistered_act() -> None:
    """The half that catches the *next* one.

    Governing a new act on one transport and forgetting the other is the failure
    this file exists for, and it cannot be caught by looking at the act — the
    act is new. It can be caught by noticing a call site nobody registered.
    """
    registered_rest = {sites["rest"] for sites in GOVERNED_ACTS.values()}
    registered_mcp = {sites["mcp"] for sites in GOVERNED_ACTS.values()}

    unregistered_rest = _modules_calling(_REST_GUARD) - registered_rest
    unregistered_mcp = _modules_calling(_MCP_GUARD) - registered_mcp

    assert not unregistered_rest, (
        f"{sorted(unregistered_rest)} enforces the envelope and is not in GOVERNED_ACTS. "
        "Add the act with both of its transports — an act governed on one is governed on none."
    )
    assert not unregistered_mcp, (
        f"{sorted(unregistered_mcp)} enforces the envelope and is not in GOVERNED_ACTS. "
        "Add the act with both of its transports."
    )


def test_the_sweep_would_notice_a_call_it_was_written_to_notice() -> None:
    """Anti-vacuity. A walker that matched nothing would pass both checks above
    and would keep passing through the commit that matters."""
    assert _modules_calling(_REST_GUARD), "the REST sweep found no call sites at all"
    assert _modules_calling(_MCP_GUARD), "the MCP sweep found no call sites at all"
    assert not _modules_calling("a_function_no_module_calls")


def test_both_transports_speak_one_refusal_vocabulary() -> None:
    """The codes live in `contextplane.arc`, not in either adapter.

    A second copy would be a second vocabulary, and the transport that got the
    newer one would say a different thing about the same decision — which is
    worse than either saying nothing, because a client would believe both.
    """
    assert envelope_guard.REFUSAL_MESSAGE is arc.REFUSAL_MESSAGE
    assert set(arc.REFUSAL_CODES.values()) == {
        "envelope_absent",
        "envelope_suspended",
        "envelope_withdrawn",
        "envelope_excluded",
    }
    for module in (envelope_guard, mcp_context):
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
        assert (
            "envelope_absent" not in source
        ), f"{module.__name__} restates a refusal code. There is one vocabulary and it is arc's."


def test_a_refusal_names_the_verdict_and_not_the_envelope() -> None:
    """A caller that learned *why* it was outside its envelope could map the
    matrix governing it, one probe at a time. It learns the verdict, because the
    remedy differs — `envelope_absent` is somebody else's job and
    `envelope_excluded` is the agent doing something it should not."""
    message = arc.REFUSAL_MESSAGE
    for leak in ("rule", "matrix", "revision", "matched", "because"):
        assert leak not in message.lower(), f"the refusal message leaks {leak!r}"


@pytest.mark.parametrize("verdict", sorted(arc.REFUSAL_CODES))
def test_every_verdict_has_its_own_code(verdict: str) -> None:
    """Mapped rather than defaulted. A verdict that fell through to
    `envelope_excluded` would tell an operator their agent misbehaved when in
    fact nobody had granted it an envelope."""
    assert arc.REFUSAL_CODES[verdict] != "envelope_excluded" or verdict == "outside_envelope"
