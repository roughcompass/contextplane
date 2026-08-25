# 0025 — The resolver does not generate, and simulation is a separate receipted operation

**Status:** Accepted 2026-08-25

> **Numbering.** The plan entry that commissioned this record (E24-T1) names it
> ADR 0022. That number was taken by `0022-a-migration-is-a-lot-and-a-lot-is-sampled.md`
> between the plan being written and this being claimed, and 0023 by the ADR
> beside it. The two repositories share one sequence — `.develop/adr/README.md`
> in `contextplane-ui` says so, and gives the reason: *"two ADR 0004s would be a
> citation nobody can resolve."* So this is 0025, the judge decision is 0026, and
> the plan's acceptance criteria were corrected in the same change rather than
> left naming files that would collide.

## Context

E22-T15 shipped the evaluation surface and set its boundary by quoting the screen
it was extending:

> `ContextLabPage.tsx:256` states it plainly: *"The resolver retrieves context
> only. It does not call a language model, generate an answer, or invent an
> evaluation score."* That boundary holds — an evaluation run resolves context and
> records a human verdict; it does not generate a response and it does not score
> itself.

That sentence is true of the resolver today and this ADR does not make it false.
`POST /v1/context/resolve` is unchanged by everything below.

What changed is the question being asked of the product. The evaluation surface
answers *"was the right context served?"* The user asked for the next one:
*"I would want the response to be a contextplane + llm (simulating a particular
agent) and response gradeable by a user (human)."* That question is unanswerable
without the agent. A retrieval-only verdict can say the envelope contained the
fact; it cannot say whether an agent holding that envelope would have used it,
and the gap between those two is most of what an operator is actually worried
about.

The forces:

- **The split is the diagnostic value.** *"The retrieval was fine and the agent
  fumbled it"* has to stay an answerable question. A design that fuses retrieval
  and generation into one opaque step answers *"the run was bad"* and stops.
- **Nothing may score itself.** E22-T15's clause was protecting against a system
  grading its own homework, and `evidence.py` enforces the same separation in
  code — `EVIDENCE_CARRIES_NO_DECISION` is asserted by the conformance suite.
- **The receipt is the record.** A generation that no receipt describes is the
  one input to an evaluation nobody can audit afterwards.
- **Not every deployment has a provider.** `extraction/provider.py` already
  settled the shape of that answer: *"a deployment that never configures a
  provider is a working deployment with one feature switched off, not a broken
  one."*

## Decision

### 1. A new operation composes resolution with a model call; the resolver is untouched

`POST /v1/evaluation/simulations` resolves context as a declared agent principal,
generates a response from the five-block envelope and the instructions in force,
and returns both with the citations linking them. `POST /v1/context/resolve` gains
no parameter, no branch and no model dependency.

**The reversal is of one sentence in one task, not of E22-T15's mechanism.**
Prompt sets, runs and persisted verdicts are consumed by this epic unchanged.
E22-T15's status stays `done` and its boundary clause is annotated as superseded
here, per the plan file's rule that a replaced mechanism is removed or its
survival is explained where the reader will look.

### 2. The two halves are separately addressable records, not one fused row

The resolution keeps its own receipt, produced by the resolver on the path every
other caller uses. The generation is a second record — `evaluation_simulations` —
that *references* that receipt id rather than embedding it.

This is the answer to the dissent below and it is a structural commitment rather
than a convention: a reader can ask *"what was served"* and *"what was said about
it"* independently, and a change to one does not rewrite the other. Fusing them
would recreate exactly what ADR 0011 refused for envelope blocks.

### 3. Citations are structured output, extracted the way extraction already extracts

The generated response names the `receipt_item_id` values it used, through the
same forced-tool-call containment `extraction/` already relies on. A model that
returns prose instead of calling the tool has failed, and failing is the correct
outcome.

**This is what makes *cited* and *ignored* facts about the run rather than a
later inference over prose.** The improvement surface (E24-T13) is built entirely
on that distinction, and a design that recovered citations by string-matching the
answer against the envelope would be inventing the evidence it then reasons from.

### 4. The guard is in the service, not in the router

The declared-principal check (assumption 2) and the judge-family constraint
(ADR 0026) are enforced in the service method both transports reach. This
workspace's standing rule, and the reason is that every service has two
transports: a check on a route is a check the MCP tool does not have.

### 5. Simulation is a distinct capability from resolution, and its absence is a state

With no provider configured, `POST /v1/evaluation/simulations` returns a refusal
naming the missing configuration. Prompt sets, runs, verdicts and the
deterministic scorer all keep working. A deployment that never buys a model still
has a complete evaluation loop over retrieval; it does not have the agent half,
and it is told which one it is missing rather than being handed an empty response.

## Assumptions

1. **A deployment with no provider configured has simulation switched off and
   evaluation still works.** Matching `extraction/provider.py`'s rule verbatim.
   The failure this rules out is a deployment where the evaluation screen is
   dark because one optional dependency is absent.
2. **The simulated agent is a declared principal per ADR 0019, and simulating an
   `unknown` principal is refused rather than defaulted.** ADR 0019 established
   that an undeclared principal is `unknown` and never `human`, precisely because
   nothing can infer the kind from the transport. Simulating a principal nobody
   has declared as an agent would be the product asserting the thing ADR 0019
   refused to assert, at the moment it is least checkable. The refusal names the
   principal and the declaration route.
3. **An agent that declared no instruction digest is distinguishable from one
   that declared an empty set.** ADR 0020's third assumption, carried forward
   without weakening. Every evaluation surface in E24 counts three states —
   *undeclared*, *declared and empty*, *declared with content* — because a
   simulation run against an agent whose instructions nobody declared is not the
   same experiment as one run against an agent that declared it has none, and a
   score that conflated them would be scoring two different things under one
   number.
4. **The receipt the resolver produces is the same receipt any caller gets.** The
   simulation path resolves through `ContextResolver.resolve`, not a copy of it
   and not with checks relaxed for evaluation. An evaluation that ran against a
   laxer path would measure something the product does not serve.

## Alternatives rejected

**Making the resolver generate.** The obvious shape — a `generate: true` flag on
`/v1/context/resolve` — and rejected because it loses the split that makes the
diagnosis possible. One operation with one receipt cannot answer whether
retrieval or the agent failed, and that question is the entire reason the user
asked for the response rather than a score. It would also put a model dependency
on the path every production caller uses, so a provider outage would degrade
serving rather than an evaluation feature.

**Calling the model from the browser.** Offered as two variants — a same-origin
proxy, and a session-scoped key held in memory — and both declined. The reasons
survive independent scrutiny: a browser-side call cannot be receipted, so the
generation half of an evaluation would have no audit record at all; it cannot be
reached over MCP, so the capability would exist for one transport; and it would
put the judge-is-not-the-candidate constraint in the one place an operator can
edit it, which ADR 0026 exists to prevent.

**A separate simulation service with its own resolver client.** Rejected because
it reintroduces the second code path assumption 4 forbids. The value of resolving
*through* the shipped resolver is that the evaluation measures the product.

**Storing the generated response inside the receipt.** Rejected on ADR 0011's
argument. A receipt describes what was served; a response is what was said
afterwards, by a different component, possibly by a model in a different
provider family. One row holding both would make the receipt's meaning depend on
whether a simulation happened to run.

## Consequences

A new table, a new endpoint, a new MCP tool surface decision (deferred: E24 ships
the REST route and records that the tool question is open), and a second consumer
of the provider layer.

**The provider layer gains a second consumer, not a second layer.**
`extraction/{anthropic,openai}_provider.py` behind `provider_registry.py` and
`factory.py` already give tool-use with a schema the model must call, exact
never-estimated usage, a key read once and never logged, and graceful
no-provider degradation. Whether the existing `ExtractionProvider` protocol
widens or a sibling protocol lands beside it is an implementation seam, decided
in E24-T3; either way there is one credential path and one usage contract.

**Token usage is reported exactly and never estimated.** The provider contract
already forbids guessing, and `UsageSource` distinguishes `provider_reported`
from `estimated` from `unknown` so a spend figure cannot silently mix them. A
simulation reports the cardinality that produced its token figure alongside it,
because `limit` is the only lever the product actually offers when a run comes
back too large.

**A cost accepted:** the dashboard now has a screen where a model runs on a
person's click, and per-tenant cost governance for that is explicitly not in E24.
Usage is measured exactly from the first run, which is what makes the budget
work possible later; nothing here bounds spend.

**A second cost:** `ContextLabPage.tsx:256` now sits on a screen that produces an
answer. The sentence is amended rather than deleted — the resolver still does not
generate, and a reader who has just watched a response appear is exactly the
reader who needs to know which component did not produce it.

## Dissent

**Composing two operations behind one endpoint recreates the fusion ADR 0011
refused for envelope blocks.** The objection is that "one call, two receipts" is
a promise made by prose, and prose erodes: the first time somebody needs a
convenience field, the response grows a flattened view of both halves, and two
releases later nobody remembers which record was authoritative.

It is answered by construction rather than by intent — decision 2 makes the two
separately addressable rows with a foreign key between them, and the simulation
row has no copy of the envelope. It is not answered *completely*, and the honest
statement of the residue is this: a single endpoint means a single failure mode
in the caller's eyes. A simulation that resolved cleanly and then failed to
generate has to report a partial success, and a caller that treats any non-200 as
"nothing happened" will discard a resolution that did happen and was receipted.
The mitigation is that the resolution's receipt exists regardless, so the record
is complete even when the response to the caller is not; the residue is that
somebody has to look for it.

**A second, and it is about what this makes cheap.** Before this ADR, producing
an agent response inside the product was impossible, which meant every claim
about agent behaviour came from outside evidence. After it, the product can
generate a response and grade it, and the temptation to treat that loop as
ground truth is considerable. Nothing here licenses that: a simulation is one
model's output under one configuration at one moment, and ADR 0026 spends its
whole length on why the grading of it is not to be trusted until it has been
checked against people. The counter-argument to this dissent is that the loop
existed anyway, in an evaluator's head, with no receipt at all.
