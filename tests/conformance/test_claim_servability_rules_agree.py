"""The several spellings of "this claim is servable", held to agreeing.

`embedding_index.py` says this file exists:

    A conformance test holds them to agreeing rather than a shared string
    pretending they are one rule.

**It did not.** Nothing under `tests/` referenced `_SERVABLE_STATUSES` or
`_SERVABLE_AS_OF`, or asserted the two agreed about anything. A docstring
asserting a guarantee nobody built is worse than silence, because the next
author reads it and stops looking — which is how this was found, by somebody
checking whether a fourth term could safely be added for E4-T2's quarantine.

**Why the rule is deliberately spelled more than once**, which is the thing a
naive "extract a shared constant" fix would destroy. The two statements ask
different questions:

- `_SERVABLE_STATUSES` (`embedding_index.py`) asks *is this servable now, at
  this transition* — it runs inside `project_claim`, deciding whether to index
  or retract.
- `_SERVABLE_AS_OF` (`claim_serving.py`) asks *was it servable as of the
  caller's instant* — it runs in the read path, against a caller-supplied
  `as_of`.

A shared string would make one of them wrong, because the second needs
`as_of`-relative terms the first has no instant to compare against. So the
contract is *agreement on the status vocabulary*, not identity of the
predicate.

**And a third spelling that must NOT agree.** `curation_queue.py` filters
`c.status <> 'superseded'` with no `as_of` at all, because an operator must see
claims the serving path refuses — a contested or unlinked claim is precisely
what the queue exists to show. A test that forced all three to match would be
wrong, and one that ignored the third would be checking two constants in one
area. So the third is asserted to *differ*, with the reason, rather than left
out and rediscovered as a surprise.

**What this actually protects.** The status vocabulary is the half of
servability that is unconditional on both serving paths, so it is the half a new
term must join in every place or silently not apply in one. E4-T2 adds
`quarantined_at` as exactly such a term, and ADR-0016 depends on it reaching all
of them.
"""

from __future__ import annotations

import pathlib
import re

from contextplane.service.memory import claim_serving
from contextplane.service.memory import curation_queue as queue_module
from contextplane.service.retrieval import embedding_index

#: Pulled out of `_SERVABLE_AS_OF` rather than hand-written, so this test reads
#: the rule instead of restating it. A restated expectation agrees with the code
#: only until somebody edits one of them.
_STATUS_IN = re.compile(r"c\.status\s+IN\s*\(([^)]*)\)", re.IGNORECASE)


def _statuses_in_read_predicate() -> tuple[str, ...]:
    match = _STATUS_IN.search(claim_serving._SERVABLE_AS_OF)
    assert match, (
        "`_SERVABLE_AS_OF` no longer carries a `c.status IN (...)` term. Either the read "
        "path stopped filtering on status -- which would serve rejected and unlinked claims -- "
        "or it was rewritten in a shape this test cannot read. Both need a human."
    )
    return tuple(sorted(value.strip().strip("'\"") for value in match.group(1).split(",")))


def test_the_two_serving_paths_agree_on_which_statuses_are_servable() -> None:
    """The guarantee `embedding_index.py` claimed and nothing checked.

    Disagreement here is not a style problem. If the index believes a status is
    servable and the read path does not, the claim's vectors occupy candidate
    slots in every ANN query while never being returned — the recall loss
    retraction exists to prevent. If the read path believes it and the index
    does not, the claim is servable and unindexed: it can be reached
    structurally and never semantically, which reads as a relevance problem and
    is a plumbing one.
    """
    indexed = tuple(sorted(embedding_index._SERVABLE_STATUSES))
    served = _statuses_in_read_predicate()

    assert indexed == served, (
        "the index and the read path disagree about which statuses are servable:\n"
        f"  embedding_index._SERVABLE_STATUSES: {indexed}\n"
        f"  claim_serving._SERVABLE_AS_OF:      {served}\n"
        "Adding a status to one and not the other makes a claim either indexed-and-unservable "
        "(recall loss) or servable-and-unindexed (structurally reachable, semantically invisible)."
    )
    assert indexed, "both are empty, so this compared nothing"


def test_the_read_predicate_keeps_its_unconditional_and_as_of_relative_halves_apart() -> None:
    """The distinction E4-T2's quarantine turns on, pinned so it cannot blur.

    `status` is filtered unconditionally; `t_invalidated_at` is compared against
    the caller's `as_of`, deliberately — "a claim closed after the instant asked
    about was still believed then". `as_of` is caller-supplied on both
    transports, so anything that must hold for *every* caller has to be in the
    unconditional half.

    That is why ADR-0016 refuses to express quarantine as a `t_invalidated_at`
    write: quarantine at 14:00, ask `as_of=13:00`, and the claim comes back.
    This test does not enforce that decision — it pins the property the decision
    rests on, so the reasoning stops being true silently.
    """
    predicate = claim_serving._SERVABLE_AS_OF

    assert ":as_of" not in _STATUS_IN.search(predicate).group(0), (
        "the status term became `as_of`-relative; every rule that relies on status being "
        "unconditional -- including quarantine, per ADR-0016 -- is now bypassable by a "
        "caller-supplied instant"
    )
    assert "c.t_invalidated_at" in predicate and ":as_of" in predicate, (
        "`t_invalidated_at` is no longer compared against `as_of`; a historical read now "
        "hides claims that were genuinely believed at the instant asked about"
    )


def test_the_curation_queue_deliberately_does_not_share_the_serving_rule() -> None:
    """The spelling that must differ, asserted so nobody 'fixes' it.

    An operator has to see what the serving path refuses. A contested claim, an
    unlinked one, a claim with an open high-impact proposal — those are the
    queue's entire subject, and applying the serving predicate would empty it.

    Pinned negatively for the same reason the other two are pinned positively: a
    later reader tidying three near-identical predicates into one shared
    constant would silently break the queue, and the test that catches it should
    say why rather than just failing.
    """
    # The queue builds its SQL inline rather than binding it to a module
    # constant, so this reads the file. Less elegant than importing a name, and
    # the alternative -- guessing at attribute names -- silently checks nothing
    # when the guess is wrong, which is how the first draft of this test passed
    # over an empty string.
    source = pathlib.Path(queue_module.__file__).read_text(encoding="utf-8")
    assert (
        "FROM memory_claims" in source
    ), "no claim query found in curation_queue.py; this test located nothing to check"

    assert "c.status <> 'superseded'" in source, (
        "the curation queue no longer filters `status <> 'superseded'`. If it adopted the "
        "serving predicate instead, the queue is now empty of exactly the claims it exists "
        "to show -- contested, unlinked, and awaiting a high-impact decision."
    )
    assert "IN ('staged', 'superseded')" not in source, (
        "the curation queue adopted the serving path's status filter. Those two rules answer "
        "different questions and must not converge: serving asks what a caller may read, the "
        "queue asks what a curator must look at."
    )
