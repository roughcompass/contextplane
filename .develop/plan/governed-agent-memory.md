# Plan — governed agent memory (bank-grade, agent-speed)

Epic-level seed of the accepted 2026-08 plan. Every epic below requires
decomposition into ≤1-day tasks (by PR to this file) before any work is
claimable. Sequencing that is safety-relevant is marked ⚙ and must land as a
required check when the first dependent task is cut.

**Supersession rule.** An epic that replaces a mechanism removes the replaced
one in the same epic — formula, field, flag, tuning knob — or records in its
body why the old path stays and until when. Nothing here relies on a dead-code
gate to catch the leftovers, because none exists: ruff flags unused imports
and variables, not unused functions or superseded branches, so a replaced
mechanism left behind passes every gate this repository runs while giving one
question two answers. Deletions ride the same PR as their replacement, where
the reviewer can see both halves.

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

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane

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

What was defeated is automatic **detection** of rankers nobody registered.
What was never defeated — and stays, because E3 and E5 carry the ⚙ pointing
here — is validation gating activation for **named** components: the registry
entry for a magnitude E3 or E5 consumes must carry independent-validation
evidence (who validated, against what data, with what result) before the
consuming feature's flag turns on, and that is encoded as a required check
when the first E3/E5 task is cut. Registration says a number is owned;
validation says somebody checked it predicts. The core registry shipped with
the first property only, so extending its schema with the evidence fields is
part of this epic, not a separate one.

Remaining otherwise: bring each new scoring magnitude under the registry as
E15–E17 land, and cover what the closure cannot — semantic ranking, UI-side
reordering — by periodic review of new ordering sites rather than a gate
pretending to be exhaustive.

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
The immediate bug fixes formerly listed here are **shipped** (2026-08-19):
`traverse_dependencies` replaced with the real `get_dependencies`, the false
"usage data" attribution dropped, "semantic data mesh" removed from both UI
scope statements, and the `cd registry` clone directory fixed in this repo
along with the prover and fidelity test that had let three mutually-consistent
copies of the wrong value pass. What remains of E10 is the ordered UI work
above. Catalog-side authoring is **E19**, cut separately rather than folded in
here: it is unblocked by E5, and it belongs to the catalog domain rather than
to this epic's memory-governance screens.

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

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane, contextplane-ui

Nothing today decides what is worth remembering, so everything is kept — which
is the assumption that fails first at machine write volume. Salience is a
linear combination of observable signals, deterministic and cheap, and
auditable in the way a decision about what to remember has to be: state change,
outcome decisiveness, novelty against existing episodes, human engagement,
entity density, tool diversity. Computed at write, because every input depends
only on the episode itself.

Ships behind the naming rule first (ADR-0002): `SearchResult.score` is renamed
before three more scores arrive, because a bare `score` is the precedent that
teaches the next author one is acceptable. The rename reaches the committed
contract, so it lands with a coordinated UI contract-pin bump — one UI PR
updating the pin and the regenerated client together, which is why this epic
spans both repositories. No field named bare `score` survives the change.

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

Two changes. Corroborating sources combine by **noisy-OR** — `1 − Π(1 − pᵢ)` —
**replacing** the shipped saturating curve
(`base + (1 − base) · headroom · (1 − e^(−mass/scale))` in
`service/memory/confidence.py`), which is not additive but is also not a
probability combination: it treats corroboration as mass against a tuned scale
rather than as independent evidence, so two strong sources and five weak ones
can land in the same place. The superseded formula and its `headroom`/`scale`
tuning knobs are **removed in the same change** — a replaced combination rule
left in place is two answers to one question. Sources are deduplicated by
**originating event** rather than by record before combining: two extractions
from one session are one observation counted twice, and the lineage digest
that makes this possible already exists.

Decay moves to **per-predicate**, from measured supersession churn rather than
authored figures (ADR-0003, which reverses the recorded model). The assumption
carrying it is stated there and is the thing most likely to be wrong: if
supersession tracks correction rather than genuine change, the rate measures
extraction quality instead of volatility. The first fit is inspected for that
before it may select, reusing the rule calibration.py already applies.

Confidence is never averaged with salience or eval_score (ADR-0002). They are
three quantities that happen to share a scale.

### E17 — Tenant-scoped scoring configuration

**Kind:** epic · **Status:** pending · **Blocked by:** E15 · **Repo:** contextplane

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

### E18 — Contract surface coherence

**Kind:** epic · **Status:** done · **Blocked by:** none · **Repo:** contextplane, contextplane-ui

**Done rather than first-wave-done, and the difference is checkable here in a
way it is not for E15–E17.** This epic states its scope as three enumerated
defects, each verified against the committed `openapi.json`. T2 fixed the
identifier names, T3 the tag vocabulary and its gate, T4 the `GET`/`POST`
collision, T1 decided how a published surface is renamed at all, and T5 carried
the three into the UI pin. There is no fourth defect the body names and no task
left uncut, so closing it records what happened rather than assuming a wave was
the scope.

One table backs the whole catalog — `Entity`, `__tablename__ = "entities"` in
`storage/models.py`; there is no `capabilities` table. Four HTTP write surfaces
sit on it (`POST /v1/capabilities`, `/v1/concepts`, `/v1/operations`, and the
generic `POST /v1/entities`), discriminated by `entity_type`. That much is
ordinary single-table inheritance and is not the problem. The problem is that
the surfaces disagree with each other about names, and in one place about
semantics. Three defects, each verified against the committed `openapi.json`:

- **`GET` and `POST /v1/entities` are unrelated operations.** `POST` asserts an
  entity through the generic profile-governed surface (tag `entities`). `GET`
  is an external-ID lookup requiring both `external_system` and `external_id`,
  and 404s when unmapped (tag `external-ids`). A `GET` on a collection path
  that cannot list the collection is the one item here that misleads an
  integrator rather than merely annoying them.
- **One resource, three names for its identifier.**
  `/v1/capabilities/{capability_id}/interface`,
  `/v1/capabilities/{entity_id}/artifacts` and
  `/v1/capabilities/{provider_cap_id}/adoptions` all take the same UUID.
- **The tag vocabulary stopped grouping.** 49 tags over 189 operations, three
  operations untagged, three delimiter conventions in use (`arc: admin`,
  `memory curation`, `external-ids`), and three paths whose methods are tagged
  into different subdomains — `/v1/capabilities` is `retrieval` on GET and
  `capabilities` on POST. `task memory` also survives the Intent rename that
  IDR-T04 already applied to the fixtures.

What is *not* wrong, recorded so a later pass does not "fix" it: the nineteen
colon custom methods (`:resolve`, `:query`, `:adjudicate`) are AIP-136 and are
applied consistently, and typed surfaces over a single-table discriminator is
the normal shape. The naming discipline slipped, not the architecture — and it
slipped because five renames each landed cleanly in code while the HTTP surface
accumulated the sediment.

This is not E13. That epic is subtraction — retiring surfaces the two-call loop
subsumes — and waits on E2, E3 and E7. This one removes no capability and waits
on nothing. Doing it first is also what keeps E13 measurable, because a target
of "≤ 6 endpoints an agent integration must know" cannot be counted while one
endpoint means two things.

The supersession rule applies with a wire-compatibility caveat: a renamed path
is removed, but only after the dual-alias window E18-T1 defines has expired,
and the window is recorded on the alias rather than left to memory.

### E19 — Catalog authoring in the dashboard

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane-ui

The dashboard can read the canonical graph and cannot write it. `POST
/v1/relationships`, `PATCH /v1/relationships/{relationship_id}` and `POST
/v1/relationships:query` reach the generated client and stop there — no adapter
function, no caller. So do `POST /v1/entities`, `GET /v1/entities:resolve`,
`/v1/concepts` and `/v1/operations`. `shared/api/catalog.ts` exports twenty
capability operations and nothing for any other entity type. An operator can
therefore traverse a dependency they have no way to create, and the Catalog
page presents capabilities as though they were the only entity type the service
has had since `02a1d07`.

The Catalog page stays a list, and that is recorded here because the opposite
was proposed and rejected on the design standard's own terms: `.develop/DESIGN.md`
in the UI repo says graphs "never replace discovery or impact lists" and
requires every graph be paired with a searchable table. `GET /v1/capabilities`
also returns a flat cursor page carrying no edges, so a canvas over it would
need one traversal per node against a service that publishes no graph total —
the browser would be inferring a shape the service declines to state. The
visual surface belongs on `/relationships`, beside the table, over the
traversal already running there.

Vocabulary follows storage: **Catalog** is the section, **entity** is the
thing, and capability, concept and operation are its types. The UI adopts that
regardless of what E18 settles on the wire, because the adapter layer is where
a contract seam is absorbed rather than mirrored into the IA.

**Still not done, and the reason moved.** #34 closed this epic because all six
first-wave tasks had shipped; a first wave is the claimable frontier rather than
the scope, and #35 reverted it. The gap that close missed — `POST /v1/entities`
having no client — is now closed by E19-T6.

What remains was found by auditing this body claim by claim rather than
assuming, which is how the last close went wrong. Its complaint about every
surface it names has two halves, "no adapter function, no caller", and one
surface still has only the first: `POST /v1/relationships:query` got its adapter
in E19-T1a and nothing calls it. Grepping the callers of all eight adapters this
epic produced also turned up `updateEntity`, shipped in T6 with no caller and
since removed. Both are E19-T7.

**What this epic cost so far, and what it found.** Six tasks were cut; nine PRs landed
on the UI and six on the service, because five of the six tasks had a premise
that did not survive contact with the tree:

- **T1b** was "add an `ETag` and an `If-Match`". Underneath it, relationship
  writes were validating `subject_type` against the *entity* family and so
  returned `unknown_entity_type` for every type a profile did declare, and
  `PATCH /v1/relationships/{X}` used its path id only as a 404 gate before
  asserting whatever the body described — returning `200` with a *different*
  `relationship_id` and leaving X untouched. The endpoint named "supersede"
  superseded nothing, on every request, and no test covered it.
- **T2** was "the page lists concepts and operations". It already did:
  `GET /v1/capabilities` is a general entity list whose `entity_type` is a
  filter, so the page was mislabelling rows it had already fetched.
- **T3** was to choose a graph library against the keyboard requirement. No
  library was needed: every clause of the standard's graph requirement comes
  free from focusable elements in the accessibility tree.
- **T4** was "behind the shell's global search". There is no global search, and
  the refusal it was meant to present could not offer the qualifying types
  because the handler dropped them.
- **T1c** hit the fifth: `target_revision` is required by every generic write
  and read by nothing, which is now E19-T5.

The common shape is that **the decomposition described the service the plan
believed existed.** What made these findable rather than shipped-over is that
each task was grounded against the handler before it was built — and what let
them survive this long is that no test asserted the effect: not the PATCH's,
not `validation.valid` on a relationship write, not what the ambiguity refusal
carried. The tree agreed with the plan by not looking.

---

## Task decomposition — first wave (the unblocked frontier)

Tasks for E1, E8, E9, E15, E16, E17, E18 and E19 only. The remaining epics
decompose after E1's decision tasks land, because their contracts would
otherwise embed values nobody has decided — the failure the earlier
decomposition audit found eight times in one pass. E1's claimable frontier *is*
its decisions.

E18 and E19 join the frontier on the same test the others pass, not by
exception: neither embeds an envelope, sensitivity-tier, grant-lifetime or
cold-start value, so neither waits on E1. E18 renames surfaces that already
ship, and E19 wires clients to endpoints that already ship governed.

**Wave status, 2026-08-20 (second update).** Every first-wave task is now done
except E19-T5 and E19-T6, both cut *from* first-wave work rather than planned
into it. E16-T2 turned out to have been satisfied when E16-T1 landed; its entry
records what covers it.

Only E18 closes as an epic, because it is the only one whose body enumerates its
scope — three named contract defects, all three fixed. E19 was closed in #34 and
that was wrong: `POST /v1/entities` is named in its body and had no task, which
is now E19-T6.

**Wave status, 2026-08-20.** E1, E8, E9, E15 and E17 have every first-wave task
done; E16 has one left (E16-T2, written and waiting on E16-T1 to land). None of
those epics is itself done, and their headers still read `pending` for that
reason: a first wave is the claimable frontier, not the scope. E1's four ADRs
decide how an Autonomy Envelope rolls out and nothing builds one; E15–E17 shipped
salience, its governance and its reporting without anything yet *consuming* a
salience score. Flipping an epic because its first wave closed would record work
as done that was never started, which is the failure this file's own
supersession rule is written against.

What the closed waves unblock is the second decomposition: E2 through E13 were
held because their contracts would otherwise embed values nobody had decided, and
ADRs 0005–0008 have now decided four of them.

### E1-T1 — ADR 0005: envelope rollout is advisory before it is enforcing

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: record the rollout decision the earlier audit found missing: landing
"no envelope, no authority" as specified breaks every existing deployment on
day one. The ADR decides the graduation path — advisory (log the would-be
refusal), then enforcing — mirroring the `advisory | warn | block` pattern PII
policy and progression definitions already use, and names the criterion for
flipping each stage. All six MADR sections, dissent included.

Acceptance:
    test -f .develop/adr/0005-envelope-rollout.md
    make doc-links && make doc-refs

### E1-T2 — ADR 0006: the data-sensitivity tier vocabulary and where it lives

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: close the vocabulary (the tier names and their order) and decide its
placement so the import contract accepts it — the earlier audit showed the
obvious placement failing `make lint` because consumers below `service` could
not reach it. The candidates are the bottom layer beside `ranking` or the
existing `config_grammar` layer; the ADR picks one and shows the consumer set
that forces the choice.

Acceptance:
    test -f .develop/adr/0006-sensitivity-tier-vocabulary.md
    make doc-links && make doc-refs

### E1-T3 — ADR 0007: grant projection lifetime and suspend propagation

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide how a ProvenanceGrant projection is bounded and how suspension
reaches a holder, honestly against what exists: there is no server-to-agent
push channel, so a sub-second revocation SLO cannot ride one. The ADR decides
the TTL, the re-validation trigger, and what SLO is actually promisable with
polling — or specifies the push mechanism as explicit new scope with its cost.

Acceptance:
    test -f .develop/adr/0007-grant-lifetime-and-suspend.md
    make doc-links && make doc-refs

### E1-T4 — ADR 0008: cold-start authority and initial posture

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: name who approves the first envelope when no conformance history exists
and what the initial posture is. The bank-plan adjudication settled the shape
— a named human authority, posture auto-accept-with-maximum-sampling, never
propose-only (which floods the review queue the plan exists to protect) — so
this ADR records it with the sampling rate and the first observation window's
length decided, not gestured at.

Acceptance:
    test -f .develop/adr/0008-envelope-cold-start.md
    make doc-links && make doc-refs

### E8-T1 — Extraction ground truth: a frozen labeled fixture and its gate

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a new frozen fixture `eval/fixtures/extraction_ground_truth.json` — 30
transcript excerpts, each labeled with the claims a correct extraction yields
(subject, predicate, value) — plus a gate test that asserts the fixture holds
exactly 30 cases, runs extraction over them, and reports precision and recall
per predicate. Report first, threshold later: the number goes in `eval/EVAL.md`
before anyone decides what to demand of it. Follows the EVAL.md discipline —
new file, frozen after first measurement, never edited in place.

Two things the task did not anticipate, recorded because both change what the
number means:

- **The measurement cannot run against `local-rules`.** That provider's own
  module says a benchmark against it measures the regexes, so a precision figure
  derived from the demo patterns and filed under "extraction quality" would be
  the self-consistent non-measurement this fixture exists to replace. The gate
  therefore splits: the fixture contract and the scoring arithmetic run always
  and need neither a database nor a provider; the measurement is opt-in on a
  real credential, following `test_extraction_live_provider.py`'s no-key-no-run
  rule.
- **The first measurement was a fixture bug, not a model result.** It reported
  precision 0.148 / recall 0.186. Eighteen of thirty excerpts named a service in
  prose while the label named it by reference, and the strategy tells a provider
  to use the reference exactly as it appeared in the data — so those labels were
  unreachable. Repaired, the same model measures 0.788 / 0.953. The gate that
  would have caught it is now in the suite. This is the argument for report-first
  in one episode: a threshold set beside that first number would have been set
  against the fixture's own defect.

Acceptance:
    .venv/bin/python -m pytest tests/integration/test_extraction_ground_truth.py -q
    make eval
    make lint && make typecheck

### E8-T2 — Retrieval relevance judged against receipts

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a relevance-judgment fixture over the existing 50 search questions —
for each, the entity ids a correct answer contains — and a report joining what
receipts say was served against those judgments, yielding precision@k
alongside the existing recall@10. Wired into `make eval`.

**precision@k is not receipt-derivable, and finding that out was the task's
real yield.** `context_receipt_items` carries the item's identity, block, source
and trust and has no rank, score or position column, so a receipt records which
items were served and not in what order. The envelope's item order is not rank
order either — `ordered_items` sorts a block by the receipt-item digest so that
two resolutions over unchanged data agree, which is a hash of the entity id. The
gate therefore reports two reads and names which is auditable: a set precision
from the receipt, and R-precision and precision@1 reconstructed from each item's
`payload["score"]`. Two follow-on consequences are recorded and not fixed: the
assembler's item cap truncates a block by that hash rather than by rank, and an
auditor cannot ask a receipt what the agent saw first.

Two missing tiebreakers were found and fixed in the same change, because the
measurement would not reproduce without them: the semantic and lexical arms both
ordered without a second key, so a tied `LIMIT` kept different rows on different
runs.

Acceptance:
    make eval
    .venv/bin/python -m pytest tests/integration/test_retrieval_relevance.py -q

### E8-T3 — Wilson-bound promotion gate, built before its consumer

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: the pure function the eval_score gate rests on: given incumbent and
candidate pass counts, promote only when the candidate's Wilson lower bound at
95% clears the incumbent's point estimate. Unit tests pin the arithmetic the
epic quotes — 19/20 yields an interval near 0.75–0.99, so a 0.95 point
estimate does not clear a 0.89 incumbent on twenty cases. No consumer yet;
the function and its tests land so the promotion path cannot later ship a
point-estimate comparison because the bound was never built.

Acceptance:
    .venv/bin/python -m pytest tests/unit/test_promotion_bound.py -q
    make lint && make typecheck

### E9-T1 — Validation evidence fields on the magnitude registry

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: extend `ranking_registry.json` entries with a validation block —
`status` (`validated` | `grandfathered`), `validated_by`, `validated_on`,
`method`, `result` — and make `ranking.py` refuse an entry carrying neither
status. The three shipped magnitudes are `grandfathered` with the reason
stated: they are in-production behaviour being brought under governance, not
new numbers claiming to have been checked. Registration says a number is
owned; validation says somebody checked it predicts; grandfathered says
neither and is honest about it.

Acceptance:
    .venv/bin/python -m pytest tests/unit/test_ranking_registry.py -q
    make lint && make typecheck && make test-unit

### E9-T2 — The required check: no validated-only consumer on an unvalidated magnitude

**Kind:** task · **Status:** done · **Blocked by:** E9-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: `scripts/check_governed_magnitudes.py` and a `make governed-magnitudes`
target wired into the CI lint job. It asserts every registry entry carries a
validation block, that `grandfathered` entries name a reason, and — the rule
E3 and E5's ⚙ points at — that any entry marked `requires_validated: true`
(which E3/E5 tasks will set on the magnitudes their flags consume) has
`status: validated`, not `grandfathered`. Anti-vacuity via `checklib`
`require_nonempty`: an empty registry already refuses to load, and the check
fails rather than passes if it inspects zero entries.

Acceptance:
    make governed-magnitudes
    .venv/bin/python -m pytest tests/unit/test_check_governed_magnitudes.py -q
    grep -q "governed-magnitudes" .github/workflows/ci.yml

### E15-T1 — Rename SearchResult.score to fused_rank_score

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — openapi.json · **Repo:** contextplane

Goal: per ADR-0002, no bare `score` field survives where the three scoring
quantities can reach. Rename the dataclass field and every call site, export
the contract, and confirm the only spec change is the two renamed occurrences.
Lands before any new score exists, while the rename is cheap.

Acceptance:
    make openapi-export && git diff --stat openapi.json
    make lint && make typecheck && make test-unit
    sh -c '! grep -rn "score: float" contextplane/types.py'

### E15-T2 — UI contract pin bump for the rename

**Kind:** task · **Status:** deferred — nothing to do · **Blocked by:** E15-T1 · **Hotspot:** yes — vendored openapi.json + generated client · **Repo:** contextplane-ui

Goal: one PR updating the vendored contract pin and the regenerated client
together, per the contract-bump procedure in `contracts/README.md`. No UI code
reads `.score` today, so the change is the pin, the client, and the pin-hash
note.

**Closed without work: the rename did not move the wire contract.** The task was
cut on the assumption that renaming `SearchResult.score` changes what the API
emits. It does not. `contextplane.types.SearchResult` is an internal dataclass,
and the API maps it onto `SearchResultItem.score` in the response model — a
different class that did not change. `openapi.json` is byte-identical before and
after E15-T1, verified by the drift gate at the time, so there is no pin to bump
and regenerating the client would produce no diff.

This is recorded rather than deleted because "we decided not to" and "nobody got
to it" look the same in a task list a month later, and because the assumption
that an internal rename reaches the contract is one the next reader is likely to
make again.

Acceptance: none. The check that this stays true is the existing `openapi.json`
drift gate in the service repo's conformance tier, which fails if the rename
ever does reach the wire.

### E15-T3 — The five write-time salience signals

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: pure functions computing five of the six signals from a session's event
window at extraction time — state_change, outcome_decisive, human_engagement,
entity_density, tool_diversity — each returning 0..1, each unit-tested against
hand-built windows including the empty one. Novelty is deliberately excluded:
it needs the claim's embedding, and embedding is queued at write and computed
asynchronously (`project_claim` queues; a consumer embeds), so a synchronous
novelty signal would be a lie about the architecture.

Acceptance:
    .venv/bin/python -m pytest tests/unit/test_salience_signals.py -q
    make lint && make typecheck

### E15-T4 — Salience stored on extracted claims, weights governed

**Kind:** task · **Status:** done · **Blocked by:** E15-T3, E9-T1 · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: a `salience` column on the claims table (additive migration), a
`salience-weights@1` registry entry (form `weights`, the six signals, reasons
per ADR discipline, validation status `grandfathered` until calibration
exists), and the write path storing Σ w·s over the five synchronous signals.
The novelty term is written as null and filled by the embedding consumer when
the vector lands — final salience is still precomputed relative to every
retrieval, which is what "at write" means for ranking purposes. Field name is
`salience`, never `score`, per ADR-0002.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m pytest tests/unit/test_salience_signals.py tests/unit/test_ranking_registry.py -q
    make test-unit && make lint && make typecheck

### E15-T5 — Salience reliability report from receipts

**Kind:** task · **Status:** done · **Blocked by:** E15-T4 · **Hotspot:** no · **Repo:** contextplane

Goal: the calibration half the epic calls non-optional, from data that already
exists: receipts record what was served, so "retrieved at least once" is
joinable today without the 30-day success label. A report bucketing stored
salience and plotting retrieval rate per bucket — reliability diagram plus
Brier score — wired into `make eval`, reported before any threshold consumes
it. The full label (cited on a succeeding turn within 30 days) stays out of
scope until the citation-to-outcome join exists; the report states which label
it used so the number cannot be mistaken for the stronger one.

Acceptance:
    make eval
    .venv/bin/python -m pytest tests/unit/test_salience_reliability.py -q

### E16-T1 — Noisy-OR replaces the saturating corroboration curve

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: corroborating sources combine by `1 − Π(1 − pᵢ)` in
`service/memory/confidence.py`, and the superseded saturating curve — the
`base + (1 − base) · headroom · (1 − e^(−mass/scale))` formula and its
`corroboration_headroom` / `corroboration_scale` knobs — is removed in the
same change, per the supersession rule. Changed confidence values invalidate
fitted calibration mappings, so the change also bumps the mapping version and
records in `calibration.py`'s terms that pre-existing fits are stored and
never selected against post-change scores. Unit tests updated to the new
arithmetic, including the property that N duplicate-lineage sources combine as
one.

Two corrections found while implementing, recorded here rather than left as a
disagreement between the plan and the tree:

- **The knobs are also columns**, so removing them is a migration and this task
  is a `storage/migrations/` hotspot, not the non-hotspot it was cut as. The
  acceptance grep below is scoped past `migrations/` accordingly: the baseline
  revision that created the columns is history and cannot stop naming them, and
  the revision that drops them has to name them to drop them.
- **The per-source probabilities are the base table**, read by authority rank,
  rather than the separate weight table the curve used. Noisy-OR needs a
  probability per source and the base table already states one; a second table
  would be a second ordering over one ladder. Corroboration is also capped one
  rounding step below the confirmed bucket, because that bucket means a human
  looked at the claim — the superseded curve did not hold that and let an
  owner-human claim with two corroborators read as confirmed.

`memory_confidence_policy` is now within two columns of being entirely dead:
nothing reads any of it, because `ConfidencePolicy` is constructed with shipped
defaults at every call site. E17 should retire the table rather than extend it,
since tenant-scoped scoring lands on the profile-binding system instead.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "confidence"
    sh -c '! grep -rn "corroboration_headroom\|corroboration_scale" contextplane/ --exclude-dir=migrations --exclude-dir=__pycache__'
    .venv/bin/python -m pytest tests/integration/test_confidence_policy_migrations.py -q
    make test-unit && make lint && make typecheck

### E16-T2 — Regression: same-session corroboration counts once

**Kind:** task · **Status:** done · **Blocked by:** E16-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: the lineage-digest dedup exists in `claim_writer.py`; this pins the
property the epic warns about with a test that would fail if it regressed —
two extractions from one originating session event combine to the single-source
confidence, not the two-source one. A vacuity control asserts the same pair
from two distinct events does raise it, so the test cannot pass by combining
nothing.

**Already satisfied when E16-T1 landed, and verified rather than assumed.** The
unit half is `test_evidence_sharing_an_independence_key_counts_once` and
`test_duplicate_lineage_scores_as_one_source`; the end-to-end half is
`test_repetition_through_one_source_does_not_raise_confidence`, which stages a
claim from four turns of one session and asserts it scores *identically* to one
turn. Its vacuity control is `test_independent_sources_agreeing_raises_confidence`,
which raises the score from two distinct sessions, and
`test_two_runs_of_one_connector_do_not_corroborate` covers the other collapsing
rule. Nine unit tests and three integration tests pass under this task's own
acceptance command.

The shipped property is stronger than the task stated. The task asked that two
extractions from one *event* count once; the independence key is
`session:{tenant}:{actor}:{session_id}`, so two extractions from two *different
events in one session* also count once — which is the case the epic actually
warns about, and the one the integration test exercises.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "lineage or corroborat"
    make test-unit

### E16-T3 — Per-predicate churn measurement, fitted and inspected

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: measure each predicate's supersession half-life from the bitemporal
history and store fitted rates the way calibration mappings are stored — a
table (additive migration), a fit report, and the ADR-0003 rule enforced in
code: a fit is stored and never selected until inspected, and the inspection
looks specifically at whether supersession reflects correction rather than
genuine change (the assumption the ADR names as most likely to be wrong).
Predicates below the observation floor carry no rate and fall back.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m pytest tests/unit/test_predicate_churn.py -q
    make lint && make typecheck

### E16-T4 — Decay reads the per-predicate rate, category as fallback

**Kind:** task · **Status:** done · **Blocked by:** E16-T3 · **Hotspot:** no · **Repo:** contextplane

Goal: `confidence_decay.py` resolves half-life per predicate where an
inspected fit exists, category-plus-subject otherwise, and its docstring — 
which currently argues against per-predicate rates — is rewritten to state the
ADR-0003 model, per that ADR's own requirement: a reversal that leaves the
original reasoning in place gives the codebase two contradictory explanations
of one behaviour.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "decay"
    sh -c '! grep -q "twenty-six figures" contextplane/service/memory/confidence_decay.py'
    make test-unit && make lint && make typecheck

### E17-T1 — The tenant-resolving accessor beside the profile services

**Kind:** task · **Status:** done · **Blocked by:** E15-T4 · **Hotspot:** no · **Repo:** contextplane

Goal: one accessor in the profile layer resolving a scoring magnitude for a
tenant — active binding's extension value if one exists, else the committed
core from `ranking.py` — per ADR-0004, which also records why it cannot live
in `ranking.py` (bottom layer, cannot reach the profile system). Every scoring
consumer resolves through it; a consumer reading the registry directly
silently ignores overrides, so the accessor's test asserts the fallback chain
in both directions.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "scoring_accessor or profile"
    make lint && make typecheck && make test-unit

### E17-T2 — Scoring overrides in the extension schema and binding lifecycle

**Kind:** task · **Status:** done · **Blocked by:** E17-T1 · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: a tenant extension may carry scoring-magnitude overrides — same forms
and validation the registry enforces, reasons included — published and
activated through the existing `plan → validate → activate → rollback`
lifecycle, with an integration test proving a planned-but-unactivated override
governs nothing and a rolled-back one restores the prior value.

**A schema gap surfaced doing this, so the task is a migrations hotspot after
all.** `profile_bindings` recorded only `extension_set_digest` — a hash over the
extension ids — and discarded the ids `plan_binding` was handed. A digest can
verify a set somebody already has and cannot produce one, so the schema had no
answer to "which extensions is this tenant governed by". Nothing needed the
answer until a resolver had to read a bound extension's contents.

The first implementation worked around it by enumerating the tenant's extensions
against the bound core revision and checking the digest matched. That is wrong in
a way that looks right: enumeration also finds extensions the tenant published
and never bound, so a tenant with one bound and one shelved extension produced a
mismatch and was refused for an ordinary configuration. The rollback test caught
it — the case the lifecycle exists for. `0059` adds
`profile_binding_extensions`; the digest stays as an integrity check over a set
the schema can now state.

Acceptance:
    env CONTEXTPLANE_TEST_PG=testcontainers .venv/bin/python -m pytest tests/integration/test_profile_bindings.py tests/integration/test_scoring_overrides.py -q
    make lint && make typecheck

### E17-T3 — Per-tenant calibration split

**Kind:** task · **Status:** done · **Blocked by:** E17-T2, E15-T5 · **Hotspot:** no · **Repo:** contextplane

Goal: the cost ADR-0004 records arriving late, paid: reliability reporting
(E15-T5) and calibration mappings key by tenant wherever a tenant runs its own
weights, with the global curve retained for tenants on core defaults. A tenant
below the observation floor reports "assurance not earned" rather than
borrowing the global curve, mirroring the small-cell suppression discipline.

The calibration half is deliberately **defined and inert**, and the reason is
recorded rather than left implicit: the split applies where a tenant overrides a
magnitude that feeds the numbers being calibrated, and no shipped override does.
Salience decides what is remembered and does not enter `confidence.score`, so
every fit this deployment writes carries the `shared` scope. Building the key now
means the separation happens on the day it is needed rather than the day somebody
remembers it is needed — the same argument that put `requires_validated` in the
registry before its check existed.

Acceptance:
    make eval
    .venv/bin/python -m pytest tests/unit -q -k "calibration"
    make test-unit

### E18-T1 — ADR 0009: how a published HTTP surface is renamed

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide the dual-alias window E13 assumes and E18-T4 needs, before the
first rename rather than during it. The ADR fixes how long an alias lives, how
it is marked in the contract (`deprecated: true` plus a sunset stamp, which
OpenAPI already models, so nothing new is invented), whether a deprecated alias
may differ in behaviour from its successor — it may not, or the window becomes
a second implementation — and what actually retires one. The honest constraint
to record: neither this repository nor the UI can currently see third-party
callers, so retirement cannot rest on an observed-zero-usage claim it has no
instrument for. All six MADR sections, dissent included.

**That constraint was stated wrongly and the ADR corrects it.** There *is* an
instrument: `usage_events.operation` holds the route template per tenant, so
"has anyone called this path" is a query that runs today. It is the wrong
instrument for two reasons the tree already records — the usage tier is
deliberately lossy, so zero observed and zero are different facts, and
`check_usage_boundary.py` forbids any decision path reading it. A retirement is a
decision. The correction matters because "we cannot see" invites somebody to
build the instrument and then use it, while "we can see and must not decide from
it" is the rule that survives the instrument existing.

Acceptance:
    test -f .develop/adr/0009-renaming-a-published-surface.md
    make doc-links && make doc-refs

### E18-T2 — One path parameter for one identifier

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — openapi.json · **Repo:** contextplane

Goal: `{capability_id}` and `{provider_cap_id}` become `{entity_id}` across the
five `/v1/capabilities/*` templates that still use them. No URL changes — a
path-template variable is positional on the wire — so this is not a rename
under E18-T1 and needs no alias; what changes is the generated client's
parameter names, which is the whole reason it is a contract hotspot. The twelve
`capability_id` occurrences inside `components` are request and response *body*
fields, out of scope and left alone, which is why the acceptance grep is scoped
to path keys rather than the file.

Acceptance:
    make openapi-export
    sh -c '! grep -nE "\"/v1/capabilities/\{(capability_id|provider_cap_id)\}" openapi.json'
    make lint && make typecheck && make test-conformance

### E18-T3 — One tag vocabulary, and a gate that keeps it

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — openapi.json · **Repo:** contextplane

Goal: one delimiter convention across all 49 tags, every operation tagged, no
path split across subdomains by method, and `task memory` moved to the Intent
vocabulary IDR-T04 already applied to the fixtures. Then
`scripts/check_contract_tags.py` and a `make contract-tags` target wired into
the CI lint job, built on `checklib` in the shape `check_surface_inventory.py`
already uses: it fails if any operation is untagged, if a tag mixes
conventions, or if one path's methods carry tags from two subdomains.
Anti-vacuity via `require_nonempty` — a run that inspects zero operations fails
rather than passes, which is how the tag gate avoids the failure mode E9's
restatement describes.

Acceptance:
    make openapi-export
    make contract-tags
    .venv/bin/python -m pytest tests/unit/test_check_contract_tags.py -q
    grep -q "contract-tags" .github/workflows/ci.yml
    make lint && make typecheck

### E18-T4 — Split the `/v1/entities` GET and POST collision

**Kind:** task · **Status:** done · **Blocked by:** E18-T1 · **Hotspot:** yes — openapi.json · **Repo:** contextplane

Goal: the external-ID lookup moves off the collection path to `GET
/v1/entities:lookup`, matching the nineteen AIP-136 colon methods the contract
already carries and its sibling `GET /v1/entities:resolve` in particular. `GET
/v1/entities` is then left **absent** rather than backfilled with a generic
entity list: inventing a list nobody asked for would be new scope inside a
coherence epic, and `GET /v1/capabilities` already serves the listing job for
the type anyone lists today. The old path keeps working for the window E18-T1
defines, marked deprecated with its sunset, and the issue that removes it is
cut in the same PR so the alias cannot outlive the plan that created it.

Acceptance:
    make openapi-export
    .venv/bin/python -m pytest tests/conformance/test_openapi_drift.py tests/conformance/test_generic_profile_parity.py -q
    make lint && make typecheck && make test-conformance

### E18-T5 — UI contract pin bump for E18

**Kind:** task · **Status:** done · **Blocked by:** E18-T2, E18-T3, E18-T4 · **Hotspot:** yes — vendored openapi.json + generated client · **Repo:** contextplane-ui

Goal: one pin bump carrying all three contract changes, per the procedure in
`contracts/README.md`. It also absorbs the drift already sitting between the
current pin (`00613eb`) and service HEAD — `update_entity` and
`update_relationship` lost their generated path suffixes in `53960d3`, two
operationId lines and nothing else — so the bump closes the existing gap rather
than widening it. No UI code reads any renamed parameter today, so the diff is
the pin, the regenerated client, and the hash note. Serialize against E15-T2:
both touch the same UI hotspot and contract-bump PRs run one at a time.

Acceptance:
    pnpm generate:api && git diff --exit-code -- apps/admin-dashboard/src/shared/api/generated/
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T1 — Relationship writes: adapter and edge authoring

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Goal: adapter functions for `POST /v1/relationships`, `PATCH
/v1/relationships/{relationship_id}` and `POST /v1/relationships:query` in
`shared/api/`, plus the authoring UI that uses them — create an edge from a
capability's detail dialog and from the Relationships page, edit or retire one
from a traversal result. The repo's own contract rules carry the detail rather
than being restated here: a fresh idempotency key per create, `If-Match` from
the detail `ETag` on update, a `412` that keeps the draft and refetches, and
branching on `errors[].code` never on message text.

**Two of those four are unbuildable against the service as it stands, and the
task splits because of it.**

`GET /v1/relationships/{relationship_id}` emits no `ETag` and `PATCH` reads no
`If-Match`. Entities do — `contextplane/api/routers/_entity_crud.py` computes one
and returns it on the detail read — and relationships were never given the same
treatment. Nothing in the contract advertises an `ETag` response header on any of
its 242 operations, so the omission is uniform rather than specific, and it means
the UI cannot send a concurrency token it is never handed, nor test a `412` that
never arrives.

`ContextplaneClient.request` also returns only the parsed body and discards
response headers, so even once the service emits an `ETag` the adapter has no way
to read it. That is a UI-repo prerequisite and a small one.

So:

- **E19-T1a** (contextplane-ui) — **done**: the three relationship adapters,
  `errors[].code` branching, and a caller-owned idempotency key per create.
  Colocated tests cover create and permission-denied. No optimistic concurrency,
  because there is nothing to be optimistic against.
- **E19-T1b** (contextplane) — **split into three, all done**; see below.
- **E19-T1c** (contextplane-ui) — **done, in two parts.** The client's
  `ETag`-reading method, the 24 test doubles it broke, the second pin bump and
  the two adapters landed first; the authoring UI and its `412` recovery
  followed. Split because the first part touches every test file in the app and
  the second touches one feature, and reviewing them together would have buried
  a 24-file mechanical migration under a new surface.

**T1b split again, because grounding it found two defects underneath it.** The
task read "add an `ETag` to the detail read and an `If-Match` to the update".
Neither was buildable as stated:

- **T1b-i — the relationship validator (done).** The surface validated its
  `subject_type` through `EntityValidator`, which reads only the `entity` family
  of the canonical document. A relationship type is declared in the
  `relationship` family, so it was never found: every relationship write, on all
  three intent routes, returned `unknown_entity_type` against a type the tenant's
  profile did declare, with `valid: false` under a mandatory binding for a write
  the service had accepted. Nothing branched on it, which is why it survived —
  and why T1a's adapter would have surfaced the artifact to an operator as though
  it were a finding.
- **T1b-ii — an update targets its path id (done).** `update_relationship` used
  its path id only to check the row existed, then asserted whatever the body
  described. A `PATCH /v1/relationships/{X}` with different endpoints returned
  `200` with a *different* `relationship_id`, created a second unrelated edge,
  and left X untouched. The endpoint summarised as "supersede a relationship"
  superseded nothing, on every request, and no test covered it. An `If-Match` on
  a write that lands on a different row than the `ETag` describes is concurrency
  control in appearance only, so this had to land first.
- **T1b-iii — the `ETag` and `If-Match` themselves (done).** Advertised in
  `openapi.json` rather than merely emitted; the validator includes
  `effective_to` because a supersession does not otherwise touch the row it ends.

The pattern from the previous wave repeated: **the decomposition described the
service the plan believed existed.** Three tasks last wave, two more here. What
distinguishes these two is that both were reachable by reading the handler —
no test asserted the PATCH's effect, and no test asserted `validation.valid` on
a relationship write, so the tree agreed with the plan by not looking.

**The client's `ETag` method moved from T1a to T1c during T1a.** It was written
first: `requestWithEtag` on `ContextplaneClient`, both methods sharing one
`perform()`. Adding it to the interface broke the typecheck in 22 test files,
each of which builds a `{ request: vi.fn() }` double that no longer satisfies
`ContextplaneClient`. The fix is mechanical but the trade is not worth taking
early — until T1b lands, `requestWithEtag` returns `etag: null` at every one of
the contract's 242 operations, so the change buys nothing and the 22 doubles pay
for it. It lands in T1c beside the first endpoint that has an `ETag` to read.
Making the method optional was the alternative and is worse: an optional method
means every adapter carries a fallback branch that no endpoint ever takes.

Recorded rather than worked around because the alternative shapes are both worse:
shipping the adapter with an `If-Match` header derived from nothing would look
like concurrency control and be none, and skipping the concurrency test would
leave the task's own acceptance list describing a case nobody exercises. Colocated tests cover the
create, the concurrency conflict, and the permission-denied path, because those
are the three the adapter can get wrong silently.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "relationship"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T2 — Catalog covers every entity type, in one vocabulary

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Goal: the Catalog page lists concepts and operations beside capabilities,
filterable by type, with `POST /v1/concepts` and `POST /v1/operations` wired for
creation — the service has offered both since `02a1d07` and the UI neither.
Naming follows the epic: Catalog the section, entity the thing, type the
discriminator, and the page copy that presents a capability as the only kind of
record is corrected in the same change. No new nav destination; this is the
existing page learning the rest of its domain.

**The premise was half wrong, in the direction that made the task smaller.**
`GET /v1/capabilities` has never been capability-only: `entity_type` is a
filter, and with the filter absent `list_capabilities` returns every type the
tenant holds. The endpoint is named for its first caller, not for what it
lists. So the page was *already* receiving concepts and operations and
presenting them under a heading that said "Capabilities", in a column headed
"Capability", with a count labelled "Capabilities on page". Nothing was missing;
the page was mislabelling rows it had already fetched.

Shipped accordingly: a service-side `?type=` filter, the epic's vocabulary
throughout, and one `createCatalogEntity` routing by type rather than three
near-identical adapters — with `parent_capability_id` in a discriminated union
member, since only a concept and an operation have a parent to send.

The filter offers the three types with dedicated create routes.
`/v1/admin/entity-types` would enumerate more, but it lists types holding a
registered *schema* rather than types that exist, and it is an admin route a
catalog browser may not be able to call. A fourth type still lists under "All
types" and is named in the Type column; only creation is limited.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "catalog"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T3 — A graph view on /relationships, beside the table

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Goal: a node-link rendering of the traversal `/relationships` already runs,
URL-addressable as a view parameter so a copied link reconstructs it, toggling
against the existing table rather than replacing it. The design standard's
graph clause is the acceptance surface rather than decoration: focused root
with visible direction, relationship type, depth, version and time scope;
progressive expansion; disclosed hidden-node counts; a legend; searchable node
names; selection opening the same accessible detail a table row opens; all of
it keyboard-operable, and no task requiring a drag to complete.

One honest gap this task must not paper over: the UI `CLAUDE.md` requires
bundle and route budgets enforced in CI, and no such gate exists in the
repository today — no `size-limit`, no budget config, nothing in `ci.yml`. So
the rendering library is chosen against the keyboard requirement first (a
library that cannot satisfy it is disqualified before size is discussed) and
the task records the measured route bundle size in its PR body. Building the
budget gate is real work and belongs to its own issue, not smuggled in here as
an acceptance line that would have to build the gate to pass.

**Shipped with no rendering library, which resolved the tension the task
flagged rather than arbitrating it.** Every clause of the standard's graph
requirement — keyboard-reachable nodes, selection opening the same accessible
detail a row opens, no drag or spatial memory — comes free from focusable
elements in the accessibility tree, and no canvas library provides any of them
without a parallel keyboard layer built beside it. So the layout is arithmetic
and the rendering is SVG with real buttons. The route bundle went 25.10 kB to
35.36 kB raw (7.53 to 10.44 kB gzipped), all first-party, and with no dependency
added there is nothing for a bundle budget to arbitrate. The budget gate is
still absent and still its own issue.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "graph"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T7 — The governed reads and edits nothing calls yet

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Found by auditing E19's body claim by claim before deciding whether the epic
could close. Its complaint has two halves — "no adapter function, no caller" —
and one surface still has only the first.

**`queryRelationships` has no caller.** E19-T1a shipped the adapter for
`POST /v1/relationships:query` and nothing uses it. The Explore area reads the
older dependency, dependents and blast-radius endpoints, which return bare edges;
the governed query returns `GovernedRelationship` rows carrying profile,
provenance, validation and readiness. That is a different answer to a different
question, so this is not a swap: it is deciding where an operator should see the
governance on an edge, and the honest candidate is the entity detail dialog,
where they are already looking at one thing.

**A governed entity edit.** `updateCapability`, the dedicated PATCH, is called
from `CapabilityOverviewPanel`. Its generic twin was shipped in T6 with no caller
and removed again rather than left standing, because an adapter without a caller
is the defect this epic is about. Bringing it back means the panel growing an
edit that routes by intent, the same choice T6 gave the create — and the same
reason: an edit that should be reviewed and an edit a producer makes on something
they own outright are different acts.

Not folded into T6. T6's question was which surface a *create* takes and it
answered it; this is the same question for reads and edits, and answering both in
one change would have made the create's answer hard to see.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "governed"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T6 — The generic entity write has no client

**Kind:** task · **Status:** done · **Blocked by:** E19-T5 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: an adapter and a caller for `POST /v1/entities`, the surface E19's own
body names alongside the three that now have one. It reaches the generated
client and stops there.

**Not the same surface E19-T2 wired.** That task used `POST /v1/concepts` and
`POST /v1/operations`, the dedicated create routes, which take a name and
optional attributes and mint a row. The generic write is the profile-governed
one: it routes by `intent`, so an ordinary agent's observation stages a claim
and only an authorized approval writes canon, and it carries the same identity,
temporal, provenance and validation envelope a relationship write does. An
operator creating an entity through the Catalog page today gets the ungoverned
route, which is the correct one for a capability and the wrong one for anything
whose write should be reviewed.

So the task is not "add a fourth create button". It is deciding which surface
the Catalog page's create should use, and the honest answer depends on the
intent the operator has — which means the create dialog grows the same intent
choice the relationship authoring dialog has, and the dedicated routes stay for
the case where a producer is registering something they own outright.

Blocked by E19-T5 because a generic write must send `target_revision`, and what
that field is for is the open question there. Building a second caller against
an unresolved answer would mean writing it twice, and would double the number
of places quietly sending a value nothing reads.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "entity write"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T5 — `target_revision` is required and read by nothing

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Every generic write requires `target_revision` on `ProfileWriteRequestV1`
(`contextplane/api/schemas/profile_writes.py:170`). `TargetRevisionV1` says why:

> Sent by the caller rather than assumed from whatever is currently bound,
> because a body composed against one revision and validated against another
> passes or fails for reasons the caller cannot see.

**Both of its fields are unread.** `profile_revision` is compared to nothing, and
`binding_revision` (`profile_writes.py:109`) appears exactly once in the tree —
its own declaration. `profile_bindings` has no revision column, so
`binding_revision` names a concept that does not exist.

#### The decision

**The value is the binding identity a caller can actually read, checked inside
the validator, and reported as a violation rather than refused.**

1. `profile_revision` carries `profile_revision_id`, the UUID. It is the only
   candidate a caller can both read (`BindingResponse.profile_revision_id`,
   already published) and receive back from every prior write
   (`ProfileAttributionV1.profile_revision_id`). No new field is exposed.
2. `binding_revision` carries `extension_set_digest`, or is deleted. It is the
   slot for the half of the governing vocabulary a revision id does not cover: a
   tenant can rebind to a different extension set at the *same*
   `profile_revision_id`, and migration `0060_binding_extension_members` names
   that rollback case as the one the binding lifecycle exists for. Leaving one
   field unread while fixing the other reproduces the defect.
3. **The comparison lives in `validate_entity_write` and its relationship twin,
   not in the routers.** `contextplane/entities/validation.py` states that it is
   the seam, and `scripts/check_profile_write_coverage.py` fails the build when a
   writer bypasses it. A check in the two routers is invisible to `SchemaService`,
   to the promotion writer, and to MCP — whose module header calls itself "the
   agent-facing twin … same services, same routing, same refusals".
4. **A mismatch emits a `Violation`, not a refusal.** The binding then decides:
   refused under `mandatory`, reported under `advisory`, absent under `unbound`.
   `ValidationOutcomeV1` already carries violations on a *successful* write for
   exactly this. A router-level refusal keyed to a caller-supplied string would
   invert that module's own first rule — "the binding decides the mode, not the
   caller. A caller that chose its own enforcement level would be a caller that
   could opt out."
5. **A new code, `stale_target_revision`.** `incompatible_target_revision` is
   live: a publication-time compile conflict emitted from three modules, with
   `tests/conformance/test_profile_schema_entity.py` asserting
   `emitted == set(CONFLICT_CODES)`. Overloading it would give one code two
   meanings on two surfaces.

#### What was tried first, and why it was wrong

The proposal was to compare against `profile_revisions.semantic_version` and
expose that on `BindingResponse`. Two independent adversarial reviews refuted it.
The refutation is recorded because the mistake is instructive rather than
embarrassing.

**The strongest evidence for it was evidence against it.** The claim was that
`tests/integration/test_generic_relationship_writes.py` seeds
`semantic_version='1.0.0'` and sends `"1.0.0"`, so enforcement would be
non-breaking. That fixture also seeds
`profile_name = f"rel-{revision_id.hex[:12]}"` — a *different profile every run*,
at the same version string. Uniqueness is
`(profile_family, profile_name, semantic_version)`, so a bare `"1.0.0"`
identifies nothing. The suite continuing to pass would have demonstrated that the
comparand is not an identifier, not that the change was safe.

It also failed the case the field exists for. `semantic_version` does not move
when a rebind changes only the extension set, so the check would return green in
exactly the scenario `TargetRevisionV1` describes.

Four shapes were in play across two repos before this decision — `"1.0.0"`,
`"core-3"`, `"relationship.v3"` and the bound UUID the E19-T1c dialog sends —
which is what a required field nobody reads produces: every client invents one.

#### Two things the task must still settle

- **A `validating` tenant cannot read what to send.** `active_binding()`
  (`contextplane/profile/bindings.py:375`) filters `state = 'active'`, while the
  validator governs on `('active', 'validating')`. A tenant bound only for
  validation is governed in advisory mode and reads `bound: false`, so it has
  nothing to attest to. Either the conformance read widens, or the check exempts
  that state and says so.
- **The guarantee is route-shaped, not tenant-shaped.** `POST /v1/concepts` and
  `/v1/operations` write the same entities through `_entity_crud` with no
  `target_revision` field at all, so anything refusable on the generic surface is
  achievable on the dedicated one. Better stated in the task than discovered
  after it ships.

The eventual `mandatory` bite changes what the service refuses, so it carries
ADR-0005's pre-flight obligation — offender scan, dry run, refusal with the
offender list, force only with a written migration plan — and needs its own
record. This task lands the violation; it does not land a flag day.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "target_revision or stale"
    make lint && make test-coverage

### E19-T4 — Entity resolution in global search

**Kind:** task · **Status:** done · **Blocked by:** E18-T5 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: `GET /v1/entities:resolve` behind the shell's global search, so a handle
resolves to one entity and an ambiguous handle is presented as the refusal the
service actually returns — `identity_ambiguous`, with the qualifying types
offered as choices and never a silently picked first match, which is the
failure the endpoint was designed to refuse. Blocked on the pin bump because
the sibling lookup path moves in E18-T4, and building against the pre-rename
client would mean writing this adapter twice.

**Two premises failed, and the second was a defect.**

There is no global search to go behind. `⌘K` focuses whatever filter the current
page has, and a page filter narrows rows already fetched — a different act from
asking the service which entity a name refers to. So the task added the surface:
a `search` slot on `AppShell`/`AppHeader` and a resolver in the header. `⌘K` was
left alone; it has coverage and does something the new field does not, and
repurposing it would trade working behaviour for a convention.

And the refusal could not offer the qualifying types, because the service never
sent them. `AmbiguousIdentity` carries its candidates — "so the caller can
requalify without a second query", per its own docstring — and the HTTP handler
dropped them, leaving a client that must branch on `code` and never on `message`
with nothing to present but "that was ambiguous". Fixed in contextplane#31, and
tested at the handler rather than through the API: `uq_entities_tenant_name`
still forbids two same-named entities in one tenant, deliberately, until the
0051 expand contracts. The UI handles the no-candidates case anyway, because
every deployment is that case until #31 ships.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "resolve"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

---

## Task decomposition — second wave (E1's build, now that its decisions have landed)

Tasks for **E1 only**. The first wave's note said the second decomposition is
what the closed waves unblock, because E2–E13 were held while their contracts
would otherwise embed values nobody had decided. ADRs 0005–0008 decided four of
those values, and all four are E1's — so what they unblock first is E1's own
build, not the twelve epics downstream of it.

**E2–E13 still wait, and for a reason narrower than before.** E2's hot write
path takes an "envelope digest check" and E7's tool registry exposes
"envelope-derived core verbs"; neither can be cut against an envelope that has no
shape yet. Cutting them now would embed a guess about the object E1-T6 defines,
which is the same failure the first wave's held epics were held to avoid. They
become cuttable when E1-T6 and E1-T7 land, not when this wave opens.

**Every value these tasks embed is quoted from an accepted ADR, not chosen
here.** Where a task states a constraint the ADRs do not settle, it says so and
names what must be established rather than assuming it.

Each task's premises were checked against the tree before it was written, which
is how E19 went — five of its six tasks had a premise that did not survive that
check. Two did not survive here either, and both are recorded in the tasks
themselves: `autonomy_envelope` is not the kind to add, and the "applicability"
an authority matrix wants is `arc_applicability_rules`, not the same-named
memory-claim concept.


### E1-T5 — The sensitivity vocabulary becomes one closed, ordered module

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: `contextplane/sensitivity.py`, exactly as ADR-0006 decided — a closed
ordered tuple at the bottom import layer, declared `"ranking | sensitivity"` in
the layers contract, with set membership derived from the order rather than
written out beside it, and `rank()` raising on a name it does not know.

Nothing about an envelope is needed to do this, and the module has consumers
today: the ADR names `contextplane/sharing/authorization.py` and
`contextplane/arc/vocabularies.py` as the two furthest apart, and verified with
probe imports that both can reach a bottom-layer module without breaking the
import contract. Those probes were reverted; this task makes them real.

The ADR is explicit that the module must *not* decide what a caller does about
an unknown name. The two call sites that treat an unreadable label as most
restrictive keep doing so at the call site; the one that refuses to rank keeps
refusing. Folding either rule into the vocabulary would change a
security-relevant decision in two places as a side effect of moving a constant.

Cut first because it is the only E1 build task with no envelope in it, so it
neither waits for the rest nor blocks them.

Acceptance:
    make lint
    make test-unit

### E1-T6a — An envelope is a `policy` artifact bound to a principal

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: the envelope object — an ARC artifact of kind `policy`, and the binding
that says which agent principal it governs.

`ck_arc_artifacts_kind` (`0001_baseline_schema.py:2420`) admits `standard`,
`policy`, `adr`, `runbook` and `capability_contract`, so `policy` needs no
migration. **`autonomy_envelope` is deliberately not added.** E1's body makes the
new kind conditional on envelopes needing to be listable as their own class, and
nothing here needs that: it costs a CHECK-constraint migration plus an
`ArtifactKind` member whose docstring currently records that "this phase adds no
new kind", and buys a filter nobody has asked for.

**The binding is the new thing, and the reason this task split.** E1's body says
identities bind to IAM workload identities, and nothing in the tree does that.
The nearest existing record is `actors`, which carries `oidc_subject` and an
`actor_kind` that already distinguishes `human` from `sync_worker`
(`0001_baseline_schema.py:217`). `arc_directive_identities` is unrelated — it
identifies directives, not principals.

Without a binding, an envelope is indistinguishable from any other `policy`
artifact, which is why the matrix (E1-T6c) is not the half to build first.

**Settled while building, and load-bearing for E1-T6c and E1-T7:**

- **The principal is an `(issuer, subject)` pair, not an `actors` row.** That is
  the settled ARC idiom — `created_by`, `opened_by`, `author`, `admitted_by` and
  `actor` are all bare TEXT pairs with no foreign key — and
  `ArcRequestContext.operator_identity` already produces one.
  `WorkloadIdentity` names the pair so a transposition at an authority boundary
  cannot pass as a valid lookup. `host_id` was considered and rejected: it is an
  unregistered label on attestation keys, with no host table behind it.
- **A binding names a revision, never an artifact.** Otherwise a governed widen
  takes effect the moment a new revision activates, and a principal's authority
  changes as a side effect of somebody publishing.
- **"The bound revision is a `policy`" is a composite foreign key, not a service
  check.** `arc_artifacts` gained `UNIQUE (artifact_id, kind)` and
  `arc_revisions` gained `UNIQUE (revision_id, artifact_id)` to make it
  expressible. A service-only guard is one refactor from gone, and losing it
  turns a `runbook`'s corpus-selection rules into an agent's authority.
- **One envelope per principal is an `EXCLUDE USING gist`, restricted to
  `active`.** Restricting to `active` is what lets suspend-then-grant be the
  widen path. It also forced a resolver rule: order by `state = 'active'` first,
  because during a widen a suspended row and its replacement both cover *now*
  and recency alone picks arbitrarily between them.
- **A revoke closes to an *empty* interval when the binding never took effect**
  (`GREATEST(now, effective_from)`), so the interval CHECK is `>=` rather than
  `>`. `tstzrange(x, x)` is empty and overlaps nothing, so a withdrawn grant
  frees its slot with no special case.
- **Only suspension is authorized at tenant scope. Grant, reinstate and revoke
  are authorized at the envelope's, and a deployment operator may act on a
  binding in any tenant.** This took three attempts and the middle one was a
  privilege escalation, recorded below because the reasoning that produced it
  was locally correct at every step.

**The escalation, and why "narrowing is safe" was the wrong rule.** Version one
authorized every operation at the envelope's scope, which meant a tenant admin
could not switch off their own misbehaving agent under a global envelope without
finding a deployment operator — the exact situation E1's "instant suspend"
exists for. Version two fixed that by authorizing the *narrowing* operations
(suspend, revoke) at tenant scope, on the argument that narrowing cannot grant
an actor anything they did not already hold. That argument is true per operation
and false over sequences, because at the time the exclusion constraint carried
`WHERE (state = 'active')` — so a suspension released the principal's slot:

    tenant admin suspends the operator's binding        (tenant scope: allowed)
    tenant admin authors a tenant `policy` artifact     (tenant admin: allowed)
    tenant admin grants it to the same principal        (tenant scope: allowed)
    resolve prefers the active row and returns it       -> self-authored
    governance has replaced deployment-mandated governance

Each step passes its own check; the trace does not. Reachability was never
analyzed, only per-call authorization. Two changes close it, and both are needed:
the exclusion constraint now reserves the principal for **any open interval
whatever the state**, so suspension releases nothing; and **revoke**, which does
close the interval, moved to envelope scope. Suspension is then genuinely pure
narrowing and can stay at tenant scope, so incident response survives.
`test_a_tenant_admin_cannot_displace_a_global_envelope` is the regression test
and fails if either half regresses. Found by adversarial review, then reproduced
before being fixed.

Consequences worth carrying forward:

- **The widen path is revoke-then-grant**, not suspend-then-grant. This also
  removed a zombie: a superseded-by-suspension binding kept an open interval
  forever, so once its replacement was revoked, `resolve` returned the *old
  suspended* row and reported a principal as suspended for a reason recorded
  during a widen months earlier.
- **A deployment operator may act on a binding in any tenant.** Allowlist
  membership confers no tenant role, so without break-glass an operator could
  never suspend or revoke a binding they granted, and a tenant could squat a
  principal's only slot with a binding the operator was powerless to remove.
- **`suspend` and `reinstate` also require an open interval.** Guarded only on
  `state`, a revoked binding — which keeps `state = 'active'`, since revocation
  closes the interval instead — could be suspended and then reinstated, leaving
  `state = 'active'` with `effective_to` in the past. A `resolve(at=...)` inside
  the old window then reported it active for a period it was suspended:
  retroactive history rewriting in a governance table.
- **The bound revision must be `active` at grant time**, checked at grant only.
  Nothing stopped binding a principal to a `draft`, which would let whoever can
  write a draft decide what an agent may do with no approval and no actor
  separation. `resolve` now carries the revision's lifecycle state so the
  decision path can see a revision revoked after binding; what to do about that
  is E1-T7's call, not this read's.
- **A tenant-scoped envelope may govern only its own tenant's principals**,
  checked separately from the write gate because break-glass bypasses that gate.
  Application-level rather than declarative, unlike the `kind` check: the SQL
  version needs the artifact's nullable tenant collapsed to a sentinel and a
  three-column composite key, and the failure it prevents is a misconfiguration
  by the trust root rather than an escalation by anyone below it.
- **`effective_from` may not precede now.** `resolve(at=...)` is what a later
  audit reads to ask whether an action was within envelope; a backdatable
  binding lets the party being audited write that answer afterwards.

**A name collision worth knowing about before reading ARC.**
`arc_expected_impact_envelopes` is a blast-radius forecast attached to an
authoring proposal version — how many selections change if a revision activates.
It is about a document, not a principal, and shares only the word. Everything
added here says `autonomy` rather than relying on context to disambiguate.

Also fixed here, because it was in the file this depends on:
`ArtifactScope.capability_id` survived the E1-T6b rename next to a guard message
already reading "an entity-scoped artifact requires an entity id". Five lines
across two modules and two tests.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    make lint && make typecheck && make test-coverage

### E1-T6b — Applicability says `capability` where it means `entity`

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — `openapi.json` · **Repo:** contextplane

Goal: `AuthorityScope.CAPABILITY` becomes `ENTITY`, and `capability_ids` /
`capability_labels` become `entity_ids` / `entity_labels`.

**Landed as a plain rename in place, not as ADR-0009's published-surface
procedure.** This task was written expecting an alias window — `deprecated:
true`, `x-sunset-on`, `x-successor`, the way E18-T4 handled the external-ID
lookup — and that expectation was wrong. That procedure exists for names
consumers already hold; this service has never been released, so there are no
signed manifests, no generated clients and no stored artifacts spelled the old
way. Migration `0061_arc_entity_scope` renames columns and closed-value checks
in place, following `0049_arc_intent_nomenclature` step for step. The paragraph
below describing the alias is left standing because it records what was assumed
and why it did not survive contact with the fact that nothing has shipped.

Same reason the manifest side renamed here too rather than being frozen: the
digest-stability argument below is sound and applies to hosts that do not exist.

Two live bugs surfaced while doing it, both fixed in the same change:
`arc_admin.py:216` validated `lower_scope_kind` against
`^(tenant|domain|capability|task)$` while line 470 fed the value to
`AuthorityScope(...)`, so `task` returned 500 and the correct `intent` was
rejected with 422. And `ArtifactScope.capability_id` was missed entirely; it was
caught later, during E1-T6a, sitting next to a guard message that already read
"an entity-scoped artifact requires an entity id".

**One table backs the whole catalog.** There is no `capabilities` table —
`Entity`, `__tablename__ = "entities"`, with `entity_type` discriminating
capability, concept and operation. E18 fixed exactly this confusion on the HTTP
paths, where one resource had three names for its identifier; the same confusion
survives untouched in ARC's applicability vocabulary.

**Nothing enforces the narrower name, which is the evidence it is wrong.**
`submission.py:596` parses `capability_ids` as bare UUIDs and validates nothing
about their type; `selection.py:229` matches them as opaque set membership
against the manifest's set. So an applicability rule scoped to a *concept* or an
*operation* is already expressible — the only obstacle is that the field
telling you what you are scoping to says the wrong word. The vocabulary is
narrower than the mechanism, which is the direction that misleads: a reader
concludes the matrix cannot scope to an operation, and it can.

This is a **published-surface rename**: `scope` is a wire enum on
`ArtifactApplicabilityRule` and both fields are wire properties, so it takes
ADR-0009's marking — the alias stays with `deprecated: true`, an
`x-sunset-on` and an `x-successor`, exactly as E18-T4 did for the external-ID
lookup. The DB columns, the Python dataclass, the `__post_init__` guard whose
message reads "capability-scoped but names no capability", and the selection and
submission paths all follow.

**`0049_arc_intent_nomenclature` is the template, and it is a close one.** That
revision renamed two columns and six closed-value checks in place, and it records
the order the checks force: drop the check, update the rows, re-add it naming the
new value — updating first violates the old check, adding first violates on the
old rows. It also rewrites `applicability_snapshot` and recomputes
`applicability_digest`, copying the digest algorithm inline rather than importing
it, "because a migration is a statement about one moment in the schema's history
and must keep producing the same bytes after the service it was written beside
has moved on".

**Only one of the two `capability_ids` may move, and the other must not.** The
name appears on both sides of the match:

- `ApplicabilityRule.capability_ids` — the *rule's* selector. Its snapshot digest
  is derived state, is "not signed evidence and nothing external verifies it"
  per `0049`, and is recomputable. **This one renames.**
- `IntentManifest.capability_ids`, mirrored into `ManifestClaims.as_claims_dict()`
  — the *manifest's* declaration, hashed into the claims digest a **host signs**
  and verification recomputes. Renaming the key changes the digest for every host
  that has not changed with it, and the failure is
  `403 blocked_manifest_unverified` rather than a validation error. **This one
  does not rename here.** It is the same reason ADR-0006 gave for leaving ARC's
  `data_sensitivity` open.

In the event **both sides renamed**, so the mismatch this paragraph was written
to explain never came into being. The reasoning holds for a released service and
this one is not: no host has signed anything, so there was no digest to preserve
and no coordinated change to stage. Had even one deployment existed, the split
above would have been the right answer.

Blocking E1-T6c rather than merely adjacent to it: a matrix written in a
vocabulary that says capability when it means entity teaches every envelope
author the same wrong thing, and renaming after envelopes exist is a migration
rather than a rename.

Acceptance:
    make openapi-export && git diff --exit-code -- openapi.json
    make lint && make typecheck && make test-coverage

### E1-T6c — The authority matrix is applicability, and never names a principal

**Kind:** task · **Status:** done · **Blocked by:** E1-T6b · **Hotspot:** yes — `openapi.json` · **Repo:** contextplane

Goal: the delegated-authority matrix expressed in `arc_applicability_rules`, and
the rule that a principal is never one of its dimensions.

**The dimensions line up, and that was checked rather than assumed.**
`ApplicabilityRule` (`contextplane/arc/types.py:462`) selects on
`intent_kinds`, `action_classes`, `entity_ids`, `entity_labels`,
`domain_ids`, `environments` and `data_sensitivity_tiers`. `IntentManifest`
(`types.py:494`) — "the attested description of what the agent is about to do" —
carries `intent_kind`, `requested_action_classes`, `entity_ids`,
`domain_ids`, `environment`, `data_sensitivity` and `repository_identity`. They
map one to one, and those are the dimensions E1's body names when it says
"stream-scoped action-class and sensitivity declarations". So the matrix is
expressed in the predicate that already ships rather than beside it.

**A principal is not among them, and must not be smuggled in.** `AuthorityScope`
is `global | tenant | domain | entity | intent` and the rule has no actor
column. An implementer reaching for `domain_ids` or `entity_labels` to encode
which principal an envelope governs would put principal-scoping outside
`_SCOPE_ORDER`, so precedence would not see it — and a rule meant to narrow
authority for one agent would widen it for every agent matching the same domain.
That is the failure this task exists to prevent, which is why the binding is
E1-T6a's and the predicate is only ever about the act.

**There is a second candidate substrate, and it loses.**
`arc_observation_class_predicate_v2` (`schemas/authoring_profile_shapes.py:146`)
is a closed, schema-validated, canonicalizable predicate over
`intent_kind | requested_action_classes | environment | data_sensitivity_tier |
entity_ids | domain_ids` — the same six dimensions, already frozen at V1 and
active at V2, already carrying the `min_items` and non-overlap checks
`arc_expected_impact_envelope_items` needs. It looks like a better fit than an
applicability rule and is not, for two reasons. It has **no precedence**: its
items are required to be non-overlapping, which is a different rule from
`_SCOPE_ORDER` resolving which of several matching rules wins, and an authority
matrix needs the second. And it is **hashed into host-signed attestation
bytes**, so giving it an authority meaning would entangle what an agent may do
with what a host has signed. E1's own body says "applicability rules", and this
is why that is right rather than merely what it says.

`data_sensitivity_tiers` is a bare `ARRAY(Text)` on every path — wire schema,
dataclass, column — with no validation anywhere. E1-T5's closed vocabulary is
what gives those strings meaning, and validating rule values against it is part
of this task. **The scale is `contextplane.sensitivity.TIERS`, not ARC's
`content_classification`**: the values in use are `restricted` and
`confidential`, and `restricted` is in the former only.

**Both failure modes were measured, and the first correct one is the opposite of
what this note originally said.** A typo does not widen:

    rule=['restrcited'], manifest='restricted'  -> does not apply
    rule=['restricted'],  manifest='regulated'  -> does not apply
    rule=[],              manifest='regulated'  -> applies
    rule=['restricted'],  manifest=None         -> does not apply

`_matches_scalar` returns `False` for a non-empty rule set that does not contain
the value, so a misspelled tier makes the rule govern **nothing**. The mechanism
is narrowing; the consequence is still the dangerous one, because for a
*mandatory* rule "governs nothing" is an obligation that silently stops
blocking.

**And the manifest side is an agent-controllable evasion, which is the bigger
half.** `arc.py:159` accepts `data_sensitivity: str = Field(min_length=1,
max_length=64)` — any string. `sensitivity.py` records that ARC's
`data_sensitivity` is "deliberately open because it is mirrored into a host's
signed attestation". So closing only the rule side is theatre: a host declares
`data_sensitivity="ultra-secret"` and escapes every tier-constrained rule, as
rows two and four above show. This task therefore does both:

- **Rule side closed.** Only a known tier may be written, refused at
  authoring, so a typo fails loudly instead of disarming an obligation.
- **Manifest side fails closed.** An unrecognized or absent tier is ranked as
  `MOST_RESTRICTIVE` at match time rather than matching nothing. That is not a
  new invention — `sensitivity.py` documents two existing call sites that
  already treat an unreadable label as maximally sensitive, and says the rule
  belongs at the call site where it is visible. Keeping the manifest field open
  while making the *interpretation* safe also preserves the reason it is open:
  the host still signs whatever it declared, and the digest is unaffected.

**A widening bug found while grounding E1-T6a, and this task owns it.**
`entity_labels` is stored, submitted, snapshotted, materialised and round-tripped
everywhere — and **never matched**. `rule_applies` (`selection.py:198`) has no
`entity_labels` branch, and `IntentManifest` has no labels field to match
against. Meanwhile `ApplicabilityRule.__post_init__` accepts labels *alone* as
satisfying the entity-scope requirement, and so does the database:
`ck_arc_rules_entity_scope_target`, read from the live schema, is

    scope <> 'entity' OR array_length(entity_ids,1) >= 1
                      OR array_length(entity_labels,1) >= 1

So this constructs, passes every constraint, and returns `True` against a
manifest about something else entirely:

    ApplicabilityRule(scope=ENTITY, entity_labels=frozenset({"payments"}))

The narrowest scope above `intent` becomes the widest. `_matches_any`'s own
docstring says "the dangerous cases are refused at construction rather than
silently widened here" — and this is the door construction leaves open.

**The repo believes this is unreachable, and that belief is out of date.**
`tests/unit/test_arc_selection.py` calls `entity_labels` "the live example" and
says it is "inert only because nothing populates the column"; `corpus.py`'s
`_obligation_rule` says the asymmetry "is inert only because `rule_applies`
matches on `entity_ids`". Both were written against **one** of the two write
paths. `ApplicabilityDraft` — direct artifact registration — indeed has no
`entity_labels` field and cannot express it. But the **authoring-proposal path
can**: `_applicability_rule` in `authoring_profile_shapes.py` accepts
`entity_labels` on the wire, `submission.py:597` reads it off the candidate,
`_applicability_rule_row` carries it into `MaterialisedApplicabilityRule`, and
`insert_applicability_rule` writes it — with nothing but the CHECK above in
between, which permits labels-only.

**Confirmed by observation, through the real write and read paths.** A probe
called `insert_applicability_rule` (the function `submission.py:314` calls) with
`entity_ids=None, entity_labels=("payments",), scope="entity"`; the database
accepted the row. Reading it back through `corpus._rule_from_row` rehydrated
`ApplicabilityRule(scope=entity, entity_ids=set(), entity_labels={'payments'})`,
and `rule_applies` returned **True** for a manifest carrying an unrelated entity
id and an unrelated domain. Nothing was hand-constructed. So the "inert" claims
in `test_arc_selection.py` and `corpus.py` need retracting along with the fix —
they are true of `ApplicabilityDraft` and false of the service as a whole.

Three ways out, to be decided with grounding rather than now:

- **Drop `entity_labels`.** It cannot be evaluated by a pure matcher — resolving
  a label to an entity needs the catalog, and `rule_applies` is deliberately
  I/O-free so a receipt replays identically months later. A selector that cannot
  be evaluated is a hole, not a feature. Costs a column drop plus the wire
  schema, submission, materialisation, provenance, corpus and shadow paths.
- **Require `entity_ids` for entity scope**, leaving labels as an unmatched
  annotation. One line, closes the widening, and leaves a dimension that still
  reads like a selector and is not one.
- **Match it**, by adding labels to `IntentManifest`. Free of the usual
  digest-stability objection now that nothing has shipped, but it makes the
  agent's own declaration authoritative for a label the catalog owns.

Whichever wins, the guard belongs in `__post_init__` next to the existing two,
because that is where the tenant and entity cases are already refused. Note that
`tests/unit/test_arc_directive_types.py::test_a_capability_rule_may_use_labels_instead_of_ids`
asserts exactly the construction that widens, so it pins the bug in place and
has to change with the fix — a test asserting a defect is not a reason to keep
the defect, but it is a reason to state what the test was for.

**Decided: drop `entity_labels`.** Adversarial review agreed with the
destination and corrected two of the reasons, both of which were wrong:

- "Corpus-load resolution would read the catalog at load time, so a replay
  resolves differently" is **false**. `as_of` is already threaded through
  `_CANDIDATES_SQL`, and the tree already has bitemporal storage. As-of-addressed
  resolution would be replay-stable. The real blockers are elsewhere: **the
  catalog has no labels at all** — `Entity` carries no label or tag column and
  no label vocabulary exists anywhere — and the **obligation tombstone** must
  answer "who did this apply to" from `applicability_snapshot` alone, with no
  live rule to re-resolve against, which forces freezing resolved ids at
  authoring time and collapses into "just use `entity_ids`". Also
  `arc_approved_exceptions.lower_scope_entity_id` is a single UUID, so an
  exception could never be granted at label granularity — half the authority
  model cannot express it.
- Rejecting "let the manifest declare labels" on the grounds that it makes the
  agent authoritative for something the catalog owns is **also false**: the
  manifest already carries `domain_ids` as free strings with no backing table
  and no validation. The correct objection is that it would make `entity_labels`
  mechanically identical to `domain_ids` — same type, same overlap semantics,
  same absent registry — which is one question with two answers.

Two claims in this note also need softening. "Nothing has shipped so removal
costs no consumer" overstates it: `openapi.json` carries the field and
`contextplane-ui` vendors that contract, so the cost is a regeneration rather
than zero. And "labels are the only governance that survives new entities" is
backwards — an empty selector already matches everything, so over-inclusion is
the default and enumeration is what you opt into; `domain_ids` and the other
non-enumerative dimensions all work today.

**The removal is scoped to the class, not the field**, because the same review
found two more instances of it, both verified:

- **`scope='domain'` with no `domain_ids` matched every manifest, at rank two —
  one rank *above* the entity hole.** So did `scope='intent'` with no selectors.
  Reachable through `ApplicabilityDraft`, not just the authoring path. The
  exception half has always required the correspondence
  (`ck_arc_exceptions_scope_selectors`); the rules table enforced two of four.
  Fixed in `__post_init__` and in SQL by `0063_rule_scope_selectors`.

  **The fallout is the finding's own evidence.** Adding the two guards broke 48
  integration tests across seven files plus the shared authoring-pipeline
  helper, and every one of them broke the same way: a fixture building
  `scope="intent"` with every selector `None`, or `scope="domain"` with no
  domain. That shape was the default a test reached for, which is exactly why it
  was worth forbidding — it was the cheapest rule to write and it matched every
  manifest at a rank above the rules that narrowed. The fixtures now name the
  selectors their scope requires.
- **The same input was a durable outage, not a widening.**
  `ApplicabilityRule.__post_init__` raises `ArcVocabularyError`, a
  `RegistryError` — neither `ValueError` nor `TypeError`, which is all
  `corpus._obligation_rule` caught. A mandatory labels-only entity rule produces
  a snapshot with empty `entity_ids` (the snapshot has no labels field), so
  rehydration threw straight past the handler and past `_obligations`, and
  *every* context resolution for that tenant failed until the row was deleted.
  The careful "an unreadable obligation must still block" path never ran. The
  tenant-scoped variant crashes the same way. Fixed by widening the `except`.

**What landed, in two changes.** The matrix-soundness half — the sensitivity
scale on both sides, the domain and intent scope guards in Python and in SQL
(`0063_rule_scope_selectors`), and the `ArcVocabularyError` fallback — then the
`entity_labels` removal (`0064_drop_entity_labels`), which touches the wire
schema and so regenerates `openapi.json`, the authoring schema snapshot, and the
260 canonical vectors. The vectors' churn is wider than the field: an
`artifact_semantics_digest` covers the shape that lost it, so every profile
digesting artifact semantics moves. The Node reference verifier agrees on all
260 afterwards, which is what says the regeneration is a regeneration rather
than a drift.

Two things deliberately preserved through the vector regeneration. The
`duplicate_set_entry` negative built its violation *out of* `entity_labels`, so
it moved to a repeated `domain_ids` entry rather than being left to pass
vacuously — a byte-pinning negative that no longer fails for the reason it names
is worse than a stale one. And the V1 authoring shape loses the field too:
`_applicability_rule` generates V1 and V2 from one description, so V1 is
regenerated in lockstep by construction rather than frozen, and will only become
frozen at first release.

**Not done here: the principal-is-never-a-dimension guard has no executable
check.** The rule is stated in `ApplicabilityRule`'s docstring and enforced only
by the absence of an actor column. That is weaker than the rest of this task and
is E1-T7's to close, because the decision path is where a principal would
actually get smuggled into a predicate.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "applicability or scope_selector"
    make openapi-export && git diff --exit-code -- openapi.json
    make arc-vectors && make lint && make typecheck && make test-coverage

### E1-T7 — The authority decision, computed and never cached

**Kind:** task · **Status:** done · **Blocked by:** E1-T6c · **Hotspot:** no · **Repo:** contextplane

Goal: the decision point that reads an envelope and answers whether a principal
may act — derived from the envelope row on every decision, per ADR-0007, with no
projection stored and no TTL.

The ADR is unusually specific about what *not* to build. There is no grant cache,
so there is no invalidation, no sweeper and no staleness window: "how long does a
grant projection live" is the wrong question, and the answer is that it does not
live, it is computed. Suspension therefore needs no propagation mechanism — a
status flip on the envelope row is visible to the next decision made by any
replica because no replica holds a copy.

**The SLO is a bound on operations, not on wall-clock**, and the task must not
restate it as a duration. "A suspended envelope authorises no operation that
begins after the flip commits" is testable; "sub-second" is not measurable on
what ships, because the latency histogram's buckets top out at ten seconds. A
revocation SLO with no test and no bucket would be a sentence rather than a
promise.

**Built as a pure function plus a two-read service.** `decide(envelope, rules,
manifest, tenant_id, as_of)` is I/O-free and takes `as_of` as a parameter, so a
receipt replays to the same verdict; `AutonomyDecisionService` does the two reads
— resolve the binding, load the bound revision's rules — on every call. Nothing
is cached, so the "no invalidation" claim is a property of the shape rather than
a discipline anyone has to maintain. The integration tests flip a row through the
service and decide again through the *same instance*, with a fixed clock, which
is what makes them a test of the mechanism rather than of a restart.

**Five verdicts, not a boolean.** `permitted | no_envelope | envelope_suspended |
envelope_withdrawn | outside_envelope`. The four refusals stay distinct because
the next two tasks need to tell them apart: E1-T8 records what *would* have been
refused, and E1-T9's pre-flight counts principals that acted with **no envelope
at all** — a scan that is unbuildable if every refusal reads the same.

**Deny by default, and `envelope_withdrawn` is the one that needed deciding.**
A binding deliberately outlives the revision it names, because ending bindings as
a side effect of revoking a document would be a decision taken silently. E1-T6a
therefore has `resolve` report the bound revision's lifecycle state and leaves the
question open; this task answers it fail-closed. A `policy` revision that ARC has
revoked, superseded or expired is one somebody took out of force, and continuing
to derive authority from it is exactly the failure the revocation was meant to
cause.

**The principal-is-never-a-dimension rule is now executable**, which E1-T6c left
open. `test_the_predicate_never_receives_the_principal` asserts on the type that
no `ApplicabilityRule` field is principal-shaped, and its companion asserts that
two principals bound to one envelope get identical verdicts. `decide` passes the
manifest to `rule_applies` and nothing else — which principal is asking was
already answered by *which envelope resolved*.

`corpus._rule_from_row` became public as `rule_from_row` for this: the decision
rehydrates the rules of one revision and must do it exactly as corpus assembly
does, and two mappings of the same row into the same dataclass would be two
places for the selector set to drift.

**Still not wired to a caller.** The decision exists and is tested; no route or
MCP tool consults it yet, because in the advisory stage it must record rather
than refuse, and that stage is E1-T8. Shipping enforcement before the stage
column exists would be landing "no envelope, no authority" on day one, which
ADR-0005 exists to prevent.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "envelope and (suspend or decision)"
    make lint && make test-coverage

### E1-T8 — Advisory recording, which is what the graduation scan later reads

**Kind:** task · **Status:** done · **Blocked by:** E1-T7 · **Hotspot:** no · **Repo:** contextplane

Goal: the `advisory` stage from ADR-0005 and ADR-0008 — an enforcement stage
column on `tenants` (`advisory | enforcing`, CHECK-constrained, defaulting to
`advisory`), and a decision path that in `advisory` refuses nothing and records
what *would* have been refused.

`tenants` already carries three tenant-level policy columns of this shape
(`is_regulated`, `notification_digest_window`, `memory_retention_days`), each
with a CHECK constraint, so the column is the house pattern rather than a new
one. Per tenant rather than per deployment, because a multi-tenant deployment
that can only graduate everybody at once cannot graduate anybody.

**No environment variable and no `Settings` field sets it.** ADR-0005's argument
is that this repository's one precedent for a security-relevant flag is that a
flag may only make behaviour *more* restrictive, and a mode read from the
environment would be the first able to widen authority — without an audit row
naming who widened it.

**The rate is one, and it is called recording.** ADR-0008: sampling at rate 1.0
is recording, and naming it sampling implies a rate somebody could lower and a
population somebody is counting, neither of which exists.

The recorded rows are not telemetry. They are the input to E1-T9's offender
scan, which is the only thing that can move a tenant to `enforcing`, so their
shape is decided by that query rather than by what is convenient to log. Note
that metric cardinality is closed here — `contextplane/metrics.py` forbids
tenant-labelled series — so "how many tenants are still advisory" cannot be a
gauge and must be a query.

**Built as `0065_envelope_enforcement_stage` plus an enforcement service that
wraps the decision rather than changing it.** `AutonomyDecisionService` still
answers only "does the envelope authorise this"; `AutonomyEnforcementService`
answers the separate question of whether a refusal refuses, and that is the only
thing the stage touches. Keeping them apart is what lets the decision stay pure
and replayable while the stage is read per request.

**The advisory table's CHECKs encode the scan's assumptions.** `permitted` is not
an admissible verdict — a permit writes no row, so admitting the value would
describe a state the table cannot hold. And `(verdict = 'no_envelope') =
(binding_id IS NULL)` is a constraint rather than a convention, because the scan
distinguishes "ungoverned principal" from "principal acting outside a real
envelope" and a row claiming both or neither is one it would have to guess about.

**`decide()` takes the principal as a parameter, and the first version did not.**
It read `envelope.principal`, substituting a placeholder when there was no
envelope — which erased the identity the graduation pre-flight counts, on exactly
the rows it counts them from. An advisory record saying "some principal had no
envelope" answers nothing. Caught by the test asserting the recorded row's
contents rather than the outcome object's, which is the reason to assert against
the database rather than the return value.

**The safe default here is the permissive one**, which is unusual for this
service and stated in the migration for that reason: `enforcing` on an
unmigrated tenant refuses every agent that tenant runs, and a fleet-wide
availability failure is not a safer outcome than a recorded one. The restrictive
direction is reached by graduating, which E1-T9 puts a pre-flight in front of.

**A failed advisory write does not block the act.** In advisory the caller
proceeds by definition, so letting a bookkeeping insert refuse an act policy
allows would make this stage more dangerous than the one it precedes. The record
is lost, logged at `exception`, and reported through `recorded=False` rather than
inferred.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m pytest tests/integration -q -k "autonomy_enforcement"
    make lint && make test-coverage

### E1-T9 — Graduation is a pre-flight over offenders, not a flag flip

**Kind:** task · **Status:** done · **Blocked by:** E1-T8 · **Hotspot:** no · **Repo:** contextplane

Goal: the admin route that moves a tenant from `advisory` to `enforcing`, in the
shape `_run_graduation_preflight` already has
(`contextplane/api/routers/admin_progression.py:385`) over a scan in the shape of
`scan_graduation_offenders` (`contextplane/service/catalog/progression.py:236`).

Reused rather than reinvented, because ADR-0005 chose this mechanism over the
four disagreeing enforcement vocabularies that ship today precisely on the ground
that it is a working implementation of this exact transition. What it already
does and this must keep: a dry-run that reports without writing, a refusal
carrying the offender list when the scan is non-empty, a bounded scan with a
timeout, and a force path that requires a written migration plan recorded on the
audit row.

One detail the ADR flags and this task must honour: the pre-flight runs on every
write rather than only on the flip, because conditioning it on the flip would
silently discard a caller's `dry_run`.

**Built as a service, not an admin route.** The five pre-flight outcomes live in
`AutonomyGraduationService`, which raises typed errors rather than building
`Response` objects. `_run_graduation_preflight` returns HTTP because it *is* the
route; splitting them here means the scan and its refusals are testable without
a client, and the route that will map them is a thin layer whose absence blocks
nothing — nothing consults the enforcement stage from HTTP yet.

**Anti-vacuity turned out to be the harder half, and it needed its own state.**
ADR-0005 requires the pre-flight to report the population it scanned rather than
only the offenders. That is not decoration: a tenant the advisory stage never
observed produces exactly the same empty offender list as a tenant whose agents
are all correctly enveloped, so `is_clean` alone would let a graduation pass on
silence. `GraduationReport.observed_nothing` is kept separate from `is_clean` so
a caller cannot read one and get the other, and `NothingObserved` is a distinct
refusal from `GraduationBlocked` because the operator's next step differs — one
needs envelopes written, the other needs traffic.

**Only `no_envelope` blocks.** `outside_envelope`, `envelope_suspended` and
`envelope_withdrawn` are all the envelope working, and graduating changes none of
those outcomes. `no_envelope` alone is the refusal that graduation converts from
a record into an outage. The population count deliberately spans *all* verdicts,
so a tenant with only working refusals reads as observed-and-clean rather than
unobserved.

**Graduating is tenant admin; demoting needs the operator allowlist.** The same
line the envelope bindings draw, for the same reason: graduating only narrows —
every act it changes was already being recorded as a would-be refusal — while a
demotion turns every refusal a tenant is making back into a record, which hands
authority back to a principal that was being refused.

**The migration plan reaches the audit row, not just the request.** Requiring it
is pointless if it is not durable: a force nobody can review afterwards is a
force with a formality in front of it. The audit payload also carries
`offenders_at_write`, so the row records what was overridden rather than only
that something was.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "autonomy_graduation"
    make lint && make test-coverage

### E1-T10 — Cold start: three principals, and the first envelope is advisory

**Kind:** task · **Status:** done · **Blocked by:** E1-T8 · **Hotspot:** no · **Repo:** contextplane

Goal: the first envelope on a deployment, per ADR-0008 — approved by the
authority that already exists, classified for actor separation, and starting
`advisory`.

**No new authority concept.** A global envelope requires an `(issuer, subject)`
pair in `ARC_GLOBAL_OPERATOR_ALLOWLIST`; a tenant envelope requires `ROLE_ADMIN`
in the owning tenant. The ADR rejects a "named human" flag on the ground that the
schema cannot assert humanity and a column claiming to would be a comment
carrying a CHECK constraint's costs.

**`global_mandatory` is how "named human authority" becomes enforceable.** It
requires three distinct principals — three distinct `(issuer, subject)`
identities, at least one on the operator allowlist, none able to fill two roles
in the same transaction — which is the existing mechanism doing what it was built
for.

**Initial state is `advisory`, not auto-accept.** Every cold-start decision in
this repository refuses rather than guesses, and an envelope that auto-accepts on
day one is a governed object whose first act is to be ungoverned. Advisory is not
propose-only: nothing is queued, nothing waits, and no operation is refused — it
records what would have been.

**Most of this was already true, and the one missing piece was the piece that
mattered.** Nothing new was needed for authority (E1-T6a's grant already
splits allowlist from tenant admin), nor for the initial state (E1-T8's column
defaults to `advisory`), nor for three-principal approval — `risk.py`
classifies any revision carrying a global mandatory rule as `global_mandatory`,
and `activation_predicates.py` then requires three distinct identities where
everything else requires two. The mechanism shipped long ago.

What was missing was anywhere that *insisted on having got it*. A binding
now refuses a global envelope whose revision is not classified
`global_mandatory`, which is what turns "approved by a named human authority"
from a sentence into a check.

**Why the classification and not the rule shape, which would have been
cheaper.** There are two activation paths and only one enforces actor
separation. `ArtifactService.activate` is gated by `assert_can_write_artifact`
alone, so a deployment operator can register and activate a global policy
carrying mandatory rules with no separation whatsoever. Checking that a rule
is global-and-mandatory would therefore be bypassable by the one principal
most able to bypass it. The classification exists only when the revision came
through the authoring pipeline, which is where the predicate runs — so its
*absence* is the signal, and a directly-registered revision fails the same
check with "no proposal version".

**Checked after authorization, not before.** A tenant admin who may not write
a global envelope gets the authorization refusal and learns nothing about that
document's approval history.

Tenant envelopes are deliberately exempt: the bar tracks the blast radius, and
a separation ritual is what deployment-wide authority costs.

**ADR-0005's Assumption 1 resolves affirmatively, with no new plumbing.** It
flagged that the tree never says which identity an envelope binds to, and that
if the answer were "none without new plumbing" the first enforcement point
would move and the advisory stage could not start where the ADR assumes.
E1-T6a answered the first half — an `(issuer, subject)` pair — and
`api/middleware/tenant.py` answers the second: it sets
`request.state.oidc_claims` on *every* authenticated request, not only ARC
ones, which is exactly how `arc_authoring.py` builds its own context. So
`POST /v1/memory/sessions/{session_id}/events` can construct a
`WorkloadIdentity` today.

**One thing the ADR names does not exist.** It cites "the intent write routes
whose authority model is `AuthorityOrigin` in `contextplane/context/intent.py`"
as a second enforcement point. `AuthorityOrigin` appears nowhere in the tree.
The memory events route does exist and is the real first enforcement point;
whatever task wires it should not go looking for the other.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "autonomy_envelope"
    make lint && make test-coverage
