<!--
  title: "Use case: reported outcomes make the next answer better"
  audience: integrator, agent builder, producer, operator
  archetype: explanation (use-case scenarios)
  summary: Six scenarios showing how CI systems, agents, and humans report what happened, and what the system is and is not allowed to conclude from it.
-->

# Use case: reported outcomes make the next answer better

Context can only improve if somebody says what happened after it was used. The
scenarios below are the reports Context Plane is built to receive, and — just
as important — the conclusions it declines to draw from them.

The shared rule across all six: **reporting is not concluding.** A stored
observation is a record that a source said something at a time. Whether it
means the context was wrong is a separate question, decided by curation with
evidence, not by proximity.

---

## 1. A CI system reports a workflow conclusion

A build fails. The CI connector posts the conclusion to `POST /v1/signals`
under its registered source id, carrying the pull request it concerns as an
external reference.

What happens: the observation is stored with the authority its source was
declared to carry, at the three times involved (when it happened, when the
producer learned of it, when the server recorded it).

What does not happen: nothing marks the context that was served to that build
as bad. Nobody has said the two are related yet.

**If the delivery is retried** — a webhook redelivery, a connector restart —
the same envelope carries the same idempotency key, and the retry answers `200`
naming the row the first call stored. The ledger holds one observation, not
one per delivery attempt.

## 2. An agent reports that a served item was stale

An agent resolves context, works from it, and discovers that one item pointed
at a runbook step that no longer exists. It reports feedback bound to the exact
receipt item it used, rated `stale`.

The binding is what makes this usable later. "The context was bad" is
unactionable; "this item, on this receipt, was stale" identifies a specific
record whose freshness can be checked.

## 3. A human reports that a handoff worked

A developer picks up a task another developer left, resumes from the checkpoint
chain, and finishes it. They report `handoff_success`.

Positive reports matter as much as negative ones. A pipeline that only receives
complaints learns only what is broken and can never tell whether anything
improved — and the aggregate that says so needs both.

## 4. An operator reports something about the system itself

An operator notices that resolution latency spiked during an incident. That is
worth recording, but it is not feedback about a served answer — it cites no
receipt and no item.

It is reported as a **diagnostic observation**, and diagnostic observations are
never learning evidence. This is enforced rather than advised: a diagnostic
submitted with `learning_eligible: true` is stored with it set to false. There
is nothing for a later reader to check it against, so treating it as evidence
would be treating an unverifiable note as one.

## 5. Two sources disagree

An external deployment tracker reports that a rollout succeeded; an incident
system reports an outage in the same window. Both are stored, both keep their
own authority, and the disagreement becomes a routed curation case.

Neither report is silently dropped, and neither wins by arriving second. A
system that overwrote the first with the second would destroy the only signal
that anything is in dispute.

## 6. A team wants to know whether any of this is working

An admin reads the floored aggregates for the last thirty days: context
quality, reuse, handoff success, adequacy, plus the learning-side backlog and
promotion numbers.

Cells with too few actors or too few events are suppressed and carry no value.
That is the point at which somebody usually asks to see the underlying rows for
one person — and the answer is that the surface does not serve them. It reports
cohorts, and the floors are what keep "how is context working here" from
turning into "how is this individual working".

---

## What a reporter should carry

| Field | Why it matters |
|---|---|
| Idempotency key | Makes a retry safe; a reused key with changed content is refused rather than silently overwriting. |
| Producer identity | A participant reports as itself; only an external system reports under a foreign producer id. |
| External references | How an observation is found later from a pull request, run, or deployment somebody already knows about. |
| Classification | Travels with the record and governs who may read what is derived from it. |

## What none of this permits

- No per-person feed, and no team performance record.
- No conclusion drawn from adjacency: two facts in the same window are two
  facts.
- No copy of a workspace body or checkpoint payload into a derived claim — an
  excerpt and a pointer, so the original stays the only original.
