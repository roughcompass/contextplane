"""The ingest stage — pulls external sources into the graph as staged facts.

Connectors (``registry.ingest.connectors``) discover, fetch, and parse
artifacts from external systems (GitHub repos, OpenAPI specs, releases,
package manifests). ``connector_registry`` maps a source's ``source_type``
to its ``Connector`` class. ``runner`` drives the per-source scheduler loop
and the discover -> fetch -> parse -> upsert pipeline. ``webhook`` receives
GitHub/GitLab push notifications and triggers an immediate run.

Every write this stage produces goes through ``CatalogService`` so the
authoritative-wins conflict policy is applied centrally, not per connector.
"""

from __future__ import annotations
