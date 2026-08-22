# 0014 — A derived magnitude is neither validated nor grandfathered

**Status:** Accepted 2026-08-22

## Context

E5 introduces numbers that decide what a human reviewer looks at first: an
acceptance-sampling policy, a leverage term, and an expected-loss term. E9's
property applies to all of them — *no ungoverned score orders anything a user
sees* — and a review queue is the most literal instance of that rule in the
product. So every one of them has to enter `ranking_registry.json`.

Grounding what that costs is what produced this ADR.

**Seven magnitudes are registered and all seven are `grandfathered`.** Their
recorded reasons are consistent and unusually honest: most say the label a
validation would rest on — *whether a claim was later retrieved and cited on a
succeeding turn* — needs a citation-to-outcome join that does not exist.

**The registry's status vocabulary has exactly two values**, and
`scripts/check_governed_magnitudes.py` states what each one means:

> Registration says a number is owned and has a written reason. Validation says
> somebody checked it predicts, and names who, against what data, and with what
> result. `grandfathered` says neither, honestly.

`validated` demands four evidence fields — `validated_by`, `validated_on`,
`method`, `result` — "because a status without its evidence is a word, and the
word is what a later reader would trust". `grandfathered` demands a reason,
"because an exemption nobody has to justify is one nobody will revisit".

**E5's three numbers are three different kinds, and only one of them fits this
vocabulary.**

*Expected loss* is a fitted quantity with no data. It needs a loss model — what
a wrong disposition actually costs — that nobody has stated, and the same
missing outcome join blocks it. It is `grandfathered` if it exists at all.

*Leverage* is measurable today. `get_blast_radius` already answers "how much
depends on this", and `promotion_eligibility` already treats that answer as a
governance trigger against a per-tenant threshold. It is a read, not a model.

*Acceptance-sampling parameters* fit **neither status**, and that is the
problem this ADR exists to solve. Given a stated acceptable defect rate and a
stated consumer's risk, the sample size and accept number follow by arithmetic
from the operating-characteristic curve. There is no held-out result because it
is not a prediction — it is a definition. Recording it as `validated` would mean
inventing a `method` and a `result` for a check nobody ran, which is exactly the
"status without its evidence" the gate refuses. Recording it as `grandfathered`
would assert that nobody checked whether the number is right, which is false in
the other direction: the derivation *is* the check, and unlike every other entry
here it is reproducible by anybody with a calculator.

## Decision

**Add a third validation status, `derived`, and give it its own evidence
requirements.**

A `derived` entry must carry:

- `derived_from` — the stated inputs the number follows from, as values rather
  than prose. For a sampling policy: the acceptable defect rate and the
  consumer's risk.
- `derivation` — the named procedure taking those inputs to this number,
  specific enough that a reader can redo it. "Binomial OC curve, single sampling
  plan" is a derivation; "standard acceptance sampling" is not.

`reason` stays required, as it is for `grandfathered`, because the *inputs* are
still a judgement somebody made and the judgement needs an owner.

**What `derived` claims, precisely, and what it does not.** It claims: given
these inputs, this number is correct, and the arithmetic is checkable without
data. It does not claim the inputs are right. A sampling plan derived from an
acceptable defect rate nobody agreed to is a correct plan for the wrong
question, and `derived` must never be read as evidence that it is the right
question.

**`requires_validated` generalises rather than gaining a sibling.** Today it
means "this consumer refuses a number nobody checked". A consumer of a sampling
policy needs the same protection and would be wrong to demand `validated`,
because that would insist on held-out evidence for something that produces
none. The flag's rule becomes: `requires_validated: true` is satisfied by
`validated` *or* `derived`, and never by `grandfathered`. The name is now
slightly wrong for what it enforces, and the entry keeps it rather than churning
seven existing records over a word.

**E5's expected-loss term does not enter the registry at all until a loss model
exists.** Not as `grandfathered`, which would make it look like the other seven
— numbers that ship and order things while awaiting evidence. This one does not
ship. If the loss model is not stated, **the queue ranks on leverage and
sampling priority and says so on the surface that displays it**, which is a
smaller claim and a true one. A reviewer who believes a rank accounts for cost
will defer to it.

## Assumptions

1. **Acceptance sampling's inputs can actually be stated by somebody.** The
   arithmetic is free; the acceptable defect rate is a policy position that
   needs an owner. If nobody will own it, `derived` buys nothing — the entry
   would derive a number from an input nobody stands behind, and
   `grandfathered` would be the honest status after all.
2. **The sample is drawn representatively.** This is the empirical assumption
   inside a derivation that otherwise has none, and it is the one most likely to
   be violated here — see the dissent.
3. **Three statuses is where this stops.** A fourth would mean the vocabulary is
   tracking something other than "what kind of evidence stands behind this
   number", and at that point the right move is a different field, not a
   longer enum.

## Alternatives rejected

**Record the sampling parameters as `validated`, treating the derivation as the
method.** Superficially reasonable and it corrupts the strongest signal in the
registry. `validated` currently means one thing across seven entries — nobody
has it — and the first entry to claim it should be one where somebody measured
predictive performance. Spending that word on a derivation makes the next reader
unable to tell the two apart, which is precisely the failure the four evidence
fields exist to prevent.

**Record them as `grandfathered` with a reason explaining the derivation.** The
minimal-change option, and it is what a hurried author would do. It is wrong
because `grandfathered` is load-bearing: it is the set of numbers this project
owes validation on, and E5's sampling parameters do not belong in that queue.
Putting them there makes the debt look bigger than it is and hides the entries
that genuinely need measuring.

**Keep two statuses and skip the registry for sampling parameters**, on the
grounds that they are configuration rather than a ranking magnitude. This fails
E9 directly: the sampling policy decides which items a human inspects, and that
is ordering something a user sees. The whole reason E9 restated its property was
that "the arithmetic sits in one function and the ordering elsewhere" — a
sampling policy is the same shape.

**Add `derived` but let it satisfy nothing, i.e. keep `requires_validated`
strict.** Then no consumer could ever gate on a derived magnitude, and the only
way to ship E5's sampling policy would be to lie about its status. A gate that
forces a false record is worse than a looser gate.

## Consequences

`scripts/check_governed_magnitudes.py` gains a status and two evidence fields,
and the loader gains the same. That check's own docstring notes the rule is
enforced twice on purpose — "it fails in review rather than at boot, and it
survives a change that relaxes the loader" — so both halves move together or the
protection is one-sided.

The registry stops being a two-bucket ledger of *checked* and *not yet checked*,
which was easy to read at a glance. Three buckets is more to hold, and the third
one's meaning is the subtlest. That cost is accepted because the alternative is a
number whose recorded status is false in one direction or the other.

E5-T3's ranked queue will need a magnitude for whatever prevents starvation, and
that one is likely `grandfathered` — an age weighting is a reasoned position, not
a derivation and not a measurement. This ADR does not make the registry mostly
`derived`; it makes room for the small number of entries that genuinely are.

## Dissent

**The strongest objection is that `derived` launders an empirical assumption as
arithmetic.** Acceptance sampling's OC curve is exact only if the sample is a
representative draw. E5's queue is *ranked*, and E5-T4 already identifies that
policy-automated dispositions are not inspections — so the sample this policy
governs is drawn from a population the queue has deliberately ordered and partly
disposed of by machine. Under those conditions the derivation is arithmetic
about a lot that does not exist, and calling the result `derived` gives it a
status implying reproducibility while the assumption doing the real work is
unrecorded and unchecked.

The response is that `derived_from` should carry the representativeness
assumption alongside the two rates, and that assumption 2 above is where it
lives. But an assumption recorded in an ADR is weaker than four required fields
in a gate, and the honest reading is that this decision has moved the
unvalidated part somewhere less visible rather than removing it. If the sampling
turns out to be biased by the ranking, the entry will still say `derived`.

A second, narrower one: `requires_validated` now means "requires validated or
derived", and keeping the name is a deliberate choice to avoid churn. Names that
have stopped describing what they enforce are how the next reader forms a wrong
model cheaply, and seven records is not a large migration. This decision took
the low-churn option and it is the part most likely to be regretted.

A third: nothing here says what happens when a `derived` entry's *inputs*
change. A new acceptable defect rate makes the old number wrong rather than
stale, and the registry has no notion of an entry being invalidated by its own
premise moving. That gap exists for `grandfathered` too and is not created here,
but `derived` is the status where it bites soonest, because the inputs are
written down and therefore editable.
