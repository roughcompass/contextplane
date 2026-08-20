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

**Kind:** epic · **Status:** pending · **Blocked by:** none · **Repo:** contextplane, contextplane-ui

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

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a relevance-judgment fixture over the existing 50 search questions —
for each, the entity ids a correct answer contains — and a report joining what
receipts say was served against those judgments, yielding precision@k
alongside the existing recall@10. Wired into `make eval`.

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

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

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

**Kind:** task · **Status:** pending · **Blocked by:** E16-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: the lineage-digest dedup exists in `claim_writer.py`; this pins the
property the epic warns about with a test that would fail if it regressed —
two extractions from one originating session event combine to the single-source
confidence, not the two-source one. A vacuity control asserts the same pair
from two distinct events does raise it, so the test cannot pass by combining
nothing.

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

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide the dual-alias window E13 assumes and E18-T4 needs, before the
first rename rather than during it. The ADR fixes how long an alias lives, how
it is marked in the contract (`deprecated: true` plus a sunset stamp, which
OpenAPI already models, so nothing new is invented), whether a deprecated alias
may differ in behaviour from its successor — it may not, or the window becomes
a second implementation — and what actually retires one. The honest constraint
to record: neither this repository nor the UI can currently see third-party
callers, so retirement cannot rest on an observed-zero-usage claim it has no
instrument for. All six MADR sections, dissent included.

Acceptance:
    test -f .develop/adr/0009-renaming-a-published-surface.md
    make doc-links && make doc-refs

### E18-T2 — One path parameter for one identifier

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** yes — openapi.json · **Repo:** contextplane

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

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** yes — openapi.json · **Repo:** contextplane

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

**Kind:** task · **Status:** pending · **Blocked by:** E18-T1 · **Hotspot:** yes — openapi.json · **Repo:** contextplane

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

**Kind:** task · **Status:** pending · **Blocked by:** E18-T2, E18-T3, E18-T4 · **Hotspot:** yes — vendored openapi.json + generated client · **Repo:** contextplane-ui

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

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Goal: adapter functions for `POST /v1/relationships`, `PATCH
/v1/relationships/{relationship_id}` and `POST /v1/relationships:query` in
`shared/api/`, plus the authoring UI that uses them — create an edge from a
capability's detail dialog and from the Relationships page, edit or retire one
from a traversal result. The repo's own contract rules carry the detail rather
than being restated here: a fresh idempotency key per create, `If-Match` from
the detail `ETag` on update, a `412` that keeps the draft and refetches, and
branching on `errors[].code` never on message text. Colocated tests cover the
create, the concurrency conflict, and the permission-denied path, because those
are the three the adapter can get wrong silently.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "relationship"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T2 — Catalog covers every entity type, in one vocabulary

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Goal: the Catalog page lists concepts and operations beside capabilities,
filterable by type, with `POST /v1/concepts` and `POST /v1/operations` wired for
creation — the service has offered both since `02a1d07` and the UI neither.
Naming follows the epic: Catalog the section, entity the thing, type the
discriminator, and the page copy that presents a capability as the only kind of
record is corrected in the same change. No new nav destination; this is the
existing page learning the rest of its domain.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "catalog"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T3 — A graph view on /relationships, beside the table

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

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

Acceptance:
    pnpm --filter admin-dashboard test -- -t "graph"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T4 — Entity resolution in global search

**Kind:** task · **Status:** pending · **Blocked by:** E18-T5 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: `GET /v1/entities:resolve` behind the shell's global search, so a handle
resolves to one entity and an ambiguous handle is presented as the refusal the
service actually returns — `identity_ambiguous`, with the qualifying types
offered as choices and never a silently picked first match, which is the
failure the endpoint was designed to refuse. Blocked on the pin bump because
the sibling lookup path moves in E18-T4, and building against the pre-rename
client would mean writing this adapter twice.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "resolve"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

