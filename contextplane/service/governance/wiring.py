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

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.governance.actors import ActorDirectoryService
from contextplane.service.governance.obligation_evidence import ObligationEvidenceService
from contextplane.service.governance.obligations import ReportingObligationService
from contextplane.service.governance.visibility import VisibilityService
from contextplane.types import Clock

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]


@dataclass(frozen=True)
class GovernanceServices:
    """What this area contributes to the typed container.

    Field names match the container's field names exactly: the composition
    root expands this object into the `Services` constructor by field name,
    so a name that drifts from the container's is a startup error naming
    the field rather than a service silently missing from the graph.
    """

    visibility: VisibilityService
    #: Nominating and classifying a reporting obligation. Takes no `visibility`
    #: collaborator: an obligation is owned by exactly one tenant and is never
    #: cross-tenant readable, so there is no decision for one to make.
    reporting_obligations: ReportingObligationService
    actor_directory: ActorDirectoryService
    obligation_evidence: ObligationEvidenceService


def build_governance_services(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> GovernanceServices:
    """Construct the governance area's services."""
    obligations = ReportingObligationService(session_factory, clock=clock)
    return GovernanceServices(
        reporting_obligations=obligations,
        actor_directory=ActorDirectoryService(session_factory, clock=clock),
        obligation_evidence=ObligationEvidenceService(session_factory, obligations=obligations),
        visibility=VisibilityService(session_factory, clock),
    )


def register_governance_jobs(
    scheduler: AsyncIOScheduler,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Register this area's periodic work on the shared scheduler.

    Here rather than in `wiring/jobs.py` for the reason that file's own ceiling
    encodes: adding a job to an area should touch the area's wiring and nothing
    in the composition root.

    **The obligation backlog is published on an interval, not computed on read.**
    Both gauges are deployment-wide and carry no tenant label -- one would turn
    each into a series per tenant -- so a per-tenant read cannot fill them
    without reporting whichever tenant happened to ask most recently.

    Hourly, because a reporting backlog moves in hours and a tighter interval
    would buy load rather than resolution. It fires once at startup so an
    operator sees a real number without waiting an hour for the first one.

    **A scheduled observer that silently stops is the known risk**, and it is
    worse for a gauge than for a worker: the value sits at its last reading,
    looking healthy. The age gauge is the defence -- a stalled job freezes the
    age, and an age that stops advancing while the count is non-zero is itself
    the signal that the observer, not the backlog, is the problem.
    """
    obligations = ReportingObligationService(session_factory, clock=clock)
    scheduler.add_job(
        obligations.observe_backlog,
        trigger="interval",
        hours=1,
        max_instances=1,
        coalesce=True,
        id="reporting_obligation_backlog",
        replace_existing=True,
        next_run_time=datetime.datetime.now(tz=datetime.UTC),
    )
