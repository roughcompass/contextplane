"""ARC domain types.

`ArcRequestContext` is the only thing here for now; directive and rule domain
types land with the selection engine.

The point of this type is that ARC needs four facts about a request that
`TenantContext` does not carry, and needs them to come from the code that already
validated them rather than from a second parse of the token.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from registry.types import TenantContext

# Claim the validated issuer is read from. `validate_oidc_token` has already
# checked it against `OIDC_ISSUER_ALLOWLIST` by the time ARC sees it.
_ISSUER_CLAIM = "iss"


@dataclasses.dataclass(frozen=True)
class ArcRequestContext:
    """Per-request ARC identity, wrapping the tenant context the middleware built.

    Four additions over `TenantContext`, each with a specific consumer:

    - **`oidc_issuer`** — global lifecycle writes are authorized by an exact
      `{issuer, subject}` pair in a deployment-managed allowlist, because the
      `admin` role is tenant-scoped and cannot serve as a deployment trust root.
      `TenantContext` retains the subject but not the issuer, which is the whole
      reason this type exists.
    - **`host_id`** — the registered agent host. Attestation verification binds a
      challenge to it, and a receipt records it.
    - **`token_restriction_digest`** — bound into MCP preflight state so a
      restriction change invalidates the session rather than silently widening
      what a live connection can do.
    - **`mcp_session_id`** — the live MCP connection, when the request arrived
      over MCP rather than REST. Server-assigned; never a caller-supplied string,
      because a caller-chosen session id would let one caller observe or hijack
      another's preflight state.

    Frozen, matching `TenantContext`: a request identity that service code can
    edit is not an identity.
    """

    tenant: TenantContext
    oidc_issuer: str
    host_id: str | None = None
    token_restriction_digest: str | None = None
    mcp_session_id: str | None = None

    # -- pass-throughs, so service code need not reach through `.tenant` --------

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.tenant_id

    @property
    def actor_id(self) -> uuid.UUID:
        return self.tenant.actor_id

    @property
    def roles(self) -> list[str]:
        return self.tenant.roles

    @property
    def oidc_subject(self) -> str:
        return self.tenant.oidc_subject

    @property
    def operator_identity(self) -> tuple[str, str]:
        """The `(issuer, subject)` pair a deployment-operator allowlist matches on.

        Exact, case-sensitive comparison is the caller's job — this only supplies
        the pair. Returning a tuple rather than a formatted string keeps callers
        from inventing their own separator and comparing the wrong things.
        """
        return (self.oidc_issuer, self.tenant.oidc_subject)

    @property
    def is_mcp_session(self) -> bool:
        return self.mcp_session_id is not None

    @classmethod
    def from_validated_claims(
        cls,
        tenant: TenantContext,
        claims: dict[str, Any],
        *,
        host_id: str | None = None,
        token_restriction_digest: str | None = None,
        mcp_session_id: str | None = None,
    ) -> ArcRequestContext:
        """Build from the claims `validate_oidc_token` already returned.

        ARC deliberately does not decode the token itself. A second parser is a
        second place for the two to disagree about `iss`, and the whole value of
        the issuer here is that it is the *validated* one — checked against the
        allowlist by the code that owns that check.

        Raises `ValueError` when the issuer claim is absent or empty, rather than
        defaulting: an ARC context with no issuer cannot authorize a global
        operation, and a silent empty string would compare unequal to every
        allowlist entry and look like a permissions problem instead of a wiring
        bug.
        """
        issuer = claims.get(_ISSUER_CLAIM)
        if not isinstance(issuer, str) or not issuer:
            msg = (
                "ArcRequestContext requires a validated 'iss' claim; got "
                f"{issuer!r}. The caller should pass the claims payload returned "
                "by validate_oidc_token, not a re-parsed token."
            )
            raise ValueError(msg)

        return cls(
            tenant=tenant,
            oidc_issuer=issuer,
            host_id=host_id,
            token_restriction_digest=token_restriction_digest,
            mcp_session_id=mcp_session_id,
        )
