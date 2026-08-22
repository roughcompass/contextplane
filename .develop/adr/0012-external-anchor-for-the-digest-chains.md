# 0012 — Anchoring the digest chains externally, and what that is not

**Status:** Accepted 2026-08-22

## Context

E6 asks for "externally anchored tamper-evidence (bounded exposure window —
never called non-repudiation)". Grounding it found the internal half already
built and the external half genuinely absent, which changes what this decision is
about.

**Two hash chains ship, and both are real.** `arc_receipt_events` chains each
event's digest over its predecessor's, appending in O(1) by locking a head row in
`arc_receipt_event_heads` rather than re-reading the chain. `arc_operational_event_heads`
does the same for a revision's operational events. `tests/integration/test_arc_digest_chain.py`
verifies the `S → R → A` chain, and `audit/actions.py` already carries a terminal
action for a receipt whose chain no longer verifies.

**What a chain alone proves, precisely.** That no *subset* of rows was altered
without also altering everything after it. It is a consistency property over the
data as stored.

**What it cannot prove.** Anything against the party that holds the storage. An
operator who can rewrite `arc_receipt_events` can recompute every subsequent
digest and rewrite the head, and the verifier — which reads the same database —
will agree. The chain and its checker fail together, and they fail silently,
because a consistently rewritten chain is indistinguishable from an untouched
one.

That gap is the entire subject of this ADR. It is not a weakness in the chain; it
is the boundary of what any purely internal mechanism can offer.

## Decision

**Publish the chain heads periodically to a store this deployment cannot
rewrite.** A digest over the current heads, at a fixed cadence, somewhere outside
the database. Verification then has two halves: the chain still verifies
internally, *and* its head at time T matches what was published at time T.

**The cadence is a published number, because it is the guarantee.** Anchoring
every N minutes means tampering before the last anchor is detectable and
tampering after it is not. A deployment states N; "we anchor periodically" is not
a claim anyone can act on.

**This is never called non-repudiation, and the reason is written here so a
marketing page's author finds it.** Non-repudiation means a party cannot deny
having done something — it requires an identity bound to an act by a signature
that party alone could produce. An anchor identifies no signer. It says the
record has not changed since it was published, and nothing about who wrote it or
whether they agree it is theirs. Calling it non-repudiation would sell a property
this mechanism does not have and cannot acquire by tightening the cadence.

The honest phrase is **bounded-exposure tamper-evidence**, and the bound is N.

**What is anchored is a digest of the heads, never content.** Content in an
external store is content this deployment no longer controls the retention of,
which collides with crypto-shredding (E6-T3) and with every erasure obligation.
A digest is unaffected by shredding — destroying a key makes content unreadable,
and the digest of a head row was never the content.

## Assumptions

1. **The external store is harder to rewrite than this database.** Not
   impossible to rewrite — harder, and by a different party. If the anchor lands
   somewhere the same operator controls, this buys nothing and should not be
   claimed. Whichever store is chosen, that property is the acceptance criterion.
2. **A missed anchor is noticed.** An anchoring job that silently stops turns the
   exposure window into "since whenever it last ran", which is the failure the
   extraction drain's oldest-pending gauge exists to catch in its own domain.
   Anchor age is a gauge, not a log line.
3. **Verification is somebody's job on a schedule.** An anchor nobody checks
   detects nothing; it only makes detection *possible*. This ADR does not decide
   who checks or how often, and that is a real gap rather than an oversight —
   see the dissent.

## Alternatives rejected

**Sign the chain head with a deployment key.** This looks like the stronger
option and is weaker for the threat that matters. A key the deployment holds is a
key the deployment can re-sign a rewritten chain with, so it defends against
everyone except the party the anchor exists to constrain. It also invites exactly
the non-repudiation language this decision refuses, because a signature implies a
signer.

**Anchor every event rather than periodically.** Removes the exposure window and
replaces it with a per-write dependency on an external service — on the receipt
path, which `resolve.py` already refuses to make best-effort. A write path that
fails when a third party is slow is a worse trade than a stated window.

**Do nothing and rely on the audit log.** The audit log lives in the same
database. It is subject to the identical rewrite, and appealing to it answers the
question by assuming it.

## Consequences

Tamper-evidence becomes a claim with a number attached, which is smaller than
what "tamper-evident" suggests unqualified — and stating the number is the point.

Operationally this adds a scheduled job, an external dependency that is *not* on
the request path, and a verification procedure somebody has to run. The last is
the part most likely to be skipped, and the one that makes the rest worth having.

## Dissent

The strongest objection is that this is ceremony unless somebody verifies, and
nobody is assigned to. An anchor written every N minutes and never checked is a
cost with no benefit — worse, it is a cost that *reads* as a benefit, and a
deployment can point at it while having exactly the tamper-evidence it had
before. That is a real risk and this ADR does not close it; it decides the
mechanism and leaves the practice to whoever operates it, which is the weakest
part of the decision.

A narrower one: the exposure window is stated as a property of the cadence, but
the real window is the cadence *plus* the time to notice a failed anchoring job.
Assumption 2 asks for a gauge; until that exists, the published N is optimistic
by an unbounded amount.

A third, on scope: two chains ship and this anchors both heads, but nothing
establishes that those two are the only tamper-evidence surfaces worth anchoring.
`audit_log` is not chained at all. Whether that is a gap or a deliberate scoping
was not decided here and should be, before somebody assumes the audit log carries
the same guarantee as a receipt chain.
