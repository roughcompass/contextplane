# 0008 — Cold start: the existing write authority, and recording rather than sampling

**Status:** Accepted 2026-08-19

## Context

The first Autonomy Envelope has no conformance history behind it, and something
has to approve it anyway. The bank-plan adjudication settled the shape as: a
named human authority approves, and the initial posture is
auto-accept-with-maximum-sampling, never propose-only, because propose-only
floods the review queue the plan exists to protect.

Two of those three commitments do not survive the tree.

**"Maximum sampling" has nothing to sample with.** There is no sampling policy,
sampling mechanism, or sample-rate parameter anywhere in the shipped package — a
case-insensitive search for `sampling`, `sample_rate` and `SamplingPolicy` across
`contextplane/` returns three hits, all prose in comments. `SamplingPolicy`
appears only in the plan, as E5's future work, blocked behind E3 and E9. There is
not even an OpenTelemetry trace sampler. The one constant in the tree that sizes
a *drawn sample* is `HUMAN_RISK_SAMPLE_SIZE = 10`, for offline evaluation review;
every other sample-shaped number is a minimum-evidence floor, below which no
verdict is issued at all.

**And nothing would count the population.** ARC's observation machinery is the
obvious host, and its live arm is inert: `record_observation` and
`ensure_result_row` have callers only in tests, so on a real deployment
`load_aggregate_counters` sums zero rows. Every candidate that requires
observation runs to the seven-day cap with `observed_count = 0`, decides
`insufficient`, and can only qualify through the offline replay corpus. The
module's own docstring says so. A sampling rate applied to a counter that is
structurally zero is a number with no referent.

**"Propose-only floods the queue" cannot be evaluated either**, because there is
no notion of queue capacity to flood. The curation queue is not a table — it is a
read-only SQL projection over `memory_claims`, entered by satisfying a predicate
rather than by insertion. There is no capacity, quota, budget or throughput
concept attached to it anywhere; the only tunables are page size and a
cluster-wide depth reading with no target or threshold beside it. The argument
against propose-only may well be right, and this repository cannot currently
measure whether it is.

What the tree *does* say, loudly and repeatedly, is how this codebase handles a
cold start, and it is the opposite of auto-accept:

- A fresh tenant's auto-promote allowlist is **empty**, so an eligible,
  uncontested, owner-originated, non-high-impact claim still waits for a human.
- Calibration ships **no mapping at all** rather than an identity mapping,
  because identity would assert something nobody checked.
- Confidence decay applies **no modifier** below three observations: "an entity
  nobody has watched change is not an entity that changes slowly; saying so would
  be inventing an observation."
- ARC **refuses to boot** if activation-approval evidence exists that predates
  the first-party writer, and tells the operator to run an explicit reviewed
  bootstrap migration instead of grandfathering it.
- Verifier registration **refuses** a verifier permitted for all four evidence
  types, with the reason stated as: day-one bootstrap is exactly when waving it
  through is most tempting.

On the identity question, the relevant fact is that a "named human authority" is
not something this schema can currently assert. `actors.actor_kind` defaults to
`'human'` and carries no CHECK constraint. The four roles are all tenant-scoped.
The only deployment-level, non-tenant-grantable identity in the system is an
exact `(issuer, subject)` pair in `ARC_GLOBAL_OPERATOR_ALLOWLIST` — deliberately
not a role, empty by default, and empty grants nobody.

And an authority for writing this object already exists, which the plan did not
notice: ARC submission calls `assert_can_write_artifact` inside the transaction
that freezes an envelope. Global scope requires the operator allowlist and no
tenant role however elevated substitutes; tenant scope requires `ROLE_ADMIN` in
the owning tenant. Separately, ARC enforces actor separation as a count of
distinct principals — two normally, three when the risk classification is
`global_mandatory`, where the accepter may be neither the submitter nor the
approver.

## Decision

**The approving authority is the one that already exists, unchanged.** A global
envelope requires an `(issuer, subject)` pair in `ARC_GLOBAL_OPERATOR_ALLOWLIST`;
a tenant envelope requires `ROLE_ADMIN` in the owning tenant. No new
authority concept, no new role, and no new "named human" flag — the schema cannot
assert humanity today, and a column that claims to would be a comment with a
CHECK constraint's costs.

**The first envelope on a deployment is classified `global_mandatory` for actor
separation, so it requires three distinct principals.** That is the existing
mechanism doing exactly what it was built for, and it is the closest thing to
"named human authority" that is actually enforceable: three distinct
`(issuer, subject)` identities, at least one of them on the operator allowlist,
none of them able to fill two roles in the same transaction.

**The initial state is `advisory`, per ADR-0005, not auto-accept.** Every
cold-start decision in this repository refuses rather than guesses, and an
envelope that auto-accepts on day one is a governed object whose first act is to
be ungoverned. Advisory is not propose-only: nothing is queued, nothing waits for
a human, and no operation is refused. It records what would have been refused.

**The recording rate is one, and it is called recording, not sampling.** Sampling
at rate 1.0 is recording; naming it sampling implies a rate somebody could lower
and a population somebody is counting, and neither exists. This also gives the
ADR-0005 flip criterion its input — the offender scan is a query over these
records — which sampling at any rate below 1.0 would make unsound, since a
principal missing from a sample is indistinguishable from a principal with an
envelope.

**The word "posture" is not used.** It already names the per-tenant
promotion-review configuration (confidence floor, blast-radius threshold,
always-review list), which is admin-gated and audited. Two meanings for one word
in one governance subsystem is how a reader ends up reading the wrong docs.

**The first observation window is 72 hours with a minimum of 1000 observations,
capped at seven days** — ARC's existing `global` figures, reused rather than
invented. This follows how ARC justifies its other windows: the approval
challenge TTL is five minutes "matching D1 enrollment's own TTL", and the
thirty-day fingerprint retention is justified by the thirty-day appeal window. A
cold-start envelope with no history is precisely the case the global figures were
chosen for, and picking a fourth number here would be the behaviour ADR-0003
declined when it said a floor "is set with the fit, not guessed here".

**The window's live arm counts nothing today, and this ADR does not pretend
otherwise.** `record_observation` has no production caller, so a real deployment
reaches the seven-day cap with `observed_count = 0` and falls through to the
replay corpus. Defining the window now is worth doing because it fixes the
numbers before anyone has an outcome to fit them to; wiring the counter is
separate work, and until it lands, "72 hours or 1000 observations" means "seven
days" in practice. Saying that is the difference between a decision and a
promise.

## Assumptions

1. **Advisory recording is enough to make the flip decidable.** ADR-0005's
   assumption 2 applies here in its sharpest form: if most requests carry no
   identifiable principal, the record is empty for the wrong reason and the first
   envelope graduates on a vacuous criterion. The pre-flight must report the
   population it scanned, not only the offenders it found.
2. **Three distinct principals is available on a fresh deployment.** The operator
   allowlist is empty by default and empty grants nobody, so a deployment that
   has configured nothing cannot create a global envelope at all. That is
   intended — it is the same fail-closed shape ARC already has — but it means
   envelope bootstrap has a configuration prerequisite, not just an approval one.
3. **The queue-flooding argument is unmeasured, and the decision does not rest on
   it.** Advisory is chosen because it is what this codebase does at cold start,
   not because propose-only was shown to flood anything. If capacity ever becomes
   measurable (E5), the comparison can be made properly.
4. **72h/1000 will turn out to be wrong.** These are ARC's numbers for a
   different judgement, borrowed because borrowing is better than inventing. The
   condition for revisiting is the first envelope whose live arm actually counts.

## Alternatives rejected

**Auto-accept with maximum sampling, as adjudicated.** Rejected on
buildability first and principle second. No sampler exists, no population counter
exists, and the observation path that would host both is inert on every
deployment — so "maximum sampling" would ship as a constant nobody reads. On
principle: it inverts five separate cold-start decisions in this repository, each
of which chose to refuse rather than assume, and one of which is a startup
assertion that refuses to boot rather than grandfather day-one evidence.

**Propose-only: every action queued for review until an envelope exists.**
Rejected, and the adjudication's instinct is probably right — but for a reason the
adjudication did not give. Not "it floods the queue", which nothing here can
measure, but that the curation queue is a predicate over `memory_claims` and
authority decisions are not claims; routing them there means either a second
queue or a semantic stretch of the first, and the ADR would be inventing a review
surface as a side effect of a bootstrap decision.

**A new "named human authority" concept — a flag, a role, or an actor kind.**
Rejected because `actors.actor_kind` has no CHECK and defaults to `'human'`, so
the assertion would be unenforced from the day it shipped, and because a
deployment-level identity already exists in the operator allowlist. A fifth
identity concept beside four tenant roles and one allowlist needs a stronger
reason than the phrase in the plan.

**A dedicated approval endpoint for envelopes.** Rejected for consistency with
ARC, which deliberately has no standalone approve endpoint: `submitted → approved`
is a side effect of a two-call challenge/proof transaction, and exactly one module
is permitted by an AST lint gate to write activation-class approval evidence.
Adding a second way to approve a governed object would be the thing that gate
exists to prevent.

**Inventing a first-window number tuned to cold start.** Rejected: there is no
data to tune against, ARC already has two justified windows, and the tree's own
counter-examples are instructive — the replay-corpus TTL is a bare thirty-day
default with no stated reason and the promotion blast-radius threshold is 5 with
only the word "cautious" behind it. Those are what an invented number looks like
a year later.

## Consequences

Envelope bootstrap requires configuration before it requires approval: a
deployment with an empty operator allowlist cannot create a global envelope. That
is a real operational step and it is not currently documented anywhere, because
nothing needed it before.

Three distinct principals for the first envelope is a genuine cost for a small
team, and it is the same cost ARC already imposes for `global_mandatory`
activation. Deployments that find it impossible will discover that at bootstrap
rather than at audit.

The advisory record is written at rate one, so its volume is one row per
authority decision. That is the cost of a decidable flip criterion, and it is
bounded by the same decision count the enforcing stage will make. If it turns out
to be too much, the answer is a shorter retention on those rows, not a lower
rate — a sampled record cannot answer "who would this break".

"72 hours or 1000 observations" reads as a measurement and behaves as "seven
days" until the observation counter has a production caller. Recorded here so the
first person to rely on the number finds the caveat next to it rather than in a
module docstring two subsystems away.

Nothing here defines what an envelope *is*, which object carries it, or where the
decision is evaluated. This ADR decides who approves the first one and what state
it starts in; ADR-0005 decides how it graduates; the object itself is still E1's
to build.

## Dissent

*On rejecting auto-accept.* The adjudication's argument was about a real risk:
an agent platform whose first act is to refuse everything is one nobody adopts,
and "advisory, recording everything" is only distinguishable from auto-accept by
the fact that somebody later reads the log. A reviewer could fairly say this ADR
took the shape the codebase likes rather than the shape the product needs, and
that the codebase's cold-start conservatism was chosen for claims and mappings —
where a wrong guess corrupts data — rather than for authority, where a wrong
refusal just stops work. The counter is that advisory *is* auto-accept from the
agent's point of view; the disagreement is only about what gets written down.

*On borrowing ARC's window numbers.* 1000 observations over 72 hours is a
threshold designed for a global mandatory directive across a whole deployment.
Applying it to a single tenant's first envelope may mean the window never closes
on sufficiency and always closes on the cap, which makes the sufficiency
condition decorative. The honest alternative was to say "seven days, and
sufficiency is not evaluated yet", which is what actually happens. Recorded
because the borrowed number may be worse than no number.

*On the three-principal requirement.* Classifying the first envelope as
`global_mandatory` to get three-principal separation is reusing a risk
classification as a lever for an unrelated property. It works, and it is the kind
of reuse that is invisible until somebody changes what `global_mandatory` means
for a different reason and quietly weakens envelope bootstrap. A dedicated
`required_distinct_count` on the envelope would be clearer and would cost a
column.
