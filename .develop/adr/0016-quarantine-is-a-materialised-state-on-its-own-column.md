# 0016 — Quarantine is materialised state, on its own column, and never on `t_invalidated_at`

**Status:** Accepted 2026-08-22

## Context

E4 builds provenance-scoped quarantine: an operator names bad memory content by
a provenance predicate — this connector run, this extractor version, this source
namespace — and it stops being servable. Nothing like it exists;
`quarantine` appears twice in the tree and both are an unrelated
profile-migration disposition.

Two shapes were on the table. **A**, materialise a state onto each matched row
and let the existing read predicate refuse it. **B**, store the rule once and
evaluate it on every read path.

Grounding this against the tree corrected the argument that was supposed to
settle it, and found that E4-T2's own recommended implementation is a bypass.

**The premise "this codebase makes things unservable by deleting from the index,
not by filtering at read" is inverted.** `embedding_index.py` says the opposite
in its own words: *"The read arms already refuse unservable claims, so a retired
claim's vector cannot produce a wrong answer directly. But an ANN query is
`ORDER BY vector <-> q LIMIT k` — every dead vector in the index occupies a
candidate slot that a live one could have used."* Retraction is a **recall**
mechanism. Correctness is the read filter. `close_superseded` does both in one
transaction.

That does not flip the answer; it sharpens the question. It is not "read filter
or index delete" — it is **what the read filter reads**. Under A it reads a
column the filter already selects on. Under B it reads a join to a rules table
that every statement must acquire.

## Decision

**Shape A. The predicate is stored durably and evaluated at write time — at
admission and at sweep. The row carries the answer. No read ever evaluates the
predicate.**

That seam keeps B's two genuine advantages (revert in one statement, coverage of
rows that arrive later) and pays none of B's read-path cost.

### The column is `quarantined_at`, and reusing `t_invalidated_at` is a bug

E4-T2 recommended the bitemporal idiom — close the row with `t_invalidated_at`
rather than adding a flag. **That recommendation is withdrawn, because the
resulting quarantine is defeated by a query parameter.**

`_SERVABLE_AS_OF` reads:

```sql
    c.status IN ('staged', 'superseded')
AND c.consolidated_at IS NOT NULL
AND c.created_at <= :as_of
AND (c.t_invalidated_at IS NULL OR c.t_invalidated_at > :as_of)
```

The `status` term is unconditional. **The `t_invalidated_at` term is
`as_of`-relative**, deliberately — *"a claim closed after the instant asked
about was still believed then, which is the whole point of asking."*

And `as_of` is caller-supplied on both transports: a REST query parameter on
`GET /v1/memory/claims`, and an argument on the `query_claims` MCP tool. So
quarantine a bad connector run at 14:00, and an agent calling
`query_claims(as_of="13:00")` is served every quarantined claim. That is exactly
the "quarantined claim served with a straight face" the task set out to prevent,
arriving through the materialised shape via the idiom the task recommended.

**Follow `discard`'s shape, not supersession's.** `discard` writes
`status='rejected'` and *"it never serves again"* — unservable at every `as_of`,
because the `status` term is unconditional. Quarantine gets a dedicated
`quarantined_at` on `memory_claims`, joined into `_SERVABLE_AS_OF` as an
unconditional `AND c.quarantined_at IS NULL`, with the matching term in
`_SERVABLE_STATUSES` so `project_claim` retracts the vector too.

**The rule-to-row ledger — which predicate closed which rows, when, by whom —
lives in a side table read only at apply, revert and audit. Never at read.**
That is the seam, stated as a rule somebody can check in review.

### Enforcement stays at read; propagation is added on top, for recall only

[ADR-0007](0007-grant-lifetime-and-suspend.md) is the ratified position on this
class of question and it rejects Shape B's sibling in as many words:

> The read-time check is what makes the guarantee; adding a sweeper on top of a
> read-time check is fine, adding one instead of it is not.

This decision adopts that rule rather than overriding it. `_SERVABLE_AS_OF`
still decides, on every read, and the quarantine write commits synchronously
with it — so there is no window in which propagation lag makes a quarantined
claim servable. The derivative drain runs *on top*, and only to reclaim ANN
candidate slots.

Propagation reuses what ships, with no new vocabulary:
`enqueue_for_sources(record_class=RECORD_MEMORY_CLAIM, source_ids=<matched>,
operation=OPERATION_REBUILD, trigger=TRIGGER_POLICY_CHANGE)`. `TRIGGER_POLICY_CHANGE`
already exists and, unlike erasure, needs no tombstone. Revert is the identical
call.

### Rows arriving after the sweep are already somebody's job

The strongest argument for B was that a predicate covers rows written later,
where a materialised state does not. **That mechanism exists.**
`SourceGovernanceService.admit` is a persisted, provenance-keyed circuit breaker
that gates every ingest — *"the check is not a lint on registration, it is the
gate on every ingest"* — with state in the database, because *"a breaker held in
memory reopens on every deploy"*.

Two gaps, both small and both additive: it is keyed on `source_id`, so it covers
a connector or a source namespace but not an extractor version
(`memory_claims.namespace` / `strategy_id`); and it is time-boxed at
`BREAKER_COOLDOWN_SECONDS = 900` with no operator-held-open mode, which an
incident quarantine needs.

So E4-T1's conditional — *prefer materialised state with a standing predicate
that re-applies on write, if that can be built without a second admission path*
— resolves to **yes**. `scripts/check_privileged_writes.py` restricts
`memory_claims` writes to four modules and is in `make all`, which is what makes
"one admission path" a guarantee rather than an intention.

## Assumptions

1. **The write side stays machine-gated and the read side does not have to
   be.** This is the load-bearing asymmetry. Writes are held to one module by a
   required gate; reads are held to a hand-maintained list, and `arms.py` records
   that the list has been wrong twice: *"Twice now this check was wired on the
   one path in front of somebody -- documented as covering 'the serving paths',
   plural, and covering one."* Shape B's correctness would rest on that surface.
2. **`quarantined_at` is added to every spelling of the servability rule.**
   There are three today — `_SERVABLE_STATUSES`, `_SERVABLE_AS_OF`, and an inline
   variant in `curation_queue.py`. The third one *should* differ: an operator
   must still see quarantined claims in the queue. That is a further argument
   against B, since a single uniformly-evaluated predicate gives the curation
   surface the wrong answer.
3. **An operator-held breaker state is acceptable operationally.** A quarantine
   that stays shut until lifted can strand a source if nobody lifts it. That is
   the intended failure direction and it still needs somebody watching.

## Alternatives rejected

**Shape B, a read-time predicate.** Rejected on cardinality and on which surface
carries the risk. An envelope suspension is one row at one choke point, which is
why ADR-0007 could put it at read. A quarantine predicate is set-valued over an
unbounded population resolved through `memory_claim_provenance`, so B is an
anti-join inside `ORDER BY emb.vector <=> ... LIMIT :limit`. And a quarantined
vector stays in the HNSW index forever, recreating precisely the recall loss
`retract` exists to prevent — worst exactly when the quarantine is largest.

**Shape A on `t_invalidated_at`.** The recommendation this ADR withdraws.
Defeated by a caller-supplied `as_of`, as shown above.

**Delete the rows.** Fastest and it destroys the record that anything was
quarantined, which is what revert and every audit need. The codebase's position
is settled: *"Revocation is temporal, never a delete"*, and migration 0062 —
*"Suspend is a status flip, not a delete... reinstating is one more flip and the
history reads as a suspension rather than a gap."*

**Rule-only revert (delete the rule row).** B's revert, and it is vacuous: it
erases the record that anything was ever quarantined, which is the failure
`grants.py` refuses.

## Consequences

`memory_claims` gains a column and the servability rule gains a term in each of
its spellings. Every path that decides servability must be found — which is
work, and is bounded and reviewable, unlike B's obligation on every future read.

Two follow-on items this decision creates rather than inherits: the source
breaker needs an operator-held-open mode and an extractor-version predicate, and
the rule-to-row ledger is a new table with a retention question, since
[E6-T2](../plan/governed-agent-memory.md) established retention is keyed on
record class.

**A claim in E4-T2 that must be struck.** The entry says the propagation path
*"already has a story for what happens when propagation is late (`pending_overdue`,
and the arms refuse to serve rather than serving stale)."* It does not cover
this: `register_derivative` defaults `blocking=False`, `register_claim_artefact`
never passes it, and only three sites register blocking — `arms.py` says a
`blocking_only` guard over `vector` *"would never fire"*. This does not damage
Shape A, whose correctness commits synchronously and whose async half is recall
only. But a reviewer who accepts that sentence will believe a guard is
protecting something it cannot reach.

## Dissent

The strongest objection is that this decision is ADR-0007 with an exception
carved for cardinality, and "this case is bigger" is the argument every
exception makes. ADR-0007 rejected a `suspended_at` column *by name*; this adds
`quarantined_at`. The distinction drawn — one row at one choke point versus a
set resolved through a join inside an ANN limit — is real, but it is a
performance argument being used to settle a correctness-shaped question, and
nobody has measured the anti-join. If B's read cost turned out to be
negligible, the principled position would be B, and this ADR would be an
optimisation wearing a design decision's clothes.

A second: assumption 2 asks that a new term reach three spellings of one rule,
and `embedding_index.py` claims *"a conformance test holds them to agreeing
rather than a shared string pretending they are one rule."* **No such test
exists** — nothing in `tests/` references `_SERVABLE_STATUSES` or
`_SERVABLE_AS_OF`, or asserts the two agree. So this decision adds a third term
to a rule stated twice and synchronised by prose, and the prose asserts a
guarantee that is not there. That test should be written before E4-T2, not
after.

A third, on scope: the operator-held breaker turns a rate limiter into an
incident control, and nothing here says who may hold it open or how a held-open
source is surfaced. That is the same gap [ADR-0015](0015-materiality-is-not-severity.md)
leaves around who may classify, and the two will be answered by the same person
or by nobody.
