<!--
  title: Use case — Compliance and audit over a regulated capability inventory
  audience: operator, operator agent
  archetype: explanation (use-case scenario)
  summary: How to use the bi-temporal data model, audit partitioning, and PII scanning to maintain a compliant, auditable capability inventory.
-->

# Use case: Compliance and audit over a regulated capability inventory

Organizations subject to change-management requirements or data-handling regulations need more than a current capability inventory. They need traceable governed writes, reconstructible historical state, and explicit controls on sensitive text. Context Plane combines bi-temporal catalog rows, a partitioned audit log, and pattern-based PII policies on selected write fields.

Canonical attributes, facts, and edges carry valid-time and transaction-time axes. Point-in-time reads reconstruct their historical state without changing current rows. Context Plane monitors audit partitions against a configurable age threshold; an operator detaches and archives them. The PII scanner warns or blocks on named write fields. It does not redact values or cover generic fact and attribute writes.

Read [Data governance and PII](../01-overview/09-data-governance.md) for the data-surface and scanner boundaries before using this scenario as a control design.

---

## Preconditions

- At least one actor in the tenant has the `auditor` or `admin` role. The `auditor` role is read-only: it can call `GET /v1/admin/audit`, and read capabilities, but it cannot write anything.
- The `audit_partition_max_age_days` setting is configured in the deployment environment. The startup and recurring checks warn when a partition exceeds this threshold. Archival remains an operator action.
- Any PII patterns and field policies you need enforced are already registered (see Step 3 below). The scanner applies policies that exist at write time — it does not retroactively rescan stored data.

---

## Step 1 — Reconstruct a past state with bi-temporal queries

Every capability and attribute row carries two time axes:

- **Valid time** (`t_valid_from` / `t_valid_to`) — when the fact was true in the world.
- **Transaction time** (`t_ingested_at` / `t_invalidated_at`) — when it was recorded in Context Plane.

The `?as_of=<iso8601>` parameter on capability reads selects the valid-time slice you want. This is the primary mechanism for reconstructing what Context Plane believed at any past instant without modifying any current data.

**Example — reconstruct the state of a capability before a lifecycle transition:**

```bash
# What did identity-service look like before the GA promotion on 2026-02-15?
curl -s \
  "https://contextplane.example.com/v1/capabilities/<entity_id>?as_of=2026-02-14T23:59:59Z" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '{entity_id, name, lifecycle, attributes}'
```

The response reflects the capability's state as recorded by that timestamp on the valid-time axis. If the capability did not exist at that time, the response is 404.

**Example — reconstruct the full capability list as of a past date:**

```bash
curl -s \
  "https://contextplane.example.com/v1/capabilities?as_of=2026-01-01T00:00:00Z" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '{total: .total, items: [.items[] | {entity_id, name, lifecycle}]}'
```

This is useful for change-management reports: run the query twice (at `T-before` and `T-after` a release window) and diff the responses to enumerate every capability that changed state.

---

## Step 2 — Query the audit log

The audit log is queryable via `GET /v1/admin/audit`. Tenant scope is injected from the caller's auth context — you cannot query another tenant's audit log. Results are returned in descending order by `(ts, audit_id)` with keyset pagination.

**Query parameters:**

| Parameter | Type | Meaning |
|---|---|---|
| `actor_id` | UUID | Filter to events emitted by a specific actor |
| `action` | string | Filter by action name (e.g. `LIFECYCLE_STATE_CHANGED`, `PROGRESSION_OVERRIDE_CREATED`) |
| `target_type` | string | Filter by entity type (e.g. `capability`) |
| `target_id` | UUID | Filter to events on a specific entity |
| `from` | ISO 8601 datetime | Earliest event timestamp to include |
| `to` | ISO 8601 datetime | Latest event timestamp to include |
| `cursor` | string | Pagination cursor from the previous page's `next_cursor` field |
| `page_size` | integer | Results per page (1–500, default 50) |

**Example — pull all events for a specific capability over the last 30 days:**

```bash
curl -s \
  "https://contextplane.example.com/v1/admin/audit?target_id=<entity_id>&from=2026-04-14T00:00:00Z&to=2026-05-14T23:59:59Z" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '.items[] | {ts, actor_id, action, detail}'
```

**Example — find all progression overrides across the tenant:**

```bash
curl -s \
  "https://contextplane.example.com/v1/admin/audit?action=PROGRESSION_OVERRIDE_CREATED" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '.items[] | {ts, actor_id, target_id, detail: .detail.reason}'
```

**Pagination.** When `next_cursor` is present in the response, there are more results. Pass it as `?cursor=<value>` in the next call:

```bash
# Page 2
curl -s \
  "https://contextplane.example.com/v1/admin/audit?action=ADOPTION_CREATED&cursor=<next_cursor>" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '{items_count: (.items | length), next_cursor}'
```

---

## Step 3 — Configure PII scanning policies

The PII scanner runs at write time on selected fields. This scenario uses workspace entry `body_md` and `references_jsonb` because that surface returns warnings to the caller. REST session bodies, direct and extracted claim strings, and artifact bodies use their own field types. Generic fact and attribute writes are outside this control.

### Select a built-in PII detector

The current write-time runtime executes built-in detectors. It stores and
validates tenant-defined regex rows but does not instantiate them during scans.
Do not base a compliance control on a custom row until that runtime limitation
is removed.

```bash
# Find the built-in SSN detector row.
PATTERN_ID=$(curl -s https://contextplane.example.com/v1/admin/pii-patterns \
  -H "Authorization: Bearer <admin-token>" \
  | jq -r '.[] | select(.name == "ssn") | .pattern_id')
```

Field policies accept three enforcement levels:

| Value | Effect |
|---|---|
| `advisory` | Match is recorded internally; write proceeds; no warning to caller |
| `warn` | Match is recorded; write proceeds; `warnings` array appears in the response |
| `block` | Match causes the write to be rejected with HTTP 422 before any row is stored |

### Override enforcement per field type

A field policy targets a specific field type and optionally a specific pattern. When a field policy matches, it takes precedence over the pattern's own `policy_override`:

```bash
# Block any PII in workspace entry body fields, regardless of category
curl -s -X POST https://contextplane.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "field_type": "workspace_entry.body",
    "policy": "block"
  }' | jq '{policy_id, field_type, policy}'
```

To target a specific pattern on a specific field:

```bash
curl -s -X POST https://contextplane.example.com/v1/admin/pii-field-policies \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "field_type": "workspace_entry.body",
    "pattern_id": "'"$PATTERN_ID"'",
    "policy": "warn"
  }' | jq '{policy_id, field_type, pattern_id, policy}'
```

For each match, the scanner prefers an exact field-and-pattern policy, then a field-wide policy, and then the pattern override. It falls back to `advisory`. After resolving each match, the scan uses the most restrictive resulting action.

### What callers see at write time

When a `warn`-level match fires on a workspace entry write, the entry is written but the response body includes a `warnings` array:

```json
{
  "entry_id": "...",
  "warnings": [
    {"field": "body_md", "categories": ["GOVERNMENT_ID"]}
  ]
}
```

A `block`-level match returns HTTP 422 and no row is written:

```json
{
  "detail": {
    "code": "pii_detected",
    "field": "workspace_entry.body",
    "categories": ["GOVERNMENT_ID"]
  }
}
```

---

## Step 4 — Audit trail for progression overrides

Every progression gate bypass writes an audit event **before** the override row is inserted. The `audit_event_id` in the override response is the foreign key into the audit log for that bypass. This ordering guarantee means the audit record exists even if the override row fails to insert — the bypass is never invisible.

**Query all overrides for a capability:**

```bash
curl -s \
  "https://contextplane.example.com/v1/admin/tenants/<tenant_id>/entities/<entity_id>/progression-overrides" \
  -H "Authorization: Bearer <admin-token>" \
  | jq '.items[] | {override_id, from_state, to_state, gate_id, reason, authorized_by, audit_event_id}'
```

**Verify each override has a corresponding audit event:**

```bash
curl -s \
  "https://contextplane.example.com/v1/admin/audit?action=PROGRESSION_OVERRIDE_CREATED&target_id=<entity_id>" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '.items[] | {ts, actor_id, detail}'
```

For a formal change-management report, join the two: the override row carries the human-readable `reason`; the audit event carries the actor, timestamp, and immutable transaction record.

---

## Step 5 — Generate a change history report for a capability

For a complete picture of everything that happened to a capability — attribute changes, lifecycle transitions, and override bypasses — query the audit log filtered by `target_id` with a time window:

```bash
curl -s \
  "https://contextplane.example.com/v1/admin/audit?target_id=<entity_id>&from=2026-01-01T00:00:00Z" \
  -H "Authorization: Bearer <auditor-token>" \
  | jq '[.items[] | {ts, action, actor_id, detail}]'
```

Paginate until `next_cursor` is absent to get the full log. For a machine-readable report, pipe to `jq -r '... | @csv'` or send to your SIEM directly.

**Key action names to watch for:**

| Action | Meaning |
|---|---|
| `CAPABILITY_CREATED` | New capability registered |
| `CAPABILITY_UPDATED` | Attributes or metadata changed |
| `LIFECYCLE_STATE_CHANGED` | Lifecycle advanced or rolled back |
| `VISIBILITY_CHANGED` | Visibility or shared tenant list updated |
| `PROGRESSION_OVERRIDE_CREATED` | Gate bypassed — always present before any override row |
| `ADOPTION_CREATED` | A consumer declared a dependency |
| `ADOPTION_DELETED` | A consumer removed a dependency |

---

## Audit partition archival

The audit table is partitioned by month (`audit_YYYY_MM`). The `audit_partition_max_age_days` setting defines the age at which monitoring reports a partition as overdue for archival:

- Operators detach overdue partitions and export them to cold storage by following the [operator runbook](../06-operations/01-ops.md#audit-log-partition-archival).
- Detached partitions are no longer queryable through `GET /v1/admin/audit` — they must be restored to a read-replica or imported into an analytics store to be queried.
- The current partition remains live and queryable. The monitoring job does not detach it.

If your retention policy requires audit data to remain queryable through the API for longer than the live-database window, increase `audit_partition_max_age_days` in the deployment configuration or export partitions to a queryable archive before they age out.

---

## Role separation: auditor vs. admin

| Capability | `auditor` | `admin` |
|---|---|---|
| `GET /v1/admin/audit` | Yes | Yes |
| Read capabilities | Yes | Yes |
| `POST /v1/admin/pii-patterns` | No | Yes |
| `POST /v1/admin/pii-field-policies` | No | Yes |
| `POST /v1/admin/tenants/{id}/progression-definitions` | No | Yes |
| `POST /v1/admin/tenants/{id}/entities/{id}/progression-overrides` | No | Yes |
| Write to any capability or subscription | No | Yes (with producer role) |

The `auditor` role is designed for compliance team members and automated audit agents that should be able to verify state and history but must never be able to alter it. Grant it separately from `producer` and `admin` — a single actor can hold multiple roles, but the auditor role alone is sufficient for all read-only compliance workflows described in this document.

---

## See also

- [Authentication](../01-overview/04-authentication.md) — JWT structure and OIDC setup
- [Authorization](../01-overview/05-authorization.md) — role grants and entitlement strings
- [Platform team shared Context Plane](02-platform-team-shared-contextplane.md) — progression definitions, lifecycle governance, and override usage
- [Audit log partition archival runbook](../06-operations/01-ops.md#audit-log-partition-archival) — partition archival and restore procedures
- [API reference](../05-reference/01-api.md) — endpoint contracts for audit, PII, and progression endpoints
