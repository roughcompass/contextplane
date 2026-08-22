"""Memory-area service construction: session memory, staged claims, and promotion.

One registration entry point per area, called by the composition root — see
`contextplane.service.governance.wiring` for the shape.

Everything here is wired unconditionally. None of it needs key material or an
external service, and several of these services register metric families that
have to exist before the first event rather than appearing once somebody has
confirmed a claim — a counter a dashboard cannot chart until it fires is a
counter nobody has.

Two collaborators arrive as explicit parameters because the memory area
cannot name their sources itself. `extraction_strategies` comes from the
extraction package, which sits *above* the service layer in the module
boundary contract, so the root selects the strategies and passes them down.
`pii_scanner` is an optional deployment hook read off the running app, which
only the composition root can see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.security.pii_scanner import PiiScanner
from contextplane.service.catalog.core import CatalogService
from contextplane.service.memory.calibration import CalibrationService
from contextplane.service.memory.capability_requests import CapabilityRequestService
from contextplane.service.memory.claim_history import ClaimHistoryService
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.confirmation import ConfirmationService
from contextplane.service.memory.consolidation import ConsolidationService
from contextplane.service.memory.curation_queue import CurationQueueService
from contextplane.service.memory.promotion import PromotionService
from contextplane.service.memory.promotion_guardrails import GuardrailService
from contextplane.service.memory.session_events import MemoryService
from contextplane.service.memory.source_governance import SourceGovernanceService
from contextplane.service.memory.source_ingest import SourceIngestService
from contextplane.service.memory.source_namespaces import SourceNamespaceService
from contextplane.types import Clock


@dataclass(frozen=True)
class MemoryServices:
    """What this area contributes to the typed container, by container field name."""

    memory: MemoryService
    claims: ClaimService
    confirmations: ConfirmationService
    calibration: CalibrationService
    consolidation: ConsolidationService
    claim_history: ClaimHistoryService
    claim_serving: ClaimServingService
    promotion: PromotionService
    promotion_guardrails: GuardrailService
    curation_queue: CurationQueueService
    capability_requests: CapabilityRequestService
    source_governance: SourceGovernanceService
    source_ingest: SourceIngestService
    #: What a replayed stream's content is, in handling terms. Read on the
    #: session-event write path so the envelope decision selects on a tier an
    #: operator declared rather than on nothing.
    source_namespaces: SourceNamespaceService


def build_memory_services(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    catalog: CatalogService,
    # Typed loosely on purpose: the concrete `Strategy` protocol lives in
    # `contextplane.extraction`, which the module boundary contract places
    # above this layer. The root's call site holds the real type; naming it
    # here would invert the layering for an annotation.
    extraction_strategies: tuple[Any, ...],
    pii_scanner: PiiScanner | None,
) -> MemoryServices:
    """Construct the memory area's services, in dependency order."""
    # The one path that creates claims. Every invariant a claim carries is a
    # property of this service rather than of the row, so there is deliberately
    # no second construction site.
    claims = ClaimService(session_factory, clock=clock)
    # Declared authority and the ingest ceiling. Every connector write goes
    # through `admit`, so a source that never declared a tier cannot write at all.
    source_governance = SourceGovernanceService(session_factory, clock=clock)

    return MemoryServices(
        memory=MemoryService(session_factory, clock=clock, extraction_strategies=extraction_strategies),
        claims=claims,
        confirmations=ConfirmationService(session_factory, claims, clock=clock),
        calibration=CalibrationService(session_factory, clock=clock),
        consolidation=ConsolidationService(session_factory, clock=clock),
        claim_history=ClaimHistoryService(session_factory),
        # The governed read surface. Everything it returns carries citations and
        # an untrusted-recall label, so no other module needs a claim-reading
        # path of its own -- and a second one would be a second place those
        # guarantees could lapse.
        claim_serving=ClaimServingService(session_factory, clock=clock),
        # Promotion is the only path from staging into the canonical graph, so it
        # is constructed here rather than per request: a second instance would be
        # a second place the guardrails could be configured differently. It takes
        # the deployment's configured scanner when there is one, so promotion
        # enforces the same PII policy as every other write path rather than a
        # parallel one of its own.
        promotion=PromotionService(session_factory, claims=claims, clock=clock, pii_scanner=pii_scanner),
        promotion_guardrails=GuardrailService(session_factory, clock=clock),
        curation_queue=CurationQueueService(session_factory),
        # The loop's return path: what consuming teams need, routed to whoever
        # owns the capability. One place the lifecycle rules live.
        capability_requests=CapabilityRequestService(session_factory, clock=clock),
        source_governance=source_governance,
        source_namespaces=SourceNamespaceService(session_factory, clock=clock),
        # The connector run loop's one write path (see contextplane/ingest/runner.py):
        # governance admits the batch, claims stages it, and catalog provisions an
        # entity for an unresolved subject only when the source's own policy opted
        # into that. One instance, same reasoning as every other service here.
        source_ingest=SourceIngestService(claims=claims, governance=source_governance, catalog=catalog),
    )
