"""The ingest stage — pulls external sources into the graph as staged facts.

Connectors (``contextplane.ingest.connectors``) discover, fetch, and parse
artifacts from external systems (GitHub repos, OpenAPI specs, releases,
package manifests). ``connector_registry`` maps a source's ``source_type``
to its ``Connector`` class. ``runner`` drives the per-source scheduler loop
and the discover -> fetch -> parse -> upsert pipeline. ``webhook`` receives
GitHub/GitLab push notifications and triggers an immediate run.

Every write this stage produces goes through ``CatalogService`` so the
authoritative-wins conflict policy is applied centrally, not per connector.

``queries`` holds the plain, session-taking read/write functions behind the
admin sync-source and sync-run endpoints — sync-source and sync-run rows are
this stage's own tables, so the admin router's SQL lives here rather than in
a generic admin-platform module.
"""

from __future__ import annotations
