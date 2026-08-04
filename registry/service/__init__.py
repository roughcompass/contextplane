"""Business logic, organized into six subdomain packages.

``catalog`` owns entities, facts, and interface/lifecycle validation — the core
data model everything else builds on. ``memory`` owns the claim pipeline from
observation to curated truth, plus the session event log claims are extracted
from. ``retrieval`` owns hybrid search, graph traversal, and capability listing —
the consumer read surface, plus the embedding write side that feeds its semantic
arm. ``workspace`` owns personal and team scratch space and its right-to-be-
forgotten purge. ``governance`` owns the cross-cutting rules every other
subdomain calls into: the cross-tenant visibility chokepoint, bi-temporal
predicate builders, the source-authority ladder, and the erasure fan-out.
``platform`` owns what a producer, consumer, or operator does with a catalog
that already exists — adoption, subscriptions and their notification fan-out,
graph projections, integration-pair lookup, operational health, and lifecycle
progression.

Each subdomain package documents its own modules; this file is the map, not
the territory. Import from the subdomain package directly, e.g.
``from registry.service.catalog.core import CatalogService`` or
``from registry.service.governance.visibility import VisibilityService``.
"""

from __future__ import annotations
