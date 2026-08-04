"""Construction of every service `create_app` wires — and how it gets there.

Three stages, because the FastAPI `app` object does not exist until partway
through startup (the scheduler and its jobs must already be built before
`app` is constructed, since `lifespan` closes over them):

1. `build_core_services` — the request-time-constructible catalog/memory
   services. Pure construction; no `app` yet, so nothing here touches
   `app.state`.
2. `attach_core_services` / `_wire_arc` / `wire_auth_context` — once `app`
   exists, populate `app.state` with what stage 1 built, the ARC service
   graph (which needs `app` to hang its own state off), and the
   loop-dependent auth trio (`oidc_cache`, `entitlement_client`,
   `claim_resolver`) built inside `lifespan`, since JIT tenant/actor
   resolution needs a running event loop for the entitlement-service HTTP
   client.
3. `build_services_container` — assembles the typed `Services` container
   (see `registry.wiring.container`) from `app.state` once every field on
   it has been set.

This module is the reason `registry.wiring` exists: before the split, all
three stages plus the scheduler, the router table, and the OpenAPI contract
lived in one 807-line `create_app`, so a change to any one of them touched
the same function as every other change and every review had to re-read
the whole thing to see what moved. Each stage above can now be read and
changed against just the services it wires.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from registry.api.auth.oidc import _OidcCache
from registry.arc.schemas.canonical import CANONICAL_PROFILE_VERSIONS
from registry.arc.service.approval_trust import ApprovalTrustService
from registry.arc.service.artifact import ArtifactService
from registry.arc.service.attestation import AttestationService, HostSignerKeyRegistry
from registry.arc.service.authorization import ArcAuthorizationService
from registry.arc.service.challenge import ChallengeNonceDeriver, ChallengeService
from registry.arc.service.continuation import ContinuationTokenProvider
from registry.arc.service.corpus import CorpusReader
from registry.arc.service.exception import ExceptionService
from registry.arc.service.jit import JitService
from registry.arc.service.preflight import PreflightRegistry
from registry.arc.service.receipt import ReceiptProvenance, ReceiptService
from registry.arc.service.receipt_read import ReceiptReader
from registry.arc.service.replay import ResponseReplayProvider
from registry.arc.service.resolution import ResolutionService
from registry.arc.service.selection import (
    SELECTION_ENGINE_VERSION,
    selection_config_digest,
)
from registry.arc.service.signing import KeyRecord, ReceiptSigningProvider
from registry.arc.service.verifier_registry import VerifierRegistry
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
from registry.service.platform.adoption import AdoptionService
from registry.service.platform.integration_lookup import IntegrationLookupService
from registry.service.platform.notifications import NotificationService
from registry.service.platform.projections import ProjectionService
from registry.service.platform.subscriptions import SubscriptionService
from registry.service.retrieval import RetrievalService
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
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    scheduler: AsyncIOScheduler,
    core: CoreServices,
) -> None:
    """Populate `app.state` with the core services and infra `create_app` built.

    Routers read every one of these off `app.state` by bare attribute name
    (`request.app.state.catalog`, etc.) — that read path predates the typed
    `Services` container and stays the contract until routers migrate over.
    """
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    # One writer per process. Two would each hold their own buffer and each report
    # their own depth, so the gauge would describe neither.
    app.state.usage_writer = UsageWriter(session_factory)
    app.state.clock = core.clock
    app.state.vocabulary = core.vocabulary
    app.state.schema = core.schema
    app.state.lifecycle = core.lifecycle
    app.state.catalog = core.catalog
    app.state.embedder = core.embedder
    app.state.retrieval = core.retrieval
    app.state.external_ids = core.external_ids
    app.state.scheduler = scheduler
    app.state.visibility = core.visibility
    app.state.adoption = core.adoption
    app.state.projections = core.projections
    app.state.subscriptions = core.subscriptions
    app.state.notifications = core.notifications
    app.state.breaking_change = core.breaking_change
    app.state.integrations = core.integrations
    app.state.interface_storage = core.interface_storage
    app.state.includes = core.includes


def _wire_arc(
    app: FastAPI,
    session_factory: Any,
    clock: Any,
    settings: Settings,
    *,
    visibility: Any,
) -> None:
    """Construct the ARC services and attach them to app state.

    Kept out of `create_app` because ARC brings its own key providers and
    would otherwise add fifty lines to a function that is already long.

    Keys come from settings, and a deployment that configured none gets
    providers holding none: the providers themselves fail closed on first
    use rather than this function silently inventing key material. A
    development key generated here would be indistinguishable, at runtime,
    from a real one.
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

    app.state.arc_signing = signing
    app.state.arc_authorization = authorization
    app.state.arc_receipts = receipts
    # Shared so a request reads the clock exactly once. Resolution assembles
    # its corpus and then evaluates it, and those two steps have to agree on
    # what "now" is or a revision can become effective between them.
    app.state.arc_clock = clock
    app.state.arc_corpus = CorpusReader(session_factory)
    app.state.arc_challenges = ChallengeService(session_factory, nonce_deriver, clock)
    app.state.arc_attestation = AttestationService(HostSignerKeyRegistry(), clock=clock)
    app.state.arc_jit = JitService(session_factory, receipts=receipts, tokens=tokens, clock=clock)
    app.state.arc_receipt_reader = ReceiptReader(session_factory, authorization=authorization)
    # One registry for the process. It holds state about connections this
    # process is serving, so it cannot meaningfully outlive it -- a restart
    # drops every connection, and any record that survived would be a
    # preflight for a caller nobody is on the other end of.
    # Session memory. Unconditional: it needs no key material and no
    # external service, so a deployment either has the tables or does not.
    # Extraction strategies are enabled here rather than inside MemoryService so
    # that a deployment with no provider queues nothing at all: with the no-op
    # provider the queue would otherwise grow, be drained into nothing, and cost
    # a write per event for no result.
    memory_strategies = tuple(STRATEGIES.values()) if settings.extraction_provider != "noop" else ()
    app.state.memory = MemoryService(session_factory, clock=clock, extraction_strategies=memory_strategies)

    # Organization-scope claim predicates. Separate from the tenant-scoped
    # vocabulary service because it takes no tenant context at all.
    app.state.global_vocabulary = GlobalVocabularyService(session_factory, clock=clock)

    # The one path that creates claims. Every invariant a claim carries is a
    # property of this service rather than of the row, so there is deliberately
    # no second construction site.
    app.state.claims = ClaimService(session_factory, clock=clock)

    # Confirmation and calibration. Both constructed unconditionally: they need no
    # key material and no external service, and their metric families have to be
    # registered whether or not anybody has confirmed a claim yet -- a counter that
    # only appears after the first event is one a dashboard cannot chart.
    app.state.confirmations = ConfirmationService(session_factory, app.state.claims, clock=clock)
    app.state.calibration = CalibrationService(session_factory, clock=clock)
    app.state.consolidation = ConsolidationService(session_factory, clock=clock)
    app.state.claim_history = ClaimHistoryService(session_factory)
    # The governed read surface. Everything it returns carries citations and an
    # untrusted-recall label, so no other module needs a claim-reading path of its
    # own -- and a second one would be a second place those guarantees could lapse.
    app.state.claim_serving = ClaimServingService(session_factory, clock=clock)
    # Promotion is the only path from staging into the canonical graph, so it is
    # constructed here rather than per request: a second instance would be a second
    # place the guardrails could be configured differently.
    app.state.promotion = PromotionService(
        session_factory,
        claims=app.state.claims,
        clock=clock,
        # The deployment's configured scanner when there is one, so promotion
        # enforces the same PII policy as every other write path rather than a
        # parallel one of its own.
        pii_scanner=getattr(app.state, "pii_scanner", None),
    )
    app.state.promotion_guardrails = GuardrailService(session_factory, clock=clock)
    app.state.curation_queue = CurationQueueService(session_factory)
    # The loop's return path: what consuming teams need, routed to whoever owns the
    # capability. Constructed here rather than per request so there is one place the
    # lifecycle rules live.
    app.state.capability_requests = CapabilityRequestService(session_factory, clock=clock)
    # Declared authority and the ingest ceiling. Every connector write goes through
    # `admit`, so a source that never declared a tier cannot write at all.
    app.state.source_governance = SourceGovernanceService(session_factory, clock=clock)
    app.state.arc_preflight = PreflightRegistry()
    app.state.arc_artifacts = ArtifactService(session_factory, authorization=authorization, clock=clock)
    app.state.arc_exceptions = ExceptionService(session_factory, authorization=authorization, clock=clock)
    # Deployment-wide and cross-tenant, unlike the two services above: see
    # `ApprovalTrustService`'s own docstring for why it cannot reuse either.
    # The trust root for approvals. Wired unconditionally: registering a
    # verifier is how a deployment acquires one, so gating it on already
    # having one would be circular.
    app.state.arc_verifier_registry = VerifierRegistry(session_factory, clock=clock)
    app.state.arc_approval_trust = ApprovalTrustService(session_factory, authorization=authorization, clock=clock)

    # Resolution is wired only when there is key material behind it. Every
    # resolution signs a receipt and seals the retained response, so without
    # a key it could not produce a receipt it could later stand behind --
    # and the providers refuse rather than emit an unsigned or unsealed one.
    # Left unset, the route answers "not configured on this deployment",
    # which is the truth; wiring it anyway would turn that into a 500 on
    # every call.
    if arc_active_key_id is not None:
        app.state.arc_resolution = ResolutionService(
            session_factory,
            attestation=app.state.arc_attestation,
            challenges=app.state.arc_challenges,
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


class _ArcVisibilityAdapter:
    """Bridges ARC's narrow capability-visibility need to `VisibilityService`.

    ARC asks "which of these capabilities may this actor see" and nothing
    else. Adapting rather than widening ARC's protocol keeps the dependency
    one-directional: ARC never learns the rest of that service's surface,
    and cannot start depending on it.
    """

    def __init__(self, visibility: Any) -> None:
        self._visibility = visibility

    async def visible_capability_ids(self, ctx: Any, capability_ids: Any) -> list[Any]:
        visible: list[Any] = await self._visibility.filter_entities(ctx.tenant, list(capability_ids))
        return visible


async def _assert_embedding_dim_matches(session_factory: Any, settings: Settings) -> None:
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


def wire_auth_context(
    app: FastAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Build the loop-dependent auth trio: oidc_cache, entitlement_client, claim_resolver.

    Called from `lifespan`, not `create_app`, because JIT tenant/actor
    resolution needs a running event loop for the entitlement-service HTTP
    client — constructing `httpx.AsyncClient()` itself doesn't need one, but
    every request that uses it does.
    """
    app.state.oidc_cache = _OidcCache()

    # Entitlement service wiring — only when the deployment has
    # configured ENTITLEMENT_SERVICE_URL. Legacy / test deployments
    # without it skip this path; the middleware fails closed (500
    # "claim resolver not configured") on the first authenticated
    # request, which is the right loud signal during transition.
    if settings.entitlement_service_url:
        app.state.entitlement_client = httpx.AsyncClient()
        bound_fetcher = functools.partial(fetch_entitlements, app.state.entitlement_client)
        app.state.claim_resolver = EntitlementResolver(
            settings=settings,
            session_factory=session_factory,
            fetcher=bound_fetcher,
        )
    else:
        app.state.entitlement_client = None
        app.state.claim_resolver = None


def build_services_container(app: FastAPI) -> Services:
    """Assemble the typed `Services` container from `app.state`.

    Called once, in `lifespan`, after every other field has already been set
    as an individual `app.state.<name>` attribute — by `attach_core_services`,
    `_wire_arc`, `wire_auth_context` (all above), and `registry.wiring.routes`
    (the workspace singleton and the erasure registry, both of which are set
    after the router table is mounted). Reading every field from `app.state`
    rather than closing over the locals that built them keeps this the one
    place that has to agree with every `app.state.<name> = ...` assignment
    across the wiring modules — a field renamed on one side and not the
    other becomes a constructor error here instead of a silent drift between
    the two.
    """
    return Services(
        settings=app.state.settings,
        engine=app.state.engine,
        session_factory=app.state.session_factory,
        clock=app.state.clock,
        scheduler=app.state.scheduler,
        embedder=app.state.embedder,
        vocabulary=app.state.vocabulary,
        schema=app.state.schema,
        visibility=app.state.visibility,
        catalog=app.state.catalog,
        lifecycle=app.state.lifecycle,
        retrieval=app.state.retrieval,
        external_ids=app.state.external_ids,
        adoption=app.state.adoption,
        projections=app.state.projections,
        subscriptions=app.state.subscriptions,
        notifications=app.state.notifications,
        breaking_change=app.state.breaking_change,
        integrations=app.state.integrations,
        interface_storage=app.state.interface_storage,
        includes=app.state.includes,
        memory=app.state.memory,
        global_vocabulary=app.state.global_vocabulary,
        claims=app.state.claims,
        confirmations=app.state.confirmations,
        calibration=app.state.calibration,
        consolidation=app.state.consolidation,
        claim_history=app.state.claim_history,
        claim_serving=app.state.claim_serving,
        promotion=app.state.promotion,
        promotion_guardrails=app.state.promotion_guardrails,
        curation_queue=app.state.curation_queue,
        capability_requests=app.state.capability_requests,
        source_governance=app.state.source_governance,
        arc_signing=app.state.arc_signing,
        arc_authorization=app.state.arc_authorization,
        arc_receipts=app.state.arc_receipts,
        arc_clock=app.state.arc_clock,
        arc_corpus=app.state.arc_corpus,
        arc_challenges=app.state.arc_challenges,
        arc_attestation=app.state.arc_attestation,
        arc_jit=app.state.arc_jit,
        arc_receipt_reader=app.state.arc_receipt_reader,
        arc_preflight=app.state.arc_preflight,
        arc_artifacts=app.state.arc_artifacts,
        arc_exceptions=app.state.arc_exceptions,
        arc_verifier_registry=app.state.arc_verifier_registry,
        arc_approval_trust=app.state.arc_approval_trust,
        # Not yet set by any deployment — see `_wire_arc` — so read with a
        # default instead of the plain attribute access used above, matching
        # how `arc.py` / `arc_admin.py` read it today.
        arc_resolution=getattr(app.state, "arc_resolution", None),
        oidc_cache=app.state.oidc_cache,
        entitlement_client=app.state.entitlement_client,
        claim_resolver=app.state.claim_resolver,
        usage_writer=app.state.usage_writer,
        workspace_service=app.state.workspace_service,
        erasure=app.state.erasure,
    )
