# 0026 — A judge is never the candidate, and its confidence is uncalibrated until fitted

**Status:** Accepted 2026-08-25

> **Numbering.** Commissioned as ADR 0023 by E24-T2; that number was taken before
> the task was claimed. See the note at the head of
> [ADR 0025](0025-simulation-is-separate-from-resolution.md) — one sequence spans
> both repositories, so this is 0026.

## Context

ADR 0025 lets the product generate an agent response. Two of the five evaluation
criteria — groundedness and answer relevance — cannot be computed by a program,
so a model grades them. That introduces a component whose failure modes are
measured, published, and specific enough to design against rather than to cite.

**Self-preference is measured, not hypothetical.** Reported at 10–25 % uniform
bias: a model scores its own output higher than a third party scores it, and the
effect does not require the judge to know which output is its own. The standing
rule in every published treatment is that the judge must not be the same model as
the candidate.

**Position, verbosity and format bias are properties of the prompt template.** A
judge that prefers the first-presented answer, or the longer one, or the one
with headings, is exhibiting a property of how it was asked — which is why
pinning the model id alone pins the wrong thing.

**Judge-model drift is 3–8 points on an unchanged rubric.** Larger than most
regressions anybody is looking for, which makes an unversioned comparison across
a model change a measurement of the model change.

**Judge confidence is a self-report on an uncalibrated scale.** This is the exact
problem `contextplane/service/memory/calibration.py` already solves for provider
confidence, and its argument transfers without modification:

> There is no mapping yet, and the honest form of that is no mapping at all. Not
> an identity mapping: identity asserts that a model reporting 0.9 is right nine
> times in ten, which nobody has checked, and storing that assertion under a
> version string is how an unexamined number acquires an authoritative look.

The forces pull against each other. The constraint that makes judging defensible
— a second provider family — is a constraint that makes judging unavailable to a
deployment that has bought one. And the mechanism that makes a launch decision
defensible — a panel — is 3× the cost on an interactive loop where nobody is
making a launch decision.

## Decision

### 1. The constraint is enforced, not advised

A simulation whose candidate model and judge model share a provider family is
**refused by the service**, and the refusal names both models and both families.

Not a docstring, not a validated-at-configuration-time warning, not a lint. An
advisory note is the shape of guidance that is followed until the day it matters
— which is the day someone is in a hurry, has one key, and wants a number.

The check lives in the service method both transports reach, per the standing
rule. Family is read from the provider selector (`anthropic`, `openai`, a
third-party registry name), which is the coarsest correct grain available: two
models from one vendor share training lineage whether or not they share a name,
and a finer rule would require knowing lineage the product cannot observe.

**A deployment with one family is told what it is missing.** The refusal names
the two families it needs, and the deterministic three criteria stay fully
available with no judge at all — so a single-provider deployment gets memory
recall, boundary violations and precision, and is told that groundedness and
relevance need a second family.

### 2. The pinned tuple is `(judge_model_id, rubric_version, prompt_template_hash)`

Carried on every judged result, all three, always.

The model id alone is insufficient because position, verbosity and format bias
live in the template. The template hash alone is insufficient because of the 3–8
point drift figure. The rubric version alone is insufficient because both of the
other two move underneath it.

Three values rather than one composite digest, because a reader comparing two
results needs to know *which* of the three moved, and a digest answers only
*that* something did. The digest is derivable from the tuple; the tuple is not
derivable from the digest.

### 3. Judge confidence is uncalibrated until a bin fit exists for that tuple

Recorded from the very first run, contributing nothing until a fit exists,
fitted in bins from human confirmations, and a fit that misses its bound is
stored and never selected. This is `calibration.py`'s mechanism reused rather
than re-derived, with one substitution: the separation key is the pinned tuple
rather than `(provider_id, model_id, strategy_id)`.

**The separation is load-bearing in the same way it is there.** A fit made under
one judge model does not describe another. A rubric edit makes a new population
for the same reason a scorer change does — the outcomes the fit was built from
were judged against a rubric that is no longer the rubric running.

**The corollary governs the UI and is not negotiable: until a fit exists for a
given tuple, a judge verdict is displayed as unproven.** A confident-looking
score on the screen whose job is calibrating trust is the same defect ADR 0019
refused when it rejected inferring `actor_kind` — a confident label on a guess,
in the place least able to absorb one.

### 4. Rubric edits mint a version, and a comparison spanning versions warns

Editing a rubric mints a version. A run keeps the version it ran under. A
comparison spanning two versions warns rather than silently conflating them.

Old runs are never re-scored under a new rubric. This is `protocol.py`'s
discipline, which freezes by digest and not by date and holds that a run whose
freeze does not match is *invalid, not adjusted*. Re-scoring would produce
numbers that were never observed, under a rubric that was not in force, and
present them beside numbers that were.

### 5. Not a fork, recorded anyway: the deterministic three are never model-judged

Required-fact recall, boundary violations and precision are computed by a
program. `judge.py` states the reason and it is quoted rather than paraphrased:

> a model-backed judge introduces a second thing whose behaviour can drift
> between the baseline run and the treatment run, and a difference in the final
> number would then have two possible causes with no way to tell them apart

This is recorded here because it is the property that keeps ADR 0024's single
journey honest. The two deterministic memory criteria are computed with no model
in the loop, which is what makes *"a failure of 1 or 3 implicates what was
served, a failure of 4 or 5 implicates what the agent did with it"* an
attribution independent of the judge.

### 6. A panel is for gating, not for every keystroke

One differently-familied judge for an interactive simulation. An opt-in panel of
three from three families with majority vote for a prompt-set run that is gating
a decision.

Split votes are the interesting output and are not smoothed. A 2–1 panel records
that it was 2–1, and the surfaces show it, because a criterion three judges
disagree about is the one most worth a human's time. Family diversity is required
of a panel for the same reason it is required of the single judge — a panel of
three from one family cancels nothing.

## Assumptions

1. **Provider family is a usable proxy for shared bias lineage.** Two models from
   one vendor are assumed to share enough training lineage that self-preference
   survives between them; two from different vendors are assumed not to. This is
   the coarsest defensible grain and it is an assumption rather than a
   measurement — nobody here has measured cross-family preference on this
   product's rubric.
2. **A deployment that wants judged criteria can obtain two provider families.**
   Recorded as an assumption because it is the one this decision imposes a real
   cost on, and because it may be false for an air-gapped deployment. Such a
   deployment gets the deterministic three and is told so.
3. **Human confirmations arrive in enough volume to fit a bin.** `calibration.py`
   requires 200 adjudicated outcomes before publishing a mapping. A judge tuple
   that never accumulates that many stays permanently unproven, which is the
   correct display and a real limitation: the calibration only pays off for a
   tuple somebody uses steadily.
4. **The rubric text and the prompt template are separable.** The rubric is what
   is being judged; the template is how the model is asked. Version and hash are
   pinned separately because an editorial fix to the template must not read as a
   rubric change, and a rubric change must not be hideable inside a template
   edit.
5. **Step-by-step reasoning before the verdict is required rather than
   encouraged.** Reported to improve judge reliability by 10–15 %, and — more to
   the point here — it is what makes a verdict arguable by the human who
   overrides it. A score with no trace is one a reviewer can only accept or
   reject.

## Alternatives rejected

**Same-family judging with a bias correction factor.** The tempting shape: allow
it, subtract the published 10–25 %. Rejected because a correction nobody has
fitted *is* the identity mapping `calibration.py` refuses, wearing a different
number. A factor drawn from a published range and applied to this product's
rubric asserts a measurement nobody made, and stores it under a version string
that reads as calibrated.

**A panel on every run.** 3× cost on exploratory iteration, buying insurance
against a decision nobody is making at that moment. Kept as an opt-in, which is
where it belongs: the person clicking "run this set as a launch gate" is a
different person, in a different mood, from the one iterating on a prompt.

**Trusting judge self-reported confidence as a probability.** This is the
identity mapping, stated plainly. Rejected on `calibration.py`'s argument in
full.

**Pinning only the judge model id.** Cheaper and wrong: it leaves the two biases
that are template properties unpinned, so a template edit that made the judge
prefer longer answers would move every score with nothing in the record saying
anything changed.

**Refusing to judge at all until calibrated.** Considered seriously — it is the
strictest reading of decision 3. Rejected because calibration is fitted *from*
judged outcomes paired with human confirmations, so a rule that withholds judging
until a fit exists can never produce the observations the fit needs. The judge
runs, records, and displays as unproven; that is what makes the bootstrapping
possible at all, and it is exactly what `calibration.py` does with provider
confidence.

**A blended score across the five criteria.** Rejected. Five criteria with no
partial credit produce five answers; averaging them would let a boundary
violation be offset by good prose, which is the one trade the safety criterion
exists to forbid.

## Consequences

Two provider families are needed for the judged half of the product, and a
deployment with one is refused rather than served a same-family verdict.

Every judged result carries three pinned values and a raw self-reported
confidence that contributes nothing until fitted. Storage grows by the tuple per
result; the alternative is results that cannot be grouped into populations.

**A judge verdict renders as unproven for a long time, possibly forever.** For
any tuple that never accumulates 200 human confirmations, the screen never stops
saying the number is unchecked. That is the intended behaviour and it is a real
cost: an evaluator will see "unproven" beside a verdict that is, in fact, usually
right, and some of them will conclude the feature is unfinished. The alternative
is the confident label on a guess, which is worse in the direction that matters.

**A rubric edit orphans the calibration for that tuple.** Deliberate, and it will
be annoying: improving a rubric resets its judge to unproven. The mechanism is
the same one that makes a swapped extraction model revert to uncalibrated with
nobody having to remember to act.

## Dissent

**Requiring a second provider family makes simulation unavailable to a
single-provider deployment, and that is a product decision dressed as a safety
one.** The objection has force. Plenty of deployments have one vendor
relationship, one procurement approval, one key; telling them the judged criteria
are unavailable is telling them the feature is unavailable, because
"groundedness and relevance" is what they came for.

It is answered rather than dismissed, in two parts. First, that deployment is
*told which two families it needs*, in the refusal, at the moment it tries —
rather than discovering later that its numbers were inflated by a bias it was
never warned about. Second, the deterministic three are fully available with no
judge at all, so the deployment is not left with nothing: it can measure
required-fact recall, boundary violations and precision, which is the entire
memory half of the rubric and the half that implicates what the product itself
served.

What the answer does not do is make the cost disappear. A single-family
deployment gets a strictly smaller product, on the strength of a published bias
range nobody has re-measured on this rubric — see assumption 1, which is the
weakest load-bearing thing in this record.

**A second dissent, sharper, about the calibration.** Decision 3 says a verdict
is unproven until 200 human confirmations exist for the tuple. In practice a
tuple changes whenever anyone edits a template, so the realistic steady state is
that almost every judge verdict on almost every deployment renders as unproven
forever, and "unproven" becomes a word people stop reading — which is the failure
mode of every warning that is always on.

The counter is that the alternative is a number that looks checked and is not,
and that a bin fit is *achievable* for a stable tuple somebody actually uses. The
honest residue is that this ADR has decided how to be uncertain rather than how
to become certain, and the second problem is not solved here.
