# Frozen pilot scenarios

Six changes that ran through the delivery-lifecycle surfaces during the pilot,
frozen here as the regression corpus. One file per change. They exist so the
lifecycle contract is pinned by situations that actually occurred rather than by
situations a test author found convenient to imagine.

## What a scenario file claims

Each file records five things about one change, and the corpus test checks all
five:

- **`expected_source_coverage`** — the state each of the four blocks reached.
  Not "it worked": `empty` and `degraded` are recorded outcomes here, and a
  scenario that reached `degraded` is the one worth keeping.
- **`trust_labels`** — the label each block's items carried. `canonical` is
  `null` in every scenario because canonical items carry no trust metadata by
  contract, and the corpus test asserts that rather than trusting the fixture:
  a scenario claiming a trust label on `canonical`, or omitting one on any other
  block, fails.
- **`prior_learning`** — whether the change retrieved reviewed learning from an
  earlier one, how the retrieval was confirmed, and whether the participant
  judged it useful. `judged_useful: false` is recorded as faithfully as `true`.
- **`refusal_or_degradation`** — every refusal or degradation the change hit.
  A change that hit none says so explicitly with `"kind": "none"`, so silence
  is a claim somebody made rather than a field somebody forgot.
- **`cardinality`** — the counts. Receipts, handoffs, external identities per
  system, and the joined/unjoined split on outcomes.

## Why the counts are asserted rather than described

An empty corpus directory must **fail** the corpus test, not pass it. A suite
that only parametrizes over whatever files it discovers reports success against
no files at all, and the failure mode is silent: somebody moves the directory,
every scenario stops running, and the gate stays green. So the corpus test
asserts a floor on the file count independently of the per-file checks, and
asserts that the per-file checks actually ran over that many files.

The same reasoning applies inside a file. A scenario whose `refusal_or_degradation`
list is empty would satisfy any check that merely iterates it, which is why the
absent case is spelled `"kind": "none"` and the corpus as a whole is required to
contain at least one real refusal and at least one real degradation. A corpus
of six untroubled changes would pin nothing the happy path does not already pin.

## Anonymization, and what is deliberately not here

Teams and repositories are named for the role they play in the dependency
relationship — one publisher, two consumers of what it publishes — because that
relationship is the property the scenarios depend on and the participants'
identities are not. The pilot's own change identifiers are **not** carried here
in any form. The mapping from these scenarios back to the changes they came from
lives in the pilot's own record; it is not derivable from this directory, which
is the point.

No raw transcripts and no private planning identifiers enter these files. One
further change from the same pilot is **excluded** from this corpus: its
checkpoint carried a pasted vendor-advisory excerpt whose license does not permit
redistribution, and admission review withheld it. Six approved records against a
floor of five leaves a margin of one — if a later admission review withdraws
another, this corpus is short and must say so rather than grow a replacement.

Two things the pilot measured are deliberately **absent** from every file:

- **Per-change terminal CI conclusion.** The pilot's record fixes these only in
  aggregate across all seven changes, so assigning one to each of six approved
  scenarios would mean inventing the assignment. The aggregate is not a
  per-scenario fact and is not recorded as one.
- **Wall-clock durations.** They include review latency and calendar, so pinning
  them would pin the pilot's schedule rather than the contract's behavior.
