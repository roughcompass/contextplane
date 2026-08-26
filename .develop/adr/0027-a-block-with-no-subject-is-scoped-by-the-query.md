# 0027 — A block the caller gave no subject for is scoped by the query, not by recency

**Status:** Accepted 2026-08-25

## Context

`POST /v1/context/resolve` takes a `query` and an optional `subject_entity_id`.
Four of the five blocks use the query. The observed-claims block does not: it
passes `subject_entity_id` to `ClaimQuery` and nothing else, so when the caller
supplies no subject — which is what Context Lab's prompt box does, and what an
agent asking a question does — the block returns the tenant's most recent claims
in recency order, whatever was asked.

The effect is what a user reported after using the screen: asking *"Who owns salt
design system?"* returned three claims about `memory-loop-demo`. Nothing failed.
The envelope came back `complete`, the block came back `success`, and the claims
in it were real, current and correctly trusted. They were simply about something
else, and no field in the response says so.

`arms.py` states the current rule and its reason:

> A structural read rather than a ranked one. Ranking would make an answer's
> contents depend on a similarity score nobody asked for, and a receipt that
> cannot be reproduced from its own inputs is decorative.

That argument is sound where it was aimed. It is aimed at the case where the
caller *did* name a subject: there, an exact structural lookup is the right
answer and borrowing a ranker for it would make a precise question return a
fuzzy answer. The argument was then applied to a case it does not describe.

**With no subject there is no structure to read.** The current behaviour is not
"structural" in that case; it is `ORDER BY recency LIMIT n` over the tenant. And
it is the reproducibility argument that suffers most:

- The **recency** read depends on wall-clock time and on every claim written
  since — neither of which is an input to the request. Two resolutions of the
  identical request, minutes apart, legitimately return different claims.
- The **ranked** read depends on the query, which *is* an input to the request,
  and on the corpus at `as_of`, which the receipt already pins.

So ranking by the query makes the block *more* reproducible from its own inputs,
not less. The rule as written protects a property the no-subject path never had.

Three further forces:

- **The ranked read already exists and is already the serving path.**
  `ClaimServingService.retrieve` fuses a semantic and a lexical arm through the
  same `fuse_hybrid_arms` entity search uses, then serves through the same
  visibility checks, the same citation construction and the same recall label as
  `query`. This is not a new retrieval path; it is one the block declines to call.
- **The neighbouring arm already made this decision.** `for_request` passes
  `term=workspace_term if workspace_term is not None else query` to the workspace
  arm: *narrow by the query when the caller gave nothing more specific.* The
  claims arm is the only one of the five that does not.
- **A deployment with no embedder must not lose the block.** `ContextArms` holds
  `embedder: Embedder | None`, and `None` is a real deployment state.

## Decision

**When the observed-claims block has no `subject_entity_id`, it is scoped by the
request's `query`.** Concretely, `observed_claims_arm` chooses among three reads:

1. **`subject_entity_id` is given** → the structural read, unchanged. The caller
   named the subject; an exact lookup is the answer, and ADR-level rationale
   quoted above governs here and keeps governing here.
2. **No subject, a query supplied, an embedder present, and an index that can
   answer** → `ClaimServingService.retrieve(query=...)`, the existing fused ranked
   read.
3. **Anything else** → the structural read, as today.

**The fourth condition was not in the first draft, and its absence was a serving
regression.** `retrieve` reads the embedding index; `query` reads the claim store.
A claim that has been consolidated but not yet drained is servable by one and
invisible to the other, so branching on the query alone silently dropped claims —
strictly worse than the irrelevant ones this decision exists to stop serving.
Five integration tests went from serving two claims to serving none, and the
integration tier is where that surfaced because `make all` does not run it.

`index_can_answer` is therefore asked per resolution: an indexed existence check,
not `index_coverage`, which aggregates over the whole claim store and would be
paid on every request. It separates the two cases that matter — an index that
cannot answer *anything* for this tenant, and one that answered *nothing* for this
query. The first is a fallback; the second is the answer. Partial lag is not
distinguished and cannot be cheaply; a half-drained tenant gets ranked results
over its indexed half, which is the case `index_coverage` exists to make visible.

The branch turns on whether a query *string* was supplied, not on whether that
string carries searchable terms. Deciding the latter means parsing a tsquery,
which is Postgres's job; reimplementing it in an arm to choose a code path would
be a second parser that drifts from the one that actually runs. The cost is that
a query of only stopwords takes the ranked read, matches nothing, and returns an
empty block. That is the better of the two available answers — the alternative is
to notice the emptiness and quietly serve recency instead, which answers a
question the caller did not ask and is indistinguishable from a real result.

**Only the permanent gap degrades, and that asymmetry is deliberate.** A degraded
block degrades the whole envelope and makes it uncacheable, so the signal has to
be worth that.

- **No embedder** degrades. The deployment cannot rank, it will not start being
  able to, and an operator can act on it. "The tenant's recent claims" and "the
  claims about what you asked" are identical in the items alone, so silence here
  would be a block that looks like it answered the question.
- **An undrained index** does not, though it produces the same fallback. It is
  transient by construction — every tenant starts with one and the drain fixes it
  — and the block returns exactly what it returned before this arm learned to
  rank, which nothing called degraded then. Marking it would make every
  resolution on a fresh or draining tenant incomplete and uncacheable, which is
  how `degraded` stops meaning anything anywhere else. The number for this
  condition already exists: `index_coverage`, whose own docstring says it is
  there to show an empty claim index on a dashboard rather than leave it to be
  discovered by reading code.
- **No query at all** does not. A caller who supplied nothing to narrow by was
  not narrowed, and reporting that as degraded would teach a reader to distrust a
  block that answered exactly what was asked.

The cost of the middle case is that a reader of one envelope cannot tell a
query-scoped claims block from an index-lagged one. An `observed_claims_block_note`
alongside the existing `arc_block_note` and `instruction_block_note` would close
that, and is deliberately left as a follow-up rather than bundled here: it is a
response-contract change, and the vendored OpenAPI pin in `contextplane-ui` makes
it one that lands on its own.

The choice is made from the request's own inputs, so the receipt still determines
the read: given the same request and the same corpus at the same `as_of`, the
same branch runs and returns the same claims.

## Assumptions

- `ClaimServingService.retrieve` enforces the same visibility and trust
  guarantees as `query`. Its docstring asserts this and gives the reason a second
  serving path was refused; if that ever stops being true, this decision inherits
  the defect and the two paths must be reconciled rather than this one reverted.
- A tenant's claim corpus is large enough that "most recent *n*" is not
  incidentally the same set as "most relevant *n*". On a small deployment the two
  coincide and this change is invisible, which is the harmless direction.
- Ranked order within the block is presentation, not entitlement: nothing
  downstream treats claim position as authority. `scoring.py` and the judge read
  the set.

## Alternatives rejected

**Leave it, and say on screen that claims are not scoped by the prompt.** Honest,
and much cheaper. Rejected because the product's claim is that one resolution
assembles the context an agent needs for *this* prompt; a block that is
constitutionally about something else is not a labelling problem. It also fails
the agent case entirely — an agent reading the envelope cannot act on a caveat
rendered in a dashboard.

**Resolve a subject from the canonical block's top match and use the structural
read.** Keeps one read path and would have returned claims about
`salt-design-system` in the reported case. Rejected because it couples two arms
that run concurrently and are fused independently: the claims block would depend
on the canonical block's ranking, so a change to entity ranking would silently
change which claims are served, and the receipt could no longer be reproduced
from the request alone — the exact property the original rule was defending.

**Always use the ranked read, subject or not.** Simplest to describe. Rejected
for the reason `arms.py` already gives: when the caller names a subject, an exact
lookup should not be replaced by a similarity score nobody asked for.

**Add a `claims_term` request field so the caller opts in.** Rejected as the
wrong default. Every caller asking a question wants claims about the question;
requiring a second field to get that means the default answer is the wrong one,
and most callers will never discover the field.

## Consequences

- Asking a question returns claims about that question. This is the point.
- A caller who relied on the observed-claims block as "recent claims in this
  tenant" — with a query supplied but no subject — gets a different set. No known
  caller does this; the block is documented as context for the prompt, and the
  recency behaviour is not documented anywhere as an interface.
- The block now depends on the embedder in one of its three branches, so a
  deployment without one has a materially different observed-claims block. Case 3
  states this in `reason` rather than leaving it to be inferred.
- One more latency-bearing read on the resolution path in the common case. It is
  the same fused read `retrieve` already serves to its own callers, bounded by
  the same `top_k`, and it runs concurrently with the other four arms under the
  assembler's existing timeout.
- `plainto_tsquery`'s conjunction had to be fixed first, in the claim lexical arm
  as well as entity search's, or this change would have scoped the block by a
  query that matched nothing and turned unrelated claims into no claims.

## Dissent

The strongest objection is that this makes one arm's behaviour conditional on
three things — a subject, a query with terms, and an embedder — where it was
previously unconditional, and conditional behaviour is where reproducibility
arguments go to die. Somebody reading a receipt now has to know which branch ran.

The answer is that the branch is a function of the request, which the receipt
already records, and that the alternative is not "unconditional" but
"unconditionally answering a question nobody asked". Still, the objection stands
against any *fourth* branch: if a future condition is added here, the block
should start recording which read produced it rather than leaving it derivable.

A second objection, weaker but worth recording: rank order inside a block is
currently discarded anyway — `ordered_items` sorts every block by receipt-item
digest, so a ranked read's ordering is thrown away before the caller sees it.
That is true, and it is a separate defect rather than an argument against this
one: the *set* this change alters is what matters here, and the digest sort
cannot make an unrelated claim relevant. Fixing the ordering is its own decision.
