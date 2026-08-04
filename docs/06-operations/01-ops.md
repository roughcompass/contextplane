# Operations Runbook

Routine and emergency procedures for operators with database access and admin API credentials. For progression-definition operations, see [progression.md](02-progression.md). For usage data — retention, erasure, and what it may and may not be used for — see [usage-data.md](04-usage-data.md).

---

## Audit log partition archival

### Background

The `audit_log` table is range-partitioned by month. The service checks partition ages hourly and emits a Prometheus gauge and a WARNING log when any child partition's lower bound is older than 24 months:

- **Gauge:** `catalog_audit_partitions_eligible_for_archival`
- **Log level:** `WARNING`, with the list of eligible partition names
- **Threshold:** 24 months (fixed; re-evaluate retention period at each compliance review)

No automatic archival occurs — this is an intentional operator gate.

**Alert threshold:** Alert when `catalog_audit_partitions_eligible_for_archival > 0`.

### Prerequisites

- Postgres 14+ (`DETACH PARTITION ... CONCURRENTLY` requires 14+)
- `pg_dump` on the operator workstation or in an ops container
- Write access to your object-storage bucket for archived dumps
- A database session with superuser or table-owner privileges

### Step 1 — Identify eligible partitions

```sql
SELECT c.relname AS partition_name
FROM   pg_inherits i
JOIN   pg_class c ON c.oid = i.inhrelid
JOIN   pg_class p ON p.oid = i.inhparent
WHERE  p.relname = 'audit_log'
ORDER  BY c.relname;
```

Partitions named `audit_log_YYYY_MM` where the year/month is more than 24 months before today are eligible. Example: if today is 2026-05-12, `audit_log_2024_04` and earlier are eligible.

### Step 2 — Dump the partition to object storage

```bash
pg_dump \
  --table=audit_log_2024_04 \
  --format=custom \
  --no-owner \
  --no-acl \
  "$DATABASE_URL" \
  > audit_log_2024_04_$(date +%Y%m%d).pgdump

# Upload to your object-storage bucket
aws s3 cp audit_log_2024_04_$(date +%Y%m%d).pgdump \
  s3://<your-archive-bucket>/registry/audit_log/

# OR: gcloud storage cp, az storage blob upload, etc.
```

Verify the dump is readable before detaching:

```bash
pg_restore --list audit_log_2024_04_$(date +%Y%m%d).pgdump | head -5
```

### Step 3 — Detach the partition (non-blocking, Postgres 14+)

```sql
ALTER TABLE audit_log
  DETACH PARTITION audit_log_2024_04 CONCURRENTLY;
```

`CONCURRENTLY` allows reads and writes on the parent table to continue during the detach. It acquires a brief exclusive lock only at the end. Without `CONCURRENTLY` (Postgres 13 and earlier), the detach blocks all reads and writes on `audit_log`.

### Step 4 — Verify the parent table is intact

```sql
-- Count rows in the parent (should not change)
SELECT COUNT(*) FROM audit_log;

-- Confirm the detached partition no longer appears in pg_inherits
SELECT c.relname
FROM   pg_inherits i
JOIN   pg_class c ON c.oid = i.inhrelid
JOIN   pg_class p ON p.oid = i.inhparent
WHERE  p.relname = 'audit_log'
  AND  c.relname = 'audit_log_2024_04';
-- Should return 0 rows
```

### Step 5 — Drop or keep the detached table

The detached partition is now an ordinary table. You may drop it once you've confirmed the dump is safely archived:

```sql
DROP TABLE audit_log_2024_04;
```

Or keep it around for a grace period. If you keep it, rename it to avoid confusion:

```sql
ALTER TABLE audit_log_2024_04 RENAME TO _archived_audit_log_2024_04;
```

### Step 6 — Record the archival event

Insert a housekeeping row into `episodes` so the operation is traceable via the audit trail:

```sql
INSERT INTO episodes (
    episode_id,
    tenant_id,
    episode_type,
    source_id,
    content_summary,
    ts,
    ingested_at
) VALUES (
    gen_random_uuid(),
    '00000000-0000-0000-0000-000000000000',  -- system tenant
    'audit_partition_archived',
    'ops/partition_archival',
    'Archived audit_log_2024_04; dump at s3://<your-archive-bucket>/registry/audit_log/audit_log_2024_04_<date>.pgdump',
    now(),
    now()
);
```

### Restore from archive (if needed)

To restore a dumped partition:

```bash
# Download the dump
aws s3 cp \
  s3://<your-archive-bucket>/registry/audit_log/audit_log_2024_04_<date>.pgdump \
  /tmp/audit_log_2024_04.pgdump

# Restore as a new table (not re-attached automatically)
pg_restore \
  --dbname="$DATABASE_URL" \
  --table=audit_log_2024_04 \
  /tmp/audit_log_2024_04.pgdump
```

To re-attach as a partition:

```sql
ALTER TABLE audit_log
  ATTACH PARTITION audit_log_2024_04
  FOR VALUES FROM ('2024-04-01') TO ('2024-05-01');
```

---

## Rotating webhook secrets

**Why rotate:** if a subscription secret is compromised or you are following your organization's routine credential-rotation policy.

### Subscription webhook secret

Subscription secrets are stored per-subscription in the database. To rotate:

1. Generate a new secret (keep it secret — it is the HMAC signing key):

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Update the subscription via the admin API:

   ```bash
   curl -X PATCH \
     "https://api.example.com/v1/admin/tenants/<tenant_id>/subscriptions/<subscription_id>" \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"webhook_secret": "<new-secret>"}'
   ```

3. Update the subscriber's endpoint to verify the new secret. Until the subscriber is updated, deliveries will have a valid signature under the new secret but the subscriber's verification code will reject them. Plan the cutover to minimise the verification gap (or briefly accept both secrets in the subscriber code during the transition).

4. Verify the signature format:

   Webhook deliveries include the header `X-Registry-Signature-256: sha256=<hex>`. The HMAC is computed over the raw request body with the subscription's secret using SHA-256. Verify:

   ```python
   import hmac, hashlib
   expected = "sha256=" + hmac.new(
       secret.encode(), body, hashlib.sha256
   ).hexdigest()
   assert hmac.compare_digest(expected, received_header)
   ```

### Sync webhook secrets (GitHub / GitLab)

The `GITHUB_WEBHOOK_SECRET` and `GITLAB_WEBHOOK_SECRET` env vars are read directly by the sync layer on each request — not cached at startup — so rotation does not require an app restart:

1. Generate a new secret.
2. Update the secret in your deployment's secret store (Kubernetes Secret, AWS Secrets Manager, etc.).
3. If your platform re-injects env vars without a restart, the new secret takes effect immediately. If not, perform a rolling restart.
4. Update the corresponding webhook configuration in GitHub or GitLab to use the new secret.

---

## Log output and trace correlation

### Breaking change — log format is now JSON

The service previously emitted unformatted plain-text lines via Python's default
stdlib logging handler (no formatter configured). Log output is now a single JSON
object per line, written to stdout.

**Any log shipper configured to parse plain-text lines must be reconfigured.**
Set `LOG_FORMAT=text` as a temporary escape hatch while you update your shipper
pipeline; see [LOG_FORMAT=text guidance](#log_formattext-guidance) below.

### JSON field reference

Every log line in JSON mode contains the following keys. Keys are lowercase with
underscores. Optional fields are absent — not null — when the condition is not met.

| Field | Always present | Type | Description |
|---|---|---|---|
| `timestamp` | yes | string | ISO 8601 UTC timestamp: `2026-05-12T14:03:22.417456Z`. |
| `level` | yes | string | Lowercase severity: `debug`, `info`, `warning`, `error`, `critical`. |
| `logger` | yes | string | Module name from `logging.getLogger(__name__)`, e.g. `registry.workers.webhook_delivery`. |
| `event` | yes | string | Log message. Positional `%s` arguments are interpolated before structlog sees the record. |
| `trace_id` | conditional | string | 32-character lowercase hex OTel trace ID. Present only when the log line is emitted inside an active OTel span. |
| `span_id` | conditional | string | 16-character lowercase hex OTel span ID. Present only when inside an active span. |
| `exception` | conditional | string | Formatted traceback string. Present when `_log.exception(...)` or `exc_info=True` is used. Newlines within the traceback are serialized as `\n` escape sequences — the full line remains a single parseable JSON object. |

Example JSON line (inside a traced request):

```json
{"timestamp": "2026-05-12T14:03:22.417456Z", "level": "info", "logger": "registry.api.routers.entities", "event": "entity created", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7"}
```

### Platform-specific trace correlation

`trace_id` and `span_id` follow the OTel field-naming convention. Some platforms
need a one-time pipeline mapping step.

**Splunk**

Configure the log source type to JSON or add `INDEXED_EXTRACTIONS = json` to your
`inputs.conf` stanza. Once enabled, `trace_id` becomes a directly searchable index
field:

```
index=registry trace_id=4bf92f3577b34da6a3ce929d0e0e4736
```

**Dynatrace**

When the Dynatrace OneAgent log module is enabled, `trace_id` is recognized
automatically and linked to the corresponding distributed-trace record in the
Dynatrace UI. No additional pipeline mapping is required.

**Datadog**

Datadog's APM correlation requires the field names `dd.trace_id` and `dd.span_id`.
Configure a Datadog log pipeline processor to remap:

- `trace_id` → `dd.trace_id`
- `span_id` → `dd.span_id`

Once mapped, log lines correlate to APM traces in the Datadog UI. This is a
one-time pipeline configuration step; no application code change is needed.

**Grafana Loki**

Use the `json` parser stage in your Promtail or Alloy pipeline config to extract
`trace_id` and `span_id` as log labels or structured metadata fields. Example query
to pivot from a trace ID to its log lines:

```logql
{app="registry"} | json | trace_id="4bf92f3577b34da6a3ce929d0e0e4736"
```

### `LOG_FORMAT=text` guidance

Set `LOG_FORMAT=text` when:

- Running locally and you want human-readable, colour-coded output in the terminal.
- Operating in an environment where the log collector cannot parse JSON (e.g. a
  legacy syslog forwarder or a CI system whose log capture strips JSON structure).
- Debugging a live issue where multi-line tracebacks are easier to read than
  JSON-escaped strings.

In `text` mode the output is not parseable as JSON. Trace IDs and span IDs are
still present in the log lines but are formatted for human readability rather than
machine extraction. Do not use `text` mode in production environments that route
logs to a structured shipper.

### Local development

Add the following to your local `.env` file for a comfortable development experience:

```
LOG_FORMAT=text     # human-readable output; avoids JSON noise in your terminal
LOG_LEVEL=DEBUG     # optional — surfaces SQLAlchemy query strings and OTel SDK
                    # internals; high-volume; use only when diagnosing issues
```

`LOG_LEVEL=DEBUG` makes the root logger emit records from every dependency
(SQLAlchemy, httpx, FastAPI, OTel SDK). This is useful for tracing an unexpected
query or header, but expect significantly more output than `INFO`. Reserve `DEBUG`
for targeted diagnosis sessions, not always-on development.

---

## Replaying failed webhook deliveries

The `notification_deliveries` table tracks every attempted delivery. Rows with `status='failed'` have exhausted retries (failed on a 4xx that is not 408 or 429). Rows with `status='pending'` and `next_retry_at` in the future are still in the retry queue.

### Identify failed deliveries

```sql
SELECT
    d.id,
    d.subscription_id,
    d.notification_id,
    d.status,
    d.attempt_count,
    d.last_attempt_at,
    d.last_error
FROM notification_deliveries d
WHERE d.tenant_id = '<tenant_uuid>'
  AND d.status = 'failed'
ORDER BY d.last_attempt_at DESC
LIMIT 50;
```

### Replay a delivery

To reset a failed delivery so the worker will retry it:

```sql
UPDATE notification_deliveries
SET
    status          = 'pending',
    next_retry_at   = now(),
    attempt_count   = 0,
    last_error      = NULL
WHERE id = '<delivery_uuid>'
  AND tenant_id = '<tenant_uuid>';
```

The worker picks it up on the next drain pass (at most `WEBHOOK_DRAIN_INTERVAL_S` seconds, default 5). If the subscriber endpoint is still returning 4xx errors, the delivery will fail again permanently.

### Bulk replay by subscription

```sql
UPDATE notification_deliveries
SET
    status          = 'pending',
    next_retry_at   = now(),
    attempt_count   = 0,
    last_error      = NULL
WHERE subscription_id = '<subscription_uuid>'
  AND tenant_id       = '<tenant_uuid>'
  AND status          = 'failed';
```

---

## Draining a stuck extraction queue

Session extraction runs off `lmm_extraction_outbox`. An event write enqueues one row
per enabled strategy per session, in the same transaction as the event; the
`extraction_drain` scheduler job claims eligible rows, calls the provider, and stages
what conforms. Rows that exhaust their retries — or fail terminally — move to
`lmm_extraction_outbox_failed`.

If extraction has stopped producing claims, the queue tells you which of three
things is happening: nothing is queued, rows are backing off, or rows are
dead-lettering.

### Is anything queued at all

```sql
SELECT
    strategy_id,
    count(*)                                          AS rows,
    count(*) FILTER (WHERE next_attempt_at IS NULL)   AS eligible_now,
    count(*) FILTER (WHERE next_attempt_at > now())   AS backing_off,
    min(enqueued_at)                                  AS oldest,
    max(attempts)                                     AS worst_attempts
FROM lmm_extraction_outbox
GROUP BY strategy_id
ORDER BY strategy_id;
```

An empty result with sessions being written means nothing is enqueueing. Two causes,
both intentional:

- `EXTRACTION_PROVIDER` is `noop`. Strategies are not enabled at all in that case,
  so no queue rows are written — queueing work to drain into nothing would cost a
  write per event for no result. `registry_extraction_outbox_pending` sits at 0.
- Every strategy is disabled for that tenant. Check
  `GET /v1/admin/extraction-strategies`.

A non-empty result where `oldest` keeps receding while `eligible_now` stays high
means the drain is not running. Check that the `extraction_drain` job is registered
and that the scheduler is up; the job is registered unconditionally, including under
`noop`, so its absence is a wiring fault rather than a configuration choice.

### Rows backing off

Retriable failures back off on a fixed schedule — 30s, 60s, 120s — and then
dead-letter. The delay is stored on the row rather than slept in the worker, so a
restart does not lose it.

```sql
SELECT strategy_id, session_id, attempts, next_attempt_at, left(last_error, 200)
FROM lmm_extraction_outbox
WHERE next_attempt_at IS NOT NULL
ORDER BY next_attempt_at
LIMIT 50;
```

`last_error` names the class of failure. Rate limiting and provider 5xx are
retriable; an authentication rejection is not and never reaches this table — it
dead-letters immediately, because three retries on a rejected credential is three
more identical calls and the backoff would hide the real problem behind an
apparently busy queue.

To make a backing-off row eligible immediately after fixing the cause:

```sql
UPDATE lmm_extraction_outbox
SET next_attempt_at = NULL, attempts = 0, last_error = NULL
WHERE outbox_id = '<outbox_uuid>';
```

Writing a new event to the same session does this too, on purpose: the earlier
failure may have been about the window, and there is now more of it.

### Rows that dead-lettered

```sql
SELECT
    strategy_id,
    session_id,
    from_seq,
    through_seq,
    attempts,
    failed_at,
    left(last_error, 300) AS error
FROM lmm_extraction_outbox_failed
WHERE tenant_id = '<tenant_uuid>'
ORDER BY failed_at DESC
LIMIT 50;
```

The window (`from_seq`..`through_seq`) is kept so you can decide between fixing a
prompt and replaying the turns. `attempts` distinguishes an exhausted retriable
failure from a terminal one that never retried.

Grouping by error tells you whether this is one bad session or a systemic problem:

```sql
SELECT strategy_id, left(last_error, 80) AS error, count(*)
FROM lmm_extraction_outbox_failed
WHERE failed_at > now() - INTERVAL '24 hours'
GROUP BY 1, 2
ORDER BY 3 DESC;
```

### Replaying a dead-lettered window

Re-queue the window and delete the dead-letter row in one transaction, so a crash
between the two cannot both lose the row and leave it queued:

```sql
BEGIN;

INSERT INTO lmm_extraction_outbox
    (tenant_id, actor_id, session_id, strategy_id, from_seq, through_seq)
SELECT tenant_id, actor_id, session_id, strategy_id, from_seq, through_seq
FROM lmm_extraction_outbox_failed
WHERE failed_id = '<failed_uuid>'
ON CONFLICT (tenant_id, actor_id, session_id, strategy_id) DO UPDATE
SET through_seq     = GREATEST(lmm_extraction_outbox.through_seq, EXCLUDED.through_seq),
    from_seq        = LEAST(lmm_extraction_outbox.from_seq, EXCLUDED.from_seq),
    next_attempt_at = NULL,
    attempts        = 0,
    last_error      = NULL;

DELETE FROM lmm_extraction_outbox_failed WHERE failed_id = '<failed_uuid>';

COMMIT;
```

**Fix the cause first.** Replaying a window that dead-lettered on a bad prompt
spends provider calls to produce the same refusals. Check the conformance metrics
before replaying:

```
registry_extraction_strategy_conformance_ratio{strategy}
registry_extraction_rejected_total{strategy,reason}
```

If the strategy is below its conformance target over a real sample, it is reported
as a defective prompt and replaying it is wasted spend. Change the prompt through
`PATCH /v1/admin/extraction-strategies/{strategy_id}` first.

### Rows for a strategy that no longer exists

A rollback can leave rows naming a strategy the running build does not have. Those
dead-letter on their first claim rather than looping, because no number of attempts
makes an unknown strategy known. To confirm:

```sql
SELECT DISTINCT strategy_id FROM lmm_extraction_outbox_failed
WHERE last_error LIKE 'unknown strategy%';
```

Re-deploying the build that had the strategy and replaying the windows is the
recovery. Deleting the rows is also valid — the source events are untouched, so
re-queueing later is always possible.

### Discarding a queue entirely

Safe: the source events are the record and the queue is derived. Losing a queue
loses only the extraction, and the same window can be re-enqueued from the events.

```sql
DELETE FROM lmm_extraction_outbox WHERE tenant_id = '<tenant_uuid>';
```

An erasure request removes the actor's queue rows and dead-letter rows in the same
transaction as their events, so no extraction work survives pointing at deleted
material. The erasure receipt reports each table separately — a single total cannot
be checked against anything.

## Refreshing the closure cache

The `closure_cache` table holds the pre-computed transitive closure of entity edges. It is warmed lazily via the `closure_outbox` — edge mutations enqueue a refresh row, and the `ClosureRefreshWorker` processes them. Reads fall back to a recursive CTE when the cache is cold.

### When to manually trigger a rebuild

- After a bulk import of edges that bypassed the outbox pattern.
- After a `TRUNCATE closure_cache` (note: `TRUNCATE` does not seed the outbox — rows stay warm via natural edge mutations).
- When blast-radius queries are unexpectedly slow and `cache_hit: false` is appearing in results.

### Check cache health

```sql
-- Count cached closures
SELECT COUNT(*) FROM closure_cache WHERE tenant_id = '<tenant_uuid>';

-- Check outbox backlog
SELECT COUNT(*) FROM closure_outbox WHERE tenant_id = '<tenant_uuid>';

-- Check recent refresh timestamps
SELECT
    MIN(refreshed_at) AS oldest_entry,
    MAX(refreshed_at) AS newest_entry,
    COUNT(*)          AS total_rows
FROM closure_cache
WHERE tenant_id = '<tenant_uuid>';
```

### Force a rebuild for a specific entity

Insert an outbox row to trigger a refresh for one entity:

```sql
INSERT INTO closure_outbox (id, tenant_id, entity_id, edge_op, created_at)
VALUES (gen_random_uuid(), '<tenant_uuid>', '<entity_uuid>', 'upsert', now())
ON CONFLICT DO NOTHING;
```

The worker picks this up on the next drain cycle and upserts the full forward and reverse closure for that entity.

### Clear stale cache entries

The nightly maintenance job deletes closure rows with `refreshed_at < now() - 90 days`. To run this manually:

```sql
DELETE FROM closure_cache
WHERE refreshed_at < now() - interval '90 days';
```

---

## Tenant onboarding

Onboarding a new production tenant requires two steps: creating the tenant record and seeding its vocabulary.

### Step 1 — Create the tenant

Via the admin API (requires an existing admin-level token):

```bash
curl -X POST \
  "https://api.example.com/v1/admin/tenants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "<tenant-slug>",
    "display_name": "<Tenant Display Name>"
  }'
```

The response includes the `tenant_id` UUID. Save it — you need it for subsequent calls.

### Step 2 — Seed vocabulary

Closed-vocabulary values (entity types, edge relationship types, lifecycle states, visibility values) must be seeded before any entity can be created. Common values to seed:

```bash
# Seed entity types
for TYPE in service library component platform; do
  curl -X POST \
    "https://api.example.com/v1/admin/tenants/<tenant_id>/vocabulary" \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"vocab_type\": \"entity_type\", \"value\": \"$TYPE\"}"
done
```

Repeat for `lifecycle_state` values (`active`, `deprecated`, `archived`, `experimental`), `edge_rel` values, and any other closed vocabulary your deployment uses.

### Step 3 — Grant the first admin in the entitlement service

The registry does not mint or store credentials — role grants live in the entitlement service and are resolved per-request from the validated JWT's `sub` claim. Grant the new tenant's first admin by adding an entitlement string in the upstream entitlement service:

```
<tenant_slug>_<ENTITLEMENT_SERVICE_DISCRIMINATOR>_ADMIN
```

For example, with `ENTITLEMENT_SERVICE_DISCRIMINATOR=REGISTRY` and tenant slug `acme`:

```
acme_REGISTRY_ADMIN
```

Map this entitlement to the admin user's `sub` (whatever your IdP issues — email, employee ID, OIDC `sub`). On the user's next authenticated request, the resolver returns the entitlement, parses it to `(tenant_slug=acme, role=admin)`, and JIT-materialises the matching actor row. No registry-side script runs; no plaintext token is generated.

The exact mechanism for editing the entitlement service is deployment-specific (LDAP write, IAM console, custom UI, ticket workflow). Refer to your entitlement-service operator runbook.

---

## Applying database migrations

Migrations use Alembic and must be applied before or alongside a service deployment.

```bash
export DATABASE_URL=postgresql+asyncpg://user:password@host:5432/registry
make migrate        # equivalent to: alembic upgrade head
```

**Before applying in production:**

1. Take a database snapshot/backup.
2. Review the migration files (`registry/registry/storage/migrations/versions/`) to understand what schema changes are being applied.
3. Apply during a maintenance window for destructive migrations (column drops, table renames).

**Rolling back:** Alembic supports downgrade steps for each migration. To roll back one revision:

```bash
cd registry && alembic downgrade -1
```

Not all migrations have a downgrade path — check the migration file before assuming a downgrade is safe.

---

## Backfilling and reindexing embeddings

When bulk-imported entities are missing embeddings, run the backfill script:

```bash
python scripts/backfill_embeddings.py
```

To move to a different embedding model, reindex under the new model id:

```bash
python scripts/reindex_embeddings.py --new-model-id <new_model_id>
```

`--new-model-id` is required. The reindex is **additive, not destructive**: it
inserts new rows and leaves the existing ones in place, so the old model stays
queryable throughout. Both scripts are safe to run while the service is live.

The cutover is the restart, not the script. The semantic arm only reads rows
whose `model_id` matches the running `EMBEDDING_MODEL`, so:

1. Run the reindex with the new id. Nothing changes for live traffic — the
   service is still reading rows under the old id.
2. Restart the API with `EMBEDDING_MODEL=<new_model_id>`. Search switches over
   in one step.
3. Once you are satisfied, delete the old rows:
   `DELETE FROM embeddings WHERE model_id = '<old_model_id>';`

If you restart before the reindex finishes, entities that have not been
reindexed yet drop out of semantic results until it completes. The lexical and
graph arms are unaffected, so search still returns answers.

Both scripts use `BACKFILL_BATCH_SIZE` (default 64) to control page size and
require `DATABASE_URL`.

---

## Restricted-network and air-gapped deployment

The service performs **no network calls to obtain its embedding model**. The
model artifact is a layer in the container image, staged at build time and read
from `EMBEDDING_MODEL_PATH` (`/opt/models/all-MiniLM-L6-v2` by default). A
running container needs egress only to Postgres and to whatever OIDC and
entitlement services you configure.

The image also sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`. Those are
defence in depth, not the mechanism: if some future code path reaches for a
model host, it fails immediately instead of hanging against an unreachable one.

### Verifying there is no egress

```bash
docker run --rm --network none \
  -e EMBEDDING_PROVIDER=onnx \
  --entrypoint python ghcr.io/roughcompass/registry:<tag> \
  scripts/verify_embedding_model.py --model-path /opt/models/all-MiniLM-L6-v2
```

With no network interface at all, this loads the model and embeds text. If it
prints `embedding artifact ok`, nothing in the embedding path needs the network.

### Building from an internal mirror

Build hosts that cannot reach the public model host stage the artifact from an
approved channel instead. The layout must mirror the manifest paths
(`onnx/model.onnx`, `tokenizer.json`, …); checksums from
`registry/embedding/model_manifest.json` are enforced either way, so a mirror
cannot substitute different weights.

```bash
# from an internal artifact store
docker build --build-arg EMBEDDING_MODEL_SOURCE=https://artifacts.corp/minilm .

# or stage it by hand first — --source also accepts a local directory
python scripts/fetch_embedding_model.py --out ./model --source /mnt/approved/minilm
python scripts/fetch_embedding_model.py --out ./model --verify-only
```

### Using an internal embedding service instead

Deployments that already run an approved embeddings endpoint can skip the
in-process model entirely, which removes ~340 MB of resident memory and lets the
pod run at the smaller resource limits:

```
EMBEDDING_PROVIDER=http
EMBEDDING_HTTP_ENDPOINT=https://llm-gateway.internal/v1/embeddings
EMBEDDING_MODEL=<an id unique to that endpoint>
EMBEDDING_DIM=<the width that endpoint returns>
```

Give the endpoint its own `EMBEDDING_MODEL` id. The semantic arm filters on
`model_id`, so a distinct id keeps its vectors from being compared against
vectors from a different model. If `EMBEDDING_DIM` differs from the stored
width, the app refuses to start until the column is migrated — see below.

### Changing the vector width

`EMBEDDING_DIM` must match the `embeddings.vector` column. Startup checks this
and refuses to boot on a mismatch rather than letting the drain fail silently in
the background.

Changing it is destructive: a stored vector cannot be converted to a different
width, only recomputed. The migration therefore requires a second, explicit
opt-in, so an unattended deploy with a mistyped value fails instead of erasing
the index:

```bash
EMBEDDING_DIM=1536 EMBEDDING_DIM_ALLOW_REBUILD=true alembic upgrade head
```

That drops every embeddings row, widens the column, rebuilds the HNSW indexes,
and re-enqueues every fact *and every consolidated claim* for the drain — the truncate
removes both kinds, so a fact-only re-enqueue would leave the claim half of the index
permanently empty. **Semantic recall is degraded from the
moment it runs until the drain catches up** — size the maintenance window
against your fact count and `OUTBOX_BATCH_SIZE`.

### Smaller artifacts

The shipped artifact is the fp32 ONNX export (~90 MB). Quantised int8 exports
are ~23 MB, but they are architecture-specific (arm64 / avx512 / avx2), which
breaks a single multi-arch image, and they drift from the reference vectors
further than the parity bar allows. Operators who want the smaller footprint and
accept the recall difference can stage one themselves and point
`EMBEDDING_MODEL_PATH` at it — give it a distinct `EMBEDDING_MODEL` id so its
vectors stay separated from the ones already stored.

---

## Disaster recovery

### Backup configuration

The service uses Postgres as its sole storage backend. Point-in-time recovery requires two components configured by the operator before an incident occurs: WAL archiving and periodic base backups.

**WAL archiving** — edit `postgresql.conf` (or set via Helm values):

```ini
wal_level = replica
archive_mode = on
archive_command = 'aws s3 cp %p s3://<your-wal-bucket>/wal/%f'

# Flush WAL to the archive on a schedule even under low write volume.
# Set this to a value that satisfies your recovery-point objective.
archive_timeout = 3300   # seconds (55 min) — adjust to your RPO requirement
```

Restart Postgres after changing `wal_level` or `archive_mode` — these settings require a server restart and cannot be applied with `pg_reload_conf()`.

Verify archiving is active:

```sql
SELECT pg_walfile_name(pg_current_wal_lsn()),
       last_archived_wal,
       last_archived_time,
       last_failed_wal
FROM   pg_stat_archiver;
```

`last_failed_wal` must be `NULL`. If it is set, fix the archive command before taking a base backup.

**Base backup** — take one immediately after enabling WAL archiving, then on a recurring schedule:

```bash
pg_basebackup \
  --host=<DB_HOST> \
  --port=5432 \
  --username=postgres \
  --pgdata=/tmp/base_backup \
  --format=tar \
  --gzip \
  --wal-method=stream \
  --checkpoint=fast \
  --label="catalog-$(date +%Y%m%d)"

aws s3 sync /tmp/base_backup/ s3://<your-wal-bucket>/base/$(date +%Y%m%d)/
rm -rf /tmp/base_backup
```

**Daily logical backup** — supplemental to WAL archiving; provides a human-readable snapshot independent of the physical backup format:

```bash
pg_dump \
  --format=custom \
  --compress=9 \
  --file=/tmp/registry-$(date +%Y%m%d).dump \
  "$DATABASE_URL"

aws s3 cp /tmp/registry-$(date +%Y%m%d).dump \
  s3://<your-archive-bucket>/logical/registry-$(date +%Y%m%d).dump

# Verify the dump is readable before discarding the local copy
pg_restore --list /tmp/registry-$(date +%Y%m%d).dump | wc -l

rm /tmp/registry-$(date +%Y%m%d).dump
```

Recommended schedule: `0 02 * * *` (02:00 UTC daily). Retention periods (logical dumps, WAL archives, base backups) are operator-defined and must reflect your organization's data-recovery SLA.

### Point-in-time restore procedure

Use this procedure to restore the database from a base backup and WAL replay to a target point in time. Complete the backup-configuration steps above and confirm WAL archiving is healthy before an incident requires them.

**Step 1 — Stop the application**

Scale the service to zero replicas before beginning restore to prevent split-brain writes:

```bash
kubectl scale deployment capability-fabric --replicas=0 -n catalog
```

**Step 2 — Identify the target recovery time and base backup**

List available base backups:

```bash
aws s3 ls s3://<your-wal-bucket>/base/ --recursive | sort
```

Choose the newest base backup whose timestamp is before the target recovery time.

**Step 3 — Restore the base backup to a new Postgres data directory**

```bash
mkdir -p /var/lib/postgresql/restore
chmod 700 /var/lib/postgresql/restore

aws s3 sync s3://<your-wal-bucket>/base/YYYYMMDD/ /tmp/base_restore/

cd /var/lib/postgresql/restore
tar -xzf /tmp/base_restore/base.tar.gz
```

**Step 4 — Configure WAL replay**

Create `/var/lib/postgresql/restore/postgresql.auto.conf`:

```ini
restore_command = 'aws s3 cp s3://<your-wal-bucket>/wal/%f %p'

# Remove or comment out to replay all available WAL
recovery_target_time = '2026-05-07 03:00:00 UTC'
recovery_target_action = 'promote'
```

Create the recovery signal file (Postgres 12+):

```bash
touch /var/lib/postgresql/restore/recovery.signal
```

**Step 5 — Start Postgres and monitor WAL replay**

```bash
pg_ctl start -D /var/lib/postgresql/restore -l /var/log/postgresql/restore.log

tail -f /var/log/postgresql/restore.log | grep -E 'restored|recovery|promoted'
```

Postgres emits `LOG: database system is ready to accept connections` once recovery is complete and the instance is promoted.

**Step 6 — Verify data integrity**

```sql
SELECT
    c.relname   AS partition,
    pg_size_pretty(pg_relation_size(c.oid)) AS size
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_class p ON p.oid = i.inhparent
WHERE p.relname IN ('audit_log', 'episodes', 'embeddings')
ORDER BY p.relname, c.relname;

SELECT COUNT(*) FROM audit_log;
SELECT COUNT(*) FROM episodes;
SELECT COUNT(*) FROM embeddings;
```

Run the application smoke test:

```bash
curl -f http://<RESTORE_HOST>:8000/healthz
```

**Step 7 — Reattach archived partitions if needed**

A physical restore only includes partitions that were attached at the time the base backup was taken. Partitions detached and archived after the backup will not be present in the restored cluster. For each archived partition that should be visible:

```bash
aws s3 cp s3://<your-archive-bucket>/registry/audit_log/audit_log_YYYY_MM.dump /tmp/audit_log_YYYY_MM.dump

pg_restore \
  --dbname="$DATABASE_URL" \
  --no-owner \
  --no-privileges \
  /tmp/audit_log_YYYY_MM.dump

psql "$DATABASE_URL" -c "
    ALTER TABLE audit_log
        ATTACH PARTITION audit_log_YYYY_MM
        FOR VALUES FROM ('YYYY-MM-01') TO ('YYYY-MM+1-01');
"

rm /tmp/audit_log_YYYY_MM.dump
```

See [Audit log partition archival](#audit-log-partition-archival) for the inverse operation.

**Step 8 — Cut over traffic and scale up**

Update `DATABASE_URL` in your deployment configuration to point to the restored instance, then scale the service back up:

```bash
kubectl set env deployment/capability-fabric DATABASE_URL="postgresql+asyncpg://..." -n catalog
kubectl scale deployment capability-fabric --replicas=2 -n catalog
kubectl rollout status deployment/capability-fabric -n catalog
```

### Quarterly restore drill checklist

Perform a full restore drill once per quarter in a non-production environment to validate the backup chain and operator familiarity with the procedure.

- [ ] Confirm `pg_stat_archiver.last_failed_wal` is `NULL` on production (WAL archiving healthy).
- [ ] Download the latest base backup to the drill host.
- [ ] Restore base backup to a fresh data directory (Step 3 above).
- [ ] Configure WAL replay to a target time before drill start (Step 4 above).
- [ ] Start Postgres and confirm `recovery.signal` is removed automatically on promotion (Step 5 above).
- [ ] Run SQL integrity checks: row counts, partition listing (Step 6 above).
- [ ] Run `/healthz` smoke test against the restored instance (Step 6 above).
- [ ] Verify that reattaching one archived partition works end-to-end (Step 7 above).
- [ ] Record actual elapsed time and compare against your organization's RTO target.
- [ ] Document any gaps or failures in the incident log and address before the next quarter.
- [ ] Destroy the drill environment after sign-off.

**Schedule:** Last Friday of March, June, September, and December.
**Owner:** On-call operator for that week.
**Sign-off required by:** Engineering lead.

Record drill results in the team incident log with the tag `dr-drill-YYYY-QN` (e.g. `dr-drill-2026-Q2`).

---

## Appendix A — Resetting the dev database

The dev database is named `registry` under both local providers. To reset local dev state to a clean migration head:

```bash
make dev-reset     # destroy the database, recreate it, migrate, restart the stack
make dev-token     # re-bootstrap the dev tenant + mock-IDP entitlement seed
```

To keep a copy of the data first, dump it before resetting and restore afterwards:

```bash
pg_dump "$(make dev-url)" > /tmp/registry_backup.sql
make dev-reset
psql "$(make dev-url)" < /tmp/registry_backup.sql
make migrate
```

Under Docker Compose the equivalent sequence is:

```bash
docker compose exec postgres pg_dump -U postgres registry > /tmp/registry_backup.sql
docker compose down -v
docker compose up -d
docker compose exec -T postgres psql -U postgres registry < /tmp/registry_backup.sql
make migrate
make dev-token
```

Production environments are out of scope for this appendix. Never run `docker compose down -v` — or `make dev-reset` — against anything but a local dev database.
