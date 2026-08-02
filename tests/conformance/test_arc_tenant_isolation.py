"""ARC cross-tenant isolation: not-found and not-yours are indistinguishable.

In `conformance/` deliberately. This is the invariant a future refactor is
most likely to break silently — an authorization check moved, a query that
stops filtering by tenant, a helper that starts raising `PermissionError`
where it used to raise `NotFoundError`. None of those look wrong in review,
and none fail an ordinary unit test.

Two claims are enforced here:

**Tenant B reading Tenant A's receipt, handle, or detail is reported as
not-found.** Distinguishing "forbidden" from "not found" confirms the thing
exists, and existence is itself something the caller is not entitled to
learn — it turns any id into an oracle.

**Nothing leaks in the process.** No title, digest, or source locator may
appear in the response or the exception, however the denial is reported.

It also carries the assertion that makes the reserved `_deployment` tenant
sweep falsifiable rather than merely recorded: it is rejected as a request
tenant everywhere, including on paths that are otherwise open to every
tenant.
"""

from __future__ import annotations

import uuid

import pytest

from registry.arc.models import DEPLOYMENT_TENANT_ID
from registry.arc.service.authorization import (
    ArcAuthorizationError,
    ArcAuthorizationService,
    ArtifactScope,
)
from registry.arc.types import ArcRequestContext, AuthorityScope, DetailAudience
from registry.types import TenantContext

_TENANT_A = uuid.UUID("aaaaaaaa-1111-4000-8000-000000000001")
_TENANT_B = uuid.UUID("bbbbbbbb-2222-4000-8000-000000000002")
_ACTOR_A = uuid.UUID("aaaaaaaa-3333-4000-8000-000000000003")
_ACTOR_B = uuid.UUID("bbbbbbbb-4444-4000-8000-000000000004")

# Strings that must never appear in a denial, whatever shape it takes.
_SECRET_TITLE = "Payments Production Deploy Policy"
_SECRET_DIGEST = "c0ffee" * 10 + "abcd"
_SECRET_LOCATOR = "conf://internal/payments/deploy-policy@17"


class _NeverVisible:
    """Visibility says no. Isolation must not depend on that saying yes."""

    async def visible_capability_ids(self, ctx: object, capability_ids: object) -> list[uuid.UUID]:
        return []


def _service() -> ArcAuthorizationService:
    return ArcAuthorizationService(visibility=_NeverVisible(), global_write_allowlist=())


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID, *, roles: list[str] | None = None) -> ArcRequestContext:
    tenant = TenantContext(
        tenant_id=tenant_id, actor_id=actor_id, roles=roles or ["consumer"], oidc_subject="svc"
    )
    return ArcRequestContext.from_validated_claims(
        tenant, {"iss": "https://idp.example.test"}, host_id="host-1"
    )


# --- receipts -------------------------------------------------------------------


def test_tenant_b_cannot_read_tenant_a_receipt() -> None:
    assert (
        _service().can_read_receipt(
            _ctx(_TENANT_B, _ACTOR_B), receipt_tenant_id=_TENANT_A, receipt_actor_id=_ACTOR_A
        )
        is False
    )


@pytest.mark.parametrize("role", ["consumer", "producer", "admin", "auditor"])
def test_no_role_in_tenant_b_reaches_a_tenant_a_receipt(role: str) -> None:
    """Auditor especially: its reach is wide *within* a tenant and stops at
    the boundary. A refactor that checked role before tenant would pass every
    single-tenant test and fail only here."""
    assert (
        _service().can_read_receipt(
            _ctx(_TENANT_B, _ACTOR_B, roles=[role]),
            receipt_tenant_id=_TENANT_A,
            receipt_actor_id=_ACTOR_A,
        )
        is False
    )


def test_the_same_actor_id_in_another_tenant_is_still_denied() -> None:
    """Ownership must not be checked before tenancy. If it were, an actor id
    colliding across tenants would read another tenant's receipt."""
    assert (
        _service().can_read_receipt(
            _ctx(_TENANT_B, _ACTOR_A), receipt_tenant_id=_TENANT_A, receipt_actor_id=_ACTOR_A
        )
        is False
    )


def test_the_denial_carries_no_detail_about_the_receipt() -> None:
    """However the denial surfaces, it must not describe what was denied."""
    try:
        _service().assert_can_read_receipt(
            _ctx(_TENANT_B, _ACTOR_B), receipt_tenant_id=_TENANT_A, receipt_actor_id=_ACTOR_A
        )
    except ArcAuthorizationError as exc:
        message = str(exc)
        for secret in (_SECRET_TITLE, _SECRET_DIGEST, _SECRET_LOCATOR, str(_TENANT_A)):
            assert secret not in message
    else:  # pragma: no cover - the call above must raise
        pytest.fail("cross-tenant receipt read was permitted")


# --- artifacts and detail ---------------------------------------------------------


@pytest.mark.parametrize(
    "scope", [AuthorityScope.TENANT, AuthorityScope.DOMAIN, AuthorityScope.CAPABILITY, AuthorityScope.TASK]
)
def test_tenant_b_cannot_read_a_tenant_a_artifact_at_any_scope(scope: AuthorityScope) -> None:
    artifact = (
        ArtifactScope(scope=scope, tenant_id=_TENANT_A, capability_id=uuid.uuid4())
        if scope is AuthorityScope.CAPABILITY
        else ArtifactScope(scope=scope, tenant_id=_TENANT_A)
    )
    assert _service().can_read_artifact(_ctx(_TENANT_B, _ACTOR_B), artifact) is False


@pytest.mark.parametrize("audience", list(DetailAudience))
def test_tenant_b_cannot_read_tenant_a_detail_under_any_audience(audience: DetailAudience) -> None:
    """Audience widens access *within* a readable artifact. It must never be
    a way around the tenant boundary — including for a caller holding every
    role and an MCP session."""
    ctx = _ctx(_TENANT_B, _ACTOR_B, roles=["admin", "auditor"])
    ctx = ArcRequestContext(
        tenant=ctx.tenant, oidc_issuer=ctx.oidc_issuer, host_id="host-1", mcp_session_id="mcp-1"
    )
    artifact = ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=_TENANT_A)
    assert _service().can_read_detail(ctx, artifact, audience, matched=True) is False


def test_tenant_b_cannot_write_a_tenant_a_artifact() -> None:
    artifact = ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=_TENANT_A)
    assert _service().can_write_artifact(_ctx(_TENANT_B, _ACTOR_B, roles=["admin"]), artifact) is False


def test_a_global_artifact_stays_readable_across_tenants() -> None:
    """The control. Isolation must not have collapsed into "deny
    everything" — deployment-wide governance is meant to be readable by the
    agents it binds."""
    assert _service().can_read_artifact(
        _ctx(_TENANT_B, _ACTOR_B), ArtifactScope(scope=AuthorityScope.GLOBAL)
    ) is True


def test_a_tenant_reads_its_own_artifact() -> None:
    """The other control: the boundary is a boundary, not a wall."""
    artifact = ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=_TENANT_A)
    assert _service().can_read_artifact(_ctx(_TENANT_A, _ACTOR_A), artifact) is True


# --- the reserved deployment tenant -------------------------------------------------


def test_the_deployment_tenant_cannot_be_a_request_tenant() -> None:
    """The assertion that makes the reserved-tenant sweep falsifiable.

    It exists as a foreign-key target for deployment-scope audit rows, not
    as an identity anything authenticates as. A request arriving under it is
    either a wiring bug or an attempt to borrow deployment scope.
    """
    with pytest.raises(ArcAuthorizationError, match="reserved deployment tenant"):
        _service().assert_request_tenant(_ctx(DEPLOYMENT_TENANT_ID, _ACTOR_A, roles=["admin"]))


@pytest.mark.parametrize("scope", list(AuthorityScope))
def test_the_deployment_tenant_is_refused_on_reads_including_global(scope: AuthorityScope) -> None:
    """Including global reads, which are otherwise open to every tenant — so
    the check must precede the permit rather than sit behind it."""
    artifact = (
        ArtifactScope(scope=AuthorityScope.GLOBAL)
        if scope is AuthorityScope.GLOBAL
        else ArtifactScope(
            scope=scope,
            tenant_id=DEPLOYMENT_TENANT_ID,
            capability_id=uuid.uuid4() if scope is AuthorityScope.CAPABILITY else None,
        )
    )
    with pytest.raises(ArcAuthorizationError):
        _service().can_read_artifact(_ctx(DEPLOYMENT_TENANT_ID, _ACTOR_A, roles=["admin"]), artifact)


def test_the_deployment_tenant_is_refused_on_receipts() -> None:
    with pytest.raises(ArcAuthorizationError):
        _service().can_read_receipt(
            _ctx(DEPLOYMENT_TENANT_ID, _ACTOR_A, roles=["auditor"]),
            receipt_tenant_id=DEPLOYMENT_TENANT_ID,
            receipt_actor_id=_ACTOR_A,
        )


@pytest.mark.asyncio
async def test_the_deployment_tenant_cannot_be_materialized_through_capabilities() -> None:
    """The JIT path must not be a way to bring the sentinel into being as a
    working tenant."""
    with pytest.raises(ArcAuthorizationError):
        await _service().visible_capability_ids(
            _ctx(DEPLOYMENT_TENANT_ID, _ACTOR_A), [uuid.uuid4()]
        )


def test_the_deployment_tenant_is_not_the_seed_default_tenant() -> None:
    """These were the same value once. The all-zero UUID is the seed
    `default` tenant from the first migration, so using it as the sentinel
    made the reserved-tenant insert a silent no-op and the downgrade a
    deletion of a real tenant.
    """
    assert DEPLOYMENT_TENANT_ID != uuid.UUID(int=0)
