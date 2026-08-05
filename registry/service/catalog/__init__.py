"""The catalog subdomain: entities, facts, and everything that governs how they are shaped and read.

``core`` holds ``CatalogService``, the thin facade over edge operations that
route handlers and the MCP server call through ``request.app.state.catalog``.
It is named ``core`` rather than ``catalog`` so the module does not stutter
against the package (a module literally named ``catalog`` inside a package
already named ``catalog`` would say the same word twice for no reason).
``entity`` and ``facts`` hold the two sub-services ``core`` delegates to —
entity CRUD and handle resolution, and fact CRUD plus sync ingestion — and
``schema`` and ``vocabulary`` hold the two validators both of those depend
on: JSON Schema conformance for attributes, and the tenant-scoped
controlled-vocabulary values a fact's predicate must resolve against.
``global_vocabulary`` is the fabric-wide counterpart used by the claim
pipeline rather than by per-tenant facts.

``lifecycle`` governs the alpha → beta → ga → deprecated → retired state
machine and optionally delegates successor-edge creation back to
``CatalogService``. ``includes`` expands the `?include=` query parameter
shared by REST and MCP read paths. ``slugs`` and ``identity`` are small,
focused validators — slug format rules, and whoami/actor resolution — used
across the rest of this package.

``interface_normalize``, ``interface_diff``, and ``interface_storage`` are
the three-step pipeline for a producer's declared interface surface: turn
whatever shape was given into a canonical form, classify what changed
between two canonical forms as breaking or not, and persist interface
records. ``version_predicates`` evaluates the semver-range predicates used
by both edge validation and the breaking-change advisor, and
``breaking_change`` is the advisor itself: it composes the diff, the
predicate evaluator, and the adoption-scoped blast radius into one report.
``external_ids`` maps this fabric's entities to identifiers in outside
systems.

``queries`` holds the plain, session-taking read/write functions behind the
admin vocabulary, capability-type-schema, and artifact-list endpoints —
the SQL those routers used to build inline, given one home so it stays
next to the tables it touches instead of scattered across the API layer.

Nothing here is re-exported. Import the module you need directly, e.g.
``from registry.service.catalog.core import CatalogService`` or
``from registry.service.catalog.entity import EntityService``.
"""

from __future__ import annotations
