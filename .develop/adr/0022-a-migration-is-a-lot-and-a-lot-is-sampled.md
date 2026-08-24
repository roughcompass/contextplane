# 0022 — A migration is a lot, a lot is sampled, and the policy records the act rather than the authority

**Status:** Accepted 2026-08-24

## Context

E12's epic body says a bulk import records a "migrated-canonical" disposition
with `disposition_actor = policy-automated`, never by widening
`approval_authority`, and that its sampled audit draws from E5's single governed
`SamplingPolicy` rather than defining a second regime.

E12-T3 has been blocked on that sentence for three waves, and the entry named two
blockers. Grounding both found a third that subsumes them: **there is no
`migrated_canonical` disposition.** `curation_cases.py` enumerates six —
`confirm`, `reject`, `supersede`, and three proposal targets — and that is not
one of them. So the writer has nothing to write and the import has nothing to
record.

The two recorded blockers were also mis-described, and the correction matters
because it is what makes this decidable:

- **The bulk-import surface exists.** It is the connector registry. E12-T1's own
  outcome says an API that bypasses it is *the failure mode to refuse*, because
  it would be a second place source types are known. What connectors do not do
  is open curation cases.
- **`record_disposition` already anticipates the policy caller**, in as many
  words: *"a policy path that arrives later has to say so, which is the point."*
  It takes `actor_kind`, stores it rather than inferring it, and the owner check
  is satisfied by routing the case to the automation principal that then disposes
  it. `resolve_sync_actor` already mints that principal per sync source.

What was actually undecided is the one thing an ADR is for: **what a migrated
claim's disposition commits to.** `DispositionPolicy` records five dimensions per
disposition, and its own docstring says every field is a property of the *target*
and that the three proposal targets "deliberately disagree on all of them". So
the answers cannot be defaulted from a neighbour by resemblance.

**Why this looked harder than it is.** Stated as *"a policy decides that
unreviewed material is canonical"*, it reads as handing a batch job the authority
a person holds — and refusing it on those terms is right. But that is not what
the existing mechanisms do, and reading them changed the question.

## Decision

### 1. A migration is a lot, and the halt already in the tree is what accepts it

`require_minimum_sample` raises unless a **person** has inspected at least
`min_sample` claims in the category, and `inspected_dispositions` — the count it
is given — **excludes automated disposals**. Its docstring is explicit that this
is E12's halt:

> E12 inherits this halt rather than defining its own, because a batch import
> that sampled itself under its own rules would be grading its own homework with
> a marking scheme it chose.

So a migrated lot cannot be accepted until a person has inspected its sample to
the policy floor, and no amount of automation shortens that floor — *"a caller
wanting to satisfy the floor cannot do it by automating more."*

This is ordinary acceptance sampling, and it is what makes the rest decidable.
The policy is not deciding that unreviewed material is canonical. A person
inspects the sample; the lot is accepted or halted on that inspection; and the
policy disposition is the bookkeeping that records the same outcome across the
uninspected remainder, which is what a lot *is*.

### 2. `disposition_actor` records the act; `approval_authority` records who stands behind it

These answer different questions and the epic is right that conflating them makes
a policy's write indistinguishable from an approver's. They are not in tension
here, because they are about different things:

- `disposition_actor_kind = policy` — a batch job performed the act. True, and
  the reason `inspected_dispositions` excludes it.
- `approval_authority = catalog_owner` — the same authority `propose_canonical`
  answers to, because it is the same target. The owner accepts the *lot* on the
  sample; they do not sign each row, and acceptance sampling has never claimed
  they do.

Nothing is widened. `catalog_owner` was already the authority over the canonical
graph before this ADR existed.

### 3. The five dimensions, and where each comes from

```
migrated_canonical:
  approval_authority   catalog_owner
  evidence_threshold   a lot whose review sample a person inspected to its
                       category's policy floor
  scope                one subject and predicate in the canonical graph
  supersession         the canonical row it replaces is closed at the asserted
                       interval
  rollback             reverse the promotion from its journal entry
```

**Four of the five are the target's, not the disposition's**, and that is the
point of `DispositionPolicy`'s docstring rather than a shortcut around it: scope,
supersession and rollback are properties of writing a canonical fact, and
`propose_canonical` answers them the same way for the same reason. A migration
that wrote canon by different rules would be a second canonical graph.

**The one that is genuinely this disposition's is the evidence threshold**, and
it is the only place migration differs from promotion. `propose_canonical`
requires *"a settled, uncontested claim above the tenant confidence floor"* —
a statement about one claim. `migrated_canonical` requires a statement about a
**lot**: the sample was inspected to the floor. That is the difference between
promoting something somebody read and accepting a batch on evidence about the
batch, and it is the whole substance of the decision.

### 4. A migrated claim is still a claim, and the disposition does not write canon

`record_disposition`'s docstring already binds this: *"Nothing here writes what
the disposition proposes — the surface that owns the target does that, under the
authority recorded on this row."* `migrated_canonical` carries
`target_kind = canonical_fact` and asks the promotion surface, exactly as the
three proposal dispositions do. It is not a back door that writes canon from the
curation table.

## Assumptions

1. **A migration has a claim category, so it has a floor.** `acceptance_for`
   falls back to the tenant's heaviest plan for an unregistered category, so a
   migration cannot escape a floor by not naming one. Verified in
   `sampling_policy.py` rather than assumed.
2. **A lot is per category, per run.** Two categories in one import are two lots
   with two floors, because their floors were set separately and for different
   reasons. A single lot spanning categories would be accepted on the weakest.
3. **The automation principal is per source, not global.** `resolve_sync_actor`
   already resolves one per sync source, so "which policy disposed this" is
   answerable from the row without a registry of service accounts — the thing
   `DISPOSITION_BY_POLICY`'s own comment says must not be required.

## Alternatives rejected

**Staging migrated material as ordinary claims and letting curation find it.**
The honest-looking option, and it fails on volume in the direction that matters:
a migration puts every row into the contested queue, the queue's ranking is
swamped, and the genuinely contested claims a reviewer should see are buried
under material nobody disputes. Acceptance sampling exists precisely because
inspecting every item of a large lot is not the careful choice — it is the choice
that stops the inspection happening at all.

**A `migration_owner` authority distinct from `catalog_owner`.** Rejected because
it invents a second signatory over one graph. The question "who may put a fact in
the canonical graph" already has an answer, and a migration is not a reason to
give it a second one — it is exactly the moment somebody would want to.

**Letting the policy proceed on a short sample and recording that it did.**
Rejected on `require_minimum_sample`'s own argument, quoted because it is the
sharpest statement of it: *"Proceeding on a short sample does not weaken the
guarantee; it removes it, while leaving a number that still looks like one."*

**Deferring this to a governance body outside the repo.** This was the previous
position and it was wrong on inspection. The DORA thresholds E4-T6 waits on are
an external legal fact that no amount of reasoning here produces. What a
disposition in *this* system's vocabulary commits to is this system's own
semantics, and the mechanisms that constrain the answer — the sample floor, the
exclusion of automated disposals from the count, the separation of act from
authority — were all already built and all already point one way.

## Consequences

`migrated_canonical` joins `DISPOSITIONS` and `DISPOSITION_ACTOR_KINDS` gains its
first real caller. E12-T4 is unblocked: a connector run opens a case per imported
claim, routes it to the run's automation principal, and disposes it — refusing
the whole lot if `require_minimum_sample` raises. E12-T3 is unblocked behind it.

**A migration can now fail for a reason that is not a bug**, and that is the
intended behaviour rather than a rough edge: an import whose category floor has
not been met by human inspection halts with `SampleTooSmall` naming the
shortfall. An operator's remedy is to review more, which is the point.

**`inspected_dispositions` gets a second reason to be right.** E12-T2 gave it its
first reader; this gives it a writer whose whole safety property is that its own
disposals do not count toward the floor it must clear.

## Dissent

**The lot boundary is doing a lot of work and this ADR does not define it
tightly.** A "lot" here is one category within one connector run, and a
sufficiently determined importer can shrink lots until each one's floor is
trivially met by a handful of inspections. Acceptance sampling assumes a lot is
a natural production batch, not a unit the producer chooses to suit the plan.
Assumption 2 states the boundary but nothing enforces a minimum lot size, and
`min_sample` is derived from tolerance and risk rather than from lot size — so
splitting is not detectably cheaper under the arithmetic, but it is not
detectably *not*, either.

The counter is that a connector run is not chosen per import — it is the sync
source's own schedule — so the boundary is a fact about how material arrives
rather than a knob. That is true today. It stops being true the first time
somebody adds a manual import that takes a batch identifier from its caller, and
whoever adds it should read this paragraph before deciding the boundary comes
from the request.
