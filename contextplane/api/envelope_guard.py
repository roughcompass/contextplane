"""The HTTP adapter for the autonomy-envelope decision: proceed, or refuse.

Mirrors `contextplane/api/pii_guard.py`'s shape deliberately, so a route
carrying both gates reads the same way twice: a coroutine taking the request and
the tenant context, returning `None` when the caller may proceed and raising the
refusal otherwise.

**This is the first place anything consults an envelope.** Everything the
autonomy epic built -- the binding, the matrix, the decision, the advisory
records, the graduation pre-flight -- is unreachable without a call site, and a
governance object nothing consults governs nothing. The graduation scan in
particular cannot observe a population that never produces a record, so until a
route calls this, a tenant can never leave `advisory` for a reason that has
nothing to do with its agents.

**In `advisory` this always returns `None`.** That is the whole of the rollout
bargain and it is why wiring this in is safe to do before anybody has an
envelope: the decision runs, the would-be refusal is recorded, and the caller
proceeds. A tenant only starts being refused once an operator graduates it,
which has its own pre-flight in front of it.

**The refusal is a 403 that names the verdict, not the envelope.** A caller that
learns *why* it is outside its envelope learns the shape of the matrix governing
it, one probe at a time. `code` carries the verdict because a caller does need
to tell "nobody has granted me an envelope" from "this act is outside the one I
have" -- the first is an operator ticket, the second is the agent doing something
it should not.
"""

from __future__ import annotations

from fastapi import Request, status

from contextplane.api.errors import build_error
from contextplane.arc import (
    ArcRequestContext,
    AutonomyEnforcementService,
    EnforcementOutcome,
    IntentManifest,
)
from contextplane.types import TenantContext

#: One refusal code per verdict. Distinct because the caller's remedy differs:
#: `envelope_absent` is somebody else's job, `envelope_excluded` is the agent's.
_REFUSAL_CODES: dict[str, str] = {
    "no_envelope": "envelope_absent",
    "envelope_suspended": "envelope_suspended",
    "envelope_withdrawn": "envelope_withdrawn",
    "outside_envelope": "envelope_excluded",
}

#: What the caller is told. Deliberately not the matched rule, the bound
#: revision, or anything about the matrix: a refusal that explained itself would
#: let a caller map its own envelope by probing.
_REFUSAL_MESSAGE = "this principal's autonomy envelope does not authorise this action"


def arc_context(request: Request, ctx: TenantContext) -> ArcRequestContext:
    """Build the ARC identity from what auth already validated.

    The claims come off `request.state`, where `api/middleware/tenant.py` puts
    them for *every* authenticated request rather than only ARC ones -- which is
    what makes this reachable from a memory route at all, and is the same source
    `arc_authoring.py` reads. ARC deliberately does not re-decode the token: a
    second parser is a second place for the two to disagree about who is asking.
    """
    claims = getattr(request.state, "oidc_claims", None) or {}
    try:
        return ArcRequestContext.from_validated_claims(ctx, claims)
    except ValueError as exc:
        raise build_error(
            status.HTTP_401_UNAUTHORIZED,
            code="unauthenticated",
            message="the request carries no validated issuer claim",
        ) from exc


async def enforce_envelope(
    request: Request,
    ctx: TenantContext,
    enforcement: AutonomyEnforcementService,
    manifest: IntentManifest,
) -> EnforcementOutcome:
    """Evaluate the caller's envelope, raising 403 only when the stage enforces.

    Returns the outcome rather than `None` so a caller can record what happened
    without asking a second time -- `run_admission` returns nothing because a
    PII verdict has no second reader, and this one does.
    """
    outcome = await enforcement.evaluate(arc_context(request, ctx), manifest)
    if not outcome.blocked:
        return outcome

    raise build_error(
        status.HTTP_403_FORBIDDEN,
        code=_REFUSAL_CODES.get(str(outcome.decision.verdict), "envelope_excluded"),
        message=_REFUSAL_MESSAGE,
    )


__all__ = ["arc_context", "enforce_envelope"]
