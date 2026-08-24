"""Connector registry — single authoritative mapping of source_type to Connector class.

Usage
-----
    from contextplane.ingest.connector_registry import get_connector

    ConnectorClass = get_connector(source.source_type)
    connector = ConnectorClass()

The keys defined here MUST match the ``source_type`` controlled-vocabulary
values exactly (vocabulary kind ``source_type``).

Adding a new connector
----------------------
1. Implement ``MyConnector(Connector)`` in ``contextplane/ingest/connectors/my_type.py``.
2. Import it here and add its key to ``CONNECTORS``.
3. Ship the connector task commit.
"""

from __future__ import annotations

from contextplane.exceptions import ValidationError
from contextplane.ingest.connector import Connector
from contextplane.ingest.connectors.backstage import BackstageConnector
from contextplane.ingest.connectors.cmdb_servicenow import ServiceNowCmdbConnector
from contextplane.ingest.connectors.docs_corpus import DocsCorpusConnector
from contextplane.ingest.connectors.markdown_adr_rfc import MarkdownADRRFCConnector
from contextplane.ingest.connectors.openapi import OpenAPIConnector
from contextplane.ingest.connectors.package_json import PackageJsonConnector
from contextplane.ingest.connectors.release_notes import ReleaseNotesConnector
from contextplane.ingest.connectors.wiki_confluence import ConfluenceWikiConnector

# ---------------------------------------------------------------------------
# Typed exception
# ---------------------------------------------------------------------------


class UnknownConnectorError(ValidationError):
    """Raised when no connector is registered for a given source_type."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CONNECTORS: dict[str, type[Connector]] = {
    "openapi": OpenAPIConnector,
    "release_notes": ReleaseNotesConnector,
    "markdown_adr_rfc": MarkdownADRRFCConnector,
    "package_json": PackageJsonConnector,
    "docs_corpus": DocsCorpusConnector,
    # E12's three named sources. Each is a product with a documented API rather
    # than a category: "CMDB" and "wiki" name markets, and a connector cannot be
    # written against a market. ServiceNow's Table API and Confluence Cloud's v2
    # pages API are the two the tree already assumed -- `source_governance.py`
    # uses a Confluence page as its example of a source whose authority must be
    # declared before its first write.
    "backstage": BackstageConnector,
    "cmdb_servicenow": ServiceNowCmdbConnector,
    "wiki_confluence": ConfluenceWikiConnector,
}


def get_connector(source_type: str) -> type[Connector]:
    """Return the ``Connector`` subclass registered for *source_type*.

    Args:
        source_type: Must match one of the five controlled-vocabulary values.

    Returns:
        The ``Connector`` subclass (not an instance).

    Raises:
        UnknownConnectorError: if *source_type* is not in ``CONNECTORS``.
    """
    try:
        return CONNECTORS[source_type]
    except KeyError:
        known = ", ".join(sorted(CONNECTORS))
        raise UnknownConnectorError(
            f"No connector registered for source_type={source_type!r}. " f"Known types: {known}."
        ) from None
