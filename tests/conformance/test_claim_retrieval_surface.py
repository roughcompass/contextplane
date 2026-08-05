"""Semantic claim retrieval has a surface, and the surface says what it returns.

This file exists because of a specific failure. The claim serving layer was built, and
nothing called it: no route, no tool, no scheduler. The MCP reference even named a
`search_claims` tool that did not exist, and a conformance test pinned that promise
against nothing. A mechanism nobody can call is indistinguishable from one that is not
there, so the surface is now asserted rather than assumed.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from registry.api.mcp.server import create_registry_mcp_server


def _tools() -> dict[str, object]:
    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
    )
    return {t.name: t for t in server._tool_manager.list_tools()}


def test_the_claim_surface_offers_all_three_reads() -> None:
    """Structural, by id, and semantic. Each answers a question the others cannot.

    `query_claims` needs the caller to name a subject or predicate; `get_claim` needs an
    id; `search_claims` takes prose. Without the third, an agent that does not already
    know what to ask for has no way in -- which is the case the semantic index was built
    for.
    """
    registered = _tools()
    for name in ("query_claims", "get_claim", "search_claims"):
        assert name in registered, f"the agent surface is missing {name}"


def test_the_documented_tool_actually_exists() -> None:
    """The reference named `search_claims` before anything implemented it.

    A doc-only promise is worse than an omission: an agent reads the reference to decide
    what to call, so a named tool that is absent sends it down a path that cannot work.
    """
    from pathlib import Path

    reference = Path(__file__).resolve().parents[2] / "docs" / "05-reference" / "02-mcp-tools.md"
    body = reference.read_text(encoding="utf-8")
    assert "## search_claims" in body, "the tool exists but the reference does not describe it"
    assert "search_claims" in _tools()


def test_the_semantic_tool_warns_that_recall_is_untrusted() -> None:
    """The label has to be in the description, not only in the payload.

    An agent reads the description when deciding whether to call and how much weight to
    give the answer. A caveat that only appears in the response arrives after that
    decision has been made.
    """
    tool = _tools()["search_claims"]
    doc = inspect.getdoc(tool.fn) or ""  # type: ignore[attr-defined]
    assert "untrusted" in doc.lower()
    assert "not an instruction" in doc.lower()


def test_the_semantic_tool_takes_a_persona() -> None:
    """Depth is a retrieval choice, so the ranked surface has to offer it too.

    A semantic surface without persona would make depth available on the structural path
    and not the one an agent reaches for when it does not know the predicate.
    """
    tool = _tools()["search_claims"]
    params = inspect.signature(tool.fn).parameters  # type: ignore[attr-defined]
    assert "persona" in params
    assert "top_k" in params
