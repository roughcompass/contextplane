# 0010 — Session events stay out of the embedding path, and the drain stays asynchronous

**Status:** Accepted 2026-08-21

## Context

E2's hot write path asks for "cheap synchronous embedding" on
`POST /v1/memory/sessions/{session_id}/events`. Three facts, checked against the
tree rather than assumed, make that sentence answer a question nobody asked.

**Session events are not an embedding target.** `contextplane/embedding/targets.py`
declares `EMBEDDING_TARGETS = {fact, claim}`. Nothing embeds a session event
today, synchronously or otherwise. So E2 is not asking to move an existing
embedding onto the hot path; it is asking to introduce a new target *and*
implement it in the one shape that contradicts how both existing targets work.

**The shipped path is asynchronous, and hardened by an outage.**
`service/retrieval/embedding_drain.py` consumes `embedding_outbox` every
`outbox_poll_interval_s` (5s) and writes `embeddings`, which is already
hash-partitioned. Its own instrumentation records why it looks the way it does:
alongside queue depth it carries `embedding_outbox_oldest_pending_seconds`,
because "depth alone cannot distinguish a queue that is short because it is
keeping up from one that is short because nothing is being enqueued — and the
second is the failure that hid an empty claim index for a whole phase." There is
also a coverage gauge for how much of what should be indexed actually is. This
is not an unconsidered default; it is a design that has already been debugged in
production shape.

**The two-call memory loop does not read through embeddings.**
`session_events.py` replays by `seq` and `context/resume.py` orders by
`sequence`. A caller that writes a turn and immediately replays the session sees
its own write because the read is ordered by a monotonic column in the same
table, not because a vector exists. Semantic retrieval over facts and claims is
a different read path with a different target set.

So the only thing synchronous embedding could buy — a just-written row being
semantically retrievable immediately — is a property no shipped reader of
session events wants, for a target that does not exist.

Against that, what it would cost is concrete. A model call inside the request
puts an unbounded external dependency in the latency budget of the same p99
E2-T6 wants published. `contextplane/embedding/` ships four providers including
`remote_http`, so "cheap" is a property of the deployment's configuration, not
of the code. And a provider outage forces a choice between refusing writes —
availability loss on a memory write path — and write-then-enqueue, which is the
asynchronous design with a failed attempt in front of it.

## Decision

**Session events are not embedded, and this ADR does not add them as a target.**
The requirement that would justify it — semantic retrieval across sessions —
belongs to whichever epic actually needs it, with its own recall evidence. Adding
a target speculatively means paying for embedding every conversational turn in
every tenant against a benefit nobody has stated.

**If session events later become a target, they go through `embedding_outbox`
like the other two.** Not because asynchronous is always right, but because two
writers into one `embeddings` table produce rows a reader cannot tell apart, and
"is this vector missing because it is queued or because it will never exist" is
precisely the question the coverage gauge was added to answer. A second path
would break the one instrument that makes the first trustworthy.

**E2's body is amended rather than implemented.** The clause "cheap synchronous
embedding" is struck from the hot-path list. Where the tree is architecturally
better than the plan, the plan changes; this document is the record of that, and
E2's task list carries the amendment.

**Read-after-write for the two-call loop is a `seq` guarantee, not an index
guarantee.** That is already true and now stated, so a future change to the
replay path knows it is load-bearing: if replay ever moves to a semantic arm, it
inherits a staleness window bounded by the poll interval, and that is a
different ADR.

**If a future target needs bounded read-after-write, the answer is a fail-closed
staleness window, not a synchronous call.** The shape already exists in ARC
source status: a stored deadline, every consumer failing closed once it passes,
and a refresher treated as an optimisation that cannot extend the bound. Worst
case is then the window, not the window plus worker lag plus however long the
worker has been dead.

## Assumptions

1. **No shipped reader of session events wants semantic retrieval.** Checked:
   replay is by `seq`, resume by `sequence`, and `EMBEDDING_TARGETS` excludes
   them. If E7's loop turns out to want cross-session semantic recall, that is a
   new requirement and reopens this.
2. **The 5s poll interval is acceptable staleness for facts and claims.** It is
   the shipped value and nothing in this decision changes it. A target with a
   tighter requirement would need the staleness-window shape above.
3. **The coverage gauge stays the trustworthy instrument.** It is only
   trustworthy while one writer populates `embeddings`. Anything that adds a
   second writer invalidates this ADR's second clause and should say so.

## Alternatives rejected

**Synchronous embedding as specified.** Rejected on the three findings above:
the target does not exist, no reader wants the property, and the cost lands on a
p99 the same epic wants published. It also does not retire the drain —
re-embedding after a model change is a backfill by construction — so it adds a
second writer rather than replacing one.

**Write synchronously, fall back to the outbox on failure.** This is the design
that looks like a compromise and is not: it has the latency of the synchronous
path, the complexity of both, and the same two-writers problem, while the
fallback branch is exercised only during incidents, which is when it is least
safe to be running rarely-tested code.

**Add session events as an async target now, ahead of a stated need.** Cheaper
than synchronous and still speculative. Embedding every conversational turn is a
per-tenant cost with no named reader, and the honest time to pay it is when
something reads it.

## Consequences

E2-T4 is satisfied by this document rather than by code, and E2-T5 and E2-T6 lose
a dependency that would have added an external call to the path they measure.

E2's hot-path list is now: authority (done, E2-T1), PII scan (already shipped),
idempotency (already shipped), provenance completeness (E2-T2), one partitioned
insert (E2-T3). No model call.

The gap this leaves is honest and worth naming: there is no cross-session
semantic recall over conversational turns, and this ADR declines to build one
speculatively rather than claiming the requirement does not exist.

## Dissent

The strongest argument for the rejected option is that an agent's own turns are
the most obviously retrievable thing in the system, and a design that cannot
answer "what did we say about retries last week" across sessions is missing
something a user will ask for on day one. That is probably true. The response is
not that the need is imaginary but that it is *unstated*: no epic in this plan
names it, no reader implements it, and building it as a side effect of a clause
about latency is how a feature arrives without anyone deciding its recall
target, its retention interaction, or its cost per tenant.

A second dissent, narrower: "cheap synchronous embedding" may have meant a local
model where the call is a few milliseconds, in which case the p99 objection
weakens considerably. `local_onnx` and `local_torch` both ship. But the provider
is deployment configuration, and a hot path whose latency profile depends on
which of four providers an operator selected is not one that can publish a p99 —
which is the objection the deployment-dependence was supposed to answer.
