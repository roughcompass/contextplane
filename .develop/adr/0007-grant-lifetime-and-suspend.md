# 0007 — A grant projection is not cached, so suspension needs no push

**Status:** Accepted 2026-08-19

## Context

E1 says a session `ProvenanceGrant` is a runtime projection of an envelope, and
promises "instant suspend (status flip, push-invalidated, sub-second SLO)". Two
of those three words describe machinery this service does not have.

**There is no server-to-client push, of any kind, anywhere.** No WebSocket route
— the word appears in no production module. No broker, queue, or pub/sub: no
Redis, Celery, Kafka, RabbitMQ, NATS in the dependency list, and no Postgres
`LISTEN`/`NOTIFY`. The only server→client stream in the service is the MCP SSE
endpoint, and `contextplane/metrics.py` treats it as the single member of a
deliberately closed set. Notifications are strictly pull — a cursor-paginated
read over a table, carrying a five-value capability-lifecycle vocabulary with
nothing authority-shaped in it. Outbound webhooks push to a tenant-registered
HTTP endpoint, not to an agent.

The MCP SDK *can* send server-initiated messages — `send_log_message`,
`send_resource_updated`, `send_ping` all exist on the pinned version. Nothing in
this repository ever calls one, and nothing holds a handle to a live session: the
only per-connection state is `PreflightRegistry`, keyed by an opaque connection
id, whose record carries no stream, session or transport reference. Push is not
one feature away; it is a registry, a fan-out primitive, and a delivery guarantee
away.

Against that, the useful finding is that **this codebase has already decided the
same question twice, and both times the answer was to not cache the verdict**:

- `contextplane/sharing/grants.py` states it outright — a grant verdict must not
  be cached, revocation takes effect at the next decision because the row is
  re-read every time, and revocation is therefore "not a state anybody has to
  notice".
- `contextplane/sharing/derivatives.py` handles the case where something *must*
  be precomputed: the grant set's content digest is part of the cache key, so a
  revocation makes derivatives unreachable immediately, and a stale read fails
  closed rather than recomputing. Invalidation by key, not by sweep.

And where a projection genuinely has a lifetime, the shipped precedent is ARC
source-approval status: a stored `next_check_at` with a 300-second freshness
window, enforced **fail-closed at read time by every consumer**, refreshed by a
60-second worker. Its module records the property that matters — once the
deadline arrives the read starts failing on its own, on every caller,
independently of whether the worker ran. Worst-case stale authority equals the
TTL, not the TTL plus worker lag.

Three numbers bound anything decided here:

- **900 seconds.** A token whose `exp - iat` exceeds 900s is refused. An MCP
  connection captures its bearer token once at SSE connect into a ContextVar and
  never refreshes it, and every tool call re-validates that frozen JWT. So a
  connection's authority already has a hard 15-minute ceiling.
- **300 seconds.** The repository's established freshness constant for
  short-lived security state, deliberately propagated across seven modules —
  source status, approval challenges, enrollment, continuation, attestation,
  OIDC.
- **Two replicas.** The default deployment runs two API pods with an in-process
  APScheduler in each. Any in-memory projection — the preflight registry, the
  entitlement cache, the OIDC cache — exists once per replica, and there is no
  primitive in this service for one replica to invalidate another's.

The entitlement cache is the live counter-example and the honest baseline: a
per-process LRU with a TTL derived from the JWT `exp` and a 30-second floor,
which means **an entitlement withdrawn upstream keeps working here for up to 900
seconds today**. Whatever this ADR promises has to be at least as good as that,
and cannot be much better without changing it.

## Decision

**A grant projection is not cached, and therefore has no TTL.** It is derived at
the decision point from the envelope row, on every authority decision, following
the rule `contextplane/sharing/grants.py` already states for the one grant table
that ships. "How long does a grant projection live" is the wrong question; the
answer this ADR gives is that it does not live, it is computed.

**Suspension takes effect at the next authority decision.** That is the
re-validation trigger, and it is the whole mechanism. A status flip on the
envelope row is visible to the next decision made by any replica, because no
replica is holding a copy.

**The promised SLO is stated as a bound on operations, not on wall-clock.** A
suspended envelope authorises no operation that begins after the flip commits.
It is deliberately not "sub-second": a wall-clock SLO would be a claim about how
long an in-flight operation may run, which this service does not bound, and it
would be unobservable on the shipped metric anyway — the latency histogram's
buckets top out at 10 seconds, so any number outside that range cannot be
measured on what exists. The one published numeric SLO in the repository is
webhook fan-out at p95 < 30s, and it is asserted by a perf test; a revocation SLO
with no test and no bucket would be a sentence, not a promise.

**Where a projection is unavoidable, it carries a deadline enforced at read time,
not a sweeper.** The ARC source-status shape: a stored expiry, every consumer
failing closed once it passes, and any refresher treated as an optimisation that
cannot extend the bound. A projection whose safety depends on a worker having run
is one whose worst case is TTL plus worker lag plus however long the worker has
been dead.

**When a deadline is needed, it is 300 seconds.** Not a new number. The
repository has propagated 300s across seven security-state modules deliberately,
and a new constant would need to argue why authority is different from
attestation and enrollment, which it is not.

**Push is specified here as explicit new scope, and is barred from ever being the
enforcement mechanism.** If sub-second suspension is later required, what it
costs is:

1. A registry mapping principal → live MCP sessions, which does not exist;
   `PreflightRecord` deliberately holds no transport reference.
2. A cross-replica fan-out primitive, which does not exist and cannot be added
   without a new dependency — a broker, or Postgres `LISTEN`/`NOTIFY`, neither of
   which this service uses today.
3. A delivery guarantee. MCP notifications are at-most-once over a stream that
   can drop; a compliance claim resting on "we sent a message" is not one that
   survives an auditor asking what happens when it is not received.

Because of (3), push may only ever be an **optimisation over** the read-time
check, never a replacement for it. A design where a missed push means unbounded
authority is strictly worse than polling, and it is the shape that "instant
suspend, push-invalidated" invites.

**Two shipped defects are recorded here and are not fixed by this ADR:**

- The ARC preflight record has a hard-coded one-hour lifetime while its own
  comment says expiry comes from the credential and the preflight must not
  outlive it. One hour is four times the maximum token TTL the service accepts.
  It is not the weak link — the record is re-validated on every ARC tool call
  against a fresh credential fingerprint, tenant selector, and restriction
  digest, and any mismatch both refuses and deletes it — but the comment and the
  constant disagree, and one of them is wrong.
- `assertion_provenance.freshness_state` and `expires_at` are columns nothing
  transitions and nothing enforces: every writer inserts `fresh`, no sweeper
  touches them, and no read checks the expiry. This is the anti-pattern the
  decision above exists to avoid — an expiry that is reported rather than
  enforced tells a reader the opposite of the truth.

## Assumptions

1. **Reading the envelope at each decision is affordable.** Nothing measures it.
   The entitlement resolver caches precisely because its upstream is a network
   call; an envelope is a local row, and the closest analogue —
   `cross_org_grants`, re-read per decision by design — does not cache. If
   measurement says otherwise, the fallback is the source-status shape with a
   300-second read-time deadline, not an unbounded in-memory cache.
2. **The envelope lives in Postgres and the decision point has a session.** If
   envelopes end up in a store the decision point cannot reach synchronously,
   the no-cache decision does not survive.
3. **900 seconds of stale entitlement is acceptable, or is separately fixed.**
   This ADR makes envelope suspension immediate while the entitlement it sits
   beside can be up to 15 minutes stale. Promising better than the weakest link
   in the same authority chain is a promise about the wrong link.
4. **An in-flight operation may complete.** The bound is on operations that
   begin after the flip. A long-running tool call that started before it is not
   interrupted, because nothing in this service can interrupt one.

## Alternatives rejected

**A TTL'd in-memory grant projection with a sweeper.** The obvious reading of
"projection". Rejected on three counts: it exists once per replica with no
cross-replica invalidation, so two pods disagree about whether a principal is
suspended; its worst case is TTL plus worker lag; and it makes revocation "a
state somebody has to notice", which is the exact phrasing `sharing/grants.py`
uses for what it refused to build.

**Push invalidation over MCP SSE, as the plan proposed.** Rejected as the
enforcement mechanism for the delivery-guarantee reason above, and rejected as
scope because it needs a session registry and a fan-out primitive that would be
the service's first broker dependency. Kept on the record as a future
optimisation with its cost priced.

**A `suspended_at` column checked by a background sweeper that revokes
projections.** Rejected: it is the `assertion_provenance` anti-pattern with a
worker attached. The read-time check is what makes the guarantee; adding a
sweeper on top of a read-time check is fine, adding one instead of it is not.

**Deriving the grant's expiry from the evidence, as
`materialize_entitlement_grant` does.** Genuinely the nicest existing design in
the tree — the grant's `expires_at` is the moment the evidence stops counting,
with the rationale that two windows would drift. Rejected because it is dead
code: no module under `contextplane/` outside its own file calls it. Following an
unexercised design is following an idea, not a precedent, and the shipped
per-actor grant beside it is unbounded.

**Publishing a sub-second numeric SLO anyway.** Rejected because it would be
unobservable on the shipped histogram and untested, and because the honest
statement — no operation begins under a suspended envelope — is a stronger
guarantee than a latency number, not a weaker one.

## Consequences

Suspension is immediate in the only sense that can be defended, and the mechanism
is that there is no mechanism: nothing to invalidate, nothing to sweep, nothing
that can be stale.

Every authority decision costs a row read. If that turns out to matter, the
fallback is named above and it degrades to a 300-second bound rather than to an
unbounded cache.

The service still cannot tell an agent it has been suspended. The agent finds
out by being refused. For a bank that is arguably the right shape — the refusal
is the enforcement and the notification is a courtesy — but it means an agent
mid-plan discovers a policy change as an error, and nothing in the product
softens that.

Two replicas can now disagree about nothing, because neither holds a copy. That
property is worth more than it sounds: it is what makes the guarantee independent
of `replicaCount`.

A number is now promised — 300 seconds, if a deadline is ever needed — that
matches seven other modules. The next person to add short-lived security state
has one number to reuse rather than a choice to make.

Two defects are on the record and unfixed: the preflight lifetime that contradicts
its own comment, and the freshness columns nothing enforces.

## Dissent

*On refusing to promise a wall-clock SLO.* A regulator asking "how quickly can
you cut off a compromised agent" will not accept "at its next decision" as an
answer, because an agent that makes no further decisions has not been cut off
from anything, and one that is mid-operation is still acting. The counter is that
a wall-clock number this service cannot measure or interrupt would be worse than
no number. Both are true. The gap is real, and closing it means the ability to
abort an in-flight operation, which nothing here has.

*On not building push.* One view is that a governed-agent product whose control
plane cannot reach its agents is missing a limb, that SSE is already open and the
SDK already has the methods, and that the ADR has priced the fully-general
solution when a best-effort nudge on the connection the agent is already holding
would cost very little. That is fair on cost. It is rejected on discipline rather
than expense: the moment a nudge exists, the read-time check starts looking
redundant, and the first person to remove it will be right about the common case
and wrong about the one that matters.

*On the 900-second entitlement staleness.* Making envelope suspension immediate
while leaving entitlements up to 15 minutes stale can be read as decorating the
easy half of the authority chain. The strongest version of that objection is that
the entitlement cache is the thing to fix, and this ADR should have said so as a
decision rather than as an assumption.
