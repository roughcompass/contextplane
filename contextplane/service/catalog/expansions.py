"""The response shapes ``IncludeService`` builds for ``?include=``.

These models live beside ``includes``, the only code that constructs them.
They were in the API schema package, which made the service that produces
them import upward into the HTTP layer for its own return type -- the
expansion is a catalog concept that two transports happen to serve, not an
HTTP one.

``contextplane.api.schemas.catalog`` imports the three container shapes it
embeds in ``CapabilityDetailResponse``; the item shapes are only ever seen
inside a container, so nothing outside this module names them but
``includes`` itself.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel


class IncludedEntityItem(BaseModel):
    """An entity surfaced by ``?include=components`` or ``?include=depends_on``.

    Same shape as EntityRefItem plus the entity's current attribute set so
    consumers don't need a second round-trip to inspect basic metadata
    (display_name, summary, lifecycle.state, owner, …).
    """

    entity_id: uuid.UUID
    tenant_id: uuid.UUID
    entity_type: str
    name: str
    external_id: str | None
    is_active: bool
    created_at: datetime.datetime
    attributes: dict[str, Any]


class EntityCollectionExpansion(BaseModel):
    """Container for an included entity collection.

    ``truncated`` signals that the per-include cap was hit and ``next`` points
    at the dedicated endpoint that returns the full set.
    """

    items: list[IncludedEntityItem]
    truncated: bool
    next: str | None = None


class ExternalIdItem(BaseModel):
    """One row of the entity_external_ids registry, surfaced via ``?include=external_ids``."""

    external_system_slug: str
    external_id: str
    url: str | None
    metadata: dict[str, Any] | None


class ExternalIdsExpansion(BaseModel):
    """Container for an ``?include=external_ids`` expansion, same truncation contract as EntityCollectionExpansion."""

    items: list[ExternalIdItem]
    truncated: bool


class InterfaceExpansion(BaseModel):
    """Latest interface surface for the capability, surfaced via ``?include=interface``.

    ``surface`` is the canonical normalised JSON Schema document; ``raw`` is
    the original artifact the caller submitted (JSON Schema, TypeScript, or
    OpenAPI 3.x). Either may be ``None`` if no surface is registered.
    """

    surface: dict[str, Any] | None
    raw: dict[str, Any] | None
    format: str | None
    version: str | None


__all__ = [
    "EntityCollectionExpansion",
    "ExternalIdItem",
    "ExternalIdsExpansion",
    "IncludedEntityItem",
    "InterfaceExpansion",
]
