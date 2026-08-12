"""What correct wiring *is*, pinned: the service graph's shape and its shared instances.

Every other gate over the composition root is blind to the failure that
matters most when the root is decomposed. `scripts/export_openapi.py` dumps
the schema with `sort_keys=True`, so `openapi.json` is order-insensitive; the
MCP catalogue snapshot is a set of tool names; mypy sees only that a field of
type `X` holds an `X`. Swap two same-typed services in the container --
`arc_proposals` given the `ProvenanceService` and `arc_provenance` given the
`ProposalService`, say -- and every one of those gates stays green. So does
the suite, until some request path notices it is talking to the wrong object.

Worse, and quieter: give one service its own fresh `SessionFactory`,
`VisibilityService`, or `Clock` instead of the shared one the rest of the
graph holds. Nothing is the wrong *type*, so nothing fails. What breaks is
the property that made sharing deliberate in the first place -- one
connection pool, one tenant-visibility chokepoint, one "now" per request --
and it breaks as a slow leak in production rather than as a red test.

This file is the analog of "byte-identical" for an artifact no byte
comparison can check. It asserts two things about every field the container
declares:

1. **Concrete type.** The field holds the class the container says it does.
2. **Object identity.** The collaborators a service was handed are the same
   objects the container exposes -- `catalog._visibility is services.visibility`,
   not merely "a `VisibilityService`".

Both are written out per field rather than derived by walking
`dataclasses.fields()`. A reflective check would pass the day someone adds a
field and forgets to wire it, because the reflection would simply describe
whatever it found. Spelled out, this file is a readable statement of what
the graph is supposed to be, and it fails when the graph stops being that.

Private attributes are read on purpose. A collaborator stored under `_clock`
is not an implementation detail here: *which object* it is is the whole
contract, and the constructor that set it is the only place that contract is
expressed. Attributes with no sharing semantics (caches, locks, counters,
buffers) are deliberately not pinned.

The container is built the way the app builds it -- through a real
`create_app()` and a real lifespan, which is where `build_services_container`
runs. Only the three startup steps that need a live database are stood down
(see `_build_wired`); every wiring call that constructs a service runs for
real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, dataclass
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from contextplane.api.auth.oidc import _OidcCache
from contextplane.api.container import Services
from contextplane.arc.service.activation import ActivationService
from contextplane.arc.service.approval_challenge import ApprovalChallengeService
from contextplane.arc.service.approval_trust import ApprovalTrustService
from contextplane.arc.service.approved_exceptions import ExceptionService
from contextplane.arc.service.artifact import ArtifactService
from contextplane.arc.service.attestation import AttestationService
from contextplane.arc.service.authorization import ArcAuthorizationService
from contextplane.arc.service.challenge import ChallengeService
from contextplane.arc.service.checkpoint_export import CheckpointExportService
from contextplane.arc.service.corpus import CorpusReader
from contextplane.arc.service.detail_retrieval import JitService
from contextplane.arc.service.drafter import DrafterService
from contextplane.arc.service.enrollment import EnrollmentService
from contextplane.arc.service.integrity import RevisionIntegrityService
from contextplane.arc.service.operational_chain import OperationalChainService
from contextplane.arc.service.preflight import PreflightRegistry
from contextplane.arc.service.proposal import ProposalService
from contextplane.arc.service.provenance import ProvenanceService
from contextplane.arc.service.qualification import QualificationService
from contextplane.arc.service.receipt import ReceiptService
from contextplane.arc.service.receipt_read import ReceiptReader
from contextplane.arc.service.replay_corpus import ReplayCorpusService
from contextplane.arc.service.review_package import ReviewPackageService
from contextplane.arc.service.risk import RiskEnvelopeValidator
from contextplane.arc.service.semantic_tests import SemanticTestService
from contextplane.arc.service.shadow import ShadowService
from contextplane.arc.service.signing import ReceiptSigningProvider
from contextplane.arc.service.source_admission import SourceAdmissionService
from contextplane.arc.service.source_status import SourceStatusService
from contextplane.arc.service.submission import ArtifactMaterialisationService
from contextplane.arc.service.verifier_registry import VerifierRegistry
from contextplane.auth.entitlements.resolver import EntitlementResolver
from contextplane.config import Settings
from contextplane.context.arms import ContextArms
from contextplane.context.receipts import ContextReceiptService
from contextplane.context.references import ReceiptReferenceIndex
from contextplane.context.resolve import ContextResolver
from contextplane.context.resume import ContextResumeService
from contextplane.embedding.stub import StubEmbedder
from contextplane.main import create_app
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
from contextplane.service.governance.visibility import VisibilityService
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
from contextplane.service.notifications.core import NotificationService
from contextplane.service.notifications.subscriptions import SubscriptionService
from contextplane.service.retrieval import RetrievalService
from contextplane.service.workspace import WorkspaceService
from contextplane.signals.ingest import SignalIngestService
from contextplane.types import SystemClock
from contextplane.usage.writer import UsageWriter
from contextplane.wiring import jobs
from contextplane.wiring import services as wiring_services
from contextplane.workspaces.checkpoints import IntentCheckpointService
from contextplane.workspaces.grants import IntentGrantService
from contextplane.workspaces.recall import WorkspaceRecall
from tests.helpers.builders import overridable_settings

# ---------------------------------------------------------------------------
# Building the graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Wired:
    """One `create_app()` result and the container its lifespan produced."""

    app: FastAPI
    services: Services
    settings: Settings


async def _noop_startup_check(*_args: Any, **_kwargs: Any) -> None:
    """Stand-in for a startup assertion that queries the database."""
    return None


async def _start_scheduler_paused(scheduler: AsyncIOScheduler, *_args: Any, **_kwargs: Any) -> None:
    """Stand-in for `jobs.start`: register with the loop, run no job.

    The real function starts the scheduler and then does database work (the
    audit-partition age check, the sync-source job registration). Starting it
    *paused* keeps the shutdown path in `lifespan`'s `finally` block real --
    `scheduler.shutdown()` on a scheduler that was never started raises --
    while no registered job ever fires against a database that is not there.
    """
    scheduler.start(paused=True)


def _build_wired() -> _Wired:
    """Build the app and run its lifespan far enough to produce the container.

    Three startup steps are stood down, and only three, each because it
    reaches for a database this test does not have: the two assertions in
    `lifespan` that open a session, and `jobs.start`. Everything that
    *constructs* a service -- `build_core_services`, `attach_core_services`,
    `build_post_app_services`, `routes.register`, `wire_auth_context`, and
    `build_services_container` itself -- runs exactly as it does in a real
    process. The remaining startup assertion
    (`_assert_drafter_decision_permits_serving`) reads a committed artifact
    off disk rather than the database, so it is left alone and runs for real.

    This couples the file to two private names in `contextplane.wiring.services`.
    That is deliberate: the alternative is a fake session factory whose canned
    results the assertions would have to be taught, which would couple it to
    the assertions' *queries* instead -- a tighter coupling to the same code.
    """
    settings = overridable_settings(
        # Configured so `wire_auth_context` takes its real branch: without an
        # entitlement service URL, `entitlement_client` and `claim_resolver`
        # are both `None` and two container fields go unpinned.
        entitlement_service_url="https://entitlement.test.local",
        entitlement_service_env="DEV",
        entitlement_service_discriminator="REGISTRY",
        entitlement_role_mapping={
            "ADMIN": "admin",
            "PRODUCER": "producer",
            "CONSUMER": "consumer",
            "AUDITOR": "auditor",
        },
    )

    async def _run() -> _Wired:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            return _Wired(app=app, services=app.state.services, settings=settings)

    with ExitStack() as stack:
        stack.enter_context(patch.object(wiring_services, "_assert_embedding_dim_matches", _noop_startup_check))
        stack.enter_context(patch.object(wiring_services, "_assert_no_legacy_activation_evidence", _noop_startup_check))
        stack.enter_context(patch.object(jobs, "start", _start_scheduler_paused))
        return asyncio.run(_run())


@pytest.fixture(scope="module")
def wired() -> Iterator[_Wired]:
    """The wired app, built once for the whole file.

    The lifespan has already exited by the time a test runs -- the engine is
    disposed and the scheduler is down. That is fine and deliberate: every
    assertion here is about which object a field holds, not about calling it.
    """
    yield _build_wired()


@pytest.fixture(scope="module")
def s(wired: _Wired) -> Services:
    """The container itself -- the subject of nearly every assertion below."""
    return wired.services


# ---------------------------------------------------------------------------
# 1. Concrete type, one field at a time
# ---------------------------------------------------------------------------


def test_core_infrastructure_fields_hold_their_declared_types(s: Services) -> None:
    assert isinstance(s.settings, Settings)
    assert isinstance(s.engine, AsyncEngine)
    assert isinstance(s.session_factory, async_sessionmaker)
    assert isinstance(s.clock, SystemClock)
    assert isinstance(s.scheduler, AsyncIOScheduler)
    # `Embedder` is a protocol; the concrete class is the one this
    # deployment's `embedding_provider` selects, "stub" here.
    assert isinstance(s.embedder, StubEmbedder)


def test_catalog_domain_fields_hold_their_declared_types(s: Services) -> None:
    assert isinstance(s.vocabulary, VocabularyService)
    assert isinstance(s.schema, SchemaService)
    assert isinstance(s.visibility, VisibilityService)
    assert isinstance(s.catalog, CatalogService)
    assert isinstance(s.lifecycle, LifecycleService)
    assert isinstance(s.retrieval, RetrievalService)
    assert isinstance(s.external_ids, ExternalIdService)
    assert isinstance(s.adoption, AdoptionService)
    assert isinstance(s.projections, ProjectionService)
    assert isinstance(s.subscriptions, SubscriptionService)
    assert isinstance(s.notifications, NotificationService)
    assert isinstance(s.breaking_change, BreakingChangeAdvisor)
    assert isinstance(s.integrations, IntegrationLookupService)
    assert isinstance(s.interface_storage, InterfaceStorageService)
    assert isinstance(s.includes, IncludeService)


def test_memory_domain_fields_hold_their_declared_types(s: Services) -> None:
    assert isinstance(s.memory, MemoryService)
    assert isinstance(s.global_vocabulary, GlobalVocabularyService)
    assert isinstance(s.claims, ClaimService)
    assert isinstance(s.confirmations, ConfirmationService)
    assert isinstance(s.calibration, CalibrationService)
    assert isinstance(s.consolidation, ConsolidationService)
    assert isinstance(s.claim_history, ClaimHistoryService)
    assert isinstance(s.claim_serving, ClaimServingService)
    assert isinstance(s.promotion, PromotionService)
    assert isinstance(s.promotion_guardrails, GuardrailService)
    assert isinstance(s.curation_queue, CurationQueueService)
    assert isinstance(s.capability_requests, CapabilityRequestService)
    assert isinstance(s.source_governance, SourceGovernanceService)
    assert isinstance(s.source_ingest, SourceIngestService)
    assert isinstance(s.signal_ingest, SignalIngestService)


def test_arc_domain_fields_hold_their_declared_types(s: Services) -> None:
    assert isinstance(s.arc_signing, ReceiptSigningProvider)
    assert isinstance(s.arc_authorization, ArcAuthorizationService)
    assert isinstance(s.arc_receipts, ReceiptService)
    assert isinstance(s.arc_clock, SystemClock)
    assert isinstance(s.arc_corpus, CorpusReader)
    assert isinstance(s.arc_challenges, ChallengeService)
    assert isinstance(s.arc_attestation, AttestationService)
    assert isinstance(s.arc_jit, JitService)
    assert isinstance(s.arc_receipt_reader, ReceiptReader)
    assert isinstance(s.arc_preflight, PreflightRegistry)
    assert isinstance(s.arc_artifacts, ArtifactService)
    assert isinstance(s.arc_exceptions, ExceptionService)
    assert isinstance(s.arc_source_admission, SourceAdmissionService)
    assert isinstance(s.arc_source_status, SourceStatusService)
    assert isinstance(s.arc_proposals, ProposalService)
    assert isinstance(s.arc_provenance, ProvenanceService)
    assert isinstance(s.arc_semantic_tests, SemanticTestService)
    assert isinstance(s.arc_operational_chain, OperationalChainService)
    assert isinstance(s.arc_checkpoint_export, CheckpointExportService)
    assert isinstance(s.arc_risk_envelope, RiskEnvelopeValidator)
    assert isinstance(s.arc_materialisation, ArtifactMaterialisationService)
    assert isinstance(s.arc_drafter, DrafterService)
    assert isinstance(s.arc_verifier_registry, VerifierRegistry)
    assert isinstance(s.arc_approval_trust, ApprovalTrustService)
    assert isinstance(s.arc_enrollment, EnrollmentService)
    assert isinstance(s.arc_review_package, ReviewPackageService)
    assert isinstance(s.arc_approval_challenges, ApprovalChallengeService)
    assert isinstance(s.arc_shadow, ShadowService)
    assert isinstance(s.arc_replay_corpus, ReplayCorpusService)
    assert isinstance(s.arc_qualification, QualificationService)
    assert isinstance(s.arc_integrity, RevisionIntegrityService)
    assert isinstance(s.arc_activation, ActivationService)


def test_task_memory_and_layered_context_fields_hold_their_declared_types(s: Services) -> None:
    assert isinstance(s.intent_checkpoints, IntentCheckpointService)
    assert isinstance(s.intent_grants, IntentGrantService)
    assert isinstance(s.workspace_recall, WorkspaceRecall)
    assert isinstance(s.context_arms, ContextArms)
    assert isinstance(s.context_receipts, ContextReceiptService)
    assert isinstance(s.context_resolver, ContextResolver)
    assert isinstance(s.context_reference_index, ReceiptReferenceIndex)
    assert isinstance(s.context_resume, ContextResumeService)


def test_auth_usage_workspace_and_erasure_fields_hold_their_declared_types(s: Services) -> None:
    assert isinstance(s.oidc_cache, _OidcCache)
    assert isinstance(s.entitlement_client, httpx.AsyncClient)
    assert isinstance(s.claim_resolver, EntitlementResolver)
    assert isinstance(s.usage_writer, UsageWriter)
    assert isinstance(s.workspace_service, WorkspaceService)
    assert isinstance(s.erasure, ErasureRegistry)


def test_arc_resolution_is_none_because_no_deployment_configures_key_material(s: Services) -> None:
    """The one field that is `None` by design rather than by omission.

    Resolution signs a receipt and seals the retained response, so it is
    wired only behind an active ARC key -- and no deployment can configure
    one yet. A future commit that makes keys configurable will make this
    field real; until then, `None` is the wired-correctly answer and a
    service here would mean the graph invented key material.
    """
    assert s.arc_resolution is None


# ---------------------------------------------------------------------------
# 2. One session factory, one clock, one settings object
# ---------------------------------------------------------------------------


def test_one_session_factory_reaches_every_service_that_takes_one(s: Services) -> None:
    """One factory means one connection pool.

    A service handed its own would work in every test and quietly double the
    pool the deployment sized. Two storage attribute names are in play --
    `_session_factory` and `_factory` -- because the two domains named the
    same collaborator differently; both are the same object.
    """
    f = s.session_factory

    assert s.vocabulary._session_factory is f
    assert s.schema._session_factory is f
    assert s.visibility._session_factory is f
    assert s.catalog._session_factory is f
    assert s.lifecycle._session_factory is f
    assert s.retrieval._session_factory is f
    assert s.external_ids._session_factory is f
    assert s.adoption._session_factory is f
    assert s.projections._session_factory is f
    assert s.subscriptions._session_factory is f
    assert s.notifications._session_factory is f
    assert s.breaking_change._session_factory is f
    assert s.integrations._session_factory is f
    assert s.interface_storage._session_factory is f
    assert s.includes._session_factory is f

    assert s.memory._session_factory is f
    assert s.global_vocabulary._session_factory is f
    assert s.claims._session_factory is f
    assert s.confirmations._session_factory is f
    assert s.calibration._session_factory is f
    assert s.consolidation._session_factory is f
    assert s.claim_history._session_factory is f
    assert s.claim_serving._factory is f
    assert s.promotion._factory is f
    assert s.promotion_guardrails._factory is f
    assert s.curation_queue._factory is f
    assert s.capability_requests._factory is f
    assert s.source_governance._factory is f

    assert s.arc_corpus._session_factory is f
    assert s.arc_challenges._session_factory is f
    assert s.arc_jit._session_factory is f
    assert s.arc_receipt_reader._session_factory is f
    assert s.arc_artifacts._session_factory is f
    assert s.arc_exceptions._session_factory is f
    assert s.arc_source_admission._session_factory is f
    assert s.arc_source_status._session_factory is f
    assert s.arc_proposals._session_factory is f
    assert s.arc_provenance._session_factory is f
    assert s.arc_semantic_tests._session_factory is f
    assert s.arc_checkpoint_export._session_factory is f
    assert s.arc_materialisation._session_factory is f
    assert s.arc_drafter._session_factory is f
    assert s.arc_verifier_registry._session_factory is f
    assert s.arc_approval_trust._session_factory is f
    assert s.arc_enrollment._session_factory is f
    assert s.arc_review_package._session_factory is f
    assert s.arc_approval_challenges._session_factory is f
    assert s.arc_shadow._session_factory is f
    assert s.arc_replay_corpus._session_factory is f
    assert s.arc_qualification._session_factory is f
    assert s.arc_activation._session_factory is f

    assert s.intent_checkpoints._session_factory is f
    assert s.intent_grants._session_factory is f
    assert s.workspace_recall._session_factory is f
    assert s.context_arms._session_factory is f
    assert s.context_receipts._session_factory is f
    assert s.context_reference_index._session_factory is f
    assert s.context_resume._session_factory is f

    assert s.usage_writer._session_factory is f
    assert s.workspace_service._session_factory is f
    assert s.claim_resolver is not None
    assert s.claim_resolver._session_factory is f


def test_one_clock_stamps_every_service_that_takes_one(s: Services) -> None:
    """One clock means one "now".

    Resolution assembles a corpus and then evaluates it; two clocks let a
    revision become effective between those two steps. The same reasoning
    holds for every write path that stamps a row.
    """
    clock = s.clock

    # `arc_clock` is not a second clock -- it is the same object, exposed
    # under the name ARC's own readers use.
    assert s.arc_clock is clock

    assert s.schema._clock is clock
    assert s.visibility._clock is clock
    assert s.catalog._clock is clock
    assert s.lifecycle._clock is clock
    assert s.retrieval._clock is clock
    assert s.external_ids._clock is clock
    assert s.adoption._clock is clock
    assert s.projections._clock is clock
    assert s.subscriptions._clock is clock
    assert s.notifications._clock is clock
    assert s.breaking_change._clock is clock
    assert s.interface_storage._clock is clock

    assert s.memory._clock is clock
    assert s.global_vocabulary._clock is clock
    assert s.claims._clock is clock
    assert s.confirmations._clock is clock
    assert s.calibration._clock is clock
    assert s.consolidation._clock is clock
    assert s.claim_serving._clock is clock
    assert s.promotion._clock is clock
    assert s.promotion_guardrails._clock is clock
    assert s.capability_requests._clock is clock
    assert s.source_governance._clock is clock

    assert s.arc_receipts._clock is clock
    assert s.arc_challenges._clock is clock
    assert s.arc_attestation._clock is clock
    assert s.arc_jit._clock is clock
    assert s.arc_artifacts._clock is clock
    assert s.arc_exceptions._clock is clock
    assert s.arc_source_admission._clock is clock
    assert s.arc_source_status._clock is clock
    assert s.arc_proposals._clock is clock
    assert s.arc_provenance._clock is clock
    assert s.arc_semantic_tests._clock is clock
    assert s.arc_operational_chain._clock is clock
    assert s.arc_checkpoint_export._clock is clock
    assert s.arc_materialisation._clock is clock
    assert s.arc_drafter._clock is clock
    assert s.arc_verifier_registry._clock is clock
    assert s.arc_approval_trust._clock is clock
    assert s.arc_enrollment._clock is clock
    assert s.arc_replay_corpus._clock is clock
    assert s.arc_qualification._clock is clock
    assert s.arc_integrity._clock is clock
    assert s.arc_activation._clock is clock

    assert s.intent_checkpoints._clock is clock
    assert s.intent_grants._clock is clock
    assert s.context_receipts._clock is clock
    assert s.context_resume._clock is clock

    # The workspace singleton is the one service that used to answer this
    # assertion `False`: `_build_workspace_service` called `SystemClock()`
    # itself while taking every other collaborator off `app.state`. It now
    # takes the clock off state too, so the exception is gone and this line
    # is what keeps it gone.
    assert s.workspace_service._clock is clock


def test_the_workspace_service_shares_the_graph_it_is_built_from(s: Services) -> None:
    """The workspace singleton is built outside the area builders, not outside the graph.

    `_build_workspace_service` in `contextplane/api/routers/workspaces.py`
    constructs it from `app.state` after the router table is mounted, rather
    than in an area's `build_<area>_services`. That is a sequencing
    difference, and it must not become a graph difference: every
    collaborator it holds is the shared instance, the clock it stamps its
    audit trail from included.
    """
    assert s.workspace_service._clock is s.clock
    assert s.workspace_service._session_factory is s.session_factory
    assert s.workspace_service._visibility_svc is s.visibility


def test_one_settings_object_is_the_one_the_app_was_built_from(wired: _Wired, s: Services) -> None:
    """Settings is a frozen snapshot; two of them is two configurations.

    The drafter is the one service that keeps a reference rather than reading
    a value out at construction time, so it is where a second `Settings`
    would actually show up.
    """
    assert s.settings is wired.settings
    assert s.arc_drafter._settings is s.settings


# ---------------------------------------------------------------------------
# 3. Shared sub-services: one instance at every consumption site
# ---------------------------------------------------------------------------


def test_one_visibility_service_is_the_cross_tenant_chokepoint(s: Services) -> None:
    """The single most load-bearing identity in this file.

    Cross-tenant filtering is enforced at one layer. A service holding a
    second `VisibilityService` still filters -- so no test fails -- but it
    filters through an object that a policy change applied to the shared one
    would never reach.
    """
    v = s.visibility

    assert s.catalog._visibility is v
    assert s.retrieval._visibility is v
    assert s.adoption._visibility is v
    assert s.projections._visibility is v
    assert s.subscriptions._visibility is v
    assert s.breaking_change._visibility is v
    assert s.integrations._visibility is v
    assert s.interface_storage._visibility is v
    assert s.includes._visibility is v
    assert s.workspace_service._visibility_svc is v

    # ARC reaches the same chokepoint through an adapter rather than
    # directly, so the identity to assert is one level in.
    assert s.arc_authorization._visibility._visibility is v


def test_catalog_writes_go_through_one_catalog_service(s: Services) -> None:
    """Lifecycle delegates `replaced_by` edge creation and ingest provisions
    entities -- both through the instance every other write path uses."""
    assert s.lifecycle._catalog is s.catalog
    assert s.source_ingest._catalog is s.catalog


def test_claims_are_written_through_one_claim_service(s: Services) -> None:
    """Every invariant a claim carries is a property of this service rather
    than of the row, so a second instance would be a second rule set."""
    assert s.confirmations._claims is s.claims
    assert s.promotion._claims is s.claims
    assert s.source_ingest._claims is s.claims

    # Consolidation is the documented exception: its constructor builds a
    # `ClaimService` when the caller supplies none, and the wiring supplies
    # none. That is deliberate -- the service is a stateless wrapper over the
    # session factory and the clock, so a second construction is not a second
    # place its invariants could drift. What must stay true is that the second
    # instance is built over the *same* two collaborators; a `ClaimService`
    # over a different factory would be a genuinely different writer.
    assert s.consolidation._claims is not s.claims
    assert s.consolidation._claims._session_factory is s.session_factory
    assert s.consolidation._claims._clock is s.clock


def test_retrieval_and_embedding_are_shared_rather_than_rebuilt(s: Services) -> None:
    """A second `RetrievalService` would mean a second embedding cache, and a
    second embedder would mean a second model load."""
    assert s.breaking_change._retrieval is s.retrieval
    assert s.context_arms._retrieval is s.retrieval
    assert s.retrieval._embedder is s.embedder


def test_interface_reads_and_ingest_governance_share_their_owners(s: Services) -> None:
    assert s.includes._interface_storage is s.interface_storage
    assert s.source_ingest._governance is s.source_governance
    # Both ingest paths ask the same governance service. A second one would spend
    # a separate ceiling, so a source at its limit could still write by choosing
    # the other entry point.
    assert s.signal_ingest._governance is s.source_governance


def test_signal_ingest_shares_the_apps_session_factory_and_clock(s: Services) -> None:
    """It used to be built per request from these two, so pinning them is what
    proves the move preserved the collaborators rather than re-deriving them."""
    assert s.signal_ingest._session_factory is s.session_factory
    assert s.signal_ingest._clock is s.clock


def test_adoption_auto_subscribes_through_the_wired_subscription_service(s: Services) -> None:
    """`adoption.auto_subscribe` is a closure over `SubscriptionService`.

    `adoption_hook()` returns a nested function rather than a bound method,
    so the shared instance is reachable only through the closure cell -- the
    identity still matters, and this is where it lives. Adoption creating an
    inbox-only subscription through a *second* service would write the same
    rows past a different set of hooks.
    """
    hook: Callable[..., Any] = s.adoption._auto_subscribe
    captured = [cell.cell_contents for cell in (hook.__closure__ or ())]
    assert any(value is s.subscriptions for value in captured)


def test_one_authorization_chokepoint_guards_every_arc_write(s: Services) -> None:
    """ARC's global-write allowlist is configuration read once. A service
    holding its own `ArcAuthorizationService` would carry whatever allowlist
    existed when it was built, forever."""
    a = s.arc_authorization

    assert s.arc_receipt_reader._authorization is a
    assert s.arc_artifacts._authorization is a
    assert s.arc_exceptions._authorization is a
    assert s.arc_source_admission._authorization is a
    assert s.arc_proposals._authorization is a
    assert s.arc_provenance._authorization is a
    assert s.arc_semantic_tests._authorization is a
    assert s.arc_materialisation._authorization is a
    assert s.arc_drafter._authorization is a
    assert s.arc_approval_trust._authorization is a
    assert s.arc_enrollment._authorization is a
    assert s.arc_review_package._authorization is a
    assert s.arc_approval_challenges._authorization is a
    assert s.arc_replay_corpus._authorization is a
    assert s.arc_qualification._authorization is a
    assert s.arc_activation._authorization is a


def test_the_arc_signing_and_receipt_chain_is_one_chain(s: Services) -> None:
    """The signer, the receipt writer built on it, and the JIT service that
    issues through that writer -- one line, not three parallel ones."""
    assert s.arc_receipts._signing is s.arc_signing
    assert s.arc_jit._receipts is s.arc_receipts


def test_the_operational_chain_appender_is_one_appender(s: Services) -> None:
    """One process-wide chain: every appender shares this module's signing
    key, so two instances would interleave two chains under one identity."""
    chain = s.arc_operational_chain

    assert s.arc_source_status._operational_chain_appender is chain
    assert s.arc_materialisation._operational_chain_appender is chain
    assert s.arc_integrity._operational_chain_service is chain


def test_the_arc_read_path_composes_one_instance_of_each_collaborator(s: Services) -> None:
    """Integrity, the review package, source status, and the corpus are each
    consumed at several sites. Every site holds the same object -- which is
    what makes an integrity verdict at one site mean the same thing at the
    next."""
    assert s.arc_integrity._review_package_service is s.arc_review_package
    assert s.arc_integrity._source_status_service is s.arc_source_status

    assert s.arc_corpus._integrity is s.arc_integrity
    assert s.arc_activation._integrity is s.arc_integrity

    assert s.arc_activation._review_package is s.arc_review_package
    assert s.arc_activation._source_status is s.arc_source_status
    assert s.arc_activation._artifacts is s.arc_artifacts

    assert s.arc_qualification._review_package is s.arc_review_package
    assert s.arc_qualification._shadow is s.arc_shadow
    assert s.arc_qualification._replay_corpus is s.arc_replay_corpus

    assert s.arc_approval_challenges._review_package_service is s.arc_review_package

    # The shadow overlay must be built on the corpus reader every other
    # reader uses, not on a parallel read path of its own.
    assert s.arc_shadow._corpus is s.arc_corpus

    assert s.arc_drafter._source_admission is s.arc_source_admission
    assert s.arc_drafter._source_status is s.arc_source_status

    assert s.arc_materialisation._risk_envelope_validator is s.arc_risk_envelope


def test_layered_context_resolves_over_one_set_of_arms(s: Services) -> None:
    """Two composers built independently could disagree about which service
    answers a block, and the resolved envelope would look identical either
    way."""
    assert s.context_arms._claims is s.claim_serving
    assert s.context_arms._arc_receipts is s.arc_receipt_reader
    assert s.context_arms._recall is s.workspace_recall

    assert s.context_resolver._arms is s.context_arms
    assert s.context_resolver._receipts is s.context_receipts


# ---------------------------------------------------------------------------
# 4. The five documented post-startup-swap keys
# ---------------------------------------------------------------------------


def test_the_five_post_startup_swap_keys_still_have_their_app_state_seam(wired: _Wired) -> None:
    """Five keys are read live off `app.state`, deliberately not through the
    container, and each one's reason is recorded in `scripts/check_state_access.py`:
    middleware and the MCP transport resolve tenants against `settings`,
    `claim_resolver`, and `oidc_cache`; the idempotency middleware and several
    unit tests read `session_factory`; durability and overhead tests install a
    different `usage_writer` on an already-running app.

    The container is a frozen snapshot taken at startup, so those readers
    need the attribute itself to keep existing. At startup the two agree,
    and that is what this pins: the seam is present and points at the
    container's object. A decomposition that stops attaching one of these
    breaks a reader no type checker is watching.
    """
    app_state = wired.app.state
    s = wired.services

    assert app_state.settings is s.settings
    assert app_state.session_factory is s.session_factory
    assert app_state.usage_writer is s.usage_writer
    assert app_state.oidc_cache is s.oidc_cache
    assert app_state.claim_resolver is s.claim_resolver


# ---------------------------------------------------------------------------
# 5. The container is the app's, and it is frozen
# ---------------------------------------------------------------------------


def test_the_container_is_reachable_where_every_handler_reads_it(wired: _Wired) -> None:
    """`services(request)` reads `request.app.state.services`; the whole file
    is about that one object, so it is worth stating that it is that one."""
    assert wired.app.state.services is wired.services
    assert isinstance(wired.services, Services)


def test_the_container_cannot_be_reassigned_out_from_under_a_handler(s: Services) -> None:
    """Frozen for a reason: the service graph does not change after startup,
    and a request handler swapping a field would swap it for every other
    handler sharing the app."""
    with pytest.raises(FrozenInstanceError):
        s.catalog = s.catalog  # type: ignore[misc]
