# Architecture

A focused architecture reference for engineers and architects who need to reason about the system without reading every file. Each section links to the canonical sources of truth for deeper dives.

---

## What the registry is

A tenant-isolated, bi-temporal catalog of an organisation's engineering capabilities. Two surfaces:

- **REST** at `/v1/*` — the primary integration surface (65 endpoints; see [api.md](01-api.md)).
- **MCP** at `/mcp/sse` — the agent-facing surface (18 tools; see [mcp-tools.md](02-mcp-tools.md)).

Both surfaces resolve through the same auth + visibility + service stack. There is no second code path for either.

---

## Component map

Top-level packages under `registry/`:

| Path | Responsibility |
|---|---|
| `registry/api/middleware/` | Per-request concerns: tenant context resolution, idempotency, rate limiting, OpenAPI error envelope, HTTP-methods routing. |
| `registry/api/routers/` | HTTP surface — one router per resource group (capabilities, adoptions, subscriptions, notifications, workspaces, admin/*, mcp). Thin adapters over services. |
| `registry/api/auth/` | OIDC discovery, JWKS cache, JWT validation pipeline. |
| `registry/auth/entitlements/` | Entitlement-service client, grant resolver, parser, cache, JIT actor + tenant materialization. |
| `registry/auth/resolver.py` | `ClaimResolverBase` abstraction. Today the entitlement-service resolver is the only concrete implementation. |
| `registry/service/` | Business logic — one module per concern. **`visibility.py` is the single chokepoint for cross-tenant queries.** |
| `registry/workers/` | Background jobs: webhook delivery, workspace expiry, closure-cache refresh, embedding drain. |
| `registry/storage/` | SQLAlchemy models + Alembic migrations (`migrations/versions/`). Head migration is the source of truth for the live schema. |
| `registry/security/` | PII pattern modules + per-tenant policy resolution. Runs on workspace-entry writes. |
| `registry/sync_worker.py` | Sync scheduler entry point. |
| `registry/ingest/` | External-source connector framework (GitHub, GitLab, OpenAPI, npm, ADR). Credentials resolve from env vars dynamically; never stored in `Settings` or the DB. |
| `scripts/` | Operational CLIs (`bootstrap_dev_tenant.py`, `seed.py`, `backfill_embeddings.py`, `partition_migrate.py`, gate scripts). |

---

## Request lifecycle

A typical `GET /v1/capabilities/{handle}` request:

```
                                  ┌─────────────────────────────┐
HTTP request                      │ FastAPI app (uvicorn)       │
  Authorization: Bearer <JWT>     │                             │
  X-Tenant-ID: <slug>?            │                             │
        │                         │                             │
        ▼                         │                             │
  OTel ASGI middleware            │   spans the whole request   │
        │                         │                             │
        ▼                         │                             │
  RateLimitMiddleware             │   per-tenant token bucket   │
        │                         │   (Postgres advisory lock)  │
        ▼                         │                             │
  get_tenant_context (Depends)    │   nine-step pipeline:       │
   1. Extract bearer              │   - oidc.validate_token     │
   2. Validate JWT                │   - entitlements.resolve    │
   3. Enrich claims               │   - select tenant by header │
   4. Resolve grants              │   - JIT actor upsert        │
   5. (resolver internal)         │                             │
   6. Empty → 403                 │                             │
   7. X-Tenant-ID selection       │                             │
   8. JIT actor upsert            │                             │
   9. Build TenantContext         │                             │
        │                         │                             │
        ▼                         │                             │
  Route handler                   │   thin: convert ctx + path  │
        │                         │   params → service call     │
        ▼                         │                             │
  service/governance/visibility.py│   filter_entities()         │
        │                         │   or assert_visible()       │
        ▼                         │                             │
  service/<concern>.py            │   business logic            │
        │                         │                             │
        ▼                         │                             │
  storage/ (asyncpg via PgBouncer)│   SQL                       │
                                  └─────────────────────────────┘
```

The visibility check on the way in is mandatory. Service code that returns entity rows MUST pass them through `filter_entities()` or invoke `assert_visible()` on a single entity. This one is enforced by review and by the cross-tenant conformance suite, not by a static gate — an entity-returning query is not reliably recognizable from source text.

---

## Data model

Conceptual model (full schema in `registry/storage/models.py` + the latest migration):

```
                  ┌──────────┐
                  │ tenants  │  ── opaque org/team boundary
                  └─────┬────┘     (slug + UUID; JIT-materialized)
                        │
              ┌─────────┴─────────┐
              │                   │
        ┌─────▼──────┐     ┌──────▼─────┐
        │  actors    │     │  entities  │
        │            │     │  (cap,     │
        │ (humans +  │     │   concept, │
        │  service   │     │   integ.,  │
        │  accts)    │     │   op,      │
        └─────┬──────┘     │   person)  │
              │            └──┬───┬───┬─┘
              │  audit_log    │   │   │
              │  (partitioned │   │   │  attributes (bi-temporal kv)
              │   by month)   │   │   ├──> facts        (bi-temporal narrative)
              ├───────────────┘   │   ├──> edges        (bi-temporal A→B)
              │                   │   └──> external_ids (upstream identifier map)
              │                   │
              │            ┌──────▼─────────┐
              │            │ adoptions /    │
              │            │ subscriptions /│
              │            │ notifications  │
              │            └────────────────┘
              │
              │            ┌────────────────┐
              └────────────│ workspaces +   │  ── private memory/notebooks
                           │ workspace_     │     scoped to actor or
                           │   entries      │     tenant; not in catalog
                           └────────────────┘
```

**Bi-temporal axes.** Attributes, facts, and edges all carry two time intervals:

- **Valid time** (`valid_from`, `valid_to`): when the fact was true in the world.
- **Transaction time** (`ingested_at`, `invalidated_at`): when the row was written / soft-deleted.

`?as_of=<iso8601>` on read endpoints time-travels in valid-time space. Reads that hit `valid_to IS NULL AND invalidated_at IS NULL` get the current authoritative state. Bi-temporal soft-delete via `invalidated_at` preserves history for audit; no row is ever hard-deleted by application code.

**Closed vocabularies.** Nine kinds, per-tenant:

| Kind | Purpose |
|---|---|
| `entity_type` | capability, concept, integration, operation, person |
| `edge_rel` | depends_on, composes, concept_of, operation_of, integrates_with, event_source, replaced_by, instance_of, conflicts_with, owned_by, … |
| `lifecycle_state` | alpha → beta → ga → deprecated → retired (commonly) |
| `visibility` | private, tenant-shared, public, regulated |
| `fact_category` | release_note, design_decision, overview, … |
| `notification_event_kind` | lifecycle.transitioned, capability.preview_version_created, … |
| `pii_category` | CONTACT, FINANCIAL, GOVERNMENT_ID, CREDENTIAL, … |

Tenant admins extend vocabularies via `POST /v1/admin/vocabularies/{kind}`. The closed set means writes against unknown values fail with HTTP 422 — typo'd values can't accidentally populate the catalog.

---

## Tenancy and visibility

Every row in every business-data table carries `tenant_id`. The catalog enforces isolation through one chokepoint:

- **`registry/service/governance/visibility.py::filter_entities()`** — returns the subset of input entities the caller can see, based on visibility level + tenant grants + adoption relationships.
- **`registry/service/governance/visibility.py::assert_visible()`** — single-entity variant; raises `TenantIsolationError` (mapped to HTTP 404 for the caller) if invisible.

Visibility levels:

| Level | Rule |
|---|---|
| `private` | Owner tenant only. |
| `tenant-shared` | Owner tenant only (alias for private at present; reserved for org-wide variants). |
| `public` | Every tenant can read. |
| `regulated` | Owner tenant only, plus per-tenant adoption grants. Writes carry stricter audit + retention semantics. |

Cross-tenant reads happen only via:

1. `visibility=public` rows.
2. An `adoptions` row recording an explicit consumer→provider relationship.

There is **no cross-tenant share mechanism** for workspaces, attributes, facts, or edges. Workspaces are tenant- or actor-scoped only.

---

## External integrations

| Integration | Direction | Purpose |
|---|---|---|
| **OIDC IdP** (any provider) | registry → IdP | JWT validation via discovery doc + JWKS. Production: real IdP. Local dev: `mock-oauth2-server`. |
| **Entitlement service** | registry → upstream | Grant resolution per request. Production: enterprise authority. Local dev: `mock-entitlement-service`. |
| **Sync sources** (GitHub, GitLab, OpenAPI, npm, ADR corpora) | sync workers → upstream | External-source ingest into the bi-temporal fact store. Credentials resolved dynamically from env at call time; never stored. |
| **Webhook subscribers** | registry → subscriber | Outbound webhook delivery for capability events. Signed with HMAC-SHA256 keyed by per-subscription secrets. |
| **Webhook senders** (GitHub, GitLab) | upstream → registry | Inbound webhook receivers at `/webhooks/github` and `/webhooks/gitlab`. Secrets read directly from env (`GITHUB_WEBHOOK_SECRET`, `GITLAB_WEBHOOK_SECRET`) for per-instance rotation without restart. |
| **OTLP collector** (Jaeger, Tempo, Honeycomb, OTel Collector) | registry → collector | Trace export over OTLP/HTTP. Disabled when `OTLP_ENDPOINT` is unset. |
| **Prometheus** | scraper → registry | Pull-based metrics at `/metrics`. |

---

## Storage

PostgreSQL 16 with the `pgvector` extension. Schema is managed by Alembic — `registry/storage/migrations/versions/` is the source of truth for the live shape; the SQLAlchemy models in `registry/storage/models.py` mirror it for typed query construction.

Notable storage patterns:

- **PgBouncer in transaction mode** sits between the app and Postgres. The asyncpg driver uses `prepared_statement_cache_size=0` to coexist with transaction-pooling.
- **Audit log** (`audit_log` table) is monthly-partitioned. The `check_audit_partition_ages` startup hook and recurring scheduler job warn when a partition is older than `audit_partition_max_age_days`. `scripts/partition_migrate.py` is the archival CLI.
- **Embeddings** (`embeddings` table) are HASH-partitioned by `tenant_id` into 8 partitions, created partitioned by the migration rather than cut over later, so there is one physical shape. The pipeline is an outbox and is polymorphic: a row is identified by `(target_type, target_id)`, where `target_type` is a closed vocabulary of `fact` and `claim`. Producers enqueue into `embedding_outbox` carrying the text and a chunk plan; the `embedding_drain` worker batches, embeds, and writes to `embeddings`. The drain never reads a source table, which is why adding a kind of target needs a new producer and no change to the consumer.
- **Closure cache** (`closure_cache` table) precomputes blast-radius traversals. `closure_outbox` drives invalidation; the `closure_refresh` worker repopulates on demand. Reads fall back to a recursive CTE when the cache is cold or `?as_of=` is older than 90 days.
- **Workspaces** carry `t_invalidated_at` for tenant-level soft-delete and `archived_at` for archive-without-delete.

---

## Background workers

Run inside the same uvicorn process via APScheduler (`SCHEDULER_USE_MEMORY_JOBSTORE=true` in local dev; SQLAlchemy job store otherwise).

| Worker | Interval | What it does |
|---|---|---|
| `_drain_webhooks` (`WebhookDeliveryWorker`) | `WEBHOOK_DRAIN_INTERVAL_S` (5s) | Picks up pending webhook deliveries, POSTs to subscriber URLs, retries with backoff, dead-letters after exhausting attempts. |
| `drain_outbox` (`embedding_drain`) | `OUTBOX_POLL_INTERVAL_S` (5s) | Embeds fact bodies queued in `embedding_outbox` and writes to `embeddings`. |
| `extraction_drain` (`ExtractionDrainWorker`) | `OUTBOX_POLL_INTERVAL_S` (5s) | Drains `memory_extraction_outbox`, calls the configured extraction provider, and stages the claims it proposes. Registered unconditionally, including under the no-op provider. |
| `consolidation_sweep` (`ConsolidationSweepWorker`) | `CONSOLIDATION_SWEEP_INTERVAL_S` (300s) | Reconciles staged claims against others about the same subject and predicate — agreement scores a claim up, disagreement marks both sides contested. |
| `promotion_sweep` (`PromotionSweepWorker`) | `PROMOTION_SWEEP_INTERVAL_S` (300s) | Proposes consolidated, subject-resolved claims for promotion, and auto-accepts the ones a tenant's own allowlist permits. See [operations/memory-curation.md](../06-operations/05-memory-curation.md). |
| `calibration_refit` (`CalibrationRefitWorker`) | `CALIBRATION_REFIT_INTERVAL_S` (21600s) | Refits confidence-calibration mappings per `(provider, model, strategy)` triple from judged (`adjudicate`d) outcomes. |
| `_expire_workspace_entries` | 1h | Honors workspace-entry retention. |
| `closure_refresh` (`ClosureRefreshWorker`) | `CLOSURE_REFRESH_INTERVAL_S` (5s) | Repopulates `closure_cache` rows the `closure_outbox` flagged as stale. |
| `check_audit_partition_ages` | startup + hourly | Warns if any audit partition exceeds the archival threshold. |

Sync jobs run via a separate `sync_worker.py` runner — same process, separate scheduler instance.

---

## Local development topology

Two providers, one topology. `make dev-up` runs each box as a local
process; `docker compose up -d` runs each as a container. The ports,
credentials, and environment are identical either way, so nothing
downstream — scripts, make targets, docs, or the diagram below — has to
know which is running:

```
┌──────────────┐    ┌──────────────────────┐    ┌─────────────────────────────┐
│ host curl /  │    │  mock OIDC provider  │    │ mock-entitlement-service    │
│ MCP client   │    │  :8090               │    │ :8091                       │
└──────┬───────┘    └──────────┬───────────┘    └─────────────┬───────────────┘
       │ JWT                   │ JWKS                          │ entitlements
       ▼                       │                               │
┌──────────────┐               │                               │
│ registry-api │◄──────────────┘                               │
│  :8000       │◄──────────────────────────────────────────────┘
└──────┬───────┘
       │ asyncpg
       ▼
┌──────────────────────────────────────┐
│ postgres (pgvector)                  │
│  :5544                               │
└──────────────────────────────────────┘

traces ──► OTLP :4318, viewable at :16686
```

Where the two differ:

- **Compose only:** pgbouncer on :6432 (the dev override already bypasses
  it), and the real Jaeger, Prometheus :9090, and Grafana :3000 rather
  than the local traces-and-metrics sink on :16686.
- **Native only:** Postgres comes from Postgres.app, a system install, or
  a pip-delivered build, and its data lives in `.devstack/pgdata`.

Bootstrap: `make dev-token && export TOKEN=$(make dev-jwt)`. See [quickstart.md](../02-get-started/01-quickstart.md).

---

## Key invariants

Things the codebase guarantees, encoded in tests + gates:

1. **Tenant isolation.** Every cross-tenant query goes through `service/governance/visibility.py`. Conformance test `tests/conformance/test_cross_tenant_isolation.py` verifies the invariant end-to-end.
1. **One writer per privileged table.** Creating a tenant row mints a principal in the authorization model; creating a staged claim asserts ontology conformance, a typed value, a resolved subject, provenance, and a visibility no broader than the subject. Those hold because exactly one module can write each table — a second writer would produce identical-looking rows enforcing none of them. `make privileged-writes` (`scripts/check_privileged_writes.py`) fails CI on a write from any other module, and covers UPDATE and DELETE as well as INSERT.
2. **Audit before mutation.** Operator overrides (progression bypass, RTBF) write the audit row first, then the override row, in a single transaction. If the override row write fails, the audit row remains as proof the bypass was attempted.
3. **Bi-temporal soft-delete.** No business-data row is hard-deleted by application code. `valid_to` (valid-time) and `invalidated_at` (transaction-time) record retirements.
4. **Settings is the env-var perimeter.** Code outside `registry/config.py` that reads `os.environ` directly carries a `# config: intentional` comment and a documented bypass reason (webhook secrets for rotation; per-connector credential refs). `scripts/check_no_doc_refs.py` enforces this and the documented-bypass rule.
5. **No external-doc references in shipped code.** Code comments must not reference `.context/` planning artifacts. Internal repo docs are fine. Gate: `scripts/check_no_doc_refs.py`.
6. **OIDC-only authentication.** There is no opaque-bearer token path. The `api_tokens` table was removed in migration `0021_entitlement_auth_consolidation`. Every authenticated request validates a JWT against the configured OIDC discovery URL.
7. **Visibility chokepoint coverage applies to MCP too.** MCP tool handlers resolve a `TenantContext` via the same OIDC + entitlement pipeline and run through the same service-layer visibility filters as REST.

---

## Deeper references

- **REST surface:** [reference/api.md](01-api.md)
- **MCP surface:** [reference/mcp-tools.md](02-mcp-tools.md)
- **Configuration:** [reference/configuration.md](03-configuration.md)
- **Authentication:** [overview/authentication.md](../01-overview/04-authentication.md)
- **Authorization:** [overview/authorization.md](../01-overview/05-authorization.md)
- **Operations runbook:** [operations/ops.md](../06-operations/01-ops.md)
- **Progression governance:** [operations/progression.md](../06-operations/02-progression.md)
- **Memory curation:** [operations/memory-curation.md](../06-operations/05-memory-curation.md)
- **CI pipeline:** [contributing/ci.md](../07-contributing/02-ci.md)
- **Architecture-quality threats + mitigations:** see the STRIDE artifact in `.context/architecture/` (planning repo) when reviewing security posture changes.
