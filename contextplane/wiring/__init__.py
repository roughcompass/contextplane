"""App-wiring package: the composition root's building blocks.

`contextplane.main.create_app` is the only place these modules get assembled
together; each one owns one part of the service graph so a change to, say,
a scheduler job doesn't touch the router table or the service constructors.

- `services` — the composition root proper: build the infrastructure every
  area shares, call each area's own `build_<area>_services` in dependency
  order, assemble the typed `Services` container from what they return.
- `stages` — what those stages hand each other (`CoreServices`,
  `PostAppServices`, `AuthContext`) and the `app.state` keys each still
  attaches for readers that have not moved to the container.
- `jobs` — the background scheduler and every job registered on it.
- `routes` — every router mounted on the app, plus the MCP surface.
- `openapi` — the OpenAPI description, tag catalogue, and security schemes.
- `tracing` — OTel SDK bootstrap.
- `http_app` — the error envelope, middleware stack, OTel instrumentation,
  and the operator probe endpoints (healthz/readyz/metrics).

The typed `Services` container is deliberately neither declared nor
re-exported here. It lives in `contextplane.api.container`, beside the routers
and MCP tools that read one per request, and this package imports it downward
to *assemble* one. A re-export would put the declaration back in this
namespace and hand every router a reason to import the assembly it is mounted
by — the import edge this placement exists to remove — so import both the type
and the per-request `services()` accessor from `contextplane.api.container`
directly. It also keeps `from contextplane.wiring import services` resolving to
the service-*construction* module below rather than to an accessor of the same
name.
"""

from __future__ import annotations
