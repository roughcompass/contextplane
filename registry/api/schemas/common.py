"""Shapes with no single domain home: navigation links, the error envelope, whoami.

Every other schema submodule in this package (``catalog``, ...) imports
``Links`` from here rather than redeclaring it, so a resource that adds a
new navigation pointer changes one class. ``ErrorItem`` is the one
structured-error row shape every router response funnels through via
``registry.api.errors`` (``build_error`` / ``coerce_to_envelope`` build the
envelope as a plain dict, not this class); it lives here rather than in
``errors.py`` itself because it is a data shape, not error-handling logic.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class Links(BaseModel):
    """HATEOAS-style navigation pointers for a resource.

    Every detail response includes ``_links.self``; richer resources
    expose pointers to related sub-resources (e.g. capability detail
    points at its dependencies, artifacts, interface).

    URLs preserve the address form the caller used — slug paths get
    slug URLs back, UUID paths get UUID URLs.
    """

    self: str
    artifacts: str | None = None
    dependencies: str | None = None
    interface: str | None = None
    capability: str | None = None
    parent: str | None = None
    tenant: str | None = None
    actor: str | None = None


class WhoAmIResponse(BaseModel):
    """Session-context payload — what the calling token resolves to.

    Returned by ``GET /v1/whoami`` and the MCP ``whoami`` tool. UIs use
    it to render permission-gated buttons before any other call.
    """

    actor_id: uuid.UUID
    actor_display_name: str | None
    actor_email: str | None
    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_display_name: str
    roles: list[str]
    # Always null. These described registry-issued opaque tokens, which no
    # longer exist — authentication is OIDC JWTs validated against the IdP.
    links: Links | None = Field(default=None, alias="_links")

    model_config = {"populate_by_name": True}


class ErrorItem(BaseModel):
    """One row in the error envelope.

    ``path`` is a JSON Pointer ("$.name", "$.attributes.lifecycle.state")
    when the error is field-specific, ``None`` otherwise.

    ``code`` is the stable, machine-readable identifier clients
    program against. Don't localise or rephrase it across responses —
    that's what ``message`` is for.
    """

    path: str | None = None
    code: str
    message: str


__all__ = ["ErrorItem", "Links", "WhoAmIResponse"]
