"""The tool registry, the tier filter, and what a default connection sees.

E7 opens with the observation that an agent connecting is handed every tool the
server has. These pin the two halves of the fix: the registry says which verbs
are core, and the filter means a default connection registers only those.

The gate in `scripts/check_mcp_tool_registry.py` holds the registry against what
the modules actually bind. What is here is the behaviour that depends on it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from contextplane.api.mcp.server import (
    SURFACE_CORE,
    SURFACE_FULL,
    core_tool_names,
    install_surface_filter,
)

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_mcp_tool_registry import _registered, main  # noqa: E402

_REGISTRY = Path(__file__).resolve().parents[2] / "contextplane" / "api" / "mcp" / "tool_registry.json"


class _FakeServer:
    """Enough of FastMCP to observe which tools a filter lets through."""

    def __init__(self) -> None:
        self.registered: list[str] = []

        def tool(*_args: object, **_kwargs: object) -> Any:
            def decorate(fn: Any) -> Any:
                self.registered.append(fn.__name__)
                return fn

            return decorate

        self.tool = tool


def _named(name: str) -> Any:
    async def fn() -> None:  # pragma: no cover - never called
        return None

    fn.__name__ = name
    return fn


def test_the_core_set_is_small_and_comes_from_the_artifact() -> None:
    """Eight, not sixty-seven. The number is the point of the epic's clause.

    Read from the committed registry rather than a constant here, so this test
    fails if the artifact quietly grows rather than agreeing with whatever it
    now says.
    """
    core = core_tool_names()

    document = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    assert core == frozenset(e["name"] for e in document["tools"] if e["tier"] == "core")
    assert 6 <= len(core) <= 8, (
        f"the core set is {len(core)}; E7 asks for six to eight verbs on a default connection, "
        "and a set that drifts past that is the surface this epic exists to bound"
    )
    assert len(document["tools"]) > len(core), "a registry where everything is core bounds nothing"


def test_a_core_surface_registers_only_the_core_verbs() -> None:
    server = _FakeServer()
    install_surface_filter(server, surface=SURFACE_CORE)  # type: ignore[arg-type]

    for name in ("whoami", "adjudicate_claim", "record_session_event", "get_blast_radius"):
        server.tool()(_named(name))

    assert server.registered == ["whoami", "record_session_event"]


def test_a_full_surface_registers_everything() -> None:
    """The control. A filter that dropped everything would pass the test above."""
    server = _FakeServer()
    install_surface_filter(server, surface=SURFACE_FULL)  # type: ignore[arg-type]

    for name in ("whoami", "adjudicate_claim"):
        server.tool()(_named(name))

    assert server.registered == ["whoami", "adjudicate_claim"]


def test_a_dropped_tool_is_not_registered_rather_than_registered_and_refused() -> None:
    """Listing a verb and then refusing it invites an agent to plan around it.

    Hiding one that still executes is obscurity. Not registering makes the list
    and the behaviour the same answer, which is the only combination that does
    not mislead. The decorator still returns the function, so the module's
    `register()` completes and a tool missing from the registry is caught by the
    gate rather than vanishing quietly here.
    """
    server = _FakeServer()
    install_surface_filter(server, surface=SURFACE_CORE)  # type: ignore[arg-type]
    fn = _named("adjudicate_claim")

    returned = server.tool()(fn)

    assert returned is fn
    assert server.registered == []


def test_the_registration_shape_the_gate_reads_is_the_one_in_use() -> None:
    """Anti-vacuity for the gate's parser.

    It finds tools by matching `_bind_tool(<name>, ...)` inside a module's
    `register()`. If that shape changed, the gate would report a clean tree
    because it found nothing -- so this asserts it finds something real in a
    module known to have several.
    """
    source = (Path(__file__).resolve().parents[2] / "contextplane" / "api" / "mcp" / "tools" / "catalog.py").read_text(
        encoding="utf-8"
    )

    found = _registered(source)

    assert {"whoami", "get_capability"} <= found


def test_the_committed_registry_agrees_with_the_code(capsys: pytest.CaptureFixture[str]) -> None:
    """End to end against the real tree, which the unit tests above cannot cover."""
    assert main() == 0
    assert "core" in capsys.readouterr().out
