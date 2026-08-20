# Plan — governed agent memory (bank-grade, agent-speed)

Epic-level seed of the accepted 2026-08 plan. Every epic below requires
decomposition into ≤1-day tasks (by PR to this file) before any work is
claimable. Sequencing that is safety-relevant is marked ⚙ and must land as a
required check when the first dependent task is cut.

### E1 — Autonomy Envelope authority object

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane

One authority object: an ARC artifact of kind `policy` whose applicability
rules carry the delegated-authority matrix. Not `capability_contract` — that
kind means the contract a published capability offers its consumers, whereas an
envelope governs an agent principal's authority. If envelopes must be listable
as their own class, adding an `autonomy_envelope` kind is a CHECK-constraint
migration (`ck_arc_artifacts_kind`, 0001_baseline_schema.py) plus `ArtifactKind`
— a deliberate act, and cheap because no service logic branches on kind; a session ProvenanceGrant is a
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

**Kind:** epic · **Status:** pending · **Blocked by:** E2, E9 ⚙ · **Repo:** contextplane

Extend `/v1/context/resolve` (no fourth surface): three concurrent
visibility-predicated candidate generators, RRF merge, batched hydration.
Receipt write splits into a synchronous, durably committed receipt-intent row
(chained, receipt-loss RPO zero) plus async hydration of arms, items and
exclusions. The intent row MUST carry a completeness discriminator —
`hydration_state` (`pending` | `complete` | `failed`) plus the item and
exclusion counts known at write time — because `context_receipts` today has
only the envelope `state` column, so an un-hydrated receipt would otherwise
read as complete with zero exclusions, which the receipts module calls worse
than no receipt. `GET /v1/receipts/{receipt_id}`, `/exclusions` and
`/references` surface that state and never present a `pending` receipt as
evidence. Derivative registration (`register_receipt_links`) stays inside the
synchronous transaction — an unregistered derivative is reached by no erasure
and swept by no expiry. Hydration lag and failure alert against an SLO, and the
shipped fail-closed guarantee in `contextplane/context/resolve.py` may only be
relaxed once the discriminator exists in schema and read models.
Trust/quarantine state in the vector index key; adversarial-selectivity
benchmark gates.

### E4 — Provenance-scoped quarantine + DORA wiring

**Kind:** epic · **Status:** pending · **Blocked by:** E2 · **Repo:** contextplane

Quarantine by provenance predicate with dry-run blast-radius preview; bulk
bitemporal revert; pre-quarantine of downstream receipts; severity
classification mapped to DORA materiality thresholds; classification-as-major
starts a tracked regulatory notification clock — initial, intermediate and
final report windows stamped as distinct deadlines on the auto-created incident
case, with at-risk escalation and deadline state visible to the operator — and
evidence-bundle export scoped to that case. Quarantine is wired to both halves:
the classification and the clock.

### E5 — Review-budget allocator + reviewer cockpit

**Kind:** epic · **Status:** pending · **Blocked by:** E3, E9 ⚙ · **Repo:** contextplane, contextplane-ui

One governed SamplingPolicy per (tenant, action class, sensitivity tier) with
acceptance-sampling math; expected-loss + leverage ranked queue with
consequence preview; `disposition_actor` (human | policy-automated) first
class; non-self-starving trust decay (frozen materiality at decay time; decay
is a trust-class transition, not supersession). Cockpit UI is the first-class
disposition surface.

### E9 — Governed magnitudes ⚙

**Kind:** epic · **Status:** partly shipped · **Blocked by:** none · **Repo:** contextplane

Restated, because the original property could not be built. It read "no
ungoverned score orders anything a user sees", enforced automatically. Three
independent designs were attempted and each was defeated the same way: the
arithmetic sits in one function and the ordering elsewhere on a bare
attribute, so a detector watching either half sees nothing. Ranking is not a
syntactic act — any comparison produces an order — so a mechanical closure over
"code that ranks" degenerates into one over all code. A gate believed
exhaustive but defeated in a few lines is worse than none, because a reviewer
who finds it trusts nothing else it reports.

What is closeable is the **parameters**. A float in a weights position is a
syntactic fact; "this comparison is semantically a ranking" is not. Shipped:
`contextplane/ranking.py` on the bottom import layer, refusing an unknown id, a
form disagreeing with its payload, a reason under twenty words, and an empty
population; three magnitudes governed, the artifact recording whether each
consumer is `consumed` (reads at import) or `pinned` (a test asserts
agreement). The boundary is stated in the module rather than implied.

Remaining: bring each new scoring magnitude under it as E15–E17 land, and cover
what the closure cannot — semantic ranking, UI-side reordering — by periodic
review of new ordering sites rather than a gate pretending to be exhaustive.

### E6 — Tamper-evident spine + records management

**Kind:** epic · **Status:** pending · **Blocked by:** E2 · **Repo:** contextplane

Externally anchored tamper-evidence (bounded exposure window — never called
non-repudiation); retention classes; schedule-driven disposal via
crypto-shredding recorded as auditable deletion events; PII block tier for
undeclared streams.

### E7 — MCP surface contract + two-call memory loop

**Kind:** epic · **Status:** pending · **Blocked by:** E1, E2 · **Repo:** contextplane

One machine-readable tool registry: default connection exposes ~6–8
envelope-derived core verbs; full surface opt-in per envelope; registry↔OpenAPI
parity gate and registry↔docs conformance gate. Two-call remember/recall with
safe defaults routing through the PII-scanned hot tier; time-to-first-memory
quickstart.

### E8 — Memory-quality eval harness

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane

Not greenfield, and the earlier claim that the core product claim was
unfalsifiable was wrong. `eval/fixtures/` already holds 50 pre-authored
retrieval questions and 20 bitemporal scenarios, `recall@10` is measured
against a live embedder, and `make eval` now runs those plus the 24 ARC
selection cases in about five seconds — they were measured but not *askable*,
which is the part that shipped.

What remains: extraction precision/recall per predicate; retrieval relevance
judged against receipts; multi-session recall; and `eval_score` — an empirical
pass rate on a **held-out** replay suite, never a model's judgement of itself.
Held-out is the whole constraint: a procedure mined from episodes scores well
on those episodes and can be worse than nothing in production.

Promotion gates on a **delta against the incumbent**, not an absolute
threshold, and on the **lower bound** of the interval rather than the point
estimate — 19/20 carries a 95% interval of roughly 0.75 to 0.99, so promoting
on 0.95 is overconfident. That buys slower promotion or a larger replay suite;
the choice is recorded, not defaulted. The gate waits on procedural memory
existing to promote; the harness does not, and comes first.

### E10 — UI/IA workstream

**Kind:** epic · **Status:** pending · **Blocked by:** E5 (screens), none (bug fixes) · **Repo:** contextplane, contextplane-ui

Ordered: cockpit dispositions + quarantine/suspend screens → nav/DESIGN.md
repositioning + ARC/PII operations out of the raw console → canon copy.
Immediate bug fixes independent of the rest. In contextplane-ui: replace the
nonexistent `traverse_dependencies` with the real `get_dependencies` and drop
the false "usage data" attribution in
`apps/admin-dashboard/src/features/getting-started/GettingStartedDialog.tsx`;
remove "semantic data mesh" from the UI scope statement (`README.md`,
`CLAUDE.md`). In contextplane: fix the stale `cd registry` clone directory in
`README.md` and `docs/02-get-started/01-quickstart.md` — it is in this repo,
not the UI repo.

### E11 — Consumption legibility (suppression-compliant)

**Kind:** epic · **Status:** pending · **Blocked by:** E3 · **Repo:** contextplane, contextplane-ui

Receipts explorer over existing endpoints; tenant-scope served-claims
aggregates under the existing suppression floors; audit-role drill-down with
recorded justification. Never per-actor cells outside the audit role.

### E12 — Migration/import path

**Kind:** epic · **Status:** pending · **Blocked by:** E1, E5 ⚙ · **Repo:** contextplane

Bulk-import API with provenance mapping; Backstage/CMDB/wiki connectors.
Provenance mapping reuses the governed assertion path — `observed_time` and
`external_record_id` map from the source record and are never server-defaulted,
and no importer-local validation runs in parallel. The batch-attested
"migrated-canonical" disposition records `disposition_actor = policy-automated`
from E5, never by widening `approval_authority`, and its sampled audit draws
from E5's single governed SamplingPolicy, inheriting that policy's
below-minimum-sample halt rather than defining a second sampling regime.

### E13 — Surface consolidation and deprecation

**Kind:** epic · **Status:** pending · **Blocked by:** E2, E3, E7 · **Repo:** contextplane

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

### E15 — Salience: deciding what is worth keeping

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane

Nothing today decides what is worth remembering, so everything is kept — which
is the assumption that fails first at machine write volume. Salience is a
linear combination of observable signals, deterministic and cheap, and
auditable in the way a decision about what to remember has to be: state change,
outcome decisiveness, novelty against existing episodes, human engagement,
entity density, tool diversity. Computed at write, because every input depends
only on the episode itself.

Ships behind the naming rule first (ADR-0002): `SearchResult.score` is renamed
before three more scores arrive, because a bare `score` is the precedent that
teaches the next author one is acceptable. Eighteen call sites, no UI
references, two contract occurrences.

Weights are a governed magnitude in `contextplane/ranking_registry.json`, so
they carry a stated reason and change by PR. Learned weights are deliberately
**not** in scope: the label is "retrieved, cited, and present on a turn that
succeeded, within 30 days", and nothing currently joins citation to turn
outcome — receipts record what was served and feedback records ratings, but not
the join. Until that exists a learned model has nothing to train on, and
shipping one would mean inventing the label.

Calibration is not optional and is what makes the number mean something: 0.7
should mean roughly seven in ten such episodes get retrieved at least once,
tracked by reliability diagram and Brier score. The retention threshold is a
precision/recall operating point chosen from the same label data, not a
constant — it moves when storage or precision economics move.

### E16 — Truth confidence: corroboration and measured volatility

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane

Refines a built system rather than building one. Source-tier base scores,
lineage-digested corroboration and bin-based calibration all ship today;
`service/memory/calibration.py` already refuses an identity mapping and stores
a fit that misses target without selecting it.

Two additions. Corroborating sources combine by **noisy-OR** — `1 − Π(1 − pᵢ)` —
rather than addition, over sources deduplicated by **originating event** rather
than by record. Two extractions from one session are one observation counted
twice, and getting that wrong inflates confidence exactly where honesty matters
most; the lineage digest that makes this possible already exists.

Decay moves to **per-predicate**, from measured supersession churn rather than
authored figures (ADR-0003, which reverses the recorded model). The assumption
carrying it is stated there and is the thing most likely to be wrong: if
supersession tracks correction rather than genuine change, the rate measures
extraction quality instead of volatility. The first fit is inspected for that
before it may select, reusing the rule calibration.py already applies.

Confidence is never averaged with salience or eval_score (ADR-0002). They are
three quantities that happen to share a scale.

### E17 — Tenant-scoped scoring configuration

**Kind:** epic · **Status:** pending · **Blocked by:** E15 ⚙ · **Repo:** contextplane

Per ADR-0004: the committed registry holds the core default, and a tenant
overrides by publishing a profile **extension** activated through the existing
`plan → validate → activate → rollback` lifecycle. Core plus extension, never
replacement — the composition the profile system was built for, applied to
scoring rather than entity schemas. No environment variable and no `Settings`
field may set any of these: a weight deciding what an agent remembers is not
deployment configuration.

Two consequences the ADR records rather than discovers later. Every consumer
must resolve through one accessor, because that accessor is where tenant
resolution happens and a consumer reading the registry directly silently
ignores overrides. And that accessor cannot live in `contextplane/ranking.py`,
which sits at the bottom import layer and cannot reach the profile system — it
belongs beside the profile services, with `ranking.py` remaining the
core-default reader.

Per-tenant weights imply **per-tenant calibration**: a tenant on its own
weights needs its own reliability curve, since a global one describes a
population no tenant matches. That is the real cost of this epic and it lands
after the decision does. Single-tenant deployments pay none of it.

