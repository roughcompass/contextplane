"""Ports and environment for the dev stack.

The defaults here are not arbitrary — every one of them matches the
published port in `docker-compose.yml`, and the environment block matches
the compose `api` service with in-network hostnames rewritten to
localhost. That correspondence is what lets `make migrate`,
`make dev-token`, `make dev-jwt`, `make dev-seed`, `scripts/seed.py`'s
built-in default, and every curl example in the docs work without
knowing which provider is running.

Ports are overridable for the cases where identical is the wrong answer:
two checkouts side by side, or a sibling project already holding one of
them. Set `DEVSTACK_PORT_OFFSET` to shift all of them at once, or
`DEVSTACK_PORT_<SERVICE>` to move exactly one.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields

# Matches the compose port mappings. Changing a default here without
# changing compose reintroduces the divergence this module exists to
# prevent.
_DEFAULT_PORTS = {
    "postgres": 5544,
    "oidc": 8090,
    "entitlements": 8091,
    "api": 8000,
    "otlp": 4318,
    "viewer": 16686,
}


@dataclass(frozen=True)
class Ports:
    """Host ports the dev stack listens on."""

    postgres: int = _DEFAULT_PORTS["postgres"]
    oidc: int = _DEFAULT_PORTS["oidc"]
    entitlements: int = _DEFAULT_PORTS["entitlements"]
    api: int = _DEFAULT_PORTS["api"]
    otlp: int = _DEFAULT_PORTS["otlp"]
    viewer: int = _DEFAULT_PORTS["viewer"]

    @classmethod
    def from_env(cls) -> Ports:
        """Build from DEVSTACK_PORT_OFFSET and DEVSTACK_PORT_<SERVICE>."""
        offset = int(os.environ.get("DEVSTACK_PORT_OFFSET", "0"))
        values: dict[str, int] = {}
        for field in fields(cls):
            explicit = os.environ.get(f"DEVSTACK_PORT_{field.name.upper()}")
            if explicit is not None:
                values[field.name] = int(explicit)
            else:
                values[field.name] = _DEFAULT_PORTS[field.name] + offset
        return cls(**values)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def database_url(ports: Ports, database: str = "registry") -> str:
    return f"postgresql+asyncpg://postgres:password@localhost:{ports.postgres}/{database}"


def build_env(ports: Ports) -> dict[str, str]:
    """Environment for the API process and every DB-touching make target.

    Mirrors the compose `api` service environment. Two deliberate
    differences, both of which narrow rather than widen:

    - No pgbouncer. The compose dev override already points the API
      straight at Postgres, so routing through a pooler locally would be
      *less* faithful to how developers actually run it, not more.
    - A single issuer in the allowlist. Compose needs two spellings
      because a token minted from the host and one minted inside the
      container network carry different issuers; there is only one
      hostname here.
    """
    url = database_url(ports)
    return {
        "DATABASE_URL": url,
        "PGBOUNCER_URL": url,
        "SCHEDULER_JOBSTORE_URL": url,
        "OTLP_ENDPOINT": f"http://localhost:{ports.otlp}/v1/traces",
        "SERVICE_NAME": "registry",
        # Job kwargs (session_factory, embedder) aren't picklable, so
        # SQLAlchemyJobStore cannot persist them.
        "SCHEDULER_USE_MEMORY_JOBSTORE": "true",
        # Zero vectors, matching the compose stack. The real providers load
        # a model artifact from disk at construction, and the native stack
        # has no image to bake one into — so requiring it would mean
        # `make dev-up` failing on a file the dev loop does not need.
        # Semantic ranking is inert; the lexical arm decides order. Set
        # EMBEDDING_PROVIDER=onnx with a staged model to exercise real
        # retrieval.
        "EMBEDDING_PROVIDER": "stub",
        # Extraction that needs no credential. The whole pipeline runs -- event
        # lands, outbox enqueues, provider extracts, conformance gate validates,
        # write path stages -- so a demo shows the real path end to end on a
        # laptop with no key and no network.
        #
        # Deliberately not the real provider even when a key is present in the
        # environment. `make dev-up` must behave the same for every developer,
        # and a stack that silently spends money because a shell happened to
        # export a key is not that. Set EXTRACTION_PROVIDER=anthropic explicitly
        # to use a model.
        "EXTRACTION_PROVIDER": "local",
        "OIDC_DISCOVERY_URL": (f"http://localhost:{ports.oidc}/default/.well-known/openid-configuration"),
        "OIDC_ISSUER_ALLOWLIST": f"http://localhost:{ports.oidc}/default",
        "RESOURCE_URI_ALLOWLIST": "registry",
        "ENTITLEMENT_SERVICE_URL": f"http://localhost:{ports.entitlements}",
        "ENTITLEMENT_SERVICE_ENV": "DEV",
        "ENTITLEMENT_SERVICE_DISCRIMINATOR": "REGISTRY",
        "ENTITLEMENT_ROLE_MAPPING": ("ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor"),
        # The mock IDP issues 3600s tokens; the production default ceiling
        # (900s) is below that, so dev would reject every JWT for
        # token-ttl-exceeded. A dev affordance, not a production posture.
        "OIDC_MAX_TOKEN_TTL_SECONDS": "3600",
    }


def render_env_file(ports: Ports) -> str:
    """Shell-sourceable rendering of `build_env`, written to `.devstack/env`."""
    lines = [
        "# Generated by `make dev-up`. Sourced by the make targets that talk",
        "# to the database or the mock services. Delete freely; regenerated",
        "# on the next `make dev-up`.",
    ]
    lines.extend(f"{key}={value}" for key, value in build_env(ports).items())
    return "\n".join(lines) + "\n"
