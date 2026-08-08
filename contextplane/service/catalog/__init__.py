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

``progression`` validates a capability's lifecycle-stage transitions against
a tenant-defined state machine, its gates, and override consumption — the
policy layer above the alpha → beta → ga machine ``lifecycle`` runs, with its
meta-schema pinned in ``progression_definition_schema.json`` beside it.
``adoption`` records a consumer tenant's declared dependency on a provider's
capability and is the only writer of the ``provides_to`` edge that
relationship creates; the ``breaking_change`` advisor above reads those same
edges to size a blast radius. ``projections`` answers "what does my tenant
ship" and "what does my tenant consume" as RBAC-scoped, visibility-filtered
views over the entity/edge graph, and ``integration_lookup`` is a narrower
public read over one denormalized index: which integrations connect two
specific capabilities.

Those four arrived from a "platform" package whose docstring described "what
a producer, consumer, or operator does with a catalog that already exists" —
true of half the codebase, and therefore not a subject. Each is a read or a
write over the entity/edge graph this package defines, which is the subject
they actually share.

``queries`` holds the plain, session-taking read/write functions behind the
admin vocabulary, capability-type-schema, artifact-list, and
progression-definition/override endpoints — the SQL those routers used to
build inline, given one home so it stays next to the tables it touches
instead of scattered across the API layer.

Nothing here is re-exported. Import the module you need directly, e.g.
``from contextplane.service.catalog.core import CatalogService`` or
``from contextplane.service.catalog.entity import EntityService``.
"""

from __future__ import annotations
