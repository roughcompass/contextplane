# 0009 — A renamed HTTP surface keeps its old path for one release, and retires on a date

**Status:** Accepted 2026-08-20

## Context

E18 renames published paths. E13 later removes some. Both need one decision made
before the first rename rather than during it: how long does the old path keep
working, and what actually turns it off.

The contract is 189 paths and **zero of them are marked deprecated** — not one
operation carries `deprecated: true`, and no operation carries an extension of
any kind. So there is no existing convention to follow and nothing to be
consistent with; this is the first one.

Three facts about the tree bound the answer.

**OpenAPI already models the marking, so nothing needs inventing.**
`deprecated: true` is a standard operation field, and a sunset date has an
obvious home as an extension beside it. The generated client surfaces
`deprecated` to consumers as a warning without any work on our side.

**The repository can see which operations are called, and must not decide from
it.** `usage_events.operation` holds the route *template*, per tenant, within a
retention window — so "has anyone called `GET /v1/entities` this month" is a
query that runs today. Two things stop that from being a retirement criterion.
The usage tier is deliberately lossy: its own module says it is buffered,
dropped under pressure, and deleted on a retention boundary, all of which are
fine for "is anyone using this" and disqualifying for "nobody is". And
`scripts/check_usage_boundary.py` enforces, over module imports, that no
authorization, entitlement, or audit path reads usage — because a service that
decides from a lossy table produces a confident wrong answer. Retiring a
published path is a decision. Deciding it from usage is the thing that gate
exists to prevent, and the fact that the query *would run* makes the temptation
worse rather than better.

The plan's framing of this constraint — that the repository has no instrument to
see callers — is not quite right, and the correction matters. There is an
instrument. It is the wrong instrument for this question, for reasons that are
written down in the module it belongs to.

**The product already has a deprecation vocabulary, for capabilities rather than
for its own API.** `contextplane/service/catalog/lifecycle.py` transitions a
capability through deprecation with a successor, and
`promotion_eligibility.py` treats `deprecated`, `retired` and `sunset` as
withdrawing states. That is a model for *catalog entries*, and this ADR is about
the service's own HTTP surface — but the shape is worth borrowing, because a
deprecation with no named successor is a removal announced politely.

## Decision

**An alias lives for one minor release and at least 90 days, whichever is
longer.** Both halves are needed: a release-count alone lets a fast month retire
a path a consumer has not looked at yet, and a day-count alone lets a slow
quarter keep three dead aliases alive. Ninety days is the shortest window that
contains a quarterly integration cycle, which is how the consumers this affects
actually plan work.

**It is marked in the contract, not in a changelog.**
`deprecated: true` on the operation, plus `x-sunset-on` carrying an ISO date and
`x-successor` naming the path that replaces it. A consumer reading the contract
learns three things without reading anything else: that this will go, when, and
where to go instead. A deprecation with no successor field is refused — if there
is genuinely no replacement the change is a removal, and it belongs under E13
where removals are argued rather than under a rename.

**A deprecated alias is the same operation, not a compatible one.** It routes to
the identical handler and returns the identical response. Any behavioural
difference — a different default, a narrower filter, an extra field — makes the
window a second implementation, and a second implementation is a second thing to
keep correct for ninety days at the exact moment nobody is looking at it.

**Retirement is a date, executed by a cut issue, and never a usage observation.**
The PR that adds an alias also cuts the issue that removes it, with the sunset
date in the title. The alias cannot outlive the plan that created it, because the
plan is a tracked item from the moment the alias exists rather than a note
somebody meant to write.

Retirement does **not** rest on observed zero usage. Usage is lossy, so zero
observed calls and zero calls are different facts; and reading usage to decide
this would put an authorization-adjacent decision on a table the boundary gate
forbids exactly that use of. An operator who wants evidence before the date may
look at usage as *input to a human judgement* — that is what it is for — and the
date holds regardless.

**A path template variable rename is not a rename under this ADR.** Path
parameters are positional on the wire, so `/v1/capabilities/{capability_id}` and
`/v1/capabilities/{entity_id}` are the same URL. What changes is generated client
parameter names, which is a source-compatibility break for consumers who
regenerate and a no-op for everyone else. No alias, no window, no sunset — and
E18-T2 proceeds without them.

## Assumptions

1. **Consumers regenerate their clients within one release cycle.** If they pin a
   generated client indefinitely, the deprecation marking reaches nobody and the
   window measures nothing. Nothing in this repository can check that.
2. **Ninety days is long enough because integration work is planned quarterly.**
   Asserted from how consumers usually work, not from anything measured here.
   The condition for revisiting is the first consumer who asks for longer with a
   reason.
3. **There will be few enough aliases that a per-alias issue is manageable.** If
   an epic ever produces twenty at once, the cut-an-issue rule becomes twenty
   issues and somebody will batch them, which is the point at which the mechanism
   needs rethinking rather than bending.
4. **`x-sunset-on` and `x-successor` stay unenforced by a gate.** They are
   conventions until something checks them. E18-T3 builds a tag gate and could be
   extended to check these too; this ADR does not require it, and until it exists
   a mis-spelled extension is a comment.

## Alternatives rejected

**Retire on observed zero usage.** The attractive option, and the one the query
makes look easy. Rejected because usage is lossy by design — a quiet month and a
dropped buffer are indistinguishable in that table — and because it would make a
retirement decision from data the boundary gate forbids deciding from. The gate
would not catch it, since it checks imports rather than intent, which makes
writing the rule down here more important rather than less.

**Retire on a fixed date with no minimum release count.** Simpler, and it lets a
fast release train retire a path that shipped six weeks ago. Rejected: the window
exists to give consumers a cycle to notice, and a cycle is a release, not a
duration.

**Keep aliases indefinitely.** Costs nothing today and is how a 189-path contract
becomes a 250-path one where a third of the surface is scar tissue. The whole
reason E18 exists is that naming discipline slipped while the code stayed
correct; permanent aliases are that failure with a policy attached.

**HTTP redirects instead of aliases.** A `308` from old to new is standard and
self-documenting. Rejected because the contract is what consumers generate from,
and a redirect is invisible in it: a generated client would carry the old
operation, follow the redirect at runtime, and never surface a deprecation. The
alias is worse plumbing and better communication, and communication is the
constraint here.

**A shorter window for surfaces we believe nobody uses.** Rejected as the same
error as usage-based retirement wearing a different hat. "We believe nobody uses
it" is exactly the claim the lossy table cannot support.

## Consequences

Every rename now costs two PRs and an issue: one to add the alias with its
sunset, one to remove it after. That is the price of a contract consumers can
plan against, and it is paid by the team making the change rather than the
consumers absorbing it.

The contract gains fields nothing validates. `x-sunset-on` is a date in a string
until a gate checks it, and this ADR knowingly ships that gap rather than
expanding E18-T3's scope to cover it.

An alias adds a routing entry that must be excluded from the endpoint-count
target E13 measures against, or the epic that removes surfaces will appear to add
them. E13's counting rule has to say whether a deprecated alias counts, and this
ADR does not answer that — it is E13's to decide, and it now has a defined thing
to decide about.

Retirement dates land in the calendar whether or not anyone is ready. That is the
intended property: a date somebody has to actively defer is a decision, and an
alias that quietly persists is not.

## Dissent

*On refusing usage as evidence.* The strongest objection is that this ADR
identifies a real instrument, concedes the query runs, and then declines to use
it — leaving retirement to a date chosen from an assumption about quarterly
planning that nobody measured either. A reviewer could reasonably say that lossy
evidence beats no evidence, and that the boundary gate is about *services*
deciding at request time rather than about a human reading a dashboard before
filing a removal PR. That reading is defensible. The decision above allows
exactly that human use and refuses only to make the date conditional on it,
because a criterion that can be satisfied by a dropped buffer is worse than a
date.

*On ninety days.* It is a number with no measurement behind it, in a repository
that has spent considerable effort refusing numbers with no measurement behind
them. The defence is that a window has to be *some* length before the first
alias, and that the alternative on offer was a usage observation that is worse.
The honest characterisation is a placeholder with a stated condition for change,
not a finding.

*On aliases over redirects.* Someone who cares more about runtime correctness
than about generated-client ergonomics would take the `308`, and would point out
that this ADR is optimising for a consumer who reads OpenAPI over one who reads
HTTP. Both consumers exist. The decision picks the one whose tooling this project
already generates for.
