"""The OpenAPI document contract: description, tag catalogue, security schemes.

Every endpoint is tenant-scoped. "admin: …" tags require the admin role
within the CALLING tenant; they are never service-operator surfaces.
Service operators interact via env vars / helm / migrations — not REST.

Kept apart from routing and service construction because this is the one
place documentation-facing wording lives — a reviewer checking "does the
`/docs` page describe this endpoint correctly" only has to read this file,
not the 40-odd router modules it describes.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from registry.config import Settings

_OPENAPI_DESCRIPTION = """\
**registry** — semantic + temporal retrieval of an organisation's
engineering capabilities, with cross-tenant adoption, subscriptions,
notifications, and a breaking-change advisor.

### Authentication & tenancy

Every endpoint is **tenant-scoped**. The calling tenant is resolved
from the `Authorization: Bearer <JWT>` header: the JWT is validated
against the OIDC discovery URL and the entitlement service returns the
grants that anchor the `TenantContext`. No endpoint ever returns data
from a tenant other than the caller's — cross-tenant relationships go
through explicit adoption events (see the `adoptions` section).

### Roles within a tenant

| Role       | Typical use                                                     |
|------------|-----------------------------------------------------------------|
| `consumer` | Read capabilities, subscribe to events, list notifications.     |
| `producer` | Create / update / publish capabilities owned by this tenant.    |
| `admin`    | Manage this tenant's vocabulary, schemas, PII policies, RBAC.   |
| `auditor`  | Read-only access to the audit log + notification history.       |

### `admin: …` endpoints are tenant-admin, not service-admin

Sections tagged `admin: …` require the **`admin` role within the calling
tenant**. They are NOT service-operator surfaces. There is no cross-tenant
admin surface in the API. Service operators (the people running the
deployment) interact with the system through environment variables, helm
values, and Alembic migrations — not REST.

### HTTP method conventions

Standard verbs (`PATCH`, `DELETE`) are the canonical surface. Operators
behind enterprise gateways that strip non-GET/POST verbs can opt into
POST-tunneled aliases (`POST .../{id}:update`, `:delete`, etc.) by
setting `REGISTRY_HTTP_METHODS_MODE=both`. The aliases are disabled by
default.
"""


_OPENAPI_TAGS: list[dict[str, str]] = [
    # ---- Producer surfaces ----
    {
        "name": "capabilities",
        "description": (
            "Producer-side CRUD for capabilities owned by the calling "
            "tenant. Create, read, update, soft-delete, and set "
            "visibility (`private` / `tenant-shared` / `public`)."
        ),
    },
    {
        "name": "concepts",
        "description": "Concept-type entities — definitions and terminology referenced by capabilities.",
    },
    {
        "name": "operations",
        "description": "Operation-type entities — the verbs a capability supports (e.g. `createPayment`).",
    },
    {
        "name": "artifacts",
        "description": (
            "Bi-temporal facts attached to a capability — descriptions, "
            "decisions, runbooks, and other free-form content. Each "
            "artifact write supersedes the previous active row."
        ),
    },
    {
        "name": "lifecycle",
        "description": (
            "Promote/demote capabilities between lifecycle states "
            "(`alpha`, `beta`, `ga`, `deprecated`, `retired`). Producer "
            "or admin role; integration capabilities additionally require "
            "≥ 2 `composes` / `depends_on` edges before leaving `alpha`."
        ),
    },
    {
        "name": "interface",
        "description": (
            "Bi-temporal interface surface declarations. Accepts "
            "JSON Schema, TypeScript types (restricted subset), or "
            "OpenAPI 3.x; normalises to a canonical InterfaceSurface used "
            "by the breaking-change advisor."
        ),
    },
    # ---- Consumer surfaces ----
    {
        "name": "retrieval",
        "description": (
            "Hybrid semantic + lexical + graph search across capabilities "
            "and facts. List capabilities, fetch a full capability record "
            "(time-travel via `as_of`), and walk outgoing dependencies."
        ),
    },
    {
        "name": "graph",
        "description": (
            "Graph primitives — reverse traversal (who depends on this?), "
            "blast-radius transitive closure (cache-first), and "
            "provider/consumer projections (`GET /v1/graph/{provider,consumer}`)."
        ),
    },
    {
        "name": "integrations",
        "description": (
            "Pair-discoverability lookup: `GET /v1/integrations?connects=A&and=B` "
            "returns integration capabilities whose member edges touch "
            "both `A` and `B`. Visibility-filtered."
        ),
    },
    # ---- Cross-tenant ----
    {
        "name": "adoptions",
        "description": (
            "Cross-tenant adoption events. A consumer tenant "
            "adopts a provider tenant's capability — the API records "
            "the relationship, creates a `provides_to` edge owned by "
            "the provider, and (transitively) creates an inbox-only "
            "subscription for change events."
        ),
    },
    {
        "name": "breaking-change",
        "description": (
            "Read-only advisory for proposed version bumps. "
            "POST a proposed version + interface; receive the diff "
            "classification, the per-element changes, the list of "
            "affected consumers (cross-tenant consumer IDs are anonymised "
            "so the provider sees impact size without learning which "
            "external tenants are affected), and a release-notes scaffold."
        ),
    },
    # ---- Async surfaces ----
    {
        "name": "subscriptions",
        "description": (
            "Subscribe to capability events (`version_published`, "
            "`deprecation`, `breaking_change`, `conflict_added`, "
            "`integration_added`). Optional webhook URL + HMAC secret; "
            "inbox-only subscriptions default to no webhook. Auto-"
            "subscription on adoption."
        ),
    },
    {
        "name": "notifications",
        "description": (
            "In-catalog inbox for capability events. Cursor-paginated "
            "list (`status=unread/read/all`) + mark-read. Payload is "
            "the `CapabilityRegistryEvent` envelope only — no body text, "
            "description, or freeform content. Follow `fetch_url` to "
            "retrieve the full canonical record."
        ),
    },
    # ---- External-ID registry ----
    {
        "name": "external-ids",
        "description": (
            "Per-entity external-ID mapping — declare that "
            "`capability X` corresponds to `Stripe customer 123` in an "
            "external system. Lookup is bi-directional."
        ),
    },
    # ---- Inbound webhook receivers ----
    {
        "name": "webhooks",
        "description": (
            "Inbound webhook receivers for external systems (GitHub, "
            "GitLab) that push changes into the catalog. HMAC-signed; "
            "the secret is per-tenant per-source (`GITHUB_WEBHOOK_SECRET`, "
            "`GITLAB_WEBHOOK_SECRET`). Public — authenticated by signature, "
            "not Bearer token."
        ),
    },
    # ---- Admin: tenant-scoped administrative surfaces ----
    # (Every "admin: …" section requires the `admin` role in the calling
    # tenant. None of these are service-operator endpoints.)
    {
        "name": "admin: tokens",
        "description": "Mint and revoke API tokens for actors in the calling tenant.",
    },
    {
        "name": "admin: sync",
        "description": (
            "Manage sync sources (connectors that push external data into "
            "the catalog) and inspect sync-run history for the calling "
            "tenant. Trigger an on-demand sync run."
        ),
    },
    {
        "name": "admin: vocabulary",
        "description": (
            "Manage closed-vocabulary values (`entity_type`, `edge_rel`, "
            "`fact_category`, etc.) for the calling tenant. New values "
            "supersede prior bi-temporal rows."
        ),
    },
    {
        "name": "admin: schemas",
        "description": (
            "Register capability-type schemas (`integration`, custom "
            "tenant types) used to validate `capability.attributes` on "
            "write."
        ),
    },
    {
        "name": "admin: edge-schemas",
        "description": (
            "Register edge-property schemas used to validate " "`edge.properties` on write. Advisory or mandatory."
        ),
    },
    {
        "name": "admin: pii",
        "description": (
            "Manage PII pattern definitions and per-tenant field "
            "policies (advisory / block). The default scanner is "
            "always on; this surface configures policy."
        ),
    },
    {
        "name": "admin: rbac",
        "description": (
            "Assign and revoke roles (`producer`, `consumer`, `admin`, "
            "`auditor`) on actors within the calling tenant."
        ),
    },
    {
        "name": "admin: audit",
        "description": (
            "Query the calling tenant's audit log — every content-"
            "access event is recorded with actor, timestamp, and target."
        ),
    },
    {
        "name": "admin: external-systems",
        "description": ("Register external systems that the tenant maps " "capabilities to via `external-ids`."),
    },
]


# Paths that are public by design — operator probes, Swagger's own assets,
# and HMAC-authenticated webhook receivers. They are kept off the document-
# level `security` requirement so Swagger UI does not present an Authorize
# prompt for them and so OpenAPI consumers correctly mark them anonymous.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = ("/healthz", "/readyz", "/metrics", "/webhooks")


def _install_openapi_security(app: FastAPI, settings: Settings) -> None:
    """Override `app.openapi` so the spec declares the auth surface Swagger UI needs.

    Declares two OpenAPI 3 security schemes:

    - `bearerAuth` — HTTP Bearer with `bearerFormat: JWT`. Always present
      so a caller can paste a JWT into Swagger's **Authorize** dialog
      without going through the interactive OAuth flow.
    - `oidcAuth` — `openIdConnect` pointing at the configured discovery URL.
      Emitted only when `settings.oidc_discovery_url` is set, so deployments
      without an IdP advertise only the bearer lane.

    The document-level `security` requirement is `[bearerAuth] OR [oidcAuth]`
    (each scheme listed as its own single-element entry — OpenAPI 3 treats
    multiple entries as logical OR). Public paths override that to `[]`.
    """

    def _openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )

        schemes: dict[str, dict[str, Any]] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": (
                    "Paste a JWT issued by an OpenID Connect provider.\n\n"
                    "- **Local development:** run `make dev-token` to seed "
                    "the dev tenant + mock-IDP client, then exchange the "
                    "client credentials for a JWT via "
                    "`POST {MOCK_OIDC_URL}/default/token` with "
                    "`grant_type=client_credentials` and `scope=registry`.\n"
                    "- **Production:** present a JWT from the IdP at "
                    "`OIDC_DISCOVERY_URL`. The entitlement service "
                    "resolves grants from the JWT's `sub` claim; the "
                    "tenant + actor are JIT-materialised on first sight."
                ),
            },
        }
        if settings.oidc_discovery_url is not None:
            schemes["oidcAuth"] = {
                "type": "openIdConnect",
                "openIdConnectUrl": settings.oidc_discovery_url,
                "description": (
                    "JWT issued by the configured OpenID Connect provider. "
                    "The token must carry `sub`, `iss`, `aud`, `exp`, and "
                    "`iat` claims; tenant + actor are JIT-materialised "
                    "from the entitlement service's grant resolution."
                ),
            }
        components = schema.setdefault("components", {})
        components["securitySchemes"] = schemes

        security_requirement: list[dict[str, list[str]]] = [{"bearerAuth": []}]
        if "oidcAuth" in schemes:
            security_requirement.append({"oidcAuth": []})
        schema["security"] = security_requirement

        for path, methods in schema.get("paths", {}).items():
            if not path.startswith(_PUBLIC_PATH_PREFIXES):
                continue
            for op in methods.values():
                if isinstance(op, dict):
                    op["security"] = []

        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]
