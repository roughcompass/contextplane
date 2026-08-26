# 0028 — A block is presented in the order its read produced, not in digest order

**Status:** Accepted 2026-08-25

## Context

`assembler.ordered_items` sorts every block's items by `receipt_item_id.value()`
— a SHA-256 digest of the block, source and item identity — and every one of the
ten places that build an `ArmOutcome` calls it. Its docstring gives the reason:

> Sorted by receipt item id, which is derived from block, source and item
> identity — so the order is a property of what the items *are* rather than of
> the query plan that found them. Two resolutions over unchanged data produce the
> same order, which is what makes a receipt checkable.

The property it establishes is real. What it costs was not stated, and is easiest
to see measured. Asking the development catalog *"which components depend on the
salt theme provider"*:

```
search() ranking                    envelope canonical block
 1. salt-design-system  0.3222       1. salt-avatar          digest 4092a5…
 2. salt-accordion      0.2000       2. salt-banner          digest 44e556…
 3. runtime-sdk-mf-bridge 0.1500     3. salt-design-system   digest 535bb8…
 4. salt-avatar         0.1000       4. salt-badge           digest 7b3d34…
 …                                   …                       (ascending hex)
```

The retriever ranks the design system first at more than three times the score of
the fourth result. The envelope presents the fourth result first, and the block is
in ascending hexadecimal order. **The ranking is computed and then discarded.**

Three forces:

- **The consumer is an agent with a finite attention budget.** The whole premise
  of a context envelope is that it assembles what a model needs for one prompt.
  Handing the model ten items in hash order and expecting it to find the relevant
  one is handing back the problem the envelope exists to solve. A person reading
  Context Lab sees the same thing: the first row is essentially random.
- **The cap cuts by digest.** `_block_from_outcome` applies
  `outcome.items[:item_cap]` *after* this sort, so a block that exceeds the cap
  drops items by hash. The best match can be discarded while a worse one is kept.
  That is a content defect and not only a presentation one.
- **The reproducibility argument does not require a digest.** It requires a
  *deterministic* order. Every one of the ten call sites already has one: each is
  fed by a read with a total `ORDER BY`, or by a ranked fusion whose final sort is
  tiebroken to a total order. Entity search's determinism was measured directly
  while fixing the natural-language defect — identical arm output and identical
  fused output across three freshly seeded databases.

The receipt is unaffected either way: `context_receipt_items` has no position
column, so a receipt records *which* items were served and never in what order.
`test_retrieval_relevance.py` says so, and returns a set for that reason.

## Decision

**`ordered_items` preserves the order its arm produced.** The digest is no longer
a sort key. All ten call sites keep calling it, and none of them changes: what
changes is that the order each read already established survives into the
envelope, and into the cap.

The function keeps its name and its place, because its job is unchanged — it is
the one point where a block's order is decided, and having one such point is what
makes this decision enforceable rather than a convention.

**The property it guarantees moves from the digest to the arms, and is asserted
there.** "Two resolutions over unchanged data produce the same order" is now a
claim about every read that feeds a block, so it is tested as one: identical
requests against an unchanged corpus return identical block orders. That is a
stronger test than the digest sort ever needed, because the digest sort made it
true by construction and therefore untestable.

## Assumptions

- Every read feeding an `ArmOutcome` has a total order. **This was assumed while
  drafting and was false**, which is worth recording because it is the whole risk
  this decision takes. The audit found:
  - *canonical* — `search`'s final sort is `(-score, name.lower(), entity_id)`.
    Total, and measured: identical arm and fused output across three freshly
    seeded databases.
  - *observed claims* — the structural read ends `…, c.claim_id`; the fused read
    ends `(-score, claim_id)`. Total. The semantic arm alone orders by vector
    distance with no tiebreak, which is harmless because fusion re-sorts.
  - *instructions* — `scope, authored_at, delta_id`. Total.
  - *ARC* — the receipt's own stored `selected` list, so the same receipt yields
    the same order.
  - *workspace* — **`recorded_at DESC` and nothing else**, in three of the four
    checkpoint reads. Two checkpoints recorded in the same instant could arrive
    either way round, and a `LIMIT` on top could keep a different one each time.

  So the workspace reads were made total in the same change, tiebroken on
  `checkpoint_id`. The latent nondeterminism was real and predated this decision;
  the digest sort was concealing it by discarding the order, which is the clearest
  argument available that concealing an order is not the same as having one. The
  test named above is what catches the next such read.
- No consumer treats item position as authority. Position now carries relevance,
  which is information; it is not entitlement, and nothing may start reading it
  as one. `scoring.py` and the judge read the served set.
- Digest order was not load-bearing anywhere. It is not recorded in the receipt,
  not part of the published contract, and not asserted by any consumer — only by
  tests of the sort itself.

## Alternatives rejected

**Add a rank field and let the consumer sort.** Honest, and it preserves the
current bytes. Rejected because the cap still cuts by digest before any consumer
sees a rank, so the best match can be gone before the field is read — and because
a payload that has to be sorted before it is useful is a worse answer than one
that arrives sorted.

**Sort by rank in the assembler, using a score on the item.** Rejected because
only two of the five blocks have anything resembling a score, and inventing one
for the other three — a checkpoint's "score", a directive's "score" — would be
fabricating a magnitude in order to sort by it. Arm order already expresses each
read's own notion of best-first without pretending they are commensurable.

**Preserve order only for the ranked blocks and keep the digest elsewhere.**
Considered seriously, since canonical and observed-claims are where the defect was
measured. Rejected because all ten sites turned out to be fed by ordered reads, so
the split would have been arbitrary, and because two rules for one function is how
a call site ends up on the wrong one.

## Consequences

- The first item in a block is the read's best answer. This is the point.
- A block that exceeds `item_cap` now drops its worst items rather than an
  arbitrary hash-selected subset. This is a change in *content*, not only order,
  and it is the more serious half of the fix.
- Tests that asserted digest ordering fail and are rewritten to assert the order
  the read produced. They were asserting the defect.
- Determinism becomes a property of the arms rather than of one sort call. That is
  a real transfer of risk: it is now possible to add a read without a total order
  and get a block that varies between identical requests. Accepted because the
  alternative is guaranteeing determinism by destroying the information, and
  because it is testable — under the digest sort it was not.
- A future block whose read genuinely has no order must establish one before
  returning items, rather than relying on this function to impose one.

## Dissent

The strongest objection is that this removes a guarantee that held unconditionally
and replaces it with one that holds only as long as every arm keeps its `ORDER BY`
total. That is a real weakening: the digest sort could not be got wrong, and this
can. Someone adding a sixth block under deadline will not read this record.

The objection is not hypothetical, and the audit above is what makes that
concrete: three of the four workspace checkpoint reads were already partial, so
the weakening this decision is accused of introducing had in fact already
happened — the digest sort was hiding it rather than preventing it. A guarantee
that holds by discarding the evidence is not a guarantee about the system.

The mitigation is a test rather than a promise, and it asserts the property
directly against the reads themselves, so a checkpoint read added without a
unique tiebreak fails rather than degrading quietly. It is worth restating that
the old guarantee bought nothing a caller could use: it made every resolution
agree on an order that was meaningless in the same way, which is consistency
rather than correctness.

A second objection: this changes what an existing consumer receives, without a
version bump. The contract does not specify block item order, so nothing
documented changes — but an undocumented order that has been stable is something
callers depend on regardless. The judgement is that a consumer relying on hash
order is relying on a bug, and that shipping the fix is better than preserving it
behind a flag nobody would turn on.
