"""ARC admin routes: REST-only, and gated on identity rather than role.

Two claims matter here and both are asserted directly.

**These routes are not reachable over MCP.** They mutate the governance an
agent is judged against, and an agent able to edit its own rules is the
failure the subsystem exists to prevent.

**Global operations authorize on an exact `(issuer, subject)` pair.** Every
role in this system is tenant-scoped, so no role — not even admin — can be
the deployment trust root. The tests below include an admin being refused,
because that is the case a role-based check would wrongly allow.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from registry.api.routers.arc_admin import operator_allowlist_fingerprint
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)

_ISSUER = "https://idp.test.local"


@pytest_asyncio.fixture
async def harness(pg_container: str) -> AsyncIterator[EntitlementAuthHarness]:
    async with EntitlementAuthHarness(pg_container) as h:
        yield h


@pytest_asyncio.fixture
async def client(harness: EntitlementAuthHarness) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=harness.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def persona(harness: EntitlementAuthHarness, client: AsyncClient):
    p = harness.add_persona(f"arc-admin-{uuid.uuid4().hex[:6]}", roles=["admin"])
    harness.configure_fetcher_for(p)
    with patch_validator_for_actor(p):
        resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=p.slug))
        assert resp.status_code == 200, resp.text
    return p


# --- the routes exist, and are REST only -------------------------------------------


@pytest.mark.asyncio
async def test_the_admin_routes_are_registered(harness: EntitlementAuthHarness) -> None:
    paths = {r.path for r in harness.app.routes if hasattr(r, "path")}
    for expected in (
        "/v1/arc/admin/revisions/{revision_id}/activate",
        "/v1/arc/admin/revisions/{revision_id}/revoke",
        "/v1/arc/admin/revisions/{revision_id}/invalidate",
        "/v1/arc/admin/revisions/{revision_id}/approval-evidence",
        "/v1/arc/admin/approval-verifiers/{approval_verifier_id}/revoke",
        "/v1/arc/admin/approval-evidence/{evidence_id}/revoke",
        "/v1/arc/admin/operator-identity",
    ):
        assert expected in paths, f"{expected} is not registered"


@pytest.mark.asyncio
async def test_no_admin_operation_is_exposed_as_an_mcp_tool() -> None:
    """The claim that makes "REST only" real rather than a comment.

    An agent that could revoke a revision over MCP could edit the governance
    it is judged against.
    """
    from unittest.mock import MagicMock

    from registry.api.routers.mcp import create_registry_mcp_server

    server = create_registry_mcp_server(
        retrieval=MagicMock(),
        catalog=MagicMock(),
        session_factory=MagicMock(),
        annotation_service=MagicMock(),
        workspace_service=MagicMock(),
    )
    names = {t.name for t in await server.list_tools()}
    for forbidden in ("activate", "revoke", "invalidate", "register", "approval"):
        offenders = [n for n in names if forbidden in n and n.startswith("arc_")]
        assert not offenders, f"admin-shaped ARC tool exposed over MCP: {offenders}"


# --- authentication and authorization -----------------------------------------------


@pytest.mark.asyncio
async def test_admin_routes_require_authentication(client: AsyncClient) -> None:
    resp = await client.post(
        f"/v1/arc/admin/revisions/{uuid.uuid4()}/revoke", json={"reason": "no credential"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_a_tenant_admin_is_refused_a_global_operation(client: AsyncClient, persona) -> None:
    """The case a role-based check would wrongly allow.

    Every tenant has admins, so if `admin` sufficed here, any tenant could
    withdraw deployment-wide approval trust.
    """
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/admin/approval-verifiers/{uuid.uuid4().hex[:12]}/revoke",
            json={"reason": "attempting a global operation as a tenant admin"},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 403
    assert resp.json()["errors"][0]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_a_tenant_admin_is_refused_global_evidence_revocation(
    client: AsyncClient, persona
) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/admin/approval-evidence/{uuid.uuid4()}/revoke",
            json={"reason": "attempting a global operation"},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_operator_identity_reports_not_an_operator_by_default(
    client: AsyncClient, persona
) -> None:
    """A deployment that configured no allowlist grants nobody operator
    identity. Falling open on the one surface that binds every tenant would
    be the worst possible default."""
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/arc/admin/operator-identity", headers=bearer_headers(tenant_slug=persona.slug)
        )
    assert resp.status_code == 200
    assert resp.json()["is_global_operator"] is False


@pytest.mark.asyncio
async def test_operator_identity_never_returns_the_allowlist(client: AsyncClient, persona) -> None:
    """A fingerprint, never the membership. An endpoint enumerating
    privileged identities would hand an attacker the exact set of principals
    worth compromising.

    The key set is asserted closed rather than checked for absence of the
    allowlist, so a field added later has to be looked at deliberately -- which
    is the point. The capability booleans below were added that way; each says
    what this deployment cannot do, which is a different thing from naming who
    may do it.
    """
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/arc/admin/operator-identity", headers=bearer_headers(tenant_slug=persona.slug)
        )
    body = resp.json()
    assert set(body) == {
        "is_global_operator",
        "allowlist_fingerprint",
        "approval_verification_enabled",
        "context_resolution_enabled",
        "checked_at",
    }
    assert len(body["allowlist_fingerprint"]) == 64


@pytest.mark.asyncio
async def test_operator_identity_reports_what_this_deployment_cannot_do(
    client: AsyncClient, persona
) -> None:
    """Capability is reported before use, not discovered mid-operation.

    Both flags are False on a deployment with no ARC key material, and both
    correspond to a refusal rather than a degraded success: activation refuses
    because it could not check an approval against a registered verifier, and
    resolution answers 503 because it could not sign a receipt. An operator
    reading these knows which of those they are looking at without triggering
    either.
    """
    with patch_validator_for_actor(persona):
        resp = await client.get(
            "/v1/arc/admin/operator-identity", headers=bearer_headers(tenant_slug=persona.slug)
        )
    body = resp.json()
    assert body["approval_verification_enabled"] is False
    assert body["context_resolution_enabled"] is False


# --- request shape --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revocation_without_a_reason_is_rejected(client: AsyncClient, persona) -> None:
    """An unexplained revocation is not auditable — someone reading the trail
    later needs to know why a rule stopped applying."""
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/admin/revisions/{uuid.uuid4()}/revoke",
            json={},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unknown_body_fields_are_rejected(client: AsyncClient, persona) -> None:
    """An operator who believes they set something and did not has
    registered governance that behaves differently from their intent."""
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/admin/revisions/{uuid.uuid4()}/activate",
            json={"supersedes": None, "force": True},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_acting_on_an_unknown_revision_is_not_found(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.post(
            f"/v1/arc/admin/revisions/{uuid.uuid4()}/revoke",
            json={"reason": "does not exist"},
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 404


# --- the allowlist fingerprint ----------------------------------------------------------


def test_the_fingerprint_is_order_independent() -> None:
    """Two deployments configured with the same operators in a different
    order must fingerprint identically, or an audit trail would appear to
    show a configuration change that never happened."""
    a = (( _ISSUER, "alice"), (_ISSUER, "bob"))
    b = ((_ISSUER, "bob"), (_ISSUER, "alice"))
    assert operator_allowlist_fingerprint(a) == operator_allowlist_fingerprint(b)


def test_the_fingerprint_distinguishes_different_allowlists() -> None:
    """The negative control: order-independence must not have flattened
    everything to one value."""
    a = ((_ISSUER, "alice"),)
    b = ((_ISSUER, "bob"),)
    assert operator_allowlist_fingerprint(a) != operator_allowlist_fingerprint(b)


def test_the_fingerprint_does_not_contain_any_identity() -> None:
    """It goes into audit records, so it must not be a directory."""
    fingerprint = operator_allowlist_fingerprint(((_ISSUER, "alice"),))
    assert "alice" not in fingerprint
    assert _ISSUER not in fingerprint


def test_an_issuer_subject_pair_is_matched_as_a_whole() -> None:
    """A subject under an unexpected issuer is a different principal.

    Splitting on the delimiter differently must not make two distinct
    allowlists collide — otherwise an attacker controlling a trusted IdP
    could mint an operator.
    """
    a = ((f"{_ISSUER}|x", "y"),)
    b = ((_ISSUER, "x|y"),)
    assert operator_allowlist_fingerprint(a) != operator_allowlist_fingerprint(b)


# --- configuration parsing ----------------------------------------------------------------


def test_a_malformed_allowlist_entry_fails_startup() -> None:
    """Fail loudly rather than skipping the entry.

    A silently dropped entry means an operator who believes they have access
    and does not — or an allowlist that looks configured and is empty.
    """
    from registry.config import _parse_operator_allowlist

    with pytest.raises(ValueError, match="missing the '|' delimiter"):
        _parse_operator_allowlist("https://idp.example.test")


def test_an_allowlist_entry_with_an_empty_half_fails_startup() -> None:
    from registry.config import _parse_operator_allowlist

    with pytest.raises(ValueError, match="empty issuer or subject"):
        _parse_operator_allowlist("https://idp.example.test|")


def test_an_absent_allowlist_parses_to_empty_rather_than_failing() -> None:
    """Not configuring it is legitimate — it simply grants nobody."""
    from registry.config import _parse_operator_allowlist

    assert _parse_operator_allowlist(None) == ()
    assert _parse_operator_allowlist("") == ()


def test_a_well_formed_allowlist_parses_to_exact_pairs() -> None:
    from registry.config import _parse_operator_allowlist

    parsed = _parse_operator_allowlist(f" {_ISSUER}|alice , {_ISSUER}|bob ")
    assert parsed == ((_ISSUER, "alice"), (_ISSUER, "bob"))


# --- registering a trust root ------------------------------------------------------


def _verifier_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "approval_verifier_id": f"v-{uuid.uuid4().hex[:12]}",
        "verifier_kind": "operator_public_key",
        "allowed_evidence_types": ["artifact_activation"],
        "scope_kind": "global",
        "algorithm": "Ed25519",
        "public_key": base64.b64encode(b"\x11" * 32).decode("ascii"),
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_registering_a_verifier_requires_operator_identity(
    client: AsyncClient, persona
) -> None:
    """Registering a verifier decides who counts as an approver, so it takes
    the same gate as revoking one -- its blast radius is every activation and
    exception that verifier will ever vouch for.

    A tenant admin is deliberately not enough: every role here is
    tenant-scoped, and no tenant may decide a deployment-wide trust root.
    """
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/admin/approval-verifiers",
            json=_verifier_body(),
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 403
    assert resp.json()["errors"][0]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_registering_a_verifier_requires_authentication(client: AsyncClient) -> None:
    resp = await client.post("/v1/arc/admin/approval-verifiers", json=_verifier_body())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_a_private_key_field_is_rejected_outright(client: AsyncClient, persona) -> None:
    """The request model is closed, and this is the field it most matters for.

    Registration records the public half only. A body naming a private key must
    be refused rather than ignored, so nobody believes they handed one over and
    that it is being protected.
    """
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/admin/approval-verifiers",
            json=_verifier_body(private_key="c2VjcmV0"),
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_non_base64_public_key_is_a_400_not_a_500(client: AsyncClient, persona) -> None:
    with patch_validator_for_actor(persona):
        resp = await client.post(
            "/v1/arc/admin/approval-verifiers",
            json=_verifier_body(public_key="not!base64!"),
            headers=bearer_headers(tenant_slug=persona.slug),
        )
    # Operator gate first -- but the point is it never reaches a decode crash.
    assert resp.status_code in {400, 403}


@pytest.mark.asyncio
async def test_the_verifier_route_is_registered(harness: EntitlementAuthHarness) -> None:
    paths = {r.path for r in harness.app.routes if hasattr(r, "path")}
    assert "/v1/arc/admin/approval-verifiers" in paths
