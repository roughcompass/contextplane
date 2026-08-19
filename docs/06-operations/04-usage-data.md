# Usage Data

How Context Plane records who used it, what that data can and cannot be used for, and
the operator procedures that keep it bounded.

For the operational metrics — request rates, queue depths, error counts, all served
to a scraper — see [ops.md](01-ops.md). This is a different tier answering a
different question, and the difference matters enough to state first.

---

## What this is, and what it is not

**Usage data is a measurement of traffic. It is not a record of what happened.**

That distinction is the load-bearing one on this page. The `audit_log` table is the
record: every write is captured synchronously, in the same transaction as the change,
with the actor, the target, and before/after snapshots. If a question begins "who
changed" or "prove that", the audit log answers it and usage data must not be asked.

Usage rows are the opposite by design:

| Property | Consequence |
|---|---|
| Buffered off the request path | A call is recorded shortly after it is served, not during |
| Dropped when the buffer fills | Under enough load, some calls are not recorded at all |
| Dropped when the database is unavailable | The same, for a different reason |
| Deleted on a retention boundary | Raw rows older than the configured window do not exist |

Every one of those is correct for "is anyone using this capability" and disqualifying
for "who accessed this capability". A dropped row and a call that never happened are
indistinguishable afterwards. **Nothing in the service decides anything from usage
data**, and that is enforced rather than documented: `make usage-boundary` fails if a
module imports `contextplane.usage` without being declared, or queries the usage tables
from outside the package.

If you need to answer an access question, the audit log and the ARC receipts are the
surfaces for it.

---

## What is recorded

One row per API call and per MCP tool invocation. What is populated today:

| Field | Meaning |
|---|---|
| `occurred_at` | When, and which monthly partition the row lands in |
| `tenant_id` | Always set. A row attributed to no tenant could not be read back by anyone |
| `actor_id` | Set where the call was authenticated; null for anonymous traffic |
| `surface` | `rest` or `mcp` |
| `operation` | The route *template*, or the MCP tool name |
| `outcome` | `ok` or `error` |
| `status_class` | `2xx` … `5xx` |
| `latency_ms` | How long the caller waited |
| `request_id` | Correlates a row with the service logs for the same request |
| `subject_entity_ids` | Which entities the call concerned, from the resolved UUID path params |
| `payload_bytes` | Response size, summed across body chunks. REST only; null for streaming |

`operation` holds the route template (`/v1/capabilities/{entity_id}`) rather than the
requested path. Storing the populated path would put entity identifiers into a text
column with unbounded cardinality; the ids belong in `subject_entity_ids`, where they
are typed and countable.

`payload_bytes` is null rather than zero for a streaming path. An SSE connection's
total is however long it stayed open, not the size of an answer, and summing the two
into one column would make any average derived from it meaningless. Zero, by contrast,
is a real measurement — a 204 sent no bytes.

### Columns that exist but are not yet populated

These are in the schema and in the event type, and **nothing writes them today**.
They will read as null in every row. Listed so nobody builds a chart on a column that
is always empty and concludes the traffic is zero:

| Field | Would hold | Status |
|---|---|---|
| `result_count` | How many rows a search returned | Needs per-route knowledge of the response body |
| `payload_tokens` | Token count of an MCP response | No token counter available at the emit point |
| `query_digest`, `query_length` | SHA-256 and length of a search query | Deliberately deferred — see below |

`result_count` is the one worth knowing about, because `outcome` deliberately does
**not** encode it: a search matching zero rows is a successful call that answered
"nothing", and folding that into the outcome would invent an error rate out of
ordinary empty results. Until `result_count` is populated, the "did callers find
anything" question cannot be answered from this data at all.

**No free text is recorded, and none can be.** A schema conformance test fails CI if
any unbounded text column is added to these tables, so this is a property of the
schema rather than a habit of the current code. Recording *what* was searched for —
even as a digest — was discussed and deferred; the columns exist so that enabling it
later needs no migration, and the decision to enable it is a product one.

---

## Rollups, and why they outlive the raw rows

Three aggregate tables are computed hourly, covering yesterday and today:

| Table | Grain |
|---|---|
| `usage_rollup_tenant_day` | tenant × day × surface |
| `usage_rollup_capability_day` | tenant × day × capability |
| `usage_rollup_tool_day` | tenant × day × MCP tool |

The capability grain carries the outcome mix and served bytes as well as call
counts, because a publisher reading it needs to know whether their capability is
failing, not only how often it is asked. Rows computed before those columns existed
carry zeros for the outcome split; re-rolling a day inside the raw retention window
recomputes it exactly, and days whose raw rows have expired keep their zeros.

**None of them holds an actor identifier.** Each holds `distinct_actors`, a
`COUNT(DISTINCT actor_id)` computed at rollup time and then discarded — which answers
"how many people" without recording which people.

That single property is what the whole retention design rests on. An aggregate with no
actor identifier is not personal data, so it carries no erasure obligation and no
retention boundary, and it is kept indefinitely. Raw rows can therefore be deleted —
on schedule or on request — without losing the answers.

Two consequences an operator should know before being surprised by them:

- **A right-to-be-forgotten request does not change any rollup value.** It deletes raw
  rows and leaves every aggregate byte-identical, so a figure quoted for a closed
  month keeps matching itself.
- **A backfill over an erased or expired window will produce different numbers**,
  because it recomputes from rows that are gone. The hourly schedule only ever touches
  yesterday and today, so a closed month is safe unless someone deliberately re-rolls
  it.

---

## Reading it

Four endpoints, all tenant-admin gated and scoped to the calling tenant. There is no
per-event endpoint, and a conformance test fails if one is added.

| Endpoint | Answers |
|---|---|
| `GET /v1/admin/usage/summary` | Volume, outcomes, and reach per surface over a window |
| `GET /v1/admin/usage/series` | The same per day, with latency percentiles |
| `GET /v1/admin/usage/tools` | Which MCP tools agents actually call |
| `GET /v1/admin/usage/capabilities` | Which capabilities callers asked about |

### The publisher's view is a different endpoint

`GET /v1/usage/owned-capabilities` answers the other question: how the capabilities
*your tenant owns* are being called, by everyone. Gated on `admin` **or** `producer`,
because a resolved principal carries exactly one role and a publisher's is `producer`.

The two surfaces differ in their scoping, not their numbers:

| Surface | Scope | Answers |
|---|---|---|
| `/v1/admin/usage/capabilities` | your tenant's own traffic | what my organisation calls |
| `/v1/usage/owned-capabilities` | capabilities you own | what everyone calls of mine |

The publisher view is the one place in this API that reads across tenants, and it
stops at totals. Calls from every tenant are summed and **no breakdown by calling
tenant is returned** — telling an owner their capability is used is the point; telling
them how heavily each named customer leans on it is a different disclosure decision.

### Fields that will mislead you if skimmed

Read these before building anything on the summary.

**`actor_days` is not a headcount.** It is the sum of each day's distinct actors, so
one person active every day for a month counts thirty times. It is a real and useful
figure — engagement volume — and it is not the number of people.

**`distinct_actors` is the headcount, and it can be null.** The true distinct count
over a window can only be computed from raw rows, so it is available while the window
is inside the retention boundary and null past it, with
`distinct_actors_unavailable_reason` explaining why. It is deliberately null rather
than the `actor_days` sum, which for a month can be thirty times too large, and
deliberately null rather than zero, which reads as "nobody used it".

**`payload_tokens` is always null**, because nothing populates the underlying column
yet — see the list of unpopulated columns above. `payload_bytes` is populated for
REST calls and null for MCP ones, so a summary covering both surfaces reports bytes
only for the REST row.

**`worst_daily_p95_ms` is the largest single-day p95, not the p95 of the window.** An
average of percentiles has no definition — daily p95s of 10 ms and 100 ms average to
55 ms, which describes no day that happened and no request that was served. For
latency over a range, read the daily series, where each percentile is exact at the
grain it was computed.

### Windows

`from` and `to` are inclusive dates. The default window is the last thirty days
ending today; today is included even though its rollup is still being recomputed, so
that traffic generated a moment ago is visible. Windows wider than 400 days are
refused with a 422 rather than served slowly.

---

## Retention

Configured by `USAGE_RETENTION_DAYS`, default 90, permitted range 30–180.

A value outside that band is **refused at startup rather than clamped**. A deployment
that asked for a year and silently got 180 days would believe it had a year of raw
history and would find out when a query returned less than it should — by which point
the rows are gone. A startup error is recoverable; silently discarded history is not.

The sweep runs hourly and deletes in independently committed batches of 5000, with a
ceiling of 50 batches per run.

**Alert on:** `WARNING` logs matching `usage_expiry.truncated`. That message means the
run stopped on the batch ceiling with rows still past the boundary — ingest is
outpacing the sweep and the retention boundary is quietly not being enforced. Stopping
because there was no work left and stopping because the ceiling was hit look identical
in a row count, which is why the distinction is logged.

**Never disable this worker.** Raw usage rows carry an actor id, so they are personal
data. Retention is what keeps that bounded, and the analytical cost of enforcing it is
zero because the rollups are actor-free and kept forever. If the sweep stops, the table
becomes an unbounded personal-data store while every dashboard still looks correct.

### Partitions

`usage_events` is range-partitioned by month, with 24 partitions pre-created. Use the
same manual `ATTACH`/`DETACH PARTITION` procedure documented for `audit_log` in
[Audit log partition archival](01-ops.md#audit-log-partition-archival) to add
partitions ahead of time or detach and archive old ones. Because the rollups are
independent of the raw rows, detaching a month's partition does not change any
aggregate answer.

`scripts/partition_migrate.py` deliberately covers only `audit_log`. It is a
whole-table rebuild-and-swap, and pointing it at `usage_events` would need the
retention sweep paused for the duration — the sweep deletes from the source while
the copy reads it, and rows it removes mid-run would fail the script's
pre-cutover count check.

---

## Right to be forgotten

Usage participates in the erasure registry as the `usage` subsystem. A request removes
the actor's raw rows within the requesting tenant and reports two counts:

- `usage_events` — rows deleted from the table
- `usage_events_buffered` — queued events discarded before they could be written

The second exists because an event buffered when the request arrives would otherwise
flush *after* the delete and reinsert the actor into a table they had just been erased
from, while the receipt said they were gone.

**One request in flight during an erasure will still be recorded.** That is inherent —
the person is still making calls while asking to be forgotten — and it is why every
erasure participant is idempotent. Repeat the request once traffic has stopped;
the second pass removes whatever arrived during the first.

Unlike the retention sweep, the erasure delete does **not** use `SKIP LOCKED` and has
no batch ceiling. Skipping a locked row is fine for expiry, which returns in an hour;
here it would report a completed erasure with rows still present.

---

## Metrics to watch

| Metric | Meaning |
|---|---|
| `contextplane_worker_dead_lettered_total{queue="usage_events"}` | Events dropped: buffer full, or a flush failed |
| `contextplane_worker_queue_depth{queue="usage_events"}` | Buffered events awaiting a flush |
| `contextplane_worker_runs_total{worker="usage_expiry"}` | Retention sweep runs, by outcome |
| `contextplane_worker_runs_total{worker="usage_rollup"}` | Rollup runs, by outcome |

A non-zero drop counter means usage numbers understate reality for that period. It
does not mean requests failed — recording is off the request path precisely so that it
cannot. This is the metric that tells you whether the numbers you are reading are
complete, so check it before quoting a figure that matters.
