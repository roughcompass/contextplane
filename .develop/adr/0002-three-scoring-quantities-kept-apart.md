# 0002 — Three scoring quantities, named apart and never combined

**Status:** Accepted 2026-08-19

## Context

The memory system needs three different numbers, and they are routinely
conflated because all three are floats between zero and one:

- **salience** — is this episode worth keeping at all?
- **truth_confidence** — is this assertion actually true?
- **eval_score** — did this procedure work on cases it was not mined from?

They are not the same quantity and are not on comparable scales. Salience is a
utility estimate; truth_confidence is a probability; eval_score is a measured
pass rate with a sampling interval around it. Averaging them, comparing them, or
feeding them to one ranker produces bugs that are very hard to trace, because
every intermediate value still looks like a plausible score.

`contextplane/types.py` currently declares `SearchResult.score: float` — a bare,
unqualified name of exactly the kind that invites a later reader to treat it as
interchangeable with any other score. Today it holds a fused retrieval rank, an
entirely different quantity from all three above. Eighteen call sites read it,
no UI code does, and it appears twice in the committed contract.

## Decision

Each quantity gets a distinct name wherever it is stored or returned:
`salience`, `truth_confidence`, `eval_score`. No field named plain `score`
survives in a schema that any of them can reach.

`SearchResult.score` is renamed to `fused_rank_score`, naming the quantity
rather than the role. The rename lands before any of the three arrive, because
renaming a field with two consumers is cheap and renaming one with five is the
kind of task that gets deferred forever.

No code path may average, sum, compare, or threshold one of these against
another. Where a decision genuinely needs two of them — promote only what is
both salient and probably true — it applies two separate thresholds and states
both, rather than combining them into a single number first.

## Assumptions

- The three will all exist. If salience or eval_score is never built, this
  costs one rename that was independently justified.
- Renaming a contract field is acceptable now: the UI consumes the contract
  through a pinned, regenerated client, so the change is a deliberate
  coordinated bump rather than a break.

## Alternatives rejected

- **One `score` field with a `kind` discriminator.** Keeps the conflation
  possible and makes it harder to see: a ranker that ignores the discriminator
  compiles and runs.
- **Leave `SearchResult.score` alone and only name the new three carefully.**
  The existing bare name is the precedent that teaches the next author a plain
  `score` is acceptable.
- **A shared numeric type with units.** More machinery than the problem needs;
  distinct field names achieve the same separation and are readable in a JSON
  response.

## Consequences

One contract change and eighteen call-site updates, plus a UI contract-pin bump
when convenient. Afterwards, a reader encountering `truth_confidence` cannot
mistake it for a retrieval rank, and a ranker cannot silently consume the wrong
quantity.

## Dissent

None recorded. The operator raised the `SearchResult.score` case directly and
agreed it should be fixed before the other three land.
