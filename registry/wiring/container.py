"""Typed service container — the single source of truth for what `create_app` wires.

Before this module, every constructed service was hung onto `app.state` under
a bare attribute name: `app.state.catalog = CatalogService(...)`. That is a
stringly-typed service locator — `app.state` is typed `State` (effectively
`Any` attribute access), so `request.app.state.catalog` type-checks as `Any`
no matter what is actually stored there. mypy --strict cannot catch a typo in
an attribute name, a service read before it is constructed, or a caller that
assumes a service is always present when it is actually `None` on some
deployments. Fifty-five keys accumulated this way, and twenty-two call sites
already worked around the gap with `getattr(app.state, name, None)` — trading
a missing service for a silent `None` that only fails once a request tries to
use it.

`Services` makes the service graph a real type. Every field the app
constructs has a name and a concrete type; a caller that asks for a field
that does not exist gets a `dataclass` construction error at startup, not a
`None` three call frames deep in a request handler. It is frozen because the
service graph does not change after `create_app` builds it — there is no
legitimate reason for a request handler to reassign a service out from under
every other handler sharing the same app.

This module is additive: `app.state.services` sits alongside the individual
`app.state.<name>` attributes, which remain the read path for routers until
they migrate over field by field. Once every router reads from `services`
instead, the individual attributes can be retired.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from registry.api.auth.oidc import _OidcCache
from registry.arc.service.approval_trust import ApprovalTrustService
from registry.arc.service.approved_exceptions import ExceptionService
from registry.arc.service.artifact import ArtifactService
from registry.arc.service.attestation import AttestationService
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.challenge import ChallengeService
from registry.arc.service.corpus import CorpusReader
from registry.arc.service.detail_retrieval import JitService
from registry.arc.service.preflight import PreflightRegistry
from registry.arc.service.receipt import ReceiptService
from registry.arc.service.receipt_read import ReceiptReader
from registry.arc.service.resolution import ResolutionService
from registry.arc.service.signing import ReceiptSigningProvider
from registry.arc.service.verifier_registry import VerifierRegistry
from registry.auth.entitlements.resolver import EntitlementResolver
from registry.config import Settings
from registry.service.catalog.breaking_change import BreakingChangeAdvisor
from registry.service.catalog.core import CatalogService
from registry.service.catalog.external_ids import ExternalIdService
from registry.service.catalog.global_vocabulary import GlobalVocabularyService
from registry.service.catalog.includes import IncludeService
from registry.service.catalog.interface_storage import InterfaceStorageService
from registry.service.catalog.lifecycle import LifecycleService
from registry.service.catalog.schema import SchemaService
from registry.service.catalog.vocabulary import VocabularyService
from registry.service.governance.erasure import ErasureRegistry
from registry.service.governance.visibility import VisibilityService
from registry.service.memory.calibration import CalibrationService
from registry.service.memory.capability_requests import CapabilityRequestService
from registry.service.memory.claim_history import ClaimHistoryService
from registry.service.memory.claim_serving import ClaimServingService
from registry.service.memory.claims import ClaimService
from registry.service.memory.confirmation import ConfirmationService
from registry.service.memory.consolidation import ConsolidationService
from registry.service.memory.curation_queue import CurationQueueService
from registry.service.memory.promotion import PromotionService
from registry.service.memory.promotion_guardrails import GuardrailService
from registry.service.memory.session_events import MemoryService
from registry.service.memory.source_governance import SourceGovernanceService
from registry.service.memory.source_ingest import SourceIngestService
from registry.service.platform.adoption import AdoptionService
from registry.service.platform.integration_lookup import IntegrationLookupService
from registry.service.platform.notifications import NotificationService
from registry.service.platform.projections import ProjectionService
from registry.service.platform.subscriptions import SubscriptionService
from registry.service.retrieval import RetrievalService
from registry.service.workspace import WorkspaceService
from registry.types import Clock, Embedder

# Type-only: this field names the writer's type on the container so a caller
# gets `UsageWriter`, not `Any`. Nothing here reads a usage number or acts on
# one. Mirrors why `registry/main.py` — the writer's other construction
# site — is itself a declared importer.
from registry.usage.writer import UsageWriter


@dataclass(frozen=True)
class Services:
    """Every service `create_app` constructs, one typed field per `app.state` key.

    Field order and grouping follow the construction order in
    `registry.main.create_app` (and the ARC sub-wiring in `_wire_arc`), not
    alphabetical order, so this class reads as a map of the same service
    graph rather than an unrelated re-sort of it.
    """

    # -- Core infrastructure -------------------------------------------------
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    scheduler: AsyncIOScheduler
    embedder: Embedder

    # -- Catalog domain (capabilities, facts, edges, cross-tenant surfaces) --
    vocabulary: VocabularyService
    schema: SchemaService
    visibility: VisibilityService
    catalog: CatalogService
    lifecycle: LifecycleService
    retrieval: RetrievalService
    external_ids: ExternalIdService
    adoption: AdoptionService
    projections: ProjectionService
    subscriptions: SubscriptionService
    notifications: NotificationService
    breaking_change: BreakingChangeAdvisor
    integrations: IntegrationLookupService
    interface_storage: InterfaceStorageService
    includes: IncludeService

    # -- Memory / claims domain (session memory, staged claims, promotion) --
    memory: MemoryService
    global_vocabulary: GlobalVocabularyService
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

    # -- ARC domain (attested context resolution — see registry/arc/__init__.py) --
    arc_signing: ReceiptSigningProvider
    arc_authorization: ArcAuthorizationService
    arc_receipts: ReceiptService
    arc_clock: Clock
    arc_corpus: CorpusReader
    arc_challenges: ChallengeService
    arc_attestation: AttestationService
    arc_jit: JitService
    arc_receipt_reader: ReceiptReader
    arc_preflight: PreflightRegistry
    arc_artifacts: ArtifactService
    arc_exceptions: ExceptionService
    arc_verifier_registry: VerifierRegistry
    arc_approval_trust: ApprovalTrustService
    # None on every deployment today: ARC key material is not yet
    # operator-configurable, so resolution has nothing to sign a receipt
    # with. See `_wire_arc` for why an unconfigured deployment gets `None`
    # here rather than a service that would sign with no key.
    arc_resolution: ResolutionService | None

    # -- Auth / entitlements (constructed in the app's lifespan, not at
    #    `create_app` time, because JIT tenant/actor resolution needs a
    #    running event loop for the entitlement-service HTTP client) --
    oidc_cache: _OidcCache
    # Both `None` on deployments that have not configured
    # ENTITLEMENT_SERVICE_URL; see the lifespan function in `registry.main`.
    entitlement_client: httpx.AsyncClient | None
    claim_resolver: EntitlementResolver | None

    # -- Usage metering --------------------------------------------------
    usage_writer: UsageWriter

    # -- Workspaces --------------------------------------------------------
    workspace_service: WorkspaceService

    # -- Erasure (right-to-be-forgotten fan-out registry) -------------------
    # Optional because the RTBF admin route has a documented degraded path:
    # a deployment that has not wired the fan-out registry still purges the
    # workspace subsystem alone rather than refusing the request outright.
    # See the admin route's own comment on that fallback.
    erasure: ErasureRegistry | None


def services(request: Request) -> Services:
    """The app's service container. Replaces getattr-on-app.state lookups."""
    container: Services = request.app.state.services
    return container
