"""Unit tests for `registry/api/routers/_common.py` — shared router machinery.

Every entity-shaped router (`capabilities.py`, `concepts.py`/`operations.py`
via `_entity_crud.py`, `graph.py`, `retrieval.py`, the MCP retrieval tool, ...)
calls into these functions to build its response shapes. A defect here is a
defect on every one of those surfaces at once, which is why this suite tests
the functions directly rather than relying on incidental coverage from
whichever router happens to exercise a given branch in its own tests.

Coverage:
- get_service            — reads `request.app.state.catalog`
- to_response            — CapabilityRecord -> CapabilityResponse
- edge_to_item           — default vs. `audit=True` shape
- entity_ref_to_item     — default vs. `audit=True` shape
- fact_to_artifact       — default vs. `audit=True` shape
- fact_to_citation       — cites, does not embed the body; `_links.self` shape
- search_result_to_item  — default (cites only) vs. `audit=True` (embeds facts
  and the owning tenant) — the audit-only disclosure boundary this module's
  own docstring calls a property of the *type*, not of the caller
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import MagicMock

from registry.api.routers._common import (
    edge_to_item,
    entity_ref_to_item,
    fact_to_artifact,
    fact_to_citation,
    get_service,
    search_result_to_item,
    to_response,
)
from registry.types import CapabilityRecord, EdgeRef, EntityRef, FactRef, SearchResult

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()


def _entity(**overrides: object) -> EntityRef:
    defaults: dict[str, object] = dict(
        entity_id=_ENTITY_ID,
        tenant_id=_TENANT,
        entity_type="capability",
        name="PaymentAPI",
        external_id="ext-1",
        is_active=True,
        created_at=_NOW,
    )
    defaults.update(overrides)
    return EntityRef(**defaults)  # type: ignore[arg-type]


def _fact(**overrides: object) -> FactRef:
    defaults: dict[str, object] = dict(
        fact_id=uuid.uuid4(),
        tenant_id=_TENANT,
        entity_id=_ENTITY_ID,
        category="description",
        body="Handles payment capture and refunds.",
        is_authoritative=True,
        is_authoritative_superseded=False,
        sync_run_id=None,
        t_valid_from=_NOW,
        t_valid_to=None,
        t_ingested_at=_NOW,
        t_invalidated_at=None,
        title="Overview",
        body_format="markdown",
        created_by=None,
    )
    defaults.update(overrides)
    return FactRef(**defaults)  # type: ignore[arg-type]


def _edge(**overrides: object) -> EdgeRef:
    defaults: dict[str, object] = dict(
        edge_id=uuid.uuid4(),
        tenant_id=_TENANT,
        src_entity_id=_ENTITY_ID,
        rel="depends_on",
        dst_entity_id=uuid.uuid4(),
        properties={"weight": 1},
        t_valid_from=_NOW,
        t_valid_to=None,
        t_ingested_at=_NOW,
        t_invalidated_at=None,
    )
    defaults.update(overrides)
    return EdgeRef(**defaults)  # type: ignore[arg-type]


def _record(**overrides: object) -> CapabilityRecord:
    defaults: dict[str, object] = dict(
        entity=_entity(),
        attributes={"owner_team": "platform"},
        lifecycle="active",
        facts=[],
        edges_out=[],
        edges_in=[],
    )
    defaults.update(overrides)
    return CapabilityRecord(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_service
# ---------------------------------------------------------------------------


class TestGetService:
    def test_returns_the_catalog_service_attached_to_app_state(self) -> None:
        sentinel = MagicMock(name="catalog-service")
        request = MagicMock()
        request.app.state.catalog = sentinel
        assert get_service(request) is sentinel


# ---------------------------------------------------------------------------
# to_response
# ---------------------------------------------------------------------------


class TestToResponse:
    def test_maps_entity_and_lifecycle_fields(self) -> None:
        record = _record(lifecycle="draft", attributes={"k": "v"})
        response = to_response(record)
        assert response.entity_id == _ENTITY_ID
        assert response.tenant_id == _TENANT
        assert response.name == "PaymentAPI"
        assert response.external_id == "ext-1"
        assert response.lifecycle == "draft"
        assert response.attributes == {"k": "v"}
        assert response.created_at == _NOW


# ---------------------------------------------------------------------------
# edge_to_item
# ---------------------------------------------------------------------------


class TestEdgeToItem:
    def test_default_shape_omits_bitemporal_and_tenant_fields(self) -> None:
        edge = _edge()
        item = edge_to_item(edge)
        assert item.edge_id == edge.edge_id
        assert item.src_entity_id == edge.src_entity_id
        assert item.rel == "depends_on"
        assert item.dst_entity_id == edge.dst_entity_id
        assert item.properties == {"weight": 1}
        assert item.tenant_id is None
        assert item.valid_from is None
        assert item.valid_to is None
        assert item.ingested_at is None
        assert item.invalidated_at is None

    def test_audit_shape_populates_tenant_and_bitemporal_fields(self) -> None:
        edge = _edge(t_valid_to=_NOW, t_invalidated_at=_NOW)
        item = edge_to_item(edge, audit=True)
        assert item.tenant_id == _TENANT
        assert item.valid_from == _NOW
        assert item.valid_to == _NOW
        assert item.ingested_at == _NOW
        assert item.invalidated_at == _NOW


# ---------------------------------------------------------------------------
# entity_ref_to_item
# ---------------------------------------------------------------------------


class TestEntityRefToItem:
    def test_default_shape_omits_tenant_and_is_active(self) -> None:
        entity = _entity()
        item = entity_ref_to_item(entity)
        assert item.entity_id == _ENTITY_ID
        assert item.entity_type == "capability"
        assert item.name == "PaymentAPI"
        assert item.external_id == "ext-1"
        assert item.created_at == _NOW
        assert item.tenant_id is None
        assert item.is_active is None

    def test_audit_shape_populates_tenant_and_is_active(self) -> None:
        entity = _entity(is_active=False)
        item = entity_ref_to_item(entity, audit=True)
        assert item.tenant_id == _TENANT
        assert item.is_active is False


# ---------------------------------------------------------------------------
# fact_to_artifact
# ---------------------------------------------------------------------------


class TestFactToArtifact:
    def test_default_shape_omits_audit_only_fields(self) -> None:
        fact = _fact()
        item = fact_to_artifact(fact)
        assert item.fact_id == fact.fact_id
        assert item.category == "description"
        assert item.title == "Overview"
        assert item.body == "Handles payment capture and refunds."
        assert item.body_format == "markdown"
        assert item.created_at == _NOW
        # Audit-only fields are not part of this schema's default population;
        # the model's field defaults win when the handler never sets them.
        assert item.tenant_id is None
        assert item.entity_id is None
        assert item.is_authoritative is None
        assert item.valid_from is None
        assert item.valid_to is None
        assert item.ingested_at is None
        assert item.invalidated_at is None

    def test_audit_shape_adds_bitemporal_and_ownership_fields(self) -> None:
        fact = _fact(t_valid_to=_NOW, t_invalidated_at=_NOW, is_authoritative=False)
        item = fact_to_artifact(fact, audit=True)
        assert item.tenant_id == _TENANT
        assert item.entity_id == _ENTITY_ID
        assert item.is_authoritative is False
        assert item.valid_from == _NOW
        assert item.valid_to == _NOW
        assert item.ingested_at == _NOW
        assert item.invalidated_at == _NOW

    def test_created_at_is_sourced_from_ingested_at_not_valid_from(self) -> None:
        """`created_at` on the wire means "when the record was ingested",
        not "when the fact became valid" — the two differ for backfilled
        facts, and this is the one place that distinction gets made."""
        fact = _fact(t_valid_from=_NOW - datetime.timedelta(days=30), t_ingested_at=_NOW)
        item = fact_to_artifact(fact)
        assert item.created_at == _NOW


# ---------------------------------------------------------------------------
# fact_to_citation
# ---------------------------------------------------------------------------


class TestFactToCitation:
    def test_cites_without_embedding_the_body(self) -> None:
        fact = _fact()
        citation = fact_to_citation(fact)
        assert citation.fact_id == fact.fact_id
        assert citation.category == "description"
        assert citation.title == "Overview"
        assert citation.created_at == _NOW
        # CitationItem has no `body` field at all — the type itself enforces
        # "cite, don't quote"; nothing to assert `is None` on here beyond
        # confirming the fields above are the only ones populated.
        assert citation.links is not None
        assert citation.links.self == f"/v1/capabilities/{_ENTITY_ID}/artifacts/{fact.fact_id}"


# ---------------------------------------------------------------------------
# search_result_to_item
# ---------------------------------------------------------------------------


class TestSearchResultToItem:
    def test_default_shape_cites_matches_and_omits_tenant_id(self) -> None:
        fact = _fact()
        result = SearchResult(
            entity=_entity(),
            matching_facts=[fact],
            score=0.87,
            retrieval_arms={"lexical": 0.5, "semantic": 0.9},
        )
        item = search_result_to_item(result)
        assert item.entity_id == _ENTITY_ID
        assert item.name == "PaymentAPI"
        assert item.entity_type == "capability"
        assert item.score == 0.87
        assert item.retrieval_arms == {"lexical": 0.5, "semantic": 0.9}
        assert len(item.citations) == 1
        assert item.citations[0].fact_id == fact.fact_id
        # The audit-only disclosure boundary: a default-view caller learns
        # neither the owning tenant nor the full artifact bodies that matched.
        assert item.tenant_id is None
        assert item.matching_facts is None

    def test_audit_shape_embeds_full_artifacts_and_owning_tenant(self) -> None:
        fact = _fact()
        result = SearchResult(
            entity=_entity(),
            matching_facts=[fact],
            score=0.5,
            retrieval_arms={"lexical": 0.5},
        )
        item = search_result_to_item(result, audit=True)
        assert item.tenant_id == _TENANT
        assert item.matching_facts is not None
        assert len(item.matching_facts) == 1
        assert item.matching_facts[0].body == fact.body
        # Citations are still populated alongside the embedded artifacts —
        # `audit` adds detail, it does not replace the default shape.
        assert len(item.citations) == 1

    def test_no_matching_facts_returns_empty_citations(self) -> None:
        result = SearchResult(entity=_entity(), matching_facts=[], score=0.1, retrieval_arms={})
        item = search_result_to_item(result)
        assert item.citations == []
