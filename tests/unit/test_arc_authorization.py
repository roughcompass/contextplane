"""The ARC authorization chokepoint: default deny, proven exhaustively.

A permission matrix is the wrong thing to spot-check. These tests enumerate
the cross product of (scope x caller relationship) and (audience x role) and
assert the full expected grid, so a rule that quietly starts permitting an
extra combination fails here rather than in production.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from registry.arc.models import DEPLOYMENT_TENANT_ID
from registry.arc.service.authorization import (
    ArcAuthorizationError,
    ArcAuthorizationService,
    ArtifactScope,
)
from registry.arc.types import ArcRequestContext, AuthorityScope, DetailAudience
from registry.types import TenantContext

_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ACTOR = uuid.UUID("33333333-3333-3333-3333-333333333333")
_OTHER_ACTOR = uuid.UUID("44444444-4444-4444-4444-444444444444")
_CAPABILITY = uuid.UUID("55555555-5555-5555-5555-555555555555")

_ISSUER = "https://idp.example.test/realms/registry"
_OPERATOR_SUBJECT = "svc-deployment-operator"
_ALLOWLIST = ((_ISSUER, _OPERATOR_SUBJECT),)

_NON_GLOBAL_SCOPES = [
    AuthorityScope.TENANT,
    AuthorityScope.DOMAIN,
    AuthorityScope.CAPABILITY,
    AuthorityScope.TASK,
]


def _ctx(
    *,
    tenant_id: uuid.UUID = _TENANT,
    actor_id: uuid.UUID = _ACTOR,
    roles: list[str] | None = None,
    subject: str = "svc-agent-host-1",
    issuer: str = _ISSUER,
    mcp_session_id: str | None = None,
) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=tenant_id, actor_id=actor_id, roles=roles or ["consumer"], oidc_subject=subject
    )
    return ArcRequestContext.from_validated_claims(
        tenant, {"iss": issuer}, host_id="host-1", mcp_session_id=mcp_session_id
    )


class _FakeVisibility:
    """Records that it was called, and with what.

    The point of these tests is that ARC *delegates* rather than deciding,
    so the fake returns something distinguishable from any plausible local
    computation.
    """

    def __init__(self, visible: list[uuid.UUID] | None = None) -> None:
        self.visible = visible if visible is not None else []
        self.calls: list[tuple[uuid.UUID, tuple[uuid.UUID, ...]]] = []

    async def visible_capability_ids(
        self, ctx: ArcRequestContext, capability_ids: Sequence[uuid.UUID]
    ) -> list[uuid.UUID]:
        self.calls.append((ctx.tenant_id, tuple(capability_ids)))
        return self.visible


def _service(visibility: _FakeVisibility | None = None) -> ArcAuthorizationService:
    return ArcAuthorizationService(
        visibility=visibility or _FakeVisibility(), global_write_allowlist=_ALLOWLIST
    )


def _scope(scope: AuthorityScope, tenant_id: uuid.UUID | None = _TENANT) -> ArtifactScope:
    if scope is AuthorityScope.GLOBAL:
        return ArtifactScope(scope=scope)
    if scope is AuthorityScope.CAPABILITY:
        return ArtifactScope(scope=scope, tenant_id=tenant_id, capability_id=_CAPABILITY)
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


# --- the reserved deployment tenant -------------------------------------------


def test_the_reserved_deployment_tenant_cannot_make_requests() -> None:
    """It exists as an audit foreign-key target, not as an identity."""
    ctx = _ctx(tenant_id=DEPLOYMENT_TENANT_ID)
    with pytest.raises(ArcAuthorizationError, match="reserved deployment tenant"):
        _service().assert_request_tenant(ctx)


@pytest.mark.parametrize("scope", list(AuthorityScope))
def test_the_deployment_tenant_is_rejected_on_every_read_path(scope: AuthorityScope) -> None:
    """Including global reads, which are otherwise open to every tenant --
    the check must precede the permit, not sit behind it."""
    ctx = _ctx(tenant_id=DEPLOYMENT_TENANT_ID, roles=["admin"])
    with pytest.raises(ArcAuthorizationError):
        _service().can_read_artifact(ctx, _scope(scope, tenant_id=DEPLOYMENT_TENANT_ID))


def test_the_deployment_tenant_is_rejected_when_reading_receipts() -> None:
    ctx = _ctx(tenant_id=DEPLOYMENT_TENANT_ID, roles=["auditor"])
    with pytest.raises(ArcAuthorizationError):
        _service().can_read_receipt(ctx, receipt_tenant_id=DEPLOYMENT_TENANT_ID, receipt_actor_id=_ACTOR)


# --- artifact reads ------------------------------------------------------------


def test_global_artifacts_are_readable_by_any_authenticated_tenant() -> None:
    """An agent cannot comply with an obligation it may not know about."""
    assert _service().can_read_artifact(_ctx(), _scope(AuthorityScope.GLOBAL)) is True
    assert _service().can_read_artifact(_ctx(tenant_id=_OTHER_TENANT), _scope(AuthorityScope.GLOBAL)) is True


@pytest.mark.parametrize("scope", _NON_GLOBAL_SCOPES)
def test_owning_tenant_can_read_its_own_artifacts(scope: AuthorityScope) -> None:
    assert _service().can_read_artifact(_ctx(), _scope(scope)) is True


@pytest.mark.parametrize("scope", _NON_GLOBAL_SCOPES)
def test_another_tenant_cannot_read_a_tenant_owned_artifact(scope: AuthorityScope) -> None:
    assert _service().can_read_artifact(_ctx(tenant_id=_OTHER_TENANT), _scope(scope)) is False


@pytest.mark.parametrize("scope", _NON_GLOBAL_SCOPES)
def test_admin_of_another_tenant_still_cannot_read(scope: AuthorityScope) -> None:
    """Elevation within a tenant never reaches across one."""
    ctx = _ctx(tenant_id=_OTHER_TENANT, roles=["admin"])
    assert _service().can_read_artifact(ctx, _scope(scope)) is False


# --- artifact writes -----------------------------------------------------------


def test_global_writes_require_the_operator_allowlist() -> None:
    ctx = _ctx(subject=_OPERATOR_SUBJECT)
    assert _service().can_write_artifact(ctx, _scope(AuthorityScope.GLOBAL)) is True


def test_a_tenant_admin_cannot_write_global_artifacts() -> None:
    """The admin role is tenant-scoped: every tenant has one, so admin can
    never be the deployment trust root."""
    ctx = _ctx(roles=["admin"])
    assert _service().can_write_artifact(ctx, _scope(AuthorityScope.GLOBAL)) is False


def test_the_allowlist_matches_on_issuer_and_subject_together() -> None:
    """A subject from an unexpected issuer is a different principal, even
    though the subject string is identical."""
    ctx = _ctx(subject=_OPERATOR_SUBJECT, issuer="https://attacker.example.test/realms/registry")
    assert _service().can_write_artifact(ctx, _scope(AuthorityScope.GLOBAL)) is False


def test_an_empty_allowlist_permits_no_global_writes() -> None:
    """A deployment that configured nothing must not fall open."""
    service = ArcAuthorizationService(visibility=_FakeVisibility(), global_write_allowlist=())
    ctx = _ctx(subject=_OPERATOR_SUBJECT, roles=["admin"])
    assert service.can_write_artifact(ctx, _scope(AuthorityScope.GLOBAL)) is False


@pytest.mark.parametrize("scope", _NON_GLOBAL_SCOPES)
def test_tenant_writes_require_admin(scope: AuthorityScope) -> None:
    assert _service().can_write_artifact(_ctx(roles=["admin"]), _scope(scope)) is True


@pytest.mark.parametrize("scope", _NON_GLOBAL_SCOPES)
@pytest.mark.parametrize("role", ["consumer", "producer", "auditor"])
def test_non_admin_roles_cannot_write_tenant_artifacts(scope: AuthorityScope, role: str) -> None:
    """Auditor included: read-heavy is not write-light."""
    assert _service().can_write_artifact(_ctx(roles=[role]), _scope(scope)) is False


@pytest.mark.parametrize("scope", _NON_GLOBAL_SCOPES)
def test_admin_cannot_write_another_tenants_artifact(scope: AuthorityScope) -> None:
    ctx = _ctx(tenant_id=_OTHER_TENANT, roles=["admin"])
    assert _service().can_write_artifact(ctx, _scope(scope)) is False


def test_assert_variants_raise_rather_than_return_false() -> None:
    service = _service()
    with pytest.raises(ArcAuthorizationError):
        service.assert_can_read_artifact(_ctx(tenant_id=_OTHER_TENANT), _scope(AuthorityScope.TENANT))
    with pytest.raises(ArcAuthorizationError):
        service.assert_can_write_artifact(_ctx(), _scope(AuthorityScope.TENANT))


# --- detail audience ------------------------------------------------------------


@pytest.mark.parametrize("audience", list(DetailAudience))
def test_an_unmatched_artifact_denies_detail_under_every_audience(audience: DetailAudience) -> None:
    """The widest audience is `all_matched_actors`, not `all_actors`."""
    ctx = _ctx(roles=["admin", "auditor"], mcp_session_id="mcp-1")
    assert _service().can_read_detail(ctx, _scope(AuthorityScope.TENANT), audience, matched=False) is False


@pytest.mark.parametrize(
    ("audience", "roles", "mcp_session_id", "expected"),
    [
        # all_matched_actors: any matched actor, no role or channel needed.
        (DetailAudience.ALL_MATCHED_ACTORS, ["consumer"], None, True),
        (DetailAudience.ALL_MATCHED_ACTORS, ["admin"], None, True),
        (DetailAudience.ALL_MATCHED_ACTORS, ["auditor"], None, True),
        # tenant_admin_auditor: exactly those two roles.
        (DetailAudience.TENANT_ADMIN_AUDITOR, ["admin"], None, True),
        (DetailAudience.TENANT_ADMIN_AUDITOR, ["auditor"], None, True),
        (DetailAudience.TENANT_ADMIN_AUDITOR, ["consumer"], None, False),
        (DetailAudience.TENANT_ADMIN_AUDITOR, ["producer"], None, False),
        # registered_gateway_only: the server-assigned MCP session, and no
        # role substitutes for it.
        (DetailAudience.REGISTERED_GATEWAY_ONLY, ["consumer"], "mcp-1", True),
        (DetailAudience.REGISTERED_GATEWAY_ONLY, ["admin"], None, False),
        (DetailAudience.REGISTERED_GATEWAY_ONLY, ["auditor"], None, False),
        (DetailAudience.REGISTERED_GATEWAY_ONLY, ["consumer"], None, False),
    ],
)
def test_the_full_audience_by_role_grid(
    audience: DetailAudience, roles: list[str], mcp_session_id: str | None, expected: bool
) -> None:
    ctx = _ctx(roles=roles, mcp_session_id=mcp_session_id)
    got = _service().can_read_detail(ctx, _scope(AuthorityScope.TENANT), audience, matched=True)
    assert got is expected


@pytest.mark.parametrize("audience", list(DetailAudience))
def test_detail_on_an_unreadable_artifact_is_denied_regardless_of_audience(audience: DetailAudience) -> None:
    """Audience widens access within a readable artifact; it never grants
    access to one the actor could not read at all."""
    ctx = _ctx(tenant_id=_OTHER_TENANT, roles=["admin", "auditor"], mcp_session_id="mcp-1")
    assert _service().can_read_detail(ctx, _scope(AuthorityScope.TENANT), audience, matched=True) is False


def test_assert_can_read_detail_raises() -> None:
    with pytest.raises(ArcAuthorizationError, match="audience"):
        _service().assert_can_read_detail(
            _ctx(), _scope(AuthorityScope.TENANT), DetailAudience.TENANT_ADMIN_AUDITOR, matched=True
        )


# --- receipts --------------------------------------------------------------------


def test_an_actor_can_read_its_own_receipt() -> None:
    assert _service().can_read_receipt(_ctx(), receipt_tenant_id=_TENANT, receipt_actor_id=_ACTOR) is True


@pytest.mark.parametrize(
    ("role", "expected"),
    [("admin", True), ("auditor", True), ("consumer", False), ("producer", False)],
)
def test_reading_another_actors_receipt_requires_admin_or_auditor(role: str, expected: bool) -> None:
    ctx = _ctx(roles=[role])
    got = _service().can_read_receipt(ctx, receipt_tenant_id=_TENANT, receipt_actor_id=_OTHER_ACTOR)
    assert got is expected


@pytest.mark.parametrize("role", ["admin", "auditor", "consumer"])
def test_no_role_reads_a_receipt_from_another_tenant(role: str) -> None:
    """Tenant isolation is checked before role, so an auditor's reach stops
    at their own tenant's boundary."""
    ctx = _ctx(roles=[role])
    assert _service().can_read_receipt(ctx, receipt_tenant_id=_OTHER_TENANT, receipt_actor_id=_ACTOR) is False


def test_own_receipt_in_another_tenant_is_still_denied() -> None:
    """Same actor id, wrong tenant: isolation wins over ownership."""
    ctx = _ctx(tenant_id=_OTHER_TENANT)
    assert _service().can_read_receipt(ctx, receipt_tenant_id=_TENANT, receipt_actor_id=_ACTOR) is False


def test_assert_can_read_receipt_raises() -> None:
    with pytest.raises(ArcAuthorizationError):
        _service().assert_can_read_receipt(_ctx(), receipt_tenant_id=_OTHER_TENANT, receipt_actor_id=_ACTOR)


# --- capability visibility is delegated -------------------------------------------


@pytest.mark.asyncio
async def test_capability_visibility_is_delegated_not_recomputed() -> None:
    """ARC must not have its own opinion about capability visibility.

    The fake returns a set unrelated to the input; ARC returning it verbatim
    is what proves no local filtering happened.
    """
    unrelated = [uuid.UUID("99999999-9999-9999-9999-999999999999")]
    visibility = _FakeVisibility(visible=unrelated)
    got = await _service(visibility).visible_capability_ids(_ctx(), [_CAPABILITY])
    assert got == unrelated
    assert visibility.calls == [(_TENANT, (_CAPABILITY,))]


@pytest.mark.asyncio
async def test_an_empty_capability_list_short_circuits() -> None:
    visibility = _FakeVisibility(visible=[_CAPABILITY])
    got = await _service(visibility).visible_capability_ids(_ctx(), [])
    assert got == []
    assert visibility.calls == []


@pytest.mark.asyncio
async def test_capability_visibility_rejects_the_deployment_tenant() -> None:
    visibility = _FakeVisibility()
    with pytest.raises(ArcAuthorizationError):
        await _service(visibility).visible_capability_ids(_ctx(tenant_id=DEPLOYMENT_TENANT_ID), [_CAPABILITY])
    assert visibility.calls == []


# --- scope invariants ---------------------------------------------------------------


def test_a_global_artifact_carrying_a_tenant_is_rejected_at_construction() -> None:
    """It would otherwise read as that tenant's private governance."""
    with pytest.raises(ValueError, match="must not carry a tenant_id"):
        ArtifactScope(scope=AuthorityScope.GLOBAL, tenant_id=_TENANT)


@pytest.mark.parametrize("scope", _NON_GLOBAL_SCOPES)
def test_a_non_global_artifact_without_a_tenant_is_rejected(scope: AuthorityScope) -> None:
    with pytest.raises(ValueError, match="requires a tenant_id"):
        ArtifactScope(scope=scope, tenant_id=None)


def test_a_capability_scoped_artifact_requires_a_capability_id() -> None:
    with pytest.raises(ValueError, match="requires a capability_id"):
        ArtifactScope(scope=AuthorityScope.CAPABILITY, tenant_id=_TENANT)
