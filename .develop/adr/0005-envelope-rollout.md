# 0005 — Envelope enforcement graduates by offender scan, not by mode flag

**Status:** Accepted 2026-08-19

## Context

The Autonomy Envelope is the authority object E1 introduces: an agent principal
acts within an envelope, and "no envelope, no authority" is the rule that makes
it worth having. Landing that rule as written turns every principal that has no
envelope — which, on the day it lands, is all of them — into a refusal. So the
rule needs a way to arrive that is not a flag day, and this record decides what
that way is.

The plan proposed mirroring "the `advisory | warn | block` pattern PII policy and
progression definitions already use". That premise does not survive contact with
the tree. **Four enforcement vocabularies ship today and none of them agrees with
another:**

- `advisory | warn | block`, a severity ordering, in
  `contextplane/security/pii_scanner.py` (`_POLICY_SEVERITY`). Stored per tenant
  in two tables with CHECK constraints; the tenant default is hard-coded to
  `advisory` at the decision point in `contextplane/security/pii_guard.py`.
- A bare `is_advisory` boolean on progression definitions
  (`contextplane/storage/models.py`), with two outcomes and no third state.
- `is_advisory` plus a 30-day `advisory_until` window, in
  `contextplane/service/catalog/schema.py` — and only for edge-property schemas.
  The column exists on `edge_property_schemas` and not on `entity_type_schemas`,
  so the repo did not apply its own pattern to both registries.
- `mandatory | advisory | unbound`, in `contextplane/entities/validation.py`,
  derived at read time from profile-binding state and stored nowhere. It is
  already a public wire value on entity write responses.

Choosing any of the four for the envelope means adopting a vocabulary that
already disagrees with three others. Choosing a fifth makes it four-against-one.

What the tree *does* offer, and the plan did not notice, is a working
implementation of this exact transition that is not a mode at all. Progression's
advisory→enforcing flip runs a **graduation pre-flight**
(`contextplane/api/routers/admin_progression.py`) over
`scan_graduation_offenders` (`contextplane/service/catalog/progression.py`),
which enumerates the exact rows that would fail under the proposed enforcing
definition. It has a dry-run report, a blocked-on-offenders refusal, a bounded
scan with a timeout, and a force path that requires a written migration plan.
The pre-flight runs on every write rather than only on the flip, because
conditioning it on the flip would silently discard a caller's `dry_run`.

Two other constraints bear on where the mode can live:

- **Metric cardinality is closed.** `contextplane/metrics.py` and
  `contextplane/arc/metrics.py` forbid tenant-, actor- and session-labelled
  series and raise on an unrecognised label value. "How many tenants are still
  advisory" is not a gauge this codebase can carry.
- **The audit vocabulary is hand-maintained and nearly silent on this.**
  `progression.transition.warned` and `progression.transition.overridden` are the
  only actions naming a rule that was deliberately not enforced. There is no
  envelope action, no envelope table, and no metric with an enforcement-mode
  label anywhere.

One premise is recorded as **unverifiable from this repository**. "Breaks every
existing deployment on day one" cannot be settled here: `pyproject.toml` and the
Helm chart both read `0.0.1` and there are no release tags, which is absence of
evidence rather than evidence of absence. The graduation path is justified below
on the cheaper and sounder ground that an interface is cheaper to stage before it
has users than after.

## Decision

**Two stages, named, and no third.** An envelope decision is either `advisory`
or `enforcing`. There is no `warn`: `warn` means, for PII, that the write
proceeds and a `pii_warning` rides the response envelope, and an authority
decision has no such carrier — inventing one is a wire-contract change to
`openapi.json` in exchange for a state nobody asked for. Two values also match
what progression actually does, which is the mechanism being reused.

**The stage is per tenant, stored as a column on `tenants`, defaulting to
`advisory`.** That table already carries three tenant-level policy columns of
this shape (`is_regulated`, `notification_digest_window`,
`memory_retention_days`), each with a CHECK constraint. Per tenant rather than
per deployment, because a multi-tenant deployment that can only graduate
everybody at once cannot graduate anybody.

**No environment variable and no `Settings` field sets it.** ADR-0004 states this
for scoring magnitudes; extending it to an authority mode is an argument rather
than a precedent, and the argument is: this repo's one existing precedent for a
security-relevant flag (`arc_drafter_model_enabled`, checked at startup against a
committed artifact) is that a flag may only make behaviour *more* restrictive.
An envelope mode read from the environment would be the first flag here able to
widen authority, and it would do so without an audit row naming who widened it.

**The flip is a pre-flight, not a write.** Moving a tenant to `enforcing` goes
through an admin route in the shape of `_run_graduation_preflight`: it runs an
offender scan first, refuses with the offender list if the scan is non-empty,
supports a dry-run that reports without writing, bounds the scan with a timeout,
and accepts a force only with a written migration plan recorded on the audit row.

**The offender scan is: every principal that acted in the observation window and
had no envelope.** Not "zero would-be refusals" — on day one every principal is
unenveloped, so a counter of would-be refusals fires on every request and
measures nothing. The scan answers a different question, "who would this break",
and it is answerable only because the advisory stage records it.

**The advisory record is an audit row, not a metric.** Two new actions:
`envelope.authority.advisory` for a decision that would have refused and did not,
and `envelope.authority.refused` for one that did. The flip criterion is a query
over the first — which rules out a metric, since the no-tenant-label rule means a
counter cannot attribute anything to the tenant being graduated. The rows are
emitted through `contextplane.audit.emit.emit()`, which writes in its own
transaction and never re-raises; a decision the request depends on must not
depend on its own record.

**The first enforcement point is the surfaces that exist.**
`POST /v1/memory/sessions/{session_id}/events` in
`contextplane/api/routers/memory.py`, and the intent write routes whose authority
model is `AuthorityOrigin` in `contextplane/context/intent.py`. Not
`POST /v1/sessions/{id}/observations`, which the plan assumes and which does not
exist. Read paths are out of scope for this ADR: every existing
advisory/enforcing mechanism in this tree gates writes, and the two read-path
mechanisms (trust coverage, ARC resolve) are both deliberately report-only.

**The four existing vocabularies are not converged here, and that is a debt this
ADR names rather than pays.** The supersession rule requires saying why an old
path stays and until when: converging PII severity, progression's boolean, the
edge-property window and profile-derived validation would touch four subsystems,
three wire contracts and a public response field, for a consistency benefit that
no current behaviour depends on. The envelope adds a fifth vocabulary of two
values. The condition for revisiting is the first time a second authority-shaped
mechanism needs a mode; at that point there are two and a shared vocabulary is
worth extracting.

## Assumptions

1. **A principal can be identified on a live request.** The tree establishes
   `TenantContext.actor_id`, the five `AuthorityOrigin` values, and ARC verifier
   principal bindings, but never says which of these an envelope binds to. If the
   answer turns out to be "none of them without new plumbing", the first
   enforcement point moves and the advisory stage cannot start when this ADR
   assumes.
2. **The advisory stage produces a queryable population.** If most requests carry
   no identifiable principal, the offender scan returns an empty list for the
   wrong reason and the flip criterion passes vacuously. The pre-flight must
   therefore report the population it scanned, not only the offenders it found —
   the same anti-vacuity rule the rest of this repo's gates follow.
3. **Per-request tenant mode lookup is affordable.** Nothing measures this. The
   nearest precedent is `progression_definition_cache_ttl_seconds`, which suggests
   a cache is expected to be needed; if it is, the cache TTL becomes the window
   during which a suspension has not taken effect, which is ADR-0007's problem.
4. **Two stages are enough.** If operators ask for a middle state where the
   refusal is visible to the caller but not fatal, that is `warn`, and it needs
   the response carrier this decision declined to build.

## Alternatives rejected

**A global `Settings` kill-switch.** Cheapest to ship and the only option an
operator can flip without touching data. Rejected because it is deployment-wide,
so per-tenant graduation is impossible, and because it would be the first flag in
this codebase capable of widening authority rather than narrowing it.

**Mode carried on the ARC artifact, graduating through the ARC revision
lifecycle.** Attractive because E1 makes the envelope an ARC artifact anyway.
Rejected because ARC's own `blocked` outcome is deliberately non-enforcing at the
transport boundary — a blocked resolution is HTTP 200 with its receipt, so the
evidence is not discarded, and both the router and the overview doc say so.
Building envelope enforcement there means either inheriting a surface that
refuses nothing, or changing a documented contract as a side effect of a rollout
decision. Also note `arc_artifacts.tenant_id` is nullable: artifacts are
global-capable, so "which tenant is this envelope's mode for" has no answer on
that table without a second one.

**Mode as a profile extension through the binding lifecycle.** The only option
that already has rollback, a one-active-binding exclusion constraint, and a
recorded actor and reason per change — genuinely the best-governed mechanism in
the tree, and the one ADR-0004 chose for scoring. Rejected for this because a
tenant with no binding resolves `unbound`, which would make "no envelope, no
authority" structurally unreachable for exactly the deployments most likely to
exist. A governance mechanism whose default state is "ungoverned" is the wrong
shape for a rule about authority.

**Time-boxed auto-graduation (`enforcing_from`), mirroring `advisory_until`.**
Rejected twice over: the precedent is half-applied in the tree, present on one of
two schema registries, so copying it means copying something the repo itself did
not finish; and a clock-driven flip turns enforcement on with no offender scan at
the moment it fires, which is precisely what the progression pre-flight exists to
prevent.

**Scope-limited hard enforcement from day one over a closed pilot set, no mode at
all.** This is what the repo did the last time it turned a detection into a
refusal: `contextplane/context/admission.py` ships a hard block on a fresh
deployment, bounded to seven named field types, with the floor in code. It is a
real option and it is cheaper than everything above. Rejected because it answers
a different question — it decides *what* is enforced rather than *when*, so the
named-criterion-per-flip becomes a criterion per pilot expansion, and it cannot
express "this tenant is not ready". Recorded here because if the advisory stage
turns out to measure nothing (assumption 2), this is the fallback, not a mode
with more values.

**Report-only permanently; the calling host enforces.** Consistent with two
existing documented conventions and breaks no deployment. Rejected because it
makes "no envelope, no authority" a claim the service cannot back, and moves
every enforcement point into code this repository does not own.

## Consequences

The flip becomes a computed answer rather than a judgement call, and the same
shape as the one flip this codebase already knows how to do. An operator asking
"what breaks if I turn this on" gets a list of principals instead of an opinion.

A fifth enforcement vocabulary enters the codebase. That cost is accepted with
its condition for repayment stated above, and it is the honest price of not
adopting one of four mutually inconsistent existing ones.

Two new audit actions and a new `tenants` column, which means a migration and a
hand edit to the closed `__all__` in `contextplane/audit/actions.py`. There is no
ORM/DDL agreement gate outside ARC, so the column and its mapping in
`contextplane/storage/models.py` are kept in sync by review rather than by a
check — a real gap, and one this ADR does not close.

The advisory stage is not free at runtime: it evaluates the authority decision
fully and then does not act on it. That is the cost of knowing who the flip would
break, and it is bounded by the same evaluation the enforcing stage will do.

Nothing here can be built until a principal is identifiable on a request
(assumption 1). If that turns out to require new plumbing, this ADR's decision
holds and its schedule does not.

## Dissent

*On two stages rather than three.* Dropping `warn` means an operator's only
options are "silently record" and "refuse". For a bank, the missing middle —
where the agent is told it is acting outside its envelope and proceeds anyway,
with the refusal visible in its own output — is arguably the most useful of the
three, because it trains the caller rather than just the operator. The counter is
that adding it means designing a warning carrier for authority decisions, and
this ADR declined to design one speculatively. If the middle state is wanted, it
should arrive as its own decision with the carrier designed, not as a third
enum member.

*On the per-tenant column.* A reviewer holding ADR-0004's line would say the
binding lifecycle is the right home for every tenant-scoped governed value and
that this ADR carved out an exception on the first try, with the `unbound`
default as the excuse. That reading is not unreasonable — the fix would be to
make an absent binding resolve to `advisory` rather than `unbound` for this one
value, which is a small change to a shared mechanism made for a single consumer.
The decision above chose the column because a rule about authority should not
depend on a tenant having adopted an unrelated subsystem, but the objection is
recorded because the two mechanisms will now diverge and somebody will have to
reconcile them.

*On the premise.* One view holds that a graduation path for a mechanism with no
code, no users and no deployments is design work done in the wrong order, and
that the pilot-scope option should have been taken on the grounds that it is what
the repo demonstrably does when it means it. The response is that the pre-flight
being reused already exists and costs little to point at a second scan, and that
the cheap thing to do here is not the same as the cheap thing to do later.
