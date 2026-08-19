# Plan — governed agent memory (bank-grade, agent-speed)

Epic-level seed of the accepted 2026-08 plan. Every epic below requires
decomposition into ≤1-day tasks (by PR to this file) before any work is
claimable. Sequencing that is safety-relevant is marked ⚙ and must land as a
required check when the first dependent task is cut.

### E1 — Autonomy Envelope authority object

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane

One authority object: an ARC `capability_contract` artifact whose applicability
rules carry the delegated-authority matrix; a session ProvenanceGrant is a
runtime projection of it, never a peer object. Instant suspend (status flip,
push-invalidated, sub-second SLO), governed widen (full ARC pipeline).
Identities bind to IAM workload identities; stream-scoped action-class and
sensitivity declarations at source-namespace registration. Owned cold start:
initial template envelope approved by a named authority, posture
auto-accept-with-maximum-sampling.

### E2 — Hot observation write path

**Kind:** epic · **Status:** pending · **Blocked by:** E1 · **Repo:** contextplane

`POST /v1/sessions/{id}/observations`. Sync: auth/tenant via the visibility
chokepoint, envelope digest check, idempotency, closed-schema + provenance
completeness (`observed_time` and `external_record_id` caller-supplied where
the stream declares an external source), PII scan per tenant policy, cheap
synchronous embedding, one partitioned insert. All else async with per-tenant
fairness and lag stamps. Published p99 includes the PII-block mode.

### E3 — Resolve-as-receipt fused retrieval

**Kind:** epic · **Status:** pending · **Blocked by:** E2 · **Repo:** contextplane

Extend `/v1/context/resolve` (no fourth surface): three concurrent
visibility-predicated candidate generators, RRF merge, batched hydration.
Synchronous receipt-intent record (single row, chained), async hydration,
receipt-loss RPO zero. Trust/quarantine state in the vector index key;
adversarial-selectivity benchmark gates.

### E4 — Provenance-scoped quarantine + DORA wiring

**Kind:** epic · **Status:** pending · **Blocked by:** E2 · **Repo:** contextplane

Quarantine by provenance predicate with dry-run blast-radius preview; bulk
bitemporal revert; pre-quarantine of downstream receipts; severity
classification mapped to DORA materiality with automatic incident-case
creation and evidence-bundle export.

### E5 — Review-budget allocator + reviewer cockpit

**Kind:** epic · **Status:** pending · **Blocked by:** E3, E9 ⚙ · **Repo:** both

One governed SamplingPolicy per (tenant, action class, sensitivity tier) with
acceptance-sampling math; expected-loss + leverage ranked queue with
consequence preview; `disposition_actor` (human | policy-automated) first
class; non-self-starving trust decay (frozen materiality at decay time; decay
is a trust-class transition, not supersession). Cockpit UI is the first-class
disposition surface.

### E9 — Model governance of allocator components ⚙

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane

Ranker, tier function, sampling designs, and fusion weights registered and
independently validated BEFORE any score-consuming feature activates. Encoded
as a required check: guarded paths flag-off until a committed
validation-evidence artifact exists and passes schema check.

### E6 — Tamper-evident spine + records management

**Kind:** epic · **Status:** pending · **Blocked by:** E2 · **Repo:** contextplane

Externally anchored tamper-evidence (bounded exposure window — never called
non-repudiation); retention classes; schedule-driven disposal via
crypto-shredding recorded as auditable deletion events; PII block tier for
undeclared streams.

### E7 — MCP surface contract + two-call memory loop

**Kind:** epic · **Status:** pending · **Blocked by:** E1 · **Repo:** contextplane

One machine-readable tool registry: default connection exposes ~6–8
envelope-derived core verbs; full surface opt-in per envelope; registry↔OpenAPI
parity gate and registry↔docs conformance gate. Two-call remember/recall with
safe defaults routing through the PII-scanned hot tier; time-to-first-memory
quickstart.

### E8 — Memory-quality eval harness

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane

Ground-truth labeled sets; extraction precision/recall per predicate;
retrieval relevance judged against receipts; multi-session recall; wired as a
release gate and published as buyer-facing evidence. Absorbs the speed
benchmark suite.

### E10 — UI/IA workstream

**Kind:** epic · **Status:** pending · **Blocked by:** E5 (screens), none (bug fixes) · **Repo:** contextplane-ui

Ordered: cockpit dispositions + quarantine/suspend screens → nav/DESIGN.md
repositioning + ARC/PII operations out of the raw console → canon copy.
Immediate bug fixes independent of the rest: remove the nonexistent
`traverse_dependencies` tool and the false "usage data" attribution from
getting-started; fix the quickstart `cd registry` typo; remove "semantic data
mesh" from the UI scope statement.

### E11 — Consumption legibility (suppression-compliant)

**Kind:** epic · **Status:** pending · **Blocked by:** E3 · **Repo:** both

Receipts explorer over existing endpoints; tenant-scope served-claims
aggregates under the existing suppression floors; audit-role drill-down with
recorded justification. Never per-actor cells outside the audit role.

### E12 — Migration/import path

**Kind:** epic · **Status:** pending · **Blocked by:** E1 · **Repo:** contextplane

Bulk-import API with provenance mapping; Backstage/CMDB/wiki connectors;
batch-attested "migrated-canonical" disposition class with sampled audit.

### E13 — Surface consolidation and deprecation

**Kind:** epic · **Status:** pending · **Blocked by:** E7 · **Repo:** contextplane

Simplicity is subtraction, not just profiling. Once the two-call
remember/recall loop (E7) is the served default: consolidate the five
observational write verbs (assert_claim, record_session_event,
add_workspace_entry, append_intent_checkpoint, ingest_signal) behind the hot
write path where their semantics overlap; deprecate redundant per-surface
variants with a dual-alias window; retire MCP tools that the registry shows
unused after the default profile ships; collapse read paths that fused resolve
subsumes. Tracked metrics: default-profile tool count (target ≤ 8), REST
endpoints an agent integration must know (target ≤ 6), and deprecated-surface
count trending to zero. Rule: no consolidation may drop a governance property
(provenance completeness, receipts, envelope gating) — surfaces shrink, the
control set does not.

### E14 — Graph/catalog boundary inversion

**Kind:** epic · **Status:** pending · **Blocked by:** E13 · **Hotspot:** yes · **Repo:** contextplane

The graph primitives live inside a package named for one of the views over
them. `service/catalog/` holds `entity`, `attribute_writes`, `facts`,
`projections` and `expansions` — entities, attributes, facts and edges, the
substrate every other subdomain reads and writes — while being named for the
producer/consumer catalog, which is one projection of that substrate rather
than its foundation. The dependency arrows follow the name: `service/memory`
imports it from eight files, and `retrieval`, `workspace` and `notifications`
each import it too, not because any of them wants a catalog but because that is
where the graph is. At 25 modules it is the second-largest area in the service
after `memory`, against 6 for `governance` and 3 for `operations`; its own
`__init__` describes it as "entities, facts, and everything that governs how
they are shaped and read"; and `core.py` carries a note saying it is named
`core` precisely so it does not stutter against the package, which is the
clearest available signal that the package name had already outgrown its
contents.

**Why this belongs to this plan.** E1–E12 all write through that substrate, so
every memory epic inherits the inversion: a hot observation write path (E2) and
a fused resolve (E3) both reach the graph by importing the catalog. The cost is
not aesthetic. It is that no import contract can express "memory may depend on
the graph but not on the catalog's product surface" while the two are the same
package, so the boundary cannot be enforced and will keep eroding as the
memory work lands on top of it.

**Explicitly not covered by E13.** E13 counts the agent-facing surface — tool
count, REST endpoints an integration must know. Relocating `entity.py` into a
graph package moves neither number, so every E13 metric can go green with this
inversion fully intact. The two are complementary subtractions, not the same
one: E13 removes surfaces, this removes a dependency direction.

**Sequencing and footprint.** Blocked by E13 because E13 deletes part of what
this would otherwise relocate — retiring the legacy capability surface first is
strictly less work than moving it and then deleting it. Marked hotspot: the
footprint is ~25 modules imported by 15 routers, the MCP server, ingest, audit,
wiring and four sibling service areas, so under the derived-footprint rule it
overlaps essentially any concurrent PR and must run serialized and alone.

**Decomposition requires an ADR first.** The target layout is a decision that
outlives the tasks implementing it, so it is recorded as an ADR before this
epic is cut into tasks — at minimum: which modules constitute the graph, where
the type-schema and vocabulary validators belong given that governing is what
they do, and whether the catalog keeps a package at all once the substrate
leaves it. The enforcing gate is `[tool.importlinter]` in `pyproject.toml`,
which already carries three contracts; a fourth naming the graph as a layer
below the catalog is what makes this durable rather than a one-time tidy, and
tasks cut from this epic are expected to land it with their first move.
