# 0021 — A delta is scoped by what it corrects, and an unscoped delta is a broadcast nobody approved

**Status:** Accepted 2026-08-24

## Context

ADR 0020 built the instruction channel and deferred selection on purpose:

> **Retrieval policy is deliberately not decided here.** Which delta is served to
> which agent, and on what basis, follows the channel existing. Deciding
> selection before the channel is built is how a policy gets fitted to an
> implementation that has not happened.

The channel exists. E22-T14 shipped the narrowest rule available — a delta
targets one declared digest, through a foreign key to submitted content — and
named it a placeholder rather than an answer.

**The placeholder does not survive contact with the case the channel was built
for.** An operator who notices an agent behaving on a wrong instruction wants to
correct *that instruction*, and the agents holding it may have submitted a dozen
digests that differ by a paragraph nobody cares about. Under digest-only
targeting the operator writes a dozen deltas, or writes one and reaches one
agent. Worse, an agent that edits its instructions the day after a correction is
authored silently stops receiving it — the digest moved, and nothing says the
correction was lost.

At the same time, the failure ADR 0020 spent most of its length avoiding is the
channel becoming a way to say something to an agent that the governed channels
would not say. A selection rule wide enough to be useful is a rule wide enough to
broadcast.

## Decision

### 1. A delta names a scope, and there are exactly three

`instruction_deltas.target_digest` becomes one of three scopes, stored as a
discriminator and a nullable target:

- **`digest`** — the delta corrects one submitted instruction set. What E22-T14
  shipped, unchanged, and still the narrowest thing an author can say.
- **`principal`** — the delta corrects whatever a named principal declares, at
  whatever digest. This is the case above: the correction follows the agent
  across edits to its own instructions.
- **`tenant`** — the delta applies to every declaring caller in the tenant.

**Three rather than a predicate language.** A rule engine over instruction
content is the inference ADR 0020 rejected as unfalsifiable, one level up: any
delta is consistent with many predicates, and an author who wrote one could not
say afterwards which agents it reached. These three are answerable — *this set*,
*this agent*, *everyone* — and each is a sentence somebody can be held to.

### 2. A `tenant`-scoped delta requires a second approver, and the schema enforces it

A tenant-scoped delta reaches every declaring agent, including ones whose
instructions nobody has read. That is a broadcast, and a broadcast one person can
author is the shape ADR 0020's second dissent warns about — *between the delta
being served and an evaluator reading the flag, the agent is acting on an
instruction its operator did not write* — with the blast radius multiplied by the
fleet.

So `scope = 'tenant'` requires `approved_by` distinct from `authored_by`, as a
CHECK rather than a service rule. **The scopes that reach one set or one agent do
not**: requiring two people to correct one agent's instructions is the friction
that makes a channel go unused, and the whole point of the channel is that a
correction is cheaper than letting an agent stay wrong.

**Decided against a size threshold** — "two approvers if it reaches more than N
agents" — because the count moves. A delta approved when it reached three agents
is a delta reaching three hundred a month later, with nobody having decided
anything, and the number that authorised it is no longer true.

### 3. Every applicable delta is served, and each says its own scope

Where scopes overlap, the served set is **every applicable delta**.

*Only the narrowest* was rejected: a tenant-wide correction about credential
handling and a digest-specific correction about deprecation checks are not
alternatives, and suppressing one because a narrower one exists would silently
withhold a governed instruction on the strength of a coincidence.

**Precedence is carried in the payload, not in the order, and that is a
correction to this ADR's first draft.** It said narrower deltas are served first
and that the order carries the meaning. It does not: `ordered_items` sorts every
block by receipt item id, deliberately, so that *"the order is a property of what
the items are rather than of the query plan that found them"* — which is what
makes a receipt checkable across two resolutions. An ADR claiming an ordering the
envelope reorders would be a mechanism consulted by nothing.

So each delta carries `scope`, and a reader — human or agent — weighs *"you were
told this"* against *"everyone was told this"* from the value rather than from
the position. That is the more honest place for it anyway: nothing obliges an
agent to read a list in order, which this ADR's own dissent already half-admits.

The service-level read still orders narrowest-first, and that is not decoration:
`DeclarationOutcome.contradiction_note()` joins the contradicted notes in that
order, so the record of what a resolution contradicted reads most-specific
first.

### 4. Contradiction is computed per delta, not per set

E22-T14 flags contradiction per delta and joins the notes. That is unchanged and
it matters more now: with three scopes, a tenant delta may contradict a declared
set that a principal delta does not, and a single flag on the resolution would
say the resolution contradicted something without saying which.

## Assumptions

1. **An operator can name the principal they mean.** E22-T7's roster makes this
   true; before it, `principal` scope would have asked for a UUID nobody could
   obtain.
2. **Distinct instruction sets per principal are few.** Already assumed by ADR
   0020 and now load-bearing in a second way: `principal` scope is only cheaper
   than `digest` scope if an agent's digests change slowly.
3. **A tenant has an approver who is not the author.** A single-operator
   deployment cannot author a tenant delta at all. That is intended — a
   deployment where one person can broadcast to every agent has no second
   approver to lose.

## Alternatives rejected

**Content similarity between the delta and the declared set.** Unfalsifiable in
exactly the way ADR 0020 names, and worse here: an agent would receive a
correction because two texts scored above a threshold, and nobody could say why
it reached that agent and not the next one.

**A delta targeting an agent's instructions directly, replacing them.** This is
the copy ADR 0020 refused to become. The product would hold the current
instructions for every agent and the distinction between *declared* and *stored
as truth* would be gone.

**Serving only the narrowest applicable delta.** Covered above: it withholds a
governed correction because an unrelated narrower one exists.

**No tenant scope at all.** Considered seriously, because it removes the
broadcast risk entirely. Rejected because the case it forbids is real — a
credential-handling correction that applies to every agent regardless of what
each was told — and forbidding it would push operators to author the same delta
per principal, which is the same broadcast with worse bookkeeping and no
approval.

## Consequences

`instruction_deltas` gains `scope`, `target_principal`, `approved_by` and
`approved_at`, and `target_digest` becomes nullable. The read gains two more
branches and stays one query.

**A delta can now reach an agent whose instruction content was never submitted.**
Under `principal` and `tenant` scope, a `declared_unknown` caller receives
corrections. That is a change in what those callers get and it is deliberate:
ADR 0020's dissent is that partial adoption leaves them receiving nothing
forever, and this is the part of that answerable without their content.
**Contradiction still cannot be computed for them** — the resolution says so
rather than reporting no contradictions, which is the same distinction ADR 0020's
third assumption already required.

**A cost accepted:** an operator can author a tenant delta that reaches agents
whose instructions nobody has read, with one other person's approval. The
alternative is authoring the same text per principal, which reaches the same
agents with no approval at all.

## Dissent

**Two approvers is a control that will be satisfied rather than exercised.** In a
small team the second approver is the person at the next desk, and the check
becomes a keystroke. That is true, and it is not an argument for removing it:
the value is that the row records two names, so an incident review can ask both
what they understood the delta to do. A control that produces evidence is worth
having even when the moment of approval is thin.

**A second, sharper.** Scope is presentation, not enforcement. Nothing stops an
agent weighting a tenant-wide instruction above a principal-specific one, and
this channel cannot make it do otherwise. The honest position is that it
*advises* and the agent decides — which is already true of every block in the
envelope, and is why both the scope and the contradiction flag are in the payload
the agent reads rather than only in the record an evaluator reads later.

That this ADR's first draft put precedence in the item order, on a block the
envelope reorders by design, is the case in point.
