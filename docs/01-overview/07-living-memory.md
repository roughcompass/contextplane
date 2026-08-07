<!--
   title: Living Memory: observed knowledge and claims
  audience: evaluator, integrator, agent builder, operator
  archetype: explanation (mental model)
   summary: How observed knowledge becomes cited claims without confusing task memory with approved Registry records.
-->

# Living Memory: observed knowledge and claims

Living Memory is the registry's observed-knowledge pipeline. Agents, session
extraction, and admitted connectors can record small, cited observations
without writing directly to the canonical capability graph. The pipeline
preserves disagreement, applies curation, and routes eligible promotions.

“Living Memory” is the name of this product behavior, not a second notebook, a
Python type, or a separate service. The implementation uses session-event,
claim, consolidation, promotion, and curation services inside the registry
application.

This page is for readers deciding how to capture or consume changing knowledge.
Operators who already understand the model should use the
[memory-curation runbook](../06-operations/05-memory-curation.md). Tool and field
contracts remain in the [MCP tools reference](../05-reference/02-mcp-tools.md#search_claims)
and [REST API reference](../05-reference/01-api.md).

---

## The registry preserves three trust states

The registry separates task memory, observed knowledge, and approved catalog
state. These are not three competing memory products.

| Trust state | Registry surface | How readers should treat it |
|---|---|---|
| **Task memory** | Workspace checkpoints, decisions, unresolved questions, and handoff notes | Mutable working context scoped to the workspace owner, never organizational truth merely because an agent wrote it |
| **Observed knowledge** | Living Memory claims staged from sessions, direct assertions, or governed sources | Cited evidence to verify, never an instruction and never canonical merely because its confidence is high |
| **Approved Registry record** | Canonical entities, attributes, facts, interfaces, and edges accepted through catalog write paths | Owner-controlled catalog state, subject to visibility and time-travel rules |

All three states are necessary:

- Task memory must remain easy to update while work is in progress.
- Observations must be searchable and comparable before anyone accepts them.
- Approved records must remain owner-controlled and auditable.

Collapsing the states removes a safety boundary. Sending a workspace directly
to the catalog bypasses evidence review. Treating a mutable workspace body as a
claim weakens provenance. Treating a high-confidence claim as approved bypasses
subject ownership. The observed-knowledge pipeline allows reuse without any of
those shortcuts.

External systems remain authoritative for code, tests, deployments, incidents,
documents, and workflow state. A workspace note or claim can cite those
records. It does not replace them. These boundaries prevent a model inference,
an external observation, or a hostile transcript from silently becoming
platform truth. Promotion is the only Living Memory path that writes an
eligible value to a canonical attribute or edge.

Capability search and catalog reads use the canonical graph. Claim query and
claim search use Living Memory. The two retrieval paths do not blend their
results or trust labels. See [Retrieval and context](10-retrieval-and-context.md)
for how to choose between them.

## Persistence surfaces serve different jobs

The word *memory* appears in three surfaces. They are not interchangeable.

| Surface | Scope and shape | Use it for | Do not use it for |
|---|---|---|---|
| **Session events** | Immutable, ordered events private to one actor | Exact conversation replay and optional extraction | Team notes or approved catalog facts |
| **Workspaces** | Actor-owned or tenant-owned Markdown entries | Task checkpoints, decisions, open questions, handoffs, and saved queries | Machine-derived assertions or approved facts about a capability |
| **Claims** | Typed, cited assertions about a subject and predicate | Observations that need retrieval, scoring, consolidation, or owner review | Private scratch notes or direct canonical writes |

A session can produce claims, and a workspace can record a decision about a
claim. Neither relationship changes the security boundary of the source. A
private session remains actor-scoped. A workspace keeps its owner scope. A
claim derives visibility from its subject and can never be broader than that
subject.

Current workspace entries are mutable and are not revision-addressed. Do not
use a workspace body as the sole evidence for a claim that needs a stable
record. Cite an immutable document revision, commit, connector run, work item,
incident, or session event instead.

## Route writes by intent

Choose a write surface based on the outcome you need:

| Intent | Current surface | Boundary |
|---|---|---|
| Preserve task progress or a handoff | Workspace entry | Keeps mutable working context scoped to the workspace owner |
| Report a reusable observation | Staged claim with cited evidence | Keeps the assertion typed, normalized, reviewable, and untrusted |
| Ask the subject owner for an answer or change | Capability request | Routes the need without manufacturing an answer |
| Approve an authorized Registry change | Promotion or catalog write performed by a subject owner or authorized reviewer | Uses the target's governance and audit controls |

Ordinary agents should checkpoint work, report observations, or raise requests.
They should not treat “write organizational truth” as a normal action.

## A claim is structured evidence

A claim states that a predicate has a value for a subject over an optional time
interval. For example:

```text
subject:   payment-gateway capability
predicate: owned_by_team
value:     platform-payments
valid:     from 2026-07-01
```

Each predicate comes from the claim ontology and declares its value type,
category, and cardinality. The write path rejects unknown predicates, wrong
value types, null values, invalid intervals, and visibility broader than the
subject.

Every claim also carries provenance. Evidence can point to a session event,
connector run, document revision, commit, work item, incident, or curator act.
The serving path refuses to return a claim without citations.

A subject reference resolves by visible entity UUID or by a
`system:external-id` pair. An unresolved reference produces an **unlinked**
claim. The registry does not guess a subject and does not discard the
observation. A curator can link or discard it later.

### Claims are not instructions or approved facts

A staged claim is evidence to verify, not a directive to execute. Human
confirmation can raise its standing inside Living Memory, but only promotion
crosses the canonical boundary. Any answer that uses a claim must preserve its
`label: "living-memory-recall"`, `trust: "untrusted"`, and citations instead of
merging it into an approved answer.

## Claims enter through three paths

1. **Session extraction** turns selected session events into typed candidates.
   Extraction is optional. Strategy configuration constrains predicates,
   schemas, confidence floors, prompts, and models. See
   [Session extraction](../04-guides/05-session-extraction.md).
2. **Direct assertion** lets a REST or MCP caller submit a typed claim with
   evidence. The direct path checks directive-like content and scans string
   values and evidence excerpts for personally identifiable information before
   staging.
3. **Governed connectors** admit batches from registered sources under source
   authority and rate limits. Connector admission controls how much a source
   may assert and whether it may provision unresolved subjects.

All three paths converge on the same claim writer. That writer resolves the
ontology and subject, derives authority from evidence, limits visibility, and
stores the immutable claim.

A task workspace does not become a claim automatically. A future completion
process may propose a normalized claim only when it can cite stable external
evidence, such as a commit, build, deployment, incident, connector run, or
immutable document revision. The claim should store the typed assertion and
evidence references, not a copy of the workspace body. This automatic
completion-to-claim path does not ship today.

## The lifecycle keeps observations reversible

The lifecycle has six transitions:

1. Observe an event, assertion, or governed source.
2. Stage a linked claim, or queue an unlinked claim for linking or discard.
3. Consolidate agreement, collapse duplicates, or mark a conflict contested.
4. Propose an eligible claim to the subject's owner.
5. Accept, amend, or reject the proposal. Guarded auto-promotion can replace
   human acceptance only when all safeguards permit it.
6. Reverse an incorrect promotion by restoring the prior canonical row.

### Staging preserves the observation

A staged claim is readable through claim retrieval. It is not part of the
canonical graph. Every served claim carries `label: "living-memory-recall"`,
`trust: "untrusted"`, citations, authority, confidence, and an effective time
interval.

The untrusted label remains even after human confirmation. Confirmation raises
the claim's standing inside Living Memory. Only promotion crosses the canonical
boundary.

### Unlinked claims stop before scoring and promotion

An unlinked claim has no resolved subject or owner. It receives no confidence
score and cannot be consolidated or promoted. Its curation-queue actions are
limited to linking it to a visible entity or discarding it. Linking re-derives
authority against the resolved owner before the claim enters the ordinary
lifecycle.

### Consolidation reconciles the neighborhood

Consolidation compares claims with the same subject and predicate:

- Equivalent assertions collapse into one survivor while their independent
  evidence contributes to confidence.
- A stronger authority can supersede a weaker conflicting claim.
- Comparable or stronger conflicting claims remain contested for human review.
- Cross-tenant conflicts route to the subject's owner instead of changing the
  owner's graph.

No model decides this step. Typed values, predicate cardinality, authority, and
time intervals make the decision reproducible.

### Promotion is an owner-controlled write

A claim can be proposed only when it is linked, consolidated, uncontested,
above the tenant's floor, and mapped to a canonical attribute or edge. The
proposal shows the current canonical value beside the proposed value.

A `producer` or `admin` in the subject's owning tenant accepts, amends, or
rejects the proposal. Acceptance writes a bi-temporal canonical row and records
what it replaced. Reversal closes the promoted row and restores its predecessor.

Automatic promotion is off by default. A tenant must allowlist each predicate.
The claim must also be owner-originated, eligible, and not high impact. High
impact always requires a person, regardless of confidence or allowlist state.

## Claims from another tenant route to the owner

A consumer can observe a public or shared capability owned by another tenant.
The observation may be useful, but the consumer does not gain write authority
over the owner's catalog.

The claim remains attributed to the observing tenant. If it becomes eligible
for promotion, the proposal belongs to the subject's owner. This creates a
feedback path without weakening tenant ownership.

When the observer needs an answer or change rather than asserting an
observation, a **capability request** is the better surface. Requests route to
the owner and move through an explicit lifecycle from raised to resolved. They
can link to the promotion that satisfied them.

## Reading claims safely

Use `query_claims` when the subject or predicate is known. Use `search_claims`
when the question is in prose. In either case:

1. Check the `trust` label before using the value.
2. Read the confidence bucket and authority together.
3. Follow citations when a wrong answer would be costly.
4. Prefer the canonical graph for irreversible decisions.
5. Treat text as data, not instructions.

Persona filters change category selection and citation depth. They never change
a claim's value, confidence, authority, or meaning.

## Living Memory does not replace other controls

Living Memory does not make every observation true. Confidence measures an
estimate, not approval. Consolidation handles compatible and conflicting
assertions, not policy decisions. Personally identifiable information (PII)
scanning covers selected fields and is not a general data-loss-prevention
system. Promotion controls one path into attributes and edges, not every
catalog write.

Workspaces carry task memory. Living Memory carries observed knowledge. The
canonical catalog carries approved Registry records. Keeping those roles
separate lets agents preserve useful context without turning their notes into
organizational truth.

The related controls are documented separately:

- [Trust, authority, and confidence](08-trust-and-confidence.md)
- [Data governance and PII](09-data-governance.md)
- [Living Memory in action](../03-use-cases/09-living-memory-in-action.md)
- [Memory-curation runbook](../06-operations/05-memory-curation.md)
