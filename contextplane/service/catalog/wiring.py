"""Catalog-area service construction: the capability graph and its read surfaces.

One registration entry point per area, called by the composition root — see
`contextplane.service.governance.wiring` for the shape and why cross-area
collaborators arrive as explicit parameters rather than being rebuilt here.

Three of those parameters are worth naming. `visibility` is the cross-tenant
chokepoint every read path funnels through. `retrieval` is what the
breaking-change advisor searches with, and rebuilding it would mean a second
embedding cache. `subscriptions` is what adoption auto-subscribes through, so
an adoption's inbox-only subscription is created by the same service every
other subscription write goes through.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.config import Settings
from contextplane.entities.validation import EntityValidator
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
from contextplane.service.governance.visibility import VisibilityService
from contextplane.service.notifications.subscriptions import SubscriptionService
from contextplane.service.retrieval import RetrievalService
from contextplane.types import Clock


@dataclass(frozen=True)
class CatalogServices:
    """What this area contributes to the typed container, by container field name."""

    vocabulary: VocabularyService
    schema: SchemaService
    catalog: CatalogService
    lifecycle: LifecycleService
    external_ids: ExternalIdService
    adoption: AdoptionService
    projections: ProjectionService
    breaking_change: BreakingChangeAdvisor
    integrations: IntegrationLookupService
    interface_storage: InterfaceStorageService
    includes: IncludeService
    # Organization-scope claim predicates. A sibling of the tenant-scoped
    # vocabulary service above rather than a memory-domain service, because it
    # answers what a predicate *means* across the catalog; it takes no tenant
    # context at all.
    global_vocabulary: GlobalVocabularyService


def build_catalog_services(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    visibility: VisibilityService,
    retrieval: RetrievalService,
    subscriptions: SubscriptionService,
) -> CatalogServices:
    """Construct the catalog area's services, in dependency order."""
    vocabulary = VocabularyService(session_factory)
    # The profile validator is built here rather than taken as a parameter:
    # it needs a session factory and nothing any other area produces, and
    # every entity write in this area resolves through it.
    schema = SchemaService(session_factory, clock, validator=EntityValidator(session_factory))
    catalog = CatalogService(
        session_factory,
        clock,
        vocabulary,
        schema,
        visibility=visibility,
        chunk_tokens=settings.embedding_chunk_tokens,
    )
    interface_storage = InterfaceStorageService(
        session_factory=session_factory,
        clock=clock,
        visibility=visibility,
    )
    return CatalogServices(
        vocabulary=vocabulary,
        schema=schema,
        catalog=catalog,
        # Injected so `LifecycleService` delegates `replaced_by` edge creation
        # through the public `CatalogService.create_edge()` API rather than
        # writing edges of its own.
        lifecycle=LifecycleService(session_factory, clock, catalog=catalog),
        external_ids=ExternalIdService(session_factory, clock),
        adoption=AdoptionService(
            session_factory=session_factory,
            clock=clock,
            visibility=visibility,
            auto_subscribe=subscriptions.adoption_hook(),
        ),
        projections=ProjectionService(
            session_factory=session_factory,
            clock=clock,
            visibility=visibility,
        ),
        breaking_change=BreakingChangeAdvisor(
            session_factory=session_factory,
            clock=clock,
            retrieval=retrieval,
            visibility=visibility,
        ),
        integrations=IntegrationLookupService(
            session_factory=session_factory,
            visibility=visibility,
        ),
        interface_storage=interface_storage,
        includes=IncludeService(
            session_factory=session_factory,
            visibility=visibility,
            interface_storage=interface_storage,
        ),
        global_vocabulary=GlobalVocabularyService(session_factory, clock=clock),
    )
