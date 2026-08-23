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

What lives here is the *declaration* — the container type and the accessor
that reads it off a request. Assembly stays with the composition root
(`contextplane.wiring.services.build_services_container`), which imports this
type; the dependency runs that way and not the reverse. The declaration sits
under `contextplane.api` because every caller of `services()` is a transport
handler — a REST router or an MCP tool — and the accessor takes a `Request`.
Keeping it here means a router's import of its own container stays inside the
api package instead of reaching up into the wiring that mounts that router,
which is the edge that made the two packages impossible to order.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from contextplane.api.auth.oidc import _OidcCache
from contextplane.arc import (
    ActivationService,
    ApprovalChallengeService,
    ApprovalTrustService,
    ArcAuthorizationService,
    ArtifactMaterialisationService,
    ArtifactService,
    AttestationService,
    AutonomyDecisionService,
    AutonomyEnforcementService,
    AutonomyEnvelopeService,
    ChallengeService,
    CheckpointExportService,
    CorpusReader,
    DrafterService,
    EnrollmentService,
    ExceptionService,
    GovernanceReadService,
    GraphPromotionAdmissionService,
    JitService,
    OperationalChainService,
    PreflightRegistry,
    ProposalService,
    ProvenanceService,
    QualificationService,
    ReceiptReader,
    ReceiptService,
    ReceiptSigningProvider,
    ReplayCorpusService,
    ResolutionService,
    ReviewPackageService,
    RevisionIntegrityService,
    RiskEnvelopeValidator,
    SemanticTestService,
    ShadowService,
    SourceAdmissionService,
    SourceStatusService,
    VerifierRegistry,
)
from contextplane.auth.entitlements.resolver import EntitlementResolver
from contextplane.config import Settings
from contextplane.context.arms import ContextArms
from contextplane.context.receipts import ContextReceiptService
from contextplane.context.references import ReceiptReferenceIndex
from contextplane.context.resolve import ContextResolver
from contextplane.context.resume import ContextResumeService
from contextplane.ownership.service import OwnershipService
from contextplane.profile.bindings import BindingService
from contextplane.profile.service import ProfileService
from contextplane.service.catalog.adoption import AdoptionService
from contextplane.service.catalog.breaking_change import BreakingChangeAdvisor
from contextplane.service.catalog.core import CatalogService
from contextplane.service.catalog.external_ids import ExternalIdService
from contextplane.service.catalog.global_vocabulary import GlobalVocabularyService
from contextplane.service.catalog.includes import IncludeService
from contextplane.service.catalog.integration_lookup import IntegrationLookupService
from contextplane.service.catalog.interface_storage import InterfaceStorageService
from contextplane.service.catalog.lifecycle import LifecycleService
from contextplane.service.catalog.projections import ProjectionService
from contextplane.service.catalog.schema import SchemaService
from contextplane.service.catalog.vocabulary import VocabularyService
from contextplane.service.governance.erasure import ErasureRegistry
from contextplane.service.governance.obligations import ReportingObligationService
from contextplane.service.governance.visibility import VisibilityService
from contextplane.service.memory.agent_accuracy import AgentAccuracyService
from contextplane.service.memory.agent_autonomy import AgentAutonomyService
from contextplane.service.memory.agent_failure_patterns import AgentFailurePatternService
from contextplane.service.memory.agent_instructions import AgentInstructionService
from contextplane.service.memory.calibration import CalibrationService
from contextplane.service.memory.capability_requests import CapabilityRequestService
from contextplane.service.memory.claim_history import ClaimHistoryService
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.memory.claim_writer import ClaimService
from contextplane.service.memory.confirmation import ConfirmationService
from contextplane.service.memory.consolidation import ConsolidationService
from contextplane.service.memory.curation_cases import CurationCaseService
from contextplane.service.memory.curation_queue import CurationQueueService
from contextplane.service.memory.promotion import PromotionService
from contextplane.service.memory.promotion_guardrails import GuardrailService
from contextplane.service.memory.quarantine import QuarantineService
from contextplane.service.memory.sampling_policy import SamplingPolicyService
from contextplane.service.memory.session_events import MemoryService
from contextplane.service.memory.source_governance import SourceGovernanceService
from contextplane.service.memory.source_ingest import SourceIngestService
from contextplane.service.memory.source_namespaces import SourceNamespaceService
from contextplane.service.notifications.core import NotificationService
from contextplane.service.notifications.subscriptions import SubscriptionService
from contextplane.service.retrieval import RetrievalService
from contextplane.service.workspace import WorkspaceService
from contextplane.signals.ingest import SignalIngestService
from contextplane.types import Clock, Embedder

# Type-only: this field names the writer's type on the container so a caller
# gets `UsageWriter`, not `Any`. Nothing here reads a usage number or acts on
# one. Mirrors why `contextplane/main.py` — the writer's other construction
# site — is itself a declared importer.
from contextplane.usage.writer import UsageWriter
from contextplane.workspaces.checkpoints import IntentCheckpointService
from contextplane.workspaces.grants import IntentGrantService
from contextplane.workspaces.recall import WorkspaceRecall


@dataclass(frozen=True)
class Services:
    """Every service `create_app` constructs, one typed field per `app.state` key.

    Field order and grouping follow the construction order in
    `contextplane.main.create_app` (and the ARC sub-wiring in `build_post_app_services`), not
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
    reporting_obligations: ReportingObligationService
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
    agent_accuracy: AgentAccuracyService
    agent_autonomy: AgentAutonomyService
    agent_failure_patterns: AgentFailurePatternService
    agent_instructions: AgentInstructionService
    claim_serving: ClaimServingService
    quarantine: QuarantineService
    promotion: PromotionService
    promotion_guardrails: GuardrailService
    sampling_policy: SamplingPolicyService
    curation_queue: CurationQueueService
    curation_cases: CurationCaseService
    capability_requests: CapabilityRequestService
    source_governance: SourceGovernanceService
    source_ingest: SourceIngestService
    #: The handling tier a replayed stream declared. Read on the session-event
    #: write path, so the envelope decision selects on a tier an operator stated
    #: rather than on nothing at all.
    source_namespaces: SourceNamespaceService
    # Constructed once here rather than per request in the router and the MCP
    # tool. The service is stateless, so building one per call was safe -- but
    # two call sites assembling their own from `session_factory`/`clock`/
    # `source_governance` is two places a collaborator can be swapped in
    # isolation, and the typed container exists so a caller asking for a
    # dependency the app does not declare fails at startup instead of three
    # frames into a request.
    signal_ingest: SignalIngestService

    # -- Profile governance (what a tenant's writes are validated against) ---
    # Two services rather than one because publication and binding have
    # different lifetimes: a revision is written once and never again, while a
    # binding is the row that moves. Collapsing them would put the immutable
    # write path and the state machine behind one name.
    ownership: OwnershipService
    profiles: ProfileService
    profile_bindings: BindingService

    # -- ARC domain (attested context resolution — see contextplane/arc/__init__.py) --
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
    arc_envelopes: AutonomyEnvelopeService
    arc_envelope_decisions: AutonomyDecisionService
    arc_envelope_enforcement: AutonomyEnforcementService
    arc_source_admission: SourceAdmissionService
    arc_graph_source_admission: GraphPromotionAdmissionService
    arc_source_status: SourceStatusService
    arc_proposals: ProposalService
    arc_provenance: ProvenanceService
    arc_semantic_tests: SemanticTestService
    # This process's operational-event signing key -- see the service's own
    # module docstring for why it is process-generated rather than gated
    # behind the same not-yet-configured key material `arc_signing` is.
    arc_operational_chain: OperationalChainService
    arc_checkpoint_export: CheckpointExportService

    # Task memory: the checkpoint chain and the participant audience that gates
    # it. Exposed here rather than constructed per request so both transports
    # share one instance, and so the retention policy a checkpoint binds at
    # write time is a deployment decision made once.
    intent_checkpoints: IntentCheckpointService
    intent_grants: IntentGrantService

    # Layered context: bounded workspace recall, and the composer that turns it
    # and three other services into the four arms one resolution reads. Both are
    # here for the same reason task memory is -- one instance per deployment, so
    # both transports resolve context over identical arms rather than each
    # building its own set and drifting on which service answers which block.
    workspace_recall: WorkspaceRecall
    context_arms: ContextArms
    # The receipt writer is reachable on its own as well as through the
    # resolver, because the receipt-lookup surface reads what resolution wrote
    # and a second instance would mean two clocks stamping one table.
    context_receipts: ContextReceiptService
    context_resolver: ContextResolver
    # The index that makes a receipt findable from the work it describes, and
    # bounded resume over both. One instance each, so a receipt written
    # through one transport is the receipt the other reads.
    context_reference_index: ReceiptReferenceIndex
    context_resume: ContextResumeService
    # The final submission prerequisite: composes risk classification and
    # expected-impact-envelope validation into the one collaborator
    # `arc_materialisation` needs to stop refusing. See `build_post_app_services`'s own
    # comment at the construction site for why injecting this one is what
    # enables `submit` for the first time on any deployment.
    arc_risk_envelope: RiskEnvelopeValidator
    arc_materialisation: ArtifactMaterialisationService
    arc_drafter: DrafterService
    arc_verifier_registry: VerifierRegistry
    arc_approval_trust: ApprovalTrustService
    arc_enrollment: EnrollmentService
    # The `S -> R` half of the D2/D3 digest chain -- `ApprovalChallengeService`
    # below is the only production caller of `assemble`; `arc_authoring.py`'s
    # `GET {PV}/review-package` and `GET {PV}/baseline-diff` routes call the
    # same instance's `get_review_package`/`get_baseline_diff` directly.
    arc_review_package: ReviewPackageService
    # The D2 two-call `artifact_activation` writer -- dormant no longer, per
    # this class's own module docstring: real on every deployment now that
    # `arc_review_package` above exists to inject into it.
    arc_approval_challenges: ApprovalChallengeService
    # ADR 041 Secs.5-9: shadow overlay, deterministic replay-corpus
    # generation/approval, and the qualification decision + acceptance
    # rules. `arc_qualification` composes both of the others; activation
    # (a later task) reads `arc_qualification` only.
    arc_shadow: ShadowService
    arc_replay_corpus: ReplayCorpusService
    arc_qualification: QualificationService
    # The one read-path integrity chokepoint (recomputes S/R/A, verifies
    # projection evidence and current verifier state, validates the
    # operational chain and durable checkpoint, and cross-checks cached
    # derived state). Wired into all four production sites this container
    # also holds (`arc_activation` below, `arc_corpus`, `select_and_verify`
    # via `arc_resolution`, and `arc_authorization.
    # assert_protected_action_authorized`) -- see that class's own module
    # docstring.
    arc_integrity: RevisionIntegrityService
    # The ten-predicate atomic activation gate (ADR 040 Sec.5, ADR 041
    # Sec.8). Predicate 10 (`operational_integrity`) calls `arc_integrity.
    # assess` directly -- see `activation.py`'s own module docstring.
    arc_governance_reads: GovernanceReadService
    arc_activation: ActivationService
    # None on every deployment today: ARC key material is not yet
    # operator-configurable, so resolution has nothing to sign a receipt
    # with. See `build_post_app_services` for why an unconfigured deployment gets `None`
    # here rather than a service that would sign with no key.
    arc_resolution: ResolutionService | None

    # -- Auth / entitlements (constructed in the app's lifespan, not at
    #    `create_app` time, because JIT tenant/actor resolution needs a
    #    running event loop for the entitlement-service HTTP client) --
    oidc_cache: _OidcCache
    # Both `None` on deployments that have not configured
    # ENTITLEMENT_SERVICE_URL; see the lifespan function in `contextplane.main`.
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
