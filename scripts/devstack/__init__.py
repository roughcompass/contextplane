"""Run the full local development stack without a container runtime.

`docker compose up -d` is one way to get the services the app needs
locally. This package is another, for environments where a container
runtime is unavailable. It brings up the same services, on the same
ports, with the same environment:

    Postgres              localhost:5544
    mock OIDC provider    localhost:8090
    mock entitlements     localhost:8091
    registry API          localhost:8000
    OTLP ingest           localhost:4318
    observability viewer  localhost:16686

That port-for-port match is the point. Every downstream command
(`make migrate`, `make dev-token`, `make dev-jwt`, `make dev-seed`), every
script default, and every curl example in the docs works against either
provider without being told which one is running.

Entry point: `python -m scripts.devstack <up|down|status|reset|logs>`,
wrapped as `make dev-up` and friends.
"""

from __future__ import annotations

__all__ = ["DEVSTACK_DIR", "state_dir"]

from pathlib import Path

# Everything the dev stack writes — cluster data, logs, PID state, the
# generated env file — lives here. Git-ignored; `make dev-reset` deletes
# the cluster inside it.
DEVSTACK_DIR = Path(".devstack")


def state_dir(root: Path | None = None) -> Path:
    """Absolute `.devstack` directory for the repo rooted at *root*."""
    base = root if root is not None else Path(__file__).resolve().parents[2]
    return base / DEVSTACK_DIR
