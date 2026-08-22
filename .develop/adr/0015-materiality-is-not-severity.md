# 0015 — Materiality is not severity, `incident` is already taken, and the thresholds are not ours to invent

**Status:** Accepted 2026-08-22

## Context

E4 requires that a quarantine be classified for regulatory materiality, and that
classification-as-major start a tracked notification clock with initial,
intermediate and final report deadlines. This ADR settles three things that
everything downstream keys off: what the classification is called, who makes it,
and where its thresholds come from.

It is first among E4's DORA tasks because the alternatives are all worse later.
A clock with legal deadlines depends on the classification; a threshold picked to
make a demo work either starts clocks nobody can meet or fails to start one that
was required, and both are discovered by an auditor rather than by a test.

**Two words E4 reaches for are already taken, and both collisions are the kind
that reads fine in review.**

`severity` is the PII scanner's ordering — `advisory < warn < block`, defined in
`security/pii_scanner.py` as `_POLICY_SEVERITY` and surfaced through
`types.py` and `context/admission.py`. It answers "how hard should this write be
stopped".

`incident` is *also* taken, twice, and this one is easier to miss.
`LIFECYCLE_REFERENCE_KINDS` in `context/lifecycle.py` includes `"incident"` as a
reference kind, and `memory_claim_provenance`'s `ck_memory_prov_kind` CHECK
includes `'incident'` as an evidence kind. In both places it means **an external
operational incident that something points at or cites** — the PagerDuty record,
not a governed internal object.

So E4's phrase "auto-created incident case" would introduce a third meaning for
a word already carrying two, in a system where one of them is a database CHECK
constraint.

**What does exist and should be reused:** the curation case machinery, with
`CASE_OPEN` / `CASE_ROUTED` / `CASE_RESOLVED`, where a disposition is a
*proposal* and its approver is recorded at disposition time rather than inferred
later. That is the right discipline and the right lifecycle.

## Decision

### The classification is `materiality`

Not `severity`. Two different orderings sharing one field name is a defect
waiting for a reader who has only seen the other one, and the PII scanner's is
the one nearly every contributor meets first — it is on the write path.

`materiality` is also the more accurate word: the question is not how bad the
event is but whether it crosses a threshold that creates an obligation.

### The governed object is a `reporting_obligation`, not an incident

Named after the thing being tracked rather than the thing that triggered it,
which both avoids the collision and is more honest about what the row is: an
obligation with deadlines, not a description of an outage.

This composes cleanly with the existing meaning rather than fighting it. A
`reporting_obligation` may *reference* an `incident` in the sense the tree
already uses — the external record — and a claim may cite that same incident as
evidence. Three objects, one relationship, no shared name.

It reuses the curation case lifecycle (`open` / `routed` / `resolved`) and the
disposition-time approver rule.

### Classification is human, with an automated *nomination*

The tempting design is automatic classification from blast radius. It is
rejected as stated, because it means a graph traversal starts a legal clock, and
`get_blast_radius` is a traversal over a graph that is itself derived from
ingested data of varying quality.

But "human classifies" alone leaves an unbounded gap between detection and
classification — and **that gap is itself the reportable delay**, so a design
that does not name it has hidden the worst part.

So: **the system nominates, a human classifies, and the time between the two is
measured and surfaced.** Concretely, crossing the blast-radius threshold creates
the `reporting_obligation` in `open` with `materiality: unclassified` and starts
a *nomination age* gauge. No filing deadline exists yet; what exists is a
visible, growing number that says how long a classification has been owed.

`unclassified` is a real state and not a null. A missing materiality reads as
"not applicable" to every query that filters on it, which is the permissive
direction taken by omission — the same failure `_declared_sensitivity` closes
for sensitivity tiers and migration 0069 closes with its `pending` default.

### The thresholds are a stated placeholder, and the placeholder is structural

DORA's materiality thresholds are external. This service does not get to invent
them, and this ADR does not cite them, because nobody involved in writing it has
verified the current regulatory text.

**That is recorded as a mechanism rather than a comment.** The threshold set
carries a ratification status, and until a ratified set is installed:

- automatic nomination still runs, because noticing is always safe;
- **classification as `major` is refused for any actor other than a human with
  the authority to make it**, and there is no automatic path to `major` at all;
- the surface says the thresholds are unratified wherever a materiality is
  shown.

An invented threshold presented as a compliance feature is worse than an absent
one: the absent one is a known gap, and the invented one is a deployment
believing it is covered.

This is the same shape as `ranking_registry.json`'s validation status and
[ADR-0014](0014-derived-magnitudes-are-a-third-status.md)'s `derived` — a number
that ships carries a machine-readable claim about what stands behind it.

## Assumptions

1. **Somebody can be named who may classify as major.** If no such role exists
   in a deployment, the obligation stays `unclassified` forever and the gauge
   grows without bound, which is the correct visible outcome but is not a
   working process. E4-T6 inherits the problem.
2. **A ratified threshold set will eventually arrive from outside engineering.**
   If it does not, this design's honest end state is a system that nominates and
   never classifies. That is still better than the alternative, but it is not
   the feature E4 describes, and saying so now is cheaper than discovering it
   after the clock work is built.
3. **Nomination age is measured from detection, not from creation of the
   obligation row.** If those differ — because a sweep runs on a schedule — the
   difference is part of the reportable delay and must not be quietly excluded.

## Alternatives rejected

**Call it `severity` and disambiguate by context.** The cheapest option and the
one that fails silently. Two orderings, four values against three, one on the
write path and one on a regulatory path; the first reader to autocomplete the
wrong one produces code that type-checks.

**Automatic classification from blast radius above a threshold.** Rejected as
the *classifier*, kept as the *nominator*. The distinction is the whole
decision: nominating is a claim that somebody should look, and classifying is a
claim that starts a legal obligation. A graph traversal is qualified for the
first and not the second.

**Human classification with no automated nomination.** Removes the machine from
a legal decision, which sounds safer and is not: it makes detection depend on
somebody noticing, and the delay between the event and the noticing is
unmeasured. An unmeasured delay in a regulatory process is the thing that gets
reported *about* you.

**Pick plausible thresholds now, marked TODO.** This is the alternative most
likely to be chosen by a hurried author, so it is written out. A TODO comment is
invisible to the operator reading a dashboard that says `materiality: major`.
The placeholder must be enforced, not annotated.

**Reuse the PII scanner's severity scale by extending it.** Considered because
it avoids a new vocabulary, and rejected because the scales measure different
things against different thresholds for different audiences. Extending
`advisory < warn < block` with a fourth value that means "notify a regulator"
would put a legal obligation in an enum consulted on every write.

## Consequences

E4 gains a state — `unclassified` — that will be the state most obligations are
in most of the time, and a gauge whose healthy value is not zero. Both are more
honest than a system where everything is classified promptly on paper.

The `major` path cannot be demonstrated end to end until a ratified threshold
set exists. That is a real cost to the demo and the correct trade: the
alternative is a demonstrable feature that is wrong.

Two vocabulary decisions are now load-bearing across E4-T6 and E4-T7, and
neither is enforced by anything yet. A lint rule that refuses `severity` on a
`reporting_obligation`, or the word `incident` as a governed object's name,
would be cheap and is not part of this decision — see the dissent.

## Dissent

**The strongest objection is that this ADR decides names and process while
deferring the only thing that makes the feature real.** Without thresholds there
is no clock, without a clock there are no deadlines, and E4-T6 and E4-T7 are
building machinery around a classification that cannot currently be made. A fair
reading is that E4's DORA half should be deferred wholesale until legal input
arrives, rather than half-built against a placeholder — and that this decision's
careful structure is a way of appearing to make progress on something blocked.
The counter is that nomination, the obligation record and the delay gauge are
useful without thresholds and are what a real classification would need anyway.
That counter is true and it is also exactly what someone would say while
building the wrong thing.

**A second, on the placeholder mechanism.** Refusing automatic `major`
classification until thresholds are ratified is enforceable; "the surface says
the thresholds are unratified" is not, and it is the half an operator actually
reads. A UI badge is not a control, and this ADR has no answer for a deployment
that renders the materiality and drops the qualifier.

**A third, narrower.** The naming decisions rest on collisions found by grep,
and grep found two. There is no gate preventing a third, and the next author to
introduce a governed noun will do exactly what E4 did — reach for the obvious
word. The durable fix is a reserved-vocabulary check, not three ADRs noticing
collisions one at a time.
