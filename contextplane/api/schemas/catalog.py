"""Pydantic request/response models for the producer and consumer catalog surfaces.

These models are the only type seam between HTTP and the service layer.
Routers are thin adapters over CatalogService / RetrievalService.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from contextplane.api.schemas.common import Links
from contextplane.service.catalog.expansions import (
    EntityCollectionExpansion,
    ExternalIdsExpansion,
    InterfaceExpansion,
)


class CreateCapabilityRequest(BaseModel):
    """Body for ``POST /v1/capabilities`` -- creates a top-level, adoptable entity."""

    name: str
    entity_type: Literal["capability"] = "capability"
    external_id: str | None = None
    capability_type: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime.datetime | None = None


class CreateConceptRequest(BaseModel):
    """Body for creating a concept entity; `parent_capability_id` links it via a `concept_of` edge."""

    name: str
    entity_type: Literal["concept"] = "concept"
    external_id: str | None = None
    parent_capability_id: uuid.UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime.datetime | None = None


class CreateOperationRequest(BaseModel):
    """Body for creating an operation entity; `parent_capability_id` links it via an `operation_of` edge."""

    name: str
    entity_type: Literal["operation"] = "operation"
    external_id: str | None = None
    parent_capability_id: uuid.UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime.datetime | None = None


class UpdateEntityRequest(BaseModel):
    """Bag of attribute updates applied bi-temporally; does not change entity_type or name directly."""

    updates: dict[str, Any]
    valid_from: datetime.datetime | None = None


class SetVisibilityRequest(BaseModel):
    """Body for PATCH /v1/capabilities/{entity_id}/visibility.

    ``visibility`` must be one of: ``private``, ``tenant-shared``, ``public``.
    ``shared_with_tenants`` is required (non-empty) when ``visibility='tenant-shared'``;
    validation is enforced at the service layer (VisibilityService) and surfaced as HTTP 422.
    """

    visibility: str
    shared_with_tenants: list[uuid.UUID] | None = None


class CapabilityResponse(BaseModel):
    """Response for the capability create/update routes.

    The bare record, without list-envelope or expansion fields.
    """

    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    external_id: str | None
    lifecycle: str
    attributes: dict[str, Any]
    created_at: datetime.datetime


class EntityDetailResponse(BaseModel):
    """Detail-GET response for concept and operation entities.

    Extends the base capability-record shape with HATEOAS navigation pointers.
    ``_links.self`` is always populated; ``_links.parent`` is populated when the
    entity carries a parent_capability_id (concept_of / operation_of edge) and
    that id is already present in the response — no extra fetch is performed.
    """

    entity_id: uuid.UUID
    name: str
    external_id: str | None
    lifecycle: str
    attributes: dict[str, Any]
    created_at: datetime.datetime
    links: Links | None = Field(default=None, alias="_links")

    # Audit-only — set by the handler when ?view=audit.
    tenant_id: uuid.UUID | None = None

    model_config = {"populate_by_name": True}


class CreateArtifactRequest(BaseModel):
    """Body for ``POST /v1/capabilities/{id}/artifacts``.

    ``title`` is required and validated server-side (1-200 chars, no
    leading/trailing whitespace). ``body_format`` is one of
    ``markdown`` (default), ``html``, ``plain``.
    """

    category: str
    title: str
    body: str
    body_format: str = "markdown"
    valid_from: datetime.datetime | None = None


class ArtifactListResponse(BaseModel):
    """Paginated artifact list. Same envelope shape as CapabilityListResponse.

    ``items`` carry the artifact rows shaped per the ``?fields=`` param;
    callers that want the full body must opt in via
    ``?fields=fact_id,category,title,body_format,created_at,body``.

    ``next_cursor`` is ``None`` when no further pages exist; pass it as
    ``cursor=`` on the next request to retrieve the following page.
    """

    items: list[ArtifactResponse]
    next_cursor: str | None


class ArtifactResponse(BaseModel):
    """An artifact (fact) attached to a capability.

    By default returns the UI-flavoured shape: the fact identifier, the
    category vocabulary value, the body, and when it was ingested. The
    bitemporal columns + tenant/entity FKs are audit-only and present
    only when ``?view=audit`` is passed (route-level ``exclude_unset``
    strips them otherwise).
    """

    fact_id: uuid.UUID
    # `category` and `created_at` are conceptually always present, but they're
    # Optional in the schema so the sparse `?fields=` projection can omit them.
    # `fact_id` is always included.
    category: str | None = None
    title: str | None = None
    body: str | None = None  # excluded by default in list responses unless ?fields=...,body
    body_format: str | None = None
    created_at: datetime.datetime | None = None  # source: t_ingested_at
    created_by_display_name: str | None = None

    # Audit-only fields — set by the handler when ?view=audit.
    # Field names drop the storage-side `t_` prefix; that's DB
    # nomenclature, not an API contract.
    tenant_id: uuid.UUID | None = None
    entity_id: uuid.UUID | None = None
    is_authoritative: bool | None = None
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None

    # HATEOAS-style navigation pointers (T08).
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Consumer read surface
# ---------------------------------------------------------------------------


class CitationItem(BaseModel):
    """A resolvable handle to the evidence behind a result, not the evidence itself.

    Mirrors the citation shape the claim surface returns, so an agent reading
    either one drills in the same way: identify the source, then fetch it.
    """

    fact_id: uuid.UUID
    category: str | None = None
    title: str | None = None
    created_at: datetime.datetime | None = None

    # Where to read the cited artifact in full.
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class SearchResultItem(BaseModel):
    """One search hit, with the evidence that made it match.

    ``citations`` names the artifacts that matched rather than embedding them.
    Bodies are documents; returning all of them inside a list response ships
    content the caller did not ask for, and each citation carries the link to
    read the one they want.

    ``tenant_id`` is audit-only, like everywhere else this shape appears.
    """

    entity_id: uuid.UUID
    name: str
    entity_type: str
    score: float
    retrieval_arms: dict[str, float]
    citations: list[CitationItem]

    # Audit-only — set by the handler when ?view=audit.
    tenant_id: uuid.UUID | None = None
    matching_facts: list[ArtifactResponse] | None = None


class SearchResponse(BaseModel):
    """Response for the search endpoint -- bounded by `top_k`, so `total` is an exact count, not an estimate."""

    # `items` is the standard envelope field name for list payloads.
    # `total` is kept here because search results are bounded by `top_k` —
    # there is no cursor-based next page, so a total count is accurate and cheap.
    items: list[SearchResultItem]
    total: int
    took_ms: float


class EdgeRefItem(BaseModel):
    """An edge between two entities.

    UI shape by default (edge id, both endpoints, relation, properties).
    Bitemporal cols + tenant_id are audit-only — present only when the
    caller passes ``?view=audit``.
    """

    edge_id: uuid.UUID
    src_entity_id: uuid.UUID
    rel: str
    dst_entity_id: uuid.UUID
    properties: dict[str, Any] | None

    # Audit-only fields — set by the handler when ?view=audit.
    tenant_id: uuid.UUID | None = None
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None


class CapabilityDetailResponse(BaseModel):
    """Capability record serialised for the consumer GET endpoint.

    Default shape is UI-flavoured. Audit-only fields (``tenant_id``,
    ``is_active``, ``superseded_facts_count``, ``as_of``) are present
    only when the caller passes ``?view=audit``; route-level
    ``response_model_exclude_unset`` strips them otherwise.

    The ``components``, ``depends_on``, ``external_ids``, and
    ``interface`` fields are populated only when the corresponding
    value appears in the ``?include=`` query parameter.
    """

    entity_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None
    created_at: datetime.datetime
    lifecycle: str
    attributes: dict[str, Any]
    facts: list[ArtifactResponse]
    edges_out: list[EdgeRefItem]
    edges_in: list[EdgeRefItem]

    # Audit-only fields — set by the handler when ?view=audit.
    tenant_id: uuid.UUID | None = None
    is_active: bool | None = None
    superseded_facts_count: int | None = None
    as_of: datetime.datetime | None = None

    # HATEOAS-style navigation pointers (T08).
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}
    components: EntityCollectionExpansion | None = None
    depends_on: EntityCollectionExpansion | None = None
    external_ids: ExternalIdsExpansion | None = None
    interface: InterfaceExpansion | None = None


class DependencyResponse(BaseModel):
    """Response for the dependency-traversal endpoint -- edges reachable from `root_entity_id` within `depth`."""

    root_entity_id: uuid.UUID
    depth: int
    as_of: datetime.datetime | None
    edges: list[EdgeRefItem]


class AdoptionResponse(BaseModel):
    """An adoption event linking a consumer to a provider capability.

    Default shape is UI-flavoured (core identifiers and intent fields only).
    Bitemporal columns are audit-only — present only when the caller passes
    ``?view=audit``. Route-level ``response_model_exclude_unset`` strips
    unset audit fields so they don't appear as null keys in default responses.
    """

    adoption_id: uuid.UUID
    tenant_id: uuid.UUID
    provider_capability_id: uuid.UUID
    consumer_tenant_id: uuid.UUID
    actor_id: uuid.UUID | None
    intent: str | None
    version_pin: str | None

    # Audit-only fields — set by the handler when ?view=audit.
    # Field names drop the storage-side `t_` prefix; that's DB nomenclature,
    # not an API contract.
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None

    # HATEOAS-style navigation pointers: self + capability pointer.
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class SubscriptionResponse(BaseModel):
    """A subscription that watches events on a capability.

    Default shape is UI-flavoured (core subscription fields only). Bitemporal
    columns are audit-only — present only when the caller passes ``?view=audit``.
    Route-level ``response_model_exclude_unset`` strips unset audit fields so
    they don't appear as null keys in default responses.
    """

    subscription_id: uuid.UUID
    tenant_id: uuid.UUID
    actor_id: uuid.UUID | None
    capability_id: uuid.UUID
    event_kinds: list[str]
    webhook_url: str | None
    webhook_hmac_secret_ref: str | None
    is_enabled: bool
    digest_window: str

    # Audit-only fields — set by the handler when ?view=audit.
    # Field names drop the storage-side `t_` prefix; that's DB nomenclature,
    # not an API contract.
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None

    # HATEOAS-style navigation pointers: self + capability pointer.
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class InterfaceReadResponse(BaseModel):
    """GET /v1/capabilities/{id}/interface response.

    Default shape exposes the canonical surface, source, format, and time-travel
    ``as_of``. Bitemporal row metadata is audit-only — present only when the
    caller passes ``?view=audit``. Route-level ``response_model_exclude_unset``
    strips unset audit fields so they don't appear as null keys in default
    responses.
    """

    capability_id: str
    interface_canonical: Any | None
    interface_source: dict[str, Any] | None
    interface_format: str | None
    as_of: str | None

    # Audit-only fields — set by the handler when ?view=audit.
    # Field names drop the storage-side `t_` prefix; that's DB nomenclature,
    # not an API contract.
    valid_from: datetime.datetime | None = None
    valid_to: datetime.datetime | None = None
    ingested_at: datetime.datetime | None = None
    invalidated_at: datetime.datetime | None = None

    # HATEOAS-style navigation pointers: self + capability pointer.
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class EntityRefItem(BaseModel):
    """An entity as it appears in a list or as a graph node.

    ``tenant_id`` and ``is_active`` are audit-only. A caller reading its own
    tenant's data learns nothing from being told whose data it is, and every
    surface returning this shape has already filtered inactive rows out.
    """

    entity_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None
    created_at: datetime.datetime

    # Audit-only — set by the handler when ?view=audit.
    tenant_id: uuid.UUID | None = None
    is_active: bool | None = None


class CapabilityListResponse(BaseModel):
    """Paginated list envelope for GET /v1/capabilities; `next_cursor` is None once the last page is returned."""

    items: list[EntityRefItem]
    next_cursor: str | None


class AdoptionListResponse(BaseModel):
    """Paginated list envelope for GET /v1/capabilities/{id}/adoptions.

    Cursor wiring: envelope-only. The adoption set for a single capability is
    small (one active adoption per consumer tenant), so ``next_cursor`` is
    always ``None`` in practice. The wrapper exists for shape consistency with
    every other list endpoint.
    """

    items: list[AdoptionResponse]
    next_cursor: str | None


class SubscriptionListResponse(BaseModel):
    """Paginated list envelope for GET /v1/capabilities/{id}/subscriptions.

    Cursor wiring: envelope-only. Subscriptions per capability per tenant are
    bounded (typically 1–5), so ``next_cursor`` is always ``None`` in practice.
    The wrapper exists for shape consistency.
    """

    items: list[SubscriptionResponse]
    next_cursor: str | None


class IntegrationListResponse(BaseModel):
    """Paginated list envelope for GET /v1/integrations.

    Cursor wiring: envelope-only. Integrations connecting two specific
    capabilities are bounded (typically 1–3), so ``next_cursor`` is always
    ``None`` in practice. The wrapper exists for shape consistency.
    """

    items: list[EntityRefItem]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Graph traversal
# ---------------------------------------------------------------------------


class TraversalResultResponse(BaseModel):
    """HTTP response shape for graph traversal endpoints.

    Maps one-to-one to TraversalResult; all UUID fields serialised as strings
    by Pydantic's default JSON encoder.
    """

    root_entity_id: uuid.UUID
    depth: int
    direction: str
    as_of: datetime.datetime | None
    nodes: list[EntityRefItem]
    edges: list[EdgeRefItem]
    version_satisfied: dict[str, bool]  # edge_id (str) → predicate result
    cache_hit: bool


# ---------------------------------------------------------------------------
# Provider/Consumer projections
# ---------------------------------------------------------------------------


class ProjectionResponse(BaseModel):
    """HTTP response shape for GET /v1/graph/provider and /v1/graph/consumer.

    Maps one-to-one to ``contextplane.service.platform.projections.Projection``.
    ``next_cursor`` is None when no further pages exist; pass it as ``cursor=``
    on the next request to retrieve the following page.
    """

    nodes: list[EntityRefItem]
    edges: list[EdgeRefItem]
    next_cursor: str | None


__all__ = [
    "AdoptionListResponse",
    "AdoptionResponse",
    "ArtifactListResponse",
    "ArtifactResponse",
    "CapabilityDetailResponse",
    "CapabilityListResponse",
    "CapabilityResponse",
    "CitationItem",
    "CreateArtifactRequest",
    "CreateCapabilityRequest",
    "CreateConceptRequest",
    "CreateOperationRequest",
    "DependencyResponse",
    "EdgeRefItem",
    "EntityDetailResponse",
    "EntityRefItem",
    "IntegrationListResponse",
    "InterfaceReadResponse",
    "ProjectionResponse",
    "SearchResponse",
    "SearchResultItem",
    "SetVisibilityRequest",
    "SubscriptionListResponse",
    "SubscriptionResponse",
    "TraversalResultResponse",
    "UpdateEntityRequest",
]
