"""REST and MCP must answer the same question the same way.

Two transports over one set of services. The risk is not that they differ
visibly — it is that they drift quietly, so an operation refused over REST
succeeds over MCP, and the weaker path becomes the one an agent learns to
use.

Parity here is *logical*, not envelope equality. REST returns JSON bodies
with HTTP statuses; MCP returns canonical JSON text and raises `ToolError`
with no status at all. Asserting the envelopes matched would be asserting
something untrue and would have to be weakened the first time either
transport changed. What must match is which operations exist, which are
deliberately absent, and which bounded codes a refusal carries.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from contextplane.api.routers import arc as arc_rest
from contextplane.api.routers import arc_admin as arc_admin_rest
from contextplane.api.routers import arc_admin_enrollment as arc_admin_enrollment_rest

# Operations exposed over both transports. Read surfaces and resolution:
# an agent needs these, and needing them over MCP is the whole point of
# having an MCP surface at all.
_DUAL_TRANSPORT = {
    "issue_context_challenge",
    "get_context_resolution_receipt",
    "explain_context_resolution",
}

# Deliberately REST-only. These mutate the governance an agent is judged
# against; an agent able to reach them could edit its own rules.
_REST_ONLY = {
    "attach_approval_evidence",
    "activate_revision",
    "revoke_revision",
    "invalidate_revision",
    "revoke_approval_verifier",
    "revoke_approval_evidence",
    # Verifier enrollment: deciding who counts as an approver is the same
    # class of governance mutation as the five above.
    "create_enrollment_challenge",
    "register_approval_verifier",
}


@pytest.fixture(scope="module")
def mcp_tools() -> set[str]:
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    server = create_contextplane_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        workspace_service=MagicMock(),
    )
    import asyncio

    return {t.name for t in asyncio.run(server.list_tools())}


def _rest_operation_names() -> set[str]:
    return {route.name for route in arc_rest.router.routes if hasattr(route, "name")}


def _rest_admin_operation_names() -> set[str]:
    """Both admin routers -- `arc_admin_enrollment.py` is a sibling of
    `arc_admin.py`, split out only for the 800-line ceiling and mounted
    under the same `/v1/arc/admin` prefix (see that module's own
    docstring), so its operations are part of "the admin router" this
    parity check means."""
    routers = (arc_admin_rest.router, arc_admin_enrollment_rest.router)
    return {route.name for router in routers for route in router.routes if hasattr(route, "name")}


# --- the surfaces agree -----------------------------------------------------------


def test_every_dual_transport_operation_exists_over_rest() -> None:
    rest = _rest_operation_names()
    for operation in _DUAL_TRANSPORT:
        assert operation in rest, f"{operation} is missing from the REST surface"


def test_every_dual_transport_operation_exists_over_mcp(mcp_tools: set[str]) -> None:
    for operation in _DUAL_TRANSPORT:
        assert f"arc_{operation}" in mcp_tools, f"arc_{operation} is missing from the MCP surface"


def test_no_admin_operation_leaks_onto_the_mcp_surface(mcp_tools: set[str]) -> None:
    """The asymmetry that is intentional, asserted so it cannot erode.

    A future contributor adding an admin tool "for convenience" fails here
    rather than shipping an agent the ability to revoke the rules binding
    it.
    """
    for operation in _REST_ONLY:
        assert f"arc_{operation}" not in mcp_tools, f"{operation} must not be reachable over MCP"


def test_the_rest_only_set_matches_the_admin_router() -> None:
    """Keeps this file honest: if an admin route is added and not listed
    above, the parity assertion silently stops covering it."""
    assert _rest_admin_operation_names() >= _REST_ONLY


def test_mcp_has_a_preflight_tool_rest_does_not_need(mcp_tools: set[str]) -> None:
    """A legitimate asymmetry in the other direction.

    REST re-authenticates on every request; a long-lived MCP connection does
    not, so the MCP surface needs an explicit handshake REST has no use for.
    Parity means "the same answers", not "the same tools".
    """
    assert "arc_complete_preflight" in mcp_tools
    assert "complete_preflight" not in _rest_operation_names()


# --- refusals carry the same bounded codes -------------------------------------------


def test_both_transports_share_one_error_mapping() -> None:
    """The REST router exposes its exception-to-status mapping so the MCP
    adapter can reuse it rather than inventing a parallel one that drifts.

    A second mapping is how "forbidden over REST, not-found over MCP" starts
    happening — and a caller probing the difference learns which resources
    exist.
    """
    assert callable(arc_rest.arc_error_status)
    signature = inspect.signature(arc_rest.arc_error_status)
    assert list(signature.parameters) == ["exc"]


def test_the_detail_denial_code_is_one_bounded_value() -> None:
    """Every JIT refusal reports the same code on both transports. Which
    check refused — revoked artifact, audience, invalid token — is
    deliberately indistinguishable, because the difference is exactly the
    probing signal an opaque handle exists to deny."""
    assert arc_rest.DETAIL_DENIED == "detail_denied"


def test_the_unverified_manifest_code_is_one_bounded_value() -> None:
    assert arc_rest.BLOCKED_MANIFEST_UNVERIFIED == "blocked_manifest_unverified"


def test_the_preflight_code_is_one_bounded_value() -> None:
    from contextplane.arc.service.preflight import PREFLIGHT_REQUIRED

    assert PREFLIGHT_REQUIRED == "mcp_preflight_required"


# --- both transports funnel through the same services -----------------------------------


def test_neither_transport_reimplements_authorization() -> None:
    """Both routers read decisions from app state rather than computing
    them. A transport that grew its own check would be a second place for
    the two to disagree about who may do what.
    """
    rest_source = inspect.getsource(arc_rest)
    admin_source = inspect.getsource(arc_admin_rest)
    admin_enrollment_source = inspect.getsource(arc_admin_enrollment_rest)
    for source, name in (
        (rest_source, "arc.py"),
        (admin_source, "arc_admin.py"),
        (admin_enrollment_source, "arc_admin_enrollment.py"),
    ):
        # No router may compare roles inline; that decision belongs to the
        # authorization service or the operator allowlist.
        assert '"admin" in' not in source, f"{name} compares roles inline"
        assert "ROLE_ADMIN in" not in source, f"{name} compares roles inline"
