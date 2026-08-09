"""Business logic: six subject-owning subdomains, plus the policy kernel they share.

``catalog`` owns entities, facts, and interface/lifecycle validation — the core
data model everything else builds on. ``memory`` owns the claim pipeline from
observation to curated truth, plus the session event log claims are extracted
from. ``retrieval`` owns hybrid search, graph traversal, and capability listing —
the consumer read surface, plus the embedding write side that feeds its semantic
arm. ``workspace`` owns personal and team scratch space and its right-to-be-
forgotten purge. ``notifications`` owns subscription lifecycle, the fan-out that
turns one capability mutation into a row per active subscriber, and the in-catalog
inbox a consumer polls. ``operations`` owns operator-facing readings about how this
deployment is doing — queue depths, curation backlog, proposal age.

``governance`` is not a seventh peer in that list. It is the **policy kernel** this
layer shares: the cross-tenant visibility chokepoint, the bi-temporal predicate
builders, the source-authority ladder, and the right-to-be-forgotten fan-out. Every
subdomain above except ``operations`` imports it, as do the routers, the MCP tool
surface, and the wiring that assembles them — while it imports none of them,
reaching only for storage and below. That direction is a declared contract in
``pyproject.toml`` rather than a habit: an import from the kernel into ``api``,
``wiring``, or any subdomain beside it fails ``make lint``. A new cross-cutting
rule belongs there; anything owning a data model of its own belongs in the
subdomain that owns the model.

Each subdomain package documents its own modules; this file is the map, not
the territory. Import from the subdomain package directly, e.g.
``from contextplane.service.catalog.core import CatalogService`` or
``from contextplane.service.governance.visibility import VisibilityService``.
``retrieval`` and ``workspace`` are the two packages that export a name of their
own — each assembles its service class from mixins in its ``__init__`` — and even
there, everything below that class is reached at its own module path.
"""

from __future__ import annotations
