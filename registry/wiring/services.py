"""Construction of every service `create_app` wires — and how it gets there.

Three stages, because the FastAPI `app` object does not exist until partway
through startup (the scheduler and its jobs must already be built before
`app` is constructed, since `lifespan` closes over them):

1. `build_core_services` — the request-time-constructible catalog/memory
   services. Pure construction; no `app` yet, so nothing here touches
   `app.state`.
2. `attach_core_services` / `_wire_arc` / `wire_auth_context` — once `app`
   exists, construct the ARC service graph (which needs `app` to hang its
   own state off) and the loop-dependent auth trio (`oidc_cache`,
   `entitlement_client`, `claim_resolver`) built inside `lifespan`, since JIT
   tenant/actor resolution needs a running event loop for the
   entitlement-service HTTP client. Each function *returns* what it built
   (`UsageWriter`, `ArcServices`, `AuthContext`) for `registry.main.create_app`
   to thread straight into stage 3. A field is also attached to `app.state`
   here only when something outside `registry.wiring` still reads it live —
   a router that has not migrated to the typed container, a middleware that
   deliberately bypasses the container's frozen snapshot, or a test harness
   that replaces the field on an already-running app — and each such
   assignment carries a comment naming that reader.
3. `build_services_container` — assembles the typed `Services` container
   (see `registry.wiring.container`) from exactly what stages 1 and 2
   returned. It takes no `app` and reads no `app.state`: every field it
   needs already arrived as a plain return value, so there is nothing left
   to re-fetch, and a field renamed on one side becomes a constructor error
   here instead of a silent drift between an `app.state.<name> = ...` and a
   matching read.

This module is the reason `registry.wiring` exists: before the split, all
three stages plus the scheduler, the router table, and the OpenAPI contract
lived in one 807-line `create_app`, so a change to any one of them touched
the same function as every other change and every review had to re-read
the whole thing to see what moved. Each stage above can now be read and
changed against just the services it wires.
"""

from __future__ import annotations

import functools
import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from registry.api.auth.oidc import _OidcCache
from registry.arc.schemas.canonical import CANONICAL_PROFILE_VERSIONS
from registry.arc.service.approval_trust import ApprovalTrustService
from registry.arc.service.approved_exceptions import ExceptionService
from registry.arc.service.artifact import ArtifactService
from registry.arc.service.attestation import AttestationService, HostSignerKeyRegistry
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.challenge import ChallengeNonceDeriver, ChallengeService
from registry.arc.service.continuation import ContinuationTokenProvider
from registry.arc.service.corpus import CorpusReader
from registry.arc.service.detail_retrieval import JitService
from registry.arc.service.preflight import PreflightRegistry
from registry.arc.service.proposal import ProposalService
from registry.arc.service.receipt import ReceiptProvenance, ReceiptService
from registry.arc.service.receipt_read import ReceiptReader
from registry.arc.service.replay import ResponseReplayProvider
from registry.arc.service.resolution import ResolutionService
from registry.arc.service.selection import (
    SELECTION_ENGINE_VERSION,
    selection_config_digest,
)
from registry.arc.service.signing import KeyRecord, ReceiptSigningProvider
from registry.arc.service.source_admission import SourceAdmissionService
from registry.arc.service.source_status import SourceStatusService
from registry.arc.service.verifier_registry import VerifierRegistry
from registry.arc.types import ArcRequestContext
from registry.auth.entitlements.client import fetch_entitlements
from registry.auth.entitlements.resolver import EntitlementResolver
from registry.config import Settings
from registry.embedding import build_embedder
from registry.extraction.strategies import STRATEGIES
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
from registry.service.memory.claim_writer import ClaimService
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
from registry.types import Clock, Embedder, SystemClock
from registry.usage.writer import UsageWriter
from registry.wiring.container import Services


@dataclass
class CoreServices:
    """The request-time-constructible services, built before `app` exists.

    A plain handoff type between `build_core_services` (which has no `app`
    to hang state off yet — the scheduler and `lifespan` need to be built
    first) and `attach_core_services` (which does). Field order follows
    construction order in `build_core_services`, not alphabetical order.
    """

    clock: Clock
    vocabulary: VocabularyService
    schema: SchemaService
    visibility: VisibilityService
    catalog: CatalogService
    lifecycle: LifecycleService
    embedder: Embedder
    retrieval: RetrievalService
    external_ids: ExternalIdService
    subscriptions: SubscriptionService
    adoption: AdoptionService
    projections: ProjectionService
    notifications: NotificationService
    breaking_change: BreakingChangeAdvisor
    integrations: IntegrationLookupService
    interface_storage: InterfaceStorageService
    includes: IncludeService


@dataclass
class ArcServices:
    """Everything `_wire_arc` constructs, in construction order.

    A plain hand-off, mirroring `CoreServices`: `_wire_arc` builds every ARC
    and memory/claims service, attaches the handful `registry.api.routers.arc`
    and a couple of tests still read straight off `app.state` (each named at
    its own assignment), and returns the rest here for
    `build_services_container` to read directly instead of re-fetching it.
    """

    arc_signing: ReceiptSigningProvider
    arc_authorization: ArcAuthorizationService
    arc_receipts: ReceiptService
    arc_clock: Clock
    arc_corpus: CorpusReader
    arc_challenges: ChallengeService
    arc_attestation: AttestationService
    arc_jit: JitService
    arc_receipt_reader: ReceiptReader
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
    # Constructed after source_governance and claims, alongside both: it is the
    # one write path connector runs use, and it needs both collaborators plus
    # the catalog service to provision an entity when a source has opted into
    # that (see its own module docstring).
    source_ingest: SourceIngestService
    arc_preflight: PreflightRegistry
    arc_artifacts: ArtifactService
    arc_exceptions: ExceptionService
    arc_source_admission: SourceAdmissionService
    # Constructed right after admission, sharing its clock: every later
    # checkpoint (submission, approval, activation, selection, protected-
    # action authorization) reads a source's local status through this one
    # instance rather than re-deriving freshness rules of its own.
    arc_source_status: SourceStatusService
    arc_proposals: ProposalService
    arc_verifier_registry: VerifierRegistry
    arc_approval_trust: ApprovalTrustService
    # None on every deployment today: ARC key material is not yet
    # operator-configurable, so resolution has nothing to sign a receipt
    # with. See `_wire_arc` for why an unconfigured deployment gets `None`
    # here rather than a service that would sign with no key.
    arc_resolution: ResolutionService | None


@dataclass
class AuthContext:
    """The loop-dependent auth trio `wire_auth_context` builds inside `lifespan`.

    `entitlement_client` lives only on this object -- `lifespan` closes the
    HTTP client itself on shutdown from the same local it captures this
    object from, so nothing else needs it on `app.state`. `oidc_cache` and
    `claim_resolver` are also attached to `app.state` (see
    `wire_auth_context`) because middleware and several test harnesses read
    or replace them live, after the container has already been assembled as
    a frozen snapshot.
    """

    oidc_cache: _OidcCache
    entitlement_client: httpx.AsyncClient | None
    claim_resolver: EntitlementResolver | None


def build_core_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> CoreServices:
    """Construct the catalog-domain services shared by routers, jobs, and MCP."""
    clock = SystemClock()
    vocabulary = VocabularyService(session_factory)
    schema = SchemaService(session_factory, clock)
    visibility = VisibilityService(session_factory, clock)
    catalog = CatalogService(
        session_factory,
        clock,
        vocabulary,
        schema,
        visibility=visibility,
        chunk_tokens=settings.embedding_chunk_tokens,
    )
    # Inject catalog so LifecycleService can delegate replaced_by edge creation
    # via the public CatalogService.create_edge() API.
    lifecycle = LifecycleService(session_factory, clock, catalog=catalog)
    embedder = build_embedder(settings)
    retrieval = RetrievalService(session_factory, clock, embedder, settings, visibility=visibility)
    external_ids = ExternalIdService(session_factory, clock)
    # visibility is instantiated above and injected into CatalogService so
    # visibility filtering is available throughout the service graph.
    # SubscriptionService is built before AdoptionService so it can be wired
    # into the auto_subscribe hook (adoption transparently creates an inbox-only
    # subscription).
    subscriptions = SubscriptionService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
    )
    adoption = AdoptionService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
        auto_subscribe=subscriptions.adoption_hook(),
    )
    projections = ProjectionService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
    )
    notifications = NotificationService(
        session_factory=session_factory,
        clock=clock,
    )
    breaking_change = BreakingChangeAdvisor(
        session_factory=session_factory,
        clock=clock,
        retrieval=retrieval,
        visibility=visibility,
    )
    integrations = IntegrationLookupService(
        session_factory=session_factory,
        visibility=visibility,
    )
    interface_storage = InterfaceStorageService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
    )
    includes = IncludeService(
        session_factory=session_factory,
        visibility=visibility,
        interface_storage=interface_storage,
    )

    return CoreServices(
        clock=clock,
        vocabulary=vocabulary,
        schema=schema,
        visibility=visibility,
        catalog=catalog,
        lifecycle=lifecycle,
        embedder=embedder,
        retrieval=retrieval,
        external_ids=external_ids,
        subscriptions=subscriptions,
        adoption=adoption,
        projections=projections,
        notifications=notifications,
        breaking_change=breaking_change,
        integrations=integrations,
        interface_storage=interface_storage,
        includes=includes,
    )


def attach_core_services(
    app: FastAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    scheduler: AsyncIOScheduler,
    core: CoreServices,
) -> UsageWriter:
    """Attach the `CoreServices` fields a live reader still needs, and build the writer.

    `core` itself already carries every field `build_core_services` built —
    `registry.main.create_app` threads it straight into
    `build_services_container`, so most of `CoreServices` never touches
    `app.state` at all (`vocabulary`, `schema`, `embedder`, and `lifecycle`'s
    sibling fields have no reader outside the container). This function's
    job is narrower than its name suggests: attach only the fields a router,
    middleware, or test harness still reads straight off `app.state` rather
    than through the typed container (each commented at its own
    assignment), and construct the one thing nothing else does — the
    process's single `UsageWriter` — returning it for the container.
    """
    # Surviving bare readers: registry.api.middleware.tenant (settings) and
    # registry.api.mcp.context._resolve_tenant (settings) both read this live
    # rather than through the container — see either one's own comment for why.
    app.state.settings = settings
    # Surviving bare readers: registry.api.middleware.idempotency.get_idempotency_context
    # reads this live; several unit tests build a bare FastAPI() app that sets
    # this attribute directly without ever constructing app.state.services.
    app.state.session_factory = session_factory
    # One writer per process. Two would each hold their own buffer and each report
    # their own depth, so the gauge would describe neither.
    # Surviving bare reader: registry.usage.recording._writer reads this live —
    # durability/overhead tests swap in a different UsageWriter after startup.
    usage_writer = UsageWriter(session_factory)
    app.state.usage_writer = usage_writer
    # Surviving bare readers: registry.api.routers.admin_lifecycle and
    # admin_extraction read this live rather than through the container.
    app.state.clock = core.clock
    # Surviving bare reader: tests/integration/test_integration_capability_exit.py
    # drives lifecycle promotion straight off app.state, on an app built via
    # create_app() whose lifespan never ran -- attach_core_services runs
    # synchronously in create_app's own body, so this is set either way.
    app.state.lifecycle = core.lifecycle
    # Surviving bare readers: registry.api.routers._common, retrieval,
    # admin_sync, registry.ingest.webhook -- none has migrated to the typed
    # container yet.
    app.state.catalog = core.catalog
    # Surviving bare readers: registry.api.routers.retrieval, graph.
    app.state.retrieval = core.retrieval
    # Surviving bare reader: registry.api.routers.external_ids.
    app.state.external_ids = core.external_ids
    # Surviving bare readers: registry.ingest.webhook, registry.api.routers.admin_sync.
    app.state.scheduler = scheduler
    # Surviving bare reader: registry.api.routers.capabilities.
    app.state.visibility = core.visibility
    # Surviving bare reader: registry.api.routers.adoptions.
    app.state.adoption = core.adoption
    # Surviving bare reader: registry.api.routers.graph.
    app.state.projections = core.projections
    # Surviving bare reader: registry.api.routers.subscriptions.
    app.state.subscriptions = core.subscriptions
    # Surviving bare reader: registry.api.routers.notifications.
    app.state.notifications = core.notifications
    # Surviving bare reader: registry.api.routers.breaking_change.
    app.state.breaking_change = core.breaking_change
    # Surviving bare reader: registry.api.routers.integrations.
    app.state.integrations = core.integrations
    # Surviving bare reader: registry.api.routers.interface.
    app.state.interface_storage = core.interface_storage
    # Surviving bare reader: registry.api.routers.capabilities.
    app.state.includes = core.includes
    return usage_writer


def _wire_arc(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    settings: Settings,
    *,
    visibility: VisibilityService,
    catalog: CatalogService,
) -> ArcServices:
    """Construct every ARC and memory/claims service and return them on one object.

    Kept out of `create_app` because ARC brings its own key providers and
    would otherwise add fifty lines to a function that is already long.

    Keys come from settings, and a deployment that configured none gets
    providers holding none: the providers themselves fail closed on first
    use rather than this function silently inventing key material. A
    development key generated here would be indistinguishable, at runtime,
    from a real one.

    `arc_signing`, `arc_clock`, `arc_challenges`, `arc_jit`,
    `arc_receipt_reader`, and `arc_preflight` are also attached to
    `app.state` -- each has a reader outside the container, commented at its
    own assignment below. Every other field this function builds exists only
    on the returned `ArcServices`.

    `catalog` is threaded in (rather than reconstructed here) so
    `source_ingest`'s entity-provisioning path writes through the same
    `CatalogService` instance every other write path uses, not a second one
    built for this function alone.
    """
    # ARC key material is not operator-configurable yet, so every hierarchy
    # starts empty. Named rather than inlined because whether resolution can
    # run at all is decided by whether there is an active key: a provider
    # with none refuses to seal rather than emitting an unsealed envelope.
    # Two shapes: the receipt signer holds full key records because it must
    # know whether a key is retired or compromised before signing with it,
    # while the three AEAD providers hold raw secrets. One active key id
    # across all of them, because they are one hierarchy.
    arc_signing_keys: dict[str, KeyRecord] = {}
    arc_secrets: dict[str, bytes] = {}
    arc_active_key_id: str | None = None

    signing = ReceiptSigningProvider(arc_signing_keys, active_key_id=arc_active_key_id)
    nonce_deriver = ChallengeNonceDeriver(arc_secrets, active_key_id=arc_active_key_id)
    tokens = ContinuationTokenProvider(arc_secrets, active_key_id=arc_active_key_id)
    replay = ResponseReplayProvider(arc_secrets, active_key_id=arc_active_key_id)

    # The allowlist comes from configuration, not from a default here. An
    # empty one permits no global writes at all, which is the correct
    # behaviour for a deployment that configured none: the one surface that
    # binds every tenant must not fall open.
    authorization = ArcAuthorizationService(
        visibility=_ArcVisibilityAdapter(visibility),
        global_write_allowlist=settings.arc_global_operator_allowlist,
    )
    receipts = ReceiptService(signing, clock)

    # Surviving bare reader: registry.api.routers.arc.
    app.state.arc_signing = signing
    # Shared so a request reads the clock exactly once. Resolution assembles
    # its corpus and then evaluates it, and those two steps have to agree on
    # what "now" is or a revision can become effective between them.
    arc_clock = clock
    # Surviving bare reader: registry.api.routers.arc.
    app.state.arc_clock = arc_clock
    arc_corpus = CorpusReader(session_factory)
    arc_challenges = ChallengeService(session_factory, nonce_deriver, clock)
    # Surviving bare reader: registry.api.routers.arc.
    app.state.arc_challenges = arc_challenges
    arc_attestation = AttestationService(HostSignerKeyRegistry(), clock=clock)
    # One registry for the process. It holds state about connections this
    # process is serving, so it cannot meaningfully outlive it -- a restart
    # drops every connection, and any record that survived would be a
    # preflight for a caller nobody is on the other end of.
    arc_jit = JitService(session_factory, receipts=receipts, tokens=tokens, clock=clock)
    # Surviving bare reader: registry.api.routers.arc.
    app.state.arc_jit = arc_jit
    arc_receipt_reader = ReceiptReader(session_factory, authorization=authorization)
    # Surviving bare reader: registry.api.routers.arc.
    app.state.arc_receipt_reader = arc_receipt_reader

    # Session memory. Unconditional: it needs no key material and no
    # external service, so a deployment either has the tables or does not.
    # Extraction strategies are enabled here rather than inside MemoryService so
    # that a deployment with no provider queues nothing at all: with the no-op
    # provider the queue would otherwise grow, be drained into nothing, and cost
    # a write per event for no result.
    memory_strategies = tuple(STRATEGIES.values()) if settings.extraction_provider != "noop" else ()
    memory = MemoryService(session_factory, clock=clock, extraction_strategies=memory_strategies)

    # Organization-scope claim predicates. Separate from the tenant-scoped
    # vocabulary service because it takes no tenant context at all.
    global_vocabulary = GlobalVocabularyService(session_factory, clock=clock)

    # The one path that creates claims. Every invariant a claim carries is a
    # property of this service rather than of the row, so there is deliberately
    # no second construction site.
    claims = ClaimService(session_factory, clock=clock)

    # Confirmation and calibration. Both constructed unconditionally: they need no
    # key material and no external service, and their metric families have to be
    # registered whether or not anybody has confirmed a claim yet -- a counter that
    # only appears after the first event is one a dashboard cannot chart.
    confirmations = ConfirmationService(session_factory, claims, clock=clock)
    calibration = CalibrationService(session_factory, clock=clock)
    consolidation = ConsolidationService(session_factory, clock=clock)
    claim_history = ClaimHistoryService(session_factory)
    # The governed read surface. Everything it returns carries citations and an
    # untrusted-recall label, so no other module needs a claim-reading path of its
    # own -- and a second one would be a second place those guarantees could lapse.
    claim_serving = ClaimServingService(session_factory, clock=clock)
    # Promotion is the only path from staging into the canonical graph, so it is
    # constructed here rather than per request: a second instance would be a second
    # place the guardrails could be configured differently.
    promotion = PromotionService(
        session_factory,
        claims=claims,
        clock=clock,
        # The deployment's configured scanner when there is one, so promotion
        # enforces the same PII policy as every other write path rather than a
        # parallel one of its own.
        pii_scanner=getattr(app.state, "pii_scanner", None),
    )
    promotion_guardrails = GuardrailService(session_factory, clock=clock)
    curation_queue = CurationQueueService(session_factory)
    # The loop's return path: what consuming teams need, routed to whoever owns the
    # capability. Constructed here rather than per request so there is one place the
    # lifecycle rules live.
    capability_requests = CapabilityRequestService(session_factory, clock=clock)
    # Declared authority and the ingest ceiling. Every connector write goes through
    # `admit`, so a source that never declared a tier cannot write at all.
    source_governance = SourceGovernanceService(session_factory, clock=clock)
    # The connector run loop's one write path (see registry/ingest/runner.py):
    # governance admits the batch, claims stages it, and catalog provisions an
    # entity for an unresolved subject only when the source's own policy opted
    # into that. One instance, same reasoning as every other service on this
    # object -- a second construction would be a second place its invariants
    # could drift from this one's.
    source_ingest = SourceIngestService(claims=claims, governance=source_governance, catalog=catalog)
    # Surviving bare reader: tests/integration/test_arc_mcp_tools.py asserts this
    # live off an app built via create_app() whose lifespan never ran -- _wire_arc
    # runs synchronously in create_app's own body, so this is set either way.
    arc_preflight = PreflightRegistry()
    app.state.arc_preflight = arc_preflight
    arc_artifacts = ArtifactService(session_factory, authorization=authorization, clock=clock)
    arc_exceptions = ExceptionService(session_factory, authorization=authorization, clock=clock)
    # Wired unconditionally, like the two services above: source admission
    # needs no key material, only the session factory, the shared
    # authorization chokepoint, and the clock.
    arc_source_admission = SourceAdmissionService(session_factory, authorization=authorization, clock=clock)
    # No operational-chain appender is wired on any deployment today, so
    # revocation/expiry recording refuses rather than partially writing --
    # see the service's own module docstring. `check_status`'s freshness
    # read needs no appender and is fully live from this construction.
    arc_source_status = SourceStatusService(session_factory, clock=clock)
    # Wired unconditionally, same shape as arc_source_admission above: no
    # key material needed, only the session factory, the shared
    # authorization chokepoint, and the clock.
    arc_proposals = ProposalService(session_factory, authorization=authorization, clock=clock)
    # Deployment-wide and cross-tenant, unlike the two services above: see
    # `ApprovalTrustService`'s own docstring for why it cannot reuse either.
    # The trust root for approvals. Wired unconditionally: registering a
    # verifier is how a deployment acquires one, so gating it on already
    # having one would be circular.
    arc_verifier_registry = VerifierRegistry(session_factory, clock=clock)
    arc_approval_trust = ApprovalTrustService(session_factory, authorization=authorization, clock=clock)

    # Resolution is wired only when there is key material behind it. Every
    # resolution signs a receipt and seals the retained response, so without
    # a key it could not produce a receipt it could later stand behind --
    # and the providers refuse rather than emit an unsigned or unsealed one.
    # Left unset, the route answers "not configured on this deployment",
    # which is the truth; wiring it anyway would turn that into a 500 on
    # every call.
    arc_resolution: ResolutionService | None = None
    if arc_active_key_id is not None:
        arc_resolution = ResolutionService(
            session_factory,
            attestation=arc_attestation,
            challenges=arc_challenges,
            receipts=receipts,
            provenance=ReceiptProvenance(
                selection_engine_version=SELECTION_ENGINE_VERSION,
                registry_build_revision=settings.build_revision,
                canonical_profile_versions=dict(CANONICAL_PROFILE_VERSIONS),
                selection_config_digest=selection_config_digest(),
            ),
            clock=clock,
            seal=replay.seal,
        )

    return ArcServices(
        arc_signing=signing,
        arc_authorization=authorization,
        arc_receipts=receipts,
        arc_clock=arc_clock,
        arc_corpus=arc_corpus,
        arc_challenges=arc_challenges,
        arc_attestation=arc_attestation,
        arc_jit=arc_jit,
        arc_receipt_reader=arc_receipt_reader,
        memory=memory,
        global_vocabulary=global_vocabulary,
        claims=claims,
        confirmations=confirmations,
        calibration=calibration,
        consolidation=consolidation,
        claim_history=claim_history,
        claim_serving=claim_serving,
        promotion=promotion,
        promotion_guardrails=promotion_guardrails,
        curation_queue=curation_queue,
        capability_requests=capability_requests,
        source_governance=source_governance,
        source_ingest=source_ingest,
        arc_preflight=arc_preflight,
        arc_artifacts=arc_artifacts,
        arc_exceptions=arc_exceptions,
        arc_source_admission=arc_source_admission,
        arc_source_status=arc_source_status,
        arc_proposals=arc_proposals,
        arc_verifier_registry=arc_verifier_registry,
        arc_approval_trust=arc_approval_trust,
        arc_resolution=arc_resolution,
    )


class _ArcVisibilityAdapter:
    """Bridges ARC's narrow capability-visibility need to `VisibilityService`.

    ARC asks "which of these capabilities may this actor see" and nothing
    else. Adapting rather than widening ARC's protocol keeps the dependency
    one-directional: ARC never learns the rest of that service's surface,
    and cannot start depending on it.
    """

    def __init__(self, visibility: VisibilityService) -> None:
        self._visibility = visibility

    async def visible_capability_ids(
        self, ctx: ArcRequestContext, capability_ids: Sequence[uuid.UUID]
    ) -> list[uuid.UUID]:
        return await self._visibility.filter_entities(ctx.tenant, list(capability_ids))


async def _assert_embedding_dim_matches(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """Refuse to start when the configured vector width disagrees with the schema.

    Caught here, this is a one-line startup error. Caught later, it is an insert
    failure in the drain — after the outbox has already accepted the work, on a
    background job whose errors surface as a retry count rather than a crash.
    Every fact ingested in between looks accepted and silently never becomes
    searchable.

    A column declared as bare ``vector`` with no width reports ``atttypmod`` -1;
    that is not a mismatch, it just means the schema imposes no constraint.
    """
    async with session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT a.atttypmod
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = 'embeddings' AND a.attname = 'vector' AND a.attnum > 0
                """
            )
        )
        row = result.first()

    if row is None:
        return
    column_dim = int(row[0])
    if column_dim < 0 or column_dim == settings.embedding_dim:
        return

    raise RuntimeError(
        f"embedding dimension mismatch: EMBEDDING_DIM is {settings.embedding_dim} but the "
        f"embeddings.vector column stores {column_dim}-d vectors. Either set "
        f"EMBEDDING_DIM={column_dim} to match the schema, or run "
        f"`EMBEDDING_DIM_ALLOW_REBUILD=true alembic upgrade head` to rebuild the column at "
        f"{settings.embedding_dim} — which deletes and recomputes every embedding."
    )


async def _assert_no_legacy_activation_evidence(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Refuse to start if `artifact_activation` evidence predates a first-party writer.

    No production code in this deployment inserts `arc_approval_evidence`
    rows of this type -- `ExceptionService` is the only writer, and it is
    hardcoded to `exception_approval`. A row of this type can therefore only
    have reached the table through something other than a writer this
    system trusts (a direct SQL insert, an old deployment's since-removed
    code path, a bootstrap script), and treating it as a real approval on
    this deployment's first boot after that writer's absence became load-
    bearing would be exactly the silent grandfathering the underlying design
    review rejected. Caught here, an operator sees one refusal at startup
    naming the count. Left uncaught, the deployment starts, serves requests,
    and every receipt asserting one of these revisions was approved is
    wrong from the first request onward.
    """
    async with session_factory() as session:
        count = (
            await session.execute(
                text("SELECT COUNT(*) FROM arc_approval_evidence WHERE evidence_type = 'artifact_activation'")
            )
        ).scalar_one()

    if not count:
        return

    raise RuntimeError(
        f"found {count} arc_approval_evidence row(s) with evidence_type = 'artifact_activation'. No "
        "production writer of this evidence type exists in this deployment, so every such row predates "
        "one and cannot be trusted to have been produced by a real approval. This deployment refuses to "
        "start with them present. Revoke the dependent active revision(s), or run an explicit, reviewed "
        "bootstrap migration that re-creates equivalent evidence through a first-party writer and records "
        "the bootstrap in the audit log, before starting this deployment again."
    )


# The committed drafter model decision artifact. A fixed repo-relative path,
# not a Settings field -- unlike the model artifact itself (which is
# deployment-local and configured), this file is the reviewed decision *about*
# that deployment, and ships in the same commit as the code that reads it.
_DRAFTER_DECISION_PATH = Path(__file__).resolve().parent.parent / "arc" / "drafter" / "model_decision.json"

_DRAFTER_DECISION_OUTCOMES = frozenset({"accepted", "human_only"})
_DRAFTER_DECISION_REQUIRED_KEYS = frozenset(
    {
        "decision_version",
        "model_artifact_digest",
        "tokenizer_digest",
        "prompt_profile_version",
        "resource_envelope",
        "license_terms_reference",
        "evaluation_manifest_version",
        "gate_results",
        "outcome",
    }
)


def load_drafter_model_decision(path: Path = _DRAFTER_DECISION_PATH) -> dict[str, Any]:
    """Load and structurally validate the committed drafter model decision.

    The one parser both the startup guard below and the conformance test
    import -- so the two can never validate different shapes of the same
    file. Raises `ValueError` (not `RuntimeError`; nothing here is a startup
    refusal by itself) on a missing file, invalid JSON, a non-closed key
    set, an unrecognized `outcome`, or a `gate_results` entry missing a
    boolean `passed`.
    """
    if not path.is_file():
        raise ValueError(f"drafter model decision artifact not found at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"drafter model decision artifact at {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"drafter model decision artifact at {path} must be a JSON object")

    actual_keys = set(raw)
    if actual_keys != _DRAFTER_DECISION_REQUIRED_KEYS:
        missing = sorted(_DRAFTER_DECISION_REQUIRED_KEYS - actual_keys)
        extra = sorted(actual_keys - _DRAFTER_DECISION_REQUIRED_KEYS)
        raise ValueError(
            f"drafter model decision artifact at {path} is not the closed shape: missing {missing}, unexpected {extra}"
        )
    if raw["outcome"] not in _DRAFTER_DECISION_OUTCOMES:
        raise ValueError(
            f"drafter model decision artifact outcome {raw['outcome']!r} is not one of "
            f"{sorted(_DRAFTER_DECISION_OUTCOMES)}"
        )
    gate_results = raw["gate_results"]
    if not isinstance(gate_results, list) or not gate_results:
        raise ValueError(f"drafter model decision artifact at {path}: gate_results must be a non-empty array")
    for entry in gate_results:
        if not isinstance(entry, dict) or not isinstance(entry.get("passed"), bool):
            raise ValueError(
                f"drafter model decision artifact at {path}: every gate_results entry needs a boolean 'passed'"
            )
    return raw


def _assert_drafter_decision_permits_serving(settings: Settings) -> None:
    """Refuse to start if the model-backed drafter is enabled beyond what the
    committed decision artifact actually earned.

    `ARC_DRAFTER_MODEL_ENABLED` is a runtime flag; the decision behind it is
    not. Flipping the flag on cannot make a `human_only` verdict, a failed
    evaluation gate, or a swapped model artifact serve just by setting an
    environment variable -- that is what this function is for. When the flag
    is false (the default, including when it is absent from the environment
    entirely), this function returns immediately without reading the
    decision artifact or the configured model-artifact path at all: a
    disabled deployment never touches either, by construction rather than by
    convention.
    """
    if not settings.arc_drafter_model_enabled:
        return

    decision = load_drafter_model_decision()

    if decision["outcome"] != "accepted":
        raise RuntimeError(
            f"ARC_DRAFTER_MODEL_ENABLED=true but {_DRAFTER_DECISION_PATH} records "
            f"outcome={decision['outcome']!r}, not 'accepted'. The model-backed drafter cannot serve on a "
            "verdict nobody made. Set ARC_DRAFTER_MODEL_ENABLED=false, or land a new decision that records "
            "'accepted' with every evaluation gate passed."
        )

    failed_gates = sorted(g.get("gate_id", "<unnamed>") for g in decision["gate_results"] if not g["passed"])
    if failed_gates:
        raise RuntimeError(
            f"ARC_DRAFTER_MODEL_ENABLED=true but {_DRAFTER_DECISION_PATH} records outcome='accepted' with "
            f"failing evaluation gate(s): {failed_gates}. An accepted outcome requires every gate to have "
            "passed; refusing to start rather than serve a model that did not actually clear its own gates."
        )

    artifact_path = settings.arc_drafter_model_artifact_path
    if not artifact_path or not Path(artifact_path).is_file():
        raise RuntimeError(
            f"ARC_DRAFTER_MODEL_ENABLED=true but ARC_DRAFTER_MODEL_ARTIFACT_PATH ({artifact_path!r}) does not "
            "name a file that exists. The decision artifact's recorded model_artifact_digest cannot be "
            "verified against a missing model artifact."
        )

    actual_digest = hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest()
    if actual_digest != decision["model_artifact_digest"]:
        raise RuntimeError(
            f"ARC_DRAFTER_MODEL_ENABLED=true but the file at {artifact_path} hashes to {actual_digest}, not "
            f"the decision artifact's recorded model_artifact_digest={decision['model_artifact_digest']!r}. "
            "The flag can never be more permissive than the artifact the decision actually evaluated; "
            "refusing to start rather than serve an unverified model."
        )


def wire_auth_context(
    app: FastAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> AuthContext:
    """Build the loop-dependent auth trio: oidc_cache, entitlement_client, claim_resolver.

    Called from `lifespan`, not `create_app`, because JIT tenant/actor
    resolution needs a running event loop for the entitlement-service HTTP
    client — constructing `httpx.AsyncClient()` itself doesn't need one, but
    every request that uses it does.
    """
    oidc_cache = _OidcCache()
    # Surviving bare readers: registry.api.middleware.tenant (oidc_cache) and
    # registry.api.mcp.context._resolve_tenant (oidc_cache) both read this live
    # rather than through the container — see either one's own comment for why.
    app.state.oidc_cache = oidc_cache

    # Entitlement service wiring — only when the deployment has
    # configured ENTITLEMENT_SERVICE_URL. Legacy / test deployments
    # without it skip this path; the middleware fails closed (500
    # "claim resolver not configured") on the first authenticated
    # request, which is the right loud signal during transition.
    if settings.entitlement_service_url:
        entitlement_client = httpx.AsyncClient()
        bound_fetcher = functools.partial(fetch_entitlements, entitlement_client)
        claim_resolver: EntitlementResolver | None = EntitlementResolver(
            settings=settings,
            session_factory=session_factory,
            fetcher=bound_fetcher,
        )
    else:
        entitlement_client = None
        claim_resolver = None

    # Surviving bare readers of claim_resolver (both branches above):
    # registry.api.middleware.tenant and registry.api.mcp.context._resolve_tenant
    # read it live, deliberately not through the container — several test
    # harnesses replace app.state.claim_resolver on an already-running app
    # (see tests/helpers/auth_harness.py), after the container has already
    # been assembled from whatever this function set at the time.
    app.state.claim_resolver = claim_resolver

    # entitlement_client itself is not attached to app.state: the only other
    # reader is registry.main's own lifespan, which closes it on shutdown
    # from the AuthContext this function returns, not from app.state.
    return AuthContext(
        oidc_cache=oidc_cache,
        entitlement_client=entitlement_client,
        claim_resolver=claim_resolver,
    )


def build_services_container(
    *,
    settings: Settings,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    scheduler: AsyncIOScheduler,
    core: CoreServices,
    arc: ArcServices,
    auth: AuthContext,
    usage_writer: UsageWriter,
    workspace_service: WorkspaceService,
    erasure: ErasureRegistry | None,
) -> Services:
    """Assemble the typed `Services` container from what every wiring step returned.

    Takes no `app` and reads no `app.state` — every argument here is exactly
    what `build_core_services`, `attach_core_services`, `_wire_arc`,
    `wire_auth_context`, and `registry.wiring.routes.register` (the
    workspace singleton and the erasure registry, both built after the
    router table is mounted) handed back. `registry.main.create_app` is the
    only caller, and threads each one straight from the wiring call that
    built it.
    """
    return Services(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        clock=core.clock,
        scheduler=scheduler,
        embedder=core.embedder,
        vocabulary=core.vocabulary,
        schema=core.schema,
        visibility=core.visibility,
        catalog=core.catalog,
        lifecycle=core.lifecycle,
        retrieval=core.retrieval,
        external_ids=core.external_ids,
        adoption=core.adoption,
        projections=core.projections,
        subscriptions=core.subscriptions,
        notifications=core.notifications,
        breaking_change=core.breaking_change,
        integrations=core.integrations,
        interface_storage=core.interface_storage,
        includes=core.includes,
        memory=arc.memory,
        global_vocabulary=arc.global_vocabulary,
        claims=arc.claims,
        confirmations=arc.confirmations,
        calibration=arc.calibration,
        consolidation=arc.consolidation,
        claim_history=arc.claim_history,
        claim_serving=arc.claim_serving,
        promotion=arc.promotion,
        promotion_guardrails=arc.promotion_guardrails,
        curation_queue=arc.curation_queue,
        capability_requests=arc.capability_requests,
        source_governance=arc.source_governance,
        source_ingest=arc.source_ingest,
        arc_signing=arc.arc_signing,
        arc_authorization=arc.arc_authorization,
        arc_receipts=arc.arc_receipts,
        arc_clock=arc.arc_clock,
        arc_corpus=arc.arc_corpus,
        arc_challenges=arc.arc_challenges,
        arc_attestation=arc.arc_attestation,
        arc_jit=arc.arc_jit,
        arc_receipt_reader=arc.arc_receipt_reader,
        arc_preflight=arc.arc_preflight,
        arc_artifacts=arc.arc_artifacts,
        arc_exceptions=arc.arc_exceptions,
        arc_source_admission=arc.arc_source_admission,
        arc_source_status=arc.arc_source_status,
        arc_proposals=arc.arc_proposals,
        arc_verifier_registry=arc.arc_verifier_registry,
        arc_approval_trust=arc.arc_approval_trust,
        arc_resolution=arc.arc_resolution,
        oidc_cache=auth.oidc_cache,
        entitlement_client=auth.entitlement_client,
        claim_resolver=auth.claim_resolver,
        usage_writer=usage_writer,
        workspace_service=workspace_service,
        erasure=erasure,
    )
