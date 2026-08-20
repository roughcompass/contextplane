# 0003 — Confidence decays against measured per-predicate churn

**Status:** Accepted 2026-08-19
**Reverses:** the rate model recorded in `contextplane/service/memory/confidence_decay.py`

## Context

Confidence decay stops an old claim from looking permanently authoritative. The
rate should track how fast the described thing actually changes: a six-month-old
fact about which database engine a service uses is nearly as good as new; a
six-month-old fact about who is on call is worthless.

The shipped model keys the rate on the claim's **category**, modified by its
**subject**. Its docstring rejects the per-predicate alternative in one line:

> Per predicate would be twenty-six figures nobody could defend one at a time.

That objection is sound against the proposal it was aimed at. It is not sound
against the proposal made here, and the difference is the whole reason this
reverses it.

## Decision

Decay is keyed on the **predicate**, with each predicate's half-life **derived
from its own observed churn rate** — measured from how often claims on that
predicate are superseded — rather than authored.

Category remains the fallback for a predicate with too few observations to
support a rate, so a new predicate is never left without one.

## Assumptions

- Supersession history is a usable churn signal. The bitemporal model records
  every supersession with both timestamps, so the rate is measurable from data
  the system already keeps rather than requiring new instrumentation. **This is
  the assumption most likely to be wrong**: if supersession is dominated by
  correction rather than genuine change, the measured rate reflects extraction
  quality, not volatility. The first fit must be inspected for that before it is
  allowed to select.
- A per-predicate rate needs a minimum observation count before it beats the
  category fallback. The floor is set with the fit, not guessed here.

## Alternatives rejected

- **Keep category + subject.** Its own docstring gives the reason to move: it
  cannot distinguish two predicates in one category that change at very
  different speeds, which is precisely the on-call-versus-database-engine case.
- **Author twenty-six half-lives by hand.** This is the alternative the shipped
  code rejected, and rejected correctly. Nobody can defend twenty-six numbers
  individually, and a table of undefendable numbers is worse than a coarse
  model that is honest about being coarse.
- **A single global half-life.** Simplest, and wrong in the direction that
  matters: it makes fast-churning facts look durable.

## Consequences

The objection in the shipped docstring is answered rather than ignored: nobody
defends the numbers one at a time because nobody chooses them. The cost moves
from authoring to measurement — the rates must be fitted, inspected, and
refitted, which is the same discipline `service/memory/calibration.py` already
applies to provider confidence, and it should reuse that machinery rather than
grow a parallel one.

Until a fit exists, the category model stays in force. Following
`calibration.py`'s existing rule, a fitted rate that fails inspection is stored
and never selected — an uninspected rate carrying a version string reads as
calibrated, which is the failure that module was written to prevent.

The docstring in `confidence_decay.py` must be updated in the same change. A
reversal that leaves the original reasoning in place as though it still governs
is how a codebase acquires two contradictory explanations of the same behaviour.

## Dissent

The shipped code is the dissent, and it is recorded here rather than deleted:
its author held that per-predicate rates are indefensible individually. That
holds for authored rates. Whether it also holds for measured ones depends
entirely on the assumption above — that supersession tracks real churn rather
than extraction error. If the first fit shows otherwise, this ADR is wrong and
the category model should stand.
