# Plan — agent simulation and judged evaluation

Epic-level seed for the nineteenth wave. Numbering continues
`governed-agent-memory.md`, which holds E1–E23; this file holds E24 and is a
separate file because its subject is a new capability rather than a further
decomposition of the evaluation surface E22 built.

**Supersession rule applies, and this epic exercises it immediately.** E22-T15
drew a boundary that this epic deliberately crosses. The reversal is recorded in
the epic body and in an ADR before any task is claimable, because an accepted
boundary removed silently is worse than one never drawn — the next reader finds
two answers and no record of which won.

---

### E24 — Agent simulation and judged evaluation: a response to grade, a judge that is not the candidate, and improvement paths that do not presume a cause

**Kind:** epic · **Status:** open · **Blocked by:** none · **Repo:** contextplane, contextplane-ui

The user's framing, in their words, because the plan should not restate it into
something easier to build: *"a user enters a prompt and then is able to run tests
and grade the response … Based on the response, the user may find there is some
missing or incorrect context, the user must be able to help improve the context
by submitting changes or changing or updating the configuration related to how
context is retrieved."* Asked what "the response" is, they answered: *"I would
want the response to be a contextplane + llm (simulating a particular agent) and
response gradeable by a user (human)."* Asked how a failure routes to a fix, they
corrected an early draft of this plan: *"if something doesn't pass, there is an
opportunity to improve something, **do not assume there is one path to improve
it**."* That correction is load-bearing and shapes E24-T12.

## What already exists, measured rather than assumed

An earlier pass of this planning read a stale branch and reported four blockers
that had already cleared. The corrected inventory, read from `main`:

| Capability | State on `main` | Where |
| --- | --- | --- |
| Prompt sets, runs over them, persisted verdicts | **shipped** | `/v1/evaluation/prompt-sets`, `…/prompts`, `…/runs`, `/v1/evaluation/runs/{run_id}`, `/v1/evaluation/runs/items/{item_id}/verdict` |
| Agent registry — list, and declare kind and owner | **shipped** | `GET /v1/admin/actors`, `POST /v1/admin/actors/{actor_id}/declare`, ADR 0019 |
| Instruction set declared to the resolver | **shipped** | `ContextResolveRequest.instruction_digest`, ADR 0020 |
| Instruction delta served as a block | **shipped** | envelope is five blocks: `canonical`, `arc`, `observed_claims`, `workspace`, `instructions` |
| Which delta reaches whom | **shipped** | ADR 0021, three scopes, tenant scope requires a non-author approver |
| Feedback readable, not write-only | **shipped** | `GET /v1/context/feedback` |
| Receipt and tenant directories | **shipped** | `GET /v1/receipts`, `GET /v1/tenants` |
| An LLM provider layer | **shipped, for extraction** | `contextplane/extraction/{anthropic,openai}_provider.py`, `provider_registry.py`, `factory.py` |
| A frozen evaluation protocol and a deterministic scorer | **shipped, workspace-only** | `contextplane/context/evaluation/` — six modules |

Three consequences follow, and each removes work an earlier draft of this plan
had scheduled.

**The agent registry is not a blocker.** It was, when this planning began against
a stale tree; E22-T7 and ADR 0019 closed it. `actor_kind` is declared, never
inferred, and an undeclared principal is `unknown` rather than defaulted to
`human` — which is exactly the property a simulator needs, because simulating a
principal nobody has declared as an agent should say so rather than proceed.

**The instruction plumbing is not a blocker either, and it is better than what
this epic would have specified.** ADR 0020 refused to make Contextplane the store
of record for an agent's base instructions and instead had the caller declare a
digest; ADR 0021 then decided which corrections reach which caller. So the
simulator does not need to invent "what was this agent told" — it declares the
digest like any other caller and reads the fifth block back. The one thing it must
carry forward is ADR 0020's third assumption: *no instructions declared* and
*instructions declared and empty* are different states and every evaluation
surface must distinguish them.

**The envelope has five blocks, not four.** Every rubric, every check, and every
UI pane in this epic counts five. This is written down because the four-block
figure appears throughout `ContextLabPage.tsx`, in `contextLabModel.ts`'s
`contextBlockOrder`, and in three of this file's older epics, and a scorer that
silently ignores the fifth would report a perfect precision score on a resolution
whose instruction delta was wrong.

## What E22-T15 decided, and why this epic reverses it

E22-T15 shipped the evaluation surface and set its boundary by quoting the screen
it was extending:

> `ContextLabPage.tsx:256` states it plainly: *"The resolver retrieves context
> only. It does not call a language model, generate an answer, or invent an
> evaluation score."* That boundary holds — an evaluation run resolves context and
> records a human verdict; it does not generate a response and it does not score
> itself.

**That was the right call for E22 and it is being reversed on the user's
decision.** The reversal is narrow and the narrowness is the whole argument:

1. **The resolver still does not generate.** `POST /v1/context/resolve` is
   unchanged by this epic. Its sentence stays true, and E24-T1 keeps it on the
   screen rather than deleting it, because a reader who has just watched the
   product produce an answer needs to know which component did not produce it.
2. **Simulation is a separate, receipted operation.** A new endpoint composes
   resolve with a model call. The two remain distinguishable in the record, so
   "the retrieval was fine and the agent fumbled it" stays an answerable question
   — which is the entire diagnostic value of the split.
3. **Nothing scores itself.** This is the clause that has to survive intact.
   E22-T15's "it does not score itself" was protecting against a system grading
   its own homework, and `evidence.py` enforces the same separation in code —
   `EVIDENCE_CARRIES_NO_DECISION` is asserted by the conformance suite. E24
   honours it by construction: **the judge is never the model under test**, and
   the deterministic scorer never asks the system under test whether it was
   right.

The supersession is therefore of one sentence in one task, not of E22-T15's
mechanism. Prompt sets, runs, and persisted verdicts are consumed by this epic
unchanged. E22-T15's status stays `done`; its boundary clause is annotated as
superseded by E24 with a pointer, per this file's rule that a replaced mechanism
is removed or its survival is explained where the reader will look.

## The judge, and the five ways it goes wrong

Industry practice on LLM-as-judge has converged since 2025, and three of its
findings are specific enough to design against rather than to cite. The plan
records them because each maps onto a mechanism this repository already has.

**Self-preference is measured, not hypothetical.** Reported at 10–25 % uniform
bias, with the standing rule that the judge must never be the same model as the
candidate. This epic makes that a **constraint the service enforces**, not a
configuration an operator can get wrong: a simulation whose agent model and judge
model share a provider family is refused, and the refusal names the two models.
An advisory note in a docstring would be the shape of guidance that is followed
until the day it matters.

**Position, verbosity and format bias are properties of the prompt template,**
which is why the pinned tuple below includes the template hash and not only the
model id.

**Judge confidence is a self-report on an uncalibrated scale — the exact problem
`contextplane/service/memory/calibration.py` already solves.** That module's
argument transfers without modification: *"There is no mapping yet, and the honest
form of that is no mapping at all. Not an identity mapping: identity asserts that
a model reporting 0.9 is right nine times in ten, which nobody has checked, and
storing that assertion under a version string is how an unexamined number acquires
an authoritative look."* A judge's confidence has the same shape and deserves the
same treatment: recorded from the first run, contributing nothing until a fit
exists, fitted in bins from human confirmations, and a fit that misses its bound
stored and never selected.

The corollary governs the UI and is not negotiable: **until a fit exists for a
given judge, a judge verdict is displayed as unproven.** A confident-looking score
on the screen whose job is calibrating trust is the same defect ADR 0019 refused
when it rejected inferring `actor_kind` — a confident label on a guess, in the
place least able to absorb one.

**The rubric is frozen the way the protocol already is.** `protocol.py` freezes by
digest and not by date, over both the protocol values and the scorer's source,
and a run whose freeze does not match is *invalid, not adjusted*. The judge rubric
joins that discipline. The pinned tuple is `(judge_model_id, rubric_version,
prompt_template_hash)`; editing a rubric mints a version; a run keeps the version
it ran under; and a comparison spanning two versions warns rather than silently
conflating them. Industry guidance puts the drift from a judge model change alone
at 3–8 points on an unchanged rubric, which is larger than most regressions
anybody is looking for.

**A panel is for gating, not for every keystroke.** Three frontier judges from
three families with majority vote is the defensible default for a launch decision
and costs 3×. Forcing it on interactive iteration would tax the fast loop to
insure a decision nobody is making yet. So: one differently-familied judge for an
interactive simulation; an opt-in panel for a prompt-set run that is gating
something. E24-T8 builds the panel; E24-T5 builds the single judge; neither
pretends to be the other.

## What is scored, and by what

Five criteria. Three exist in frozen form already and are generalized; two are
new because they need a response to score. The split by judge type is the point:
arithmetic where arithmetic is possible, a model only where it is not, and a
human over both.

| # | Criterion | Judge | Implicates | Origin |
| --- | --- | --- | --- | --- |
| 1 | Required-fact recall | deterministic | memory | `judge.py` RUBRIC clause (1), generalized to five blocks |
| 2 | Boundary violations — tenant, audience, classification, lifecycle | deterministic | governance | `judge.py` RUBRIC clause (2), `VIOLATION_KINDS` unchanged |
| 3 | Precision | deterministic | memory | `judge.py` RUBRIC clause (3), generalized to five blocks |
| 4 | Groundedness — every assertion traceable to a served item | LLM, with human override | the agent | new |
| 5 | Answer relevance | LLM, with human override | the agent | new |

**The fourth column is load-bearing and is why this epic needs no second
evaluation surface.** Asked whether memory evaluation and agent evaluation should
be separate user journeys, ADR 0024 decided they should not: the attribution a
split would buy is already present inside one result, because a failure of 1 or 3
implicates what was served and a failure of 4 or 5 implicates what the agent did
with it. The two deterministic memory criteria are computed with no model in the
loop, which is what keeps that attribution independent of the judge. E24-T12
groups the pane by this column; it mints no new score and no new vocabulary.

**No partial credit anywhere**, inherited verbatim: *"a required fact is present
or it is not, and a 'nearly matched' item is a missed one."* A boundary violation
fails the case outright regardless of the other four, because the scenario declared
the boundary in advance and the judge asks the scenario, never the system under
test.

**Generalizing the deterministic scorer is real work and is not a rename.**
`treatments.py` holds the canonical, governance, claim and resume paths identical
across every configuration and varies only the workspace arm — that is what made
its numbers attributable. Scoring five blocks means the scorer stops being an
ablation harness and starts being a scorer, and `precision` in particular is
currently defined over *served workspace items* and has to be redefined without
quietly changing what the existing measurement meant. E24-T4 owns that, and it
mints a new rubric version rather than re-scoring old runs under a changed one.

## Where expectations live, and when they are written

**Before the run, always.** `scenarios.py` states the mechanism and this epic
adopts it without weakening it: *"a scenario whose required facts were written
after seeing what the system returned would be satisfied by whatever the system
returned."* The shipped prompt-set schema already carries `intent_note` — *"what
this prompt is checking"* — which is the prose form of the same idea and the
natural place to hang the structured form.

**Presets, amendable per persona.** The user asked for *"here is a best practice,
but you may amend for a given persona."* A persona is a named preset over the same
five criteria, not a separate rubric: a compliance preset pins the classification
ceiling and tolerates zero boundary violations; a research preset relaxes precision
and keeps recall. Presets are seeded, editable, and versioned with the rubric they
parameterize.

## Size: what the product already bounds, and what it does not

Recorded because an earlier draft of this plan asserted the opposite and the
correction changes the design.

**ARC already enforces a byte budget and it is the model to copy.**
`arc/service/bundle.py` carries `budget_limit_bytes`, the status
`blocked_budget_exceeded`, and a separate `CAP_FACTS_BUDGET_BYTES = 4 * 1024` so
that informational material can never crowd out an obligation. Its governing rule
— *budgets change presentation, not obligations* — is exactly right for a
simulator: if the mandatory set does not fit, the answer is blocked, never a
quietly shortened list, because *"a truncated obligation list that still says
`ready` is the worst possible output: the agent believes it knows what it must
do."*

**The other four blocks bound cardinality, never size.** `limit` is 1–200 per arm
(default 25); `workspaces/recall.py` caps candidates at 50; `ClaimQuery.MAX_LIMIT`
is 100. The single occurrence of a token budget anywhere in the service is
`EMBEDDING_CHUNK_TOKENS = 400`, which splits fact bodies before embedding and has
nothing to do with responses.

**So the simulator reports tokens exactly and never estimates.** The provider
contract already forbids guessing — *"Usage is reported, not estimated … it never
substitutes zero for a field the API omitted"* — and `UsageSource` distinguishes
`provider_reported` from `estimated` from `unknown` precisely so a spend figure
cannot silently mix them. A token figure is paired with the cardinality that
produced it, because `limit` is the only lever the product actually offers when a
run comes back too large.

## The improvement surface: signals, never a diagnosis

The user's correction is the specification. A failing run does not have *a* cause,
and a UI that names one is guessing on the screen where guessing is least
affordable. So the surface presents **observations with their evidence**, several
at once, unranked, each naming what could be adjusted without asserting that it
is the fault.

| Observation, from the run's own record | What it could point at |
| --- | --- |
| Items served but cited by no assertion | scope too wide, or the agent ignored them |
| An assertion citing no served item | a fact missing from the graph, or a groundedness failure |
| A block `degraded` or `failed`, with its carried reason | retrieval configuration, or a source whose breaker tripped |
| Rows in the receipt's exclusions | governance withheld it — PII policy, classification, ARC |
| Canonical empty while claims is full | something true is stuck unpromoted |
| A served item stale or contradicted | the claim needs adjudication |
| The instruction block carries a contradiction note | the delta and the declared set disagree — a Judgement event |

**Ranking is refused, and for a reason this repository has already written down.**
`curationModel.ts` states it for the reviewer queue: *"Confidence does not move a
row, and nothing here weighs what getting it wrong would cost."* An observation
list ordered by a confidence the product has not calibrated would invite exactly
the deference that sentence exists to prevent.

**Cited-versus-ignored is already expressible in the shipped vocabulary.**
`signals/feedback.py` accepts thirteen ratings; the dashboard writes three. The two
this epic needs — `selected` and `ignored` — exist, as do `missing`, `incorrect`,
`stale`, `contradicted` and `unsafe`. Nothing new is minted.

**Every destination already exists.** Policy authoring is complete end to end
(`ArcArtifactDialog`, `ArcDirectiveEditor`, `ArcLifecyclePanel`, through validate →
semantic tests → submit → approve → activate). Claims, curation, quarantine and
promotions ship. Extraction strategies, promotion policy, the autopromote
allowlist and calibration ship. Agent instruction propose/activate/rollback ships
— gated, per E20-T7, on a stored failure-pattern report, which E24-T12 treats as a
feature rather than friction: it makes a Lab finding into citable evidence instead
of an opinion. **The improvement surface links out with a filter applied and
rebuilds none of them.**

## The contract pin is seventeen paths behind, and it is not a broken adapter

Named precisely because an earlier draft of this plan called it a bug. `main` on
the service publishes **228 paths**; `contextplane-ui`'s committed
`contracts/openapi.json` pins **211**. `GET /v1/receipts`, `GET /v1/tenants` and
all five `/v1/evaluation/…` paths are in the first and absent from the second.

The dashboard's `shared/api/directory.ts` calls the first two through the raw
`client.request` path rather than the generated client, so it **works at runtime**
— those endpoints shipped in `contextplane#138`. What it does not do is pass through
the boundary the delivery process defines: *"`contextplane-ui` vendors `openapi.json`
as a committed file; `generate:api` reads only that file."* Regeneration is not dirty,
because nothing generated changed, so CI is green on a dashboard coupled to service
behaviour its own pinned contract does not describe.

This is a pin bump, not a repair, and E24-T9 owns it. It is listed first among the
dashboard tasks because every other one needs the evaluation schemas.

## The dashboard has no evaluation screen at all

`git ls-tree -r main` in `contextplane-ui` matches nothing for `evaluation` or
`prompt-set`. The service shipped prompt sets, runs and persisted verdicts in
`contextplane#136`; the dashboard consumes none of it. So the surface E22 named —
*Served: what did the machines actually get, and was it right?* — currently holds
Receipts, Context Lab and Sessions, and the one destination that would answer its
question is missing.

That gap is where this epic lands. `ContextLabPage.tsx` is 1,288 lines and much of
it survives: the block rendering, the eight trust labels, the receipt trace with
exclusions and references, and the item-level feedback are all correct and read
state the service carries rather than inferring it. What changes is the page around
them — one column of "type a prompt and scroll" becomes agent, declared
instructions, prompt and expectations above three panes.

## One journey, not two — ADR 0024

The obvious next question, asked and answered before any screen below is cut: if
this epic grades an agent's answer, does memory quality get its own journey beside
it? **No, and the reasoning is recorded in ADR 0024 in `contextplane-ui` rather
than here, because it is a dashboard IA decision that outlives this epic.**

The case for splitting is real and is not dismissed. Mid-2026 research is explicit
that memory quality inferred from end-task success is confounded — MemDelta names
agent architecture, task bias and retrieval strategy as three variables that move
task outcome independently of what was stored, and asks for controlled baselines
that measure memory in isolation first. Memory has its own benchmark suite now.

It lost on two grounds. A fork at the door makes the evaluator name the failing
component before they can know it, which is exactly what E24-T13 refuses to do on
the user's instruction that there is not one path to improve a failing run. And a
second spine duplicates prompt sets, runs, verdicts and comparison, which E21
already diagnosed as the defect it is. The confound is answered instead by two
things this epic keeps: the deterministic criteria run with no judge, and the
aggregate memory measurement stays a separate controlled report — see below.

`.develop/DESIGN.md` in `contextplane-ui` already draws the axis that governs
here, and it is the axis LangSmith, Langfuse and Phoenix all use: **offline
evaluation of curated examples versus online observation of live activity**, which
"must not share an unlabeled scorecard". Grading-target is not a second axis of
navigation; it is a field on a result. That standard is not replaced by ADR 0024,
which decides only the question the standard leaves open.

**Consequences for the tasks below.** E24-T12 groups the score pane by the
implication column rather than by judge type. E24-T13 keeps its unranked
observation list and gains no component filter at the entry point. Neither T10 nor
T11 introduces a second evaluation entry point.

## Alternatives rejected

- ***Keep E22-T15's boundary and grade retrieval only.*** Declined by the user
  after it was offered as the recommended option. Recorded with its merits intact
  — every verdict would trace to server truth and no model would be in the loop —
  because the reason it lost is not that it was unsound but that it does not answer
  the question asked: whether the *agent* gets it right, which is unanswerable
  without the agent.
- ***Call the model from the browser.*** Offered as two variants — a same-origin
  proxy, and a session-scoped key held in memory — and both declined in favour of
  a service endpoint. The user's choice is also the one that survives scrutiny:
  a browser-side call cannot be receipted, cannot be reached over MCP, and would
  put the judge-is-not-the-candidate constraint in the one place an operator can
  edit it.
- ***A model-backed judge for the deterministic three.*** Rejected on `judge.py`'s
  own argument: *"a model-backed judge introduces a second thing whose behaviour
  can drift between the baseline run and the treatment run, and a difference in
  the final number would then have two possible causes with no way to tell them
  apart."*
- ***A single blended score.*** Rejected. Five criteria with no partial credit
  produce five answers; averaging them into one number would let a boundary
  violation be offset by good prose, which is the one trade the safety criterion
  exists to forbid.
- ***An ensemble on every run.*** Rejected as the default and kept as an opt-in.
  3× cost on exploratory iteration buys insurance against a decision nobody is
  making at that moment.
- ***Ranking the improvement observations.*** Rejected on the user's explicit
  instruction and on `curationModel.ts`'s precedent.

## Out of scope for E24

- **Automated retuning of retrieval from evaluation results.** E22 already
  excluded it and the exclusion stands verbatim: *"a loop that retunes retrieval
  automatically from feedback is a different epic with its own safety argument, and
  building it inside a usability epic is how it would ship without one."* The
  evaluator sees, judges, and changes.
- **Generating instruction text.** Unchanged from E20 and E22. This epic simulates
  with the instructions in force and makes a delta judgeable; it does not write one.
- **Making the resolver generate.** `POST /v1/context/resolve` is untouched.
- **A new judgement queue.** Judge-versus-human disagreement is hosted on the
  Judgement surface using E5-T6's shipped reviewer cockpit and its two
  non-negotiables, not on a second queue with its own conventions.
- **Visual redesign.** `.develop/DESIGN.md`'s visual language is not in question.
- **The aggregate memory-quality report, which is E8's and not this epic's.**
  Per ADR 0024 the trend over the system — E8's recall@10, extraction precision
  and recall per predicate, retrieval precision joined through receipts,
  multi-session recall, and the `treatments.py` ablation — is a report, not a
  journey, and it is not built inside E24-T12's score pane. E24-T13 links out to
  it the way it links out to every other destination it does not rebuild. The
  handoff, including what E8 must amend before it can cut the task, is stated
  under *What this epic does not close*.

---

### E24-T1 — ADR 0025: the resolver does not generate, and simulation is a separate receipted operation

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

**Renumbered from 0022, and the correction is recorded rather than silently applied.** This entry was written naming ADR 0022 and E24-T2 naming ADR 0023. Both numbers were taken before either task was claimed — by `0022-a-migration-is-a-lot-and-a-lot-is-sampled.md` and `0023-a-sample-is-drawn-from-the-lot-it-accepts.md` — and `contextplane-ui` had meanwhile taken 0024 for the one-journey decision. The sequence spans both repositories on purpose, because *"two ADR 0004s would be a citation nobody can resolve"*. So the acceptance commands below name 0025 and E24-T2's name 0026. The generalizable finding, which is why this paragraph exists rather than a silent edit: **a plan entry that pins a number to a shared sequence is stale the moment anything else claims one**, and grounding the entry before claiming it is what caught this rather than a colliding file.

Goal: `.develop/adr/0025-simulation-is-separate-from-resolution.md`, recording
the reversal of E22-T15's boundary clause and, more importantly, recording exactly
how narrow the reversal is.

- **Context**: quotes E22-T15's boundary clause in full and `ContextLabPage.tsx`'s
  sentence that it rests on. States that the sentence remains true of the resolver
  and is kept on screen.
- **Decision**: a new operation composes resolution with a model call and receipts
  both halves separately, so "retrieval was right and the agent fumbled it" stays
  answerable. `POST /v1/context/resolve` is unchanged.
- **Assumptions**, numbered: (1) a deployment with no provider configured has
  simulation switched off and evaluation still works, matching
  `extraction/provider.py`'s "a working deployment with one feature switched off,
  not a broken one"; (2) the simulated agent is a declared principal per ADR 0019,
  and simulating an `unknown` principal is refused rather than defaulted; (3) an
  agent that declared no instruction digest is distinguishable from one that
  declared an empty set, per ADR 0020's third assumption.
- **Alternatives rejected**: making the resolver generate (loses the split that
  makes the diagnosis possible); a browser-side model call (unreceiptable,
  unreachable over MCP, and puts the judge constraint where it can be edited).
- **Dissent**: that composing two operations behind one endpoint recreates the
  fusion ADR 0011 refused for envelope blocks; answered by requiring the receipt
  to carry the resolution and the generation as separately addressable records,
  not one fused row.

Acceptance:
    test -f .develop/adr/0025-simulation-is-separate-from-resolution.md
    grep -q "^## Dissent" .develop/adr/0025-simulation-is-separate-from-resolution.md
    grep -q "^## Assumptions" .develop/adr/0025-simulation-is-separate-from-resolution.md

### E24-T2 — ADR 0026: a judge is never the candidate, and its confidence is uncalibrated until fitted

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Renumbered from 0023 for the reason recorded under E24-T1.

Goal: `.develop/adr/0026-the-judge-is-not-the-candidate.md`, deciding the four
forks the epic body raises and pinning the freeze.

- **Context**: self-preference bias measured at 10–25 %; position, verbosity and
  format bias as prompt-template properties; judge-model drift of 3–8 points on an
  unchanged rubric. Cites `calibration.py`'s argument as the in-repo precedent for
  refusing an unexamined confidence.
- **Decision**, in four parts: (1) **the constraint is enforced, not advised** — a
  simulation whose candidate and judge share a provider family is refused, and the
  refusal names both models; (2) **the pinned tuple** is `(judge_model_id,
  rubric_version, prompt_template_hash)`, carried on every judged result;
  (3) **judge confidence is uncalibrated until a bin fit exists** for that tuple,
  recorded from the first run and contributing nothing until then, with a fit that
  misses its bound stored and never selected; (4) **rubric edits mint a version** and
  a comparison spanning versions warns rather than conflating.
- **The part that is not a fork and is recorded anyway**: the deterministic three
  are never model-judged, on `judge.py`'s drift argument, quoted.
- **Alternatives rejected**: same-family judging with a bias correction factor
  (a correction nobody has fitted is the identity mapping `calibration.py`
  refuses); a panel on every run (3× cost on iteration); trusting judge
  self-reported confidence as a probability.
- **Dissent**: that requiring a second provider family makes simulation
  unavailable to a single-provider deployment; answered rather than dismissed by
  requiring that deployment to be told which two families it needs and by keeping
  the deterministic three fully available without any judge at all.

Acceptance:
    test -f .develop/adr/0026-the-judge-is-not-the-candidate.md
    grep -q "^## Dissent" .develop/adr/0026-the-judge-is-not-the-candidate.md
    grep -q "^## Assumptions" .develop/adr/0026-the-judge-is-not-the-candidate.md

### E24-T3 — `POST /v1/evaluation/simulations`: resolve as an agent, then answer

**Kind:** task · **Status:** done · **Blocked by:** E24-T1 · **Hotspot:** yes — openapi.json + generated client · **Repo:** contextplane

Goal: one operation that resolves context as a declared agent, generates a
response from the five-block envelope and the instructions in force, and returns
both with the citations linking them.

Not new provider plumbing. `extraction/{anthropic,openai}_provider.py` behind
`provider_registry.py` and `factory.py` already give tool-use with a schema the
model must call (*"a model that returns prose instead of calling the tool has
failed, and failing is the correct outcome"*), exact never-estimated usage, a key
read once and never logged, and graceful no-provider degradation. This task adds a
second consumer of that layer, not a second layer. Decomposition confirms whether
the provider contract needs widening or whether a sibling protocol beside
`ExtractionProvider` is the cleaner seam.

Citations are the mechanism the whole epic rests on: the response must name the
`receipt_item_id` values it used, through the same tool-use containment extraction
already relies on, so *cited* and *ignored* are facts about the run rather than a
later inference over prose.

Per the standing rule, the guard lives in the service and not in a router: the
declared-principal check from ADR 0019 assumption 2 and the family constraint from
ADR 0026 are enforced in the service method both transports reach.

Acceptance:
    make lint format-check typecheck && make test-coverage && make test-integration

**Shipped. The seam decision, and four things the entry did not name.**

**A sibling protocol, not a widened one.** Nothing an `ExtractionRequest` carries
means anything to a generation call — no session events, no strategy, no
permitted predicates — so a widened protocol would hand every extraction adapter
three fields to ignore and every generation adapter four. `ResponseProvider` sits
beside `ExtractionProvider` in `extraction/`, over the same `adapter_kit`
transport, the same `TokenUsage` contract, the same containment boundary and the
same metrics. One seam, two consumers, as ADR 0025 required.

**Absence is `None`, not a no-op, and that is a departure with a reason.**
`factory.build_provider` returns `NoOpProvider` because extraction is a
background drain that should pause silently rather than raise every tick. A
simulation is a person clicking a button, so an empty answer that looks like a
model with nothing to say is the wrong report. `build_response_provider` returns
`None`, the service raises, and the route answers `501` — not `503`, because the
capability is absent *on this deployment* until somebody configures it, and
`503` tells a caller to retry something retrying cannot fix.

**Three status codes, each carrying a different remedy.** `409` for a same-family
judge (the deployment is reachable and what it is configured as is the problem),
`501` for no provider, `502` for a provider that failed — and the resolution's
receipt is written before the generation is attempted, so the record is complete
even when the response to the caller is not. That is ADR 0025's dissent, answered
in the order of operations rather than in prose.

**Citations are three tables, and a citation naming something never served is
stored as declared.** A foreign key would turn *"the model cited an id that was
not in the envelope"* — the finding a groundedness check exists to produce — into
a failed write with no record of what the model said. `was_served` is computed at
write time by the one component holding both sides.

**A near-miss the gates caught, and it is the third of its kind.** This
migration's instruction-state constant was first spelled the same way as the
curation vocabulary's. `tests/conformance/test_learning_curation.py` scans every
migration for that spelling and holds the *last* one equal to the curation set —
so a second, unrelated vocabulary under that name would not have shadowed the
check, it would have *become* what the check believed it was checking, and the
curation vocabulary would have gone ungoverned from this migration onward. The
generalizable rule: a constant named after a word another vocabulary already owns
is not a style question when a gate greps for it.

### E24-T4 — The deterministic scorer covers five blocks, under a new rubric version

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** no · **Repo:** contextplane

Goal: required-fact recall, boundary violations and precision computed over all
five envelope blocks, as a scorer rather than an ablation harness.

`treatments.py` holds canonical, governance, claim and resume identical and varies
only the workspace arm — the property that made its numbers attributable, and the
reason this is a rebuild rather than a widened loop. `precision` is currently
*served workspace items in the relevant set ÷ served workspace items* and its
generalization must not quietly redefine what the shipped measurement meant.

So: a new `RUBRIC` version and a new `judge_source_digest`, with the frozen one
left intact and selectable. `protocol.freeze()`'s existing rule does the rest — a
run whose freeze does not match the one its results were collected under is
invalid, not adjusted. `VIOLATION_KINDS` is unchanged; the fifth block adds items
to judge, not a fifth kind of violation.

Decomposition confirms the instruction block's authorization facts: a delta's
audience is decided by ADR 0021's three scopes, and the scorer asks the scenario
rather than the system under test, per `judge.py`'s safety clause.

Acceptance:
    make lint format-check typecheck && make test-coverage && make test-integration
    make eval

**Shipped, and generalizing found a defect the entry did not anticipate.**

**The shipped scorer's tenant dimension fires on every real item.** `judge.py`
reads `payload.get("tenant_id")`, and no arm writes a tenant into a payload —
not `queries.py`'s checkpoint payload, not `workspaces/recall.py`'s, not
`arm_payloads.py`'s canonical or claim payloads, not `instructions.py`'s delta
payload. Every scenario in the frozen corpus declares `permitted_tenant_ids`, so
`str(None)` misses the permitted set on every served item, and `SAFETY_TOLERANCE
= 0` then disqualifies every configuration on every scenario. A check that fires
on everything distinguishes nothing, which is the same defect as one that fires
on nothing wearing the opposite sign.

**The fix is a measurement change and therefore a version, not a patch.** A
tenant is a property of the resolution, not of the item — every arm queries
`WHERE tenant_id = ctx.tenant_id` — so `envelope_judge.score()` takes the served
tenant as an argument and consults a payload only where one actually states a
tenant, which is the case a fixture describing a leak wants caught. `judge.py` is
left byte-identical, because editing it would move `protocol.freeze()`'s default
digest and invalidate the closed workspace-retrieval decision's identity.

**A dimension that cannot be checked is now recorded rather than passed.**
Audience is expressible on a workspace item (`intent_id`) and on an instruction
delta (`scope`, per ADR 0021); canonical, ARC and claim payloads state none.
Classification is inexpressible on a canonical item, because assembly enforces
`trust is None` there. `UncheckedDimension` names the item, the block, the
dimension and why. The alternative — silence — is `containment.py`'s own warning
applied to a scorer: a check unable to fire is a hole that looks exactly like a
working defence. **The structural exemption is not the unreadable-label rule**,
which survives unchanged: a label the vocabulary does not recognise still ranks
as the most restrictive thing it could be, because guessing downward is what
publishes it.

**Attribution is carried per block.** `BlockTally` reports served, relevant and
required-found for every block including the empty and failed ones, because ADR
0024's single journey rests on the attribution being inside one result — which is
only true if the result says which arm produced which number.

**The freeze gained a registry rather than a second mechanism.**
`protocol.JUDGE_SOURCES` maps a version to its source file, `freeze()` and
`judge_source_digest()` take `judge_version`, and both default to the workspace
scorer so the closed decision's `protocol_digest` is reproduced byte-identically.
`assert_unchanged` re-digests the scorer *the run actually used*, read off the
collected freeze — defaulting to v1 there would report drift on every v2 run,
which is the fires-on-everything defect again in a different place.

### E24-T5 — The LLM judge: groundedness and relevance, with evidence and a pinned tuple

**Kind:** task · **Status:** done · **Blocked by:** E24-T2, E24-T3 · **Hotspot:** no · **Repo:** contextplane

Goal: two criteria a program cannot compute, scored by one model that is never the
candidate, returning its reasoning and the span it relied on — never a bare score.

Step-by-step reasoning before the verdict is required rather than encouraged: it
is reported to improve judge reliability by 10–15 % and, more to the point here, it
is what makes a verdict arguable by the human who overrides it. A score with no
trace is one a reviewer can only accept or reject.

Every judged result carries `(judge_model_id, rubric_version,
prompt_template_hash)` and its raw self-reported confidence. The confidence is
recorded and contributes nothing until E24-T6 fits it, per ADR 0026 part 3.

Acceptance:
    make lint format-check typecheck && make test-coverage && make test-integration

**Shipped. Four things the entry did not name.**

**The judge grades what the candidate was shown, and nothing held it.**
`context_receipt_items` records *which* items a resolution served and
deliberately not their content, so a judge asked whether an answer is grounded in
what was served had nothing to check against — and re-resolving would grade a
different envelope than the answer came from. The simulation now records the
material, serialized exactly once through one function so the model and the
record get byte-identical bytes; two serializations would let a judge grade
content that differed from what the candidate saw, in a way nothing would report.
This is a correction to E24-T3 rather than a judge feature, and it landed there.

**`reasoning` is declared before `verdict` in the schema, and the order is
load-bearing.** A model filling structured fields does so in declaration order,
so a verdict declared first is a verdict reached first and rationalised
afterwards. The requirement that reasoning precede the verdict is enforced by
where it sits, not by asking politely, and a unit test asserts the ordering
because it is the kind of thing a tidy-up would silently reverse.

**The prompt-template hash is computed from the template, the rubric, the tool
name and the output schema** — every input to the model this repository controls
— so editing a word mints a new calibration population without anybody
remembering to bump a constant. The per-request boundary is deliberately excluded:
including it would give every single call its own hash and make calibration bins
of size one.

**A partial judgement is refused rather than stored.** One criterion recorded as
though only one had been asked for would report a clean run over a criterion
nobody graded — the same defect as an errored prompt dropped from a run, in a
different place.

**The transport was shared rather than copied.** Generating an answer and judging
one are the same operation over different schemas: a model handed instructions,
data, and one tool it must call. `_invoke_tool` is that operation, and both roles
go through it, so the containment argument and the usage contract cannot come to
hold for one and not the other.

### E24-T6 — Judge calibration: bins per pinned tuple, fitted from human confirmations

**Kind:** task · **Status:** done · **Blocked by:** E24-T5, E24-T7 · **Hotspot:** no · **Repo:** contextplane

Goal: a judge's self-reported confidence becomes a number that predicts, or is
honestly reported as not yet predicting.

The mechanism exists and is reused rather than re-derived:
`service/memory/calibration.py` fits bins from judged outcomes, refuses an identity
mapping, stores a fit that misses its bound without selecting it, and separates
populations whose numbers do not mean the same thing. Here the separation key is
the pinned tuple — a fit made under one judge model does not describe another, and
ADR 0026 part 4 makes a rubric edit a new population for the same reason.

The human confirmations that feed it come from E24-T7's override path, which is
why this is blocked on it rather than only on T5.

Acceptance:
    make lint format-check typecheck && make test-coverage && make test-integration

**Shipped. Two findings, one of them a defect in an existing read.**

**The arithmetic was imported, not copied.** `service/memory/calibration.py`'s
`fit`, `calibration_error` and thresholds are reused verbatim; only the
separation key changes, from `(provider, model, strategy, scorer, tenant)` to the
pinned tuple. A second implementation of a calibration curve would be a second
answer to *"is this number trustworthy"*, which is the question the module exists
to give one answer to.

**A `DISTINCT ON ... ORDER BY fitted_at DESC` picks arbitrarily on a tie.** Two
fits written in the same instant tie on the timestamp, and the read then reports
whichever row Postgres happened to return — which is how a *superseded* fit comes
to be presented as a tuple's current state. `states()` orders by `status =
'active'` first, so the answer is a property of the rows rather than of clock
resolution. **`calibration.active_mappings` has the same shape and the same
latent tie**; it is named here rather than changed, because it is E8's read and a
drive-by fix to somebody else's query is how two lanes disagree about what a row
means.

**What the bound actually catches, measured rather than assumed.** With
`MIN_ADJUDICATED_FOR_MAPPING = 200` and `PRIOR_STRENGTH = 20`, a two-bin
disagreement *cannot* miss the bound: the error is weighted by how many
observations landed in each bin, so one deviant bin among two large ones is a
small effect. What misses it is confidence spread across many bins with
correctness uncorrelated to it — which is the right thing for the bound to catch,
and is now what the test asserts rather than a case that would have passed.

### E24-T7 — Expectations on a prompt, and a human verdict over a judged one

**Kind:** task · **Status:** done — override half with E24-T5, expectations half beside it · **Blocked by:** E24-T4 · **Hotspot:** yes — openapi.json + generated client · **Repo:** contextplane

Goal: a prompt in a set carries its declared expectations; a reviewer confirms or
overrides each judged criterion; the override is what calibration learns from.

Expectations extend the shipped `AddPromptRequest`, which already carries
`intent_note` — *"what this prompt is checking"* — and are declared before the run
on `scenarios.py`'s argument. Presets seed them and a persona is a named preset,
not a separate rubric.

The override contract is not invented. `POST /v1/memory/claims/{claim_id}:adjudicate`
already has the right shape for "a human says whether a machine's judgement was
correct": a closed verdict literal and a range-bound observed confidence, both
constrained at the view model so an unknown verdict is a 422 before the service is
touched, feeding a calibration observation table. E24-T7 follows it rather than
minting a parallel vocabulary.

The shipped run verdict (`right | wrong | unusable`, note required for anything but
`right`, one per reviewer, second replaces first) is unchanged and stays the
whole-item judgement. Per-criterion override is a finer grain beneath it, not a
replacement — decomposition states which of the two an evaluation surface shows
first.

Acceptance:
    make lint format-check typecheck && make test-coverage && make test-integration

**The override half shipped with E24-T5, and the split is recorded rather than
absorbed.** The two halves of this entry turned out to have different blockers:
the per-criterion override is meaningless without a judged criterion to override,
so it landed in the same change as the judge; declared expectations extend
`AddPromptRequest` and need neither. Landing them together would have made one
PR that half of the reviewers could not evaluate.

**What shipped with the judge:** `evaluation_judgement_reviews`, a row per
reviewer per judged criterion, following the claim-adjudication contract rather
than minting a parallel vocabulary — a closed verdict literal
(`confirmed | overruled | unsure`) and a range-bound observed confidence.

**An override is a second row, never an update to the judge's.** The pair *(what
the judge said, what the person said)* is the only thing calibration can be
fitted from, so overwriting would destroy the input E24-T6 needs — and it would
erase the disagreement E24-T12 renders as a visible state. `is_disputed` is
derived from the reviews rather than stored, so the two cannot disagree.

**`unsure` is information about the reviewer, not a third verdict on the answer.**
Calibration excludes it, for the reason `calibration.py` excludes an undecidable
adjudication: counting it either way would bias the fit. It still requires a
reason, because a reviewer who cannot tell has told the next reader something
worth reading.

### E24-T8 — A panel of judges, for a run that is gating a decision

**Kind:** task · **Status:** done · **Blocked by:** E24-T5 · **Hotspot:** no · **Repo:** contextplane

Goal: an opt-in three-family panel with majority vote on a prompt-set run, and a
disagreement that is visible rather than averaged away.

Opt-in per the epic body: 3× cost is right for a launch gate and wrong for
iteration. The family-diversity requirement is the same one ADR 0026 enforces for
the single judge, extended — a panel of three from one family cancels nothing.

Split votes are the interesting output and are not smoothed. A 2–1 panel records
that it was 2–1, and the surfaces show it, because a criterion three judges
disagree about is the one most worth a human's time. This is the escalation path
from judge disagreement to human adjudication that mature eval stacks maintain,
and it lands on E5-T6's shipped cockpit rather than a new queue.

Acceptance:
    make lint format-check typecheck && make test-coverage && make test-integration

**Shipped, and one decision the entry did not name.**

**An evenly split panel reports no majority at all.** `majority` is `None` on a
tie rather than tie-broken, and that is the decision rather than an omission: a
panel that split evenly has not decided, and inventing a winner would report
agreement nobody reached. An even panel is a configuration mistake this makes
visible instead of papering over.

**Family diversity is required across the panel, not merely against the
candidate.** Three judges from one family cancel nothing — the whole reason a
panel is worth 3× is that its members are biased in different directions, and a
panel that agrees because its members share a lineage is one expensive judge
reported as three. The refusal names both colliding positions.

**A panel extends the single judge rather than replacing it.** Position zero *is*
the interactive judge, so a deployment that configured one already has a panel of
one and adds members rather than reconfiguring. That is also why `panel_position`
is `NOT NULL DEFAULT 0` — a nullable column would let two "the single judge" rows
coexist under Postgres's distinct-NULL rule and silently turn a re-judge into a
second opinion.

**The panel outcome is computed, never stored.** It is a view over rows that
already exist, and a stored copy would be a second answer that could not be
corrected when a member was re-judged.

### E24-T9a — A duplicate response-model name renames the published one

**Kind:** task · **Status:** done · **Blocked by:** none · **Hotspot:** yes — openapi.json + generated client · **Repo:** contextplane

Not in the original decomposition. Cut and closed while claiming E24-T9, because
that task cannot pass its own acceptance until this is fixed.

**What happened.** E24-T3 added `CitationResponse` in
`api/schemas/simulation.py`. `api/routers/memory.py` already had one. FastAPI
cannot publish two schemas under one name, so it qualifies **both** by module
path — and the consequence lands on the *incumbent*: the published
`CitationResponse` silently became
`contextplane__api__routers__memory__CitationResponse`. Every generated client
referencing the old name stops compiling, and nothing in the adding change looks
wrong. It was found by `contextplane-ui`'s build breaking on a schema E24 never
touched.

**Why no existing gate caught it.** `reserved-vocabulary` governs governed nouns
on the wire; `contract-tags` governs grouping. Neither looks at schema *names*,
and the collision is invisible in review — the two classes are in different
subsystems and neither is wrong on its own. The only place it shows is the
exported document.

**The gate found a second one on its first run.**
`scripts/check_contract_schema_names.py` refuses any schema name containing `__`,
which is the marker FastAPI leaves when it had to disambiguate. It immediately
reported `ReceiptListResponse`, declared in both `api/schemas/directory.py`
(`{items, next_before}`) and `api/schemas/receipts.py` (`{receipts}`) — two
different shapes, both qualified, neither holding the plain name a client would
reference. That predates E24 entirely.

**The rule for resolving one, recorded because it is not symmetric:** the *newer*
model takes the longer name. A collision renames whichever was published first,
so giving the new one the awkward name is what leaves the existing contract
alone.

Acceptance:
    make contract-schema-names
    make lint format-check typecheck && make test-coverage

### E24-T9 — The contract pin catches up, seventeen paths and five schemas

**Kind:** task · **Status:** pending · **Blocked by:** none · **Hotspot:** yes — openapi.json + generated client · **Repo:** contextplane-ui

Goal: `contracts/openapi.json` is bumped to `main`'s 228 paths and the generated
client regenerated, before any dashboard task in this epic is claimed.

Not a repair. `GET /v1/receipts` and `GET /v1/tenants` work at runtime —
`directory.ts` reaches them through the raw `client.request` path and they shipped
in `contextplane#138`. What is broken is the boundary: the delivery process makes
the vendored contract the interface between the repos, and a dashboard calling
endpoints its own pinned contract does not describe passes CI because regeneration
is not dirty when nothing generated changed.

Decomposition checks whether the identifier-field gate and
`scripts/identifier-fields-baseline.json` move under the new schemas, and whether
`directory.ts`'s three adapters should now come from the generated client rather
than hand-written parsing.

This is first among the dashboard tasks because the evaluation schemas do not
exist in the pinned contract at all, so every screen below is blocked on it.

Acceptance:
    pnpm generate:api && git diff --exit-code
    pnpm lint && pnpm lint:identifier-fields && pnpm type-check && pnpm test && pnpm build

### E24-T10 — The Served surface gains the destination that answers its question

**Kind:** task · **Status:** pending · **Blocked by:** E24-T9 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: prompt sets, their runs and their verdicts become a screen.

The service shipped all three in `contextplane#136` and the dashboard consumes none
of them — `git ls-tree -r main` matches nothing for `evaluation` or `prompt-set`.
E22-T10 named the Served surface's question as *"what did the machines actually
get, and was it right?"* and placed Receipts, Context Lab and Sessions under it;
the destination that answers the second half is absent.

Read path before write, per `DESIGN.md` and per E22's own diagnosis: the set list
and a run's items come first, and creating a set is reached from that list. Run
headers load without their items, which is the shape `GET …/runs` already returns
and the reason it returns it.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "evaluation"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E24-T11 — Context Lab becomes the simulator

**Kind:** task · **Status:** pending · **Blocked by:** E24-T3, E24-T9 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: agent, instructions in force, prompt and expectations above three panes —
context, response, score.

Not a rewrite. `ContextLabPage.tsx` is 1,288 lines and the parts that carry server
state correctly all survive: block rendering with per-block state never inferred
from emptiness, the eight trust labels, the receipt trace with exclusions and
references, and item-level feedback. What changes is the page around them and the
block count — `contextBlockOrder` is four and the envelope is five, and a scorer or
a pane that silently omits the instruction block would report a clean run over a
wrong delta.

The agent picker is a `ResourcePicker` over `GET /v1/admin/actors`, per ADR 0018
and E22-T4's shipped primitive. Undeclared principals render as `unknown` with a
declare action rather than being filtered out, per ADR 0019 assumption 2, and
simulating one is refused by the service rather than by the screen.

Instructions in force are shown from the fifth block and are editable **for the run
only**. The editor must distinguish *no instructions declared* from *declared and
empty*, per ADR 0020's third assumption. Proposing an edit as a real instruction
version is not this screen's job and stays gated on E20-T7's failure-pattern
report.

The sentence at `ContextLabPage.tsx:256` is amended, not deleted: the resolver
still does not generate, and a reader who has just watched a response appear needs
to know which component did not produce it.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "simulation"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E24-T12 — The score pane: evidence before verdict, unproven when uncalibrated

**Kind:** task · **Status:** pending · **Blocked by:** E24-T5, E24-T7, E24-T11 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: five criteria, each showing what it concluded and what it concluded it from,
with an override that is one action away and a calibration state that is never
implied.

Three rules, each with a precedent in this repository rather than in a style guide:

1. **No bare scores.** Every judged criterion shows the judge's reasoning and the
   span it relied on. A verdict a reviewer can only accept or reject is not
   reviewable.
2. **An uncalibrated judge says so.** Until E24-T6 has a fit for the pinned tuple,
   the verdict renders as unproven. This is `calibration.py`'s argument on screen:
   an unexamined number must not acquire an authoritative look.
3. **No blended score, and no ranking by confidence.** Five criteria produce five
   answers; a boundary violation fails the case whatever the other four say.
   `curationModel.ts`'s rule holds — *"confidence does not move a row, and nothing
   here weighs what getting it wrong would cost"*.

Disagreement between judge and reviewer is a visible state, not a silent
overwrite, and it escalates onto the Judgement surface's shipped cockpit.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "score"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E24-T13 — The improvement surface: observations, several at once, unranked

**Kind:** task · **Status:** pending · **Blocked by:** E24-T12 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: a failing run offers every opportunity its record supports, names none of
them as the cause, and links each to the surface that already handles it.

The user's instruction is the acceptance criterion in prose: *"do not assume there
is one path to improve it."* The observation table in the epic body is the seed set;
decomposition assigns each row its evidence source and its destination.

**Rebuilds nothing.** Policy authoring, claims, curation, quarantine, promotions,
extraction strategies, promotion policy, the autopromote allowlist, calibration
and the agent instruction lifecycle all ship. Each observation deep-links with a
filter applied.

Two behaviours are load-bearing. Recording a conclusion writes the rating that
matches it from the thirteen `signals/feedback.py` already accepts — `selected`,
`ignored`, `missing`, `incorrect`, `stale`, `contradicted`, `unsafe` — rather than
collapsing everything into the three the dashboard writes today. And the
instruction door stays gated: E20-T7 requires a stored failure-pattern report to
justify an instruction change, so the surface pulls the report first and the
finding becomes citable evidence.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "improvement"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

### E24-T14 — Comparing two runs, in the vocabulary ARC already uses

**Kind:** task · **Status:** pending · **Blocked by:** E24-T10 · **Hotspot:** no · **Repo:** contextplane-ui

Goal: two runs of one prompt set, side by side, with what moved named rather than
diffed as text.

The shipped `GET …/runs` returns headers without items precisely so a comparison
can start by choosing two rows without reading the whole history — the endpoint's
own docstring says so, and the screen honours it.

Named change kinds rather than a text diff, following ARC's baseline-diff
vocabulary (`mandatory_block_added`, `conflict_changed`, and the rest): a reader
comparing runs is asking what changed in kind, and a character diff over serialized
payloads answers a different question.

A comparison spanning two rubric versions warns, per ADR 0026 part 4. Decomposition
confirms whether a run pins the resolver configuration it ran under — E22-T15
already flagged that it almost certainly must, since a comparison across a config
change is meaningless if neither side records which config produced it.

Acceptance:
    pnpm --filter admin-dashboard test -- -t "comparison"
    pnpm lint && pnpm type-check && pnpm test && pnpm build

---

## Sequencing

Two service chains and one dashboard chain, with one crossing.

- **ADRs first, and they are parallel.** E24-T1 and E24-T2 block nothing but each
  other's readers and everything downstream. Neither is claimable work in the sense
  the delivery process means; both must land before the tasks that enact them.
- **Service chain A — simulation.** T1 → T3. Hotspot at T3.
- **Service chain B — scoring.** T4 → T7 → T6, with T2 → T5 → {T6, T8} joining at
  T6. Hotspot at T7.
- **Dashboard chain.** T9 first, always — the evaluation schemas are absent from
  the pin, so every screen is blocked on it. Then T10 and T11 in parallel, T12
  after both halves it needs, T13 after T12, T14 after T10.
- **The crossing.** T11 needs T3; T12 needs T5 and T7. Per the delivery process,
  each dashboard task's `Blocked by:` names the server task, and no atomic
  cross-repo merge is assumed.

Three tasks touch the contract and are serialized against each other and against
every other hotspot claim in flight: T3, T7, T9.

## What this epic does not close

Named rather than left as a remainder, on this file's convention.

- **Retrieval that retunes itself from verdicts.** Excluded here and in E22, with
  the same reason: it needs its own safety argument and would ship without one if
  it rode inside a surface epic.
- **A second judge criterion set for non-English responses.** The bias literature
  is about English rubrics on English outputs; nothing here has measured the
  transfer.
- **Cost governance for simulation and panels.** Usage is reported exactly, per the
  provider contract, and a per-tenant budget over it is a separate concern with a
  rate-limit-shaped answer already precedented in `RATE_LIMIT_*`.
- **The remaining identifier fields.** E22-T12 owns the eleven on ownership and
  profiles, and E22's own out-of-scope entry owns the rest — *"the remaining ~26
  blocked identifier fields ... each need a read that does not exist."* This epic
  adds one picker (the agent) and adds no free-text identifier field, which the
  shipped gate enforces anyway.

### Handoff to E8 — the memory-quality measurements have no reader

Recorded here, in full and with its evidence, rather than edited into E8 directly:
`governed-agent-memory.md` is under active revision by another lane, and a
finding stated once in the file that discovered it costs nothing, while a
concurrent edit to a file somebody else is rewriting costs a conflict and risks
two half-versions of the same amendment. **Whoever next owns E8 applies this;
nothing here is claimable and no E8 task is cut by this file.**

**The finding.** E8's harness measures recall@10, extraction precision and recall
per predicate, retrieval precision joined through receipts, multi-session recall,
and salience reliability with a Brier score beside it. Every one of those
terminates in `eval/EVAL.md` and the `make eval` target. There is no endpoint and
no screen. The person whose job is judging whether the system is trustworthy
cannot see any of it, and ADR 0024 assumption 3 makes this load-bearing rather
than cosmetic: without the report, the only memory signal an evaluator sees is the
one arriving through an LLM-judged agent run, which is the confound MemDelta
names and the one that ADR's dissent says would win.

**Why it is an amendment before it is a task.** E8's body names four remainders —
extraction precision, retrieval relevance, multi-session recall, and `eval_score`.
"These measurements have no reader" is not among them. Cutting a task whose
premise the body does not carry is how E19 shipped six of eight tasks on premises
that did not survive contact with the tree. So the body is amended to state the
defect, and then the tasks are cut against it.

**What the amendment should say**, because the shape has a name here already:
this is the third instance of one failure. E9's `requires_validated` had no caller
outside its tests, so a running service was more permissive than its pipeline.
E17's `resolve_weights` had no production caller, so a tenant that configured its
own scoring got core values and no indication otherwise. E8 measures memory
quality and nothing reads the measurement. `governed-agent-memory.md` already
wrote after the second one that *"twice in one audit is enough to make it a thing
to check for rather than a thing to notice"* — this is the third, and it is the
citation the amendment should carry.

**Two consequences to state rather than absorb.** It is two tasks, not one: `make
eval` writes to a markdown file, so a service task must serve the measurements
before any dashboard task can render them, and that service task is new scope
rather than wiring. And cutting a dashboard task makes E8 span both repositories,
so its header becomes `Repo: contextplane, contextplane-ui` per
[`plan/README.md`](README.md).
