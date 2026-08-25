# 0023 — A sample is drawn from the lot it accepts, and E12 needs a flow it does not have

**Status:** Accepted 2026-08-24, supersedes 0022

## Context

0022 decided what `migrated_canonical` commits to and shipped a service writing
it. An adversarial review of the merged change found the central claim false, and
it is false in a way worth recording precisely, because the mistake is available
to anybody who reads `SamplingPolicy` and wants a batch import to be governed.

**What 0022 claimed:** a migration is a lot; a person inspects its sample to the
category's floor; `require_minimum_sample` enforces that; the policy disposition
then records the same outcome across the uninspected remainder.

**What the code did:** checked `inspected_dispositions` — a count of the tenant's
prior human dispositions since a caller-supplied instant — and then created the
lot's cases. The check ran *before the lot existed*, so not one inspected item
could have come from the lot. The number licensing acceptance was arbitrary prior
curation of unrelated material.

**The module being consumed had already forbidden the reading**, and 0022 quoted
it while contradicting it:

> the reviewed subset is not a random sample of the lot… The figure is a floor on
> effort, not a guarantee about the residue, and **no caller should describe it as
> the second**.

0022's §1 describes it as exactly the second. That sentence exists because an
earlier dissent asked for it, and it was written before there was any caller to
mislead. There is now a record of the first caller misreading it.

Three further consequences of the same error, all reachable:

- **Nothing consumed the inspections.** One qualifying window accepted unlimited
  lots forever, because no row was marked as spent. A test asserted this as a
  safety property — accepting a 30-row lot and then finding the count unmoved —
  reading it as *"a policy cannot clear its own floor"* when what it demonstrated
  was *"the floor stays satisfied for everything that follows"*.
- **The lot's category was a caller-supplied label** never checked against the
  claims, so an importer could name whichever registered category had the laxest
  floor. 0022's Assumption 1 checked the opposite direction — that naming
  *nothing* could not escape a floor.
- **The window was caller-supplied and unbounded**, so the importer chose the
  denominator. `require_minimum_sample`'s own docstring calls that *"grading its
  own homework with a marking scheme it chose"*.

## Decision

### 1. A sample is drawn from the lot it accepts, or it is not a sample

Stated as a rule this repo can be held to, because the alternative was reached by
careful reasoning from real mechanisms and still ended up wrong:

**A count of prior work is not evidence about a new batch.** Acceptance sampling
licenses a conclusion about a lot's residue only when the inspected items were
drawn from that lot. Any design that satisfies a floor using dispositions
recorded before the lot arrived is asserting something about material nobody
looked at, and the arithmetic it borrows says nothing about it.

`inspected_dispositions` is a floor on *effort* over a tenant's queue. It is a
governance metric, not a sampling frame, and it must not be used as one.

### 2. `migrated_canonical` is withdrawn, and E12 goes back to blocked

The disposition, the service, the migration and the tests are removed. E12-T3,
E12-T4 and E12-T5 return to blocked — on a correctly identified blocker this
time, which is the only thing 0022 bought.

### 3. What the real design needs — attacked before anything was built on it

This section first carried a four-step sketch: stage the lot, draw a sample from
it into the curation queue, have a person disposition the sample, complete
acceptance on their decisions. **It was reviewed adversarially before
implementation, and two of its four steps name things that conflict with
mechanisms already in the tree.** What follows is what the review established.

**Step 1 was false about the word it used.** `status = 'staged'` is not a holding
state — it is the *live, servable* one. `claim_serving`'s `_SERVABLE_AS_OF`
serves `status IN ('staged','superseded')` once consolidated, and the promotion
sweep selects staged rows and can promote them to canonical facts with no case,
no sample and no acceptance. "Stage the lot; nothing is accepted" describes the
opposite of what staging does.

**Step 2 was a category error.** The curation *queue* is a projection over
`memory_claims` via `backlog_predicate`; curation *cases* are axis-keyed rows read
through `cases_for`, which is plain FIFO with no ranking and no lot filter.
Sampled items cannot be "placed in the curation queue" without either marking
them contested — a lie that blocks promotion of the whole axis — or adding a
fifth backlog reason. And a several-hundred-case sample read FIFO buries every
contradiction raised after it.

**The arithmetic does not do what the sketch assumed.** `minimum_sample` is
derived for a **zero-acceptance plan**, so a lot is acceptable only with zero
defects in the sample — but `AcceptanceState` carries `inspected` alone and `met`
is `inspected >= min_sample`. A sample with many rejections still passes. The
existing halt is a counter, not an acceptance number. There is also no defect
mapping: of six dispositions, `confirm` is presumably conforming and `reject` a
defect, and the three `propose_*` targets are not verdicts on the row at all.

**And the unit is wrong.** The sample unit is a claim; the case unit is an axis;
`open_case` is idempotent per axis, so two lots touching one axis share a case
and one human decision counts toward both floors — this ADR's own error at n=1.
`minimum_sample` has no finite-population correction, so where the connector path
produces a handful of claims per artifact, `n > N` is the normal case rather than
the edge, and the honest answer there is 100% inspection.

**What the review proposes instead, and it is a better shape:** make the **lot**
the unit of state rather than the claim. A `claim_lots` row carrying status,
source run, category and sample seed; a `lot_id` on `memory_claims`; claim
visibility gated on lot status in the four readers that matter, with a
conformance test that the reader set is closed. The lot closes when its run
reaches a terminal state; only then is a seeded sample drawn and recorded in an
explicit `lot_sample` table, so the frame is auditable and re-drawing is
detectable. **Acceptance is a transition on the lot row, not a disposition per
claim** — which removes the need to resurrect `migrated_canonical` at all, and
makes rejection a single `t_invalidated_at` sweep over `lot_id`.

Two governance questions remain, and they are decisions rather than
implementation: **who may be routed a sample case** — with a check that the owner
is a human actor kind, since `actor_kind` is a stored parameter defaulting to
`human` and an import routing to its own sync-worker would manufacture its own
acceptance evidence — and **what happens to a rejected lot on resubmission**,
since re-running a connector reproduces the same rows and a fresh draw against
unchanged material clears eventually.

### 4. Two defects the attempt exposed are kept fixed

They were latent before it and outlive it.

**The two transports did not agree about what they accept.** The HTTP route
hand-writes a `Literal`; the MCP tool took a bare `str` and let the service
validate — against the *whole* vocabulary. While every disposition was
operator-recordable this was invisible. The moment one was meant to be
service-only, the agent-facing surface accepted it, and the conformance test that
claimed to hold the surfaces closed inspected only the HTTP model. Both now build
from `OPERATOR_DISPOSITIONS`, and the test reads both.

**The vocabulary-versus-schema test named one migration.** It now discovers the
latest migration pinning the set. A test pointing at whichever migration was
current when it was written goes stale silently the next time the vocabulary
moves, which is the drift it exists to catch.

## Assumptions

1. **`inspected_dispositions` keeps its current meaning.** This ADR does not
   change it; it records that it is not a sampling frame. If a future change
   makes it lot-scoped, §1 is unaffected — the rule is about where a sample comes
   from, not about one function.
2. **A staged flow is acceptable for migration.** It means an import does not
   complete in one call. That is a real cost, and it is the cost of the guarantee;
   an import that completes immediately is one that was not sampled.

## Alternatives rejected

**Patching 0022 to keep the disposition with a weaker claim.** Rejected because
there is no weaker claim that supports the disposition. `migrated_canonical`
exists to record an outcome for material nobody inspected; without a sample of
the lot there is no evidence for that outcome, and the disposition becomes an
assertion dressed as a measurement.

**Keeping the service unwired until a caller exists.** Rejected on this repo's
most-repeated finding: the eighteenth wave found six mechanisms built, wired and
consulted by nothing, and this one had no caller at all — not registered on
`Services`, not reachable from any connector. Leaving it would have been
authoring the defect deliberately, having spent a wave removing instances of it.

## Consequences

E12 is no longer complete. That is the honest state, and preferable to a shipped
governance mechanism whose stated safety property does not hold.

**The review that found this was adversarial and was asked to attack a merged
change.** It found what ordinary review had not, in code that had passed every
gate — 11,103 unit tests, 3,316 integration tests, a conformance tier written
specifically to pin this vocabulary. Gates check that the code does what it says.
They cannot check whether what it says is true.

## Dissent

**Withdrawing may be an overcorrection.** Two of the four defects — the
caller-supplied window and the unchecked category label — are fixable in an
afternoon, and a version that took its window from the lot's own arrival time and
validated the category against the claims would be meaningfully harder to abuse.
The response is that neither touches the central error: a fixed window over
unrelated prior curation is still not a sample of the lot, and tightening the
inputs to an invalid inference makes it look more rigorous without making it
valid.

**The staged flow in §3 was a sketch and it was wrong too.** Attacking it before
implementation found two of four steps in conflict with shipped mechanisms. That
is the second time in one wave that careful reasoning from real code produced a
design that did not survive an adversarial reader — and the second time the
review cost minutes where the mistake would have cost a release.

**The replacement in §3 has exactly the status the sketch did.** It comes from
the review rather than from me, which makes it differently-sourced, not verified.
Nobody has attacked *it*. Whoever implements it should assume it is wrong
somewhere and find out where first.
