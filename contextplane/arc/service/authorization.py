"""The one place ARC decides who may see or change what.

Every ARC read and write funnels through this module. That is the point: a
second place making the same decision is a second place to get it wrong, and
the two will drift. The registry already applies this pattern to cross-tenant
entity reads through `VisibilityService`; this is the ARC-shaped equivalent
for governed context artifacts, applicability rules, JIT detail, and
receipts.

Three rules shape everything here.

**Default deny.** Each decision function starts from "no" and only reaches
"yes" through a rule that explicitly says so. There is no trailing
`return True`, and no branch falls through to permitted. A vocabulary value
this module has never heard of therefore denies rather than sails past an
`if` that did not match it.

**Global writes are not an admin role.** The `admin` role is tenant-scoped:
every tenant has its own admins, so an admin of any tenant would otherwise
be able to edit deployment-wide governance. Writes to global artifacts are
instead authorized against an exact `(issuer, subject)` pair in a
deployment-managed allowlist -- an identity no tenant can grant itself.

**Capability visibility is not reimplemented.** Whether an actor may see a
capability is `VisibilityService`'s decision and stays there, delegated
through an injected protocol. Copying that logic would mean ARC could
diverge from the rest of the registry about who can see what -- exactly the
kind of split-brain the chokepoint pattern exists to prevent.

**Protected-action authorization is a fourth, independent decision.**
`assert_protected_action_authorized` below is the §6.3 "protected-action
authorization" chokepoint: whether an action gated by one revision's
current governance may proceed, given that revision's integrity right now.
It is deliberately not folded into `assert_can_write_artifact`/
`assert_can_read_artifact` above -- those decide whether an actor may touch
ARC's *own* authoring surface (a scope/role question with no revision in
play at all, e.g. creating a family or editing a draft), while this decides
whether the governance *content* a caller is about to act on still stands
behind that action (an integrity question). `integrity` arrives as a
per-call argument, not a constructor dependency: `RevisionIntegrityService`
is itself assembled from a `ReviewPackageService` that takes this class as
its own `authorization` dependency, so this class taking one back at
construction would be circular. A caller that already holds both simply
passes the second one in.

The `RevisionIntegrityService` import below is `TYPE_CHECKING`-only for the
same reason, one level further: even a bare top-level import, never
constructed, would still execute `integrity.py`'s own imports of
`review_package.py` and `approval_challenge.py` -- both of which import
*this* module for their own `authorization` dependency -- so importing the
name eagerly here would be a real circular import, not merely a
theoretical one. `assert_protected_action_authorized` below imports the one
runtime value it actually needs, `PURPOSE_AUTHORIZATION`, locally, inside
its own body, once this module has already finished loading.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.arc.models import DEPLOYMENT_TENANT_ID
from contextplane.arc.types import ArcRequestContext, AuthorityScope, DetailAudience
from contextplane.auth.roles import ROLE_ADMIN, ROLE_AUDITOR
from contextplane.exceptions import RegistryError

if TYPE_CHECKING:
    from contextplane.arc.service.integrity import RevisionIntegrityService


class ArcAuthorizationError(RegistryError):
    """A request was denied. Carries a reason for the audit record only.

    The reason is deliberately not for the caller: telling an actor *why*
    they were denied leaks the existence and shape of things they cannot
    see. Routers translate this to a bare 403/404 and the reason goes to
    the audit trail.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CapabilityVisibility(Protocol):
    """The subset of `VisibilityService` ARC needs.

    Narrow on purpose: ARC asks only "which of these can this actor see",
    never "make this visible". A protocol rather than the concrete class so
    the resolution transaction can supply a session-bound implementation --
    a capability's visibility must be read in the same snapshot as the rest
    of the resolution, not from a service that opens its own session.
    """

    async def visible_capability_ids(
        self, ctx: ArcRequestContext, capability_ids: Sequence[uuid.UUID]
    ) -> list[uuid.UUID]: ...


@dataclasses.dataclass(frozen=True)
class ArtifactScope:
    """The scope and ownership of an artifact or rule, for authorization only.

    A `global` artifact has no `tenant_id`; every other scope must have one.
    That invariant is checked here rather than assumed, because a global
    artifact that carried a tenant would be readable as if it were that
    tenant's private governance.
    """

    scope: AuthorityScope
    tenant_id: uuid.UUID | None = None
    capability_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.scope is AuthorityScope.GLOBAL:
            if self.tenant_id is not None:
                msg = "a global artifact must not carry a tenant_id"
                raise ValueError(msg)
        elif self.tenant_id is None:
            msg = f"a {self.scope} artifact requires a tenant_id"
            raise ValueError(msg)
        if self.scope is AuthorityScope.CAPABILITY and self.capability_id is None:
            msg = "a capability-scoped artifact requires a capability_id"
            raise ValueError(msg)


class ArcAuthorizationService:
    """Single chokepoint for ARC artifact, rule, detail, and receipt access."""

    def __init__(
        self,
        *,
        visibility: CapabilityVisibility,
        global_write_allowlist: Iterable[tuple[str, str]] = (),
    ) -> None:
        self._visibility = visibility
        # Frozen at construction: an allowlist that could be appended to at
        # runtime is an allowlist that can be appended to by a bug.
        self._global_writers = frozenset(global_write_allowlist)

    # -- request admission ----------------------------------------------------

    def assert_request_tenant(self, ctx: ArcRequestContext) -> None:
        """Reject the reserved deployment tenant as a *requesting* tenant.

        It exists so deployment-scoped audit rows have a foreign key target,
        not as an identity anything authenticates as. A request arriving
        under it means either a wiring bug or an attempt to borrow
        deployment scope, and both must fail loudly rather than inherit
        whatever that scope can reach.
        """
        if ctx.tenant_id == DEPLOYMENT_TENANT_ID:
            msg = "the reserved deployment tenant cannot be a request tenant"
            raise ArcAuthorizationError(msg)

    # -- artifacts and rules --------------------------------------------------

    def can_read_artifact(self, ctx: ArcRequestContext, artifact: ArtifactScope) -> bool:
        """Global artifacts are readable by every authenticated tenant;
        everything else only by its owning tenant.

        Global governance is deployment-wide policy that applies to all
        callers, so hiding it from the actors it binds would be perverse --
        an agent cannot comply with an obligation it is not allowed to know
        about. Detail *within* a readable artifact is separately gated by
        audience.
        """
        self.assert_request_tenant(ctx)
        if artifact.scope is AuthorityScope.GLOBAL:
            return True
        return artifact.tenant_id == ctx.tenant_id

    def can_write_artifact(self, ctx: ArcRequestContext, artifact: ArtifactScope) -> bool:
        """Global writes need the operator allowlist; tenant writes need admin.

        The two are different in kind, not degree. No tenant role, however
        elevated, grants the first.
        """
        self.assert_request_tenant(ctx)
        if artifact.scope is AuthorityScope.GLOBAL:
            return ctx.operator_identity in self._global_writers
        if artifact.tenant_id != ctx.tenant_id:
            return False
        return ROLE_ADMIN in ctx.roles

    def assert_can_read_artifact(self, ctx: ArcRequestContext, artifact: ArtifactScope) -> None:
        if not self.can_read_artifact(ctx, artifact):
            msg = f"actor may not read a {artifact.scope} artifact in this tenant"
            raise ArcAuthorizationError(msg)

    def assert_can_write_artifact(self, ctx: ArcRequestContext, artifact: ArtifactScope) -> None:
        if not self.can_write_artifact(ctx, artifact):
            msg = f"actor may not write a {artifact.scope} artifact in this tenant"
            raise ArcAuthorizationError(msg)

    # -- JIT detail -----------------------------------------------------------

    def can_read_detail(
        self,
        ctx: ArcRequestContext,
        artifact: ArtifactScope,
        audience: DetailAudience,
        *,
        matched: bool,
    ) -> bool:
        """Whether this actor may see source locators, digests, and prose.

        `matched` means the artifact was actually selected for this actor's
        task. It is a precondition for every audience, not a shortcut: the
        widest audience is `all_matched_actors`, not `all_actors`, so an
        artifact an actor was never subject to stays unreadable regardless
        of role.
        """
        if not self.can_read_artifact(ctx, artifact):
            return False
        if not matched:
            return False
        if audience is DetailAudience.ALL_MATCHED_ACTORS:
            return True
        if audience is DetailAudience.TENANT_ADMIN_AUDITOR:
            return ROLE_ADMIN in ctx.roles or ROLE_AUDITOR in ctx.roles
        if audience is DetailAudience.REGISTERED_GATEWAY_ONLY:
            # A gateway is identified by the live MCP session the server
            # assigned it, never by a caller-supplied string -- otherwise any
            # caller could claim to be one.
            return ctx.is_mcp_session
        return False

    def assert_can_read_detail(
        self,
        ctx: ArcRequestContext,
        artifact: ArtifactScope,
        audience: DetailAudience,
        *,
        matched: bool,
    ) -> None:
        if not self.can_read_detail(ctx, artifact, audience, matched=matched):
            msg = f"actor may not read detail under audience {audience}"
            raise ArcAuthorizationError(msg)

    # -- receipts -------------------------------------------------------------

    def can_read_receipt(
        self,
        ctx: ArcRequestContext,
        *,
        receipt_tenant_id: uuid.UUID,
        receipt_actor_id: uuid.UUID,
    ) -> bool:
        """Own receipts always; another actor's only as tenant admin or auditor.

        A receipt records what context an agent was given and what it was
        told it must do. Within a tenant that is exactly what an auditor
        exists to inspect, and exactly what a peer agent has no business
        reading.
        """
        self.assert_request_tenant(ctx)
        if receipt_tenant_id != ctx.tenant_id:
            return False
        if receipt_actor_id == ctx.actor_id:
            return True
        return ROLE_ADMIN in ctx.roles or ROLE_AUDITOR in ctx.roles

    def assert_can_read_receipt(
        self,
        ctx: ArcRequestContext,
        *,
        receipt_tenant_id: uuid.UUID,
        receipt_actor_id: uuid.UUID,
    ) -> None:
        if not self.can_read_receipt(ctx, receipt_tenant_id=receipt_tenant_id, receipt_actor_id=receipt_actor_id):
            msg = "actor may not read this receipt"
            raise ArcAuthorizationError(msg)

    # -- capabilities ---------------------------------------------------------

    async def visible_capability_ids(
        self, ctx: ArcRequestContext, capability_ids: Sequence[uuid.UUID]
    ) -> list[uuid.UUID]:
        """Delegate, deliberately. See this module's docstring."""
        self.assert_request_tenant(ctx)
        if not capability_ids:
            return []
        return await self._visibility.visible_capability_ids(ctx, capability_ids)

    # -- protected actions ------------------------------------------------------

    async def assert_protected_action_authorized(
        self,
        session: AsyncSession,
        revision_id: uuid.UUID,
        *,
        integrity: RevisionIntegrityService,
    ) -> None:
        """The §6.3 "protected-action authorization" chokepoint -- see the
        module docstring for how this differs from every scope/role gate
        above. Raises `ArcAuthorizationError` (never a bare bool: unlike
        the scope/role gates above, there is no legitimate "check and
        proceed differently" caller for this one -- every caller either
        authorizes the action or does not attempt it) carrying `assess`'s
        own bounded reason code as `.reason`, which is exactly the detail
        `ArcAuthorizationError` already documents as audit-only, never
        returned to the denied caller.
        """
        # Deferred to break the module-level import cycle -- see this
        # module's own docstring.
        from contextplane.arc.service.integrity import PURPOSE_AUTHORIZATION  # noqa: PLC0415

        result = await integrity.assess(session, revision_id, PURPOSE_AUTHORIZATION)
        if not result.valid:
            raise ArcAuthorizationError(result.reason_code or "revision integrity assessment failed")


__all__ = [
    "ArcAuthorizationError",
    "ArcAuthorizationService",
    "ArtifactScope",
    "CapabilityVisibility",
]
