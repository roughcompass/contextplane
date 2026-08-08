"""App-wiring package: the composition root's building blocks.

`contextplane.main.create_app` is the only place these modules get assembled
together; each one owns one part of the service graph so a change to, say,
a scheduler job doesn't touch the router table or the service constructors.

- `container` — the typed `Services` dataclass and its per-request accessor.
- `services` — construction of every service, plus the auth trio and the
  `Services` container assembly (both of which need `app` to already exist).
- `jobs` — the background scheduler and every job registered on it.
- `routes` — every router mounted on the app, plus the MCP surface.
- `openapi` — the OpenAPI description, tag catalogue, and security schemes.
- `tracing` — OTel SDK bootstrap.
- `http_app` — the error envelope, middleware stack, OTel instrumentation,
  and the operator probe endpoints (healthz/readyz/metrics).

This package intentionally does not re-export `container.services` (the
per-request container accessor) at the top level: `contextplane.wiring.services`
names the service-*construction* module below, not that function, so
`from contextplane.wiring import services` has to resolve to the module. Import
the accessor from where it lives instead: `from contextplane.wiring.container
import services`.
"""

from __future__ import annotations

from contextplane.wiring.container import Services

__all__ = ["Services"]
