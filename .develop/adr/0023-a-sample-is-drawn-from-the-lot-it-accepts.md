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

### 3. What the real design needs, sketched so it is not re-derived

A governed migration is a **staged flow**, not a synchronous call:

1. The import stages its claims as a named lot, uninspected. Nothing is accepted.
2. A sample of the required size is drawn **from that lot** and placed in the
   curation queue, marked as belonging to it.
3. A person dispositions the sampled items. Their decisions are the evidence.
4. Acceptance completes — or the lot is rejected — on those decisions, and only
   then does anything record an outcome for the remainder.

Every step is a mechanism that does not exist: there is no lot record, no
sampling frame, no way to mark a case as a lot's sample, and no resumable
acceptance. That is what E12-T3 is blocked on, and it is considerably more than
"a rule".

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

**The staged flow in §3 is a sketch and may be wrong too.** It is written from
the same chair, by the same reasoning that produced 0022, and has not been
implemented or attacked. Whoever builds it should treat §3 as a starting point
rather than a specification — and should have it reviewed adversarially before it
merges rather than after.
