<!--
   title: Feedback and learning: reported outcomes and what is derived from them
  audience: evaluator, integrator, agent builder, operator
   summary: How reported observations and feedback become bounded, cited learning evidence without becoming a surveillance record.
-->

# Feedback and learning: reported outcomes and what is derived from them

Context Plane records what happened *after* an answer was served. A CI system
reports a workflow conclusion, an agent reports that a served item was stale, a
human reports that a handoff worked. Those reports are stored as observations,
bound to exactly what they are about, and used as evidence for claims the
curation pipeline can later route.

Nothing here concludes anything on its own. Ingestion stores what a source
said; it derives no success, no failure, no causal link. The separation is the
point: a system that inferred "the deployment failed because the context was
wrong" from two adjacent facts would manufacture evidence, and the manufactured
evidence would be indistinguishable from the reported kind.

This page is for readers deciding how to report outcomes or consume what is
derived from them. Operators should use the
[feedback and privacy runbook](../06-operations/07-feedback-and-privacy.md).
Field-level contracts live in the [REST API reference](../05-reference/01-api.md).

---

## Three things get reported, and they are not the same

| What | Surface | What it means |
|---|---|---|
| **An observation** | `POST /v1/signals` | Something happened in a registered source: a workflow concluded, a deployment rolled back, an incident opened. |
| **Feedback about a served answer** | `POST /v1/context/feedback` | A verdict on context that was actually served, bound to the receipt — and usually the exact item — it is about. |
| **A diagnostic observation** | `POST /v1/context/feedback` | A note about the system that cites no served answer, and is therefore never learning evidence. |

Observations arrive through a **registered source**. A source must be declared
before it may write anything: the declaration records what its claims are worth
(its authority tier) and how many it may write per window. An undeclared source
is refused outright, so an unowned connector cannot quietly become an input to
what the system believes.

## Who may report as whom

A participant of this deployment may only report as itself. A `human` or
`agent` observation must carry the caller's own actor id; only an `external`
observation carries a foreign system's producer id. This is what keeps
"the pipeline said so" from being writable by anyone who can reach the route.

## Feedback binds to what it judges

Feedback that cannot be traced back to what it judges cannot be used as
evidence about anything, so the binding is resolved against the receipt's own
rows before anything is written:

- **Item-specific** feedback cites a receipt and an exact item on it.
- **Receipt-level** feedback cites a receipt and no item.
- **A diagnostic observation** cites neither, and is never learning-eligible —
  even when the reporter asks for it to be.

An item that belongs to a different receipt is refused rather than stored, and
a refused submission leaves no row behind.

## Reporting the same thing twice is safe

Every submission carries its own idempotency key, and for observations that key
is part of the envelope rather than a transport header — a proxy that dropped a
header would otherwise give one submission two identities that could disagree.

A call that stored a submission answers `201`. A call that recognised one
already stored answers `200` with the same body, so a client retrying a dropped
response can tell that its retry found the first write rather than making a
second. A key reused with *different* content answers `409`, because nothing
the caller can retry will make both reports true.

## What is derived, and what is preserved

Reported evidence feeds the same curation pipeline described in
[Living Memory](07-living-memory.md). Extraction stages bounded claims that
carry an excerpt and a pointer back to the evidence — never a copy of the
workspace body or checkpoint payload the excerpt came from. Contradiction is
preserved rather than resolved on the way in: two sources that disagree produce
a routed curation case, not a silent winner.

## Aggregates are floored before anyone reads them

Reported outcomes are read back as aggregates, never as a per-person feed.
Every cell is thresholded before it is served: a cell that does not clear both
floors is suppressed and carries no value, and the floors in force are served
alongside the numbers so a suppressed cell is legible rather than mysterious.

| Floor | Value |
|---|---|
| Distinct actors per cohort | 5 |
| Events per cell | 5 |

The reads are admin-only, and the whole metric set is served over one window
rather than one metric per request. A caller able to name a single metric could
ask for exactly the one whose cells are thin and repeat that across windows
until a suppressed cell was bracketed; serving the whole set makes that probing
no cheaper than reading everything.

This surface reports **cohorts, not people**. It is not a performance record
for an individual or a team, and the floors exist to keep it from becoming one
by accident.

## What retrieval does with the evidence

Whether workspace recall uses semantic retrieval at all is a recorded decision
rather than a runtime toggle. The approved arm is committed as a closed
artifact that the code reads at import time; configuration cannot be more
permissive than it, because configuration has no say in the matter. See
[Retrieval and context](10-retrieval-and-context.md).

## Erasure reaches what was derived

An erasure request removes the actor's reported observations and feedback, and
propagates to what was derived from them — staged claims, evidence links, and
the derivative records that point at any of it. Erasure is judged by what it
reaches, not by what it deletes first, and the request returns per-subsystem
counts so an operator can see what was actually covered. See
[Data governance](09-data-governance.md).
