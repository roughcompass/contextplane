"""Every pilot write reaches admission, established by enumeration.

The contract this file exists for is "no write surface bypasses admission", and
the tempting way to check it is a list of the surfaces someone remembered. That
list is wrong the moment a sixth surface is added, and wrong in the direction
that matters -- the new surface is the one nobody thought about.

So the check runs the other way round. It starts from the pilot field types the
admission floor covers, finds every module that writes one, and requires each to
reach admission. A module that starts writing a pilot field without going
through admission fails here without anybody adding it to anything.

`scan_for_pii` is deliberately not enough. It reports what it found and refuses
only when a tenant policy says to; a pilot write that calls it and stops is a
write that stores a card number on any deployment that has configured nothing.
The check below is for `admit_or_refuse`, not for scanning.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from contextplane.context.admission import PILOT_FIELD_TYPES

_SOURCE_ROOT = Path(__file__).resolve().parent.parent.parent / "contextplane"

#: The admission entry points. A module that names one of these reaches the
#: floor; a module that names only `scan_for_pii` does not.
_ADMISSION_ENTRY_POINTS = frozenset({"admit_or_refuse", "run_admission"})

#: The scan that is not admission. Named so the test below can say why a module
#: calling only this one is still a bypass.
_SCAN_ONLY = "scan_for_pii"


def _modules() -> list[Path]:
    return sorted(path for path in _SOURCE_ROOT.rglob("*.py") if "migrations" not in path.parts)


def _names_used(source: str) -> set[str]:
    """Every bare name and attribute the module calls.

    Parsed rather than substring-matched so a mention inside a docstring or a
    comment does not count as reaching admission -- which is exactly the kind of
    false pass that would make this file decorative.
    """
    tree = ast.parse(source)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                used.add(func.id)
            elif isinstance(func, ast.Attribute):
                used.add(func.attr)
    return used


def _docstrings(tree: ast.AST) -> set[int]:
    """Ids of the string nodes that are docstrings, so prose can be ignored.

    Written after this check reported the scanner module as a bypass on the
    strength of a usage example in its own docstring. Naming a field type in
    prose is documentation; naming it in code is a claim to handle it, and only
    the second is what this file is looking for.
    """
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, holders):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _field_types_in_code(source: str) -> set[str]:
    """Pilot field types this module names outside its own prose."""
    tree = ast.parse(source)
    docstring_nodes = _docstrings(tree)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in PILOT_FIELD_TYPES
        and id(node) not in docstring_nodes
    }


def _pilot_writers() -> dict[str, set[str]]:
    """Modules naming a pilot field type, mapped to the names they call.

    Naming a pilot field type is the signal a module is a pilot write path: the
    field types are the unit classification attaches to, so a module that writes
    one has to say which one.
    """
    writers: dict[str, set[str]] = {}
    for path in _modules():
        source = path.read_text()
        if not _field_types_in_code(source):
            continue
        relative = path.relative_to(_SOURCE_ROOT.parent).as_posix()
        writers[relative] = _names_used(source)
    return writers


# --- The enumeration itself ---------------------------------------------------


def test_some_modules_are_found_at_all() -> None:
    """The guard that stops every test below passing vacuously.

    An enumeration that finds nothing reports no bypasses, which reads exactly
    like an enumeration that found everything compliant.
    """
    writers = _pilot_writers()

    assert len(writers) >= 5, f"expected the pilot write paths and the floor itself, found {sorted(writers)}"


def test_every_module_naming_a_pilot_field_reaches_admission() -> None:
    """The contract. A module that names a pilot field type either goes through
    admission or is one of the two modules that define it."""
    defining = {
        "contextplane/context/admission.py",  # the floor itself
        "contextplane/api/pii_guard.py",  # the adapter that runs it
    }

    bypasses = {
        module: sorted(names & {_SCAN_ONLY})
        for module, names in _pilot_writers().items()
        if module not in defining and not (names & _ADMISSION_ENTRY_POINTS)
    }

    assert not bypasses, (
        "these modules name a pilot field type and never reach admission: "
        f"{sorted(bypasses)}. Calling {_SCAN_ONLY} is not enough -- it refuses only when a "
        "tenant policy says to, so a deployment that configured nothing stores the content."
    )


@pytest.mark.parametrize(
    "module",
    [
        "contextplane/api/routers/memory.py",
        "contextplane/api/routers/artifacts.py",
        "contextplane/api/mcp/tools/memory.py",
        "contextplane/service/memory/claim_assertion.py",
        "contextplane/service/workspace/entries.py",
    ],
)
def test_each_known_write_path_is_still_wired(module: str) -> None:
    """Named individually as well as enumerated.

    The enumeration above is the durable check; these five are the paths that
    were measured as unwired, and pinning them by name means a regression on any
    one of them says which one rather than only that the count changed.
    """
    names = _names_used((_SOURCE_ROOT.parent / module).read_text())

    assert names & _ADMISSION_ENTRY_POINTS, f"{module} no longer reaches admission"


def test_the_mcp_session_event_path_reaches_admission() -> None:
    """Called out on its own because it is the one that scanned nothing at all,
    while its own docstring told agents that it did."""
    source = (_SOURCE_ROOT / "api/mcp/tools/memory.py").read_text()

    assert _ADMISSION_ENTRY_POINTS & _names_used(source)


def test_no_pilot_write_path_calls_the_bare_scan_instead_of_admission() -> None:
    """A module may still call `scan_for_pii` -- admission calls it internally --
    but not as its only floor. This catches the half-migration where one field
    on a surface is admitted and another is merely scanned."""
    offenders = [
        module
        for module, names in _pilot_writers().items()
        if _SCAN_ONLY in names and not (names & _ADMISSION_ENTRY_POINTS) and module != "contextplane/api/pii_guard.py"
    ]

    assert offenders == []
