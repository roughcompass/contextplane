# Authorization

How Context Plane decides **what the authenticated principal can do**. Once [Authentication](04-authentication.md) has produced a validated claim set, authorization turns those claims into a `TenantContext` — the tenant the request operates on, the actor it's attributed to, and the role set that gates write access.

---

## Pipeline

```
Validated claim set (sub, iss, aud, …)
        │
        ▼
Claim resolver               → entitlement service round-trip
                                    (with cache + single-flight)
        │
        ▼
Tenant grants                → 403 if empty
  (list of (tenant_slug, role))
        │
        ▼
X-Tenant-ID selection        → 400 if multiple grants + no header
  (single grant auto-selects)        403 if header doesn't match a grant
        │
        ▼
JIT actor upsert             → idempotent (oidc_subject, tenant_id) → actor_id
        │
        ▼
TenantContext (tenant_id + actor_id + role)
```

The grant resolver runs once per request and produces the `TenantContext` route handlers receive. The default and only live resolver calls an external entitlement service over HTTP and parses the response into a list of `(tenant_slug, role)` grants — see [Default `oidc` mode](#default-oidc-mode--entitlement-service) below.

---

## Default `oidc` mode — entitlement service

The default deployment resolves grants by calling an external entitlement service over HTTP. Enable it by setting:

```
ENTITLEMENT_SERVICE_URL=https://entitlement.example.com
ENTITLEMENT_SERVICE_ENV=PRD
ENTITLEMENT_SERVICE_DISCRIMINATOR=REGISTRY
ENTITLEMENT_ROLE_MAPPING=ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor
```

When the JWT validates, the resolver calls the entitlement service keyed by `sub`, receives a list of entitlement strings, and parses each one.

### Entitlement string grammar

Every entitlement is `<tenant_slug>_<DISCRIMINATOR>_<ROLE>`:

| Token | Meaning |
|---|---|
| `<tenant_slug>` | Stable external tenant identifier. JIT-upserted into the `tenants` table on first sight. |
| `<DISCRIMINATOR>` | Service token. Multiple registry-shaped services may share one entitlement endpoint with different discriminators (`REGISTRY`, `GRAPHREGISTRY`, `DATA_CATALOG`, …). Strings that don't match this deployment's discriminator are silently dropped — they belong to a different service. |
| `<ROLE>` | External role suffix. Mapped to one of `admin / producer / consumer / auditor` via `ENTITLEMENT_ROLE_MAPPING`. |

Parsing rules:

- Strings with the wrong discriminator → dropped (counted under `contextplane_entitlement_parse_ignored_total`).
- Empty tenant slug, unknown role suffix → dropped + WARNING log (counted under `contextplane_entitlement_parse_dropped_total`).
- Multiple entitlements for the same tenant → the highest role wins (`admin > producer > consumer > auditor`).

Example: an upstream that returns `["111205_CONTEXTPLANE_ADMIN", "111205_CONTEXTPLANE_CONSUMER", "999_CONTEXTPLANE_AUDITOR", "111205_GRAPHREGISTRY_ADMIN"]` produces the grants:

| tenant_slug | catalog_role |
|---|---|
| `111205` | `admin` (consumer was lower; `GRAPHREGISTRY` dropped) |
| `999` | `auditor` |

### Role mapping

`ENTITLEMENT_ROLE_MAPPING` is comma-separated `EXTERNAL:internal` pairs. Multiple external suffixes can map to the same internal role — useful during LDAP rename rollouts where old and new strings coexist:

```
ENTITLEMENT_ROLE_MAPPING=ADMIN:admin,ROLE_ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor
```

Internal roles are fixed at `{admin, producer, consumer, auditor}`; the mapping defines the external lexicon, not the catalog's role set.

### Cache + stale-on-failure

The resolver caches resolved grants in-process, keyed by the JWT's `jti` claim when the issuer mints one, or a SHA-256 of `resolved_identity:iat` when it doesn't. A per-key lock single-flights concurrent requests for the same JWT so only one upstream call happens per key at a time.

Each entry's lifetime is bounded by the JWT's own `exp` claim (with a 30-second floor so a token that's nearly expired doesn't cause pathological cache churn) — there is no separate operator-configured TTL. The cache is a bounded LRU sized by `ENTITLEMENT_CACHE_MAX_ENTRIES` (default 10000); the oldest entry is evicted once the bound is reached.

When the entitlement service call fails, what happens next depends on *why*:

- **Auth errors** (`401`/`403` from upstream), an unrecognized subject, a `429`, or a malformed response → the cache is never consulted. These are the upstream's authoritative answer and surface as `401`, `403`, or `503` respectively — see [Failure-to-status mapping](#failure-to-status-mapping).
- **5xx / timeout / network failure** → stale-on-failure is unconditional, not an operator toggle: if a non-expired cache entry exists for that JWT, Context Plane serves the cached grants, logs a warning, and writes an `auth.entitlement_stale_cache_served` audit row (best-effort — a write failure there doesn't block the response). With no usable cache entry, Context Plane returns `503`.

`contextplane_entitlement_cache_total{result="hit"|"miss"|"fallback"}` counts every resolution outcome; `fallback` is specifically the stale-serve path above.

### HTTP timeouts

The resolver's HTTP client is bounded:

```
ENTITLEMENT_CONNECT_TIMEOUT_MS=250
ENTITLEMENT_READ_TIMEOUT_MS=1500
ENTITLEMENT_MAX_RETRIES=1
```

The hot path runs this on every cache miss, so bounded failure prevents request thread pile-up against a slow upstream.

---

## Tenant selection — `X-Tenant-ID` header

A principal may hold grants for multiple tenants. The `X-Tenant-ID` header selects which tenant the current request operates on:

| Grants | Header | Outcome |
|---|---|---|
| 1 grant | absent | Auto-select the only grant. |
| 1 grant | matches grant | Select. |
| 1 grant | does **not** match | 403. |
| >1 grants | absent | 400 listing the available tenant external IDs. |
| >1 grants | matches one grant | Select. |
| >1 grants | does **not** match any grant | 403. |

The header name is the literal `X-Tenant-ID` — it is not configurable, and there is no legacy alias.

`GET /v1/whoami` runs through this same selection table rather than a separate tenantless path: a caller holding exactly one grant gets it auto-selected, so `whoami` reads as "tell me who I am" without an extra header in the common single-tenant case. A caller holding grants in more than one tenant still gets `400` with the available-tenant list from `whoami` until `X-Tenant-ID` is supplied — which is itself the discovery step for a client that doesn't yet know which tenants it can act as; repeat the call with the header set to see the selected tenant's identity.

---

## JIT actor materialization

The actor row is keyed by `(tenant_id, oidc_subject)`. On first authenticated request from a new principal, the resolver upserts the row in the selected tenant and surfaces the resulting `actor_id` for use in audit logs. The actor's `display_name` defaults to the JWT's `sub` claim unless overridden out-of-band.

If the resolver receives an entitlement for a tenant that an operator has disabled (`tenants.disabled_at IS NOT NULL`), that grant is silently dropped (counted under `contextplane_entitlement_dropped_entries_total{reason="tenant_disabled"}`). Disabling a tenant operator-side is the runtime kill-switch.

---

<!-- Reserved for future alternative resolvers. The entitlement-service path is the only live grant-resolution strategy today. -->


---

## Failure-to-status mapping

The middleware translates the resolver's typed exceptions to HTTP status:

| Resolver error | HTTP status | Body |
|---|---|---|
| `EntitlementAuthError(401)` | 401 | `authentication required` |
| `EntitlementAuthError(403)` | 403 | `access denied` |
| `EntitlementNotFoundError` | 403 | `access denied` |
| `EntitlementRateLimitError` | 503 | `service unavailable` |
| `EntitlementMalformedError` | 503 | `service unavailable` |
| `EntitlementServiceError` | 503 | `service unavailable` (cache served first if available) |
| Empty grants after parsing | 403 | `access denied` |
| `X-Tenant-ID` does not match any grant | 403 | `access denied` |
| Multiple grants, no `X-Tenant-ID` | 400 | `{error, message, available_tenants}` |
| Selected tenant disabled by operator | 403 | `access denied` |

Cache MUST NOT be consulted on auth errors — the resolver enforces this. Auth failures from upstream are authoritative.

---

## Local development

`make dev-token` (see [Authentication → Local development](04-authentication.md#local-development)) seeds entitlements in the mock entitlement service for the same `sub` the JWT will carry (`registry-dev` under the client_credentials grant). The seeded entitlement is `dev_CONTEXTPLANE_ADMIN`, which parses to `(tenant_slug=dev, role=admin)` under the default mapping.

To exercise multi-tenant grants locally, PUT additional entitlements directly to the mock entitlement service:

```bash
curl -X PUT http://localhost:8091/admin/entitlements/registry-dev \
  -H "Content-Type: application/json" \
  -d '{"scenario":"success_multi_tenant","entitlements":["dev_CONTEXTPLANE_ADMIN","acme_CONTEXTPLANE_CONSUMER"]}'
```

The next request from `registry-dev` resolves to two tenant grants — `X-Tenant-ID` then becomes mandatory.

---

## What's not in this doc

- **JWT validation, OIDC discovery, claim contract.** Those are authentication — see [Authentication](04-authentication.md).
- **Per-endpoint role requirements.** Each REST and MCP endpoint documents its required role in its own reference page; this doc covers how roles are *resolved*, not which routes need which role.
- **Tenant provisioning.** Tenants are JIT-materialized from entitlement strings; bulk-import and out-of-band onboarding are per-deployment concerns.
