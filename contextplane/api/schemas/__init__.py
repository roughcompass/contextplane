"""Pydantic request/response schemas, split by the domain that owns them.

- ``common`` — shapes with no single domain home: navigation links, the
  structured-error envelope, whoami.
- ``catalog`` — the producer/consumer catalog surface (capabilities,
  concepts, operations, artifacts, adoptions, subscriptions, interface,
  graph traversal, search).

Import from the submodule that owns the shape you need
(``contextplane.api.schemas.catalog``, ``contextplane.api.schemas.common``) rather
than from this package directly — nothing is re-exported here. A shape
that later turns out to be shared across a new subdomain gets its own
submodule at that point rather than growing one of these into a second
monolith.
"""

from __future__ import annotations
