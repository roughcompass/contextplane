"""Typed exception hierarchy for the registry service.

``RegistryError`` is the one root every domain-specific exception in this
codebase eventually subclasses, even the ones that never reach an API
boundary (a background worker's sink failure, an embedding provider's
malformed-artifact error, an auth-layer entitlement-client failure). Rooting
everything here means a caller that wants to catch "any typed error this
service raises, as opposed to a genuine bug" has one class to catch, and a
new exception module never has to decide between ``Exception``,
``RuntimeError``, and ``ValueError`` as its base — the answer is always
``RegistryError`` or one of its existing subclasses.

``CatalogError`` is the API-visible subtree: the exceptions
``contextplane.api.errors.map_catalog_error`` (REST) and
``contextplane.api.mcp.context._map_catalog_error`` (MCP) know how to translate
into a response. Not everything that subclasses ``RegistryError`` subclasses
``CatalogError`` — plenty of internal-only failures (a worker's sink error,
an embedder's artifact error) root directly on ``RegistryError`` because no
router ever sees them and giving them API-shaped status codes would imply a
contract that doesn't exist.
"""

from __future__ import annotations


class RegistryError(Exception):
    """Root of every typed, domain-specific exception in this codebase.

    Catch this instead of ``Exception`` when you want "a failure this
    service's own code raised on purpose" without also catching genuine
    bugs (``AttributeError``, ``KeyError`` from a real programming error,
    etc.). Most callers want a more specific subclass — ``CatalogError``
    for anything that should reach an API boundary, or one of the
    internal-only roots (workspace authorization, embedding-provider
    failures, ...) for everything else.
    """


class CatalogError(RegistryError):
    """Base for every catalog-domain error.

    The API-visible subtree: every exception `map_catalog_error` (REST) and
    `_map_catalog_error` (MCP) translate into a response subclasses this,
    directly or indirectly.
    """


class TenantIsolationError(CatalogError):
    """Raised when a request would read or write data outside its TenantContext."""


class VocabularyError(CatalogError):
    """Raised when a value violates a registered controlled vocabulary."""


class ValidationError(CatalogError):
    """Raised when input fails JSON Schema or entity-type validation."""


class LifecycleError(CatalogError):
    """Raised when a lifecycle-state transition violates the state machine."""


class NotFoundError(CatalogError):
    """Raised when a requested entity, fact, or edge does not exist for the tenant."""


class ConflictError(CatalogError):
    """Raised when an insert would violate a uniqueness constraint (e.g. duplicate external ID)."""
