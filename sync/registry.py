"""Connector registry — single authoritative mapping of source_type to Connector class.

Usage
-----
    from sync.registry import get_connector

    ConnectorClass = get_connector(source.source_type)
    connector = ConnectorClass()

The keys defined here MUST match the ``source_type`` controlled-vocabulary
values exactly (vocabulary kind ``source_type``).

Adding a new connector
----------------------
1. Implement ``MyConnector(Connector)`` in ``sync/connectors/my_type.py``.
2. Import it here and add its key to ``CONNECTORS``.
3. Ship the connector task commit.
"""

from __future__ import annotations

from sync.connector import Connector
from sync.connectors.docs_corpus import DocsCorpusConnector
from sync.connectors.markdown_adr_rfc import MarkdownADRRFCConnector
from sync.connectors.openapi import OpenAPIConnector
from sync.connectors.package_json import PackageJsonConnector
from sync.connectors.release_notes import ReleaseNotesConnector


# ---------------------------------------------------------------------------
# Typed exception
# ---------------------------------------------------------------------------


class UnknownConnectorError(Exception):
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
