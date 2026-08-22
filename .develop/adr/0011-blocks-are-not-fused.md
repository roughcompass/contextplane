# 0011 — The context envelope's blocks are not fused into one ranked list

**Status:** Accepted 2026-08-22

## Context

E3's body asks for "three concurrent visibility-predicated candidate generators,
RRF merge". Grounding that against the tree found the first clause already
satisfied at a different count and the second refused by the shipped design, on
the record.

**The arms already run concurrently, and there are four.** `assembler.assemble`
runs them under `asyncio.gather` with a per-arm timeout and a per-arm item cap,
both applied by the assembler rather than trusted to each arm — "an arm that
ignored its own bound would otherwise decide how large every response gets, and
a slow arm would decide how long every caller waits". `BLOCK_NAMES` is canonical,
ARC, observed claims, workspace.

**The refusal to merge is the assembler's first stated property**, ahead of
anything about correctness or performance:

> **Authority is not flattened.** The four arms stay four blocks. Nothing merges
> them, re-ranks across them, or promotes a workspace note next to a canonical
> answer because it scored well. A single ranked list would be more convenient to
> consume and would destroy the only signal telling a reader which claims the
> registry stands behind.

An RRF merge is precisely that single ranked list.

## Decision

**`/v1/context/resolve` does not fuse its blocks, and E3's "RRF merge" clause is
struck.** The plan changes; the tree does not.

**The rule for the next author is: fuse within an authority class, never across
one.** That is checkable in review, unlike "be careful with ranking".

Reciprocal rank fusion is not suspect — it is *correct* one layer down, and this
system already uses it there. `search.py` fuses semantic, lexical and graph arms
with governed weights, and should: those three are interchangeable retrievers
over one corpus, answering one question, and disagreeing only about which
document best answers it. Fusing them recovers a better ranking than any of them
alone.

The four context blocks are not that. They are four *authority classes* —
what the registry asserts, what an attested resolution selected, what was
observed and staged, what someone wrote in a workspace. Fusing them answers
"what is most relevant" by discarding "what does the registry stand behind", and
the second is the question this product exists to answer. A workspace note that
scored well would arrive beside a canonical fact with nothing marking the
difference.

## Assumptions

1. **No consumer needs one ordered list badly enough to pay for it.** If one
   does, it merges client-side with the block labels still attached, which is
   strictly more information than a pre-merged list. Checked: no shipped caller
   flattens the envelope today.
2. **The block set stays small enough to reason about.** Four is reviewable. If
   it reaches eight, "which block do I read first" becomes a real burden and this
   decision is worth revisiting — not by fusing, but by deciding which blocks a
   given intent should receive at all.

## Alternatives rejected

**Fuse, and carry the authority class as a field on each item.** The obvious
compromise, and it fails on how ranked lists are consumed rather than on what
they contain. An agent given fifty ordered items reads the top few; a field on
item forty is not a signal, it is a footnote. Ordering *is* the recommendation,
and a merge makes the recommendation without regard to authority however well
each row is labelled.

**Fuse only the non-canonical blocks.** Narrower and still wrong in the same
direction: an attested ARC selection and a workspace note are not
interchangeable either, and the pair a reader is most likely to confuse is
exactly the pair this would merge.

**Rank the blocks against each other rather than the items.** Not rejected,
because it is not fusion — deciding which block an intent should receive, or
which to spend a small context window on, is a real question. It belongs to
whichever task builds per-block budgets, and it does not require flattening
anything.

## Consequences

E3 loses a task and keeps its content. The remaining work — the receipt
completeness discriminator, the synchronous intent row, trust state in the vector
index key, the adversarial-selectivity benchmark — is untouched by this.

A caller wanting one list merges client-side. That is a real cost and it is the
right place to pay it: the caller knows what it is for, and the service does not.

## Dissent

The strongest argument for merging is not convenience, and stating it as
convenience — which the assembler's docstring does — is the weakest form of this
decision.

It is this: an agent with a small context window has to choose what to read, and
this envelope hands it that choice with no help. Four blocks, each bounded
separately, and nothing saying which matters for *this* intent. Telling every
caller to merge client-side means every caller solves an authority-weighting
problem worse than the service could, inconsistently, and without access to the
trust metadata the service holds. Refusing to rank is not neutrality; it exports
a hard problem to the party least equipped to solve it, and then calls the result
the caller's decision.

The response is that a ranked list is the wrong shape for that help, not that the
need is imaginary. What an agent actually needs is a budget — how much of a small
window to spend on each block for this intent — and that is answerable without
flattening anything, because it ranks *blocks* rather than items across them.
Nobody has built it, so today the dissent is describing a real gap and the
decision is only declining one particular fix for it.

A second, narrower one: "fuse within an authority class, never across one" is a
rule about a boundary this codebase has never had to draw precisely. Observed
claims and ARC selections are different classes here, but a future block that
sits ambiguously between two would make the rule an argument rather than a check.

## What this does not claim, having checked

**Truncation is already reported, and an earlier draft of E3-T1 said otherwise.**
The claim was that a truncated block and a genuinely small one look identical.
They do not: `_block_from_outcome` records `truncated to N of M item(s)` in the
block's `reason` and marks the block `degraded`, so the live envelope says how
much was cut. The receipt's arm row carries `truncated_by_cap` besides. The task
entry asserted a gap the assembler had already closed, and this section exists
because a decision record that repeats a false premise is worse than one that
never mentioned it.

**What is genuinely open is ordering within a block.** `kept =
outcome.items[:item_cap]` takes the arm's first N, so within-block order is
whatever that arm produced and the cap removes the tail of *that* order. For an
arm that ranks, this is fine. For one that does not, the cap is removing
arbitrary items and calling the rest the answer. Which arms rank and which do not
is not recorded anywhere, and that is the next real question about ordering here
— not fusion.
