# 0020 — The instruction set is declared, never stored as truth, and the delta is context

**Status:** Accepted 2026-08-24

## Context

The constraint, in the user's words:

> prompts can be registered in the contextplane and they can originate within
> the agent instructions (e.g., CLAUDE.md). this system should not duplicate
> what's already available to agent, but it should be able to see what the agent
> used and should be able to provide new or improved or contradictory
> instructions as context.

Three requirements, and the third changes the architecture.

**Contextplane cannot see what an agent was told.** `ContextResolveRequest` has
nine properties — `query`, `subject_entity_id`, `intent_ids`,
`workspace_reference`, `workspace_term`, `arc_receipt_id`,
`lifecycle_references`, `limit`, `max_age_s` — and not one carries the caller's
instruction set. Every judgement the product can currently make about a
resolution is made without knowing what the agent was told to do with it, which
is a substantial part of why *"was this context right?"* is hard to answer. A
resolution that served a correct claim to an agent instructed to ignore it and a
resolution that served the same claim to an agent instructed to act on it are
indistinguishable in the record.

**And the product must not become the second copy.** An agent's base
instructions live with the agent — a `CLAUDE.md`, a system prompt, an
integration's configuration. Copying them in creates two answers to one
question, which this plan's supersession rule exists to prevent, and the copy
is stale from the first edit nobody mirrored.

The envelope today has four blocks: `canonical`, `arc`, `observed_claims`,
`workspace` (`context/schemas/envelope.py:59`).

## Decision

### 1. Declaration is a per-resolve digest, with content submitted once per digest

The caller sends a digest of its instruction set on every resolve. If the
service has not seen that digest, the caller submits the content once, keyed by
the digest, and every later resolve carries the digest alone.

**Decided against a measured cost rather than an assumed one.** The alternative
— per-session attestation — was proposed to avoid a round trip. The round trip
it avoids is: one submission per *distinct instruction set*, not per resolve.
An agent's `CLAUDE.md` changes on the order of days; a session issues many
resolves. Against E7-T4's measured two-call loop of **27 ms for two MCP calls**
on a local database, one call is roughly 13 ms, so the submission costs about
13 ms **once per distinct set** and amortises to approximately zero across a
session. Per-session attestation would save that 13 ms and lose the property
that makes declaration worth having: a session that changed its instructions
mid-run would still be attested under the old ones, and the record would say
something false rather than something incomplete.

Digest-only resolves carry a fixed ~64 bytes and no additional round trip at
all, which is the cost that recurs and is therefore the one worth minimising.

### 2. An unknown digest is accepted and recorded, never refused

Refusing makes a first-run resolve fail — the agent has no way to know the
service has not seen its set until it is told, and failing the resolve punishes
the caller for a state the service is in. Warning is worse than either: a
warning nobody reads is an unrecorded acceptance with extra text.

So an unknown digest resolves normally, the resolution records that the
instruction set was **declared but not submitted**, and the surfaces built on
this distinguish three states, never two:

- **no instructions declared** — the caller sent no digest;
- **declared, content unknown** — a digest arrived and its content was never
  submitted;
- **declared and known** — the delta can be computed against it.

Collapsing the first two is what would make partial adoption invisible, and the
dissent below is about exactly that.

### 3. The fifth block is suppressible by the same rules as the other four

An unsuppressible block is a channel around every floor the product has. If a
delta could reach an agent when the PII scan, the visibility rules and the
autonomy envelope would have withheld everything else in the envelope, then the
instruction channel is the way to say to an agent what the governed channels
refuse to say. It obeys the same rules, and this is recorded rather than left
implicit precisely because the tempting argument — *instructions are not data*
— is the argument that would create the hole.

### The part that is not a fork, recorded anyway

**The delta returns through `/v1/context/resolve` as a block, never a side
channel.** It therefore inherits provenance, trust class, the receipt,
suppression, and evaluability, all of which already exist and none of which
would apply to a dedicated endpoint.

The counterfactual, stated plainly: an instruction delivered outside the
envelope is **the one input to agent behaviour the governance machinery cannot
see**. Every other input — claims, canonical facts, ARC artifacts, workspace
material — arrives with a receipt an evaluator can read back. An instruction
arriving beside them with none would be the single highest-leverage input to
what the agent does and the only one with no record.

### Contradiction is a governance event, and is served flagged

When a delta contradicts the declared base set, the resolution **records that a
contradiction occurred and what was contradicted**, and the delta **is served,
flagged**. The three options were served silently, withheld pending review, and
served-and-flagged.

*Served silently* is not available and is named here so it cannot be chosen as
an implementation detail later: an instruction that overrides what an operator
told their agent, without saying so, is the product changing agent behaviour
behind the operator's back.

*Withheld pending review* was rejected because the contradicting delta is
usually the valuable one — the whole point of the channel is to say "your
instructions are wrong about this" — and a channel that withholds its most
useful message until a human notices is a channel nobody comes to rely on.

*Served and flagged* keeps the correction useful and the record honest. The
agent receives it; the resolution says a contradiction was served and against
what; the evaluator can see it and disagree.

## Assumptions

1. **An integration can compute a digest of its own instruction set.** It has
   the bytes; hashing them is not a new capability. An integration that cannot
   is one that does not know what it is instructing its agent with.
2. **Distinct instruction sets are few relative to resolves.** This is what
   makes the one-time submission amortise. If an integration generated a fresh
   set per request the cost model inverts, and the surfaces would show it — the
   count of distinct digests per session is observable.
3. **A contradiction can be detected against a declared set.** This rests on the
   base set being submitted, not merely digested; for a `declared, content
   unknown` set, contradiction cannot be computed and the surfaces say so rather
   than reporting no contradictions.

## Alternatives rejected

**Storing the agent's base instructions in Contextplane as the store of
record.** Two copies, one stale, and the product becomes responsible for
material it does not own. It also inverts the user's constraint directly.

**A dedicated instruction endpoint outside `/v1/context/resolve`.** This loses
the receipt, which is the whole argument. It also loses suppression, provenance
and trust class — four properties that would each have to be rebuilt on the new
surface, and would drift from the originals the first time one changed.

**Inferring the instruction set from the agent's behaviour.** Unfalsifiable:
any behaviour is consistent with many instruction sets, and a product that
guessed would be scoring agents against a set it invented.

**Making declaration mandatory.** Rejected because it turns adoption into a
breaking change for every existing integration, and because a mandatory field
callers do not understand gets filled with a constant — at which point the
record says every agent shares one instruction set, which is worse than saying
nothing.

## Consequences

`ContextResolveRequest` gains a tenth property and the envelope gains a fifth
block. Both are additive; a caller that sends no digest resolves exactly as it
does today, and its resolutions record `no instructions declared`.

The evaluation surface can, for the first time, ask *what was this agent told,
and did what we served fit it?* That question is E22-T15's subject and it is
unanswerable without this channel.

**A cost accepted:** partial adoption. Some integrations will never declare, and
their resolutions will carry `no instructions declared` forever. That is legible
rather than silent, which is the most this decision can buy.

**Retrieval policy is deliberately not decided here.** Which delta is served to
which agent, and on what basis, follows the channel existing. Deciding selection
before the channel is built is how a policy gets fitted to an implementation
that has not happened.

## Dissent

**Declaration puts a cost on every integrator for a benefit only the evaluator
sees.** The agent gains nothing at the moment it declares — it does more work,
sends more bytes, and receives the same envelope it would have received anyway.
The benefit accrues to a human looking at a screen later. Asymmetric costs like
that are how optional protocol features end up with single-digit adoption, and
an evaluation surface built on a signal that arrives 8% of the time is a
surface that reports on 8% of the fleet while looking like it reports on all of
it.

This is answered by a requirement rather than by optimism: **every evaluation
surface distinguishes "no instructions declared" from "instructions declared and
empty", and shows the declared fraction.** A judgement built on partial adoption
is then visibly partial. The objection is not that the mechanism is wrong; it is
that a mechanism this shape degrades quietly, and the answer is to make the
degradation loud.

**A second, on the contradiction path.** Serving a flagged contradiction means
the product can change what an agent does while the operator's own instructions
say otherwise, and the flag is read by a human who may look days later. The
counter is that the alternative — withholding — makes the channel useless for
its main purpose. The residual risk is accepted and named: between the delta
being served and an evaluator reading the flag, the agent is acting on an
instruction its operator did not write.
