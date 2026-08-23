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

**Kind:** epic · **Status:** done · **Blocked by:** none · **Repo:** contextplane

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

**Status note — ten tasks done, one clause of this body not built.** T1–T10
cover the authority object, the binding, the matrix, the decision, the
advisory stage, graduation and cold start. What they do not cover is
*"stream-scoped action-class and sensitivity declarations at source-namespace
registration"*. E1-T6c quotes that phrase but reads it as naming the matrix's
dimensions, which drops the part that says **where** the declaration happens.

It is genuinely unbuilt. `arc_source_connectors` is the only registration
surface in the tree and it declares schemes, hosts, media types, verifiers and
a size ceiling — no action class and no sensitivity tier.

**And it cannot be built here, because its subject belongs to E2.** "Stream"
is E2's word throughout this plan — E2 speaks of "where the stream declares an
external source" and E6 of a "PII block tier for undeclared streams" — and E2
is the observation write path, which ADR-0005 established does not exist. So
the clause depends on a concept defined by an epic that is itself blocked by
this one. That circularity is the finding, not an oversight to fix silently.

The scoping call belongs to a human: either the clause moves to E2, where the
stream it scopes is defined, or E1 gains a task blocked on E2 and this epic
stays open across it. **E1 is deliberately not marked done** — a first wave is
the claimable frontier, not the scope, and closing an epic because its
decomposed tasks finished is the same error that closed E19 prematurely.

**Resolved: the second option, and the fact that made it undecidable is gone.**
The circularity above rested on E2 not existing. E2 is now done, and E2-T2 gave
"stream" a concrete referent: the `(source_system, source_namespace)` pair a
replayed session event carries. There is a stream to scope to, so the clause can
be cut against something rather than moved to keep it next to a word. E1 gains
**E1-T11** and stays open across it.

**What grounding it found, which changes what the task is for.** The clause reads
as a security requirement — stop an agent understating the sensitivity of what
it is about to touch — and on both wired paths that is already prevented, by two
different mechanisms neither of which is this clause:

- On `POST /v1/arc/context/resolve` the manifest's `data_sensitivity` and
  `requested_action_classes` are **host-attested**. `verify_attestation` takes
  the manifest, produces a `manifest_claims_digest`, and the challenge is
  validated against that digest. An agent cannot understate its sensitivity
  without the enrolled host signing the understatement.
- On the session-event write path the route *constructs* the manifest itself and
  the caller supplies neither field, so there is nothing to forge.

So the clause's stated motivation is met. What is genuinely missing is narrower
and worth naming precisely: **replayed external content has no declared
sensitivity at all**, and the envelope decision on that path therefore selects on
`intent_kind` alone. A replay from a chat export and a replay from a payroll
system are not the same handling risk, and today neither says which it is.

**Closed by E1-T11.** Every clause of the body above is accounted for, walked one
at a time rather than counted:

- *One authority object, a `policy` artifact whose applicability rules carry the
  matrix, and not `capability_contract`.* T6a and T6c, with the kind decision
  recorded rather than assumed.
- *A session `ProvenanceGrant` is a runtime projection, never a peer object.*
  Holds by absence: no such object exists, which is the right way for that clause
  to be true.
- *Instant suspend — status flip, push-invalidated, sub-second SLO.* **Amended,
  not built.** ADR-0007 struck the wall-clock number and replaced it with a bound
  on operations: a suspended envelope authorises nothing that begins after the
  flip commits, visible to the next decision on any replica because no replica
  holds a copy. A sub-second figure would have been unobservable on the shipped
  histogram, whose buckets stop at ten seconds.
- *Governed widen — full ARC pipeline.* True by construction. There is no widen
  operation; widening means binding to a different revision, and
  `_assert_envelope_revision` requires that revision to be `active`, which only
  ARC's authoring pipeline produces.
- *Identities bind to IAM workload identities.* T6a, via
  `WorkloadIdentity(issuer, subject)`.
- *Stream-scoped declarations at source-namespace registration.* T11, and
  narrower than it read — see above.
- *Owned cold start: a template envelope approved by a named authority, posture
  auto-accept-with-maximum-sampling.* T10.

One stale sentence found while auditing and deliberately not edited, because
amending an accepted decision is its own act: ADR-0007 says "the one published
numeric SLO in the repository is webhook fan-out at p95 < 30s". That was already
wrong when written — `tests/perf/test_arc_latency.py` publishes two more — and
E2-T6 added a fourth. The claim it supports, that a wall-clock revocation SLO
would be untested and unbucketed, is unaffected; the count is not.

### E2 — Hot observation write path

**Kind:** epic · **Status:** done · **Blocked by:** E1 · **Repo:** contextplane

`POST /v1/sessions/{id}/observations`. Sync: auth/tenant via the visibility
chokepoint, envelope digest check, idempotency, closed-schema + provenance
completeness (`observed_time` and `external_record_id` caller-supplied where
the stream declares an external source), PII scan per tenant policy, one
partitioned insert.

**Amended: "cheap synchronous embedding" is struck from this list**, by
ADR-0010. Session events are not an embedding target -- `EMBEDDING_TARGETS` is
`{fact, claim}` -- and no shipped reader of them wants one: replay is by `seq`
and resume by `sequence`, so the two-call loop already sees its own write. The
clause asked to introduce a target *and* implement it in the one shape that
contradicts how both existing targets work, at the cost of a model call inside
the p99 this same epic wants published. All else async with per-tenant
fairness and lag stamps. Published p99 includes the PII-block mode.

**Closed.** Every clause above is accounted for, and three of them were closed
by amendment rather than by code — which is the point of walking the list here
rather than counting six green tasks:

- *The route.* `POST /v1/sessions/{id}/observations` never existed, and neither
  did an observations table. The write path is `POST
  /v1/memory/sessions/{session_id}/events`; recorded in the third wave's
  preamble before any task was cut against the wrong surface.
- *Auth, envelope digest check.* E2-T1 — the envelope decision reaching the
  write path, ahead of admission.
- *Idempotency, closed schema, PII scan per tenant policy.* Already shipped;
  `_Strict` forbids unknown fields and `run_admission` carries the policy.
- *Provenance completeness.* E2-T2, with `observed_at` rather than
  `observed_time` and the conditional keyed per event.
- *One partitioned insert.* E2-T3, hashed on `tenant_id` rather than ranged on
  time, because ranging would have weakened the key sequence allocation
  depends on.
- *Cheap synchronous embedding.* Struck by ADR-0010 (E2-T4).
- *Async remainder with per-tenant fairness and lag stamps.* E2-T5.
- *Published p99 including block mode.* E2-T6, published as p95 with the
  arithmetic for why 40 samples cannot support a p99 — an amendment stated in
  the test rather than a bound quietly loosened until it stopped failing.

E1 stays open: one clause of its body — stream-scoped action-class and
sensitivity declarations at source-namespace registration — is unresolved, and
E2-T2 deliberately declined to pre-empt it.

### E3 — Resolve-as-receipt fused retrieval

**Kind:** epic · **Status:** done — every task closed, two amended on the record · **Blocked by:** E2, E9 ⚙ · **Repo:** contextplane

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

**Grounded before decomposition, and three of this body's clauses do not survive
contact. Read this before cutting anything from the paragraph above.**

**"Three concurrent candidate generators" — concurrency ships, and there are
four.** `assembler.assemble` runs the arms under `asyncio.gather` with a per-arm
timeout and a per-arm item cap, both applied by the assembler rather than
trusted to each arm. `BLOCK_NAMES` is four: canonical, ARC, observed claims,
workspace. So this clause asks for something that exists, at the wrong count.

**"RRF merge" is refused by the shipped design, on the record, and the refusal is
better.** `assembler.py`'s first stated property is that authority is not
flattened: "The four arms stay four blocks. Nothing merges them, re-ranks across
them, or promotes a workspace note next to a canonical answer because it scored
well. A single ranked list would be more convenient to consume and would destroy
the only signal telling a reader which claims the registry stands behind."

An RRF merge is precisely that single ranked list. Reciprocal rank fusion is a
good tool where the inputs are interchangeable retrievers over one corpus, which
is what it does inside `search.py`'s three arms and why that fusion is fine.
Here the inputs are four *authority classes*. Fusing them answers "what is most
relevant" by discarding "what does the registry stand behind", and the second is
the question this product exists to answer. **The plan changes, not the tree:
E3-T1 is an amendment striking the merge, not a task implementing it.**

Two things that would genuinely improve ranking are still open and are not this:
ordering *within* a block, and telling the caller how much of each block was cut
by the item cap.

**The synchronous receipt write stays, on a measurement.** The body splits it
into an intent row plus async hydration. That trades a guarantee `resolve.py`
states outright — availability for evidence — and the saving was never measured.
It is now: the whole resolve, "four arms, assembled, labelled and receipted, in
one synchronous call", is **p95 12.9ms against a 150ms budget**, and the write is
bounded by construction at four arm rows plus at most 200 items whatever the
corpus does. E3-T3 is amended rather than built, with the conditions that would
reopen it written into that entry.

**"The intent row MUST carry a completeness discriminator" — the premise holds
and is the strongest clause in the body.** `context_receipts` has `state` and no
`hydration_state`; an un-hydrated receipt would read as complete with zero
exclusions. Confirmed against `0032_context_receipts.py`.

But the ordering the body states must be enforced rather than assumed, because
today's code is the *safe* side of it. `resolve.py` says: "The receipt write is
not best-effort. If it fails, the resolution fails... an answer nobody can later
show they were given is the thing receipts exist to prevent." Splitting the write
into a synchronous intent row plus async hydration **relaxes exactly that**, and
the body already says the relaxation may only happen once the discriminator
exists in schema and read models. So the discriminator lands first, alone, and
the split lands second — never in one change, because a reviewer cannot see a
missing guarantee in a diff that adds a column.

**What is untouched and still true:** derivative registration staying inside the
synchronous transaction, the read surfaces never presenting a `pending` receipt
as evidence, hydration lag alerting against an SLO, trust state in the vector
index key, and the adversarial-selectivity benchmark. Those are the epic's real
content and none of them was checked against a wrong premise.

**Closed.** All eight tasks are done, two of them as amendments rather than
implementations: E3-T1 struck the RRF merge because fusing the blocks discards
the question this product exists to answer, and E3-T3 kept the synchronous
receipt write on a measurement — p95 12.9ms against a 150ms budget — with the
conditions that would reopen it written into the entry.

The two ranking improvements this body's audit named as "still open and not
this" — ordering within a block, and telling the caller how much of each block
the item cap removed — are deliberately not cut as E3 tasks. They are real and
they are a different epic's subject; recorded here so they are not mistaken for
E3 remnants.

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

**Kind:** epic · **Status:** done · **Blocked by:** none · **Repo:** contextplane

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
consuming feature turns on. Registration says a number is owned; validation
says somebody checked it predicts.

**Two clauses of this body are now stale and are amended here rather than
implemented.** Both were written before E9-T1 and E9-T2 landed.

*"The core registry shipped with the first property only, so extending its
schema with the evidence fields is part of this epic."* Done, in E9-T1. The
block ships: `status` (`validated` | `grandfathered`), `validated_by`,
`validated_on`, `method`, `result`, and `requires_validated`, with the loader
refusing an entry that declares no status, a `grandfathered` entry with no
reason, and a `validated` entry with no evidence behind the word.

*"That is encoded as a required check when the first E3/E5 task is cut."* Also
done, in E9-T2 — earlier than this sentence anticipated. `make
governed-magnitudes` runs inside `make lint`, which CI runs and the `gate`
required check covers, and it enforces `requires_validated: true ⇒ status:
validated` against the artifact.

**What both of those left behind is the part that actually gates, and this
epic still owns it.** The rule lives only in CI. `ranking.py`'s loader does not
enforce it, so a running service is *more permissive than the pipeline* — which
inverts this module's own stated posture, where an unknown id raises and an
empty registry raises at import. And `requires_validated()` has no caller
outside its tests: nothing consults it at the moment a feature would serve.

**"Before the consuming feature's flag turns on" presumed a flag mechanism that
does not exist, and should not be built for this.** The repository has exactly
one genuine feature switch (`arc_drafter_model_enabled`), and ADR-0005 rules out
env-var flags that widen authority. What it does have is the right *shape*:
`assert_drafter_decision_permits_serving` refuses to boot when a flag claims
more than a committed artifact earned. The registry is such an artifact, and the
chokepoint every consumer already passes through is the accessor — so the
activation gate is the **read**, not a flag. A magnitude flagged as requiring
validation and not yet validated cannot be read, and the three `consumed`
entries are read at import, so the service refuses to start. Same guarantee as
the drafter assertion, no new mechanism. E9-T3.

**Nothing can currently be moved from `grandfathered` to `validated`**, because
no evaluation harness exists to produce the evidence — that is E8. This is the
fail-closed ordering rather than a gap: a flag cannot be set until it can be
satisfied.

Remaining otherwise: bring each new scoring magnitude under the registry as
E15–E17 land — those belong to those epics, with this one supplying the rule —
and cover what the closure cannot (semantic ranking, UI-side reordering) by
periodic review of new ordering sites rather than a gate pretending to be
exhaustive. E9-T4 runs the first such review and, because a cadence with no
mechanism is a wish, gives the next one a trigger.

**Closed with E9-T4.** All four tasks are done and what is left belongs
elsewhere by design: E15–E17 register their own magnitudes as they land, and the
quarterly review issue carries the part no gate can. Two things this epic does
*not* claim, so nobody later reads the closure as more than it is. Automatic
detection of unregistered rankers remains unbuilt and unbuildable here, which is
a stated boundary rather than a deferral. And no magnitude in the registry is
validated — the schema, the artifact gate, the loader refusal and the coupling
rule are all in place, and every one of the seven entries is `grandfathered`,
because producing validation evidence needs an evaluation harness and that is
E8. The mechanism is complete; the evidence is not, and the ordering between
those two is deliberate.

### E6 — Tamper-evident spine + records management

**Kind:** epic · **Status:** pending · **Blocked by:** E2 · **Repo:** contextplane

Externally anchored tamper-evidence (bounded exposure window — never called
non-repudiation); retention classes; schedule-driven disposal via
crypto-shredding recorded as auditable deletion events; PII block tier for
undeclared streams.

**Grounded before decomposition. Two of these four are further along than the
body implies and one has just acquired its missing half.**

*A digest chain over receipt events ships.* `arc_receipt_events` chains and
`tests/integration/test_arc_digest_chain.py` verifies it; `audit/actions.py`
already carries a terminal action for a receipt whose chain no longer verifies.
So the internal half of tamper-evidence exists. **What does not exist is the
"externally anchored" half** -- nothing publishes a periodic digest anywhere
outside this database, so a party who can rewrite the database can rewrite the
chain and its verifier together. That is the whole content of the clause and it
is E6-T1.

*The naming constraint is load-bearing and belongs in the ADR, not a comment.*
"Bounded exposure window, never called non-repudiation" is a claim about what the
mechanism buys: anchoring every N minutes means tampering is detectable except
within the last N, and it identifies no signer. A deployment that markets this as
non-repudiation has mis-sold it.

*Retention classes already ship, and E6-T2 is rescoped around that.*
`contextplane/retention/` carries a `retention_policies` table over twelve record
classes with legal basis, four erasure modes, holds and tombstones, plus a sweep
worker. The clause reads as though none of it exists. What is genuinely missing
is one class: `session_event`, governed by a per-tenant integer and an
`expires_at` column entirely outside the framework -- the newest and
highest-volume record class is the one retention does not reach.

*Crypto-shredding is named in the tree and does not exist.* Migration 0066 cites
crypto-shredding as the reason `memory_session_events` needs no time partitions
-- "disposal by destroying the key, recorded as an auditable deletion event" --
and there is no key, no destruction and no such event. A design decision has
already been taken *on the strength of* a mechanism nobody built. That is worse
than an unbuilt feature and it is E6-T3's first paragraph.

*"PII block tier for undeclared streams" now has a stream to be undeclared.*
E1-T11 built `memory_source_namespaces`, so "undeclared" is checkable:
`sensitivity_of` returns `None`. The clause becomes a policy over that lookup
rather than a concept without a subject, which is what it was when this body was
written.

### E7 — MCP surface contract + two-call memory loop

**Kind:** epic · **Status:** pending · **Blocked by:** E1, E2 · **Repo:** contextplane

One machine-readable tool registry: default connection exposes ~6–8
envelope-derived core verbs; full surface opt-in per envelope; registry↔OpenAPI
parity gate and registry↔docs conformance gate. Two-call remember/recall with
safe defaults routing through the PII-scanned hot tier; time-to-first-memory
quickstart.

**Grounded before decomposition, and the gap is much larger than "add a
registry".**

*There is no registry, and the default surface is everything.* Tools live as
module-level functions across fifteen modules, each exposing a `register()` that
decorates its functions onto the FastMCP server, and `server.py` calls all
fifteen unconditionally. Counting the tool functions in those modules gives
roughly **120**, against the six to eight this body wants a default connection to
show. An agent connecting today is handed the entire surface, and the largest
single module is memory curation at thirty-eight.

*"Envelope-derived" is now buildable and was not when this was written.* E1
shipped the autonomy envelope, the applicability matrix, and
`enforce_envelope`; a principal's envelope can therefore decide which verbs it
sees. That makes the clause concrete: the default is the core verbs, and the rest
is opt-in by an envelope that names them.

*Half a parity gate ships.* `tests/conformance/test_memory_rest_mcp_parity.py`
asserts every memory operation exists over both surfaces and that no memory tool
takes an actor identifier. It covers memory only, and it compares operations
rather than a registry -- because there is no registry to compare against. So
E7's parity gate is an extension of something real rather than a new idea, and
it should say so rather than duplicating that file.

*The two-call loop mostly ships.* The MCP memory tools route writes through
admission -- `tools/memory.py` records that this path once called `record_event`
directly and scanned nothing, which is the defect it was fixed for. What is
missing is the *defaults*: nothing makes the safe path the one an agent gets
without asking.

### E8 — Memory-quality eval harness

**Kind:** epic · **Status:** done — except `eval_score`, blocked on procedural memory · **Blocked by:** none · **Repo:** contextplane

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

**Audited clause by clause after its three tasks finished, and it does not
close.** Two of the four things this body says remain are still remaining:

- *Extraction precision/recall per predicate* — E8-T1.
- *Retrieval relevance judged against receipts* — E8-T2.
- *Multi-session recall* — **nothing.** No fixture, no report, no test. It is
  not blocked on anything: extracted claims are the cross-session carrier, they
  are already an embedding target, and whether a claim extracted in one session
  is retrievable from another is measurable today. E8-T4.
- *`eval_score`, an empirical pass rate on a held-out replay suite* — **nothing,
  and this one is genuinely blocked.** The string `eval_score` appears nowhere
  in the tree outside this plan. E8-T3 built the Wilson bound the gate will
  rest on and said plainly it had no consumer, which was right. But the body's
  claim that "the harness does not [wait on procedural memory], and comes
  first" only half holds: the *arithmetic* could come first and did; the
  *suite* cannot, because a held-out replay suite is held out from mining, and
  nothing mines procedures. There is no artifact to score. That is a correction
  to this body, not a task somebody forgot.

So E8 stays open on E8-T4, and its last clause stays blocked on procedural
memory with the reason written down rather than rediscovered.

**Closed on everything buildable.** E8-T4 shipped the multi-session recall
measurement this body's audit found missing.

`eval_score` stays blocked, and the reason is recorded rather than left to be
rediscovered: a held-out replay suite is held out *from mining*, and nothing
mines procedures, so there is no artifact to score. E8-T3 built the Wilson bound
the gate will rest on and said plainly it had no consumer, which was the right
order. This reopens when procedural memory exists — not before, because a pass
rate over an empty suite is a number with no referent.

### E10 — UI/IA workstream

**Kind:** epic · **Status:** done — all thirteen tasks closed · **Blocked by:** E5 (screens), none (bug fixes) · **Repo:** contextplane, contextplane-ui

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

**Closed.** Thirteen tasks: the cockpit dispositions and quarantine screens, the
navigation and DESIGN repositioning, the PII and ARC operations off the raw
console, the validator convergence across nine adapters, and canon copy last —
which is the order this body set, and the reason it set it. Copy written before
the screens describes an intention; the one defect canon copy found was a claim
about the audit log that four earlier passes had read past.

Two things this epic surfaced that outlive it. **E14-T1** — thirteen of fourteen
ARC admin paths are write-only — was found by building E10-T7 and shaped four
screens; three of them ship saying so on the page rather than papering over it.
And the ARC exception grant form turned out to transcribe an approval rather
than make one, which is a property of the contract no entry here had noticed.

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

**Kind:** epic · **Status:** done — except the deprecation clause, blocked on a usage corpus · **Blocked by:** E2, E3, E7 · **Repo:** contextplane

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

**Metric status, after decomposition measured all three.** Default-profile tool
count is **already met at exactly 8** — E7-T1's registry records `core_count: 8`
against `tool_count: 67`, so E13 keeps it shrunk rather than shrinking it. The
REST target is **8 against ≤ 6**, two over, with both candidate pairs nameable
(E13-T1). Deprecated-surface count is **blocked, deliberately and on the
record** (E13-T3): there is no usage corpus, because nothing has been released,
and retiring a tool on absence of evidence is refused. The dual-alias window is
**struck** — this is a greenfield repository with no external consumers, so
surfaces that consolidate are replaced, not aliased.

**Closed on what the metrics allow.** Default-profile tool count was already
met at exactly 8, so this epic keeps it shrunk rather than shrinking it. The REST
target was 8 against ≤ 6 and E13-T1 named both candidate pairs. The dual-alias
window is struck: greenfield repository, no external consumers, so surfaces that
consolidate are replaced rather than aliased.

Deprecated-surface count stays blocked, deliberately and on the record (E13-T3):
there is no usage corpus because nothing has been released, and retiring a tool
on absence of evidence is refused. That is a precondition, not a task somebody
skipped.

### E15 — Salience: deciding what is worth keeping

**Kind:** epic · **Status:** done — except the retention threshold, which needs observed volume · **Blocked by:** none · **Repo:** contextplane, contextplane-ui

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

**Audited clause by clause after its tasks finished, and it does not close.**

**"No field named bare `score` survives the change" is false, in the place it
matters most.** `SearchResultItem` on the wire still has one, and
`api/routers/_common.py` populates it with `score=result.fused_rank_score` —
the field is renamed internally for a stated reason and renamed straight back at
the API boundary. That is worse than not having renamed it: an internal name and
a wire name now disagree about the same number, and the wire name is the one a
UI author reads. E15-T1's acceptance was `! grep "score: float"
contextplane/types.py`, which is narrower than its own goal sentence and passed
while the contract kept the field. E15-T6.

**The retention threshold does not exist, and deliberately is not being cut as a
task.** E15-T5 built the label data a threshold would be chosen from — retrieval
rate per salience bucket, with a Brier score beside it — which was the right
order. Choosing the operating point needs observed volume that no development
tree has. Unlike the validation refusal in E9, a wrong threshold here fails
*open* in the destructive direction: it discards memories. So this is recorded as
the next step with its precondition named, rather than built against no data.

Everything else holds: the six signals are computed at write with novelty the
only one that is not (T3), the weights and now their saturation ceilings are
governed magnitudes (T4, E9-T4), the reliability diagram and Brier score ship
(T5), and learned weights stay out of scope with the missing citation-to-outcome
join as the stated reason.

**Closed, including the audit finding this body raised against itself.**
E15-T6 and E15-T7 carried the rename through to the wire and its contract pin,
so `SearchResultItem` now says `fused_rank_score` and no bare `score` survives —
which is what the original clause claimed and did not deliver.

The retention threshold remains deliberately uncut. Choosing a precision/recall
operating point needs observed volume no development tree has, and unlike the
validation refusal in E9 a wrong threshold here fails *open in the destructive
direction*: it discards memories. E15-T5 built the label data it would be chosen
from, which was the right order to stop at.

### E16 — Truth confidence: corroboration and measured volatility

**Kind:** epic · **Status:** done · **Blocked by:** none · **Repo:** contextplane

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

**Closed, and the only one of four audited together that could be.** Every
clause was checked against the tree rather than against its task list:
`confidence.py` computes `1 - Π(1 - pᵢ)` and the `headroom`/`scale` knobs are
gone from the file, not merely unused (T1); same-session corroboration counts
once (T2); `confidence_decay.py` reads a predicate's measured rate where twenty
supersessions in a year support one and falls back to the category otherwise,
with the first fit inspected before it may select (T3, T4); and confidence never
meets salience anywhere — the two are computed in modules that do not import
each other, which is a stronger guarantee than a rule nobody enforces.

### E17 — Tenant-scoped scoring configuration

**Kind:** epic · **Status:** done · **Blocked by:** E15 · **Repo:** contextplane

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

**Audited after its three tasks finished, and it is the worst of the four: the
epic's central property is not true.** This body requires that "every consumer
must resolve through one accessor, because that accessor is where tenant
resolution happens and a consumer reading the registry directly silently ignores
overrides". `profile/scoring.py` says the same thing about itself, at length,
and calls it "the failure this module exists to make impossible to write by
accident".

`resolve_weights` has **no production caller.** Only its own unit tests. Every
scoring consumer in the tree reads `ranking.weights(...)` directly —
`claim_serving.py` at import, so it could not be tenant-scoped even in
principle; `search.py` and `salience.py` inside functions, so they could be.

What ships is therefore a complete override lifecycle a tenant can publish,
validate, activate and roll back, whose result nothing reads. A tenant that
configures its own scoring gets the core values and no indication otherwise —
which this body already names as indistinguishable from an override that failed,
at every layer above. E17-T4.

This is the same shape as the `requires_validated` field E9-T3 had to give teeth:
a governance object nothing consults governs nothing. Twice in one audit is
enough to make it a thing to check for rather than a thing to notice.

**Closed by E17-T4.** All three scoring consumers now resolve through the
accessor, `scripts/check_scoring_accessor.py` refuses a direct `ranking.weights`
read outside it, and `salience.combine` takes the resolved map as a required
argument so a caller with no tenant cannot call it at all.

Two clause notes so the closure is not read as more than it is. The blocker on
E15 was about ordering -- this epic needed salience weights to exist, which
E15-T4 delivered -- and not about E15 finishing; E15 remains open on E15-T6, and
a bare `score` on the wire has nothing to do with tenant scoring. And "no
environment variable and no `Settings` field may set any of these" is **true
today and unenforced**: `config.py` has no scoring field, nothing stops one
being added, and a name-matching check would be fragile enough to be worse than
the honest note. Recorded rather than half-built, the same call this plan made
about `CONTRADICTION_PENALTY`.

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

**Kind:** epic · **Status:** done · **Blocked by:** none · **Repo:** contextplane-ui

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

**The audit, run again after E19-T7, surface by surface.** Every surface this
body names now has both halves:

| Surface | Adapter | Caller |
|---|---|---|
| `POST /v1/relationships` | E19-T1a | Relationship authoring dialog |
| `PATCH /v1/relationships/{id}` | E19-T1a | Same dialog, supersede path |
| `POST /v1/relationships:query` | E19-T1a | Entity detail, Connections panel (T7) |
| `POST /v1/entities` | E19-T6 | Catalog create, governed route |
| `PATCH /v1/entities/{id}` | E19-T6, removed, restored in T7 | Entity detail, governed edit |
| `GET /v1/entities:resolve` | E19-T4 | Entity handle resolution in the shell |
| `/v1/concepts`, `/v1/operations` | E19-T6 | Catalog create, direct route |

The other body claims: the Catalog page names every entity type it lists (T2),
the graph sits on `/relationships` beside the table rather than replacing the
list (T3), and the Catalog/entity/type vocabulary is what the UI uses.

**Still open, and this epic stays open with it.** E19-T7's audit turned up a
defect this body does not name: the catalog write path sends no `If-Match`,
against a contract that honours it on thirteen mutations and a convention that
requires it. That is E19-T8. Closing here on the strength of the table above
would repeat exactly what #34 did -- treat a finished frontier as a finished
scope -- and #35 reverted that once already.

**One more entry for the list of premises that did not survive.** E19-T7 itself
made the epic's characteristic mistake: it substituted `POST /v1/entities` for
the `PATCH /v1/entities/{id}` the task named, on the assumption that a subject id
in the body would route the write. It does not -- the service reads the target
from the path -- so the governed edit would have created a second entity on the
approval route. Six of eight tasks now, and the one that failed was written
*after* this paragraph was, which is the part worth sitting with: knowing the
failure mode by name did not prevent it. What caught it was reading the handler;
what let it ship was a test that asserted body and method and not path.

**Closed by E19-T8.** The surface table above holds, and the one defect the audit
found beyond this body -- no `If-Match` on the catalog write path -- is fixed:
the read keeps its validator, the three patches that honour a precondition send
it, and a `412` keeps the operator's draft and shows the newer state beside it
rather than discarding either.

Eight tasks, and **six had a premise that did not survive contact with the
tree**. That ratio is this epic's real output. It is also why closing it on a
finished task list was wrong the first time: the list said nothing about whether
the body was satisfied, and only walking the body surface by surface did.

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

Landed differently from the third acceptance line below, which was written
against a wiring shape that was not used and would fail if run: the string
`governed-magnitudes` does not appear in `ci.yml`. It does not need to. `make
lint` invokes the target, CI runs `make lint`, and the `gate` required check
covers that job — so the check is required, by one wiring rather than two. The
line is corrected rather than the wiring, because a stale acceptance criterion
reads to the next person as a regression.

Acceptance:
    make governed-magnitudes
    .venv/bin/python -m pytest tests/unit/test_check_governed_magnitudes.py -q
    make lint   # the target runs inside it; CI runs `make lint` under `gate`

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

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

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

**Landed in two PRs, because the first one was wrong in the way this epic keeps
warning about.**

The read half went where the task said: the entity detail dialog's Connections
panel, which is renamed from "Adoption & subscriptions" because it now lists
edges too and an operator asking what a thing is connected to should not need to
know that adoptions and relationships live under different words. The four
governed facts -- profile revision, validation verdict, readiness, asserting
system -- lead each row, because they are exactly what the traversal views
cannot show. `has_more` is stated rather than paged: a second pagination model
inside a dialog that has none would be worse than saying the list is partial.

The edit half shipped against the wrong endpoint. This task says the fix is
bringing back the generic twin removed in T6; instead the first attempt sent the
edit to `POST /v1/entities` with `identity.subject_id` naming the row. **The
service does not read the write target from there.** `_routed_write` takes it
from the path, and `create_entity` calls it with `entity_id=None`, so on the
approval route it falls through to `catalog.create_entity` -- an operator
editing a capability would have minted a second one. `PATCH
/v1/entities/{entity_id}` is the surface, and the contract says so: "the same
three routes a create takes, adding nothing to it but the subject".

**Why the tests did not catch it, which is the reusable part.** They asserted
the request body and the method, and both were correct; nothing asserted the
*path*. A call to the wrong endpoint carrying a right body was indistinguishable
from a right call. That is the failure this epic's body already names about five
earlier tasks -- "no test asserted the effect... the tree agreed with the plan by
not looking" -- reproduced one task later. The guard added afterwards refuses any
call to `/v1/entities` from the edit path, and was verified by reverting the fix.

The lesson generalises past this task: **for an adapter, the endpoint is part of
the behaviour.** A test that pins body and method and not path is checking the
half that was never in doubt.

Also found here and deliberately left: `updateCapability` sends no `If-Match`,
though the contract honours it and this workspace's UI conventions require it
for updates. Fixing it means the 412 flow -- retain the draft, refetch, ask the
operator to review the newer state -- which is its own change with its own error
handling. E19-T8.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "governed"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E19-T8 — The catalog write path sends no `If-Match`

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Found while building E19-T7 and deliberately left out of it, because doing it
properly is a different change with its own error handling.

Thirteen mutations in the contract honour `If-Match`, and exactly one adapter
sends it: `updateRelationship`, which E19-T1b built along with the refusal code
a stale precondition comes back with. Every catalog write goes without --
`updateCapability`, `setCapabilityVisibility`, `changeCapabilityLifecycle`, and
the concept and operation patches. The contract's own wording for the capability
patch is that an absent precondition "logs a warning and accepts the write", so
today the dashboard is the caller generating those warnings.

The workspace's UI conventions state the rule outright: preserve detail-response
`ETag` values, send `If-Match` for updates and deletes, and on `412` retain the
draft, refetch, and ask the user to review the newer state.

**Half the work is already done and that is worth knowing before cutting this.**
`client.ts` already returns `{ etag, value }` from a request with headers, so the
transport can produce it; `catalog.ts` discards it. So this is not "add ETag
support" -- it is threading a value the client already has through the catalog
adapters and the panel that holds the draft.

**The half that is not mechanical is the 412.** A refusal has to keep the
operator's edited JSON, refetch the entity, and show what changed underneath
them -- not silently overwrite and not discard the draft. The relationship
authoring dialog already solved this shape once and should be read before this
is built rather than a second answer invented.

Scope is the catalog write path. The admin, subscription and external-id patches
honour `If-Match` too and are the same defect, but they belong to whichever epic
owns those surfaces; naming them here rather than fixing them keeps this task
reviewable and stops the next reader believing the sweep was exhaustive.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "If-Match"
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

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

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

## Task decomposition — third wave (E2, now that the envelope has a shape)

Tasks for **E2 only**. The second wave said E2–E13 become cuttable when E1-T6
and E1-T7 land, because until then their contracts would embed a guess about an
object with no shape. Both have landed, so E2 is cuttable — and E2 is the one
worth cutting first, because six of the remaining epics are blocked on it.

**E2's stated subject does not exist, and this is the sixth task set in this
plan where that has been true.** The epic opens with
`POST /v1/sessions/{id}/observations`. There is no such route and no
observations table: the `arc_observation_*` tables are ARC's shadow-observation
machinery, which watches what activating a revision would do to the corpus, and
has nothing to do with an agent writing what it saw. ADR-0005 already recorded
this in passing when choosing enforcement points; it is stated here as the thing
the decomposition is actually about.

**The real surface is `POST /v1/memory/sessions/{session_id}/events`**
(`api/routers/memory.py:156`) writing `memory_session_events`. So E2 is a
hardening of a shipped write path, not a greenfield one — which changes what
every task below is allowed to assume, and means the tasks must say what already
holds rather than re-specifying it.

**Measured against E2's own sync list, before any task was written:**

| E2 asks for | State in the tree |
|---|---|
| auth/tenant via the visibility chokepoint | present — `get_tenant_context` |
| PII scan per tenant policy | present — `run_admission` before the write |
| idempotency | present — sequence allocation retried on unique violation |
| envelope digest check | **absent** — nothing consults E1's object |
| `observed_time`, `external_record_id` | **absent** — neither column exists |
| one partitioned insert | **absent** — `relkind` is `r`, a plain table |
| cheap *synchronous* embedding | **inverted** — embedding is async, via `embedding_drain.py` |
| per-tenant fairness, lag stamps | **absent** — no fairness primitive anywhere in the tree |

Three of those are ordinary build work. One is a decision that contradicts a
shipped design and therefore takes an ADR first, the way E1's four did.

**Standing rule for every decomposition from here: where the tree is
architecturally better than the plan, the plan changes.** This document was
written before most of the code existed, so a clause of an epic is a statement
of intent, not a finding. Six task sets so far have hit a premise that did not
survive contact with the tree, and the reflex those near-misses train is to
treat the epic as authoritative and the code as behind. That reflex is wrong in
one specific direction, and it is the expensive one: a task that "implements the
spec" by replacing a better shipped design with a worse specified one is a
regression that every gate will pass, because the tests get rewritten to match.

Concretely, when a task and the tree disagree:

- Say which is better *on the evidence*, and put the evidence in the task.
- If the tree wins, the task's deliverable is an amendment to the epic, not a
  change to the code. Record it here so the next reader sees the epic's sentence
  and its correction together.
- If the epic wins, say what the shipped design got wrong, because a reversal
  nobody can justify later gets reversed again.
- Where it is genuinely open, that is an ADR, and the ADR names the incumbent as
  the default outcome. Moving off a working design is the change, and the change
  is what carries the burden.

E2-T4 is the live instance: a first draft of it asked what would happen *when*
embedding became synchronous, which had already conceded the question.

### E2-T1 — The envelope decision reaches a write path

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: `AutonomyEnforcementService.evaluate` consulted by
`POST /v1/memory/sessions/{session_id}/events`, refusing in `enforcing` and
recording in `advisory`.

**Everything E1 built is unreachable until this lands.** The decision, the
advisory records and the graduation pre-flight are all tested and all
uncalled — a governance object nothing consults governs nothing, and the
graduation scan in particular cannot observe a population that never produces a
record.

**The plumbing exists and was checked.** `api/middleware/tenant.py:130` sets
`request.state.oidc_claims` on every authenticated request, not only ARC ones,
which is how `arc_authoring.py:85` builds its own context — so this route can
construct an `ArcRequestContext` and hence a `WorkloadIdentity`. It already
takes `request: Request`. That resolves ADR-0005's Assumption 1 affirmatively:
no new plumbing.

**Do not go looking for `AuthorityOrigin`.** ADR-0005 names a second enforcement
point as "the intent write routes whose authority model is `AuthorityOrigin` in
`contextplane/context/intent.py`". That symbol exists nowhere in the tree. The
memory events route is the one real enforcement point the ADR names, and this
task is only that one.

**What it took, beyond the call site.** The three autonomy services existed but
were reachable from nowhere: not exported from `contextplane.arc`'s front door,
not built in `build_arc_services`, not fields on the typed container. So the
task was mostly composition -- export, construct, type -- and the actual gate is
one `await` in `record_event`.

**The gate is a `Depends`-free adapter in `api/envelope_guard.py`, shaped after
`api/pii_guard.py`.** A route carrying both now reads the same way twice, and
the second gate did not invent a third pattern for "check something before the
write".

**`arc_envelope_enforcement` is not `| None` on the container**, unlike
`memory`. A deployment may legitimately not run session memory; none may
legitimately run ungoverned. Typing it as always present makes a missing
dependency an import-time failure rather than a write that quietly skipped its
authority check.

**Authority runs before the PII scan.** A principal with no authority to write
should be told that, rather than having its body scanned first and refused on
content it was never entitled to submit. Ordering is only observable when both
would refuse, so there is a test that sends a body tripping admission and
asserts the envelope refusal wins.

**The refusal names the verdict, not the envelope.** `envelope_absent` and
`envelope_excluded` are different tickets -- one is an operator's job, the other
is the agent doing something it should not -- but the message says nothing about
the revision, the rule or the matrix, because a caller that learns *why* it is
outside its envelope can map that envelope one probe at a time. A test asserts
the absence of those words rather than trusting the message to stay terse.

Four of the five tests fail with the call site removed, which is what says they
test the gate rather than the route.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "memory_events_envelope_gate"
    make lint && make typecheck && make test-coverage

### E2-T2 — Provenance completeness: when the caller must say when and which

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: `observed_time` and `external_record_id` on `memory_session_events`,
required exactly when the stream declares an external source.

**Neither column exists**, so this is additive. The conditional requiredness is
the substance: an event a caller observed at some upstream time is not the same
as one this service received then, and without the distinction every
bitemporal read over this table is reading receipt time and calling it
observation time.

**This task inherits E1's unresolved clause and cannot settle it alone.** E1's
body asks for "stream-scoped action-class and sensitivity declarations at
source-namespace registration", and *what declares an external source* is
exactly that missing registration surface. `arc_source_connectors` is the only
registration table in the tree and carries no such declaration. So either this
task also builds the declaration site, or the conditional collapses to
"required whenever the caller supplies a source", which is weaker and should be
chosen deliberately rather than by default.

**The tree already had a provenance vocabulary, and it is better than E2's.**
`assertion_provenance` carries `source_system`, `source_namespace`,
`external_record_id`, three distinct clocks (`event_time`, `observed_at`,
`ingested_at`), an `authority` enum, freshness and revocation. So the columns
are named `observed_at`, not E2's `observed_time` -- two shipped tables spell it
that way and one does not, and a fourth spelling in a fifth table makes the next
reader ask whether the difference means something.

**One upstream clock, not three, and that is a decision.** For a conversational
turn replayed from an external system, `event_time` and `observed_at` collapse:
a chat message's timestamp is both when it was said and when the exporter saw
it. `created_at` already plays `ingested_at`. A column the source cannot
distinguish would hold a duplicate value with no rule for when it differs.

**Not `assertion_provenance` reused, deliberately.** Its three inbound foreign
keys are entity attributes, relationship metadata and ownership assignments --
each "somebody claimed X about Y". A conversational turn is not a claim about an
entity, and a foreign key would make that category error load-bearing.

**The conditional resolved to the weaker of the two answers, and this is the
scoping call E1 left open.** E1 wants declarations "at source-namespace
registration"; that surface does not exist. Rather than build a registry whose
only consumer is one CHECK, an event that names a `source_system` *is* an event
declaring an external origin. What that gives up: a namespace cannot state once
that all its events carry external identity, so the guarantee is per row and a
caller can be inconsistent across a stream. What it avoids: pre-empting a
decision that belongs to whoever resolves E1's clause. If that registry appears,
the CHECK narrows to reference it and no data migrates.

**Threading it through surfaced an interaction worth more than the columns.**
`record_event` allocates `seq` by inserting and retrying on unique violation.
A second unique key on the table means a duplicate upstream record raises the
*same* SQLSTATE as a lost sequence race -- so the retry would burn all its
attempts on a deterministic collision and then report a sequence-allocation
failure for what is an idempotent replay. The discriminator matches on the key
column rather than the index name, because on a hash-partitioned table Postgres
reports the *child* index whose generated name depends on which partition the
tenant hashed to. The parent's name never appears in the error, which is worth
knowing anywhere else that branches on a constraint name.

**"Caller-supplied" needed the HTTP surface, so the contract moved.** A first
pass stopped at the migration and the service, which left four columns that no
caller outside Python could write -- the same defect the envelope gate's own
docstring names about governance objects nothing consults. `POST
/v1/memory/sessions/{id}/events` now takes an optional `external` object and
`openapi.json` is regenerated; the addition is a new optional field and a new
schema, so the contract change is backward compatible and the UI pin can follow
on its own schedule rather than atomically.

Nested rather than four sibling fields, so the incomplete state is
unrepresentable: request validation refuses a partial origin where the table's
two CHECKs would otherwise surface as a 500 on a well-formed-looking request.
`_translate` gained `ConflictError -> 409` for the same reason -- untranslated,
a duplicate replay read as a service fault on what is the dedup working.

**The read surface is deliberately not built.** `SessionEvent` does not carry the
origin and `EventResponse` does not return it, so provenance is write-only today
and verifiable only by the 409 or by SQL. The consumer that would justify it --
replay reconciliation asking "which upstream records do you already have" --
is not stated by any epic here, and ADR-0010 declined to build a read path
speculatively for exactly this reason. Adding it later is four columns on a
SELECT and two optional response fields, with no migration.

**The MCP `record_session_event` tool is unchanged**, so an agent cannot declare
an external origin and an ingestion client can. That asymmetry is intended: a
replay is an ingestion act, and the MCP surface is where an agent writes its own
turns.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m pytest tests/integration -q -k "memory_session_events or memory_rest"
    make all

### E2-T3 — `memory_session_events` is partitioned

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: the events table partitioned, so retention and disposal are a detach
rather than a delete.

**It is a plain table today** (`relkind = 'r'`), and E2's "one partitioned
insert" assumes otherwise. The precedent to follow is `audit_log`: range
partitioning by timestamp with `_monthly_partition_bounds` generating a fixed,
deterministic set at migration time, deliberately not `date.today()` — a
partition set that depends on when the migration ran names its children
differently in every environment.

**Converting a populated table is the whole difficulty**, and the tree has done
it once: `scripts/partition_migrate.py` plus the `audit_log_new` shadow table in
the baseline. That is the shape to reuse, and the reason this is its own task
rather than a clause of another.

**Hashed on `tenant_id`, not ranged on `created_at`, and this task nearly went
the other way.** The `audit_log` precedent above is range-by-time, and following
it would have broken the invariant sequence allocation depends on. Postgres
requires the partition key to appear in every unique key, so
`uq_mse_session_seq (tenant_id, actor_id, session_id, seq)` would have become
`(..., seq, created_at)` -- a strictly weaker constraint under which a session
straddling a partition boundary can hold two events with the same `seq`.
Sessions crossing a month boundary are ordinary, `seq` is what replay orders by,
and `session_events.py` allocates the next one by inserting and retrying on
unique violation. So the failures are duplicate turns in a replayed conversation
and a retry loop that stops converging, neither of which raises anything.

Hashing on `tenant_id` costs nothing and keeps everything: it is already the
leading column of that unique key, so the partition key adds no column to it,
and it leads `ix_mse_replay` and `ix_mse_listing` too, so both hot reads prune to
one partition where a time range would fan out -- neither read is bounded by
time. It is also the shipped precedent rather than an invention: `embeddings` is
`PARTITION BY HASH (tenant_id)` with the same primary-key shape.

**And the reason to reach for time partitioning turns out not to apply.** The
paragraph below assumed E6's disposal is a partition detach. It is
crypto-shredding -- disposal by destroying the key, recorded as an auditable
deletion event -- which operates on content rather than physical layout. A
partition scheme chosen for a mechanism this service does not use would have
cost the invariant and bought nothing. That correction is the standing rule
again: the plan's assumption about E6 was wrong, so the plan changed.

Rebuilt rather than converted, because the table is empty in every deployment
that will run this and has no inbound foreign keys. The `audit_log_new` shadow
route stays the answer for a *populated* table.

`ix_mse_expiry` now fans out across partitions. Accepted deliberately: it is a
background sweep with no latency budget, and paying there so the two foreground
reads prune is the right side of that trade.

**The rebuild dropped eight CHECKs on the first attempt, and that is the part
worth remembering.** Postgres has no `ALTER TABLE ... PARTITION BY`, so the
migration drops and recreates -- and the first DDL was written by hand from the
column list, losing every CHECK plus `seq`'s width (`BIGINT`, not `INTEGER`) and
`expires_at`'s NOT NULL. Two behavioural tests caught the tokenizer pair; the
other six would have gone unnoticed until a bad row reached production. The
second attempt derives the DDL from the baseline's own definition and changes
only the primary key and the `PARTITION BY` clause, and the schema was then
diffed column-by-column and constraint-by-constraint against a pre-migration
snapshot rather than re-read.

`tests/conformance/test_session_events_partitioning.py` pins the shape, and
three of its four shape assertions fail against the unpartitioned table. The one that
matters asserts `uq_mse_session_seq` still holds exactly four columns, because
a fifth is the silent regression this whole task is about.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m pytest tests/conformance -q -k "session_events_partitioning"
    make lint && make test-coverage

### E2-T4 — ADR: what the two-call loop needs from a just-written event

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide read-after-write visibility for a freshly written event. **The
shipped asynchronous drain is the incumbent and the default outcome; the ADR
has to justify moving off it, not justify keeping it.**

E2 says "cheap synchronous embedding" on the write path. The tree embeds
**asynchronously** -- `service/retrieval/embedding_drain.py` writes `embeddings`
keyed `(target_type, target_id)` into an already hash-partitioned table. An
earlier draft of this task asked what would happen *when* the path went
synchronous, which quietly assumed the epic beats the implementation. It does
not get to: a plan sentence is a statement of intent written before the code
existed, and where the code is architecturally better the sentence is what
changes.

**On the evidence so far the incumbent looks stronger, and the epic looks
self-contradictory.** A model call on the hot path sits inside the latency
budget of the same p99 E2-T6 wants published. A provider outage forces a choice
between refusing writes -- availability loss on a memory write path -- and
write-then-enqueue, which is the async design with extra steps. Re-embedding
after a model change needs a drain regardless, so synchronous embedding does not
retire the drain; it adds a second path into one table and a reader that cannot
tell which produced a row.

**So the real question is not sync-versus-async.** It is what E7's two-call
memory loop sees when it resolves immediately after a write, which is the only
thing synchronous embedding would actually buy. The ADR should answer that
directly, and the cheaper answers deserve to be ruled out before the expensive
one is adopted: `embeddings` already carries a `ts_vector`, so a lexical arm can
cover a row the vector arm has not reached yet; a bounded staleness window with
a fail-closed read is the shape ARC source-status already uses; and a "pending
embedding" discriminator lets a reader distinguish *not yet* from *never*, which
is E3's own complaint about un-hydrated receipts.

If synchronous embedding survives that, it is the right answer and the ADR says
so with the numbers. If it does not, **the ADR amends E2's body** rather than
the code being bent to match it.

Six of the ten values E1 needed came from four ADRs written first, and the ones
that went wrong went wrong where a task assumed instead. This is the same shape,
with the addition that the thing not to assume here is the epic.

**Concluded: keep the drain, strike the clause.** Three findings decided it,
and the first was not anticipated when this task was written.

`EMBEDDING_TARGETS` is `{fact, claim}` -- **a session event is not an embedding
target at all.** So the clause was not asking to move an existing embedding onto
the hot path; it was asking to add a new target and simultaneously implement it
in the shape that contradicts both existing ones.

**No shipped reader wants the property it would buy.** Replay orders by `seq`
and resume by `sequence`, so a caller that writes a turn and replays the session
already sees its own write -- the monotonic column does it, not a vector.

**And the incumbent is hardened by an outage, not merely shipped.** The drain
carries `embedding_outbox_oldest_pending_seconds` beside queue depth because
depth alone cannot tell a queue that is keeping up from one nothing is
enqueueing, "the failure that hid an empty claim index for a whole phase". A
second writer into `embeddings` would break the coverage gauge that makes the
first trustworthy.

The deliverable was therefore an amendment to E2's body rather than code, which
is the standing rule working as intended: this is the first task in the plan
whose output is the epic changing.

Acceptance:
    test -f .develop/adr/0010-read-after-write-embedding.md
    make doc-links && make doc-refs

### E2-T5 — Per-tenant fairness and lag stamps for the async remainder

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

*(Unblocked: ADR-0010 struck the synchronous-embedding clause, so what stays
asynchronous is no longer an open question.)*

Goal: the work E2 moves off the hot path, scheduled so one tenant cannot starve
another, and stamped so lag is observable.

**There is no fairness primitive anywhere in the tree** — no weighted queue, no
round-robin scheduler, no per-tenant budget. Every existing worker drains in
insertion order, which is precisely the starvation E2 names. So this task
introduces a mechanism rather than applying one, and its first job is to say
what "fair" means here: equal share per tenant, share proportional to some
entitlement, or bounded worst-case latency regardless of share.

**Lag cannot be a tenant-labelled metric.** `contextplane/metrics.py` forbids
tenant labels, so "how far behind is tenant X" has to be a column and a query,
the same conclusion E1-T8 reached for the advisory records.

**The async remainder turned out to be one queue, and it already existed.**
`memory_extraction_outbox`, drained by `workers/extraction_drain.py`. So this
was not "move work off the hot path and then schedule it" but "the work is
already off the path and nothing schedules it fairly".

**FIFO here was a default, not a decision, and that is what made this build work
rather than an amendment.** The drain claimed with `ORDER BY enqueued_at LIMIT
:lim FOR UPDATE SKIP LOCKED`. Its docstring defends per-row isolation at
length -- "a provider that times out on one session must not stall the twenty
behind it" -- and says nothing at all about ordering or tenants. Contrast
ADR-0010's embedding drain, where the shipped shape *was* a considered design
hardened by an outage; here the question had simply never been asked.

**Fair means bounded head-of-line, and that definition came from the module's
own stated value.** Rows are ranked oldest-first within each tenant and the
batch is filled by rank, so every tenant's oldest window is taken before any
tenant's second. The worst case one tenant can impose on another drops from
"until my backlog clears" to one row per tick. That is the drain's existing
isolation principle applied one grain coarser, rather than a new principle
imported from outside.

Weighted shares and latency targets were both rejected as inventing
configuration -- an entitlement concept and a deadline column respectively --
to solve what ordering already solves. If a tenant later needs a *larger* share
than its peers, that is when a weight earns its place.

Postgres refuses `FOR UPDATE` alongside `row_number()`, so the rank is computed
in a CTE and the lock taken on the join back.

**The lag half was a blind spot the tree had already paid for elsewhere.** The
drain carried a depth gauge and no age gauge -- exactly the gap the embedding
drain's own comment records: "depth alone cannot distinguish a queue that is
short because it is keeping up from one that is short because nothing is being
enqueued", the failure that hid an empty claim index for a whole phase. Added,
unlabelled, because `metrics.py` forbids tenant labels; per-tenant lag stays a
query, the same conclusion E1-T8 reached for the advisory records.

The starvation test fails against the old FIFO claim. Its companion -- oldest
window first *within* one tenant -- passes under both, and that is the point of
it: it guards the fairness ordering from becoming arbitrary ordering, since
extraction reads a `from_seq`/`through_seq` range and processing a later window
first would stage claims out of conversational order.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "extraction_drain"
    make lint && make test-coverage

### E2-T6 — The published p99, including the PII-block mode

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a p99 for the write path that is asserted, not stated.

E2 says "published p99 includes the PII-block mode", which is the interesting
half: the blocking path does more work than the passing one, so a p99 measured
only on clean writes describes the case that was never in doubt.

**The one published numeric SLO in this repository is webhook fan-out at
p95 < 30s, and it is asserted by a perf test.** That is the bar: a number with a
test behind it, not a sentence. The latency histogram's buckets top out at ten
seconds, so any figure outside that range is unmeasurable on what ships and must
not be published — the same constraint that made E1-T7's suspension SLO a bound
on operations rather than on wall-clock.

Unblocked. T1 has landed and ADR-0010 removed the model call, so the path being
measured is now the path that will ship -- which was the only reason to wait.

**Landed as p95, not p99, and the reason is arithmetic rather than preference.**
The harness takes 40 samples, following `tests/perf/test_arc_latency.py`. The
99th percentile of 40 observations *is the 40th* -- the maximum -- which is not a
percentile but the worst thing that happened, dominated by GC and container
scheduling. A stable p99 wants roughly ten times the samples, on a test whose
sibling already records that seeding dominates its runtime. So E2's body is
amended: a p99 here would be a number that moved every run and got raised until
it stopped failing.

**Also amended: one bound, not two, because the gap was measured.** A first
draft set 200ms clean and 250ms blocked, assuming refusing costs materially
more. Observed local p95 is 10.3ms clean and 12.1ms blocked -- 18%, not a
category. Two budgets 25% apart described a gap that is not there, and 200ms
against a 10ms reality is twenty times the headroom needed to catch anything.
One bound held by both modes is the stronger claim anyway: blocking may not
become materially slower than passing, which separate budgets would license.

**And a correction to ADR-0007's aside, repeated in this plan.** It says "the
one published numeric SLO in this repository is webhook fan-out at p95 < 30s".
There are more: `test_arc_latency.py` publishes `resolve_context` p95 <= 200ms
and `retrieve_context_detail` p95 <= 250ms, with a fixture that builds a real
2,000-revision design point. That file is the template this task followed, and
it is a better one than the webhook test.

Two anti-vacuity guards, because a perf test that measures the wrong path passes
loudly: one asserts the blocked body still trips admission -- if the scanner
vocabulary moved, the block-mode measurement would silently become a second
measurement of the faster clean path and its budget would never fire -- and one
asserts an advisory record exists, proving the envelope decision ran inside the
number rather than being bypassed by the harness.

## Task decomposition — fourth wave (E9's remainder, against what shipped)

E9's first two tasks landed before its epic body was last revised, so the
grounding pass found the body describing work that exists. The evidence schema
ships; the artifact gate ships and is required. What is left is smaller than the
body implies and more load-bearing: the rule those tasks encoded lives only in
CI, and the field that names it has no consumer at the moment a feature would
serve.

### E9-T3 — `requires_validated` refuses at the read, because there is no flag to gate

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: the coupling rule holds at runtime, not only in the pipeline, and it holds
at the one place every consumer already passes through.

Three changes, each small:

1. **`ranking._load` refuses `requires_validated: true` with a status other than
   `validated`** — the same rule `scripts/check_governed_magnitudes.py` already
   enforces on the artifact. Today the loader accepts it, so the running service
   is more permissive than CI. That is backwards for a module whose own
   docstring says an unknown id raises and an empty registry raises at import,
   and it means the guarantee survives only as long as nobody edits the artifact
   without running the gate.

2. **The gate additionally requires `coupling: consumed` when
   `requires_validated` is true.** A `pinned` entry — one whose agreement is
   asserted by a test rather than read by the code, like
   `source-authority-ladder@1` — is never read on a serving path, so a refusal
   at the accessor would never fire for it. Flagging such an entry as
   validation-gated would look like protection and be none.

3. **A test that the refusal reaches process start.** The three `consumed`
   entries are read at import (`claim_serving.py` binds `_ARM_WEIGHTS` at module
   level), so an unsatisfiable flag means the service does not boot. That is the
   property worth pinning, and it is the same one
   `assert_drafter_decision_permits_serving` pins for the drafter.

**Why here and not in E3/E5, which is where the epic body puts it.** The body
says the check is encoded "when the first E3/E5 task is cut". Left there, the
first task that wants a validated magnitude has to build the governance
machinery as a side quest, in a branch about fused retrieval, reviewed by
somebody looking at retrieval. Built here, E3 and E5 each set one boolean. The
machinery is also cheaper to get right in isolation, because its failure mode is
"the service will not start" and that is easier to reason about away from a
feature.

**Why the read, and not a feature flag.** The body says "before the consuming
feature's flag turns on", which presumes a flag mechanism. There is none: the
repository has exactly one genuine feature switch (`arc_drafter_model_enabled`),
every other "turn this on" is a purpose-built column on a purpose-built table,
and ADR-0005 explicitly rules out an env-var flag that can widen authority. What
the tree does have is the right shape — a startup assertion that refuses to
serve when a flag claims more than a committed artifact earned. Making the
accessor refuse gets the same guarantee with no new mechanism, no new table, and
no new configuration surface for an operator to get wrong.

**What this cannot do yet, stated rather than discovered later.** Nothing in the
tree can move an entry from `grandfathered` to `validated`, because producing
that evidence needs an evaluation harness and that is E8. So this ships a
refusal nobody can currently satisfy. That is the fail-closed ordering working:
the flag cannot be set until it can be met. It also means this task's own
registry stays four `grandfathered` entries with `requires_validated: false`,
and the refusals are proved against synthetic registries — the pattern
`test_ranking_registry.py` already uses, and for the reason it states: a test
that only inspects today's entries passes forever once they are correct.

Landed as cut, with one thing learned on the way. The `doc-refs` gate refuses a
bare ADR number in source, so the comment explaining why there is no flag to
gate had to inline the constraint -- an environment variable may not widen
authority, because a widening no audit row names as anyone's decision is what
that rule prevents -- rather than cite the decision that records it. Which is
the gate working: the reason now travels with the code that depends on it.

Both new rules were verified load-bearing by disabling each and watching four
tests fail between them.

Acceptance:
    make governed-magnitudes
    .venv/bin/python -m pytest tests/unit/test_ranking_registry.py tests/unit/test_check_governed_magnitudes.py -q
    make lint && make typecheck && make test-coverage

### E9-T4 — The first review of ordering sites, and a trigger for the next

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: run the review the epic promises instead of the closure it could not
build, and leave behind something that makes the next one happen.

E9 concluded that automatic detection of unregistered rankers is not buildable —
three designs, each defeated by separating the arithmetic from the sort — and
that the honest substitute is periodic review. A periodic review that nothing
schedules is a sentence in a plan.

Two deliverables:

**The sweep.** Every numeric literal in a weights, threshold, floor or ladder
position across the scoring, ranking, confidence, salience and calibration
paths, each either registered or recorded with the reason it does not qualify.
`service/memory/confidence.py`'s `headroom` and `scale` are known candidates and
are a good test of the boundary: E16 removes them outright when it replaces the
saturating curve with noisy-OR, so registering them would govern a number
already scheduled for deletion. Whichever way that goes, the answer is written
down — the value of the sweep is as much the recorded non-findings as the
findings, because the next reviewer starts from them rather than from scratch.

**The trigger.** A scheduled workflow that opens an issue on a fixed cadence,
following `stale-claims.yml`, which already does exactly this shape with
`issues: write`. Deliberately *not* a gate: a check that claimed to find
unregistered rankers would be the exhaustive-closure this epic already rejected
three designs of, and a gate believed exhaustive but defeated in a few lines is
worse than none. An issue is honest about being a prompt for a human.

Scope note: E15–E17 each bring their own new magnitudes under the registry as
they land. Those are their tasks, not this one; this covers what is in the tree
today and what no epic owns.

**What the sweep found**, recorded in full at
[`.develop/reviews/ordering-sites.md`](../reviews/ordering-sites.md):

- **`DECAY_FLOOR = 0.10` was declared twice** -- `service/memory/confidence.py`
  and `service/memory/confidence_decay.py`, each with its own justifying comment
  and its own test importing its own copy. Neither was wrong and nothing
  connected them, so a reviewer changing one would have left the two paths
  flooring differently with both suites green. This is the registry's own stated
  failure -- "no way for a reviewer to find its siblings" -- sitting in the tree,
  and it is the finding that justifies the review existing.
- **The salience weights were governed and their saturation ceilings were not**,
  which governed half an arithmetic: the registered weights are applied to values
  `_ENTITY_DENSITY_CEILING` and `_TOOL_DIVERSITY_CEILING` normalise, so moving a
  ceiling reorders every episode while every weight stays put.
- `CONTRADICTION_PENALTY` **qualifies and was deliberately not registered.** None
  of the three forms describes a single multiplicative coefficient, and calling
  it a one-key weights map is the vocabulary abuse the registry exists to
  prevent. Adding a `coefficient` form is a change to `ranking.py` that its own
  docstring says is not made in passing. Left with the reason, as the entry that
  justifies the form when somebody adds it.
- Evaluation-treatment parameters, calibration bucket counts, sample sizes,
  page limits and every resource-shaped constant were considered and excluded,
  each with the reason. Those non-findings are the compounding half: the sweep's
  regex finds them every quarter, and without the record the next reviewer
  re-derives the same exclusions.

**Two gaps closed on the way, both surfaced by the work rather than sought.**
`_FORMS` had admitted `threshold` since the module was written with no accessor
to read one, so the registry accepted a form it could not serve -- registering
the three entries above is what surfaced it. And `ladder()` had no payload guard
where `weights()` did: invisible while a dict was the only other shape, because
iterating one yields its keys, so a mistagged weights entry came back as a ladder
of field names rather than an error. Widening the payload type to admit a number
is what made it fail, and the fix belongs to both forms.

**The trigger is a quarterly issue, not a check.** `ordering-site-review.yml`
follows `stale-claims.yml` and knows nothing about the tree. A check claiming to
find unregistered rankers is the exhaustive closure this epic already rejected
three designs of; an issue is honest about being a prompt for a human. It opens
one at a time -- a second would not mean twice the review, it would mean the
first was not done, and two stale prompts are easier to ignore than one.

Not swept: the UI repository. E9 names UI-side reordering as uncoverable by the
closure and it is equally uncovered here; claiming otherwise would be the
overclaim the whole approach is written against.

Acceptance:
    make governed-magnitudes
    .venv/bin/python -m pytest tests/unit/test_ranking_registry.py -q
    make all

## Task decomposition — fifth wave (what the four-epic audit found)

E8, E15, E16 and E17 all had every task done and all four sat marked pending.
Walking each body against the tree rather than against its task list closed one
and reopened three, which is the same ratio E19's audit produced and the reason
that ritual exists: a finished task list is evidence about the tasks, not about
the epic.

Two of the three findings are the same defect wearing different clothes -- a
mechanism built, correct, tested, and consulted by nothing. E9-T3 fixed a third
instance in the same session. It is now a thing to look for rather than a thing
to notice.

### E8-T4 — Multi-session recall has no measurement

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a fixture and a report answering whether material from one session is
retrievable from another, joined into `make eval` beside the existing recall@10
and precision@k.

E8's body names multi-session recall as one of four things remaining and no task
covered it. It is not blocked: **extracted claims are the cross-session carrier,
not session events.** ADR-0010 declined to make session events an embedding
target and said so, but claims already are one, and a claim extracted from
session A is exactly what a later session should be able to reach. So the
question the fixture asks is "was the claim extracted in an earlier session
retrieved in this one", and it is answerable against what ships.

Report first, threshold later, following E8-T1's discipline and for the reason
that task learned the hard way -- its first measurement was a fixture bug, and a
threshold set beside that number would have been set against the defect.

The fixture is frozen after first measurement and never edited in place, per
`eval/EVAL.md`. Whether the measurement needs a live embedder decides whether it
runs always or opt-in on a credential, the same split E8-T1 made and for the same
reason: a recall figure measured against a stub measures the stub.

**First measurement: 10/12 = 0.833 at recall@10**, stub embedder, no threshold.
The two misses are queries whose wording shares little with the claim text, which
is what a lexical-dominant regime is expected to miss.

**The fixture found two things about the system before it measured anything, and
both were found by failing.** A staged claim is not retrievable: `project_claim`
refuses to queue until `consolidated_at` is set, and the drain is what turns a
queued row into a vector. A fixture that staged and then queried reported
**0/12** -- a harness failure wearing the shape of a recall result. The
anti-vacuity assertion is the only thing that distinguished them, which is the
argument for writing one into every measurement rather than only reporting.

So: **an agent's cross-session recall depends on consolidation having run.** That
is a real property, not a test detail, and it is recorded in `eval/EVAL.md` where
somebody reading the figure will find it.

**A third thing was found by the ontology refusing the fixture.** The first draft
used a `uses_tool` predicate and gave `owned_by_team` an entity value. Neither
exists: the shipped global ontology has no `uses_tool`, and `owned_by_team` is
typed `string`. The claim writer refused both. The fixture now carries a
`value_kind` per claim and was rewritten against the predicates that exist --
which is the same "decomposition described the service the plan believed
existed" failure, caught this time by a type the service already enforced.

Acceptance:
    make eval
    .venv/bin/python -m pytest tests/integration -q -k "multi_session"
    make all

### E15-T6 — The rename stopped at the adapter

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — openapi.json · **Repo:** contextplane, contextplane-ui

Goal: `SearchResultItem.score` becomes `fused_rank_score` on the wire, the
contract is exported, and the UI pin follows.

E15's body says no field named bare `score` survives, and one does.
`api/routers/_common.py` writes `score=result.fused_rank_score`, so the rename
E15-T1 made for a stated reason is undone one layer later. That is worse than
never having renamed it: the internal name and the wire name now disagree about
the same number, and the wire name is the one a UI author reads and copies.

**How it survived is the part worth carrying forward.** E15-T1's acceptance line
was `! grep -rn "score: float" contextplane/types.py`. Its goal sentence was "no
bare `score` field survives where the three scoring quantities can reach". The
acceptance checked one file; the goal described the tree. A green acceptance
narrower than its own goal is indistinguishable from a met goal, and this is the
second time in two audits that a passing check has certified something it did not
cover -- the first being an adapter test that pinned body and method but not path.

So this task's own acceptance greps the *contract*, not a module.

Cross-repo, backward-incompatible on a response field, so it follows the shape
the workspace already uses: the service PR renames and exports, one UI PR bumps
the pin and regenerates the client together.

**Landed with a guard rather than only a rename**, because the way this survived
was a check narrower than its own goal. E15-T1's acceptance grepped
`contextplane/types.py`; its goal sentence described the tree. The new test greps
the **contract** -- the artifact the claim is about, and the surface a UI author
copies a name from -- and refuses any schema property called exactly `score`.
`fused_rank_score`, `salience`, `eval_score` and `confidence` all pass, because
each says which quantity it is. Verified by reintroducing the field and watching
it fail.

Three test call sites had to move with it, all constructing the response model.
None of them was asserting the old name as a property worth keeping; they were
building a fixture.

**The UI turns out to need almost nothing, and that is checked rather than
assumed.** `SearchResultItem.score` appears in the dashboard exactly twice: in
the generated client, and in one test fixture. No product code reads it -- the
Context Lab page mentions "score" only in prose about what the resolver does not
do. So the pin bump is a regeneration and a fixture edit. It is still its own PR
in its own repo, because this rename is *not* backward compatible and the two
repos cannot merge atomically. E15-T7.

Acceptance:
    make openapi-export
    .venv/bin/python -m pytest tests/conformance/test_openapi_drift.py -q
    make all

### E15-T7 — The contract pin bump for the second rename

**Kind:** task · **Status:** done · **Blocked by:** E15-T6 · **Hotspot:** yes — vendored openapi.json + generated client · **Repo:** contextplane-ui

**Landed in contextplane-ui#21.** The vendored contract carries
`SearchResultItem.fused_rank_score` and no `score`, the generated client follows
it, and `ContextLabPage.test.tsx`'s fixture names the new field. The short window
this entry worried about — the dashboard's pin disagreeing with the service about
a response field — is closed. The pin has since moved again, to `0277c66` with
E20-T10.

Goal: vendor the contract E15-T6 exported, regenerate the client, and fix the one
fixture that names the old field.

E15-T2 was the same task for the first rename and closed as "nothing to do",
because no UI code read `SearchResult.score`. This one is not quite nothing: the
generated client carries `score: number` on `SearchResultItem`, and
`ContextLabPage.test.tsx` builds a fixture with it. Both move; no product code
does.

The window matters and is short. Between the service merging and this landing,
the dashboard's vendored contract disagrees with the service about a response
field -- which is exactly why the workspace splits these into two PRs rather than
pretending an atomic cross-repo merge exists, and why the second one should not
wait.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "search"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E17-T4 — The tenant accessor no consumer calls

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: the scoring consumers resolve through `profile.scoring.resolve_weights`,
so a tenant's activated override actually changes what that tenant is served.

E17 ships a complete override lifecycle -- publish, validate, activate, roll back
-- whose result nothing reads. `resolve_weights` has no caller outside its unit
tests, and every consumer reads `ranking.weights(...)` directly, which
`profile/scoring.py`'s own docstring describes as "the failure this module exists
to make impossible to write by accident".

Three consumers, and they are not equally hard, which is most of this task:

- `search.py` reads inside a function that already has the request's tenant.
  Straightforward.
- `salience.py` reads inside `score()`, which is called from extraction where a
  tenant is in scope. Straightforward, with the caveat that salience is computed
  at write and a tenant changing weights does not retroactively rescore -- worth
  stating in the module rather than discovering.
- `claim_serving.py` binds `_ARM_WEIGHTS` at **import**, so it cannot be
  tenant-scoped without moving the read into the request path. That is the real
  work and it is also what makes the module's current shape wrong: an
  import-time bind is a decision that this number is the same for everybody,
  taken before anybody asked.

**The refusal matters more than the resolution.** Wiring two of three and leaving
the third is the state this task exists to end, so the deliverable includes
something that fails when a scoring consumer reads the registry directly.
`ranking.py` cannot enforce it -- it sits below the profile system and cannot
know who is allowed to call it -- so the enforcement is a lint or
architecture check over call sites, in the shape the repository already uses for
boundaries it will not leave to documentation.

E9-T3 solved the neighbouring problem and its shape does not transfer: there, a
refusal at the read was possible because *nobody* should read an unvalidated
magnitude. Here the direct read is legitimate for the core default and wrong only
for a tenant-scoped consumer, so the check is about the caller and not the value.

**Two of this task's own premises were wrong, both in the same direction: the
work was easier than the entry claimed.**

`claim_serving.py` was described here as "the real work", needing the read moved
into the request path. `_fused_candidates` already took a session and a
`tenant_id`; the import-time bind was above it for no reason the code required.
Deleting the module-level constant and resolving inside the function was three
lines.

`search.py` was described as straightforward and was the one that pushed back.
`search()` had no session of its own -- the arms each open theirs -- so
resolving means opening one, and the fusion-only unit tests asserted the session
factory was *never* called. That assertion was a fair description of the method
until it started resolving for a tenant instead of reading a deployment
constant. The tests now supply a session that answers "no active binding", which
is the core weights they were all written against.

`salience.combine` takes the resolved map as a **required** keyword rather than
defaulting to core. A default would restore the silent-core path the first time
somebody found the argument inconvenient, and a caller with no tenant to resolve
for should not be able to call it at all.

**The check is `scripts/check_scoring_accessor.py`, wired into `make lint`.** It
refuses a `ranking.weights` call outside the accessor and deliberately permits
`threshold` and `ladder` anywhere, because neither is overridable --
`validate_overrides` takes a weight map, demands the key set match the core and
demands it sum to one. A gate that flagged `confidence_decay.py`'s floor would
be teaching the wrong rule. It fails if it finds no read *inside* the accessor
either, so a clean result means it looked.

Three of my own earlier tests had to change, and each was checked for whether
the property still held rather than relaxed to fit. The registry's
consumer-coupling test accepts two spellings now, since a weights consumer
resolves through the accessor; the entry still names the module the number
governs, which is what a reader wants to find. The import-time boot-refusal test
moved its witness from `claim_serving._ARM_WEIGHTS` to
`confidence_decay.DECAY_FLOOR` -- the guarantee is unchanged because the refusal
lives in `_load`, not in an accessor, which is why E9-T3 made it whole-registry
rather than lazy.

Acceptance:
    .venv/bin/python -m pytest tests/unit/test_scoring_accessor.py -q
    make scoring-accessor
    make all && make test-integration

## Task decomposition — sixth wave (E1's last clause, cuttable now that E2 exists)

### E1-T11 — A replayed stream declares its handling tier once, not per event

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: an operator registers a source namespace with a data-sensitivity tier and
an action class, and the session-event write path puts them in the
`IntentManifest` the envelope decision reads — so a replay from a payroll export
is governed as restricted without the caller saying so, and without the caller
being able to say otherwise.

**This is the narrow remainder of E1's clause, not the whole of it.** The clause
reads as "stop an agent declaring its own sensitivity", and both wired paths
already prevent that -- host attestation on the ARC resolve path, and a
route-constructed manifest on the memory path. See E1's body. What is left is
that replayed external content declares *nothing*, so the envelope decision on
that path selects on `intent_kind` alone.

**The stream identity already exists and is not being invented here.** E2-T2 put
`source_system` and `source_namespace` on `memory_session_events`, with a CHECK
that an event naming one names both. That pair is the stream. E2-T2 chose the
per-event conditional and said explicitly it was the weaker of two answers and
that it declined to pre-empt this decision; this is that decision, and it does
not migrate any data -- the CHECK narrows to reference the registry when the
registry exists.

**Where the registration lives, and why not `arc_source_connectors`.** That table
is the obvious candidate and is the wrong one. It registers *connectors* --
schemes, hosts, media types, verifier ids, a byte ceiling, a credential ref --
which is how ARC fetches a document it was pointed at. A replayed conversational
turn is pushed to us by an exporter and fetched from nowhere; it has no scheme
and no host. Reusing the table would mean every replay source inventing an
`allowed_schemes` to satisfy a constraint that describes a different act, which
is the `assertion_provenance` mistake E2-T2 declined to make one table over.

So: a new table keyed on `(tenant_id, source_system, source_namespace)`.

**The tier is the closed one, and that is the point of ADR-0006 having closed
it.** `contextplane/sensitivity.py` exports an ordered `TIERS` and a `rank()`
that refuses a name it does not know. The column carries a CHECK generated from
that tuple, so a registration cannot name a tier the program cannot rank. ARC's
own `data_sensitivity` stays the open string it is -- ADR-0006 decided those two
vocabularies stay separate, and this task does not reopen that.

**An unregistered namespace needs no new rule, which is worth knowing before
writing one.** Leave `data_sensitivity` unset and `_declared_sensitivity` already
reads it as most restrictive, so an unregistered stream gets the strictest
envelope until somebody registers it -- the pressure pointing the right way, and
arrived at without a second copy of a rule that exists. What the task must not do
is substitute `public` on absence, which would make skipping registration the way
to get permissive handling.

**The premise this task would otherwise die on is verified, not assumed.** A
manifest field is worth nothing unless a decision acts on it, and three separate
audits in this plan have now found a mechanism nobody consulted. So it was
checked before the task was written: `selection.rule_applies` ends with
`_matches_scalar(rule.data_sensitivity_tiers, _declared_sensitivity(manifest.data_sensitivity))`,
and `arc_applicability_rules` carries the column. The envelope decision loads
those rules through `_LOAD_RULES` and evaluates them with the same function. So
the wiring is a lookup and a field, not a new dimension.

**And the fail-closed rule is already written, in the place this task would
otherwise have re-invented it.** `_declared_sensitivity` reads an unknown tier
*or an absent one* as the most restrictive, and its docstring records why: the
manifest's field is caller-supplied and open, so plain set membership let a host
escape every rule that named a tier by sending `"ultra-secret"` or nothing —
"both were measured before this was written". That means an unregistered
namespace already lands on the strict answer this task argues for, and the
registry's job is to let an operator say *which* tier a stream actually is,
rather than to install a default that already exists. Do not add a second
fail-closed rule beside that one.

**Action class is the weaker half and may not survive contact.** The clause names
it alongside sensitivity, and `requested_action_classes` exists on the manifest.
But a *stream* does not obviously have one action class -- a chat export carries
questions, decisions and tool traces alike -- where it does obviously have one
handling tier. Build the sensitivity half first, then decide whether a
per-stream action class is a real declaration or a field an operator would have
to guess at. Recording a guess is worse than recording nothing.

**Built as cut, with one addition the task did not anticipate and one premise it
got wrong.**

The addition: `arc_envelope_advisory_records` gains a nullable
`data_sensitivity`. Without it the declaration was **unobservable** -- a record
said a principal was refused and not what tier the matrix judged it at, so "was
this act judged restricted because the stream said so, or because nobody had
declared it" had no answer. Those are the same verdict and different facts, and
only the second is somebody's omission to fix. It is also what let the wiring be
proved end to end rather than asserted: the route test registers a stream and
reads the tier back off the record, and fails when the lookup is stubbed out.
Null means absent, never `restricted`, because storing the strict reading would
erase exactly the distinction the column is for.

The premise it got wrong: this entry said `arc_source_connectors` was the wrong
home and stopped there. The closer call is `memory_source_governance`, which
already registers *sources* and already declares a *tier* for them. It is still
wrong, twice over. It keys on `sync_sources.source_id`, and a sync source is a
connector we run -- it has a `schedule`, a `credentials_ref` and `sync_runs`
recording us going to fetch. A replay exporter is not scheduled by us and holds
no credential of ours; it authenticates and pushes. **Every registration surface
in the tree describes something we fetch from, and this is the first that
describes something that pushes to us**, which is why none of them fit. And its
`authority_tier` is a different axis: how much a claim from a source is *worth*,
against how carefully its content must be *treated*. A payroll export is highly
sensitive and a weak authority; an owner's OpenAPI sync is the strongest
authority and entirely public.

The action-class half was dropped, as the entry allowed. A stream has one
handling tier and does not have one action class, and a column an operator has to
guess at records a guess.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m pytest tests/integration -q -k "source_namespace or envelope"
    make all && make test-integration

## Task decomposition — seventh wave (E3, against the resolve path that exists)

Grounding this epic found three clauses that do not survive contact, recorded in
its body above. The largest is that E3 asks for an RRF merge and the shipped
assembler refuses to merge, with a stated reason that is better than the clause.
So the first task here is an amendment, and the build starts at the second.

The ordering of T2 and T3 is load-bearing and is the reason they are two tasks.

### E3-T1 — ADR: the four blocks are not fused, and why RRF belongs one layer down

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: an ADR recording that `/v1/context/resolve` does not fuse its blocks into
one ranked list, that E3's "RRF merge" clause is struck, and where reciprocal
rank fusion *is* correct in this system.

The distinction is the whole content and it is not "fusion is bad". `search.py`
fuses three arms with governed weights and should: semantic, lexical and graph
are interchangeable retrievers over one corpus, and the question is which
document is most relevant. `assembler.py` composes four *authority classes*, and
fusing them answers relevance by discarding provenance -- "which of these does
the registry stand behind" stops being answerable when a workspace note can
outrank a canonical fact on score.

State the rule the next author needs: **fuse within an authority class, never
across one.** That is checkable, unlike "be careful with ranking".

Record what the refusal costs, because it is not free. A caller wanting one
ordered list must merge client-side and will do it worse; the envelope is larger
than a top-k; and an agent with a small context window has to decide which block
to spend it on. The last is real and belongs to whichever task builds
per-block budgets, not to a fusion nobody can audit.

**One claim this entry made was false, and the ADR says so rather than
inheriting it.** It asserted that a truncated block and a genuinely small one
look identical. They do not: `_block_from_outcome` writes `truncated to N of M
item(s)` into the block's `reason` and marks it `degraded`, and the receipt's arm
row carries `truncated_by_cap` besides. The assembler had already closed that
gap. What is genuinely open is ordering *within* a block -- the cap takes the
arm's first N, so for an arm that does not rank, the cap removes arbitrary items
and calls the rest the answer, and which arms rank is recorded nowhere.

Acceptance:
    make doc-links
    sh -c 'test -f .develop/adr/0011-blocks-are-not-fused.md'
    make lint

### E3-T2 — The receipt says whether it is finished

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: `context_receipts` gains `hydration_state` (`pending` | `complete` |
`failed`) plus the item and exclusion counts known at write time, the read
surfaces expose it, and `GET /v1/receipts/{id}`, `/exclusions` and `/references`
never present a `pending` receipt as evidence.

**This lands alone, before anything asynchronous, and that is the point of it
being its own task.** Today every receipt is complete by construction because
`receipts.record` writes arms, items and exclusions in one transaction and
`resolve.py` fails the resolution if it fails. The column is therefore
uninteresting today -- every row is `complete` -- and that is exactly why it can
be reviewed on its own merits rather than inside a change that also removes a
guarantee.

The counts matter as much as the state and are the part easy to drop. A
`complete` receipt with zero exclusions and a `pending` one with zero exclusions
are the same row without them; with them, a reader can tell "nothing was
withheld" from "we have not written down what was withheld yet". The receipts
module's own standard is that a receipt which reads as complete while withholding
something is worse than no receipt.

Every existing row is `complete`, backfilled, because it was.

**The default is the refusing one, and the fixtures are what settled it.**
Nineteen tests insert receipts as raw SQL, so NOT NULL with no default meant
editing all nineteen -- and two attempts at scripting that produced malformed
literals, which was the signal to stop and ask what a receipt inserted *without*
a hydration claim actually means. It means no claim was made, and the honest
reading of no claim is "not yet evidence". The column defaults to `pending`, and
exactly one fixture needed changing: the one whose test reads exclusions and
therefore does need its receipt to be evidence.

Defaulting to `complete` would have been the permissive direction taken by
omission, which is the shape of a validation status defaulting to validated.

**The summary read surfaces the state; the evidence reads refuse it.** That is
the clause "surface that state and never present a `pending` receipt as
evidence" taken literally and split where it actually divides. `GET
/receipts/{id}` returns 200 with the state and counts, because a caller polling
for a resolution it triggered must be able to learn "not yet". `/exclusions` and
`/references` return 409: both are consumed as complete answers, and an empty
list from a half-written receipt is indistinguishable from one from a finished
receipt. 404 still comes first, so a caller cannot learn an id exists by getting
a different refusal.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m pytest tests/integration -q -k "receipt"
    make all

### E3-T3 — The receipt intent commits synchronously; the rest hydrates after

**Kind:** task · **Status:** amended — not built, on a measurement · **Blocked by:** E3-T2 · **Hotspot:** no · **Repo:** contextplane

Goal: the synchronous path writes a chained receipt-intent row and returns;
arms, items and exclusions hydrate asynchronously; receipt-loss RPO is zero.

**This is the task that relaxes a guarantee, and it must be reviewed as that.**
`resolve.py` currently states: "The receipt write is not best-effort. If it
fails, the resolution fails. That is a deliberate trade of availability for
evidence." This task trades some of it back. What must survive is the property
that trade bought -- that no answer is given which nobody can later show was
given -- so the *intent* row is still synchronous and still fails the resolution
if it fails. What becomes asynchronous is only the detail of what was served.

Blocked on E3-T2 rather than bundled with it because a diff that both removes a
guarantee and adds the discriminator making the removal safe is a diff where a
reviewer cannot see the removal. The epic body already asks for this ordering;
this records that it is a review property, not a sequencing preference.

**`register_receipt_links` stays inside the synchronous transaction**, and the
reason is not symmetry: an unregistered derivative is reached by no erasure
sweep and swept by no expiry, so deferring it makes a retention hole that closes
only if the worker runs. Erasure correctness is not eventually consistent.

Hydration lag and hydration failure alert against an SLO. Follow what the
extraction drain already does rather than inventing a shape: a depth gauge is
not enough, because a queue that is short because it is keeping up and one that
is short because nothing is being enqueued read identically -- that drain carries
an oldest-pending age for exactly this reason and this needs the same.

**Measured before relaxing the guarantee, and the measurement says do not.**

This task trades a guarantee `resolve.py` states explicitly -- "The receipt write
is not best-effort. If it fails, the resolution fails. That is a deliberate trade
of availability for evidence" -- for a latency saving. Nobody had measured the
saving. So it was measured first.

**`tests/perf/test_layered_context.py::test_context_resolve_p95_is_within_budget`
covers exactly this path** -- its docstring says "four arms, assembled, labelled
and receipted, in one synchronous call" -- and reports **p95 = 12.9ms against a
150ms budget** (min 9.2, max 12.9, n=20, local). The receipt write is bounded
above by that whole-path figure, and the path has 137ms of headroom.

**The write is also bounded by construction, so the number does not drift with
scale.** `DEFAULT_ITEM_CAP` is 50 per arm and the assembler applies it rather
than trusting each arm, so a receipt is at most four arm rows plus 200 item rows
plus exclusions, whatever the corpus does. This is not a cost that grows.

So the split would relax a stated availability-for-evidence guarantee, add a
durable payload and a drain worker with its own lag SLO, and buy a saving inside
a 12.9ms p95 on a 150ms budget. **Not built.** E3's body is amended: the
synchronous receipt write stays.

**What E3-T2 delivered is still worth having and is not withdrawn.** The
completeness discriminator makes a half-written receipt unable to pass as
evidence. Today nothing writes one, so every receipt is `complete` -- which makes
the column cheap insurance rather than dead weight, and it is what any future
split would need first.

**What would reopen this**, stated so the next reader does not have to re-derive
it: the item cap rising substantially, the resolve p95 approaching its budget, or
a CI measurement materially worse than the local one. Any of those is a reason to
measure again; none of them is true now.

Acceptance:
    .venv/bin/python -m pytest tests/perf/test_layered_context.py -q -m perf
    make all

### E3-T4 — Trust and quarantine state in the vector index key

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: a quarantined or untrusted item cannot be returned by a vector scan,
because the scan cannot see it -- not because a filter removed it afterwards.

The distinction is the task. Filtering after retrieval means a quarantined item
consumes a slot in the top-k and the caller silently gets fewer results than
asked for, and it means every future scan path has to remember the filter. In
the key, the index answers the right question directly.

Check first whether the arms already predicate on audience inside the query --
`arms.py` says "All three resolve the audience inside the query rather than
filtering after", which is the same shape and may already be the mechanism this
extends rather than a new one to build.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "quarantine or vector"
    make all

**Delivered, and the task's two premises were both wrong — in opposite
directions.**

**There is no quarantine state.** `quarantine` appears twice in the tree, both
in `profile/migration.py` as a *profile migration disposition*, nothing to do
with memory items. No claim, embedding, or workspace entry is ever quarantined.
The word came from the epic body, not from the system.

**Trust is already in the index, and more strongly than a key column.**
`project_claim` re-derives servability on every write and **retracts the
vectors** when a claim stops being servable — status outside
`('staged','superseded')`, `consolidated_at` unset, `t_invalidated_at` set, or
no owning tenant. The rows are deleted, not flagged. So an untrusted claim
already cannot be returned by a vector scan because the scan cannot see it,
which is exactly what this task asked for, built before the task was written.

**What was actually wrong is the thing the task did not look at.**
`ClaimServingService.retrieve` opens with the rule this task exists to enforce —
*"Filters are applied in the query rather than to the ranked list. Filtering
afterwards returns however many of the top k happened to survive, which is a
shorter answer wearing the same shape as a complete one."* — and then filtered
subject visibility on the ranked list, four lines below. So did `query` and
`consolidated_since`.

Those filters could never drop anything. All three reads select on
`c.owning_tenant_id = :tid`, and `owning_tenant_id` is the **subject entity's**
tenant by construction: `_resolve_subject` derives it from `entity.tenant_id` on
both branches, `link_subject` writes the same value, and an unresolved subject
leaves it NULL, which the equality excludes. So the subject was always the
caller's own entity and `is_visible` was always going to say yes.

They cost one entity `SELECT` plus **one ACL query per row** to reach that
foregone conclusion. Measured on the serving path: **2N+2 queries became N+1** —
52 → 26 at 25 rows, 22 → 11 at ten. Exactly halved, three read paths.

Replaced with `_assert_owner_pinned`, which **raises rather than filters**, and
that is the whole point. If somebody removes a tenant predicate, a post-filter
keeps the answer correct and hides the regression — the query silently scans
every tenant's partitions and discards what it finds, and nobody learns until a
bill says so. The tripwire makes it a failure on the first run.

`get` is deliberately untouched: `_BY_ID_SQL` carries **no** tenant predicate by
design, because it must serve a public claim about another tenant's entity. That
is the one path where the two visibility checks were ever load-bearing, and a
test now pins the *absence* of the pin there as well as its presence elsewhere.

**The four unit tests that proved the filter worked did so against a row the
database cannot produce.** Each fabricated `owning_tenant_id=uuid.uuid4()` — a
foreign-owned row — through a mock router unconstrained by any WHERE clause. The
new tripwire caught them on the first run, which is the cleanest evidence that
the state they tested was unreachable. The integration suite's real cross-tenant
tests (`test_a_private_subject_never_appears_in_a_cross_tenant_query`,
`test_retrieval_never_crosses_a_visibility_boundary`) pass unchanged against
Postgres, which is what shows the pin — not the filter — was holding the
boundary all along.

One post-filter remains on purpose: `min_confidence` is applied to the
read-decayed number, which exists in no column to select on, and pushing it into
SQL would mean a second copy of the decay arithmetic. That cost is now stated in
the docstring rather than contradicted by it.

**Fourth instance of the recurring finding** — a mechanism built, correct,
tested, and consulted by nothing. Previously `requires_validated` (E9-T3),
`queryRelationships` (E19-T7), `resolve_weights` (E17-T4). This one is the worst
shape of it so far: not merely unread, but paying N queries per request to
compute an answer its own WHERE clause had already decided.

### E3-T5 — The adversarial-selectivity benchmark, and what it gates

**Kind:** task · **Status:** done · **Blocked by:** E3-T4 · **Hotspot:** no · **Repo:** contextplane

Goal: a benchmark measuring whether a caller can influence what the resolver
selects, and a gate on its result.

"Adversarial selectivity" is the epic's phrase and needs a definition before it
can be measured, which is most of this task. The concrete threat this system
already worries about: a manifest field a caller supplies changing which
obligations apply. E1's audit found the analogous hole in the sensitivity tier
-- a host sending an unknown value escaped every rule that named one -- and
`_declared_sensitivity` closes it by reading unknown as most restrictive. The
benchmark's job is to find the next one of those, not to produce a relevance
number.

So it belongs beside the eval harness in `eval/`, follows the frozen-fixture
discipline, and reports before it gates.

Acceptance:
    make eval

**Delivered, and it found what it was built to find.** The definition was most
of the task, as the entry predicted. The one adopted is narrow and mechanical:
*a caller escapes when a rule that applies to an honest manifest stops applying
to a manifest the same caller could equally well have sent.*

`eval/fixtures/adversarial_selectivity.json` probes every one of the six
dimensions `rule_applies` reads, one minimal mandatory directive each so an
outcome is attributable to the dimension rather than to interacting rules.
**13 of 16 probe variants shed their rule.**

**Two live open-vocabulary evasions, which is the next E1 hole and its twin.**
`environment` escapes on all four variants and `domain_ids` on all three.
Both are matched by bare set membership with no fail-closed wrapper, so
`environment=None`, `environment="prod-ish"` and `domain_ids=[]` each shed
every rule scoped to them. No attestation helps: the caller has not lied, it
has declined to say, and declining to say is read as not matching.

The other three — `entity_ids`, `requested_action_classes`, `intent_kind` — are
misdeclaration, which selection cannot detect and attestation is supposed to.
Separating the two kinds matters: reported together, this would overstate the
defect by a factor of four.

**E1's fix does not transfer, and that is why nothing is fixed here.** Reading
an unknown sensitivity tier as most-restrictive works because sensitivity is an
ordered scale with a maximum. Environments and domains have no ordering. The two
candidates — apply every scoped rule when the manifest is silent, or refuse a
manifest that omits a dimension some active rule scopes on — each cost something
real, so this reports and ratchets rather than deciding. `eval/EVAL.md` carries
the table and both options.

What is gated: `data_sensitivity` must stay closed (parametrised, and the
harness's anti-vacuity control — the one dimension known to be closed must
report exactly one escape, of the kind selection cannot see), and the escaping
set must equal what the fixture records. Equality rather than a count, so a hole
*closing* also fails, which is when the fix gets recorded instead of absorbed.

A dimension list is pinned against the matcher too: a seventh dimension added to
`rule_applies` without a probe fails here, because an unprobed dimension is
exactly where the next one of these would live.

## Task decomposition — eighth wave (E6 and E7, both unblocked by E1 and E2 closing)

### E6-T1 — ADR: what an external anchor buys, and what it must never be called

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide where a periodic digest is published, how often, and what the
resulting claim is -- before any of it is built, because the claim is the part
that gets overstated.

The internal chain ships and is verified. Its limit is precise: a party who can
rewrite the database can rewrite the chain and the verifier together, so the
chain proves nothing against the operator. An external anchor closes exactly that
and nothing else.

**The naming constraint is the ADR's main output.** E6's body says "never called
non-repudiation", and the reason must be written where a marketing page's author
will read it: anchoring every N minutes makes tampering detectable *except within
the last N*, and it identifies no signer. The exposure window is a number a
deployment must state, not a detail.

Decide and record: the cadence and therefore the window; what is anchored (a
digest of the chain head, not content); where (the options differ in trust
assumptions -- a public transparency log, a second party's store, a notary -- and
"cheapest" is not the criterion); and what an operator does when verification
fails, which is the question nobody asks until it does.

**Grounding sharpened the clause and turned up a third chain nobody had
counted.** Two ship -- `arc_receipt_event_heads` and
`arc_operational_event_heads` -- so the anchor publishes both heads. `audit_log`
is **not** chained at all, which this task did not know; the ADR records that as
an open scoping question rather than anchoring two things and implying three.

The decision that took the most argument was rejecting a signature. Signing the
head with a deployment key looks stronger and defends against everyone except the
party the anchor exists to constrain -- and it invites exactly the
non-repudiation language the clause forbids, because a signature implies a
signer.

Acceptance:
    make doc-links
    sh -c 'test -f .develop/adr/0012-external-anchor-for-the-digest-chains.md'
    make lint

### E6-T2 — Session events are the record class retention does not govern

**Kind:** task · **Status:** pending — rescoped · **Blocked by:** none · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: a retention class is a named policy -- how long, and what disposal means --
that a stream or a claim category is assigned to, instead of the single
per-tenant `memory_retention_days` that decides everything today.

Check the shape E1-T11 just built before inventing another. That task registered
`(tenant_id, source_system, source_namespace)` with a handling tier and found
that no existing registration surface fitted because they all describe things the
service fetches. A retention class attaches to the same key for the same reason,
and if it does, this is a column and a vocabulary rather than a table.

The vocabulary is the decision: a class that says only "90 days" is a number with
a name. It has to carry what happens at the end -- delete, shred, anonymise --
because those differ in what survives and an operator choosing between them is
choosing what an auditor will still be able to see.

**Rescoped, because retention classes already ship and this task's premise was
false.** It said retention is "the single per-tenant `memory_retention_days` that
decides everything today". It is not. `contextplane/retention/` carries a
`retention_policies` table keyed on `(policy_version, record_class)` with
`legal_basis`, `retention_days`, `erasure_mode`, `minimization_action`,
`tombstone_behaviour` and `verifier_disclosure`; a holds store that can keep a
record past its period *attributably*; tombstones with per-tenant salts; and a
`RetentionExpiryWorker` that sweeps, consults holds, and enqueues. Twelve record
classes are governed, including `context_receipt`, `memory_claim`, `audit_log`
and `workspace_entry`.

It even carries the vocabulary this entry said it would have to invent -- "it has
to carry what happens at the end" is `erasure_mode`, and the four values are
`delete`, `minimize`, `minimize_and_tombstone` and `exempt`.

**Sharpened by E6-T3: the gap is not structural, it is a period nobody
enforces.** Grounding migration 0066 established that session events carry
`expires_at`, written on every row from the tenant's `memory_retention_days`,
and that `ix_mse_expiry` exists specifically to sweep on it — and that
**nothing sweeps.** `RetentionExpiryWorker` iterates the twelve record classes
in `retention_policies`; `session_event` is not one, and `retention/` does not
reference `memory_session_events` at all. The only `DELETE` against that table
is actor erasure.

So the concrete failure is: the highest-volume record class in the system
advertises a retention period the database CHECK-constrains to 1–180 days,
carries a column and a purpose-built index to enforce it, and **accumulates
forever**. A deployment reading its own configuration would believe otherwise.

That changes what this task has to decide first. Not "invent a vocabulary" —
that exists — but whether `session_event` becomes the thirteenth governed record
class, inheriting holds, tombstones and the sweeper, or keeps its bespoke
per-tenant period with a sweeper of its own.

Prefer the first, and the reason is `legal_basis`: it is the field the framework
is built around, and a record class outside the framework has no legal basis
recorded anywhere. The argument against is that `memory_retention_days` is
per-*tenant* while `retention_policies` is keyed on `(policy_version,
record_class)` — so folding it in either drops per-tenant configurability or
needs a tenant override the framework does not currently have. Whichever way it
goes, say what happens to the 1–180 CHECK, because it is the only place that
period is currently constrained at all.

**What is actually missing is one record class: `session_event`.** Session events
are governed by `tenants.memory_retention_days` -- a CHECK-constrained integer
between 1 and 180, read per write in `session_events.py` -- and an `expires_at`
column, entirely outside the policy framework. So the newest and highest-volume
record class in the system is the one class the retention design does not reach:
no legal basis, no erasure mode, no hold can protect it, no tombstone records its
disposal.

That is the task. Bring `session_event` under `retention_policies`, decide its
mode against the four that exist rather than adding a fifth, and reconcile the
per-tenant integer with a policy row -- the integer is a tenant's *choice within*
a class, which the framework does not currently model and may need to.

**Do not reach for `arc_source_connectors` or a per-stream table.** The earlier
draft of this entry suggested attaching retention to the stream registration
E1-T11 built. Retention is a property of a *record class*, which is what the
shipped framework says and what a legal basis attaches to; a per-stream period
would be a second retention system keyed on something a lawyer does not reason
about.

Acceptance:
    .venv/bin/python -m alembic upgrade head
    .venv/bin/python -m pytest tests/integration -q -k "retention"
    make all

### E6-T3 — Crypto-shredding, which a shipped decision already assumes exists

**Kind:** task · **Status:** done · **Blocked by:** E6-T2 · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: per-scope content keys, disposal by destroying the key, and an auditable
deletion event recording that it happened.

**The claim is worse than unbuilt: there is no *mode* for it either.**
`retention/policies.py` declares exactly four erasure modes -- `delete`,
`minimize`, `minimize_and_tombstone`, `exempt`. None of them is shredding. So
migration 0066 rests on a disposal mechanism that has neither an implementation
nor a name in the vocabulary that would have to carry it, and adding one is a
change to a closed set that twelve record classes are already classified
against.

**Start by reading migration 0066, because this task is repairing a claim already
in the tree.** That migration chose hash partitioning over range partitioning for
`memory_session_events` and justified it partly by saying disposal is
crypto-shredding, "which operates on content, not on physical layout". There is
no key and no shredding. The partitioning decision is still right on its other
grounds -- the unique key argument is independent and sufficient -- but a design
decision resting partly on an unbuilt mechanism is the thing to fix first, either
by building it or by striking the clause.

The deletion event is not a log line. It is the evidence that disposal happened
on schedule, so it belongs in the audited stream with the scope, the schedule
that triggered it, and what became unreadable -- and it must survive the data it
describes, which is the one property a naive implementation loses.

Note what crypto-shredding cannot do: it makes content unreadable, not absent.
Row counts, timestamps and graph shape survive. If a retention class promises
erasure rather than unreadability, it needs a different disposal and E6-T2's
vocabulary is where that distinction lives.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "shred or disposal"
    make all && make test-integration

**Delivered as a strike, not a build — and grounding it found something larger
than the clause it was sent to fix.**

*Why strike.* Crypto-shredding has no consumer. No retention class asks for
unreadability-rather-than-absence, and adding a fifth erasure mode to a closed
set of four that twelve record classes are already classified against is a
vocabulary change that needs a demand first. Building it now would be another
mechanism consulted by nothing — the exact outcome E6-T4 already produced once
and had to revert.

*The correction needed correcting.* The first replacement premise said disposal
for this table is `expires_at` expiry, full stop. That is the **design** and it
is not the behaviour: `expires_at` is written on every row from the tenant's
`memory_retention_days`, `ix_mse_expiry` exists to sweep on it, and
**nothing sweeps.** `RetentionExpiryWorker` operates on the twelve record
classes in `retention_policies`; `session_event` is not one, and `retention/`
does not reference the table at all.

So migration 0066 carried **two** claims about a disposal that does not happen.
The second was easy to miss: *"`expires_at` sweeps now fan out across
partitions. Accepted: `ix_mse_expiry` is a background sweep with no latency
budget"* — present tense, costing out a job that does not run. Both are amended;
the second is restated conditionally, because the trade it describes is still
the right one to have made about a sweep that will exist.

*The partitioning conclusion never moved.* The hash key is a subset of
`uq_mse_session_seq` and leads both read indexes, so range partitioning breaks
that invariant regardless of how disposal works. Only the premise was wrong,
twice.

**This sharpens E6-T2 rather than closing it.** The finding is not a docstring
problem: the highest-volume record class in the system advertises a retention
period the database CHECK-constrains to 1–180 days, carries a column and a
purpose-built index to enforce it, and accumulates forever. That is E6-T2's
subject and it now has a concrete failure rather than a structural observation.

### E6-T4 — An undeclared stream is blocked, not merely handled carefully

**Kind:** task · **Status:** done — as an amendment · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a PII block tier that applies to content arriving from a stream nobody
registered.

E1-T11 made "undeclared" checkable: `sensitivity_of` returns `None`. Today that
absence already produces the strictest *envelope* answer, because
`_declared_sensitivity` reads an absent tier as most restrictive. This clause is
about the *admission* path instead, which is a different decision made by a
different module -- `pii_guard` scans against a tenant policy and knows nothing
about streams.

The question this task must answer rather than assume: is blocking right? An
undeclared stream that fails admission cannot be replayed at all, which turns an
operator's omission into an outage for a tenant's import. The alternative --
scan-and-quarantine -- keeps the data and marks it. Whichever is chosen, the
argument belongs in the code, and it should be checked against how the envelope
path resolved the same tension, which was to be strict without refusing the
write.

**Resolved by measurement, and the answer is that the clause is already
satisfied.** The task said to answer "is blocking right?" before building. The
answer turned out to be that there is nothing left to block.

`admission.blocking_field_policies()` is the cross product of every pilot field
and every prohibited class, and `PROHIBITED_CLASSES` is read off the shipped
detectors rather than written by hand. So admission already refuses **every class
its scanner can detect, on every field it governs, from any source**. Content
from an undeclared stream is held to exactly the floor content from a `public`
one is.

**This was established by building the mechanism and finding it did nothing.** A
tier-derived floor was written -- an extra `field:*` wildcard applied when the
resolved tier is most-restrictive, threaded from the write path through both
guards -- and then run against the same inputs with and without it. Every outcome
was identical, because the floor had already blocked everything the scanner
recognises. The mechanism was reverted rather than shipped: a parameter that
changes no outcome is the "governance object nothing consults" defect this plan
keeps finding in other people's work, and writing a good docstring for it would
have made it harder to notice, not better.

The design it was reaching for is still recorded, because it is the right shape
if the situation changes: **map the handling tier to the floor, not the
registration state.** Building "undeclared streams are stricter" literally makes
identical content admissible or not depending on a registration its author does
not control, with the operator's remedy -- register the stream -- having nothing
to do with the phone number in the text. An undeclared stream reaches a strict
tier anyway, because absent already reads as most-restrictive.

**One real gap surfaced and it is not about streams.**
`security.pii_guard.scan_for_pii` builds a scanner carrying the *tenant's own*
patterns and logs what they match. `admission._scanner` carries only the
built-ins. So a tenant-configured pattern is detected, written to the detection
log, and **never enforced by admission at any tier** -- a tenant that adds a
custom detector and sets it to block gets a log entry and an admitted write. That
deserves its own task and belongs to whoever owns the admission floor, not to
E6.

The finding is pinned by a test rather than left in this entry, so a future
change that makes the floor non-exhaustive fails rather than silently reopening
the hole the clause was worried about.

Acceptance:
    .venv/bin/python -m pytest tests/unit/test_admission.py -q
    make all

### E7-T1 — The tool registry, and what a default connection sees

**Kind:** task · **Status:** done — with one clause that cannot be satisfied yet · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: one machine-readable registry naming every MCP tool, its surface tier, and
the REST operation it corresponds to -- and a default connection that exposes the
core verbs rather than all of them.

Roughly 120 tool functions register unconditionally today across fifteen modules.
An agent connecting is handed all of them, which is the problem this epic opens
with, and the fix is not a smaller server but a declared core.

**Choosing the six to eight is the task, and it should be derived rather than
argued.** The two-call loop names its own: remember and recall. The rest should
come from what an agent actually calls to complete a task, which the receipts
already record -- so read them rather than picking. A core verb set chosen by
taste will be defended by taste.

The registry is a committed artifact, not a computed one, for the reason the
governed-magnitude registry is: a generated list agrees with the code by
construction and therefore cannot catch the code being wrong. Its parity with
the code is a gate, which is E7-T2.

Acceptance:
    .venv/bin/python -m pytest tests/conformance -q -k "tool_registry"
    make all

**Shipped.** `contextplane/api/mcp/tool_registry.json` is the committed artifact:
every tool with its `module`, its `tier`, and the REST operation it corresponds
to. `scripts/check_mcp_tool_registry.py` reports **70 registered, 70 listed, 8
core over 7 REST paths**, and E13-T4 turned those last two figures into ratchets
so the tier cannot widen without somebody raising a ceiling on purpose.

**The one clause that could not be honoured: "read the receipts rather than
picking."** E7-T1 asked for the core set to be *derived* from what agents
actually call, on the argument that "a core verb set chosen by taste will be
defended by taste." That evidence does not exist, and E13-T3 established why:
nothing has shipped, so there is no call corpus, and `install_tool_metrics`
answers "was this called in the current scrape window" rather than "has any
agent ever needed this."

So the set was chosen by argument, and the entry says so rather than implying a
derivation happened. Two things limit the damage the original clause feared:
E13-T1 fixed the *unit* the set is measured in — paths, not operations, since a
path carrying two methods is one thing for an integrator to learn — and E13-T4
ratchets it, so taste can no longer quietly expand what taste chose. Re-deriving
from receipts stays available once a release produces them; it is not a
prerequisite for the registry existing.

**Not in this task:** the default connection actually *serving* only the core.
That is E7-T3, which the epic already cut as "the full surface is opt-in, per
envelope" — this task delivers the declaration it enforces.

### E7-T2 — Parity, extending the gate that already half-exists

**Kind:** task · **Status:** done · **Blocked by:** E7-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: every registry entry names a tool that exists and a REST operation that
exists, in both directions, plus the docs conformance half.

`tests/conformance/test_memory_rest_mcp_parity.py` already asserts every memory
operation exists over both surfaces and that no memory tool takes an actor
identifier. **Extend it rather than writing a second one.** Two parity gates
disagreeing about what parity means is worse than one covering less, and that
file's actor-identifier rule is a property the wider gate should inherit rather
than restate.

The docs half is the one that rots: a tool documented and removed, or added and
undocumented, are both surfaces where an agent's expectation and the server
disagree. Prefer deriving the docs from the registry to checking two hand-written
lists agree.

Acceptance:
    .venv/bin/python -m pytest tests/conformance -q -k "parity"
    make all

**Delivered, and what widening the actor rule found.** Nine tests now: the
registry↔code directions live in a `make lint` gate, and the conformance file
carries the contract half (every REST mapping names an operation the OpenAPI
document has), the docs half (all 8 core tools documented, nothing documented
that was removed, and a ratchet at 20 on the undocumented extended surface), and
the actor rule generalised from memory to every tool.

Widening the actor rule surfaced two tools that take one — `grant_intent_participation`
and `revoke_intent_participation`. **Exempted, on evidence rather than
convenience:** the parameter is the operation's *patient*, never its principal.
Authority comes from `ctx.actor_id`, minted from the validated JWT where no tool
argument reaches; `_require_owner` refuses any caller whose active role on the
task is not owner — strictly above what the grant confers; that check is in the
service both transports call; and self-grant is refused by the contract
dataclass and again by `ck_grant_not_self`. The exemption set is pinned for
**equality**, so a third tool appearing and one of these disappearing both fail.

The rule inverts inside the exemption, which is the part worth writing down. For
session memory the parameter's *absence* is the control — nothing downstream
would refuse a caller who named a colleague. For a grant, the parameter's
*presence* is what the owner check operates on. Removing it would break REST/MCP
parity and delete delegation, not close a hole.

**A defect found next door, filed rather than fixed here.**
`uq_task_participant_grant` is `(tenant_id, intent_id, actor_id)` with no expiry
in the key, and `revoke` keeps the row and sets `expires_at`. So re-granting a
previously revoked participant violates the constraint, and both adapters catch
only `AudienceDenied` — the caller gets a 500 rather than a refusal.
Participation is effectively one-shot per actor per task, and nothing tests
re-grant-after-revoke. That is E7-T5, below.

### E7-T5 — Participation is one-shot per actor, and fails as a 500

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: an actor who was granted, revoked, and granted again gets participation
back, and no reachable input produces an unhandled `IntegrityError`.

Found while judging E7-T2's exemption, in code that exemption did not touch.
`uq_task_participant_grant` is `(tenant_id, intent_id, actor_id)`
(`contextplane/workspaces/models.py:52`) and revoke is a soft delete —
`queries_audience.py:152` sets `expires_at` and keeps the row. The second grant
therefore collides with the first, and `routers/intent_memory.py:100` and
`mcp/tools/intent_memory.py:150` both catch only `AudienceDenied`.

Two shapes, and the choice matters more than the fix:

- **Re-grant reactivates the existing row.** Keeps one row per actor per task,
  so `fetch_actor_role` stays a single lookup — but the grant history is
  overwritten, and who granted participation the first time stops being
  recoverable.
- **Expiry joins the uniqueness key**, so revoked rows accumulate and only the
  live one is unique. Keeps history; every audience read has to learn to ignore
  the dead rows, and a missed predicate is a revoked participant who can still
  read.

Prefer the first unless the audit trail is required, and if it is, say where
that requirement comes from. Either way the adapters need to stop turning a
constraint violation into a 500.

Acceptance:
    .venv/bin/python -m pytest tests/integration/test_intent_memory_surfaces.py -q
    make all

### E7-T3 — The full surface is opt-in, per envelope

**Kind:** task · **Status:** pending · **Blocked by:** E7-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: a principal sees the core verbs by default and the wider surface only where
its autonomy envelope says so.

This clause was unbuildable when the epic was written and is buildable now: E1
shipped the envelope, the applicability matrix and `enforce_envelope`, so "which
verbs may this principal see" is the same question the matrix already answers
about acts.

**Listing and calling are two decisions and both must be made.** Hiding a tool
from the list while still executing it when called is security by obscurity;
refusing at call time while listing it invites an agent to plan around a verb it
cannot use. Do both, and make the refusal say which it was -- the envelope guard's
existing refusal codes already distinguish "no envelope" from "outside envelope",
and an agent's remedy differs.

Check first whether the MCP layer can reach an envelope decision at connect time.
Tools authenticate per call rather than at SSE handshake, which may mean the
listing is per call too, and that changes the shape of this task.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "mcp and envelope"
    make all

### E7-T4 — Safe defaults on the two-call loop, and a quickstart that proves them

**Kind:** task · **Status:** pending · **Blocked by:** E7-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: the remember/recall pair an agent reaches first is the safe one, and a
quickstart measures how long it takes to get there.

The routing mostly ships -- the MCP memory tools go through admission, and
`tools/memory.py` records that this path once called `record_event` directly and
scanned nothing, which is what it was fixed for. What is missing is that nothing
makes the safe path the default an agent gets without asking for it.

"Time to first memory" is a number, so it needs a definition before it can be
quoted: from what, to what, measured how. Undefined it becomes a marketing figure.
Define it as a scripted path a test executes, so the claim and its evidence are
the same artifact -- the shape `make eval` already uses for every other published
figure here.

Acceptance:
    make eval
    .venv/bin/python -m pytest tests/integration -q -k "two_call or quickstart"
    make all

## Task decomposition — ninth wave (E4, whose central noun does not exist yet)

Grounded before decomposing, and the grounding moves most of the epic. E3-T4
established that **`quarantine` appears twice in the whole tree**, both in
`profile/migration.py` as a profile-migration disposition. No claim, embedding,
receipt or workspace entry has ever been quarantined. So E4 is not wiring an
existing mechanism to DORA — it is building the mechanism, and the wiring is the
smaller half.

What E4 *can* build on, all of it already shipped:

- **`get_blast_radius`** — a real graph traversal with a service method, a REST
  route (`routers/graph.py`) and an MCP tool. E4's "dry-run blast-radius
  preview" is this function applied to a quarantine predicate, not a second
  traversal.
- **`PromotionPolicy.blast_radius_threshold`** — per-tenant, `DEFAULT 5`,
  `CHECK >= 0`, already consulted by `promotion_eligibility`. Blast radius is
  *already* a governance trigger in this system; E4 adds a second consumer, not
  a new concept.
- **`IMPACT_*`** — a closed vocabulary of reasons a claim needs review, with
  `IMPACT_BLAST_RADIUS` among them and an honest note that "narrows a surface"
  is only decidable for a listed subset of predicates.
- **Curation cases** — `CASE_OPEN`/`CASE_ROUTED`/`CASE_RESOLVED`, where a
  disposition is a *proposal* and its approver is recorded at disposition time
  rather than inferred later. E4's "auto-created incident case" is a case.
- **Bitemporal invalidation** — `t_invalidated_at` plus the derivative
  propagation and erasure machinery that already reaches embeddings.

What does not exist at all: quarantine, bulk revert, and every DORA noun —
materiality, regulatory clock, report windows, at-risk escalation. Searched for
`dora`, `DORA`, `materiality`, `regulatory`, `notification_clock`: **zero hits
each**.

**A naming trap, flagged before somebody walks into it.** `severity` is already
taken. It is the PII scanner's `advisory < warn < block` ordering, in
`types.py`, `admission.py` and `pii_scanner.py`. DORA materiality is a different
axis measured against different thresholds, and a field called `severity` on an
incident case will be read as the scanner's by anyone who has seen the scanner
first. Name it `materiality` and keep them apart.

### E4-T1 — ADR: is quarantine a state, or a predicate evaluated at read?

**Kind:** task · **Status:** done — landed as ADR-0016, out of order · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

**Answered by [`ADR-0016`](../adr/0016-quarantine-is-a-materialised-state-on-its-own-column.md),
which shipped with E4-T2 rather than before it.** Recorded here because a task
left `pending` next to its own delivered answer is how the answer gets written
twice. The ADR settles every question this entry posed:

- **Materialised state**, on its own `quarantined_at` column — not a read-time
  predicate, and not a reuse of `t_invalidated_at`. The reuse would have been a
  bug: `_SERVABLE_AS_OF`'s term is `as_of`-relative and `as_of` is caller-supplied,
  so a quarantine expressed that way is bypassable by the caller it restrains.
- **Index retraction *and* read filtering**, which this entry framed as an
  either/or. It also corrects this entry's premise: the codebase does *not* make
  things unservable by deleting from the index. Retraction is a **recall**
  mechanism — a dead vector still occupies a candidate slot in
  `ORDER BY vector <-> q LIMIT k` — and the read filter is what makes a row
  unservable. So both, for different reasons, rather than one instead of the other.
- **Rows arriving after the sweep** get their own section: they are the emitting
  connector's problem, not a standing predicate's.

Goal: decide the shape before anything is built, because the two shapes have
different failure modes and only one of them matches what this system already
does.

The epic says "quarantine by provenance predicate", which reads as a rule
evaluated per read: *anything from this connector run, this extractor version,
this source namespace*. The alternative is a state materialised onto each
affected row when the predicate is applied.

The argument for materialising, and it is the same argument E3-T4 landed on:
**this codebase already knows how to make something unservable, and it does it
by removing the row from the index rather than by filtering at read.**
`project_claim` retracts a claim's vectors the moment it stops being servable.
A read-time provenance predicate would be a second, weaker mechanism — every
future scan path would have to remember it, and the first one that forgets is a
quarantined claim served with a straight face.

The argument against, which the ADR has to answer honestly: a predicate is
revocable in one statement and a materialised state is a bulk write over an
unbounded set, which is exactly why E4 also asks for bulk revert. And a
predicate can be *evaluated* against rows that arrive after it was written,
where a materialised state cannot — a connector still emitting bad claims keeps
producing servable ones until somebody re-runs the sweep.

Prefer materialised state with a **standing predicate that re-applies on
write**, if that can be built without a second admission path; say so explicitly
if it cannot, because then the choice is genuinely between the two failure
modes and the ADR should pick one and name what it is giving up.

Whichever is chosen, it must answer: does a quarantined claim disappear from the
index (E3-T4's retraction), or stay indexed and get filtered? Answering
"filtered" contradicts a decision made two tasks ago and needs to say why.
## Task decomposition — tenth wave (E5, and the E9 rule that governs all of it)

E5 asks for numbers that decide what a human reviewer looks at first. E9's
property applies to every one of them — *no ungoverned score orders anything a
user sees* — and a review queue is the most literal case of that rule in the
product. So the constraint is not a footnote here; it is the frame.

**What the registry looks like today, which decides what E5 can honestly
claim.** `ranking_registry.json` holds **seven** governed magnitudes and **all
seven are `grandfathered`** — none validated. The recorded reasons are
consistent and worth reading before adding an eighth: most say the label a
validation would rest on (*whether a claim was later retrieved and cited on a
succeeding turn*) needs a citation-to-outcome join that does not exist.

E5's numbers are not all the same kind, and lumping them together is the
mistake to avoid:

- **Acceptance-sampling parameters are *derived*, not fitted.** They follow
  from a stated risk tolerance — what fraction of bad dispositions is
  acceptable, and with what confidence. That is an argument, not a
  measurement, and it can be recorded as a *complete* one. This is the first
  magnitude in this registry that could be governed without being
  grandfathered, and that is worth doing deliberately.
- **Leverage is measurable today.** `get_blast_radius` already answers "how
  much depends on this", and `promotion_eligibility` already treats the answer
  as a governance trigger against a per-tenant threshold. Leverage is a read,
  not a new model.
- **Expected loss is neither, and this is the honest gap.** It needs a loss
  model — what a wrong disposition actually costs — and nobody has stated one.
  The same missing outcome join that grandfathered the other seven blocks it.

**The queue is FIFO today.** `curation_queue.py` orders by `created_at` at both
its listing sites. There is no ranking to fix, which means E5 does not improve a
ranking — it *introduces* one, and introduces its failure modes with it.

**`action_class` already exists**, in ARC's scope vocabulary
(`lower_scope_action_class`). E5 must reuse it or state why a review action
class is a different axis. Two vocabularies spelled the same way is how a
reviewer's policy ends up keyed on the wrong one.

### E5-T1 — ADR: which of E5's numbers can be governed, and which cannot yet

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide, before any of them is written, which E5 magnitudes enter the
registry validated, which enter grandfathered, and which do not enter because
they cannot yet be stated.

All seven existing entries are grandfathered and each says honestly why. An
eighth added the same way is defensible; an eighth added *silently* the same way
while the epic describes it as "acceptance-sampling math" is not — the phrase
implies a derivation, and a reader will assume one happened.

The ADR's real work is separating the three kinds above and refusing to let the
third pretend to be the first. If expected loss cannot be stated, the queue
ranks on leverage and sampling alone and says so, which is a smaller claim and a
true one.

Acceptance:
    make doc-refs doc-links
    make all

**Delivered as [ADR-0014](../adr/0014-derived-magnitudes-are-a-third-status.md),
and the finding is sharper than this entry predicted.** The registry's status
vocabulary has exactly two values, and **E5's sampling parameters fit neither**.

`validated` demands four evidence fields because "a status without its evidence
is a word, and the word is what a later reader would trust". `grandfathered`
demands a reason because "an exemption nobody has to justify is one nobody will
revisit". An acceptance-sampling parameter follows by arithmetic from a stated
defect rate and consumer's risk: there is no held-out result, because it is not
a prediction. Recording it `validated` would mean inventing a method and a
result for a check nobody ran; recording it `grandfathered` would assert nobody
checked, which is false in the other direction — the derivation *is* the check,
and unlike every other entry it is reproducible by anybody with a calculator.

So the ADR adds a third status, `derived`, with `derived_from` and `derivation`
as its evidence fields, and generalises `requires_validated` to be satisfied by
`validated` **or** `derived`, never by `grandfathered`.

Two consequences for the tasks below. **Expected loss does not enter the
registry at all** until a loss model exists — not even as `grandfathered`, which
would make it look like the seven numbers that ship and order things while
awaiting evidence. This one does not ship, so E5-T3 and E5-T6 rank on leverage
and sampling and say so on the surface. And **E5-T3's anti-starvation magnitude
is likely `grandfathered`**: an age weighting is a reasoned position, neither a
derivation nor a measurement. This ADR does not make the registry mostly
`derived`.

The dissent is what to carry into E5-T2 and E5-T4: `derived` may launder an
empirical assumption as arithmetic. The OC curve is exact only for a
representative draw, and E5's queue is *ranked* and partly disposed of by
policy — so the derivation risks being arithmetic about a lot that does not
exist. The representativeness assumption belongs in `derived_from`, but an
assumption in a field is weaker than one in a gate.

### E4-T2 — The quarantine state, and the revert that makes it usable

**Kind:** task · **Status:** done · **Blocked by:** E4-T1 · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: a provenance predicate quarantines the claims it matches, and one
statement puts them back.

Bulk revert is not a convenience feature here and should not be scheduled as
one. An operator who cannot undo a quarantine will not run it on a real
incident, so the mechanism that has no revert is a mechanism nobody uses under
the conditions it was built for.

**Amended by [ADR-0016](../adr/0016-quarantine-is-a-materialised-state-on-its-own-column.md);
this paragraph's original recommendation was a bypass and is struck.**

It said: reuse `t_invalidated_at` rather than adding a flag. That is defeated by
a query parameter. `_SERVABLE_AS_OF`'s `status` term is unconditional, but its
`t_invalidated_at` term is **`as_of`-relative** — deliberately, since "a claim
closed after the instant asked about was still believed then, which is the whole
point of asking". And `as_of` is caller-supplied on both transports: a query
parameter on `GET /v1/memory/claims`, an argument on the `query_claims` MCP
tool. Quarantine a bad connector run at 14:00, and `query_claims(as_of="13:00")`
serves every quarantined claim.

**Follow `discard`'s shape instead.** It writes `status='rejected'` and "it never
serves again" — unservable at every `as_of`, because that term is unconditional.
Quarantine gets a dedicated `quarantined_at`, joined into `_SERVABLE_AS_OF` as
an unconditional `AND c.quarantined_at IS NULL`, with the matching term in
`_SERVABLE_STATUSES` so `project_claim` retracts the vector too. The
rule-to-row ledger — which predicate closed which rows, when, by whom — is a
side table read at apply, revert and audit, **never at read**. That answers what
the struck paragraph was right to worry about: a bare boolean forgetting the
provenance of the decision.

Reuse the derivative propagation path for reaching embeddings —
`enqueue_for_sources(..., operation=OPERATION_REBUILD,
trigger=TRIGGER_POLICY_CHANGE)`, which needs no new vocabulary and no tombstone,
and whose revert is the identical call.

**A second claim struck.** This entry said that path "already has a story for
what happens when propagation is late (`pending_overdue`, and the arms refuse to
serve rather than serving stale)". It does not cover this:
`register_derivative` defaults `blocking=False`, `register_claim_artefact` never
passes it, and `arms.py` already records that a `blocking_only` guard over
`vector` "would never fire". This does not damage the design — correctness
commits synchronously and only recall is async — but a reviewer who accepts that
sentence will believe a guard is protecting something it cannot reach.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "quarantine"
    make all

**Delivered.** Migration 0071 adds `quarantined_at` to `memory_claims`, read
**unconditionally** in `_SERVABLE_AS_OF` and in `project_claim`, plus a
`claim_quarantines` ledger and a `claim_quarantine_members` join table.
`QuarantineService` applies, previews and reverts.

Four decisions worth carrying forward, each with the test that pins it:

- **Revert restores the recorded membership, never a re-run of the predicate.**
  The graph moves, so a claim written after the quarantine and matching the same
  predicate was never withheld — and revert must not claim to restore it.
- **A claim held by a second, unreverted quarantine stays withheld.** Otherwise
  reverting yesterday's incident republishes what today's is withholding.
- **A predicate matching nothing is refused rather than recorded.** A quarantine
  that withheld nothing reads later as one that was tried and worked.
- **`apply` does not overwrite an existing `quarantined_at`.** Relabelling
  content as withheld later than it was would make the earlier quarantine's
  revert restore something the later one still means to withhold.

The selector vocabulary is **closed** — connector run, extractor version,
namespace prefix — and each maps to an index that already exists. Left open, an
operator could withhold by confidence or by author, which are not provenance
statements and are ways to make content disappear for a reason nobody wrote
down.

**E4-T3 is not done by this.** `preview` returns the *match set*, which is what
the predicate reaches directly. The blast radius — what depends on those claims
— is `get_blast_radius`, and wiring it is still that task.

Not asserted here, deliberately: that the vectors leave the index. Correctness
commits with the column write in the same transaction; the propagation enqueue
is a recall concern and is asynchronous by design, so asserting index state here
would either test the drain or encode a race.

### E4-T3 — The preview is `get_blast_radius`, not a second traversal

**Kind:** task · **Status:** done · **Blocked by:** E4-T2 · **Hotspot:** no · **Repo:** contextplane

Goal: before applying a quarantine, an operator sees what it would reach — using
the traversal that already exists.

`get_blast_radius` ships with a service method, a REST route and an MCP tool,
and `promotion_eligibility` already treats its result as a governance trigger
against a per-tenant threshold. The preview is that traversal seeded from the
predicate's match set.

Writing a second traversal is the failure mode to avoid, and it is a likely one
because the shapes differ slightly: promotion asks about one claim, quarantine
asks about a set. Widen the existing one; two graph walks that disagree about
what "downstream" means is a worse outcome than a slightly awkward signature.

The dry run must be honestly labelled as a *point-in-time* answer. The graph
moves; a preview taken and acted on ten minutes later reached a different set,
and an operator who believes otherwise will under-quarantine and not know it.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "blast_radius or quarantine"
    make all

**The premise was wrong, and the correction is the interesting part.** The entry
says *"`promotion_eligibility` already treats its result as a governance
trigger"* — meaning `get_blast_radius`'s result. It does not.
`promotion_eligibility.blast_radius_for` is its own statement:

    SELECT count(DISTINCT src_entity_id) FROM edges
     WHERE dst_entity_id = :eid AND rel IN ('depends_on','composes','provides_to') ...

One hop, three edge types, no visibility filter.
`GraphClosureCache.get_blast_radius` is a different thing entirely — transitive
to depth 5, cache-first, visibility-filtered, exposed over REST and MCP.

**So the two graph walks the entry warned about already exist — and they
disagree on purpose.** `blast_radius_for`'s docstring says why: *"A deeper
traversal would count transitively, but the count is a review threshold rather
than a correctness property, and a direct-dependant count is the one an owner
can verify by looking."* That is right for promotion, which asks "would enough
owners notice to warrant review". It is wrong for quarantine, which asks "what
content rests on this" — and a claim four hops away rests on withheld content
exactly as much as one hop away.

Collapsing them would have broken whichever one lost. They stay separate, and
both now say so.

**Which left a third option the entry did not consider: call the existing
traversal once per seed.** Not a second traversal, and not a widened one.
`get_blast_radius` is single-root by construction — `closure_cache` is keyed by
root and the CTE recurses from one — so teaching it about sets would rewrite a
cached path REST and MCP both serve, to answer a question an operator asks
occasionally. Per-seed invocation reuses the cache and agrees about what
"downstream" means by *being the same code*.

**`preview` now returns two sets that mean different things**, which is the
distinction an operator acts on:

- `matched` — the claims that would be withheld. Exact; the predicate decides it.
- `downstream` — what depends on those claims' subjects. **Advisory: applying
  the quarantine withholds none of it.**

**The seed cap is reported, not silent.** Fifty subjects, and the result carries
`seeds_traversed` beside `seeds_total` with a `truncated` property. A capped
downstream set that did not say it was capped would read as the answer. The same
property covers the no-traversal-wired case: zero seeds traversed out of one is
`truncated`, because an untraversed subject is not a subject with no dependants.

The point-in-time warning the entry asks for was already on `preview` from
E4-T2 and is extended to cover both sets.

### E4-T4 — Pre-quarantine of downstream receipts

**Kind:** task · **Status:** done · **Blocked by:** E4-T2 · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: a receipt whose inputs are being quarantined stops being servable before
the sweep reaches it, not after.

The ordering is the whole task. A quarantine that walks its blast radius one row
at a time serves the last unreached receipt right up until it arrives, which is
the window an incident response exists to close. Marking the downstream set
first and reconciling afterwards inverts that: the cost of being wrong becomes a
withheld receipt that should have been served, and withholding is the safe
direction.

E3-T2 built the vocabulary this needs. `hydration_state` already gates
`/exclusions` and `/references` behind a 409 `receipt_not_hydrated`, and
`HYDRATION_SERVABLE` is the frozenset deciding it. Whether pre-quarantine is a
fourth hydration state or a separate column is a real question — a receipt that
is fully hydrated but provisionally withheld is not the same thing as one that
was never hydrated, and collapsing them loses the difference an operator needs.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "receipt and quarantine"
    make all

**The ordering the entry asks for is unnecessary here, and that is the finding.**
It prescribes marking the downstream set first and reconciling afterwards, to
close the window in which a row-at-a-time sweep still serves the receipts it has
not reached. There is no such window: `apply` is **one transaction**. The claims
and the receipts that quoted them become withheld at the same instant and no
reader observes one without the other — strictly stronger than mark-first, and
without the marked-but-unreconciled state that design would add. "Mark first" is
a remedy for an incremental sweep this code does not do.

**The residual race is stated rather than papered over.** A resolution already
in flight can commit a receipt citing a claim a moment after this transaction
withholds it. Serving refuses the claim from that instant so no *new* resolution
can cite it, but a receipt recording a true past serving is not reached. That is
a reconciliation sweep's job and this task does not build one.

**A separate column, not a fourth `hydration_state`.** Three reasons, weakest
first: the states answer different questions ("has this finished recording what
it served" against "may this be shown right now"); `HYDRATION_SERVABLE` exists
precisely to make a fourth state expensive, and its docstring says so. And
decisively — **reversibility**. Pre-quarantine is provisional by definition, so
overwriting `hydration_state` would destroy the value to reconcile *back* to,
and un-withholding would have to guess between `complete` and `failed`. That is
the argument `claim_quarantine_members` already rests on.

`withheld_by` travels with `withheld_at` under a CHECK that moves them together.
Without it, releasing one incident's receipts would re-derive the set, and a
receipt reached by two open incidents would be released by whichever finished
first.

**The blocking discovery: the servability gate was in one router, and the second
transport went without it.** `api/routers/receipts.py` checked `hydration_state`
before serving. The four MCP tools over the same service reads checked nothing —
and `get_receipt_exclusions`'s docstring told its caller that *"an empty list
means nothing was withheld"*, which is exactly the belief the REST 409 exists to
prevent, since an unhydrated receipt has recorded no exclusions yet. One surface
refused and the other asserted the opposite about the same row.

**That is the third time** a guard written at a transport was missing from a
second transport over the same service — after E13-T5's checkpoint scan and
E3-T8's overdue guard. So the rule is now one function the reads call and the
transports only render, `GET /receipts/{id}` still publishes both states rather
than refusing (it is the surface a caller polls to learn to wait and an operator
reads to learn why), and the MCP header tool publishes them too, which it did
not before.

### E4-T8 — The quarantine mechanism is complete and reachable by nothing

**Kind:** task · **Status:** done · **Blocked by:** E4-T3, E4-T4 · **Hotspot:** no · **Repo:** contextplane

Goal: `QuarantineService` is invocable by an operator.

E4-T2 built apply/preview/revert, E4-T3 gave the preview a blast radius, E4-T4
made receipts follow. `grep -rn QuarantineService contextplane/` finds it in its
own module and nowhere else: **no REST route, no MCP tool, no entry in
`wiring/`.** Nothing in production constructs it, so nothing passes the
`blast_radius` or `receipts` collaborators either, and the whole mechanism is
tested and inert.

This is the pattern this plan keeps finding — `requires_validated`,
`queryRelationships`, `resolve_weights`, the E3-T4 filters — and it was authored
here rather than inherited, which is worse. Filed rather than folded into E4-T4
because a surface is its own decision: who may quarantine, whether preview and
apply are one call or two, and what an idempotency key means for an operation
whose predicate matches a moving set.

**E10-T1 depends on this, not on E4-T2.** Its entry lists E4-T2 and E4-T3 as
blockers, but a screen cannot call a service with no endpoint. Retarget it when
this lands.

Acceptance:
    .venv/bin/python -m pytest tests/conformance -q -k "tool_registry or parity"
    make all

**Three routes under `/v1/admin/claim-quarantines`, and three decisions a
surface makes permanent.**

**`preview` and `apply` are separate routes, not one route with a `dry_run`
flag.** A flag puts the decision to *look* and the decision to *withhold* on the
same request, so a caller who gets the boolean wrong withholds content by
accident — and withholding being consequential is the whole reason this surface
exists. Two paths cannot be confused by a boolean.

**No idempotency key on `apply`, deliberately.** A key exists so a retry after a
dropped response finds the first result rather than making a second one. That
model does not hold here: the predicate matches a *moving* set, so "the
identical request" does not identify an identical outcome, and a replayed key
would return a quarantine whose recorded membership no longer describes what a
re-run would withhold. Applying twice is already safe — `apply` refuses to
overwrite an existing `quarantined_at` — and the ledger then records two
incidents, which is what happened. A key would have made the second invisible.

**A predicate matching nothing is a `409`, not an empty success**, and a second
revert is a `409` rather than a `200` with zero: "already reverted" and "nothing
left to restore" are different facts about an incident.

**The wiring, and the gate that corrected its shape.** Both collaborators come
from the composition root and cannot come from anywhere else: the boundary
contract places `contextplane.context` above the service layer, so the memory
area may not import the receipt withholder, and the retrieval service that
answers the blast radius is built in an area it has no handle on. The first
attempt put that reasoning in `wiring/services.py` and pushed it past the
tighter 250-line ceiling that file carries — whose stated purpose is exactly
that *"adding a service to an existing area touches that area's `wiring.py` and
the container's field list, and nothing here."* The reasoning moved to
`service/memory/wiring.py`; the root keeps two arguments and a pointer.

**The test that distinguishes wired from half-configured.** A `QuarantineService`
built without collaborators still applies and reverts, so a test checking only
`quarantined_at` passes on a deployment whose receipts keep serving the withheld
content. `test_applying_through_the_route_also_withholds_the_receipts_that_quoted_it`
asserts the receipt read turns `409 receipt_withheld`; setting
`receipt_withholding=None` fails that test and nothing else. Measured.

### E4-T5 — ADR: materiality is not severity, and the word is already taken

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide what makes an incident *major*, on what evidence, and who can say
so — before a clock depends on the answer.

This is first among the DORA tasks because everything else keys off it. The
classification starts a regulatory clock with legal deadlines; a threshold
picked to make the demo work is a threshold that either starts clocks nobody can
meet or fails to start one that was required.

Three things the ADR must settle:

1. **The name.** `severity` is taken — it is the PII scanner's
   `advisory < warn < block`, in `types.py`, `admission.py` and
   `pii_scanner.py`. Use `materiality`. Two different orderings sharing one
   field name is a defect waiting for a reader who has only seen the other one.
2. **Who classifies.** Automatic classification from blast radius is
   attractive and dangerous: it means a graph traversal starts a legal clock. If
   it is automatic, the ADR states the threshold and where it is governed; if it
   is human, it states what happens between detection and classification,
   because that gap is unbounded and is itself the reportable delay.
3. **What "major" means here specifically.** DORA's thresholds are external
   and this service does not get to invent them. Cite them or state plainly
   that the mapping is a placeholder pending legal input — an invented threshold
   presented as a compliance feature is worse than an absent one.

Acceptance:
    make doc-refs doc-links
    make all

**Delivered as [ADR-0015](../adr/0015-materiality-is-not-severity.md), and there
were two naming collisions rather than one.**

`severity` was the known one. **`incident` is also taken, twice** — as a
`LIFECYCLE_REFERENCE_KINDS` entry and as an `evidence_kind` in
`memory_claim_provenance`'s CHECK constraint. In both it means an *external*
operational incident that something points at or cites. So "auto-created
incident case" would have given a word already carrying two meanings a third,
one of which is enforced by the database. The governed object is a
`reporting_obligation` — named for what is tracked rather than what triggered
it, which composes with the existing meaning instead of fighting it.

**On who classifies, the ADR splits the question the task posed as binary.**
Automatic classification is refused as the *classifier* and kept as the
*nominator*: crossing the blast-radius threshold creates the obligation in
`open` with `materiality: unclassified` and starts a **nomination-age gauge**.
Nominating says somebody should look; classifying starts a legal obligation, and
a graph traversal is qualified for the first only. This also names the gap the
task warned about — the delay between detection and classification is measured
rather than unbounded, because that delay is itself the reportable one.

`unclassified` is a state, not a null, for the reason `_declared_sensitivity`
and migration 0069's `pending` default both exist: a missing value reads as "not
applicable" to every filter, which is the permissive direction taken by
omission.

**The threshold placeholder is structural rather than a TODO.** Until a ratified
set is installed, nomination runs but there is no automatic path to `major` at
all. A TODO comment is invisible to an operator reading a dashboard that says
`materiality: major`.

The dissent is worth reading before E4-T6 starts: without thresholds there is no
clock, and a fair reading is that E4's DORA half should be deferred wholesale
rather than half-built against a placeholder. The counter — nomination, the
obligation record and the gauge are useful anyway — is true, and is also exactly
what somebody would say while building the wrong thing.

### E4-T6 — The notification clock, and why a missed deadline must be loud

**Kind:** task · **Status:** blocked — on a decision nobody here can make · **Blocked by:** E4-T5, E4-T5b, ratified DORA thresholds · **Hotspot:** no · **Repo:** contextplane

**Picked up, and stopped before writing code. ADR-0015's own dissent is why:**

> *"Without thresholds there is no clock, without a clock there are no deadlines,
> and E4-T6 and E4-T7 are building machinery around a classification that cannot
> currently be made. A fair reading is that E4's DORA half should be deferred
> wholesale until legal input arrives, rather than half-built against a
> placeholder — and that this decision's careful structure is a way of appearing
> to make progress on something blocked."*

This entry asks for deadlines stamped **at classification time**. Nothing can
classify: DORA's thresholds are external, this service does not get to invent
them, and E4-T5 recorded that the mapping waits for legal input. Building the
clock now would produce deadline machinery that never fires — a mechanism
nothing consults, which is the defect this plan has caught five times already
and would this time have authored deliberately.

The dissent's counter — that the obligation record and the delay gauge are
useful without thresholds — is true, and it is also, in its own words, *"exactly
what someone would say while building the wrong thing."* So the useful part is
cut out as **E4-T5b** rather than smuggled in under a task whose goal needs the
clock.

**What unblocks this:** a ratified threshold set, from legal. Not a date, not a
volume of internal usage. When it arrives, this entry is buildable as written.

Acceptance (unchanged, for when it unblocks):
    .venv/bin/python -m pytest tests/integration -q -k "incident or deadline"
    make all

### E4-T5b — The obligation record ADR-0015 decided, which nothing implemented

**Kind:** task · **Status:** done · **Blocked by:** E4-T5 · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: `reporting_obligation` exists, with `materiality` including `unclassified`,
and the delay gauge that makes an unclassified backlog visible.

**Found by picking up E4-T6 and grepping for what it presumed.**
`reporting_obligation` and `materiality` appear **nowhere in the codebase**.
E4-T5 is marked done and was — an ADR task delivers a decision — but nothing
implemented it, and no task covered the implementation. That is the second time
this session: E5-T1 decided the `derived` status and E5-T1b had to be filed to
build it.

The pattern is worth naming rather than fixing twice more. **An ADR task's
"done" means the decision is recorded, never that the code exists.** Where an
ADR's Consequences section names artefacts, those artefacts need a task, and the
plan currently creates one only when somebody trips over the gap.

What this task is, exactly — ADR-0015's Consequences name all of it:

- The `reporting_obligation` record, named for what is tracked rather than for
  what triggered it. **Not** `incident`: that word is already taken twice — a
  `LIFECYCLE_REFERENCE_KINDS` entry and an `evidence_kind` in
  `memory_claim_provenance`'s CHECK, where it means an *external* operational
  incident something points at.
- `materiality`, **not** `severity`. That word is the PII scanner's
  `advisory < warn < block` in three modules, and two orderings sharing one
  field name is a defect waiting for a reader who has only seen the other.
- `unclassified` as a first-class state — *"the state most obligations are in
  most of the time"* — and a gauge **whose healthy value is not zero**.

Deliberately **not** in scope: any automatic classification. That needs the
thresholds E4-T6 is blocked on, and a placeholder threshold presented as a
compliance feature is worse than an absent one.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "obligation"
    make all

**Shipped: the record, the states, the gauge, and a route that reaches all
three.** Exposed in the same change that creates it, because a governed record
nothing can reach is the defect this plan has now found five times.

Three things the entry did not have to say and that the build settled:

- **The gauges are deployment-wide and unlabelled.** A `tenant_id` label was the
  first attempt and `tests/conformance/test_metric_surface.py` refused it:
  one tenant label turns one metric into one series per tenant, and the
  Prometheus that dies from it dies months later under load. So the per-tenant
  figure is a *read* and the metric is a deployment-wide *observation*, and a
  test asserts the read publishes nothing.
- **A scheduled observer that silently stops is the known risk**, and it is
  worse for a gauge than a worker: the value sits at its last reading looking
  healthy. The age gauge is the defence — a stalled job freezes the age, and an
  age that stops advancing while the count is non-zero is the signal that the
  observer is the problem.
- **`wiring/jobs.py` had two lines of headroom** under its 800-line ceiling and
  could not take another registration. Rather than widen the allowlist, the
  seventeen `_describe_*` report formatters moved to `wiring/job_summaries.py`
  — they are not composition, and none of them knows a scheduler exists — and
  governance now registers its own job through `register_governance_jobs`,
  which is what the ceiling is for.

Classification stays manual, and `E4-T6` stays blocked on the thresholds that
would change that.

### E4-T5c — A reserved-vocabulary gate, so the next collision is caught by a machine

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a governed noun that already means something else fails a gate, not a
grep.

ADR-0015's third dissent asks for this directly:

> *"The naming decisions rest on collisions found by grep, and grep found two.
> There is no gate preventing a third, and the next author to introduce a
> governed noun will do exactly what E4 did — reach for the obvious word. The
> durable fix is a reserved-vocabulary check, not three ADRs noticing collisions
> one at a time."*

Two collisions are already documented — `severity` and `incident` — and the
second is enforced by a database CHECK, so a third would be found the same way:
late, by someone reading carefully.

The check is cheap and the shape is settled by this repo's own gates: read the
reserved words from where they are already defined rather than restating them,
refuse a new governed object or column reusing one, and require a per-entry
written reason for any deliberate exception — the pattern
`check_governed_magnitudes.py` and `check_file_sizes.py` both use.

ADR-0015 says a lint rule "would be cheap and is not part of this decision".
Filed so it stops being nobody's.

Acceptance:
    make all



Goal: classification-as-major stamps initial, intermediate and final deadlines
on the incident case, and their approach and breach are visible without anybody
asking.

Three deadlines, stamped as distinct instants at classification time rather than
computed on read. Computed deadlines drift when the classification timestamp is
corrected, and "when was this due" is precisely the question an audit asks.

The case machinery exists: `CASE_OPEN`/`CASE_ROUTED`/`CASE_RESOLVED`, with a
disposition recording its approver at disposition time — the same discipline
this needs.

**At-risk escalation is a gauge, not a log line**, and ADR-0012's assumption 2
is the precedent: a scheduled job that silently stops turns a deadline into
"whenever somebody looks". Anchor age was the analogous case there. Deadline
state must be observable when nothing is happening, because nothing happening is
the failure.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "incident or deadline"
    make all

**Shipped with an empty allowlist, which is the part worth noting.** Nothing on
the scanned surfaces violated it, so every entry that ever appears in
`ALLOWLIST` will be a decision somebody wrote a reason for rather than debt
somebody inherited.

Scope is the two surfaces where a second meaning does damage: wire schemas —
ADR-0015's refusal was about a *field* named `severity`, and the wire name is the
one a UI author reads — and migration columns and CHECK values, which is where
`incident` already lives twice. Internal locals are deliberately out of scope: a
`severity` local inside the PII scanner is that module's own word for its own
concept, and a gate that fired on it would be switched off within a week.

The reservations are checked against their owners rather than restated, so a
reserved word whose meaning moves fails instead of quietly reserving a word
nothing uses.

### E4-T7 — Evidence-bundle export, scoped to one case

**Kind:** task · **Status:** pending · **Blocked by:** E4-T6 · **Hotspot:** no · **Repo:** contextplane

Goal: everything a regulator asks for about one incident, exported as one
bundle, with the scope enforced rather than described.

`arc_get_review_package` is the closest shipped thing and is worth reading
before designing this, but it is not the same: a review package supports a
decision that is about to be made, and an evidence bundle documents one already
taken. The audiences differ and so does what may be included.

The scoping is the hard part and it cuts both ways. A bundle that quietly
includes rows outside the case is a disclosure; one that quietly omits rows
inside it is an incomplete regulatory filing. Neither can be checked by reading
the output, so the scope predicate belongs in the query and the test belongs on
the boundary — a second case's rows seeded alongside, and asserted absent.

Note what the digest chains do and do not offer here. ADR-0012 is explicit that
an internal chain proves nothing against the party holding the storage, and that
the honest phrase is bounded-exposure tamper-evidence. An evidence bundle must
not imply more than that, and must never use the word non-repudiation — the ADR
says why, and says it expecting this task to be where somebody is tempted.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "evidence_bundle"
## Task decomposition — eleventh wave (E13, whose headline target E7 already met)

E13 is measured against three tracked metrics, and grounding them first changes
what the epic is for. Two are now measurable exactly, because E7-T1 committed
the registry that measures them.

**Default-profile tool count, target ≤ 8: already met, at exactly 8.**
`tool_registry.json` records `core_count: 8` against `tool_count: 67`, and
`install_surface_filter` makes a default connection expose only the core tier.
E13 does not have to shrink this. It has to keep it shrunk, which is a gate
rather than a project — and the gate exists, since the registry is checked
against the code in both directions by `make lint`.

**REST endpoints an agent integration must know, target ≤ 6: currently 8.** The
eight core tools map to eight distinct operations, one each. Two over, and the
pairs worth examining are visible: `search_capabilities` (`GET /v1/search`)
beside `get_capability` (`GET /v1/capabilities/{id}`), and `resume_context`
beside `registry_resolve_context`. Each pair is a search-and-read or a
resolve-and-resume over one subject. Whether either collapses is a real
question; the point is that the gap is two and both candidates are nameable, so
this is a decision rather than a search.

For scale: the full REST surface is **191 paths, 243 operations**. The target
was never about that number — it is about how much an *agent integration* must
learn, and the core tier is what defines that.

**Deprecated-surface count trending to zero: this metric cannot start.** Two
findings, and both are amendments rather than work.

*First, the dual-alias window contradicts a standing project constraint.* This
is a greenfield repository with no released version and no external consumers.
A dual-alias window exists to protect integrations that already exist, and there
are none. Building one would be building a compatibility mechanism for a
compatibility problem the project has decided it does not have. **The clause is
struck**: surfaces that consolidate are replaced, not aliased. If that ever
becomes wrong, it becomes wrong on the day something ships, and that is when the
window gets designed against real consumers.

*Second, "retire MCP tools that the registry shows unused" has no data source.*
The registry's tool records carry `name`, `module`, `tier`, `rest` — and no
usage field. `install_tool_metrics` does instrument every tool, by rebinding the
decorator factory so that instrumentation cannot be forgotten per tool, but that
is a Prometheus counter: ephemeral, scraped, and explicitly not something a
browser may read. More fundamentally, E7-T1 already recorded why this corpus
does not exist — *"this service has never been released, and the receipts in a
development tree are the test suite's."* Retiring tools on usage evidence is
blocked on the same missing corpus that made E7-T1 derive its core set from a
stated rule instead of a measurement.

### E13-T1 — The two REST operations over budget, decided rather than searched

**Kind:** task · **Status:** done · **Blocked by:** E7-T2 · **Hotspot:** no · **Repo:** contextplane

Goal: get the agent-facing REST surface from eight operations to six, or record
why one of the pairs must stay two.

Both candidates are already named above. Take each on its merits:

`search_capabilities` and `get_capability` are a search and a read over the same
subject. Collapsing them means a search that can return one fully-hydrated
result, which is a real API design and also a way to make the common read pay
for the search path.

`registry_resolve_context` and `resume_context` are closer to genuinely
different: one assembles context for a new question, the other continues an
established one. If they stay two, the entry says so and the target moves to
seven with a reason, rather than the target quietly not being met.

**Do not collapse by adding a mode parameter.** Two operations behind one path
with a discriminator is the same two operations plus a branch, and it makes the
count look met while the thing an integrator must learn is unchanged — which is
what the metric was measuring.

Acceptance:
    .venv/bin/python -m pytest tests/conformance -q -k "parity"
    make all

**Decided. The metric's unit was never defined, and that is most of the
answer.**

"REST endpoints an agent integration must know" counts **8 operations over 7
paths** — `record_session_event` and `list_session_events` are POST and GET on
the same `/v1/memory/sessions/{session_id}/events`. Nobody had said which of the
two numbers the target of 6 was about, and the difference is a whole unit.

**The unit is paths**, because the metric measures what an integrator has to
learn, and a path with two methods on it is one thing to learn. So the figure is
**7 against a target of 6**.

**Both candidate pairs stay two, and the reasons are different.**

*`search_capabilities` and `get_capability`.* A search returns ranked matches;
a read returns one entity with its facts. Collapsing them means either the
search always hydrates — paying the read's cost on every query — or it takes a
flag, and this entry already refuses the flag: two operations behind one path
with a discriminator is the same two operations plus a branch, and it makes the
count look met while what an integrator must learn is unchanged. Re-shaping to
`GET /v1/capabilities` plus `GET /v1/capabilities/{id}` is tidier REST and the
same two paths.

*`registry_resolve_context` and `resume_context`.* Closer to genuinely
different, and they are. Resolve assembles a four-block context envelope for a
new question. Resume returns a bounded checkpoint window with open questions and
a next action — a different shape, not a mode of the first. The workspace arm of
`resolve` overlaps in *source*, not in what it answers.

**So the target moves to 7, which this entry anticipated.** It said: "If they
stay two, the entry says so and the target moves to seven with a reason, rather
than the target quietly not being met." That is what happened, arrived at from
the other direction — not by collapsing a pair from 8, but by counting paths
rather than operations in the first place.

Seven is the floor without dropping a capability. `whoami`, the two context
verbs, the session-event path, claim search, capability search and capability
read: nothing there is reachable from another, and dropping any of them means an
agent reaching for a second surface to complete one turn — which is the rule the
core tier was chosen by.

**E13-T4 should ratchet paths, not operations**, and at 7. A ratchet on
operations would pass while somebody added a third method to an existing path,
which is exactly the growth an integrator feels.

### E13-T2 — The five observational write verbs, and what they actually share

**Kind:** task · **Status:** done · **Blocked by:** E13-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: decide which of the five write verbs overlap enough to consolidate, on
evidence rather than on the fact that they are all writes.

They are `assert_claim` (memory_curation), `record_session_event` (memory),
`add_workspace_entry` (workspace), `append_intent_checkpoint` (intent_memory),
`ingest_signal` (signals) — five verbs in five modules, which is itself a
signal that they were built as five domains rather than one path with five
shapes.

**The rule from ADR-0011 applies here almost verbatim, and it is the reason this
task is not obviously a good idea.** That decision refused to fuse the context
envelope's blocks because they are four *authority classes*, and the rule it
left was: *fuse within an authority class, never across one*. A session event, a
staged claim, and a workspace note are not the same authority class either — one
is an observation, one is an assertion entering a governed lifecycle, one is
somebody's note. A single write verb over all three either loses that
distinction or carries a discriminator that reintroduces it, and the second is
five verbs wearing one name.

So the honest output may be that two of the five consolidate and three do not.
The epic's own rule — *no consolidation may drop a governance property* — is the
test, and provenance completeness is the property most at risk: `assert_claim`
enters the derivation and confidence machinery, `record_session_event` does not.

Acceptance:
    .venv/bin/python -m pytest tests/conformance -q
    make all

**Decided: none of the five consolidate, and the grounding turned up a defect
that matters more than the decision.**

The five differ on **five distinct authorization models**, which settles it
before ADR-0011's authority-class rule is even reached:

| verb | PII scan | evidence | authorized by | idempotency | mutability |
|---|---|---|---|---|---|
| `assert_claim` | containment **and** PII, via `stage_claim_defended` | **≥1 required**, closed `kind` vocabulary | tenant | key on REST | staged, promotable |
| `record_session_event` | `admit_or_refuse`, session-event field type | none | your own session | none | immutable |
| `add_workspace_entry` | `_scan_field`, three outcomes | optional references | workspace membership | none | mutable |
| `append_intent_checkpoint` | **none** | none | participation grant | **key required** | append-only chain |
| `ingest_signal` | `admit_or_refuse` | envelope references | ingest role | — | — |

A single verb over these is one verb with five authorization branches, and the
branch *is* the discriminator E13-T1 already refused: the same surfaces plus a
switch, with the count looking smaller and nothing an integrator learns getting
shorter.

`assert_claim` is furthest from the rest and it is the epic's own rule that says
so. It is the only one requiring evidence, the only one running directive
containment, and the only one entering a lifecycle where promotion — reviewed
later, by a different actor — can move a value onto the canonical graph. Folding
it into anything drops provenance completeness, which E13 forbids by name.

**The defect: `append_intent_checkpoint` is the one write verb that does not
scan for PII, on either transport.** Filed as E13-T5 below. It is the same shape
as a bug this codebase already fixed once — `record_session_event`'s docstring
records that it "called `record_event` directly and scanned nothing, while this
tool's own docstring told agents it did" — except the checkpoint tool never
claimed to scan, so nothing contradicted it and nobody looked.

### E13-T5 — Checkpoints are agent-written free text, unscanned, and served to a second agent

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: `append_intent_checkpoint` scans before storage, like the other four
observational writes do.

`goal`, `decisions`, `assumptions`, `completed_checks`, `open_questions` and
`next_action` are free text an agent composes. Neither
`api/mcp/tools/intent_memory.py` nor `api/routers/intent_memory.py` nor
`workspaces/checkpoints.py` calls `admit_or_refuse`, `scan_for_pii` or any
scanner — the only "scan" in that area is prose about something else.

**Why this is worse than an unscanned note.** A checkpoint is the resume
surface: `resume_context` serves its content to whoever picks the task up next.
So unscanned text written by one agent is served to another, which is the
crossing a scan on the workspace entry beside it is there to prevent.

Two things to settle rather than assume:

- **Which field type.** `PII_FIELD_TYPE_SESSION_EVENT` is the closest existing
  one and may simply be right; if a checkpoint needs its own policy, say what
  differs rather than adding a constant that reads as a distinction.
- **Which fields.** All six are agent-authored, but `metadata` on a session
  event is deliberately *not* scanned and its tool docstring says so loudly. If
  any checkpoint field is meant to be the same, it needs the same warning in the
  same place.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "intent_memory and pii"
    make all

**Both settled, and neither the way the entry guessed.**

**Which field type: two new ones, not the session-event constant.** The module
that owns the vocabulary already answers this — *"Classification attaches to a
field, not to a module: two surfaces writing the same field carry the same
obligation."* A checkpoint is not a session event, and reusing
`memory_session_event.body` would mean a tenant cannot state a policy about one
without stating it about the other. So `intent_checkpoint.body` and
`intent_checkpoint.references`, split for the reason the signal pair is split:
the evidence array carries `authorized_uri`, which is separately authored and is
a real token channel. That split matters more here than for signals, because
`authorized_uri` is deliberately omitted from the checkpoint digest material — a
digest names what a checkpoint *means*, and a scan is about what its bytes
*contain*.

**Which fields: all of them, and no warning is needed.** The premise behind the
second question was wrong. A checkpoint has no counterpart to a session event's
`metadata`: `CLIENT_FIELDS` is closed to content, so "every client field" needs
no carve-out and there is nothing that was admitted on the understanding it goes
unscanned.

**Where it went: the service, not either route.** Both transports already call
`assert_participant` before calling in, so authorization still precedes the
scan — but a transport-level scan is one a second transport can be written
without, and that is precisely how this path acquired two surfaces and no scan.

**And before the task lock.** The lock serializes every append to one task;
holding it across a detector sweep would make append throughput a function of
scan cost. Nothing the scan decides depends on task state, so nothing is lost by
refusing first. `test_the_scan_runs_before_the_task_lock_is_taken` pins it.

**Nowhere had recorded the omission as deliberate.** Seven pilot field types
covered every other write. That is what made this a gap rather than a decision.

### E13-T3 — A usage signal that could justify retiring anything

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide what evidence would justify retiring a tool, and either build the
thing that produces it or record that retirement waits for a release.

This is the task the epic assumed away. `install_tool_metrics` gives a per-tool
Prometheus counter, which answers "was this called in the current scrape window"
and not "has any agent ever needed this". The registry carries no usage field.
And E7-T1 already established that the corpus does not exist because nothing has
shipped.

Three options, and picking one is the deliverable:

- **Wait for a release.** Honest, and it makes E13's third metric explicitly
  blocked rather than quietly unmeasured. Cheapest, and probably right.
- **Persist per-tool call counts** beyond the scrape window, tenant-scoped.
  Real data, but it is a new retention question about a new record class —
  E6-T2 established that retention is keyed on record class with a legal basis,
  so this is not a free table.
- **Derive from receipts.** Receipts record what a resolution served, not which
  tool the caller invoked, so this measures something adjacent and would need to
  say so.

Whichever is chosen, the anti-pattern to refuse is retiring on *absence of
evidence* — a tool nobody called during a development tree's test runs is not a
tool nobody needs, and the registry comment already says why.

Acceptance:
    make all

**Decided: wait for a release.** E13's third metric — deprecated-surface count
trending to zero — is **explicitly blocked**, not quietly unmeasured, and that
is the whole deliverable.

*Why not persist per-tool call counts.* It is the option that looks like
progress and is the most expensive wrong turn available. A per-tool counter
table is a new record class, so under E6-T2's framework it needs a
`legal_basis`, a `retention_days` and an `erasure_mode` before it may store
anything — and E6-T3 has just established that the last record class added
outside that framework advertises a period nothing enforces. Building a
retention obligation in order to measure a metric nobody can act on yet inverts
the cost.

It is also the option that would produce a *number* before it produces
*evidence*, and a number is what gets acted on. Six months of development-tree
call counts would show every extended tool at zero, which is true and means
nothing — no agent has ever connected.

*Why not derive from receipts.* Receipts record what a resolution served, not
which tool a caller invoked. That measures an adjacent thing, and the adjacency
is exactly where a retirement decision would go wrong: a tool can be essential
and appear in no receipt, because not every tool resolves context.

*What "wait" concretely means*, so this is a decision and not a deferral:

1. The metric is marked blocked in E13's epic body, with this task as the
   reason. An unmeasured metric that nobody has declared blocked reads as a
   metric somebody forgot.
2. **Retirement on absence of evidence is refused now, in writing**, rather
   than left as a temptation for whoever first looks at a Prometheus dashboard.
   `install_tool_metrics` gives a per-tool counter, and that counter answers
   "was this called in the current scrape window" — never "has any agent ever
   needed this". The two are indistinguishable on a graph.
3. The trigger to revisit is a *release with real connections*, not a date and
   not a volume of internal usage. E7-T1 already had to make this exact
   substitution once, deriving its core set from a stated rule because "this
   service has never been released, and the receipts in a development tree are
   the test suite's". The same corpus is still missing, and it is the same
   corpus.

E13-T4's ratchet is unaffected: it gates the two metrics that *are* measurable —
core tool count and the core tier's REST footprint — and those need no usage
data at all.

### E13-T4 — The consolidation gate, so the counts cannot drift back

**Kind:** task · **Status:** done · **Blocked by:** E13-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: the two metrics that are now measurable become gates, at the numbers
actually achieved.

E7-T1's registry gate already holds the tool list against the code in both
directions. This adds the budgets: core tier at most 8, and the distinct REST
operations the core tier maps to at most whatever T1 lands on.

Ratchet, not a fixed target — the same shape as the undocumented-extended-tools
ratchet E7-T2 landed. A number that can only go down is a gate somebody has to
argue with to weaken; a target in a document is a number that drifts and is
noticed a year later.

The value of this task is entirely in it existing before the counts are hit,
rather than after. E13's stated purpose is that *simplicity is subtraction*, and
subtraction without a ratchet is a one-time cleanup that grows back.

Acceptance:
    make lint

**Delivered as two ratchets inside the existing registry gate**, not a second
script. That gate already knows what the core tier is; a separate check would be
a second place the definition lives.

`_CORE_TOOL_CEILING = 8` and `_CORE_PATH_CEILING = 7`. Both set at what is
*achieved* — the tool count met its target, and the path count is E13-T1's floor
rather than E13's unreachable six. A ratchet holding an aspiration nothing can
satisfy is a failing build, not a gate.

**Paths, not operations**, per E13-T1, and mutation-testing confirmed the
distinction is real rather than pedantic. Promoting an extended tool that
introduces a new path trips *both* ratchets. Promoting one that adds a method to
a path already in the set trips only the tool ratchet — which is correct: an
integrator learning `/v1/memory/sessions/{id}/events` learns it once whether it
carries one method or three. An operation ratchet would have flagged that as
surface growth and missed nothing an integrator feels.

The failure message says "lower the ratchet when the count drops — never raise
it to fit", because the one way this gate becomes decoration is somebody
adjusting the number instead of the surface.    make all
### E5-T1b — ADR-0014 decided a third status; nothing implemented it

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: `derived` exists in the two places ADR-0014 named, so E5-T2 can register a
sampling parameter as what it is.

**Found while grounding E5-T2, and it blocked it.** E5-T1 is marked done, and it
was — an ADR task delivers a decision. But `ranking.py` still read
`_VALIDATION_STATUSES = frozenset({"validated", "grandfathered"})` and gated on
`status != "validated"`, and `scripts/check_governed_magnitudes.py` still read
`_STATUSES = ("validated", "grandfathered")`. So the status E5-T2 must record
its parameters under did not exist, and an entry using it would have refused the
whole registry at import.

No task covered the implementation. The ADR's own Consequences section named
both halves and said **"both halves move together or the protection is
one-sided"** — which is precisely what had happened, except that neither half
had moved.

**What landed.** Both halves, together:

- `derived` joins the status vocabulary in the loader and in the gate.
- It requires `derived_from` and `derivation`, mirroring the four-field rule for
  `validated`. A derivation nobody can reproduce is a number with a nicer word
  on it.
- `requires_validated` is satisfied by a named set, `_GATE_SATISFYING`, rather
  than by a comparison against one literal — so a fourth status cannot be added
  without somebody deciding which side of that line it falls on. `derived`
  qualifies because a reproducible derivation is a stronger warrant than a
  validation run once; **`grandfathered` still never does**, and that has its own
  test, because adding a status the gate accepts is only safe while the one it
  must keep out still fails.

Mutation-checked: widening `_GATE_SATISFYING` to include `grandfathered` fails
three tests.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "ranking_registry or governed_magnitudes"
    make all

### E5-T2 — The SamplingPolicy, keyed on a tuple two-thirds of which exists

**Kind:** task · **Status:** done · **Blocked by:** E5-T1, E5-T1b · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: one governed sampling policy per (tenant, action class, sensitivity tier),
with the sampling parameters derived from a stated risk tolerance rather than
chosen.

Two of the three key components already exist and must be reused, not
redeclared. `sensitivity.TIERS` is `("public", "internal", "confidential",
"restricted")` and is already the source of a generated CHECK constraint in
migration 0068 — do the same here rather than writing the four values out.
`action_class` exists in ARC's scope vocabulary; reuse it or justify a second
axis in the entry.

E1's audit is the precedent for the unknown-value case: a host sending an
unrecognised sensitivity tier escaped every rule that named one, and
`_declared_sensitivity` closes it by reading unknown as *most restrictive*. A
sampling policy keyed on a tier must fail the same way — an unknown tier gets
the heaviest sampling, never the default.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "sampling"
    make all

**The title was right: two-thirds of the key existed, and the third does not.
Both halves moved, and the entry invited exactly that — "reuse it or justify a
second axis."**

**Action class became claim category.** ARC's `ActionClass` is `merge`,
`deploy`, `production_configuration_mutation`, `secret_release`, `data_export`
— governed *actions an agent takes*. This queue holds *claims awaiting
adjudication*, and a claim is not an action; keying a review budget on it would
key on a dimension no queued row carries. There is a second, independent
reason: the layer contract places `arc` above `service`, so `service.memory`
may not import `ActionClass` at all. `CLAIM_CATEGORIES` is closed, on this
layer, already a column on `memory_claims`, and already what decay keys on.

**The sensitivity tier is absent, and that is the finding.** E1's handling tier
is declared on *streams*: `memory_source_namespaces` is keyed on
`(tenant_id, source_system, source_namespace)`. A `memory_claims` row has a
`namespace` and **no `source_system`**, so it cannot reach that table. There is
no tier to select a policy by, and inventing one inside a sampling policy would
have manufactured a governance fact. A policy keyed on a column nobody can
populate is a policy nobody can select. Giving claims a derivable tier is a real
change and its own task.

**The sample size is derived, and it is the first entry the third status was
added for.** For a zero-acceptance plan `n >= ln(beta) / ln(1 - p)`; at a 1%
tolerance and 5% consumer's risk that is 299, which is the governed
unconfigured floor. `requires_validated: true` on that entry is satisfied by
`derived` — the rule `_GATE_SATISFYING` was named for in E5-T1b.

**Three things checked rather than assumed.** The module *reads* the registry at
import, so `coupling: consumed` is true rather than claimed — a governed
magnitude nothing reads is governed in name only. The stored `min_sample` is
re-derived on every load and a row whose three numbers disagree is refused, so a
budget edited in the database cannot serve as though it were derived. And the
category CHECK is generated from `CLAIM_CATEGORIES`, so a second write path
cannot introduce a value the service would have refused.

**ADR-0014's dissent is carried into the code, not dropped.** The arithmetic is
exact for a *representative draw*. This queue is ranked and E5-T4 will let a
policy dispose of items without a human, so the reviewed subset is not a random
sample and the true consumer's risk is not the one this number was derived
against. It is a floor on effort, **not** a guarantee about the residue, and the
module says so where a caller reads it.

**A governance pin caught a design error.** Two registry tests assert every
shipped magnitude is `grandfathered`, true of all seven until this one. The
tempting fix was widening the assertion; the test's own docstring forbids it —
*"delete the assertion for that id and record the evidence, not to relax the
rule"* — because widening lets the *next* entry claim `derived` without anybody
deciding. The exception is named by id with its reason, and a third test keeps
that list honest: an exempted id must still be in the registry and still not be
`grandfathered`, because a stale exemption reads as a live waiver.

### E5-T3 — The ranked queue, and the starvation it introduces

**Kind:** task · **Status:** done — shipped in #109, with a defect found on the way · **Blocked by:** E5-T2 · **Hotspot:** no · **Repo:** contextplane

Goal: the review queue orders by leverage and sampling priority instead of
arrival time, without any item becoming unreachable.

**The starvation is created by this task, not inherited.** FIFO cannot starve
anything; a ranked queue can, and this one has a feedback loop built into it.
Confidence decays with age, a decayed claim ranks lower, a claim that never gets
reviewed never has its confidence refreshed, and it decays further. The item
sinks because it sank.

`DECAY_FLOOR` does not fix this and it is important to see why, because it looks
like it should. The floor bounds the decayed *value* — `DECAY_FLOOR + (stored -
DECAY_FLOOR) * 2^(-age/half_life)` asymptotes to 0.10 rather than to zero — so a
claim never decays out of existence. But *rank* is relative, and an item pinned
at the floor is below every item that has not decayed. The value is bounded and
the position is not.

Whatever fixes it — an age term in the ordering, a reserved fraction of the
queue, a hard maximum wait — must be stated as a property with a number, and
that number is a governed magnitude like the rest. "We also consider age" is not
checkable.

Consequence preview belongs here rather than in its own task: a rank a reviewer
cannot interrogate is a rank they will learn to ignore, and `get_blast_radius`
already produces the material.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "queue and (rank or starv)"
    make all

**Shipped, and the way it nearly did not is the part worth keeping.** The
ranked query carried two defects only a database could see: `_RANK_JOIN`
concatenated onto the finished base query, which put a `LEFT JOIN` after the
`WHERE` it must precede, and `_RANK_COLUMNS` never imported at all — so the
`ORDER BY` and the keyset cursor both named columns the `SELECT` did not
produce.

**Every unit test passed throughout**, because none of them executes SQL. The
integration tier caught both the first time it ran the path, which is exactly
what that tier is for and exactly why it is not in the coverage gate.

So the fix was not two lines. `_QUEUE_BASE` split into `_QUEUE_SELECT` plus the
predicate, because the ranked query adds columns to one and a join to the other
and each has exactly one legal position; `backlog_predicate` takes the join as a
parameter rather than leaving a caller to append it. And an integration test now
asserts the ordering this queue exists to provide — an escalated claim ahead of
a recent one, end to end through the route — because nothing did.

### E5-T3b — `curation_queue.py` holds two concerns, and the ceiling found the seam

**Kind:** task · **Status:** done · **Blocked by:** E5-T3 · **Hotspot:** no · **Repo:** contextplane

Goal: the curation-case lifecycle lives beside the queue read, not inside it.

E5-T3 pushed the file past the 800-line ceiling. The ordering it added was
extracted to `curation_ranking.py` — that was the seam the *new* code created,
and taking it is what kept the overshoot to 29 lines instead of 111. The file is
on the allowlist for the remainder, with that reason.

What the ceiling is now pointing at is an **older** seam. The module holds a
read-only queue *and* a read-write case lifecycle — `open_case`, `route_case`,
`record_disposition`, `case`, `cases_for`, plus `CurationCase`,
`DispositionPolicy` and the `CASE_*` vocabulary. `CurationQueueService`'s own
class docstring already draws the line: *"Reads only. Acting on an item goes
through the service that owns that decision, so the queue cannot become a second
write path into claims."* The cases half is precisely that second write path,
living in the same file as the sentence denying it.

Roughly 300 lines, touching both transports. Deliberately not bundled into a
ranking rewrite: one diff carrying both would be reviewable as neither.

**Do it before E5-T4, not after.** That task adds `disposition_actor` to the
case lifecycle, so it lands squarely in the half that should have moved — and
doing it in the wrong file first makes the move bigger and the history harder to
read.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "curation"
    make all

**Split, and the allowlist entry drained rather than reworded.**
`curation_queue.py` is 300 lines and `curation_cases.py` is 584 — both under the
ceiling, so the file-size allowlist loses its only memory-area entry.

`CurationCaseService` is a separate class, not a second module holding the same
one. That is the point: `CurationQueueService`'s docstring promises *"reads
only… so the queue cannot become a second write path into claims"*, and a class
cannot promise that while holding `record_disposition`. Both transports reach
the write half through their own accessor, so a read route cannot arrive at a
disposition by holding the wrong object.

The tests moved with it rather than being pointed at a compatibility shim:
fourteen unit tests construct `CurationCaseService` now, and the router and MCP
suites pass one mock under both names, because what they assert is what the
transport does and not which service held the method.

### E5-T4 — `disposition_actor`, and what changes once a policy can dispose

**Kind:** task · **Status:** done · **Blocked by:** E5-T2 · **Hotspot:** no · **Repo:** contextplane

Goal: every disposition records whether a human or a policy made it, as a first
class field rather than something inferred from an actor id.

The case machinery is most of the way there. A disposition is already a
*proposal* rather than a write, and it already records **who may approve it, at
disposition time rather than inferred later** — which is exactly the discipline
this needs, applied to a different question.

The part that needs care is what a policy-automated disposition means for the
sampling math. Acceptance sampling assumes the sample is *inspected*; if a
policy disposes of an item, that item was not inspected, and counting it as a
reviewed sample inflates the measured quality of a queue nobody looked at.
Either policy dispositions are excluded from the sample or they are a separate
stream with their own acceptance criteria, and the entry should say which and
why. This is the failure mode where the number keeps looking fine.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "disposition"
    make all

**The entry asked which of two sampling treatments, and why. Policy dispositions
are excluded from the sample.**

The alternative — a separate stream with its own acceptance criteria — needs a
defect tolerance and a consumer's risk *for automated disposal*, and nobody has
measured either. Writing them would be inventing a governance fact to make an
automated path look governed, which is the thing E5-T2's own entry refused when
it declined to key a policy on a tier no claim can reach.

Excluding has one property that decided it: **the human sample requirement is
unchanged by automation.** A more aggressive policy cannot shrink what a person
still has to review, so automating disposal can never improve the measured
figure by reducing the evidence behind it. That is exactly the failure the entry
named — the number that keeps looking fine.

`disposition_actor_kind` is a column and not an inference. Telling a policy's
actor from a person's means knowing which service accounts are automation, which
lives outside this table, changes without a migration, and is wrong for the
deployment that just added one. A CHECK ties it to the disposition so a row can
never be resolved without saying who decided, and the backfill is `human` on
proof rather than assumption: until this migration there was no policy path at
all.

### E5-T5 — Decay as a trust-class transition, with materiality frozen

**Kind:** task · **Status:** done · **Blocked by:** E5-T4 · **Hotspot:** no · **Repo:** contextplane

Goal: a claim losing trust to age is recorded as a transition between trust
classes, at a materiality frozen when the decay happened — not as a
supersession, and not as a number that quietly moves.

Ground this before building: **decay today is applied at read, not stored.**
`serve_confidence` computes the effective number from `stored`,
`confidence_scored_at`, the category half-life and an optional hold. So there is
no stored decayed value to mistake for a supersession, and half of what this
task guards against is already structurally impossible. Check the rest of that
claim before writing anything — the entry may shrink to the transition record
alone.

"Frozen materiality at decay time" is the part with teeth, and it is the same
property E4-T6 wants for its deadlines: a value computed on read drifts when its
inputs are corrected, and "what was this worth when we let it decay" is exactly
the question a review asks afterwards. Stamp it.

`NON_DECAYING_VALUE_TYPES` is `{"prose"}` today, which means prose claims never
enter this path at all. Whether that is right is not this task's question, but
the entry should note it so the transition record is not read as universal.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "decay"
    make all

**Grounded, as the entry asked, and four things changed.**

**1. The premise holds. Decay is read-time.** `effective_confidence` in
`confidence_decay.py` computes from `stored`, `confidence_scored_at`, the
half-life and an optional hold, and `confidence_read.serve` is what callers get.
No decayed value is stored anywhere, so "not a supersession, and not a number
that quietly moves" is already structurally true and needs nothing built.

**2. "Trust class" is the five confidence buckets**, and they already exist:
`unreliable`, `weak`, `moderate`, `strong`, `confirmed` in `confidence.py`,
"none narrower than the accuracy tolerance a calibration check can verify". So
the transition this task wants is a *downward bucket crossing*, which is a thing
the codebase can already name.

**3. And that is where the task actually gets hard, which the entry does not
say.** Because decay is read-time, **nothing happens when a claim crosses a
boundary.** There is no moment to hang a record on: the claim is `strong` on one
read and `moderate` on the next, and no code ran in between. A transition record
therefore needs an *observer* — a sweep that evaluates claims and records
crossings — and that is most of the work, not the table.

That also means the sweep's own cadence decides what `transitioned_at` means. A
daily sweep records "the day we noticed", not "the day it crossed", and those
differ by up to the interval. Whichever is stamped, the entry has to say which,
because a review asking "when did we let this decay" will read it as the second.

**4. `materiality` is now a reserved noun and this entry may not use it.**
E4-T5b took it for the reporting obligation's classification, and
`scripts/check_reserved_vocabulary.py` refuses a second governed meaning — which
is the third collision that rule has caught and the first it caught *before* the
code was written.

The resolution is better than a new word: **the thing being frozen is already
called `confidence`.** What a review wants is the effective confidence at the
crossing plus the inputs it was computed from, and `memory_claims` already
carries `confidence_inputs` for exactly that purpose. So this entry needs no new
noun at all; it reached for one because "materiality" is what the sentence
wanted, not because the value lacked a name.

**Note, as the entry asked:** `NON_DECAYING_VALUE_TYPES` is `{"prose"}`, so
prose claims never enter this path and the transition record is not universal.
Whether that exemption is right is still not this task's question.

**Shipped as the grounding said it had to be: a sweep, not a table.**

`claim_trust_transitions` records downward bucket crossings, written by
`TrustTransitionSweep` because nothing else can notice one. The column is
**`observed_at`, not `transitioned_at`** — the sweep records when it saw, and the
crossing happened somewhere in the interval before that. Naming it for the
crossing would answer "when did we let this decay" wrong by up to one interval,
with nothing in the row to reveal it.

Three properties the build had to get right, each pinned:

- **Seeded from the stored score.** A claim with no prior transition is compared
  against the bucket its *undecayed* score falls in — where it started. Without
  that, the first pass either invents a transition or misses a real one.
- **Idempotent, and enforced.** The first pass moves the last-seen bucket to
  where the claim already is, so the second records nothing;
  `ck_trust_transition_moved` refuses a row whose buckets are equal, so a bug
  that lost the comparison fails loudly instead of writing a decay history that
  never happened.
- **Contiguous.** Each `from_bucket` is the previous `to_bucket`, so a reviewer
  sees two drops where there were two rather than one summarising both.

Measured in buckets, not points: 0.86 → 0.85 is a bigger numeric move than 0.84
→ 0.71 and only the second crosses nothing. Recording numeric movement would
fill the table with changes no consumer can act on.

**Read by `ClaimHistoryService.trust_history_for`**, which is the surface that
already answers "given the claim I was told about, what happened to it" — losing
trust to age being one of the things that happened. A record with no reader is
the defect this plan keeps finding; it does not get to be one here.

Correction to the entry's arithmetic while building: decay runs toward
`DECAY_FLOOR`, not toward zero (`floor + (stored - floor) * 2**(-age/half_life)`),
so a 0.90 claim sits at 0.50 after one half-life rather than 0.45.

### E5-T6 — The reviewer cockpit

**Kind:** task · **Status:** pending · **Blocked by:** E5-T3, E5-T4 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: the disposition surface a reviewer actually works in, with the rank's
reasoning visible and the consequence of each disposition shown before it is
taken.

Blocked on T3 and T4 rather than on the whole epic: the cockpit needs an
ordering to display and a disposition vocabulary to offer, and neither the
sampling policy's internals nor the decay transition changes what it renders.

Two things this UI must not do, both learned in this repo. It must not present a
rank as authoritative when its expected-loss term is absent — if E5-T1 lands
without a loss model, the cockpit says the queue is ordered by leverage and
sampling, because a reviewer who believes a number accounts for cost will defer
to it. And per `.develop/DESIGN.md`, client authorization shapes the UI only;
the disposition's approver check is the service's, and the cockpit showing a
button is not the same as the write being permitted.

E19-T7's defect is the one to keep in mind while building the adapter: the
endpoint is part of the behaviour, and a test that asserts the body and method
but not the path will pass while the call goes somewhere that does something
else entirely.

## Task decomposition — twelfth wave (E10, E11, E12 — the last three undecomposed epics)

Taken together in one wave because each one's grounding turned up the same kind
of finding: a mechanism already shipped that changes what the epic is asking
for, and in two cases makes it smaller.

### E11-T1 — ADR: an explorer that recomputes is the differencing attack

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: decide whether E11's aggregates read a stored series or compute live —
before any screen is built, because the two have different disclosure
properties and the wrong one cannot be fixed in the UI.

**Grounding found more than "existing suppression floors".**
`contextplane/signals/aggregates.py` and the `privacy_aggregates` table are a
full differencing defence, and its opening sentence is the finding: *"The hard
part is not the floors — it is the recompute."*

The attack it defends against is not a small cell. It is two figures for the
same cell: computed over a window, published, and computed again after an
erasure. **Every floor holds perfectly while that happens** — both figures clear
the minimum, neither names anybody, and subtracting them names one person's
contribution exactly.

Three mechanisms carry the defence and none is a step somebody remembers: one
version of a cell ever, enforced by a unique key so a recompute has nowhere to
leave its predecessor; withholding is one-way and sticky across every later
pass; and a withheld cell zeroes its actor count too, because reporting "six
actors now" beside a reader's memory of seven has disclosed that the erased
subject was one person.

**So an explorer is exactly the shape of thing that breaks this.** A screen
letting an auditor run the same breakdown on Monday and again on Friday
reproduces the attack with no floor violated and no bug anywhere — the module
that prevents it is the *writer*, and a reader that recomputes has routed around
it. The docstring already says the read surfaces compute live and floor on the
way out, which is *"correct for a question asked now"* and is not what an
explorer is.

The ADR decides: E11's aggregates read the stored series, or E11 states why its
particular reads cannot participate in a difference. Prefer the first.

Acceptance:
    make doc-refs doc-links
    make all

**Delivered as [ADR-0013](../adr/0013-an-explorer-that-recomputes-is-the-attack.md).**
The aggregates read the stored series; they do not compute live.

Two refinements the writing produced. **The receipts half is unaffected and
stays live** — a receipt records one resolution rather than an aggregate over a
population, so reading it twice discloses nothing that reading it once did not.
Folding it into this decision would have made E11-T2 harder for no gain.

And **a metric E11 wants that is not in `AGGREGATE_METRICS` is a writer change,
not a reader change.** That set is closed so a metric cannot be computed by one
pass and forgotten by the next, and adding a live computation beside the stored
series to cover a gap would reintroduce the recompute while looking like a small
convenience. It also inherits a retention question, since `_SOURCE_CLASS_FOR`
makes an aggregate carry its source's record class — friction in the right
place, because an aggregate outliving its sources is a breach no floor detects.

The dissent is worth reading before E11-T2 starts: this generalises from one
writer to a whole epic, and a metric with no per-actor contribution arguably
cannot leak by subtraction at all. The honest fix is a per-metric analysis
nobody has done; the ADR takes the conservative line because being wrong in the
permissive direction is silent.

### E11-T2 — The receipts explorer, over endpoints that already exist

**Kind:** task · **Status:** done — shipped in contextplane-ui#36 · **Blocked by:** E11-T1 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: a reader can find a receipt, see what it served and what it withheld, and
follow its references — without a new endpoint.

E3-T2 is the relevant recent work and it changed what a receipt can say about
itself. `hydration_state` now distinguishes a receipt that is finished being
written from one that is not, `/exclusions` and `/references` refuse with a 409
`receipt_not_hydrated` rather than answering emptily, and `GET /receipts/{id}`
returns the state so a caller can tell which it has.

**That 409 is a UI state, not an error.** An explorer that renders it as a
failure teaches its reader that the system is broken when it is being careful.
Render it as "still being written", and make the distinction visible, because
the alternative — an empty exclusions list — is the thing E3-T2 exists to
prevent a caller from believing.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**The entry names one 409 reason; there are two.** `receipt_not_hydrated` is
the system being careful and waiting fixes it. E4-T4 added `receipt_withheld`,
where an operator withheld the content deliberately and waiting fixes nothing.

Collapsing them would be the same mistake in the other direction: it leaves
somebody refreshing a screen that will never change, and hides that a decision
was taken. So the withheld notice offers no re-read and says plainly that
re-reading returns the same refusal. A real error stays a real error — swallowing
a 500 into "still being written" would be the third version of it.

Two smaller findings: `GET /receipts/{id}` deliberately does not refuse (it is
the poll surface), so the header is a separate query and stays readable when it
is the only thing that can be read; and there was **no adapter for finding a
receipt at all**, only for opening one by id, which made "a reader can find a
receipt" unbuildable until `findReceiptsByReference` was added.

### E11-T3 — Audit-role drill-down, and the justification that is the control

**Kind:** task · **Status:** pending · **Blocked by:** E11-T1 · **Hotspot:** no · **Repo:** contextplane

Goal: an auditor can see per-actor detail nobody else can, and every such read
records why it was made.

`ROLE_AUDITOR` already exists as a first-class role in `auth/roles.py` and
`VALID_ROLES`, so this is an authorization question with an answer, not a new
role to invent.

The justification is the whole control and it must be **recorded before the data
is returned, in the same transaction**. A justification captured after the read,
or best-effort alongside it, is a field that is empty exactly when it matters —
the read that somebody did not want to explain is the read that completes and
leaves no note. This is the same discipline `resolve.py` applies by refusing to
make its receipt write best-effort.

Free text is right here and worth defending: a dropdown of reasons produces the
reason nearest the top, and the point is a sentence somebody has to be willing
to have read back to them.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "audit and justification"
    make all

### E12-T1 — The connector framework exists; three named sources do not

**Kind:** task · **Status:** pending · **Blocked by:** E5-T4 · **Hotspot:** no · **Repo:** contextplane

Goal: Backstage, CMDB and wiki reach the catalog through the connector
framework that already ships, not through a second import path.

`contextplane/ingest/connector_registry.py` is a single authoritative
`source_type → Connector` mapping with `get_connector` and a typed
`UnknownConnectorError`, and five connectors already use it: `docs_corpus`,
`markdown_adr_rfc`, `openapi`, `package_json`, `release_notes`. None of them is
one of E12's three, so the work is three connectors and not a framework.

A bulk-import API that bypasses the registry is the failure mode to refuse. It
would be a second place source types are known, and the first thing that goes
wrong is that one of them knows about a source the other does not.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "connector"
    make all

### E12-T2 — Provenance mapping, following the precedent E2 already set

**Kind:** task · **Status:** pending · **Blocked by:** E12-T1 · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: `observed_time` and `external_record_id` come from the source record and
are never server-defaulted — enforced by the schema, not by the importer.

**E2 already built this exact property for a different record class.** Migration
0067 added `external_record_id` and `observed_at` to `memory_session_events`
with a CHECK requiring `source_namespace`, `external_record_id` and
`observed_at` to be present *together or not at all*. That constraint is the
enforcement: an importer that forgets one is refused by the database rather than
by a code review.

Copy the shape. A `NOT NULL DEFAULT now()` on `observed_time` would be a
server-defaulted value wearing a caller-supplied name, and it would be
indistinguishable afterwards from a genuine one — which is the whole reason the
epic names this property.

Also inherit E1-T11's rule for undeclared sources: `SourceNamespaceService`
returns `None` for a namespace nobody registered rather than substituting a
default, so an import from an unregistered source is a refusal and not a silent
`internal` classification.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "import and provenance"
    make all

### E12-T3 — The migrated-canonical disposition, and a halt E5 has not defined

**Kind:** task · **Status:** pending · **Blocked by:** E5-T2, E5-T4, E12-T2 · **Hotspot:** no · **Repo:** contextplane

Goal: a bulk import records its own dispositions honestly, sampled under the one
governed sampling policy rather than a second regime.

Two constraints the epic states and both are right. `disposition_actor =
policy-automated` comes from E5-T4 and is never approximated by widening
`approval_authority` — those answer different questions, and conflating them
means a policy's write becomes indistinguishable from an approver's.

The sampled audit draws from E5's single `SamplingPolicy`. **A second sampling
regime here would be the failure E5-T4 already names**: acceptance sampling
assumes the sample was inspected, and a batch import that samples itself under
its own rules is grading its own homework with a marking scheme it chose.

**A gap to close in E5 rather than work around here.** E12 says this inherits
the policy's *"below-minimum-sample halt"*, and E5-T2 as written does not define
one. Either E5-T2 gains it — a sampling policy that cannot draw its minimum
sample must stop rather than proceed on a short one — or E12-T3 is blocked on a
property that does not exist. Raise it against E5-T2; do not define a halt here,
because a halt defined by its consumer is the second regime this task refuses.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "migrated_canonical"
    make all

### E10-T1 — Quarantine and suspend screens

**Kind:** task · **Status:** done — shipped in contextplane-ui#29 · **Blocked by:** E4-T8 · **Hotspot:** no · **Repo:** contextplane-ui

**Retargeted.** This entry named E4-T2 and E4-T3 as its blockers, and both were
done while this stayed unbuildable: a screen cannot call a service with no
endpoint, and `QuarantineService` had no route, no tool and no wiring until
E4-T8. The dependency was on the surface all along.

Goal: an operator can preview what a quarantine would reach, apply it, and
revert it, from a screen.

Blocked on E4-T2 and E4-T3 specifically rather than on E4 as a whole: this needs
the state and the preview, and nothing here depends on the DORA half.

Two properties from E4 that the UI must not soften. The preview is a
**point-in-time** answer — the graph moves, and a screen that presents a
ten-minute-old preview as current will cause an under-quarantine nobody
notices. And revert is not a secondary action tucked in a menu: E4-T2's entry
argues that an operator who cannot undo a quarantine will not run one on a real
incident, which makes revert's discoverability part of whether the feature
works at all.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**Landed.** Preview is a separate call from apply, so a caller cannot withhold
content by getting a boolean wrong. The screen keeps `matched` and the advisory
`downstream` set apart, because merging them would tell an operator that
applying makes the downstream list disappear. Revert is a primary action, not a
menu item: an operator who cannot see how to undo a quarantine will not run one
on a real incident.

### E10-T2 — Navigation and DESIGN.md repositioning

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Goal: the information architecture reflects what the product does, and
`.develop/DESIGN.md` and the nav agree.

Unblocked, and deliberately not waiting on the screens above — an IA decided
after every screen exists is an IA fitted to the screens that happened to get
built. E19 already landed catalog-side authoring, so there is enough surface to
arrange.

**E10's "cockpit dispositions" is not part of this.** That work is now E5-T6,
cut with the epic that owns the disposition vocabulary and the ranked queue it
displays. Listing it in both places would be two teams building one screen.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**The document contradicted its own opening line, so the document moved.** The
IA section said *"Organize navigation by user intent, not by endpoint or database
table"* and then listed seven sections — Overview, Catalog, Memory, Workspaces,
Governance, Audit, Administration — six of them named after the thing stored.
The shipped nav was already four groups named for what someone came to do, so it
is the better artifact and `DESIGN.md` was rewritten to describe it, plus the
rule that was implicit in it: **a destination is named for its reader's job, not
for the scope the job runs in.** If describing who it serves needs the word
"and", it is more than one destination.

**"Tenant work" was the counterexample, and hid the headline loop.** One
destination named after a scope, with three unrelated readers behind it:
notifications and learning evidence; ownership and profile governance; and
intent participants with their checkpoint chains. The third is the chain
`resume_context` serves to whoever picks a task up next, and it was reachable as
the third tab of a page named after a tenancy — `DESIGN.md`'s own list had no
section for it at all. Now `Tasks` under *Work with context*, `Activity` under
*Monitor usage*, `Ownership & profiles` under *Governance*, each panel unchanged
in its own feature folder.

**An unrecognized address used to render the catalog.** `DESIGN.md` asks that a
copied URL reconstruct the same view; silently reconstructing a *different* one
is worse than reporting nothing, because it is indistinguishable from having
asked for that page. There is now a not-found destination, and no nav item
claims `aria-current` while it shows.

**Five parallel dispatch lists collapsed into one route table.** Adding a
destination meant editing a union, a matcher, a chunk loader, an identity
predicate, and two nested ternary chains. Nothing made them agree, so a route
could load its chunk and highlight nothing in the nav.

### E10-T5 — `shared/api/tenantWork.ts` outlived the destination it was named for

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane-ui

Goal: the API module names a domain, like every one of its siblings, rather than
a page that no longer exists.

E10-T2 dissolved the "Tenant work" destination into `Activity`, `Tasks` and
`Ownership & profiles`. The 620-line API module behind it still carries the old
name over three domains' worth of calls: notifications, learning and signals
(240–322); ownership and profiles (323–488, 587–620); intents and checkpoints
(489–586). Every sibling in `shared/api/` — `admin`, `audit`, `catalog`,
`relationships` — is named for a domain. This one was named for a screen, which
is why it accumulated three.

Deliberately not folded into E10-T2: that change was navigation, this one is the
data layer, and one PR carrying both would be two changes wearing one title.

**The part that needs a decision rather than a move.** Lines 84–240 are thirteen
generic validators — `requiredString`, `nullableNumber`, `stringArray` — used by
all three groups. Splitting three ways needs a fourth module for them. Check
first whether the siblings already have their own copies of these: if they do,
the extraction is larger than this task and serves more than it, and that is
worth knowing before a fourth private copy is created.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**Landed in contextplane-ui#25, and the check this entry asked for came back
positive.** It said to establish, before creating a fourth private copy of the
validators, whether the siblings already keep their own. They do — `admin.ts`,
`arcAuthoring.ts` and `entityResolution.ts` each carry `isRecord`,
`requiredRecord`, `requiredString` and `nullableString` — **and they have already
drifted.** The same failure reads `"… is not text."`, `"… must be text."` or
`"… is not a string."` depending on which module happened to parse it, and
`arcAuthoring`'s `requiredRecord` says `Invalid API {label}.` where the others
say `Invalid API response: {label} is not an object.`

So the validators are extracted once into `parse.ts` rather than copied a fifth
time: splitting three ways without it would have turned four copies into six.
The module becomes `activity.ts`, `ownership.ts` and `intents.ts`, and the
342-line test file splits the same way so every source keeps a same-named
sibling. Test count unchanged at 457 — this moves code, it does not add
behaviour.

### E10-T11 — Three modules still carry their own copies of the validators

**Kind:** task · **Status:** done — for the three named; the rest is E10-T12 · **Blocked by:** E10-T5 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: `parse.ts` is the only definition of the response validators.

E10-T5 extracted them and pointed the three modules it created at them.
`admin.ts`, `arcAuthoring.ts` and `entityResolution.ts` still hold their own,
and the drift above is the argument: three spellings of one refusal is three
things a reader has to recognise as the same failure.

**Deliberately not folded into E10-T5**, and the reason is what makes this its
own task rather than a tidy-up: it **changes message text**, and some tests
assert on that text — `test_a_foreign_receipts_exclusions_are_not_readable`'s
sibling in this repo asserts `"Invalid API response: capability_id is not
text."` verbatim. So this is a behaviour change wearing a refactor's clothes,
and it wants its own review rather than a diff nobody separates from a move.

Settle one thing while doing it: whether the divergent messages are *only*
wording. `arcAuthoring`'s `requiredRecord` takes a label and produces a
different sentence shape, so a caller matching on the message — if any does —
breaks differently from one matching the others.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**Landed in contextplane-ui#26, and the answer to this entry's question was "no,
not only wording."**

- `arcAuthoring.stringArray` took `(record, key)` where every other copy takes
  `(value, label)` — **a different function wearing the same name**. Its two
  call sites were converted rather than the shared one bent to fit.
- `admin.stringArray` returned a mutable `string[]`, and three of its fields
  come from *generated* contract types that declare `string[]`. Those sites copy
  the readonly result rather than cast it: the shared validator returns
  `readonly` so a parsed response cannot be edited in place, and a cast keeps
  the annotation while dropping the guarantee.
- One test asserted `"entity_id is not a string"` verbatim. That is the
  behaviour change this task was cut from E10-T5 to make reviewable, arriving
  exactly where the entry predicted it.

### E10-T12 — The other five modules, and the convention question underneath them

**Kind:** task · **Status:** done — five converted; `catalog.ts` is E10-T13 · **Blocked by:** E10-T11 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: `parse.ts` is the only definition, across the whole API layer.

E10-T11 was filed against three modules. Measured after converting them, **five
more** carry copies — and the reason they were not swept in with the rest is
that they do not agree on a calling convention:

| convention | modules |
|---|---|
| `(value, field)` | `agents.ts`, `audit.ts` |
| `(record, key)` | `contextplane.ts`, `entityWrites.ts`, `relationships.ts` |

`parse.ts` uses `(record, key)` for field readers and `(value, label)` for
container checks. So converting the first group rewrites every call site rather
than an import, and that is a decision about which shape is right — not a move.

**Settle the convention before touching a line.** `(record, key)` reads the key
off the record and can name the field in its own refusal without the caller
repeating it; `(value, field)` lets a caller validate something that is not a
record field at all. Both are defensible and the codebase currently asserts
both. Whichever wins, the other's call sites change, so picking after starting
is how this becomes two rewrites.

**Decided, so this is a move and not a decision: `(record, key)` for field
readers, `(value, label)` for container checks** — which is what `parse.ts`
already does, and what three of the five remaining modules already do.

Two reasons, and the second is the one that matters. `(value, field)` makes the
caller write the field name twice — `requiredString(value.rate, "rate")` — and
nothing holds the two together, so a copy-paste that updates one and not the
other produces a refusal naming the wrong field. `(record, key)` cannot drift
that way because there is only one name.

And the case `(value, field)` exists to serve — validating something that is not
a record field — is already served: `requiredArray` and `stringArray` take a
value and a label precisely because a container is not always read off a key.
So nothing is lost by converting `agents.ts` and `audit.ts`; their call sites
become `requiredString(record, "key")` and the second name disappears.

`agents.ts` was added by E20-T10 with its own copies — the duplication was still
growing while the task to stop it was open, which is the argument for a lint
rule over a sweep. Consider whether one is cheap here: a `no-restricted-syntax`
forbidding a top-level `function requiredString` outside `parse.ts` would have
caught it at the point it was written.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**Landed in contextplane-ui#28.** Two more duplicate pairs surfaced while doing
it and were folded in, because they are the same defect: `optionalBoolean` was
identical in two modules, and `requiredInteger` in two more — where the copies
**disagreed on ordering**, one delegating to `requiredNumber` and one checking
`Number.isInteger` directly, so they produced different messages for a
non-finite value.

Ten test assertions named the old wording, and one of them showed the shared
message was the *weaker* one. `nullableString` now says "is not text or null"
where `requiredString` says only "is not text", so a refusal states what was
allowed and a reader can tell an optional field from a required one without
opening the source. The spellings this replaced were four: "is not text", "must
be text", "is not a string", "is not nullable text".

### E10-T13 — `catalog.ts` speaks a different validator dialect

**Kind:** task · **Status:** done — shipped in contextplane-ui#30 · **Blocked by:** E10-T12 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: the catalog adapter refuses in the same words as every other adapter.

It is not a copy of the shared validators. It has its own vocabulary —
`string()`, `boolean()`, `record()`, `unknownRecord()` — with a different
calling convention again and, more consequentially, **messages carrying no
`Invalid API response:` prefix at all**: `"${label} was not a boolean."`,
`"${label} was not a list."`

So a caller cannot recognise a catalog parse failure by the same shape as any
other, and a log filter written for one misses the other. Converting it rewrites
that whole parse layer plus every assertion against it, which is why it was left
out of E10-T12 rather than bundled into a change that was already ten
assertions wide.

**Decide the prefix deliberately.** Every other adapter leads with `Invalid API
response:`, which is what makes these failures greppable as a class. If catalog
has a reason to differ, it is not recorded anywhere; if it does not, the prefix
is the smaller half of this task and worth doing even if the vocabulary stays.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**The entry asked whether catalog had a reason to differ. It did, and it was
not the wording.** Its labels are *qualified* — `Capability created_at` locates
a failure that `created_at` does not, and `created_at` sits on five catalog
objects. Converting straight to `requiredString(record, key)` would have deleted
a real diagnostic, so the label moved into `parse.ts` as an optional third
argument and the eight already-converted adapters were left untouched.

Also folded in: `quarantine.ts` carried a tenth copy of `stringArray` under the
name `stringIds`, introduced one PR earlier by E10-T1. And `stringArray` now
names the offending index rather than saying the list "contains data", which is
true and no help on a list of a hundred.

### E10-T3 — ARC and PII operations out of the raw console

**Kind:** task · **Status:** done — PII half shipped, ARC half decomposed · **Blocked by:** E10-T2 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: the ARC and PII operations somebody currently performs against raw
endpoints have screens.

Scope this by reading what those operations actually are before designing
anything — `arc_get_review_package`, the approval and challenge paths, and the
PII policy surfaces are each a different job with a different reader, and a
single "ARC console" is how they end up sharing a screen none of them fits.

The E19-T7 defect is the one to carry into the adapter work here: the endpoint
is part of the behaviour. A governed edit sent to the collection path instead
of the item path mints a second record instead of updating one, and a test
asserting the body and the method but not the path passes while it happens.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**Measured before designing anything, which is what the entry asked for.** The
gap is sixteen operations, and they do not belong on one screen.

**PII: three of six operations had an adapter.** Shipped in
contextplane-ui#24 — `POST /v1/admin/pii-field-policies` and
`DELETE .../{policy_id}`, which is the operator's primary personal-data control
and was a `curl` because the dashboard could list policies and not change one.

`POST /v1/admin/pii-patterns` is **deliberately not built**: the runtime builds
its scanner from the built-in detector modules only, so a registered tenant
regex generates no matches. The operator guide says so in a warning block. A
form for it would be a control that stores configuration and enforces nothing —
shipped knowingly, which is worse than the accident.

**ARC: twelve admin operations have no adapter**, and they are four different
readers, cut below. Counted honestly: some `/v1/arc/admin/...` paths have a
non-admin twin that *is* adapted, and those are excluded. That the service
exposes several operations on two paths is a surface-consolidation question for
E13, not for this epic.

The one thing they share is the E19-T7 discipline: the endpoint is part of the
behaviour, a governed edit sent to the collection path mints a record instead of
updating one, and a test asserting body and method but not path passes while it
happens.

### E10-T6 — The PII vocabularies are closed in the service and open in the contract

**Kind:** task · **Status:** done — service side; the UI can drop its copy next · **Blocked by:** none · **Hotspot:** yes — vendored openapi.json · **Repo:** contextplane

Goal: `field_type` and `policy` are published as the closed sets they are.

The service holds `PILOT_FIELD_TYPES` (nine values) and refuses an unrecognised
one with `NotAPilotField`; `policy` is `advisory | warn | block`. The contract
types both as bare `string`.

So a client cannot offer a correct picker without duplicating the vocabulary,
and E10-T3 duplicated it — nine strings written into
`PiiFieldPolicyEditor.tsx`. The alternative was a free-text box, which lets an
operator save a policy that stores, lists, and **silently governs nothing**: a
value missing from a select cannot be chosen, a typo in a text box cannot be
seen.

This is the same failure the codebase already refuses twice — `PROHIBITED_CLASSES`
is read off the shipped detectors rather than restated, for exactly the reason
that a hand-written second list disagrees first in the direction that silently
admits. Publishing the enum deletes the duplicate rather than documenting it.

**Two live defects found while scoping this, and they are worse than the
contract-shape problem it was filed for.**

**`field_type` is never validated on the policy-write path.** In
`api/routers/admin_pii.py`, `body.field_type` goes straight into
`insert_pii_field_policy` with no check against `PILOT_FIELD_TYPES`. So
`POST /v1/admin/pii-field-policies` with `"workspace_entry.bodies"` — one
character wrong — stores a row, lists it back, and **governs nothing**, because
resolution matches the field type as an exact string. An operator reading their
own policy list sees the control they think they configured.

That is precisely what `NotAPilotField` exists to prevent one layer down:
`admit()` refuses an unrecognised field type rather than admitting it, and its
docstring says silence on an unrecognised field "is how admission gets switched
off for a surface by a typo". The *writing* path never asks. E10-T3's select
mitigates it for UI callers and does nothing for anyone using the API.

**`_VALID_POLICIES` is a fourth copy of the policy vocabulary**, hand-written in
`admin_pii.py` beside `_POLICY_SEVERITY` in `pii_scanner.py` — which is the
authority, since it also carries the ordering the scanner takes the maximum
over. Two lists, and the one that decides is not the one the route checks.

So the deliverable is three things, not one: validate `field_type` fail-closed
against the pilot set, read the policy vocabulary from the scanner instead of
restating it, and *then* publish both as `Literal` so the contract carries them
and clients stop duplicating what E10-T3 had to duplicate.

**Sequence it after E4-T8.** Both regenerate `openapi.json`, and a merge
conflict in a generated file is the one kind worth avoiding by ordering.

Acceptance:
    make contract-tags
    make all

**All three landed, and the middle one was the defect worth the task.**

**`field_type` is validated fail-closed now.** It reaches the insert through a
published `Literal` *and* a set check against `PILOT_FIELD_TYPES`, because the
`Literal` is hand-written and the pilot set is what the scanner resolves
against. A policy for anything else stored, listed, and was consulted by
nothing.

**The policy vocabulary is read from the scanner.** `POLICY_VALUES` is derived
from `_POLICY_SEVERITY` — the map that also carries the ordering the maximum is
taken over — so the list the route checks is now the list that governs. It is
ordered least severe first, because a caller offering these as a choice should
present them in the order that decides.

**Both are published as enums**, so `PiiFieldPolicyCreate.field_type` and
`.policy` carry their values into the contract.

**Publishing created the duplication it removes, so it has a gate.** A `Literal`
cannot be built from a runtime frozenset, so the vocabulary is necessarily
written a second time — safe only while something holds the two in agreement.
`tests/conformance/test_pii_vocabularies_published.py` is that something, and it
names both directions of a divergence. Mutation-checked: one character in the
`Literal` fails it. That is `PROHIBITED_CLASSES`'s principle applied where the
second list is unavoidable — where it cannot be derived, it gets a gate.

**One behaviour change.** An invalid `policy` is now refused by the schema
rather than by the route's own check. Over HTTP that is the same 422; the unit
test called the handler directly, so it now asserts the schema refuses it, which
is stronger — the refusal happens before the handler runs.

**A third gate classified the change.** `test_every_module_naming_a_pilot_field_reaches_admission`
fired, because `admin_pii.py` now names pilot field types and never calls
`admit`. Adding it to `defining` would have been false: it does not define the
vocabulary, it *governs* it, and calling `admit` there would scan a policy row
carrying no content. Classified as that third category with the reason written
down.

**What the UI can now drop.** E10-T3 wrote nine field-type strings into
`PiiFieldPolicyEditor.tsx` because the contract typed `field_type` as a bare
string. On the next contract pin bump that component can read the generated
union instead, and the comment explaining why it was duplicated comes out with
it.

### E10-T7 — Who may approve: the approval-verifier surface

**Kind:** task · **Status:** done — shipped in contextplane-ui#31 · **Blocked by:** E10-T3 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: enrolling, challenging and revoking an approval verifier has a screen.

Three operations, no adapter: `POST /v1/arc/admin/approval-verifiers`,
`.../enrollment-challenges`, and `.../{approval_verifier_id}/revoke`.

Its reader is whoever decides *who is allowed to approve an ARC change* — a
different person from whoever approves one, and that separation is the control.
A screen that let the same session enrol a verifier and then use it would defeat
the reason verifiers are enrolled at all; whether the service enforces that
separation is the first thing to check, because if it does not, this screen
must not imply it does.

**Checked, and the answer is more useful than yes or no.** Actor separation *is*
enforced, by `_check_actor_separation` in `arc/schemas/authoring_profiles.py`:
submitter and approver must be distinct principals, and a `global_mandatory`
risk classification requires three distinct principals across submitter,
approver and activator.

**But that is the proposal lifecycle, not verifier enrolment.** Nothing prevents
the actor who enrols a verifier from later approving with it.
`VerifierRegistry.register` validates shape only and says so — *"Authorization is
the caller's; the route holds the operator gate"* — and there is no
separation-of-duty check anywhere between enrolment and use.

So the screen must not present enrolment as if it carried the separation that
approval does. If four-eyes over enrolment is wanted, it is a service change and
its own task, not something a UI can assert.

`GET /v1/arc/admin/operator-identity` belongs here rather than in a screen of
its own: it answers "which verifier am I", which is the question this page's
reader has, and one read is not a destination.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**The entry's question is answered on the screen.** Enrolment is *not*
separated from approving, so the page says so rather than implying a control
that does not exist.

Two things the entry did not anticipate, both consequences of E14-T1:

- **No list endpoint**, so revoke takes a pasted id and the screen shows the
  verifier id once, telling the operator to record it.
- **Signing cannot happen in the browser.** The proof is a signature by the key
  being enrolled, so the screen hands over the canonical bytes and the signing
  domain and takes back a signature. A test asserts no private-key field exists.

Conditional fields follow `binding_kind`: the service forbids the provider pair
on an exact binding, so offering all six invites a refusal naming a field the
operator was handed.

### E10-T8 — Proof that an approval happened, and withdrawing it

**Kind:** task · **Status:** done — shipped in contextplane-ui#34 · **Blocked by:** E10-T7 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: attaching and revoking approval evidence has a screen, and so does
invalidating a revision.

`POST /v1/arc/admin/revisions/{revision_id}/approval-evidence`,
`POST /v1/arc/admin/approval-evidence/{evidence_id}/revoke`, and
`POST /v1/arc/admin/revisions/{revision_id}/invalidate` — the last has no
non-admin twin, unlike activate and revoke, which do and are already adapted.

**Invalidate and revoke are not the same act** and the screen must not let them
read as one. Establish which is reversible and which is a statement that the
revision should never have been active, before designing either control; a
reader who picks the wrong one cannot tell from the button.

**Established, and the question as posed has no answer: neither is reversible.**
`revoke` is documented "Terminal", and `test_a_revoked_revision_cannot_be_reactivated`
holds it there. `invalidate` is terminal too. So a screen built around
"reversible versus not" would be sorting them on an axis they do not differ on.

What they differ on is **what the tombstone tells an auditor**, and the service
keeps two codes to say it — `OBLIGATION_MISSING_REVOKED` and
`OBLIGATION_MISSING_INVALID`, pinned by
`test_invalidation_tombstones_differently_from_revocation`:

- **Revoke** says the rule no longer applies. The revision was validly in force
  until now, and everything resolved under it stands.
- **Invalidate** says the content was wrong or its source is gone. That reaches
  *backwards*: it casts doubt over every resolution made while it was active.

So the distinction the screen must carry is not undo-ability but blast radius in
time, and invalidate is the one that reaches into the past. Design from that.

Blocked by E10-T7 because evidence names a verifier, and a screen that attaches
evidence citing verifiers nobody can enrol is half a workflow.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**Shipped on the finding above: neither ending is reversible.** The screen
asks which of the two statements is true — the rule stopped applying, or the
content was wrong — and will not act until one is chosen. There is no default,
because the two request bodies are byte-identical and a default would be a
silent choice between opposite claims about the past.

Two adapter functions rather than one taking a flag, for the same reason: the
path is the entire difference, so a boolean is precisely how they get swapped. A
test asserts the invalidate path is used and the revoke path is not.

### E10-T9 — Documented deviations: the exception surface

**Kind:** task · **Status:** done — shipped in contextplane-ui#32; the register waits on E14-T1 · **Blocked by:** E10-T3 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: granting and revoking an ARC exception has a screen.

`POST /v1/arc/admin/exceptions` and `.../{exception_id}/revoke`.

An exception is a governed statement that a policy does not apply here, for a
reason, until a date. Its reader is an auditor as much as an operator, so the
screen's job is as much *showing the standing exceptions and why* as granting
one — a grant form with no register beside it makes the exceptions invisible the
moment they are created, which is the opposite of what a documented deviation is
for.

Check whether the service requires an expiry. If it does not, this screen should
say what an open-ended exception means before offering one, because an exception
with no end is a policy change wearing a smaller word.

**Both questions checked. The expiry answer is the expected one; the other one
removes half this task's stated goal.**

- **Expiry is not required.** `effective_from` is mandatory and
  `effective_until` is nullable, so an open-ended exception is grantable
  today. The entry's instinct stands and the screen should say so before
  offering the control.
- **Standing exceptions cannot be shown.** There is no read path for an
  exception — see E14-T1. So "showing the standing exceptions and why", which
  this entry names as as much of the job as granting one, cannot be built here.
  The register this task asks for is a service change.

A third thing, not asked about and worth knowing before designing the form:
`ExceptionApprovalBody` requires `evidence_id`, `approval_verifier_id`,
`approved_payload_digest`, `audit_log_reference` and an approval timestamp. That
is a **pre-assembled approval envelope**, not something an operator composes at a
screen. Whatever this page does, granting is transcribing an approval that
already happened elsewhere — which also makes it depend on E10-T7 in practice,
since the envelope names an enrolled verifier.

Rescoped to what the API supports: grant, revoke, say plainly what an
open-ended exception is, and say plainly that this screen cannot show what is
already in force. Re-scope back when E14-T1 lands.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**Shipped rescoped, as the amendment above says.** The open-ended warning is
on the screen and disappears once an end date is given, and a blank end date is
*omitted* from the body rather than sent as null — an absent field is the
contract's own way of saying "no end".

The register of standing exceptions is not built and cannot be until E14-T1
lands. The screen states that at the top rather than presenting a grant form
with no list beside it.

### E10-T10 — Where ARC's inputs come from: source governance

**Kind:** task · **Status:** done — shipped in contextplane-ui#33 · **Blocked by:** E10-T3 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: registering source connectors, upload policies and replay corpora has a
screen.

`POST /v1/arc/admin/source-connectors`, `.../source-upload-policies`, and
`.../observation-replay-corpora`.

These three configure what ARC is allowed to read and what it replays against,
which makes them the highest-blast-radius controls in this group and the least
obviously so — nothing about registering a connector looks like it changes what
governance concludes. Whatever this screen does, it should make that visible.

`ArcSourceEvidenceSection.tsx` already renders source evidence and is 755 lines;
read it before adding a second source-shaped surface, because the answer may be
that this belongs beside it rather than apart from it.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**The entry's placement question, answered by reading what it asked me to
read.** `ArcSourceEvidenceSection` is titled "2. Bind approved source evidence"
— step 2 of authoring one artifact. These three registrations are deployment
configuration every such binding afterwards inherits. Different job, different
reader, and the dependency runs one way.

What makes the blast radius real is `allowed_verifier_ids`: it decides who may
approve material the connector fetches, for every future fetch. It is one field
among six and the only one granting authority over material that does not exist
yet, so it is called out on its own.

One accessibility defect found by writing the tests: three forms on one page
shared four label texts, `Owning scope` three times. Renamed per section rather
than worked around in the test — the tests could not address them
unambiguously, and neither could a screen reader.

### E10-T4 — Canon copy

**Kind:** task · **Status:** done — shipped in contextplane-ui#35 · **Blocked by:** E10-T3 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: the words the product uses about itself are true and consistent.

Last for a reason: copy written before the screens describes an intention, and
copy written after describes what shipped.

The standard is set by what this repo has already had to fix. "Semantic data
mesh" was removed from both UI scope statements and a false "usage data"
attribution was dropped, because neither was true. The nearby ADR-0012 rule is
the same rule in another domain: never call bounded-exposure tamper-evidence
*non-repudiation*, and the reason it is written down is that a marketing page's
author is the person most likely to reach for the stronger word.

Acceptance:
    pnpm lint && pnpm type-check && pnpm test && pnpm build

## Task decomposition — sixteenth wave (the integration tier is flaky under CI capacity)

### E15b-T2 — Four workers, four CPUs, and a Postgres each

**Kind:** task · **Status:** amended — the proposed fix is measured wrong, and one flake was a real bug · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: an integration run that fails means a test is wrong.

**Three unrelated flakes in one afternoon**, each green on a re-run of the same
commit:

- `test_extraction_end_to_end.py::test_an_extracted_claim_is_inference_tier_not_extraction_tier` — `IndexError`
- `test_reporting_obligations.py::test_a_nomination_lands_unclassified_and_says_so` — hung in `epoll_wait`
- `test_reporting_obligations.py::test_the_backlog_reports_the_age_of_the_longest_wait` — same run

**The runner already diagnoses this itself**, in a line it prints on every CI
run:

> *4 CPU(s) available; running 4 worker(s) instead of 8. The committed count was
> measured on an 18-core host, and each worker also runs its own Postgres
> container — past capacity this does not merely slow the tier, it raises
> CancelledError out of connection acquisition and fails unrelated tests.*

So the failure mode is known, named, and printed — and the clamp still lands on
*four workers, four CPUs, four Postgres containers*. The wall-clock evidence
agrees: the same tier took **6:35** on one PR and **14:37** on the next, with no
change to what it runs.

**Why this belongs beside E15b-T1 rather than inside it.** That task cut the
critical path by removing a dependency nothing needed. This one is the other
half of the same instruction: a flaky tier costs a full re-run, which is worse
than a slow one, and it also teaches a reader that red does not mean broken —
after which nobody reads the red.

**What this task must not do:** clamp by guessing. `workers_supported` is
deliberate, documented, and bound into a sealed control precisely so *"a run
that quietly used a count the repository did not commit"* cannot happen. Whatever
number replaces four has to arrive the same way the eight did — measured, and
committed as the count.

Acceptance:
    # the tier passes on three consecutive CI runs of one unchanged commit
    make all

## Task decomposition — fifteenth wave (CI is the bottleneck, measured)

One task, filed against the standing instruction that CI must not become a
bottleneck. It is not a guess: the numbers are below.

**Measured, and the answer is the opposite of what this entry assumed.**

Reducing the worker count was the obvious fix and it is the wrong one. On the
18-core reference host, over the whole tier:

| workers | wall clock |
|---|---|
| 4 | **141 s** |
| 2 | **264 s** |

Halving the workers costs **1.87x**. On a CI runner that takes the tier from
about seven minutes to about thirteen — which puts it straight back on the
critical path E15b-T1 just took it off, and trades a probabilistic cost for a
certain one that is larger.

**And one of the three flakes was not a flake.** The 2-worker run failed
`test_running_the_sweep_twice_records_it_once` and
`test_a_second_fall_is_recorded_from_where_it_was_last_seen` — both E5-T5's, and
both failing for a real defect in `TrustTransitionSweep`: it read
`ORDER BY claim_id LIMIT 500` with no cursor, and nothing in its predicate
excludes a claim it has already examined. So it looked at the same first page on
every pass forever and **every claim beyond it decayed unobserved.** Fixed, with
a `batch=1` test that tells a walking sweep from a stopping one.

That failure presented as flakiness *because* it was order-dependent: whether it
failed depended on how many claims the run happened to have seeded before it.
Which is the lesson worth keeping — **a flaky-looking integration failure is a
hypothesis, not a diagnosis**, and this entry filed the hypothesis as one.

**What is left of the original finding.** Two flakes are still unexplained:
`test_an_extracted_claim_is_inference_tier_not_extraction_tier` (IndexError) and
`test_a_nomination_lands_unclassified_and_says_so` (hung in `epoll_wait`). Both
passed locally and on re-run, and the second's traceback is consistent with the
capacity the runner warns about. Two is a thinner basis than three, and neither
has been reproduced.

**So the task changes shape.** Not "pick a smaller worker count" — that is
measured to cost more than it saves. Either reproduce the two remaining failures
under constrained CPU and fix what they actually are, or accept them as the cost
of the current ratio and say so. Guessing at a number is what this entry was
already told not to do, and halving would have been that with a measurement
attached to the wrong question.

### E15b-T1 — The critical path is twice what it needs to be, and one tier runs twice

**Kind:** task · **Status:** done — the fan-out; the duplication kept, with a reason · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a PR's checks finish in about the time the slowest tier takes, not the sum
of two of them.

**Measured, from `.github/workflows/ci.yml` and the run history:**

| job | duration | depends on |
|---|---|---|
| `unit` (`make test-coverage`) | 8.5–11.5 min | `changes` |
| `integration` (`make test-integration`) | ~8 min | **`unit`** |
| `conformance` (`make test-conformance`) | ~4 min | **`unit`** |
| `image` | — | **`unit`** |

So the critical path is `unit` **then** `integration`: **17–20 minutes**, on
every code PR.

**Nothing is passed between those jobs.** Each does its own `actions/checkout`
and its own `make install-dev`. `needs: unit` buys fail-fast — do not spend an
integration runner if unit is already broken — and costs eight minutes of
wall-clock on every PR that passes, which is nearly all of them.

**The repository has already accepted this argument once**, one level down. The
`conformance` job carries this comment:

> *Depends on unit, not integration. There is no artifact passing between the
> two and conformance does not consume anything integration produces, so
> chaining them only serialized two independent jobs and put the slowest tier in
> front of a contract-drift gate that would have failed in a fraction of the
> time.*

Every clause of that applies again with `unit` in place of `integration`.
Fanning all three out from `changes` puts the path at `max(unit, integration)`
≈ **11.5 minutes**.

**Second finding, independent of the first: the conformance tier runs twice.**
`unit` runs `make test-coverage`, which is
`pytest tests/unit tests/conformance --cov ...`. The `conformance` job runs
`make test-conformance`, which is `pytest tests/conformance -v`. Same directory,
no marker filter, no file list — the second adds `-v` and a shorter timeout and
nothing else. That is a whole tier of duplicated work on every PR, on a job that
is *also* serialized behind the job that already ran it.

Deciding which of the two should keep the tier is the real content of this task
and is not obvious: the coverage run needs conformance included to hit its
ratchet, and the separate job exists so a conformance failure is legible rather
than buried in a coverage summary. Both are good reasons; they just do not both
need to execute it.

**What this task must not do:** trade correctness for speed. No tier is dropped,
no test is deselected, no timeout is loosened. The whole change is topology and
one duplication.

Acceptance:
    make all
    # and: a PR's `gate` job completes in materially less wall-clock than
    # the 17-20 minutes measured above, with the same set of checks reported.

## Task decomposition — fourteenth wave (ARC's admin surface has no read side)

Found by building E10-T7 and then checking E10-T9's premise. Not a defect in
either screen: it is the same absence twice, and counting the surface showed it
is the same absence thirteen times.

**One finding, and it is the kind ADR-0012 exists to catch.** The audit page
claimed "Immutable history", "Trace immutable service activity" and "This
history is append-only". None is true: `audit_log` is an ordinary table with no
hash chain, no signature and no append-only trigger, and `audit/emit.py`
swallows its own write failures by design so a mutation can succeed with no
audit row. The claim implied both unchangeability and completeness and delivered
neither, to the one reader who would act on it.

Replaced with the two limits stated plainly rather than the claim deleted, and
pinned by a test that also asserts the word does not reappear. The workspaces
page drew a contrast against "an immutable audit history", implying something
here provides one; corrected too.

The rest of the UI copy was scanned for the same class of claim and is clean.
The other uses of "immutable" — a source locator, a profile revision, a `seq` —
are accurate and left alone.

**Fanned out.** `integration`, `conformance` and `image` now depend on
`changes` rather than on `unit`, each carrying the same
`if: needs.changes.outputs.code == 'true'` that `unit` already had, so docs-only
PRs skip exactly as before. The measured critical path goes from `unit` **then**
`integration` (17–20 min) to `max(unit, integration)` (~11.5).

The cost is stated in the workflow rather than left implicit: a PR whose unit
tier is already broken now burns an integration runner before anybody knows.
That buys back eight minutes on every PR that passes, which is nearly all of
them.

`native-stack` and `perf` keep `needs: unit` deliberately — both are
schedule-only and neither is on a PR's path, so moving them would add risk for
no wall-clock.

**The duplication is kept, and now it is a decision.** `make test-coverage` runs
`tests/unit` and `tests/conformance` together because the coverage ratchet counts
on both, and the separate job exists so a conformance failure reads as a
conformance failure instead of arriving buried in a summary of ten thousand
tests. Running beside `unit` instead of behind it, that duplication costs runner
minutes and no wall clock at all — which is what makes keeping it defensible
where before it was merely unnoticed.

### E14-T1 — Every ARC governance object is invisible the moment it is created

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** yes — api/routers/, arc/service/ · **Repo:** contextplane

Goal: the governance objects the ARC admin surface creates can be read back.

**The count, because the shape of this is not obvious one endpoint at a time.**
Of fourteen paths under `/v1/arc/admin`, **thirteen are POST-only**. The single
`GET` is `operator-identity`, and it reads the *caller* — not any governed
object. So there is no way, through the API, to answer:

- which approval verifiers are enrolled
- which exceptions are standing, against what, and until when
- what approval evidence a revision carries
- which source connectors, upload policies or replay corpora are registered

Each of those is a governance object whose whole purpose is to be inspectable
later. An exception in particular is *defined* as a documented deviation, and a
deviation nobody can enumerate is an undocumented one with a paper trail in the
audit log.

**Why this is filed here rather than fixed inside a UI task.** It has now
determined the shape of two screens and will determine two more:

- **E10-T7** shipped with the gap stated on the screen: enrolment shows the
  verifier id once and tells the operator to record it, because revoking later
  needs an id nothing can look up.
- **E10-T9** loses half its stated goal — the register of standing exceptions it
  asks for beside the grant form.
- **E10-T8** and **E10-T10** attach evidence and register sources with the same
  absence underneath.

Four screens papering over one missing read side is four places to un-paper
later. The alternative — each UI task inventing its own workaround — is how a
gap becomes permanent.

**The audit log is not the answer**, and it is worth saying so before somebody
offers it. It records that an object was created. It does not report current
state: an exception granted and then revoked appears twice, and reconstructing
"what is in force right now" from an event stream is a query the operator asking
the question cannot write.

**Scope note.** The read side does not have to be six endpoints. The objects
share a shape — scope, validity window, who approved, whether revoked — and the
first design question is whether one governance-object index serves all of them
or whether the differences are load-bearing. Decide that before writing routes.

**Both transports.** Whatever is added exists in the service, not a router, and
appears on REST and MCP alike — the rule this plan has recorded three times, and
a read-only surface added to one transport is the cheapest possible place to
break it again.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "arc and (list or read)"
    make all

## Task decomposition — thirteenth wave (three defects found while judging E4-T1)

None of these is E4. All three were found by reading the servability and
propagation machinery closely enough to decide ADR-0016, and all three are
pre-existing. Filed separately so a quarantine PR does not quietly carry three
unrelated fixes.

### E3-T6 — `discard` leaves the claim's vectors in the index

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: a discarded claim's vectors leave the index, as a superseded or
unconsolidated claim's already do.

`ClaimService.discard` sets `status='rejected'` on a claim that may be `staged`
and consolidated — that is, currently indexed — and never calls `project_claim`.
The module does not import it. `close_superseded` and `mark_consolidated` both
do.

`embedding_index.py` says `project_claim` is *"Called from the two places that
change whether a claim is servable"*. `discard` is a third, and the docstring
has been counting wrong.

**Not a correctness leak** — every read filters on `status`, so a rejected claim
cannot be served. It is exactly the recall loss retraction exists to prevent:
*"every dead vector in the index occupies a candidate slot that a live one could
have used"*, bounded only by retention expiry. One call to fix, and the
docstring's count to correct with it.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "discard or embedding"
    make all

**Delivered.** One `project_claim` call in `discard`, in the same transaction as
the status write so the row and the index cannot disagree if the request dies
between them, and the docstring's "two places" count corrected to three.

The regression test asserts against `embeddings` rather than through a search,
because **a search returns the same answer either way** — every read filters on
`status`, so the defect is invisible from the result set. That is why it
survived, and it is why the test has to look at the index directly.

It also asserts the `embedding_outbox` row is gone. A queued request left behind
would be re-embedded by the next drain, putting the vector back and making the
retraction look intermittent rather than absent — which is a worse bug to
diagnose than the one being fixed.

Mutation-checked: removing the call makes the test fail with `assert 1 == 0`.

### E3-T7 — The conformance test that holds the servability rules together does not exist

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: the several spellings of "this claim is servable" are held to agreeing by
a test rather than by a sentence claiming a test exists.

`embedding_index.py` states: *"A conformance test holds them to agreeing rather
than a shared string pretending they are one rule."* **No such test exists.**
Nothing under `tests/` references `_SERVABLE_STATUSES` or `_SERVABLE_AS_OF`, or
asserts the two agree.

A docstring asserting a guarantee nobody built is worse than silence, because
the next author reads it and stops looking — which is how this was found, by
someone checking whether a third term could safely be added.

There are three spellings today: `_SERVABLE_STATUSES` in `embedding_index.py`,
`_SERVABLE_AS_OF` in `claim_serving.py`, and an inline variant in
`curation_queue.py`. **The third one should differ and the test must say so**, in
the entry rather than as a discovered surprise: an operator must still see a
discarded or quarantined claim in the curation queue, so the queue's predicate is
deliberately not the serving predicate. A test that forced all three to match
would be wrong, and a test that ignored the third would be checking two things
that are already in one file.

Worth doing before E4-T2, which adds a fourth term to a rule currently
synchronised by prose.

Acceptance:
    .venv/bin/python -m pytest tests/conformance -q -k "servable"
    make all

**Delivered**, and the entry's warning about the third spelling held: the
curation queue's predicate must differ, so it is asserted **negatively** with
the reason in the failure message, rather than left out and rediscovered.

The contract turned out to be **agreement on the status vocabulary, not identity
of the predicate**. A shared constant would make one of the two wrong, because
the read path needs `as_of`-relative terms the index path has no instant to
compare against. So the test reads the status term *out of* `_SERVABLE_AS_OF`
rather than restating it — a restated expectation agrees with the code only
until somebody edits one of them.

A second test pins the split ADR-0016 rests on: `status` unconditional,
`t_invalidated_at` `as_of`-relative. That reasoning now stops being true loudly
rather than silently.

Mutation-checked rather than assumed — adding `rejected` to
`_SERVABLE_STATUSES` fails the first test with the divergence named.

One thing about the writing worth recording. The third test's first draft
located the queue's SQL by guessing at module attribute names, found none, and
asserted over an empty string: a test that passed while checking nothing, which
is the exact failure this file exists to correct. It reads the module source
now, which is less elegant and cannot do that.

### E3-T8 — A third receipt read nobody has listed, in the list's own docstring

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: the overdue-propagation guard covers the receipt reads it is supposed to,
and the set is derived rather than hand-maintained.

`arms.py` names `ContextReceiptService.get` and `.exclusions_for` as unguarded by
`pending_overdue`. `.arms_for` is the same shape, joins the same receipt, and is
on neither the list nor the guard.

The docstring beside that list already makes this task's argument: *"Twice now
this check was wired on the one path in front of somebody -- documented as
covering 'the serving paths', plural, and covering one -- and both times the miss
was found by a reader who went looking for the set."* This is the third time,
found the same way, on the list that records the first two.

So the fix is not only to add `.arms_for`. **A hand-maintained list of read
paths has now been wrong three times**, and the entry should decide whether the
set can be derived — every public read on the service, or every method touching
a receipt table — rather than curated. If it cannot, say why, because the next
occurrence is otherwise already scheduled.

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "overdue or receipt"
    make all

**Delivered, and the finding was larger than a missing entry: two
hand-maintained lists disagreed about the same surface.**

`arms.py` named `ContextReceiptService.get` and `.exclusions_for` as an
unguarded gap "fixing it is not this module's to do". `derivative_handlers.py`
simultaneously described "the context-receipt read surface" as **"a deliberate
answer recorded at the arms rather than an omission"**. One said defect, the
other said intentional, and that is how it stayed unguarded.

Deriving the rule settles all three reads at once. Guard a read exactly when it
serves a column a *blocking* handler rewrites; `receipt_link` does
`UPDATE ... SET item_key = :marker` on `context_receipt_items` and
`context_receipt_exclusions`, and nothing else.

- `exclusions_for` serves that column. **Genuinely unguarded, now fixed.**
- `get` returns the `context_receipts` header, which carries no minimized
  field. `arms.py` named it anyway — **the list was wrong in the other
  direction**, and a guard there could not fire.
- `arms_for` serves `context_receipt_arms`, untouched by any blocking handler.
  Correctly unguarded, and on no list at all.

So the answer to "can the set be derived" is: the **mandatory half can**, and
`tests/conformance/test_overdue_guard_covers_the_blocking_reads.py` now does it.
It extracts the blocking handlers' tables, finds every module whose *code* names
one, and requires each to be classified as a serving path that guards, a writer
that must not, or a declaration that runs no query. A guard that is merely
*present* is not required by the rule — asserting the set both ways would turn a
lower bound into a straitjacket, and the failure this task exists to prevent is
a missing guard, not a spare one.

**The check made the same mistake twice while being written, which is why it is
AST-based.** Its first version matched the table names in `arms.py`'s prose
about this rule and flagged it as an unclassified reader. Its second asked
whether `exclusions_for`'s source contained `pending_overdue` and got a yes from
that function's own docstring — so the mutation test passed with the guard
removed. It now matches call nodes. A check that reads prose as evidence of
behaviour is exactly the failure it was written to end.

One cost accepted: `context.receipts -> workspaces.recall` joins the
import-linter exemption list, taking the counted minority direction from 5 to 6
against 8. The refusal type belongs in a propagation module rather than a
workspace one, and the entry says so — if that list grows again, moving the
exception is the fix rather than a seventh exemption.

---

## Task decomposition — Sixteenth wave (agent authorship and retraining feedback loop)

### E20 — Agent authorship, outcome grading, and prompt-retraining feedback loop

**Kind:** epic · **Status:** done — all ten tasks closed · **Blocked by:** none · **Repo:** contextplane

`contextplane` today improves the *content* of the memory store (consolidation, promotion, calibration of extraction-provider confidence) but has no mechanism for tracking whether a specific agent's own contributions are accurate, or for feeding that back into anything the agent does differently next time. The request driving this epic: track which agent authored each claim, grade claim outcomes, aggregate per-agent accuracy, support a "prompt retraining" loop that turns failure patterns into improved agent instructions, and measure whether accuracy improves after a new instruction version activates.

Two decisions were made explicitly by the user before this plan was finalized, both overriding this repo's existing conventions, and both are recorded here so the reasoning survives the PR:

**Decision 1: the per-actor privacy floor in `learning_reads.py` is removed, uniformly, for every actor kind (human and agent alike) — no carve-out.** That module's docstring currently states individual/per-actor aggregates are forbidden by design ("a surface that must not exist"), enforced by `MIN_COHORT_ACTORS=5`/`MIN_CELL_EVENTS=5` floors. The user decided this enterprise deployment requires per-actor monitoring to be possible for any actor, full stop. This is a genuine privacy-policy reversal, not a narrow technical exception, and ships with an ADR recording it (E20-T1) precisely because it is the kind of decision this repo's own convention says must outlive its PR.

**Decision 2: `actor_kind` classification (human vs. agent vs. degree of autonomy) has no reliable source signal today, and inventing one is out of scope for this epic.** Investigation (not the initial design) found that `upsert_entitlement_actor` — the one function both the REST and MCP auth paths actually call — takes only `(session, tenant_id, oidc_subject, display_name)`. There is no machine-identity signal anywhere in `contextplane/auth/entitlements/resolver.py` to source a kind from, and the `WorkloadIdentity` concept that looks like a fit lives entirely inside ARC's autonomy-envelope subsystem with no connection to this code path. Worse, a human driving Claude Code or VSCode Copilot connects through the *identical* MCP transport an unattended agent would use, so even a transport-based classification would misclassify human-in-the-loop coding sessions as autonomous. This epic does **not** attempt to fix `actor_kind`. Per-agent tracking is built entirely on `author_actor_id`, which is already populated correctly on every claim regardless of what `actor_kind` says about the row it points to.

A related, more valuable insight surfaced during design and is now a first-class part of this epic: **a human's mid-session correction of an agent that was expected to complete autonomously is itself a strong, real-time failure signal** — arguably better than a reviewer's after-the-fact `correct`/`incorrect` verdict, because it pinpoints exactly where the agent needed steering. This is not a new concept to invent: `memory_session_events.kind` already distinguishes `user_message` (human) from `agent_action` (agent-initiated) from `tool_invocation`, ordered by `(tenant_id, actor_id, session_id, seq)`. A mid-session intervention is simply a `user_message` event with a `seq` between two `agent_action` events instead of only at the session's start. Nothing today computes anything from that pattern. This epic adds an **autonomy-rate** metric derived from it, as a second, independent dimension alongside adjudicated correctness — an agent can be accurate-but-needy (lots of hand-holding) or fast-but-wrong, and those are different problems requiring different prompt fixes.

**Closed.** All ten tasks are done, including both decisions this body
recorded as overriding existing convention: the per-actor privacy floor removed
uniformly with ADR-0017 recording the reversal, and `actor_kind` classification
left alone with the investigation that found no source signal written down.

The autonomy-rate metric derived from mid-session `user_message` interventions
shipped as the second dimension alongside adjudicated correctness, which was the
insight this epic gained during design rather than started with.

### E20-T1 — ADR 0017: the per-actor privacy floor is removed, for every actor kind

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: `.develop/adr/0017-per-actor-aggregates-are-no-longer-floored.md`, recording that `learning_reads.py`'s `MIN_COHORT_ACTORS`/`MIN_CELL_EVENTS` floors and the "no per-actor cell, ever" policy they enforced are rescinded, for humans and agents alike, with no `actor_kind`-conditioned exception.

This ADR is written before the code change (E20-T2) that executes it, matching this plan's own convention (e.g. E4-T1, E5-T1, E18-T1 all precede their execution tasks) that a decision outliving its PR gets recorded before the PR that enacts it, not folded into the PR's description where the next reader has to reconstruct why. Sections follow `.develop/adr/README.md`'s required shape:

- **Context**: quotes `learning_reads.py`'s own docstring verbatim (the "individual surveillance and team-performance evaluation are both forbidden" sentence) and states plainly that this epic needs exactly the surface that sentence forbids — a per-`author_actor_id` accuracy read.
- **Decision**: the floor is removed uniformly; there is no `actor_kind` branch anywhere in the replacement code, and the ADR says so explicitly so a future reader does not go looking for one.
- **Assumptions**: numbered, at minimum — (1) an agent's accuracy is an operational fact about a service principal a tenant runs and is entitled to see broken down as finely as it likes; (2) a human author's accuracy, once the floor is gone, is visible at the same granularity, and that is the actual scope of the reversal — it is not agent-specific; (3) `ROLE_AUDITOR`-style access control (E11-T3's precedent: authorization plus a recorded justification) is the correct place to put any residual access restriction on a per-human accuracy read, not a floor on the aggregate itself, since a floor and an authorization check answer different questions ("can this exist" vs. "can this reader see it") and this repo's own E11 already chose authorization over suppression for the audit drill-down.
- **Alternatives rejected**: keeping the floor for humans and exempting only agents (rejected: the user's decision is uniform removal, and a carve-out would be an undocumented two-tier policy of exactly the kind ADR-0016 exists to avoid leaving implicit); keeping the floor and adding an `agent`-only bypass flag (rejected for the same reason, plus it leaves the removed policy half-alive, which the plan's own supersession rule forbids).
- **Consequences**: every consumer of `Floors`/`MIN_COHORT_ACTORS`/`MIN_CELL_EVENTS` is enumerated and dispositioned in E20-T2, not here — this ADR is the "why," T2 is the "where."
- **Dissent**: written honestly per this repo's convention (ADR-0013's own Dissent section is the model) — the counterargument that removing an actor-level floor makes a per-human accuracy figure a performance-management surface indistinguishable from what the original module's docstring called "forbidden," and that "the user decided" is a process answer to a design question, recorded fairly even though the decision stands.

Acceptance:
    test -f .develop/adr/0017-per-actor-aggregates-are-no-longer-floored.md
    grep -q "^## Dissent" .develop/adr/0017-per-actor-aggregates-are-no-longer-floored.md
    grep -q "^## Assumptions" .develop/adr/0017-per-actor-aggregates-are-no-longer-floored.md

**Delivered.** Two things the writing added to what the entry specified.

**The no-carve-out decision has a second, stronger reason than uniformity.**
The entry gives one — a carve-out would be an undocumented two-tier policy. The
ADR adds the one that makes it not a choice: **there is no `actor_kind` signal
to branch on**, so an agent-only exemption would be keyed on a field nobody can
populate correctly, which is not a narrower policy but an arbitrary one. A
reader looking for the missing branch is told explicitly that it does not exist
and why.

**The dissent found a sharper objection than the entry anticipated.** The entry
framed it as "a per-human accuracy figure is a performance-management surface".
The sharper form is that **assumption 3 substitutes authorization for
suppression, and the two answer different questions** — a floor constrains what
*exists*, authorization constrains who *reads*. A surface that cannot be
constructed cannot be leaked by a misconfigured role, an over-broad audit grant,
a log line, a cached response, or a future endpoint that forgets to ask. The
module's own docstring makes precisely that argument, and this decision
overrides it **without refuting it**.

And the substitute is not built. Nothing in E20 requires the
authorization-plus-justification read assumption 3 offers in exchange, so the
honest description of the state after E20-T2 is that the protection was removed
and the replacement was named. E20-T2 should not close without either building
it or recording that it is owed.

Also recorded, so E20-T2 does not sweep it up: `signals/aggregates.py`'s
erasure-differencing withholding is **untouched**. That is ADR-0013's concern
about `privacy_aggregates`, orthogonal to actor cardinality, and removing it
would reintroduce a disclosure this decision says nothing about.

### E20-T2 — Remove the floor: `learning_reads.py` and every consumer

**Kind:** task · **Status:** done · **Blocked by:** E20-T1 · **Hotspot:** yes — service/memory/ · **Repo:** contextplane
**Kind:** task · **Status:** pending · **Blocked by:** E20-T1 · **Hotspot:** yes — service/memory/ · **Repo:** contextplane

Goal: `contextplane/service/memory/learning_reads.py` loses `Floors`, `FloorsTooLoose`, `MIN_COHORT_ACTORS`, `MIN_CELL_EVENTS`, `Cell.suppressed`/`Cell.value`'s null-on-suppress behavior, and `build_breakdown`'s remainder-combination/withholding logic — replaced by a `Breakdown` that always carries every cell's true value, no `partial`/`withheld` states (a breakdown is either built or the query returned no rows), and `LearningReadService`'s three methods (`claim_aging`, `contradiction_backlog`, `promotion_yield`) construct cells directly from query rows with no floor test. The module docstring is rewritten to state the module's new, narrower claim: these are tenant-scope learning aggregates with no suppression, and the sentence "individual surveillance and team-performance evaluation are both forbidden" is deleted, not softened, per ADR-0017.

Every consumer of the removed names is found and updated in the same change, so this is not a half-removal the supersession rule forbids:

- `contextplane/signals/aggregates.py` imports `COHORT_TENANT`, `Breakdown`, `Cell`, `Floors`, `build_breakdown` from `learning_reads.py` (module docstring's own "Three mechanisms... none of them is a step somebody remembers" section is about a *different* concern — erasure-differencing on `privacy_aggregates`, orthogonal to the actor floor) — this module keeps importing `COHORT_TENANT`, `Breakdown`, `build_breakdown` (their shapes still apply to `privacy_aggregates`' feedback/signal-mix metrics, which are not per-actor and are not in scope of ADR-0017), and stops importing `Floors`; its own writer logic already does its own withholding via the erasure-differencing mechanism (mechanisms 1-3 in its docstring), which is untouched.
- Any admin/API route or MCP tool reading `LearningReadService.floors` or serializing a `Breakdown.partial`/`withheld` field is updated to the new shape.
- Test fixtures asserting `FloorsTooLoose` or a suppressed cell are removed or rewritten to assert the new unsuppressed behavior.

Acceptance:
    sh -c '! grep -rn "MIN_COHORT_ACTORS\|MIN_CELL_EVENTS\|FloorsTooLoose\|class Floors" contextplane/ --include="*.py" --exclude-dir=__pycache__'
    sh -c '! grep -rn "individual surveillance and team-performance evaluation" contextplane/'
    .venv/bin/python -m pytest tests/unit -q -k "learning_reads or privacy_aggregates"
    .venv/bin/python -m pytest tests/integration -q -k "learning_reads or aggregates"
    make test-unit && make lint && make typecheck

**Delivered. Three things the entry's consumer list did not name, and all three
were load-bearing.**

**The floor was also a database CHECK, and removing only the code half would
have broken the writer on real data.** Migration 0043 created
`privacy_aggregates` with `CONSTRAINT ck_aggregate_meets_the_floor CHECK
(suppressed OR actor_count >= 5)` — the same rule, written where no application
change can reach it. With the writer no longer suppressing and no longer zeroing
`actor_count`, a window covering four contributors offers `suppressed = false`
with `actor_count = 4` and the insert is refused. **The unit tests could not see
it** — they drive a fake session — so this would have surfaced as an aggregate
worker failing in production. Migration 0072 drops it. Found by taking this
entry's own acceptance grep literally rather than treating it as a formality.

`ck_aggregate_suppressed_carries_no_value` is deliberately kept: `suppressed`
did not disappear with the floor, it changed cause, and a cell withheld by the
differencing rule must still carry no value or that defence leaks through the
column it withheld.

**The OpenAPI contract changes.** `BreakdownOut` loses `floors`, `partial` and
`withheld`; `CellOut` loses `suppressed` and its `value` stops being nullable;
`FloorsOut` is deleted outright. `openapi.json` is regenerated, and
`test_openapi_drift` is what caught it. Checked before assuming: the dashboard
does **not** consume these schemas — its `withheld` references are receipt
exclusions, a different concept — so nothing in `contextplane-ui` breaks.

**Fifty tests across three files covered the floors**, and separating them was
most of the work rather than a tidy-up. Thirteen died with the mechanism; the
rest survive, including every structural absence check (no route names an actor,
none takes a cohort parameter, the response model carries no contributor counts).

Those survivors now assert something weaker, and the files say so: they are
properties of *these three routes*, not a policy the system enforces. A failure
means somebody widened this surface, not that they breached a rule — and nothing
outside those files prevents a new surface from doing what these do not.

**The differencing defence survived intact, which was the thing most at risk.**
`signals/aggregates.py` still withholds a cell whose recompute disagrees with a
published figure; what changed is that `suppressed` now enters the upsert as
`False` always and is driven purely by that statement, rather than being seeded
by a floor verdict from the writer. The relevant unit test —
"the database can withhold a cell this side considers reportable" — passes
unchanged.

**One behaviour changed shape rather than disappearing.** An empty window used
to be stored as a *withheld* cell with a null value, because an empty breakdown
failed the floors. It is now stored as a breakdown with no cells and a total of
zero. The property the test exists for — "computed, nothing to report" is
distinguishable from "never computed" — is unchanged.

**The dissent's warning is now the live state.** ADR-0017 offered
authorization-plus-justification as the replacement for suppression, and nothing
in this task built it. Per-actor aggregation is constructible and the substitute
is named but absent. E20's later tasks should not treat that as settled.

### E20-T3 — Migration: accuracy index and three new tables

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — storage/migrations/ · **Repo:** contextplane

Goal: one Alembic revision, `contextplane/storage/migrations/versions/0073_agent_accuracy_and_instructions.py` (`down_revision = "0072_drop_the_aggregate_actor_floor"`), that:

*Renumbered from the 0071/0070 this entry was written against.* Both were taken
while E20 was being decomposed — 0071 by E4-T2's quarantine column and ledger,
0072 by E20-T2's drop of the aggregate actor floor, which turned out to be a
database CHECK as well as application code. The head moves; the entry's content
does not.

1. Adds `CREATE INDEX ix_memory_claims_author_created ON memory_claims (author_actor_id, created_at) WHERE author_actor_id IS NOT NULL` — lets `AgentAccuracyService`'s join drive from `memory_claims` on actor+window and complete via the existing `ix_memory_adjudication_claim`, instead of scanning `memory_claim_adjudication` per candidate row. The existing lone `ix_memory_claims_author` stays (other readers key on author without a time bound).
2. Creates `agent_failure_pattern_report` table with columns: `report_id UUID PK, tenant_id, author_actor_id, window_start, window_end, n_adjudicated, n_incorrect, n_intervention_sessions, n_sessions, groups JSONB, generated_at, generated_by`. `groups` is a JSONB snapshot storing `[{claim_category, predicate, incorrect_count, total_count, rate, example_claim_ids}]`, matching `memory_calibration_mapping.bins`' precedent of storing a fitted aggregate as an inspectable blob rather than a normalized child table. `CHECK (window_end > window_start)`, `CHECK (n_incorrect <= n_adjudicated)`.
3. Creates `agent_instruction` table with columns: `instruction_id UUID PK, tenant_id, author_actor_id, version, content, motivated_by_report_id UUID FK, status, activated_at, superseded_at, created_at, created_by`. `UNIQUE (author_actor_id, version)`, `CHECK status IN ('active','superseded','rejected')`, `CHECK (status <> 'active' OR motivated_by_report_id IS NOT NULL)` — the literal enforcement that an instruction cannot activate without citing the report that motivated it, as a database CHECK (mirrors `ck_memory_calibration_error`'s precedent of putting the activation gate in the schema, not only the service). `CHECK ((status = 'active') = (activated_at IS NOT NULL AND superseded_at IS NULL))`. Partial unique index `uq_agent_instruction_active ON agent_instruction (author_actor_id) WHERE status = 'active'` — `memory_calibration_mapping`'s own `uq_memory_calibration_active` pattern.

No change to `actors.actor_kind` — deliberately, per this epic's scope decision above.

Acceptance:
    alembic upgrade head
    alembic downgrade -1 && alembic upgrade head
    .venv/bin/python -m pytest tests/integration/test_migrations.py -q
    make test-unit && make lint && make typecheck

### E20-T4 — `AgentAccuracyService`: per-author accuracy, on read

**Kind:** task · **Status:** done · **Blocked by:** E20-T2, E20-T3 · **Hotspot:** no · **Repo:** contextplane
**Kind:** task · **Status:** pending · **Blocked by:** E20-T2, E20-T3 · **Hotspot:** no · **Repo:** contextplane

Goal: `contextplane/service/memory/agent_accuracy.py`, structurally parallel to `calibration.py` (frozen dataclasses + pure aggregation + a thin service over `session_factory`), not to `learning_reads.py` (no `Floors` — none apply after E20-T1/T2).

`accuracy_for(ctx, *, author_actor_id, window_start, window_end, breakdown="overall")` joins `memory_claim_adjudication a` to `memory_claims c` on `c.claim_id = a.claim_id`, filters `c.author_actor_id = :actor AND c.author_tenant_id = :tenant AND a.verdict IN ('correct','incorrect') AND a.adjudicated_at >= :start AND a.adjudicated_at < :end`, grouped by `claim_category`/`predicate` or ungrouped for `"overall"`. `undecidable` verdicts count toward the header total but are excluded from `rate`'s denominator — same justification `calibration.py` already gives for the identical exclusion. **`author_tenant_id`, not `owning_tenant_id`, is the scoping column** — stated in the query's own comment (mirroring `learning_reads.py`'s existing comment distinguishing the two columns for a different query), because getting this backwards would scope an agent's accuracy to claims about a different tenant's subjects rather than to the tenant that ran the agent.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "agent_accuracy"
    .venv/bin/python -m pytest tests/integration -q -k "agent_accuracy"
    make test-unit && make lint && make typecheck

**Delivered.** One statement, grouped or not, with the `undecidable` split done
in `AccuracyGroup` rather than in SQL — one pass, and the arithmetic where a
reader can see it.

**The scoping decision is proved by exactly one test, deliberately.**
`test_accuracy_is_scoped_to_the_tenant_that_ran_the_agent` seeds two claims by
the same agent in the same window, one about this tenant's subject and one about
another tenant's. Both were written by this tenant's agent, so both belong in
its figure. Mutation-checked: swapping to `owning_tenant_id` fails that test and
**passes the other seven** — including the cross-tenant isolation test, which is
why "another tenant's agent does not appear" is not sufficient evidence that the
scoping is right.

Two shapes worth carrying into E20-T5 and T6:

- **`rate` is `None`, never `0.0`, when nothing was decided.** A window whose
  every verdict was undecidable has an *unknown* accuracy, and zero is a
  specific and wrong claim a caller would act on. Being wrong every time is
  `0.0`; the two are different facts.
- **`overall` is summed from the groups, not queried separately.** Two
  statements over one window would eventually differ by a filter added to one of
  them, so disagreement is made unrepresentable rather than tested for.

**No confidence interval, deliberately.** Four of five is 80% and so is eight
hundred of a thousand. `n_decided` travels with every rate so the difference is
visible — weaker than an interval, and what can be justified without deciding
what a "reliable" rate means, which this module has no basis to answer.

E20-T3's migration is 0073 rather than the 0071 the entry named; both earlier
numbers were taken while E20 was being decomposed.

### E20-T5 — `AgentAutonomyService`: intervention-rate from session events

**Kind:** task · **Status:** done · **Blocked by:** E20-T3 · **Hotspot:** no · **Repo:** contextplane

Goal: `contextplane/service/memory/agent_autonomy.py`. Reads `memory_session_events` ordered by `(tenant_id, actor_id, session_id, seq)` (the existing `ix_mse_replay` index) for a given `author_actor_id` over a window. For each distinct `session_id`, finds every `agent_action` row and asks whether a `user_message` row occurs at a later `seq` within the same session *after* the first `agent_action` — that is an intervention, distinct from the session's initiating `user_message` (the human's original kickoff, which is not a correction). A session with zero such later `user_message` rows completed autonomously; one or more marks it intervened.

`autonomy_for(ctx, *, author_actor_id, window_start, window_end) -> AutonomyBreakdown`. This is one query per call (grouped by `session_id`, a boolean `bool_or(kind='user_message' AND seq > first_agent_action_seq)` via a window function), not materialized, matching E20-T4's on-read rationale. Explicitly out of scope: distinguishing a *correction* `user_message` from an unrelated follow-up question in the same session — this epic treats any post-kickoff `user_message` as an intervention, a coarser signal than "was this specifically a correction," and states that simplification here rather than silently assuming it away; refining it is a natural follow-on once real data shows whether the coarse signal is too noisy.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "agent_autonomy"
    .venv/bin/python -m pytest tests/integration -q -k "agent_autonomy"
    make test-unit && make lint && make typecheck

**Delivered.** One statement, one window function: `min(seq) FILTER (WHERE kind
= 'agent_action') OVER (PARTITION BY session_id)` is each session's own boundary
between brief and correction, so no session can be classified against another's
first action.

**The boundary is the whole module, and one test carries it.**
`test_the_opening_message_is_the_brief_and_not_an_intervention`. Mutation-checked
by replacing `seq > first_agent_action` with `seq >= 1`: four tests fail,
including that one. Without the boundary every session is intervened and the
metric reports a constant nobody can act on.

Two decisions the entry did not have to name, both about what *not* to count:

- **A session with no `agent_action` is excluded entirely.** Nothing ran
  autonomously and nothing was corrected, so it is not evidence either way.
  Counting it as autonomous would reward an agent for sessions it never started.
- **The rate is over sessions, not events.** One session steered three times is
  one intervened session. A per-event rate would make a single messy session
  look systemic, and would move when an agent happened to emit more actions.

`intervention_rate` is `None` rather than `0.0` when there were no sessions,
matching E20-T4's `rate` and for the same reason: zero is the specific and
flattering claim that the agent never needed help.

The coarseness the entry flagged is preserved and stated in the module rather
than assumed away: a correction and an unrelated follow-up question are both
post-kickoff `user_message` rows, and both count. That is the honest signal
available without classifying free text.

### E20-T6 — `AgentFailurePatternService`: mechanical failure-cluster aggregation

**Kind:** task · **Status:** done · **Blocked by:** E20-T4, E20-T5 · **Hotspot:** no · **Repo:** contextplane

Goal: `contextplane/service/memory/agent_failure_patterns.py`. `build_report(ctx, *, author_actor_id, window_start, window_end, examples_per_group=5) -> FailurePatternReport` reuses `AgentAccuracyService`'s exact window/scoping parameters (not a parallel redefinition of "the window"). Groups `incorrect`-verdict claims by `(claim_category, predicate)`, each group carrying `incorrect_count`, `total_count` for that group (so the report states both "how often does this group appear among failures" and "how often does this group fail when it appears" — the first alone conflates a predicate the agent uses constantly-and-mostly-rights with one rarely touched and always wrong), and up to `examples_per_group` example `claim_id`s with `value_jsonb` and the adjudicator's `note`, resolved via a lateral join — matching `calibration.py`'s "a bin's value is a sentence anybody can check" standard.

Also calls `AgentAutonomyService.autonomy_for` for the same window and includes `n_intervention_sessions`/`n_sessions` on the report, so a failure-pattern report answers both "what did the agent get wrong" and "how often did it need help," per this epic's two-dimension design.

`build_report` persists its result to `agent_failure_pattern_report` (E20-T3) and returns the `report_id` alongside the dataclass — stated plainly as a write despite the "build" framing, because E20-T3's `ck_agent_instruction_activation_cited`-equivalent CHECK requires citing a stored `report_id`, not a report a human read once with nothing to point back to.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "agent_failure_pattern"

**Delivered.** Groups by `(claim_category, predicate)`, examples through a
lateral join so the cap applies per group rather than to the report, and both
counts on every group.

**The two-count design is the whole value and one test proves it.**
`owned_by_team` failing 2 of 12 and `depends_on` failing 2 of 2 are identical by
raw failure count — and `owned_by_team` sorts *first*. Only the denominator
separates a predicate the agent uses constantly and mostly gets right from one
it touches rarely and always gets wrong, and only the second is worth an
instruction change.

**A clean window still writes a report**, which the entry did not specify and
which E20's premise requires: "nothing went wrong" is the baseline a later
report is compared against, and measuring whether accuracy moved after an
instruction change needs a before.

`total_count` counts decided verdicts, not claims. An unreviewed claim says
nothing about whether the agent got it right, so including it would make a
well-reviewed predicate look worse than an ignored one.

`FailureGroup.rate` has no `None` case, and that is a property of the query
rather than of the class: `_PATTERN_SQL` only emits a group with at least one
incorrect verdict, so the denominator is never zero. Stated rather than
defended with a guard that could not fire.
    .venv/bin/python -m pytest tests/integration -q -k "agent_failure_pattern"
    make test-unit && make lint && make typecheck

### E20-T7 — `AgentInstructionService`: versioned activation, gated and reversible

**Kind:** task · **Status:** done · **Blocked by:** E20-T3, E20-T6 · **Hotspot:** no · **Repo:** contextplane

Goal: `contextplane/service/memory/agent_instructions.py`, mirroring `CalibrationService`'s `publish`/`active_mappings`/`load_active` shape:

- `propose(ctx, *, author_actor_id, version, content, motivated_by_report_id) -> uuid.UUID` — inserts with no path to `status='active'` directly; validates `motivated_by_report_id` resolves to a real `agent_failure_pattern_report` row scoped to the same `author_actor_id`, raising `ValidationError` naming the report id (backs up E20-T3's DB CHECK with an actionable message before an opaque constraint violation would surface).
- `activate(ctx, *, instruction_id, now)` — within one transaction, demotes the current active row for that `author_actor_id` to `superseded` (`superseded_at=now`), then activates the target — the `publish()` pattern exactly. Refuses if `motivated_by_report_id IS NULL` (redundant with the DB CHECK, better error message).
- `rollback(ctx, *, author_actor_id, now) -> uuid.UUID | None` — supersedes the current active row, reactivates the immediately-prior superseded row (`activated_at DESC`) if one exists, else returns `None` (an agent's first version has no predecessor) — built from the same status-transition primitive `activate` already has.
- `active_instruction(ctx, *, author_actor_id)` and `history(ctx, *, author_actor_id)` — read methods mirroring `active_mappings`/`load_active`.

`content` is untouched, unvalidated free text — this service's only job is version lineage, activation gating, and rollback; the content itself is entirely a human/external-process decision, per this epic's design boundary.

**Delivered, with one refinement the entry did not specify and one it did not
anticipate.**

*Specified but worth stating as built:* the activation gate is checked in the
service **and** in the database, and both are kept. The DB CHECK makes the rule
true for every writer; the service check makes the failure legible instead of an
opaque constraint violation. `test_the_database_refuses_an_active_version_with_no_evidence`
drives raw SQL deliberately — the service cannot produce that state, so going
around it is the only way to test the constraint, and going around it is exactly
the writer the constraint exists for.

*The refinement:* `propose` also validates that the cited report is **about the
same agent**. A version citing another agent's report satisfies the foreign key
*and* the activation CHECK — it is the one way to build a fully-constrained row
that means nothing, and only the service can see it.

*The thing the entry's wording would have got wrong:* it specifies rollback
reactivates "the immediately-prior superseded row (`activated_at DESC`)", and
that is right — but the reason matters, because "previous version" reads as
version *number*. After a rollback and a re-activation the two differ:
v1 → v2 → rollback to v1 → activate v3 leaves v2 numerically before v3 while v1
is what was actually in force.
`test_rollback_returns_to_what_was_in_force_not_to_the_previous_number` pins the
temporal reading.

Rollback with no predecessor returns `None` **and leaves the incumbent active** —
asserted, because the obvious implementation demotes first and would strand the
agent with no instruction at all.

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "agent_instructions"
    .venv/bin/python -m pytest tests/unit -q -k "agent_instructions and rollback"
    .venv/bin/python -m pytest tests/integration -q -k "agent_instructions"
    make test-unit && make lint && make typecheck

### E20-T8 — MCP tools: an agent may read its own accuracy/autonomy/patterns, never another's

**Kind:** task · **Status:** done · **Blocked by:** E20-T4, E20-T5, E20-T6, E20-T7 · **Hotspot:** no · **Repo:** contextplane

Goal: three read-only tools added to `contextplane/api/mcp/tools/memory_curation.py`, following `adjudicate_claim`'s exact shape (`ctx = await context._resolve_tenant(...)` first line, manual UUID parsing raising `ToolError`, typed-exception mapping via `_map_error`, `json.dumps(context._serialize(...))` return, Google-style docstring, added to `__all__` and `register()`):

- `get_my_accuracy(window_start, window_end, breakdown="overall") -> str` — **no `author_actor_id` parameter**; always `ctx.actor_id`. Asking about another actor is structurally impossible, not merely gated.
- `get_my_autonomy(window_start, window_end) -> str` — same self-only shape.
- `get_my_failure_patterns(window_start, window_end) -> str` — same self-only shape.

**No MCP tool for `propose`/`activate`/`rollback`.** Those change what an agent does next and belong on an admin surface with a human in the loop (E20-T9/T10) — an agent that could activate its own instruction could self-author its own behavior change with no human involved, a materially different trust boundary than adjudicating a claim (already unrestricted today).

Acceptance:
    .venv/bin/python -m pytest tests/unit -q -k "mcp and (get_my_accuracy or get_my_autonomy or get_my_failure_patterns)"
    sh -c '! grep -n "async def propose\|async def activate\|async def rollback" contextplane/api/mcp/tools/*.py'
    make test-unit && make lint && make typecheck

**Delivered, and two existing gates did real work rather than waving it
through.**

`make lint`'s **registry gate** (E7-T1) refused the build until all three tools
were listed with a tier — "an unlisted tool reaches a default connection with
nobody having decided its tier". Listed as `extended`, so a default connection
still sees eight verbs.

The **parity ratchet** (E7-T2) then refused them as undocumented, holding the
undocumented extended surface at 20. Documented rather than ratcheted up, which
is what the ratchet is for. `docs/05-reference/02-mcp-tools.md` gains a section
that states the self-only property as a *control* rather than an omission, and
explains the two readings most likely to be got wrong: `rate: null` means
unknown and not zero, and a group's `incorrect_count` without its `total_count`
ranks the predicate you use most rather than the one you fail at.

Their `rest` mappings are **null**, not the routes E20-T9 will build. Listing a
path that does not exist yet would fail the parity gate that checks every
registry mapping names a real operation — T9 fills them in when the operations
are there.

The three services are wired into `MemoryServices` and the API container, so the
tools reach them the same way every other tool on this surface reaches its
service.
### E20-T9 — Admin REST surface: accuracy, autonomy, failure patterns, instruction lifecycle

**Kind:** task · **Status:** done · **Blocked by:** E20-T7 · **Hotspot:** no · **Repo:** contextplane

Goal: routes `GET /v1/agents/{actor_id}/accuracy`, `GET /v1/agents/{actor_id}/autonomy`, `GET /v1/agents/{actor_id}/failure-patterns`, `POST /v1/agents/{actor_id}/instructions` (propose), `POST /v1/agents/{actor_id}/instructions/{instruction_id}:activate`, `POST /v1/agents/{actor_id}/instructions:rollback`, `GET /v1/agents/{actor_id}/instructions`. Gated by the same tenant-scoped mutation role this codebase's admin routes already require; mutating routes carry `Idempotency-Key` handling per existing convention (reuse whatever the calibration/promotion-proposal admin routes already use — not reinvented here).

Acceptance:
    .venv/bin/python -m pytest tests/integration -q -k "agent_instructions_route or agent_accuracy_route or agent_autonomy_route"
    make test-unit && make lint && make typecheck

**Delivered, and the entry's suggestion to reuse `HttpMethodRouter` is
withdrawn — it produces a route that cannot work.**

That helper gives a PATCH/PUT/DELETE route a POST-tunnelled twin. These three
are POST-only *actions*, not verbs on a resource. Registering `activate` through
it puts a plain `POST /{actor_id}/instructions/{instruction_id}` beside the
aliased `...:activate`, **and the plain route matches first** with
`instruction_id` bound to `"<uuid>:activate"` — the request 422s on UUID parsing
rather than activating anything. Caught by the integration test, not by review.

They are plain `@router.post` routes with the action in the path, which is the
shape this codebase already uses for `POST /claims/{claim_id}:link`.

**The admin gate is asserted, not assumed**, and the router docstring says why:
before the per-actor floor was removed an actor-level figure could not be
constructed at all, so this authorization is not the outer layer of a defence —
it is the whole of it. `test_a_consumer_cannot_read_another_actors_figures`
pins it.

`GET /failure-patterns` **writes**, and both the route docstring and the OpenAPI
description say so rather than hiding it: it stores the report and returns its
id, because an instruction change has to cite a stored one.

The registry's three `rest` mappings, left null in E20-T8 because the routes did
not exist, are now filled in — and the parity gate that checks every mapping
names a real operation passes.
### E20-T10 — Admin dashboard: agent performance and instruction lifecycle screens

**Kind:** task · **Status:** done · **Blocked by:** E20-T9 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: new feature `apps/admin-dashboard/src/features/agents/` (a distinct top-level feature — it answers "how is this agent principal doing," a different question from `memory`'s claim-curation surfaces or `analytics`'s usage-volume surfaces, per this repo's business-vocabulary-over-generic-buckets convention). Reuses existing composition primitives (`PageContainer`/`PageHeader`/`SummaryStrip`/`TableSection`/`EmptyState` — the same pieces `AnalyticsPage.tsx` and `SettingsPage.tsx` already use):

- Accuracy view: overall rate in a `SummaryStrip`, `claim_category`/`predicate` breakdown in a `TableSection`.
- Autonomy view: autonomy rate alongside accuracy — presented together, not on separate screens, since this epic's design treats them as two dimensions of one question.
- Failure-pattern view: table of `(claim_category, predicate, rate, example count)`, expandable to example claims; `EmptyState` when a window has no failures.
- Instruction lifecycle view: current active instruction's content and `activated_at`; a form (React Hook Form, one `useForm` at this feature's boundary per CLAUDE.md) to propose a version citing a `report_id` selected from a `<select>` populated from the agent's own failure-pattern reports (not free-text UUID entry — enforces the "must cite a report" discipline as a usability layer on top of the DB CHECK); an activate action; a rollback action behind a confirmation step (state-changing, hard-to-undo-again, on live agent behavior).
- Generated OpenAPI client types for the new routes — never hand-written DTOs, per CLAUDE.md.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "agents"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

**Delivered** in contextplane-ui#23, with the contract pin bumped to `0277c66`
in the same PR. Four things worth carrying forward.

**An unmeasured rate is not a zero one, and the whole surface turns on it.** The
service returns `null` when nothing was adjudicated, which means the opposite of
"wrong every time". The adapter keeps the `null`, the model renders it "Not
measured", and every rate is shown beside the counts it came from. Folding
`null` to `0` would report a failing agent where the service reported an
unmeasured one.

**Failure patterns rank by rate, never by volume** — the reason the report
carries both counts. The test uses the pair that makes it concrete: a predicate
at 40 incorrect of 1000 must sort *below* one at 3 of 4.

**The instruction in force is read off `status`.** A proposal is a row too, and
reading the highest version would name a proposal as governing live behaviour —
in exactly the state this screen exists to let somebody fix. The test sets the
active instruction to version 2 and a proposal to version 3, so a version-based
implementation fails it.

**Rollback is hidden, not merely confirmed, when there is nothing behind the
current instruction.** It restores the previously *active* one, ordered by
`activated_at`; with one activation ever, offering the button would promise a
result the server declines to produce.

**One deviation, stated rather than silent.** The entry specified a `<select>`
for the report picker. The repo lints native selects out in favour of
`SearchableSelect`, so it is that behind a `Controller`. The lint rule is the
enforced convention and outranks the entry's prose.

## Out of scope for E20

- **`PERSONA_AGENT` configurability.** `claim_serving.py`'s `PERSONA_AGENT` governs claim-serving depth/category filtering — an orthogonal concern to this epic's authorship/grading/retraining loop. Dropped rather than silently omitted.
- **`actor_kind` classification of humans vs. agents.** See Context above — no reliable signal exists today.
- **LLM-generated instruction content.** This epic builds the versioning/gating/measurement scaffold; content is authored externally.
- **A scheduled worker for accuracy/autonomy materialization.** Computed on read; see ADR-0013 rationale above.

