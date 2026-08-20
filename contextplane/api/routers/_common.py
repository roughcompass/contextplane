"""Shared helpers for entity-shaped routers (capability/concept/operation/artifact).

``get_service`` and ``to_response`` were originally defined as private
``_service`` and ``_to_response`` in ``capabilities.py`` and imported
across module boundaries by ``concepts.py``, ``operations.py``, and
``artifacts.py``. The underscore prefix claimed module-private; the
cross-module imports proved the symbols were de-facto shared. Promoted
here with an explicit ``__all__`` so the contract is intentional.

``edge_to_item`` is similarly shared: capabilities.py uses it for inline
``edges_out`` / ``edges_in`` lists and graph.py uses it for traversal
result edges. Both honour the same ``?view=audit`` contract.

``fact_to_artifact`` and ``entity_ref_to_item`` join them for the same reason.
Every surface that emits a fact or an entity owes the caller the same shape, and
the endpoints that hand-rolled their own each drifted: one returned storage
columns and full bodies unconditionally, another the tenant identifier by
default, a third omitted the one timestamp the default shape is supposed to
carry. Sharing the serialiser is what makes the shape a property of the type
rather than of whoever wrote the handler.

``ViewParam`` is here for the matching reason. The parameter was declared as a
bare string on fifteen operations and validated by ten hand-written copies of the
same comparison, one of which was missing — so a value rejected everywhere else
was accepted there. As a literal, the set of accepted values is declared once,
enforced by the framework, and published in the schema, which is where a
generated client can pick it up instead of restating it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Query, Request

from contextplane.api.schemas.catalog import (
    ArtifactResponse,
    CapabilityResponse,
    CitationItem,
    EdgeRefItem,
    EntityRefItem,
    SearchResultItem,
)
from contextplane.api.schemas.common import Links
from contextplane.service.catalog.core import CatalogService
from contextplane.types import CapabilityRecord, EdgeRef, EntityRef, SearchResult

ViewParam = Annotated[
    Literal["default", "audit"],
    Query(
        description=(
            "Response shape. `default` returns the fields a caller acts on. "
            "`audit` adds the bitemporal columns and the owning tenant, for "
            "reconstructing what the record looked like and when."
        )
    ),
]


def get_service(request: Request) -> CatalogService:
    """Return the ``CatalogService`` instance attached to the running app."""
    service: CatalogService = request.app.state.catalog
    return service


def to_response(record: CapabilityRecord) -> CapabilityResponse:
    """Convert a ``CapabilityRecord`` to the basic ``CapabilityResponse`` shape."""
    return CapabilityResponse(
        entity_id=record.entity.entity_id,
        tenant_id=record.entity.tenant_id,
        name=record.entity.name,
        external_id=record.entity.external_id,
        lifecycle=record.lifecycle,
        attributes=record.attributes,
        created_at=record.entity.created_at,
    )


def edge_to_item(edge: EdgeRef, *, audit: bool = False) -> EdgeRefItem:
    """Convert an EdgeRef to the response item.

    Default shape is UI-flavoured (no bitemporal cols, no tenant_id).
    Pass ``audit=True`` to populate the full audit shape — used by
    ``?view=audit`` on any endpoint that emits edges.
    """
    if audit:
        return EdgeRefItem(
            edge_id=edge.edge_id,
            tenant_id=edge.tenant_id,
            src_entity_id=edge.src_entity_id,
            rel=edge.rel,
            dst_entity_id=edge.dst_entity_id,
            properties=edge.properties,
            valid_from=edge.t_valid_from,
            valid_to=edge.t_valid_to,
            ingested_at=edge.t_ingested_at,
            invalidated_at=edge.t_invalidated_at,
        )
    return EdgeRefItem(
        edge_id=edge.edge_id,
        src_entity_id=edge.src_entity_id,
        rel=edge.rel,
        dst_entity_id=edge.dst_entity_id,
        properties=edge.properties,
    )


def entity_ref_to_item(entity: EntityRef, *, audit: bool = False) -> EntityRefItem:
    """Convert an EntityRef to the response item.

    Default shape identifies the entity and says when it came into being. The
    owning tenant and the soft-delete flag are audit-only: a caller reading its
    own tenant's data learns nothing from being told whose data it is, and
    inactive rows are filtered out of every list that returns this shape.
    """
    if audit:
        return EntityRefItem(
            entity_id=entity.entity_id,
            tenant_id=entity.tenant_id,
            entity_type=entity.entity_type,
            name=entity.name,
            external_id=entity.external_id,
            is_active=entity.is_active,
            created_at=entity.created_at,
        )
    return EntityRefItem(
        entity_id=entity.entity_id,
        entity_type=entity.entity_type,
        name=entity.name,
        external_id=entity.external_id,
        created_at=entity.created_at,
    )


def fact_to_artifact(fact: object, *, audit: bool = False) -> ArtifactResponse:
    """Convert a fact — a stored row or a retrieval reference — to the response item.

    Deliberately structural rather than typed on one class: the same shape is
    owed for a fact read from an entity and for one returned as a search match,
    and those arrive as different types carrying the same attributes.

    Default shape is what a caller acts on. The bitemporal columns, the owning
    tenant, the parent entity and the authority flag are audit-only, and the
    field names drop the storage-side ``t_`` prefix because that is database
    nomenclature rather than part of the contract.
    """
    common = {
        "fact_id": fact.fact_id,  # type: ignore[attr-defined]
        "category": fact.category,  # type: ignore[attr-defined]
        "title": getattr(fact, "title", None),
        "body": fact.body,  # type: ignore[attr-defined]
        "body_format": getattr(fact, "body_format", None),
        "created_at": fact.t_ingested_at,  # type: ignore[attr-defined]
    }
    if audit:
        return ArtifactResponse(
            **common,
            tenant_id=fact.tenant_id,  # type: ignore[attr-defined]
            entity_id=fact.entity_id,  # type: ignore[attr-defined]
            is_authoritative=fact.is_authoritative,  # type: ignore[attr-defined]
            valid_from=fact.t_valid_from,  # type: ignore[attr-defined]
            valid_to=fact.t_valid_to,  # type: ignore[attr-defined]
            ingested_at=fact.t_ingested_at,  # type: ignore[attr-defined]
            invalidated_at=fact.t_invalidated_at,  # type: ignore[attr-defined]
        )
    return ArtifactResponse(**common)


def fact_to_citation(fact: object) -> CitationItem:
    """Convert a fact to the citation that stands in for it in a search result.

    A search result answers "which capability", and names the evidence that made
    it match. It is not a way to read that evidence: the body is a document, and
    returning every one of them inside a list response bloats the payload with
    content the caller did not ask for and cannot tell was truncated.

    So a match cites rather than quotes — enough to show *why* this result
    matched and to fetch the thing itself. Reading the body is the artifact
    endpoint's job, and ``_links.self`` is how a caller gets there.
    """
    return CitationItem(
        fact_id=fact.fact_id,  # type: ignore[attr-defined]
        category=fact.category,  # type: ignore[attr-defined]
        title=getattr(fact, "title", None),
        created_at=fact.t_ingested_at,  # type: ignore[attr-defined]
        _links=Links(self=f"/v1/capabilities/{fact.entity_id}/artifacts/{fact.fact_id}"),  # type: ignore[attr-defined]
    )


def search_result_to_item(result: SearchResult, *, audit: bool = False) -> SearchResultItem:
    """Serialise one search hit: the entity that matched, and what made it match.

    Shared with the tool surface, which previously reflected the service
    dataclass straight onto the wire and so answered the same question with
    storage column names, the owning tenant, and every matched body inline.

    The default shape cites the matching artifacts rather than embedding them.
    Under ``audit`` the full artifact shape is included as well — that is the one
    place bodies come back inline, because a caller reconstructing what the index
    saw wants what the index saw.
    """
    item = SearchResultItem(
        entity_id=result.entity.entity_id,
        name=result.entity.name,
        entity_type=result.entity.entity_type,
        score=result.fused_rank_score,
        retrieval_arms=result.retrieval_arms,
        citations=[fact_to_citation(f) for f in result.matching_facts],
    )
    if audit:
        item.tenant_id = result.entity.tenant_id
        item.matching_facts = [fact_to_artifact(f, audit=True) for f in result.matching_facts]
    return item


__all__ = [
    "ViewParam",
    "edge_to_item",
    "entity_ref_to_item",
    "fact_to_artifact",
    "fact_to_citation",
    "get_service",
    "search_result_to_item",
    "to_response",
]
