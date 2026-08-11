# Lifecycle Pilot Runbook

Operating procedures for a delivery-lifecycle pilot: what to do when the
orchestrator submitting outcomes goes down, what to do when this service goes
down, how to drain a backlog without making things worse, and who may stop the
pilot.

Audience: the pilot's accountable owner, the platform/CI owner who runs the
submission step, and operators with database access. For general operations see
[ops.md](01-ops.md); for the memory-curation queue that outcome-derived claims
flow into, see [memory-curation.md](05-memory-curation.md).

---

## The shape of the integration, in one paragraph

An orchestrator — a CI workflow step, in this pilot — posts delivery outcomes to
`POST /v1/signals` when work concludes. Every submission is initiated by that
side. This service starts nothing, waits on nothing, and holds no workflow
state: there is no run table, no stage table, and no state machine. A stage name
is data a caller supplies on a context request, compared and never stored.

That asymmetry decides everything below. **An outage of the orchestrator is an
absence.** **An outage of this service is something the orchestrator must
notice.**

---

## What connects an outcome to the context that preceded it

An outcome is joined to its receipt through the external work both of them name
— repository, commit, work item, deployment — and through nothing else. There is
no receipt id in an outcome and no outcome id in a receipt.

A reference is identified by four fields together: source system, source
namespace, **kind**, and external id. Kind is part of that identity, so an
outcome submitted with a misspelled kind does not fail. It stores, it binds to a
reference row of its own, and it then joins to nothing — and the change reads
downstream as one whose outcome never arrived.

**The submission path refuses both unjoinable shapes** (an outcome citing no
external work, and one citing a kind outside the closed set), so this failure
should not reach storage. The vocabulary is ten values:

```
run  stage  work_item  repository  artifact  action  build  deployment  incident  outcome
```

**If an outcome looks missing, check the kind spelling first.** The symptom of a
bad kind is silence, not an error, and it is indistinguishable from an outcome
that was never sent.

```sql
-- Two reference rows for one external id, differing only by kind, is the
-- signature of a misspelling that got past an older or bypassed submitter.
-- One external id legitimately carrying two kinds is possible but rare, so
-- read the result as a shortlist to check rather than as a verdict.
SELECT external_id, array_agg(DISTINCT kind ORDER BY kind) AS kinds
  FROM context_external_references
 WHERE tenant_id = :tenant
 GROUP BY external_id
HAVING COUNT(DISTINCT kind) > 1
 ORDER BY external_id;
```

### The failure submission cannot refuse: a right kind with a wrong id

A misspelled kind is refused at submission. **A correctly spelled kind carrying
the wrong external id is not, and cannot be.** The id belongs to the other
system; this one has no way to know which of its values was meant. Such an
outcome binds cleanly to a legitimate reference row, joins no receipt, and reads
downstream exactly like an outcome that never arrived.

The query below is the compensating control. It lists outcomes that have sat
bound and unjoined for longer than a chosen age — the age is what separates a
wrong id from a receipt that has simply not been written yet, so **set it from
the submitting orchestrator's own latency**, not from this value. Six hours is a
starting point for the pilot's submitter, not a measured threshold.

```sql
-- Outcomes bound to external work no receipt cites. Adjust the interval to
-- comfortably exceed how long the submitter may lag; too short a window
-- reports healthy outcomes whose receipts are still in flight.
SELECT binding.subject_id AS signal_id,
       reference.kind,
       reference.external_id,
       binding.bound_at
  FROM context_reference_bindings AS binding
  JOIN context_external_references AS reference
    ON reference.reference_id = binding.reference_id
   AND reference.tenant_id = binding.tenant_id
 WHERE binding.tenant_id = :tenant
   AND binding.subject_type = 'external_signal'
   AND binding.bound_at < now() - INTERVAL '6 hours'
   AND NOT EXISTS (
       SELECT 1
         FROM context_reference_bindings AS receipt_binding
        WHERE receipt_binding.tenant_id = binding.tenant_id
          AND receipt_binding.reference_id = binding.reference_id
          AND receipt_binding.subject_type = 'context_item'
   )
 ORDER BY binding.bound_at;
```

**Empty means empty, not unreachable.** No rows is the query having run and
found nothing stuck. If it errors, that is not a clean result — do not record
"no unjoined outcomes" from a read that could not reach the database.

**What to do with a row.** Compare its `external_id` against the work the
receipt named. A near-miss (a truncated sha, an id from a neighbouring
repository, a run id where a work-item id was meant) identifies the submitter
bug. The outcome is not repaired here: the row is evidence, and the fix belongs
in the submitter. Rebinding it by hand would fabricate a join the source never
asserted.

---

## The orchestrator is down

Nothing to do on this side, and that is the design rather than an omission. No
partial state exists to repair because no workflow state exists at all.
Recovery is a read, not a reconciliation.

The obligation sits with the submitter: **undelivered submissions are persisted
and replayed after recovery, with their original times preserved.**

### Draining a backlog

1. Replay the queued submissions in any order. Each is its own occurrence,
   identified by `{source_system}:{object}:{object_id}:{attempt}`.
2. **Preserve the original event times.** Do not restamp a submission with the
   moment the queue drained. The gap between when work concluded and when this
   service admitted it is the evidence of the outage, and restamping launders it
   into looking like prompt reporting.
3. **Preserve timestamp offsets byte-stably, or serialize in canonical UTC.**
   The stored content digest is computed over the rendering of those instants,
   so the same moment replayed under a different offset would digest differently
   — and a true redelivery would then be refused as a conflicting reuse of its
   own key instead of converging on the row it already wrote.
4. Watch for `429` (see below) and pace accordingly.

### Reading the response

| Response | Meaning | Retry? |
|---|---|---|
| `201` | Stored for the first time. | No. |
| `200` with `replayed: true` | This exact submission was already stored. The retry found the first write. | No — it succeeded. |
| `409` | The submission key was reused with **different content**. | **Never.** This is a bug in the submitter, not a transient fault. Fix the payload or the key. |
| `429` | The source is over its declared ingest ceiling for the current window. | Yes, after the window rolls. |
| `404` | The source id is unregistered, or belongs to another tenant. Both answer identically on purpose, so a source id cannot be used to probe what exists in other tenants. | No — check the id and the tenant. |

### The ceiling interaction, which decides whether a drain is realizable

- **Replaying something already stored costs nothing.** The redelivery check
  runs before the ceiling is consulted, so a client retrying a drain it could
  not finish does not make its own situation worse.
- **First-time submissions do spend the ceiling.** A backlog larger than the
  window's remaining allowance is refused partway through with `429`.

So size the pilot source's `ingest_ceiling` against the largest backlog an
outage is expected to produce, not against steady-state traffic. To inspect or
raise it:

```sql
SELECT source_id, ingest_ceiling, window_seconds, window_count, window_started_at, breaker_open_until
  FROM memory_source_governance
 WHERE tenant_id = :tenant;
```

A `breaker_open_until` in the future means the source tripped its circuit and
will be refused until that instant regardless of the window.

### How long a replay may be delayed

**72 hours is an operational expectation for the pilot's submitter, not a
behaviour of this service and not a gate.** Nothing computes the age of a
replay, no index bounds it, and a 73-hour replay is stored and deduplicated
exactly as a one-hour one is. It is stated so an outage exceeding it is
recognised as a recorded pilot incident rather than absorbed silently.

---

## This service is down

The orchestrator must **fail loudly at its own timeout** (30 seconds or less is
the recommendation) and must never represent unserved context as context this
service provided.

**Absent context is absent.** Proceeding without it is a decision the
orchestrator's own policy may permit; recording it as though it had been served
is not, because every later measurement of whether governed context helped
depends on knowing which changes actually received any.

### What a degraded answer looks like, and why it is not a failure

A context response is not all-or-nothing. Every response carries four blocks and
a `state`, and a partial answer is reported rather than hidden:

- `state: complete` — every block answered.
- `state: degraded` — at least one block is degraded or failed. `quality.reasons`
  says which and why.
- `state: blocked` — the response cannot be relied on.

**A `degraded` or `blocked` response is still `200`.** It is a correct answer to
"what context is available right now", and the answer is "not enough to rely
on". Branch on `state` and `quality`, never on the HTTP status alone, and never
infer completeness from a block simply having items.

`quality.cacheable` is `false` on any degraded response. **Do not cache one.**
A cached degraded answer outlives the outage that caused it and hands the next
reader a stale picture with no sign that anything went wrong.

### Failed and empty are different, and the difference is the point

An empty block means the sources had nothing to say. A failed block means they
could not be asked. Treating the second as the first is how an agent proceeds
confidently on an incomplete picture — it is the specific failure this response
shape exists to prevent.

The same distinction applies to the governance block: a response naming no
attested resolution returns an empty governance block with a note saying so.
That is a complete answer whose emptiness the caller can fix by supplying one,
not a degraded response.

### Every resolution leaves a receipt, including the ones that failed

A resolution whose evidence could not be written **fails outright** rather than
returning an answer. An answer nobody can later show they were given is
indistinguishable from an audited one at the moment somebody needs the audit.

The practical consequence for an incident review: failed resolutions are in
`context_receipts` alongside the successful ones, carrying the state the caller
was actually given.

```sql
SELECT receipt_id, state, cacheable, resolved_at
  FROM context_receipts
 WHERE tenant_id = :tenant
   AND resolved_at BETWEEN :incident_start AND :incident_end
   AND state <> 'complete'
 ORDER BY resolved_at;
```

---

## Notifications are not part of this pilot

This service does make outbound calls in general — subscriber webhook delivery,
with retry and dead-lettering. **The pilot does not subscribe the orchestrator
to notifications.** There is therefore no channel from here to it, and the claim
that an orchestrator outage cannot corrupt state covers the outcome-submission
flow only. If a subscription is added later, it brings its own failure modes and
this runbook does not cover them.

---

## The frozen scenarios, and what they are for

Six changes that ran through these surfaces during the pilot are preserved as a
regression corpus under `tests/fixtures/lifecycle_context_pilot/`. One file per
change, anonymized: teams and repositories are named for the role they play in
the dependency relationship, and the pilot's own change identifiers are not
carried in any form.

They are not documentation. `tests/conformance/test_lifecycle_pilot_corpus.py`
checks the corpus is well formed and non-vacuous, and
`tests/integration/test_lifecycle_pilot_exit.py` replays those scenarios against
the shipped surfaces. Run both together:

```bash
make test-lifecycle-pilot     # needs Docker; the exit half uses a real database
```

Each file records the block coverage the change reached, the trust label its
items carried, whether it retrieved reviewed learning from an earlier change and
whether that helped, every refusal or degradation it hit, and its counts. A
change that hit no refusal says so explicitly rather than omitting the field.

**One change from the pilot is deliberately absent.** Its checkpoint carried a
pasted vendor-advisory excerpt whose license does not permit redistribution, and
admission review withheld it. Six approved records against a floor of five is a
margin of one: if a later admission review withdraws another, the corpus is
short and must be reported short rather than topped up with an invented
scenario. The corpus gate fails below five for exactly that reason.

### What the corpus records that an operator should know

**An outcome that fails to join raises no alarm.** Four outcome envelopes during
the pilot arrived with the reference kind spelled `workflow-run` instead of
`workflow_run`. They stored cleanly, bound cleanly, and then never joined to the
receipts citing the same external id — so the changes read as work whose outcome
had not arrived yet. Detection took two days and happened because somebody
compared by hand.

The spelling itself is now refused at the boundary, so this exact failure cannot
be re-entered, and the exit gate asserts that refusal. **What still does not
exist is a signal for a join that did not happen for some other reason.** A
receipt that reads "no outcome yet" is indistinguishable from one whose outcome
went somewhere else, and nothing reports the difference. Treat an unexplained
run of outcome-less receipts as a data-quality question rather than as evidence
that CI was quiet — the diagnostics under
[what connects an outcome to the context that preceded it](#what-connects-an-outcome-to-the-context-that-preceded-it)
are where to start.

**A reconnect returns superseding learning without marking it as superseding.**
One pilot change resumed correctly and unhelpfully: the response carried
reviewed learning that had overturned the checkpoint's own premise, and the
participant did not notice, because newer learning arrives beside the checkpoint
rather than flagged as contradicting it. The surface returned exactly what it
contracts to return. This is recorded as an open usability finding, not a
defect, and it is not fixed. An operator briefing participants on a resume
should say so out loud.

**Retrieval that matches on every recorded dimension can still be wrong.** One
change retrieved a reviewed workaround that matched on repository, capability,
environment, stage and work type, applied it, and was reverted after an
incident. The workaround encoded another service's assumption about retry
budgets. The dimensions the selection carries did not include the one that
mattered, and no amount of matching would have caught it.

---

## Stop conditions

**Any one of three roles may halt the pilot. One is enough; halting requires no
consensus and no review meeting.**

1. **The accountable pilot owner** — for any reason, including "this is not
   working".
2. **The pilot tenant's security owner** — for a data-handling or isolation
   concern.
3. **The engineering coordinator** — on a fail-closed defect. Specifically: a
   read that served material an erasure was withdrawing, a cross-tenant leak, or
   an outcome path recording results it cannot attribute.

### Halting

1. Stop the submission step in the pilot repositories. Nothing here needs to be
   drained or reconciled first — an absence of submissions is a safe state.
2. Record the reason and the instant. An outage or defect that stopped the pilot
   is part of its evidence, not an embarrassment to leave out of it.
3. Leave stored data in place unless the stop reason is a data-handling concern,
   in which case follow the erasure procedure in [ops.md](01-ops.md) rather than
   deleting rows directly.

**Do not restart by replaying a backlog accumulated during a halt without first
resolving the stop reason.** A drain is exactly what re-introduces whatever the
halt was called for.

### What a halt does not do

It does not roll anything back. There is no workflow state to unwind, and stored
outcomes remain valid observations of work that really concluded. A halted pilot
has less evidence than a completed one, not corrupted evidence.
