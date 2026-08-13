"""Ownership changes never change authorization, and the code is inspected to prove it.

This is a negative property about the *whole* auth path, and no behavioural test
can establish it: exercising a request that happens not to consult ownership shows
nothing about the request that might. So this file reads the source of the
authorization surface and requires that it never mentions ownership at all.

The hazard is concrete. If any entitlement decision consulted an ownership
assignment, then "assign an owner" would become a privilege-escalation primitive:
anyone able to name themselves owner of a thing would thereby acquire whatever
owners are permitted to do, and the assignment surface is deliberately easier to
reach than the entitlement surface.

Both directions are checked, because either alone leaves the door open from the
other side — auth must not read ownership, and the ownership service must not
write entitlements.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent / "contextplane"

#: The node kinds that may carry a docstring, so one can be told from a value.
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

#: The modules that decide what a caller may do.
_AUTHORIZATION_MODULES = (
    _ROOT / "auth",
    _ROOT / "api" / "middleware",
    _ROOT / "service" / "governance",
)

#: Names that would mean an authorization decision had reached into ownership.
_OWNERSHIP_MARKERS = ("ownership_assignments", "contextplane.ownership", "OwnershipService")

#: Names that would mean the ownership service had reached into authorization.
_AUTHORIZATION_MARKERS = (
    "entitlement",
    "vocabulary_values",
    "grant_",
    "claim_resolver",
    "roles",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


@pytest.mark.parametrize("module_root", _AUTHORIZATION_MODULES, ids=lambda p: str(p.relative_to(_ROOT.parent)))
def test_no_authorization_module_reads_ownership(module_root: Path) -> None:
    """An entitlement decision that consulted ownership would be an escalation path.

    Read as text rather than by import graph, because a string table name reaches
    the database just as well as an import does — and a raw `SELECT ... FROM
    ownership_assignments` is exactly the shape an import-based check would miss.
    """
    offenders: list[str] = []
    for path in _python_files(module_root):
        source = path.read_text(encoding="utf-8")
        for marker in _OWNERSHIP_MARKERS:
            if marker in source:
                offenders.append(f"{path.relative_to(_ROOT.parent)}: {marker}")

    assert not offenders, (
        "these authorization modules reference ownership; ownership is accountability, not permission, "
        f"and joining them makes assigning an owner an escalation primitive: {offenders}"
    )


def _code_tokens(path: Path) -> set[str]:
    """Every identifier and non-docstring string literal in a module.

    Docstrings are excluded deliberately. This file's own subject matter is
    authorization, so the modules it guards discuss it at length in prose — a
    text scan flags those and reports the explanation of a rule as a violation of
    it. What matters is whether the *code* names authorization state.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # `clean=False`: the cleaned form differs from the raw node value, so the
    # default would never match and every docstring would read as a value.
    docstrings = {
        ast.get_docstring(node, clean=False) for node in ast.walk(tree) if isinstance(node, _DOCSTRING_OWNERS)
    }

    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.add(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr)
        elif isinstance(node, ast.alias):
            tokens.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings:
            tokens.add(node.value)
    return tokens


def test_the_ownership_service_writes_no_authorization_state() -> None:
    """The reverse direction: ownership must not grant anything.

    A service that could write an entitlement while recording an owner would
    achieve the same escalation from the other side, and the check above would not
    see it.
    """
    offenders: list[str] = []
    for path in _python_files(_ROOT / "ownership"):
        blob = " ".join(_code_tokens(path)).lower()
        for marker in _AUTHORIZATION_MARKERS:
            if marker in blob:
                offenders.append(f"{path.relative_to(_ROOT.parent)}: {marker}")

    assert not offenders, f"the ownership package references authorization state: {offenders}"


def test_the_ownership_router_exposes_no_permission_field() -> None:
    """No response from this surface may carry something a caller could read as a permission.

    A field named `permissions` or `can_*` on an ownership response would be read
    as authoritative by the first client that saw it, whatever the docstring said.
    """
    source = (_ROOT / "api" / "routers" / "ownership.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    suspicious: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name.startswith("can_") or name in {"permissions", "entitlements", "grants"}:
                suspicious.append(name)

    assert not suspicious, f"the ownership surface exposes permission-shaped fields: {suspicious}"


def test_the_ownership_surface_is_rest_only() -> None:
    """No MCP tool may assign or transition ownership.

    These are owner-authorized administrative actions. An agent that could
    validate an assignment could establish accountability for anything it could
    name, so the absence is a decision — asserted here so that adding a tool has
    to be a deliberate change to this test rather than a quiet addition.
    """
    tools_dir = _ROOT / "api" / "mcp" / "tools"

    assert not (tools_dir / "ownership.py").exists(), (
        "ownership is a REST-only administrative surface; adding an MCP tool for it is a decision to make "
        "explicitly, by changing this test and saying why"
    )

    offenders = [
        path.name for path in _python_files(tools_dir) if "ownership_assignments" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"these MCP tool modules touch ownership assignments: {offenders}"
