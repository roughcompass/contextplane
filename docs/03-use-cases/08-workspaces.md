<!--
  title: Use case — Workspaces as task memory
  audience: integrator (consumer), integrator (producer), end-user agent
  archetype: explanation (use-case scenario)
  summary: How humans and agents use actor- or tenant-scoped workspaces for task checkpoints, decisions, handoffs, and persistent cross-session memory.
-->

# Use case: Workspaces as task memory

Context Plane's workspace surface keeps task context separate from the approved
catalog.

For a human, task memory can contain evaluation notes, saved incident queries,
and provisional decisions anchored to the catalog entities they concern.

For an agent, it can contain actor-scoped checkpoints written at the end of one
session and retrieved at the start of the next, so the goal, decisions,
unresolved questions, artifact references, completed checks, and next action do
not have to be reconstructed.

It is the same primitive. A workspace is a container of typed, Markdown-bodied
entries: `note`, `decision`, `open_question`, `saved_query`, or `saved_view`.
Entries can reference capability UUIDs. Visibility is determined by
`owner_kind`: an actor-owned workspace is visible to its owner and auditors;
a tenant-owned workspace is readable by every role holder in the owning tenant.
Workspaces never cross tenant boundaries.

Current workspaces do not support an audience made from selected task
participants. Tenant ownership is therefore broader than a team or task
boundary. It is suitable for organization-wide working context, not a
sensitive handoff between a few specialist agents.

**Before calling any workspace endpoint:** the [tenant](../01-overview/03-vocabulary.md#tenant) must be provisioned and a valid bearer token must be available. A `producer` can create and manage its actor-owned workspace. An `admin` can create and manage a tenant-owned workspace. Authorized consumers and auditors can read according to the visibility rules. See [authentication.md](../01-overview/04-authentication.md) for how to obtain a token.

---

## Scenario 1 — An agent checkpointing work across sessions

An agent that evaluates capabilities during a task needs a place to record what
it decided and why. The next session can retrieve that checkpoint rather than
re-evaluating the catalog from scratch. Entries persist in the database and
remain visible to sessions that present the same actor identity.

The agent creates a personal workspace once, during its first session:

```bash
curl -X POST https://contextplane.example.com/v1/workspaces \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "agent-memory-capability-decisions",
    "owner_kind": "actor",
    "description": "Persistent decisions and observations recorded across agent sessions"
  }'
```

After reaching a decision about a capability, the agent writes a `decision` entry anchored to the relevant entity UUID:

```bash
curl -X POST https://contextplane.example.com/v1/workspaces/<workspace_id>/entries \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "decision",
    "body_md": "Adopted payments-v3 for checkout flow. Evaluated against payments-v2 and stripe-bridge. Decisive factor: payments-v3 exposes idempotency keys on all mutation endpoints.",
    "reference_ids": ["<capability-uuid>"],
    "references_jsonb": {
      "rejected": ["<payments-v2-uuid>", "<stripe-bridge-uuid>"],
      "session_id": "<agent-session-id>"
    }
  }'
```

In the next session, before evaluating the same area, the agent queries its prior decisions:

```bash
curl "https://contextplane.example.com/v1/workspaces/search?kind=decision&reference_ids=<capability-uuid>" \
  -H "Authorization: Bearer <token>"
```

The search returns every `decision` entry that references the target capability UUID, across the agent's personal workspaces and any tenant-owned workspaces in its tenant. The agent reconstructs its prior reasoning without re-evaluating the catalog. This resume path uses the same actor identity. A different actor cannot read the personal checkpoint.

**Organization-wide agent memory.** An admin can create a tenant-owned
workspace when every role holder in the tenant may read the content:

```bash
curl -X POST https://contextplane.example.com/v1/workspaces \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "tenant-migration-memory",
    "owner_kind": "tenant",
    "description": "Shared migration decisions and open questions"
  }'
```

Any role holder in the owning tenant can read the workspace. Only an admin can
write entries. There is no grant step and no selected-participant audience.
This surface does not yet support a planner and executor writing a private
shared checkpoint under separate actor identities. Use an external task system
with the required access controls for that handoff.

**Expiry caveat.** Entries without an `expires_at` persist indefinitely. An
agent should set retention deliberately instead of defaulting every checkpoint
to permanent storage. If `expires_at` is set, the background expiry worker
soft-invalidates the entry after that timestamp. It disappears from list and
search results and is no longer useful as task memory.

---

## Scenario 2 — An architect evaluating capability candidates

An architect is deciding whether to adopt one of three shared capabilities for a new product feature. She wants to record her findings without them becoming part of the shared record on each capability, which is visible to producer teams and other consumers.

She creates a personal workspace scoped to her actor:

```bash
curl -X POST https://contextplane.example.com/v1/workspaces \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "auth-service-eval-q2",
    "owner_kind": "actor",
    "description": "Evaluation notes for Q2 auth library decision"
  }'
```

The response includes a `workspace_id`. She then adds entries that reference the capability UUIDs she is evaluating:

```bash
curl -X POST https://contextplane.example.com/v1/workspaces/<workspace_id>/entries \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "decision",
    "body_md": "Ruling out token-service-v1: rate-limit behavior is undocumented and the owner has not responded for 6 weeks.",
    "reference_ids": ["<capability-uuid>"]
  }'
```

Later she adds an `open_question` entry for a point she needs to resolve before the decision is final. When the evaluation is complete, she archives the workspace with a `PATCH` — it disappears from her default listing but remains readable with `include_archived=true` if she needs to trace her reasoning later.

The capability's own record, visible to the producer and other consumers, is untouched throughout. Her working notes stayed private.

---

## Scenario 3 — An admin curating an incident scratchpad

During a live incident, an organization may need a shared space to record
observations, pin queries, and log decisions. The content is suitable only when
every role holder in the tenant may read it.

An admin creates a tenant-owned workspace:

```bash
curl -X POST https://contextplane.example.com/v1/workspaces \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "incident-2026-05-12-db-latency",
    "owner_kind": "tenant",
    "description": "Shared scratchpad for the May 12 DB latency spike"
  }'
```

Any role holder in the owning tenant can read the workspace automatically. An
admin saves Context Plane query used to check blast radius:

```bash
curl -X POST https://contextplane.example.com/v1/workspaces/<workspace_id>/entries \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "saved_query",
    "body_md": "GET /v1/capabilities/<capability-id>/dependents?depth=3 — shows everything downstream of the slow query service",
    "reference_ids": ["<capability-uuid>"]
  }'
```

When the incident is resolved, an admin adds a `decision` entry summarizing the
root cause and chosen fix. The workspace remains searchable task memory. The
authoritative incident record stays in the incident-management system.

---

## Visibility model

Workspaces have exactly two owner kinds. Choose at creation time; it cannot be changed afterwards.

| `owner_kind` | Visible to | Typical use |
|---|---|---|
| `actor` | The owning actor and tenant auditors | Personal task memory across sessions and individual evaluation notes. The owning producer writes entries. |
| `tenant` | Every role holder in the owning tenant | Organization-wide incident notes, shared queries, and decision logs. An admin writes entries. |

A workspace never crosses tenant boundaries. The current API has no
selected-participant or cross-tenant grant. If content needs a narrower or
broader audience, publish the appropriate result through a governed external
system or Context Plane surface with that visibility.

---

## Expiring entries automatically

Individual entries can carry an `expires_at` timestamp for content that is only meaningful for a bounded period — a scratchpad note during an active investigation, a saved query that will become stale once a migration completes.

```bash
curl -X POST https://contextplane.example.com/v1/workspaces/<workspace_id>/entries \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "note",
    "body_md": "Circuit breaker is tripped on payment-service — monitor until 18:00 UTC.",
    "expires_at": "2026-05-12T18:00:00Z"
  }'
```

A background worker runs on a schedule and soft-invalidates entries whose `expires_at` has passed. Invalidated entries are excluded from list and search results but are retained for audit and compliance purposes. Physical deletion happens only through an explicit right-to-be-forgotten admin request — not through the expiry worker.

---

## Searching across workspaces

The search endpoint returns entries from every workspace visible to the caller (their personal `actor`-owned workspaces plus every `tenant`-owned workspace in their tenant). It accepts a full-text `q` string, a `kind` filter, and `reference_ids` to find all entries that mention a specific capability:

```bash
# Find all decision entries that reference a specific capability
curl "https://contextplane.example.com/v1/workspaces/search?kind=decision&reference_ids=<capability-uuid>" \
  -H "Authorization: Bearer <token>"
```

Results are cursor-paginated. Entries from workspaces the caller cannot access are never included — the visibility gate runs at the service layer before any row is returned.

Search uses full-text matching and reference filters only. Semantic similarity
search is not available today.

Before semantic workspace retrieval can ship, a controlled comparison must
show that task memory improves continuity over no workspace memory and that
semantic retrieval adds material value over lexical and reference search. The
evaluation must pre-register its scenarios, metrics, thresholds, and judge
rubric. A language-model judge may score the rubric, but humans must review a
risk-based sample and every safety failure.

The feature must authorize workspaces before similarity comparison or ranking.
It must test isolation, deletion, expiry, redaction, classification and access
changes, encryption-tier upgrades, and source revocation across vectors,
embedding text chunks, full-text indexes, summaries, caches, outboxes, logs,
and exports. Encrypted workspace content must remain excluded at write and
query time until protection tests prove that no plaintext derivative survives.

## Route task memory and observed knowledge by intent

Checkpoint work in a workspace when another session needs the current goal,
decisions and rationale, completed checks, unresolved questions, artifact
references, and next action.

Current entries have no revision history. For resumable task history, append a
new entry for each checkpoint instead of changing an earlier checkpoint with
`PATCH`. This convention improves continuity but does not make an entry
immutable evidence. Revision-addressable checkpoints do not ship today.

Stage a Living Memory claim when the useful result is a small assertion about a
capability that can be typed and cited. The claim should point to stable
evidence, such as a commit, document revision, connector run, work item, or
incident. Do not copy a complete workspace entry into the claim.

Raise a capability request when the missing piece requires an answer or change
from the capability owner. A question in a workspace does not route work to
that owner.

---

## What workspaces are not

**Not a channel to the capability owner.** Workspace entries are visible only within the owning actor or tenant scope — nothing written here reaches the tenant that owns a capability you referenced. A `note` carrying that capability's id in `reference_ids` is how you keep an observation about someone else's work on your own side of the boundary. If a producer needs to know something, that conversation happens in the tools your organisation already runs for it.

**Not versioned or immutable.** Entry bodies are mutable with `PATCH`.
Workspaces do not version the history of edits. The audit log captures mutation
events for operators, but an entry is not an immutable record and must not be a
claim's sole evidence.

**Not a task-participant collaboration boundary today.** Actor-owned workspaces are
personal. Tenant-owned workspaces are readable across the owning tenant and
writable by admins. Neither option represents a selected set of specialist
agents working on one task.

**Not a vector store today.** Entry bodies are full-text searchable via
`GET /v1/workspaces/search` and filterable by `kind`, `reference_ids`, owner,
and date range. There is no embedding-based or semantic similarity search.

---

## Where this connects

- [AI agent capability discovery](01-ai-agent-capability-discovery.md) — an agent evaluating capabilities during discovery can record its reasoning in a workspace before committing to an adoption.
- [Compliance and audit](07-compliance-and-audit.md) — workspace mutation events (create, update, delete, expiry) are emitted to the audit log.

---

## Read next

- [API reference](../05-reference/01-api.md#workspaces) — endpoint contracts for `POST`, `GET`, `PATCH`, `DELETE` on workspaces and entries
- [Authentication](../01-overview/04-authentication.md) — how to obtain a bearer token
- [Authorization](../01-overview/05-authorization.md) — how role grants and tenant selection scope the token
- [PII policies guide](../04-guides/04-pii-policies.md) — workspace entry bodies are PII-scanned on write; this guide explains how policies are configured
