"""Governance-area service construction: the cross-tenant visibility chokepoint.

One registration entry point per area, called by the composition root. The
root builds core infrastructure (settings, engine, session factory, clock),
calls each area's builder in dependency order, and assembles the typed
container from what they return — so adding a service to this area is an edit
to this file and to the container's field list, and to nothing else.

Governance is built first among the service areas because the areas whose
services hold a visibility collaborator take `visibility` as an explicit
parameter — catalog, notifications, retrieval, and ARC. Cross-tenant
filtering is enforced at one layer, and an area that constructed its own
would filter through an object no policy change to the shared one ever
reaches.

Four builders take no `visibility` argument at all — memory, layered
context, entitlements, usage — and that is not an omission to correct.
Where memory needs a visibility decision it calls the module-level helpers
in `contextplane.service.governance.visibility` (`claim_authority.py`,
`claim_serving.py`), which take a session and need no constructed instance.
Threading the parameter into those builders would hand them an object
nothing behind them reads.

`ErasureRegistry` — governance's other container field — is deliberately not
built here. It is assembled in `contextplane.wiring.routes` because its
participant list spans subsystems that only exist once the router table is
mounted (the workspace singleton among them), and moving it would mean
building it twice or reaching for a half-built graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.governance.visibility import VisibilityService
from contextplane.types import Clock


@dataclass(frozen=True)
class GovernanceServices:
    """What this area contributes to the typed container.

    Field names match the container's field names exactly: the composition
    root expands this object into the `Services` constructor by field name,
    so a name that drifts from the container's is a startup error naming
    the field rather than a service silently missing from the graph.
    """

    visibility: VisibilityService


def build_governance_services(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> GovernanceServices:
    """Construct the governance area's services."""
    return GovernanceServices(visibility=VisibilityService(session_factory, clock))
