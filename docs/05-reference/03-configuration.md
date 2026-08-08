# Configuration Reference

The canonical inventory of every environment variable the service reads is `.env.example` at the Context Plane project root. This document explains the variables and their operational meaning. The `.env.example` file is the single source of truth for defaults; if these two files disagree, `.env.example` wins.

`Settings` in `contextplane/config.py` is the single env-var reader. Code outside that file that reads `os.environ` directly is either documented as an intentional exception or is a bug.

**Two intentional exceptions** not in `Settings`:

1. `GITHUB_WEBHOOK_SECRET` and `GITLAB_WEBHOOK_SECRET` are read directly by `contextplane/ingest/webhook.py` to support per-instance secret rotation without a full settings reload.
2. Per-connector credentials (in `contextplane/ingest/`) are resolved by a dynamic reference string at runtime; the set is not fixed, so they cannot live in `Settings`.

---

## Required variables

These have no default. The app raises at startup if they are unset.

| Variable | Type | Description |
|---|---|---|
| `DATABASE_URL` | string | Async (asyncpg) connection string. Format: `postgresql+asyncpg://user:password@host:5432/registry`. The app process talks to PgBouncer in production; migrations talk to Postgres directly. |

---

## Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — (required) | Primary async connection string (asyncpg). |
| `PGBOUNCER_URL` | `$DATABASE_URL` | Runtime app → PgBouncer path. Defaults to `DATABASE_URL` when unset. |
| `SCHEDULER_JOBSTORE_URL` | `$DATABASE_URL` | URL for APScheduler's SQLAlchemyJobStore (durable job rows). Ignored when `SCHEDULER_USE_MEMORY_JOBSTORE=true`. |
| `SCHEDULER_USE_MEMORY_JOBSTORE` | `false` | Set `true` to use APScheduler's in-process MemoryJobStore. Jobs are lost on restart. Useful for local dev. |

---

## Embedding

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_PROVIDER` | `onnx` | Which implementation produces vectors: `onnx`, `sentence_transformers`, `http`, or `stub`. See below. |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Identifies the embedding space. Stamped into `embeddings.model_id`; the semantic arm only matches rows carrying the current value. |
| `EMBEDDING_MODEL_PATH` | `/opt/models/all-MiniLM-L6-v2` | Directory holding the staged model artifact. Container images bake it in at this path. |
| `EMBEDDING_DIM` | `384` | Stored vector width. Must match both the model and the `embeddings.vector` column; the app refuses to start on a mismatch. |
| `EMBEDDING_CHUNK_TOKENS` | `400` | Token budget per chunk when splitting fact bodies for embedding. |
| `EMBEDDING_CACHE_MAXSIZE` | `10000` | LRU cache size for previously-embedded chunks. |
| `EMBEDDING_HTTP_ENDPOINT` | — | Remote embeddings endpoint. Required when `EMBEDDING_PROVIDER=http`, ignored otherwise. |
| `EMBEDDING_HTTP_CONNECT_TIMEOUT_MS` | `500` | Connect timeout for the remote provider. |
| `EMBEDDING_HTTP_READ_TIMEOUT_MS` | `5000` | Read timeout for the remote provider. |
| `EMBEDDING_HTTP_MAX_RETRIES` | `2` | Retries after the first attempt. Server errors and timeouts are retried; 401/403 are not. |

### Choosing a provider

| Value | Runs | Needs network | Notes |
|---|---|---|---|
| `onnx` | in-process ONNX Runtime over `EMBEDDING_MODEL_PATH` | no | The default. The artifact ships inside the container image. |
| `sentence_transformers` | in-process torch | no, if `EMBEDDING_MODEL_PATH` is a local directory | Needs `pip install "registry[torch]"` — roughly 750 MB more, and more memory than the shipped resource limits allow. |
| `http` | a remote OpenAI-compatible endpoint | yes | No model in the process. Adds a network round trip to the query path. |
| `stub` | nothing — zero vectors | no | Smoke tests. Search still answers, but every semantic distance is identical, so ranking falls to the lexical arm. |

**A provider that cannot load stops the app.** There is no automatic downgrade to
zero vectors. Rows written by the stub are indistinguishable from real ones once
stored, so degrading silently would corrupt the index rather than reduce quality;
a process that refuses to start is recoverable, an index full of zero vectors is
not. `EMBEDDING_PROVIDER=stub` is the only way to get them, and it has to be asked
for.

For deployments with no egress, see the restricted-network section in
[the operations guide](../06-operations/01-ops.md).

> `EMBEDDING_MODEL=stub` was the previous way to select zero vectors. It still
> works and logs a deprecation warning; use `EMBEDDING_PROVIDER=stub`.

---

## Session-observation extraction

Context Plane's only LLM dependency, and entirely optional. A deployment that never
sets these captures and replays sessions normally and produces no session-derived
claims.

| Variable | Default | Description |
|---|---|---|
| `EXTRACTION_PROVIDER` | `noop` | Which provider turns session events into candidate claims: `noop`, `local`, `anthropic`, `openai`, or the name of an installed third-party provider. See below. |
| `EXTRACTION_MODEL` | `claude-haiku-4-5-20251001` | Model the extraction strategies request. Ignored by `noop` and `local`, which have no model to select. It is also part of the calibration key `(provider_id, model_id, strategy_id)`, so changing it points calibration at a fresh set of mappings rather than the ones a deployment has accumulated. **Set it explicitly when you select a provider other than `anthropic`** — see the note below. |
| `CONSOLIDATION_SWEEP_INTERVAL_S` | `300` | How often staged claims are reconciled against one another. Far wider than the embedding poll because a decision can cost a provider call; safe to widen because the sweep is idempotent, so a longer interval only means a staler answer rather than a wrong one. |
| `PROMOTION_SWEEP_INTERVAL_S` | `300` | How often consolidated claims are proposed for promotion, and auto-accepted where a tenant's own guardrails permit it. The allowlist is empty by default, so nothing auto-promotes until an operator opts a predicate in per tenant; widening the interval only makes the review queue and the canonical graph staler, never wrong. |
| `CALIBRATION_REFIT_INTERVAL_S` | `21600` | How often judged adjudications are refit into calibration mappings, one extraction strategy at a time. Hours-scale rather than minutes-scale: a mapping needs a couple hundred judged outcomes before it is even stored, so widening this only delays how soon a fresh mapping reflects the latest judged claims. |
| `EXTRACTION_TIMEOUT_S` | `60` | Per-call ceiling for the provider, in seconds. Extraction is never on the ingest hot path, so a generous timeout costs queue latency rather than request latency. |
| `EXTRACTION_API_KEY` | — | Credential for whichever provider needs one. Required when `EXTRACTION_PROVIDER` names a credentialed provider, ignored otherwise. Operator-supplied at deploy time; never committed. Held as a secret: it does not appear in `repr(Settings())` and error paths report header names only. |
| `CLAUDE_API_KEY` | — | **Deprecated alias** for `EXTRACTION_API_KEY`. Still accepted so existing deployments keep working. |
| `ANTHROPIC_API_KEY` | — | **Deprecated alias** for `EXTRACTION_API_KEY`, same as above. |
| `EXTRACTION_BASE_URL` | — | Endpoint extraction calls. Empty means the selected adapter's vendor default. **Security-relevant and change-controlled** — see below. |
| `EXTRACTION_AUTH_HEADER` | — | Header the credential is sent in. Empty means the adapter's own default: `x-api-key` for `anthropic`, `Authorization` for `openai`. |
| `EXTRACTION_AUTH_TEMPLATE` | — | How the credential is spelled inside that header, e.g. `Bearer {key}`. Must contain the literal `{key}` exactly once. Empty means the adapter's default. |
| `EXTRACTION_EXTRA_HEADERS` | — | Anything else the endpoint requires, as `Name:value,Name:value`. Held as a secret, because gateways routinely authenticate with a second header. |

### Precedence among the credential names

`EXTRACTION_API_KEY` is canonical. `CLAUDE_API_KEY` and `ANTHROPIC_API_KEY` are
accepted as deprecated aliases, and when more than one is set the canonical name
wins — declaration order is what makes that so.

There is deliberately no startup warning when a legacy name supplies the value.
The alias mechanism discards which name a value came from, so a warning here
could not tell whether the thing it warns about actually happened, and a warning
that cannot distinguish those is worse than none.

### `EXTRACTION_BASE_URL` is a data-egress decision

This is where session transcripts are sent. Changing it sends conversation
content to a different operator, so it belongs under the same review as any
other egress change rather than an ad-hoc config edit.

A non-`https` value warns at startup rather than failing: loopback and trusted
in-cluster addresses are legitimate. A URL carrying userinfo (`user:pass@host`)
is refused outright. Redirects are never followed — a compromised gateway
answering `302` would otherwise be handed the credential, since HTTP clients
strip `Authorization` across origins but know nothing about a custom auth
header.

### Third-party providers

A provider installed as a Python distribution declaring a
`contextplane.extraction_providers` entry point becomes a legal
`EXTRACTION_PROVIDER` value. The `EXTRACTION_PLUGIN_*` naming convention governs
variables read by that provider's own code, which lives outside this repository
— nothing here validates or documents them, and no gate in this repo enforces
that convention.

### Choosing a provider

`noop` is the default and pauses extraction entirely. Sessions are still captured
and replayed, and connector-fed claims still land — a deployment that sets none of
this is complete, not degraded. The state is logged once at startup so an absence
of claims has a visible explanation.

`local` is a small set of deterministic pattern rules. No key, no network, no model
artifact. It is what the local dev stack runs, so nothing downstream of extraction
needs a credential to work on, and it drives the whole pipeline end to end. Output
quality reflects the rule set rather than a model: claims record `local-rules-v1`
and usage is reported as estimated. Do not benchmark extraction against it.

`anthropic` and `openai` call a real model — the first through the Anthropic
Messages API, the second through any OpenAI-compatible chat-completions endpoint.
Both read the same transport settings, so either can be pointed at a gateway
instead of at the vendor. Selecting either without a key stops the app at startup
rather than falling back — a deployment that asked for a model and got nothing would
report healthy while producing nothing.

A name that is neither built in nor installed is refused the same way. A provider
supplied by another package joins this list by declaring an entry point; see
[bring your own provider](../06-operations/06-bring-your-own-provider.md).

### `EXTRACTION_MODEL` and the calibration key can disagree

A strategy that pins no model sends whichever model the selected provider
declares as its default. `EXTRACTION_MODEL` is a separate thing: it overrides
that globally when set, and it is the `model_id` written into every calibration
mapping's key.

Left at its default, those two are the same string only for `anthropic`. Select
`openai` and requests carry that adapter's declared default while calibration
evidence is still filed under `claude-haiku-4-5-20251001` — a model the
deployment never called. Nothing errors, and the mappings look correct until
somebody compares two providers' calibration and finds them keyed to the same
model.

**Set `EXTRACTION_MODEL` explicitly whenever `EXTRACTION_PROVIDER` is not
`anthropic`.** Its default cannot distinguish "unset" from "deliberately
haiku", which is why the variable is not simply emptied.

An unrecognized value also stops the app. A typo that quietly became `noop` would
look exactly like a deployment whose sessions contain nothing extractable.

The local dev stack pins `EXTRACTION_PROVIDER=local` and stays there even when a key
is present in the environment, so `make dev-up` behaves the same for every
developer. Set the variable explicitly to use a model.

See [Session extraction](../04-guides/05-session-extraction.md) for the operator
guide and [Operations](../06-operations/01-ops.md) for the queue runbook.

## Outbox + drain

| Variable | Default | Description |
|---|---|---|
| `CLOSURE_REFRESH_INTERVAL_S` | `5` | Interval for the closure-cache refresh drain. Correctness survives without it (CTE fallback); traversal latency does not. |
| `OUTBOX_POLL_INTERVAL_S` | `5` | Drain interval (seconds) for the embedding outbox. |
| `OUTBOX_BATCH_SIZE` | `32` | Max rows claimed per drain pass. |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Per-row retry ceiling before the outbox row moves to the dead-letter table. |
| `BACKFILL_BATCH_SIZE` | `64` | Page size for the backfill / reindex scripts. |

---

## Webhook delivery

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_DRAIN_INTERVAL_S` | `5` | Cadence for the WebhookDeliveryWorker drain job (seconds). The p95 SLO caps fan-out at 30 s; this default keeps well inside the SLO with headroom for retries. |
| `WEBHOOK_REQUEST_TIMEOUT_S` | `10.0` | Per-delivery HTTP timeout (seconds). |
| `WEBHOOK_BATCH_SIZE` | `50` | Max deliveries claimed per drain pass. |

---

## HTTP method routing

| Variable | Default | Description |
|---|---|---|
| `CONTEXTPLANE_HTTP_METHODS_MODE` | `rest` | `rest` — register standard verbs (PATCH / PUT / DELETE). `post_only` — register only POST-tunneled aliases. `both` — register both. Use `post_only` or `both` for deployments behind enterprise proxies that strip non-GET/POST verbs. |
| `CONTEXTPLANE_HTTP_METHOD_ALIAS_SEPARATOR` | `colon` | Separator in POST-tunneled aliases. `colon` → `/{id}:update`. `slash` → `/{id}/update`. |

---

## Authentication — JWT validation

Context Plane accepts OIDC JWT credentials only. There is no opaque-bearer / API-token path. See [Authentication](../01-overview/04-authentication.md) for the full claim contract.

| Variable | Default | Required when | Description |
|---|---|---|---|
| `OIDC_DISCOVERY_URL` | — | always (auth enabled) | OpenID Connect discovery document URL. Validator reads `issuer`, `jwks_uri`, and supported algorithms from this doc. |
| `OIDC_ISSUER_ALLOWLIST` | empty (legacy) | production | Comma-separated list of acceptable `iss` claim values. Tokens with a non-allowlisted issuer are rejected even if the signature validates. Empty = no allowlisting (NOT recommended in production). |
| `RESOURCE_URI_ALLOWLIST` | empty (legacy) | production | Comma-separated list of acceptable `aud` claim values. ADFS carries the resource URI here; this lists what this deployment will accept. |
| `OIDC_CLIENT_ID_ALLOWLIST` | empty | optional | Comma-separated list of acceptable `azp` / `client_id` values. Empty = check skipped (NOT recommended in production — any token from a trusted JWKS would pass). |
| `OIDC_MAX_TOKEN_TTL_SECONDS` | `900` | always | Context Plane-enforced upper bound on token lifetime. Tokens where `exp - iat` exceeds this — or where `iat` is absent — are rejected. Defense-in-depth against IdP misconfiguration issuing long-lived tokens. |

## Authentication — entitlement-service grant resolution

Once the JWT validates, Context Plane resolves grants by calling an external entitlement service keyed on the JWT's `sub`. See [Authorization](../01-overview/05-authorization.md) for the grant flow.

| Variable | Default | Required when | Description |
|---|---|---|---|
| `ENTITLEMENT_SERVICE_URL` | empty | always (auth enabled) | Base URL of the entitlement service. Setting this enables the entitlement-resolution path; the four fields below all become required (`Settings.__post_init__` raises otherwise). |
| `ENTITLEMENT_SERVICE_ENV` | empty | `ENTITLEMENT_SERVICE_URL` set | `env` query parameter passed to the entitlement service. Typically `PRD`, `NPD`, or `DEV`. |
| `ENTITLEMENT_SERVICE_DISCRIMINATOR` | empty | `ENTITLEMENT_SERVICE_URL` set | Per-deployment middle token of the entitlement grammar `<tenant_slug>_<DISCRIMINATOR>_<ROLE>`. Multiple registry-shaped services can share one entitlement endpoint with different discriminators (`REGISTRY`, `GRAPHREGISTRY`, …). Must be non-empty with no whitespace. |
| `ENTITLEMENT_ROLE_MAPPING` | empty dict | `ENTITLEMENT_SERVICE_URL` set | Comma-separated `EXTERNAL:internal` pairs mapping the upstream role suffix to one of `admin / producer / consumer / auditor`. Multiple external suffixes may map to the same internal role (covers LDAP rename rollouts). Example: `ADMIN:admin,PRODUCER:producer,CONSUMER:consumer,AUDITOR:auditor`. |
| `ENTITLEMENT_CONNECT_TIMEOUT_MS` | `250` | optional | TCP/TLS connect timeout to the entitlement service (milliseconds). Bounded because this call sits in the auth hot path on every cache miss. |
| `ENTITLEMENT_READ_TIMEOUT_MS` | `1500` | optional | Read timeout for the entitlement-service response (milliseconds). |
| `ENTITLEMENT_MAX_RETRIES` | `1` | optional | Maximum retries on network failure / 5xx / 429. Must be 0 or 1. |
| `ENTITLEMENT_CACHE_MAX_ENTRIES` | `10000` | optional | Per-process in-memory cache size for resolved grants. Per-entry TTL is bounded by the JWT's own `exp` claim, not by a separate setting. |

---

## Progression

| Variable | Default | Description |
|---|---|---|
| `PROGRESSION_DEFINITION_CACHE_TTL_SECONDS` | `60` | TTL (seconds) for the cached progression-definition lookup. `0` disables caching. Short TTL keeps the cache fresh after operator edits without a restart. |

---

## Rate limiting

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Set `false` / `0` / `no` to disable enforcement without redeploying. |
| `RATE_LIMIT_READ_PER_MINUTE` | `600` | Per-tenant read budget (GET/HEAD) per minute, per process. In a multi-process deployment the effective limit across N workers is up to N × this value. |
| `RATE_LIMIT_WRITE_PER_MINUTE` | `60` | Per-tenant write budget (POST/PUT/PATCH/DELETE) per minute, per process. |

---

## Usage recording

| Variable | Default | Description |
|---|---|---|
| `USAGE_RETENTION_DAYS` | `90` | How long raw usage events are kept. Permitted band **30–180**; a value outside it is refused at startup rather than clamped. |

Raw usage rows carry an actor identifier, which makes them personal data with an
erasure obligation — and the retention boundary is what keeps that bounded. Daily
rollups carry no actor identifier, are therefore not personal data, and are kept
indefinitely, so a report for a closed month remains reproducible after its raw
rows have gone.

A value outside the band is a startup error on purpose. Clamping would leave an
operator believing they had a year of raw history and finding out only when a query
returned less than it should, at which point the data no longer exists.

---

## Metrics exposition

| Variable | Default | Description |
|---|---|---|
| `METRICS_BEARER_TOKEN` | *(unset)* | **Required.** Bearer credential the Prometheus scraper must present on `GET /metrics`. Unset means `/metrics` returns `503` and serves nothing — there is no unauthenticated fallback. |

`/metrics` is not a harmless endpoint. Its exposition publishes process-global
counters: the full route table, entitlement-failure counts, rate-limit
rejections, and the MCP tool catalog with per-tool call counts. It is also a
rate-limit bypass prefix, so an uncredentialed endpoint is both unauthenticated
and unthrottled.

Generate one credential per deployment (`openssl rand -hex 32`) and supply it to
the scraper's own `authorization` block. Kubernetes discovery annotations remain
enabled, so a scraper that has not yet been given the credential reports the
target **down with a 401** rather than silently disappearing from the target
list — a visible failure rather than an unmonitored one.

---

## Observability

| Variable | Default | Description |
|---|---|---|
| `OTLP_EXPORTER_TIMEOUT_S` | `2` | Per-export timeout for the OTLP span exporter, in seconds. Deliberately short: tracing must not add latency to a request when the collector is slow or gone. |
| `OTLP_ENDPOINT` | — | OTLP HTTP endpoint for trace export (Jaeger, Honeycomb, Tempo, OTel Collector). Omit to disable tracing. Example: `http://otel-collector:4318/v1/traces`. |
| `SERVICE_NAME` | `registry` | Service name used in OTel resource attributes. |

---

## Logging and build identity

| Variable | Default | Description |
|---|---|---|
| `LOG_FORMAT` | `json` | `json` emits structured records to stdout, which is what a log collector wants. `console` emits human-readable lines for local work. |
| `LOG_LEVEL` | `INFO` | Root logger level. `DEBUG` surfaces SQLAlchemy statements, which is useful locally and far too noisy in production. |
| `BUILD_REVISION` | `unknown` | Identifies the running build, reported on the health surface and in structured logs. Set it from the commit at image build time (`BUILD_REVISION=$(git rev-parse HEAD)`); left unset it reads `unknown`, which makes a deployed version unattributable. |

## External sync

| Variable | Default | Description |
|---|---|---|
| `CONNECTOR_RUN_TIMEOUT_S` | `300` | Per-connector run timeout (seconds). Applies to the full connector coroutine including pagination. |
| `GITHUB_WEBHOOK_SECRET` | — | Webhook secret for GitHub ingest. Set in your deployment secret store; not committed. Read directly by `contextplane/ingest/webhook.py` (not via `Settings`) to support per-instance rotation without a reload. |
| `GITLAB_WEBHOOK_SECRET` | — | Webhook secret for GitLab ingest. Same pattern as `GITHUB_WEBHOOK_SECRET`. |

Per-connector credentials are not listed here — they are resolved by a dynamic reference string at runtime. Set them in your deployment's secret store under the names the connector definitions request.

---

## Performance / partitioning

| Variable | Default | Description |
|---|---|---|
| `EMBEDDINGS_PARTITION_COUNT` | `8` | Partition fan-out for the HASH-partitioned embeddings table. Changing this after initial setup requires a partition migration. |

---

## Closure refresh worker

| Variable | Default | Description |
|---|---|---|

---

## Deployment patterns

The env vars are the same regardless of deployment target. The only thing that varies is how you set them:

| Target | Mechanism |
|---|---|
| Docker Compose (local) | `docker-compose.yml` `environment:` section or `.env` file |
| Kubernetes | `ConfigMap` for non-secrets + `Secret` for secrets, both mounted as env vars |
| AWS ECS / Fargate | Task definition `environment` + `secrets` (from Secrets Manager or Parameter Store) |
| AWS Lambda | Function environment variables |
| EC2 / systemd | `EnvironmentFile=/etc/contextplane/env` (chmod 600, root-owned) |
| Google Cloud Run | Service environment variables + Secret Manager for secrets |

**Never commit secrets.** Database passwords, webhook secrets, OIDC client secrets, and API tokens are always operator-provided at deploy time, never checked into the repository.

## Agent Readiness Context (ARC)

| Variable | Default | Purpose |
|---|---|---|
| `ARC_GLOBAL_OPERATOR_ALLOWLIST` | *(empty)* | Exact `issuer\|subject` pairs permitted to write deployment-wide governance. Comma-separated. |
| `ARC_DRAFTER_MODEL_ENABLED` | `false` | Whether the model-backed drafter may serve. See "Drafter model decision gate" below -- this can never be more permissive than the committed decision artifact. |
| `ARC_DRAFTER_MODEL_ARTIFACT_PATH` | *(empty)* | Filesystem path to the deployment-local model artifact. Only read when `ARC_DRAFTER_MODEL_ENABLED=true`. |

### Drafter model decision gate

The model-backed drafter is gated by a committed decision artifact,
`contextplane/arc/drafter/model_decision.json`, not by `ARC_DRAFTER_MODEL_ENABLED`
alone. The artifact records an `outcome` of either `accepted` or `human_only`
plus the per-gate evaluation results that justify it, evaluated against a
version-controlled fixture corpus.

At startup, if `ARC_DRAFTER_MODEL_ENABLED=true`, the deployment refuses to
start unless the artifact's `outcome` is `accepted`, every one of its
evaluation gates passed, and `ARC_DRAFTER_MODEL_ARTIFACT_PATH` names a file
whose SHA-256 matches the artifact's recorded `model_artifact_digest`. Setting
the flag to `true` cannot make a `human_only` verdict, a failed gate, or a
swapped model artifact serve.

The committed decision as of this writing records `human_only`: no
evaluation could be executed against a real candidate model in the
environment that produced it (no deployment-local model artifact and no
network route to obtain or run one). `ARC_DRAFTER_MODEL_ENABLED` therefore
stays `false`, and the human structured proposal-editing form is the only
authoring path -- this weakens no review, approval, or activation contract.

### Why this is an allowlist and not a role

Every role in this system is tenant-scoped: each tenant has its own admins, so
`admin` cannot serve as the deployment trust root. If it could, an admin of any
tenant would be able to edit governance that binds every other tenant. Global
writes therefore match an exact `(issuer, subject)` pair — an identity no tenant
can grant itself.

The delimiter between issuer and subject is `|` rather than `:` because issuers
are URLs and already contain colons.

### Failure behaviour

**Empty grants nobody.** That is the correct default: a deployment that
configured nothing must not fall open on the one surface binding every tenant.
Global writes return `403` until an operator is configured.

**A malformed entry fails startup.** An entry with no delimiter, or with an empty
issuer or subject, raises rather than being skipped. Skipping would leave an
operator believing they have access when they do not, or an allowlist that looks
configured and is empty.

### What appears in audit

A SHA-256 fingerprint of the sorted list, never its membership. An audit log
enumerating privileged identities would hand an attacker the exact set of
principals worth compromising. The fingerprint is stable across orderings, so a
reordered configuration is not mistaken for a change.

