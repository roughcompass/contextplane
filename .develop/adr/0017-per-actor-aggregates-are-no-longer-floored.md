# 0017 — The per-actor aggregate floor is removed, for every actor kind

**Status:** Accepted 2026-08-22

## Context

E20 needs to know whether a given agent's contributions are accurate: a
per-`author_actor_id` accuracy read, aggregated over adjudicated claim outcomes.

`contextplane/service/memory/learning_reads.py` forbids exactly that surface,
and says so in terms that leave no room for a narrow exception:

> **What these aggregates deliberately cannot express.** There is no per-actor
> cell and no cohort finer than the tenant. Not an omission: the only permitted
> use is measuring system quality, and individual surveillance and
> team-performance evaluation are both forbidden — so a per-team breakdown is
> not a feature this withholds pending a floor, it is a surface that must not
> exist.

It is enforced by `MIN_COHORT_ACTORS = 5` and `MIN_CELL_EVENTS = 5`, applied at
construction, with `Floors` *refusing* a looser configuration rather than
clamping it — "a deployment that configured three actors per cohort and got five
would keep believing it had configured three".

So this is not a gap E20 can build around. The thing E20 is for is the thing
that module exists to prevent.

**The repository owner decided the floor is removed.** That decision is the
input to this ADR, not its output. What this record does is state the decision
precisely, say what it costs, and keep the argument against it on the file.

## Decision

**`MIN_COHORT_ACTORS`, `MIN_CELL_EVENTS`, `Floors` and `FloorsTooLoose` are
removed from `learning_reads.py`, and per-actor aggregates become
constructible.**

**Uniformly, for every actor kind. There is no `actor_kind` branch anywhere in
the replacement code, and there is not meant to be one.** A reader looking for
the agent-only carve-out should stop looking: it does not exist. Two reasons,
and the second is the load-bearing one:

1. The decision as taken is uniform removal. A carve-out would be an
   undocumented two-tier policy of exactly the kind an ADR exists to prevent
   being left implicit.
2. **There is no reliable `actor_kind` signal to branch on.**
   `upsert_entitlement_actor` — the one function both the REST and MCP auth
   paths call — takes `(session, tenant_id, oidc_subject, display_name)` and
   nothing else. `WorkloadIdentity` looks like a fit and lives entirely inside
   ARC's autonomy-envelope subsystem, with no connection to this path. And a
   human driving Claude Code or Copilot connects over the *identical* MCP
   transport an unattended agent would, so even a transport-based guess would
   misclassify human-in-the-loop sessions as autonomous.

   A carve-out conditioned on a field nobody can populate correctly is worse
   than no carve-out: it would read as a protection while classifying people
   arbitrarily.

**The sentence quoted above is deleted, not softened.** A module whose docstring
says a surface "must not exist" while the code constructs it is worse than
either state on its own, because the next reader trusts the prose.

**The scope of the reversal is not agents.** It is per-actor aggregation, and
humans are actors. Anyone reading this as an agent-monitoring change has
misread it, which is why it is stated here rather than left to be discovered
from the absence of a branch.

**Where residual protection goes instead.** Not a floor on the aggregate —
authorization on the read, following the precedent E11-T3 already set for the
audit drill-down: `ROLE_AUDITOR`-style access plus a justification recorded in
the same transaction as the read. A floor and an authorization check answer
different questions — *can this exist* versus *can this reader see it* — and E11
already chose the second for the same class of surface.

## Assumptions

1. **An agent's accuracy is an operational fact about a service principal the
   tenant runs**, and a tenant is entitled to see it broken down as finely as it
   likes. This is the assumption the decision is easiest to defend on.
2. **A human author's accuracy becomes visible at the same granularity**, and
   that is the actual scope of the reversal rather than a side effect. If this
   assumption is unacceptable in some deployment, the answer is authorization on
   the read, not a floor — but somebody has to build that, and until they do the
   surface is open to whoever may call it.
3. **Authorization is a sufficient substitute for suppression here.** A floor
   protects against a reader who is allowed to call the endpoint; an
   authorization check protects against one who is not. These are different
   threats, and this decision accepts the first in exchange for the second.
   Assumption 3 is the one most likely to be wrong, and the dissent says why.

## Alternatives rejected

**Keep the floor for humans, exempt only agents.** The intuitive compromise, and
rejected on two grounds. The decision as taken is uniform; and there is no
`actor_kind` signal to condition it on, so the exemption would be keyed on a
field that is unreliable by construction. An access-control rule keyed on a
value nobody can populate correctly is not a narrower policy, it is an
arbitrary one.

**Keep the floor and add an agent-only bypass flag.** Same objection, plus it
leaves the removed policy half-alive — the module would still carry the
machinery and the docstring of a rule that no longer governs the case anyone
cares about, which is the half-removal this plan's supersession rule forbids.

**Leave `learning_reads.py` alone and build a second, unfloored aggregate path
for E20.** Superficially the least invasive option and the worst one available:
two aggregate surfaces with different disclosure rules, and the module docstring
still claiming per-actor cells cannot exist while a sibling module constructs
them. The floors were centralised in the first place precisely because "the same
floors, enforced uniformly" is unachievable with two definitions.

## Consequences

Per-actor accuracy becomes constructible for every actor, including humans.
That is the decision, stated plainly, and it is a real reduction in what this
system structurally prevents.

`Cell.suppressed`, the partial-total recomputation, and `build_breakdown`'s
withholding logic lose their reason to exist in this module. E20-T2 enumerates
and dispositions every consumer; this ADR deliberately does not, because the
"why" and the "where" going stale together is how a decision record stops
matching the code.

**One mechanism that is *not* affected, and must not be swept up with this.**
`signals/aggregates.py` withholds cells whose recomputation after an erasure
would disclose a subject by subtraction — the differencing defence recorded in
[ADR-0013](0013-an-explorer-that-recomputes-is-the-attack.md). That is an
orthogonal concern about `privacy_aggregates`, not about actor cardinality, and
removing it would reintroduce a disclosure this decision says nothing about. It
keeps importing `COHORT_TENANT`, `Breakdown` and `build_breakdown`; it stops
importing `Floors`.

## Dissent

**"The user decided" is a process answer to a design question, and it is the
answer this ADR ultimately rests on.** Recorded as such rather than dressed up:
nothing below changes the outcome, and it is all true anyway.

The strongest objection is that removing an actor-level floor makes a per-human
accuracy figure a **performance-management surface indistinguishable from what
this module's own docstring called forbidden**. The framing that carries the
decision — "an agent is a service principal, and its accuracy is an operational
fact" — does not survive the uniform application: a human author's accuracy is
not an operational fact about a service principal, it is a measurement of a
person's work, published at a granularity the system was explicitly built to
make impossible. Assumption 2 states this honestly, which is not the same as
answering it.

Assumption 3 is where the substitution is weakest. Authorization is a check on
*who reads*; a floor is a constraint on *what exists*. A surface that cannot be
constructed cannot be leaked by a misconfigured role, an over-broad audit
grant, a log line, a cached response, or a future endpoint that forgets to ask.
The original docstring makes exactly this argument about why suppression must
live at construction — "an unfloored aggregate that exists at all can be logged,
cached or served by the next code path somebody adds" — and this decision
overrides that reasoning without refuting it.

And the replacement is not built yet. E20-T2 removes the floor; nothing in E20
requires the authorization-plus-justification read that assumption 3 offers in
exchange. Until something does, the honest description of the state after E20-T2
is that the protection was removed and the substitute was named.

A narrower one: deleting the docstring sentence rather than softening it is
right for the code, and it also erases the only place a reader would learn the
policy ever existed. This ADR is now that place, which is what an ADR is for —
but it means the deletion is only safe as long as this file is discoverable from
the module, and nothing enforces that link.
