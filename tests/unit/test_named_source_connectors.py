"""The three named sources, tested where they can be: `parse` and the refusals.

E12-T1. `parse` is the half the contract requires to be pure — no network, no
database, no filesystem — so it is the half a test can hold to its word, and
`tests/unit/test_connector_parse.py` already sets that convention for the five
connectors that shipped before these.

What is deliberately not asserted here: that `discover` pages correctly against
a real Backstage, ServiceNow or Confluence. Nothing in this repository can stand
one up, and a test against a mock of an API this code also models would agree
with itself by construction. The endpoints and envelopes were read from each
product's published API reference and are named in each module's docstring;
this file covers what a fixture can actually decide.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from contextplane.ingest.connector import CredentialError, DiscoveredArtifact
from contextplane.ingest.connector_registry import CONNECTORS, get_connector
from contextplane.ingest.connectors.backstage import BackstageConnector, _split_ref
from contextplane.ingest.connectors.cmdb_servicenow import ServiceNowCmdbConnector
from contextplane.ingest.connectors.wiki_confluence import ConfluenceWikiConnector, _to_text

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("backstage", BackstageConnector),
        ("cmdb_servicenow", ServiceNowCmdbConnector),
        ("wiki_confluence", ConfluenceWikiConnector),
    ],
)
def test_each_named_source_resolves_through_the_registry(source_type: str, expected: type) -> None:
    """The registry is the single authoritative mapping, and a bulk-import path
    that bypassed it would be a second place source types are known — the first
    thing that goes wrong is that one of them knows about a source the other
    does not."""
    assert get_connector(source_type) is expected
    assert CONNECTORS[source_type] is expected


def test_the_named_types_say_which_product_they_are() -> None:
    """ "CMDB" and "wiki" name markets, and a connector cannot be written against
    a market. The registry key carries the product so an operator registering a
    source is choosing a protocol rather than a category."""
    assert "cmdb_servicenow" in CONNECTORS
    assert "wiki_confluence" in CONNECTORS
    assert "cmdb" not in CONNECTORS
    assert "wiki" not in CONNECTORS


# ---------------------------------------------------------------------------
# Backstage
# ---------------------------------------------------------------------------

_BACKSTAGE_ENTITY = {
    "apiVersion": "backstage.io/v1alpha1",
    "kind": "Component",
    "metadata": {
        "uid": "b6f1a0d2-0000-0000-0000-000000000001",
        "etag": "ZTkxYjhmNzk",
        "name": "checkout",
        "namespace": "default",
        "title": "Checkout service",
        "description": "Takes payment and issues the order.",
        "tags": ["payments", "tier-1"],
        "annotations": {"backstage.io/managed-by-location": "url:https://git.acme.io/checkout/catalog-info.yaml"},
        "links": [{"url": "https://runbooks.acme.io/checkout", "title": "Runbook"}],
    },
    "spec": {"type": "service", "lifecycle": "production", "owner": "team-payments", "system": "orders"},
}


def _backstage_artifact() -> DiscoveredArtifact:
    return DiscoveredArtifact(
        artifact_id="component:default/checkout",
        source_url="url:https://git.acme.io/checkout/catalog-info.yaml",
        artifact_type="Component",
        content_revision="ZTkxYjhmNzk",
    )


def test_a_backstage_entity_becomes_one_fact_keyed_on_its_reference() -> None:
    """Keyed on `kind:namespace/name`, not the uid.

    The descriptor format says the uid *"can change over time"* and should not
    be used as an external reference. Keying on it would mint a new subject on
    this side every time the other side re-ingested the same component.
    """
    facts = BackstageConnector().parse(_backstage_artifact(), json.dumps(_BACKSTAGE_ENTITY).encode())

    assert len(facts) == 1
    fact = facts[0]
    assert fact.category == "software_catalog_entity"
    assert (
        fact.entity_id
        == BackstageConnector()
        .parse(
            _backstage_artifact(),
            json.dumps(
                {**_BACKSTAGE_ENTITY, "metadata": {**_BACKSTAGE_ENTITY["metadata"], "uid": "different"}}
            ).encode(),
        )[0]
        .entity_id
    )


def test_the_backstage_body_carries_what_the_catalog_asserts_and_not_its_plumbing() -> None:
    """Prose, not the descriptor. A YAML blob reproduced verbatim carries the
    same information in a form neither a reader nor an extractor can use, and it
    would carry the catalog's bookkeeping as though it were an assertion."""
    body = BackstageConnector().parse(_backstage_artifact(), json.dumps(_BACKSTAGE_ENTITY).encode())[0].body

    assert "Checkout service" in body
    assert "Takes payment and issues the order." in body
    assert "owner: team-payments" in body
    assert "lifecycle: production" in body
    assert "payments, tier-1" in body
    assert "https://runbooks.acme.io/checkout" in body
    # The two fields the catalog generates about itself.
    assert "etag" not in body
    assert "b6f1a0d2" not in body


def test_a_backstage_entity_asserts_no_validity_it_was_not_given() -> None:
    """`valid_from` stays absent rather than taking this process's clock.

    A server-defaulted instant is indistinguishable afterwards from one the
    source stated, which is the property E12-T2 turns into a schema constraint.
    """
    assert (
        BackstageConnector().parse(_backstage_artifact(), json.dumps(_BACKSTAGE_ENTITY).encode())[0].valid_from is None
    )


def test_a_backstage_entity_that_does_state_an_instant_keeps_it() -> None:
    stamped = {
        **_BACKSTAGE_ENTITY,
        "metadata": {
            **_BACKSTAGE_ENTITY["metadata"],
            "annotations": {"backstage.io/created-at": "2026-08-01T09:30:00Z"},
        },
    }
    fact = BackstageConnector().parse(_backstage_artifact(), json.dumps(stamped).encode())[0]
    assert fact.valid_from == datetime(2026, 8, 1, 9, 30, tzinfo=UTC)


def test_an_unparseable_entity_reference_is_refused_rather_than_defaulted() -> None:
    """An artifact id this connector did not mint means something upstream is
    handing us references from elsewhere, and defaulting the missing part would
    attach the resulting claims to the wrong entity silently."""
    with pytest.raises(ValueError, match="not a Backstage entity reference"):
        _split_ref("checkout")


# ---------------------------------------------------------------------------
# ServiceNow CMDB
# ---------------------------------------------------------------------------

_CMDB_RECORD = {
    "result": {
        "sys_id": {"value": "0f1c2d3e4a5b6c7d", "display_value": "0f1c2d3e4a5b6c7d"},
        "name": {"value": "checkout-svc", "display_value": "Checkout Service"},
        "short_description": {"value": "Payment capture", "display_value": "Payment capture"},
        "sys_class_name": {"value": "cmdb_ci_service", "display_value": "Business Service"},
        "operational_status": {"value": "1", "display_value": "Operational"},
        "business_criticality": {"value": "1", "display_value": "1 - most critical"},
        "owned_by": {"value": "9a8b7c6d", "display_value": "Priya Ramanathan"},
        "sys_updated_on": {"value": "2026-08-03 14:22:07", "display_value": "03/08/2026 14:22:07"},
    }
}


def _cmdb_artifact() -> DiscoveredArtifact:
    return DiscoveredArtifact(
        artifact_id="cmdb_ci_service/0f1c2d3e4a5b6c7d",
        source_url="https://acme.service-now.com/api/now/table/cmdb_ci_service/0f1c2d3e4a5b6c7d",
        artifact_type="cmdb_ci_service",
    )


def test_a_configuration_item_reads_as_its_display_values() -> None:
    """A raw reference field is a `sys_id` and a link, which reads afterwards as
    an opaque identifier attached to a claim nobody can check."""
    fact = ServiceNowCmdbConnector().parse(_cmdb_artifact(), json.dumps(_CMDB_RECORD).encode())[0]

    assert fact.category == "configuration_item"
    assert "Checkout Service" in fact.body
    assert "owned by: Priya Ramanathan" in fact.body
    assert "operational status: Operational" in fact.body
    # The stored value behind a display value is not what a reader should see.
    assert "9a8b7c6d" not in fact.body


def test_a_configuration_item_states_when_the_source_last_saw_it_change() -> None:
    """`sys_updated_on` is a claim about the *record*, not about the world — and
    stating it is better than leaving it absent, because a CMDB's whole value is
    that somebody maintains it and the date is how a reader judges whether
    anybody still does."""
    fact = ServiceNowCmdbConnector().parse(_cmdb_artifact(), json.dumps(_CMDB_RECORD).encode())[0]
    assert fact.valid_from == datetime(2026, 8, 3, 14, 22, 7, tzinfo=UTC)


def test_a_configuration_item_parses_whether_or_not_display_values_were_asked_for() -> None:
    """`parse` is reachable with either response shape, and a `KeyError` several
    frames down would be a worse answer than the value."""
    flat = {"result": {"sys_id": "0f1c2d3e4a5b6c7d", "name": "checkout-svc", "sys_updated_on": ""}}
    fact = ServiceNowCmdbConnector().parse(_cmdb_artifact(), json.dumps(flat).encode())[0]
    assert "checkout-svc" in fact.body
    assert fact.valid_from is None


@pytest.mark.asyncio
async def test_the_cmdb_refuses_an_anonymous_source() -> None:
    """The Table API has no anonymous access, so a source registered without a
    credential is a misconfiguration to name now rather than a 401 later."""
    with pytest.raises(CredentialError, match="no anonymous access"):
        await ServiceNowCmdbConnector().validate(None)


@pytest.mark.asyncio
async def test_the_cmdb_refuses_a_credential_that_is_not_basic_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed variable is the operator who set it; a rejected one is
    whoever owns the account. A 401 cannot tell them apart, so this does."""
    monkeypatch.setenv("CP_TEST_SNOW", "just-a-token")
    with pytest.raises(CredentialError, match="username:password"):
        await ServiceNowCmdbConnector().validate("CP_TEST_SNOW")


# ---------------------------------------------------------------------------
# Confluence wiki
# ---------------------------------------------------------------------------

_WIKI_PAGE = {
    "id": "884736",
    "status": "current",
    "title": "Retry policy for the payments gateway",
    "spaceId": "9901",
    "version": {"number": 7, "createdAt": "2026-07-19T11:04:00.000Z"},
    "body": {
        "storage": {
            "value": (
                "<p>We retry <strong>three</strong> times.</p>"
                "<p>Then we page the on-call.</p>"
                "<ul><li>backoff is exponential</li></ul>"
                "<p>Escape the tag like this: &lt;p&gt;</p>"
            ),
            "representation": "storage",
        }
    },
}


def _wiki_artifact() -> DiscoveredArtifact:
    return DiscoveredArtifact(
        artifact_id="884736",
        source_url="https://acme.atlassian.net/wiki/api/v2/pages/884736",
        artifact_type="page",
        content_revision="7",
    )


def test_a_wiki_page_becomes_text_and_not_markup() -> None:
    """Storage format served raw would put XHTML into a claim's value, where a
    reader and an extractor both have to strip it."""
    fact = ConfluenceWikiConnector().parse(_wiki_artifact(), json.dumps(_WIKI_PAGE).encode())[0]

    assert fact.category == "wiki_page"
    assert "# Retry policy for the payments gateway" in fact.body
    assert "We retry three times." in fact.body
    assert "<strong>" not in fact.body
    assert "<ul>" not in fact.body and "<li>" not in fact.body
    assert "We retry three times.\n\nThen we page the on-call." in fact.body
    # The one angle bracket that *should* survive is the author's, and it
    # survives as text rather than as the tag it spells. Its own test below.
    assert "Escape the tag like this: <p>" in fact.body


def test_the_structure_of_a_page_survives_as_paragraphs() -> None:
    """Block ends become newlines before tags are stripped. Stripping first
    would collapse a page into one line, which is how a paragraph boundary
    becomes a sentence boundary that was never there."""
    text = _to_text("<p>first</p><p>second</p>")
    assert text == "first\n\nsecond"


def test_an_escaped_tag_written_by_an_author_is_not_removed_as_markup() -> None:
    """Entities are decoded *after* tags are stripped, so `&lt;p&gt;` typed by a
    person survives as text."""
    assert _to_text("<p>write &lt;p&gt; to mean a paragraph</p>") == "write <p> to mean a paragraph"


def test_a_wiki_page_states_when_this_revision_came_into_being() -> None:
    """`version.createdAt` is the source's own instant. This process's clock
    would look afterwards exactly like one the source had asserted."""
    fact = ConfluenceWikiConnector().parse(_wiki_artifact(), json.dumps(_WIKI_PAGE).encode())[0]
    assert fact.valid_from == datetime(2026, 7, 19, 11, 4, tzinfo=UTC)


def test_an_unversioned_page_asserts_no_instant() -> None:
    unversioned = {key: value for key, value in _WIKI_PAGE.items() if key != "version"}
    assert ConfluenceWikiConnector().parse(_wiki_artifact(), json.dumps(unversioned).encode())[0].valid_from is None


@pytest.mark.asyncio
async def test_confluence_refuses_an_anonymous_source() -> None:
    with pytest.raises(CredentialError, match="no anonymous API access"):
        await ConfluenceWikiConnector().validate(None)


# ---------------------------------------------------------------------------
# Shared properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("connector", "artifact", "raw"),
    [
        (BackstageConnector(), _backstage_artifact(), json.dumps(_BACKSTAGE_ENTITY).encode()),
        (ServiceNowCmdbConnector(), _cmdb_artifact(), json.dumps(_CMDB_RECORD).encode()),
        (ConfluenceWikiConnector(), _wiki_artifact(), json.dumps(_WIKI_PAGE).encode()),
    ],
)
def test_parse_is_reproducible(connector: object, artifact: DiscoveredArtifact, raw: bytes) -> None:
    """The contract's own words: "calling it twice with the same arguments must
    return equivalent results". A connector that minted a fresh id, or read a
    clock, would fail here rather than in a store that had already accepted two
    subjects for one thing."""
    first = connector.parse(artifact, raw)  # type: ignore[attr-defined]
    second = connector.parse(artifact, raw)  # type: ignore[attr-defined]
    assert first == second
    assert all(isinstance(fact.entity_id, uuid.UUID) for fact in first)


@pytest.mark.parametrize(
    ("connector", "artifact", "raw"),
    [
        (BackstageConnector(), _backstage_artifact(), json.dumps(_BACKSTAGE_ENTITY).encode()),
        (ServiceNowCmdbConnector(), _cmdb_artifact(), json.dumps(_CMDB_RECORD).encode()),
        (ConfluenceWikiConnector(), _wiki_artifact(), json.dumps(_WIKI_PAGE).encode()),
    ],
)
def test_every_fact_keeps_the_place_it_came_from(connector: object, artifact: DiscoveredArtifact, raw: bytes) -> None:
    """A claim whose source_url is not the artifact's is a claim nobody can go
    back and check."""
    for fact in connector.parse(artifact, raw):  # type: ignore[attr-defined]
        assert fact.source_url == artifact.source_url
        assert fact.commit_sha is None
