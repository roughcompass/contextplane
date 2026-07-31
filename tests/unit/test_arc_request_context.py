"""ArcRequestContext carries the four facts TenantContext does not."""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from registry.arc.types import ArcRequestContext
from registry.types import TenantContext

_ISSUER = "https://idp.example.test/realms/registry"
_SUBJECT = "svc-agent-host-1"


def _tenant() -> TenantContext:
    return TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        roles=["admin"],
        oidc_subject=_SUBJECT,
    )


def test_carries_the_validated_issuer_tenant_context_lacks() -> None:
    tenant = _tenant()
    ctx = ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER, "sub": _SUBJECT})
    assert ctx.oidc_issuer == _ISSUER
    assert not hasattr(tenant, "oidc_issuer")


def test_operator_identity_is_the_issuer_subject_pair() -> None:
    """Global lifecycle writes match on this exact pair."""
    ctx = ArcRequestContext.from_validated_claims(_tenant(), {"iss": _ISSUER})
    assert ctx.operator_identity == (_ISSUER, _SUBJECT)


def test_operator_identity_is_a_tuple_not_a_joined_string() -> None:
    """A formatted string invites each caller to pick its own separator.

    Two callers disagreeing on the separator would compare different things
    against the same allowlist.
    """
    ctx = ArcRequestContext.from_validated_claims(_tenant(), {"iss": _ISSUER})
    assert isinstance(ctx.operator_identity, tuple)
    assert len(ctx.operator_identity) == 2


@pytest.mark.parametrize("claims", [{}, {"iss": ""}, {"iss": None}, {"iss": 42}])
def test_missing_or_malformed_issuer_raises(claims: dict[str, object]) -> None:
    """Failing loudly beats defaulting to an empty issuer.

    An empty issuer compares unequal to every allowlist entry, so it would
    surface as a permissions problem rather than as the wiring bug it is.
    """
    with pytest.raises(ValueError, match="validated 'iss' claim"):
        ArcRequestContext.from_validated_claims(_tenant(), claims)  # type: ignore[arg-type]


def test_pass_throughs_match_the_wrapped_tenant_context() -> None:
    tenant = _tenant()
    ctx = ArcRequestContext.from_validated_claims(tenant, {"iss": _ISSUER})
    assert ctx.tenant_id == tenant.tenant_id
    assert ctx.actor_id == tenant.actor_id
    assert ctx.roles == tenant.roles
    assert ctx.oidc_subject == tenant.oidc_subject


def test_host_and_session_default_to_absent() -> None:
    """A REST request has no MCP session, and an unattested one has no host."""
    ctx = ArcRequestContext.from_validated_claims(_tenant(), {"iss": _ISSUER})
    assert ctx.host_id is None
    assert ctx.mcp_session_id is None
    assert ctx.token_restriction_digest is None
    assert ctx.is_mcp_session is False


def test_mcp_session_is_reported_when_present() -> None:
    ctx = ArcRequestContext.from_validated_claims(
        _tenant(),
        {"iss": _ISSUER},
        host_id="host-1",
        token_restriction_digest="a" * 64,
        mcp_session_id="server-assigned-connection-id",
    )
    assert ctx.is_mcp_session is True
    assert ctx.host_id == "host-1"
    assert ctx.token_restriction_digest == "a" * 64


def test_context_is_frozen() -> None:
    """A request identity service code can edit is not an identity."""
    ctx = ArcRequestContext.from_validated_claims(_tenant(), {"iss": _ISSUER})
    assert dataclasses.is_dataclass(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.oidc_issuer = "https://attacker.example"  # type: ignore[misc]
