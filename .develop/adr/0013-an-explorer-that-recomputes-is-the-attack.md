# 0013 — E11's aggregates read the stored series; an explorer that recomputes is the attack

**Status:** Accepted 2026-08-22

## Context

E11 asks for a receipts explorer, tenant-scope served-claims aggregates "under
the existing suppression floors", and audit-role drill-down. Grounding the
phrase "existing suppression floors" is what produced this ADR, because it
understates what ships by a wide margin and the gap is exactly where E11 would
have gone wrong.

**The floors exist and are the smaller half.** `learning_reads.py` sets
`MIN_COHORT_ACTORS = 5` and `MIN_CELL_EVENTS = 5`, and `Floors` *refuses* a
looser configuration rather than clamping it — "a deployment that configured
three actors per cohort and got five would keep believing it had configured
three, and the next person to read that configuration would trust it."

**The larger half is a differencing defence, and its own opening line says so:**

> The hard part is not the floors — it is the recompute.

The attack is not a small cell. It is *two figures for the same cell*: computed
over a window, published, and computed again after an erasure. **Every floor
holds perfectly while that happens.** Both figures clear the minimum, neither
names anybody, and subtracting them names one person's contribution exactly.

Three mechanisms in `signals/aggregates.py` and `privacy_aggregates` carry the
defence, and none of them is a step somebody remembers:

1. **One version of a cell, ever.** A unique cell key makes a predecessor
   unstorable, so a recompute has nowhere to leave the figure it replaced. The
   comparison happens inside the database, against the row it is about to
   overwrite, with no window in which two versions exist.
2. **Withholding is one-way.** Once withheld, a cell stays withheld through
   every later pass. A cell suppressed at an erasure and recomputed cleanly a
   day later would publish the post-erasure figure beside the reader's memory of
   the pre-erasure one — the same subtraction, one pass later.
3. **A withheld cell keeps no counts.** Value to NULL *and* actor count to zero,
   because a cell reporting "six actors now" beside a reader's memory of seven
   has disclosed that the erased subject was one person.

Windows are keyed on the row's own write instant, never on the time it reports,
so a window that has ended can only ever shrink — which is what makes mechanism
1 sound.

## Decision

**E11's aggregates read the stored series in `privacy_aggregates`. They do not
compute breakdowns live.**

An explorer is precisely the shape of thing that defeats the mechanism above. A
screen that lets an auditor run the same breakdown on Monday and again on Friday
reproduces the differencing attack **with no floor violated and no bug
anywhere**, because the component that prevents it is the *writer*, and a reader
that recomputes has routed around it rather than through it.

The module already says the live path is not this: the read surfaces "compute
their breakdowns live and floor them on the way out, which is correct for a
question asked now; it is not a stored series." An explorer is not a question
asked now. It is a surface whose entire value is asking the same question at two
times and comparing, which is the attack stated as a feature.

**A metric E11 wants that is not in `AGGREGATE_METRICS` is a writer change, not
a reader change.** That set is closed deliberately, "so a metric cannot be
computed by one pass and forgotten by the next". Adding a live computation
beside the stored series to cover a gap would reintroduce exactly the recompute
this decision removes, and it would look like a small convenience while doing
it.

**The receipts half of E11 is unaffected and stays live.** A receipt is a record
of one resolution, not an aggregate over a population; reading it twice
discloses nothing that reading it once did not. This decision is about the
aggregate surface only, and conflating the two would make the explorer harder
than it needs to be.

## Assumptions

1. **The stored series covers what an explorer needs to show, or the writer is
   extended.** Today `AGGREGATE_METRICS` is two metrics. If E11's screens need a
   third, that is a change to `signals/aggregates.py` and to
   `_SOURCE_CLASS_FOR` — an aggregate is a derivative and must inherit its
   sources' retention rather than a period chosen at the screen.
2. **Cell staleness is acceptable to a reader, and visible to them.** A stored
   series is as fresh as its last pass. An explorer that presents a stale figure
   as current invites the reader to reconcile it against something else, which
   is a differencing attack performed by an honest user.
3. **The cohort stays the tenant.** `COHORT_TENANT` is a literal because there
   is no membership model to group by, "and inventing one would build the
   per-team performance surface the policy forbids". A drill-down UI is the
   place most likely to want a finer cohort; wanting it is not a reason to have
   one.

## Alternatives rejected

**Compute live and floor on the way out, as the current read surfaces do.** The
straightforward option and the one this ADR exists to refuse. It is correct for
a single question and wrong for a surface built to be re-asked, and the failure
is silent: every floor passes, every response is individually defensible, and
the disclosure is in the difference between two of them.

**Compute live, but forbid the UI from showing history.** Defends against the
feature and not the attack. The reader has a memory and a screenshot; the second
request is a second request whatever the client renders. A privacy property
enforced in a client is not enforced.

**Rate-limit or log repeated identical breakdowns.** Detection rather than
prevention, and it fails on the timescale that matters — an erasure and a
re-read a week apart are not a suspicious pattern, they are ordinary use. It
also puts the burden on whoever reads the log, which
[ADR-0012](0012-external-anchor-for-the-digest-chains.md) already identifies as
the part most likely to be skipped.

**Serve the stored series but recompute on a cache miss.** The worst option,
because it is the rejected one wearing the accepted one's name: the fallback
path is the live computation, it fires exactly when the series is incomplete,
and nothing in the response distinguishes the two.

## Consequences

E11's aggregate screens are bounded by what the writer materialises, which is
narrower than "any breakdown a reviewer might want" and will feel like a
limitation before it feels like a guarantee. That is the trade: the alternative
is a surface that is more capable and quietly discloses.

Adding a metric becomes a change with a retention question attached, because
`_SOURCE_CLASS_FOR` makes an aggregate inherit its source's record class. That
is friction in the right place — an aggregate outliving its sources is a
retention breach that no floor detects.

The audit-role drill-down (E11-T3) is unaffected by this decision and governed
by its own: per-actor detail is authorized by `ROLE_AUDITOR` and recorded with a
justification written **in the same transaction as the read**, because a
justification captured afterwards is empty exactly when it matters.

## Dissent

The strongest objection is that this ADR generalises from one writer to a whole
epic. `privacy_aggregates` was built for feedback-rating and signal-source
mixes; nothing establishes that every aggregate E11 eventually wants
participates in a difference at all. A count of receipts issued per day, with no
per-actor contribution, arguably cannot leak by subtraction — and forcing it
through a materialising writer buys nothing and costs a schema change. This
decision takes the conservative line because deciding per-metric requires a
per-metric argument that nobody has made yet, and the failure mode of being
wrong in the permissive direction is silent disclosure. But "conservative
because the analysis is missing" is a weaker justification than it reads as, and
the honest fix is the per-metric analysis rather than this rule.

A second: assumption 2 asks that staleness be visible, and nothing in this
decision makes it so. A stored cell carries `computed_at`, so the material
exists — but an explorer that shows it in a tooltip has satisfied the letter of
this while leaving a reader to reconcile figures across a refresh, which is the
honest-user version of the attack. Where staleness must be surfaced, and how
prominently, is not decided here and should be before the screen is built.

A third, on scope: this says nothing about the *receipts* half sharing a screen
with the aggregate half. Two surfaces with different disclosure properties in
one view is how a reader ends up believing the stricter one's guarantees apply
to both.
