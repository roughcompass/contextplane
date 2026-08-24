"""Which verbs a principal sees, and the two ways that must not be enforcement.

E7-T3. The tool registry has always said the extended tier *"requires an
autonomy envelope that names it"*. Nothing enforced it, and every connection saw
every registered tool.

The entry's own framing is that listing and calling are two decisions and both
must be made. E7-T3a made the call decision; this is the listing one. Neither
substitutes for the other, and the properties here are mostly about keeping that
true:

- **A filtered listing is not an authorization boundary.** FastMCP shares one
  `_tool_cache` across connections and refills it from whichever listing ran
  last, so a listing that were the only guard would be no guard at all.
- **The core tier is never filtered.** It is the floor a default connection
  exposes, and a principal that could not see those eight verbs could not open
  the loop at all.
- **In `advisory` nothing is filtered**, because the rollout bargain is a
  property of the decision rather than of the surface.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Final

import pytest

from contextplane.api.mcp import server as mcp_server
from contextplane.arc import IntentKind

_REGISTRY: Final = pathlib.Path(mcp_server.__file__).with_name("tool_registry.json")


def _registry() -> dict[str, object]:
    return json.loads(_REGISTRY.read_text(encoding="utf-8"))


def test_every_tool_declares_the_intent_kind_its_act_corresponds_to() -> None:
    """An unclassified tool would be listed to everybody or to nobody, and both
    are decisions nobody made."""
    kinds = mcp_server.tool_intent_kinds()
    names = {entry["name"] for entry in _registry()["tools"]}  # type: ignore[index,union-attr]

    assert set(kinds) == names
    assert set(kinds.values()) <= {"read_only", "data_access"}


def test_both_declared_kinds_are_intent_kinds_the_matrix_selects_on() -> None:
    """The envelope answers "may this principal perform this act". Reusing its
    vocabulary is what makes "which verbs may they see" the same question rather
    than a second, parallel one."""
    for kind in set(mcp_server.tool_intent_kinds().values()):
        assert kind in {member.value for member in IntentKind}


def test_a_tool_that_writes_is_not_classified_as_a_read() -> None:
    """The distinction that matters for autonomy is read versus write. Spot-
    checked against the tools whose misclassification would matter most: three
    that write into the record, and three that only look at it."""
    kinds = mcp_server.tool_intent_kinds()

    for writer in ("assert_claim", "record_session_event", "adjudicate_claim"):
        assert kinds[writer] == "data_access", f"{writer} writes and is classified as a read"
    for reader in ("get_claim", "search_capabilities", "list_curation_queue"):
        assert kinds[reader] == "read_only", f"{reader} only reads and is classified as a write"


def test_the_core_tier_is_never_filtered() -> None:
    """Read out of the filter's own source, because the property is a branch
    rather than a value: a principal that could not see the eight core verbs
    could not open the two-call loop at all, and an envelope has no business
    deciding whether an agent may say who it is."""
    source = ast.unparse(ast.parse(pathlib.Path(mcp_server.__file__).read_text(encoding="utf-8")))
    assert "tool.name in core or" in source, (
        "the listing filter no longer exempts the core tier. Core is the floor a default "
        "connection exposes; filtering it would make `whoami` an envelope decision."
    )


@pytest.mark.parametrize(
    "absent",
    ["no ARC on the deployment", "no resolvable identity", "no validated issuer claim"],
)
def test_an_unanswerable_question_does_not_filter(absent: str) -> None:
    """`None` means "do not filter", and it is returned for every case where an
    answer cannot honestly be given.

    Filtering on an unanswered question would hide verbs from a principal nobody
    had decided anything about — and would make adopting ARC a prerequisite for
    using the tools rather than a decision about autonomy.
    """
    source = pathlib.Path(mcp_server.__file__).read_text(encoding="utf-8")
    body = source[source.index("async def _permitted_intent_kinds") :]
    body = body[: body.index("\n#: The session a listing")]

    # Three `return None` paths, one per absence above.
    assert body.count("return None") >= 3, (
        f"a path that cannot answer ({absent}) now filters instead of declining to. "
        "An absent envelope system is not a refusal."
    )


def test_the_listing_is_not_the_enforcement() -> None:
    """The call path keeps its own guard, and this file would be dangerous if it
    did not. Asserted by naming the pairing E7-T3a registered, so removing the
    call-side guard fails here as well as in its own file."""
    from tests.conformance.test_envelope_reaches_both_transports import GOVERNED_ACTS

    assert GOVERNED_ACTS, "no act is envelope-governed at call time; the listing filter is alone"
    for act, sites in GOVERNED_ACTS.items():
        assert sites["mcp"], f"{act} has no MCP call-time guard"


def test_the_filter_asks_the_envelope_twice_and_not_once_per_tool() -> None:
    """Sixty-two evaluations per listing would put an envelope decision on a
    request path that runs whenever a client reconnects. Tools reduce to two
    kinds, so every tool of a kind shares one answer."""
    source = pathlib.Path(mcp_server.__file__).read_text(encoding="utf-8")
    body = source[source.index("async def _permitted_intent_kinds") :]

    assert "for kind in (IntentKind.READ_ONLY, IntentKind.DATA_ACCESS)" in body, (
        "the filter no longer evaluates a fixed, small set of kinds. If a third kind is "
        "genuinely needed, add it here; if it became per-tool, that is an evaluation per "
        "tool on every reconnect."
    )


def test_an_unknown_kind_is_treated_as_absent_rather_than_permitted() -> None:
    """The rule this tree applies to an unregistered sensitivity tier and to an
    unconfigured sampling category: a value nobody registered must not escape
    every rule that names one."""
    source = pathlib.Path(mcp_server.__file__).read_text(encoding="utf-8")

    assert 'kinds.get(tool.name, "data_access")' in source, (
        "an unclassified tool now defaults to the permissive kind. A verb nobody classified "
        "must not be the one that is always visible."
    )


def test_the_listing_borrows_no_agent_session() -> None:
    """A listing is not part of any session an agent is running, and borrowing
    one would attach an advisory record to work the agent did not do."""
    assert mcp_server._LISTING_SESSION.startswith("mcp:")
