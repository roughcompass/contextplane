"""The three configurations under test, built from injected reads.

One ablation and two treatments, differing in exactly one thing: what the
workspace arm does. The canonical, governance, claim and resume paths are
identical across all three, which is what makes a difference in the score
attributable to workspace recall rather than to a differently-configured system.

**The reads are injected, never imported.** This module composes arms over a
`WorkspaceSource` the caller supplies. That is the same split the assembler
makes for the same reason -- every failure path stays reachable from a fixture --
and it is also what keeps an evaluation harness from taking a dependency on the
service it is evaluating.

**Authorization is the candidate set, not a filter over it.** The semantic
treatment resolves the authorized checkpoints *first* and scans only those. It
never scores, counts, ranks or times an item the caller may not see, because no
such item is ever in the array being scanned. A post-filter design would compute
a similarity for every checkpoint in the tenant and drop the unauthorized ones
afterwards, and the difference is observable from outside: a score distribution
that shifts with content the caller cannot read, a count taken before filtering,
and a latency that tracks how much of the tenant's corpus is not theirs.

**Exact, not approximate.** The scan compares against every authorized candidate.
The question under test is whether semantic matching finds material lexical
matching misses; an approximate index would fold in a second question -- how well
that index approximates -- and a negative result could not be attributed to
either.
"""

from __future__ import annotations

import dataclasses
import math
from typing import TYPE_CHECKING, Protocol

from contextplane.context.assembler import ArmOutcome, Exclusion, ordered_items
from contextplane.context.evaluation.protocol import (
    CONFIG_BASELINE,
    CONFIG_TREATMENT_A,
    CONFIG_TREATMENT_B,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from contextplane.context.assembler import ContextArm
    from contextplane.context.evaluation.scenarios import Scenario
    from contextplane.context.schemas.envelope import ContextItemV1

#: How many semantic matches the exact scan may contribute. The same ceiling the
#: lexical arm applies, for the same reason: an arm that could be asked for an
#: unbounded page would let one request decide how much work every other request
#: waits behind.
SEMANTIC_LIMIT = 20

#: Cosine similarity a candidate must reach to be served. A floor rather than
#: pure top-k: top-k alone always returns something, so a scenario with no
#: semantically related material would be answered with the least-unrelated
#: checkpoint in the task and scored as a precision failure that is really a
#: missing threshold.
SEMANTIC_FLOOR = 0.30


@dataclasses.dataclass(frozen=True)
class Candidate:
    """One authorized checkpoint the semantic scan may consider.

    Carries the item it would become rather than the row it came from: the scan
    decides which candidates to serve, not what a served item looks like, and
    building items here would put a second item-construction path beside the
    production one.
    """

    item_key: str
    text: str
    item: ContextItemV1


class Embedder(Protocol):
    """The narrow slice of an embedding model this harness needs.

    Matches the shape the production embedders already expose, so a real model
    can be passed straight in and a deterministic one substituted in tests
    without either knowing about the other.
    """

    model_version: str

    def encode(self, texts: list[str]) -> Sequence[Sequence[float]]:
        """Embed each text, in order."""
        ...


class WorkspaceSource(Protocol):
    """The three reads a configuration is built from.

    Deliberately three separate methods rather than one parameterised read. The
    third is not a variant of the first two: it returns the *authorized set*,
    which is an input to candidate generation rather than a set of results, and
    collapsing it into a search call is precisely how authorization becomes a
    filter applied afterwards.
    """

    async def lexical(self, scenario: Scenario) -> ArmOutcome:
        """Lexical recall over the caller's own tasks."""
        ...

    async def reference(self, scenario: Scenario) -> ArmOutcome:
        """Recall by the external work item a checkpoint cited."""
        ...

    async def authorized_candidates(self, scenario: Scenario) -> tuple[Candidate, ...]:
        """Every checkpoint this caller may see, resolved before any scoring."""
        ...


def _merge(*outcomes: ArmOutcome) -> ArmOutcome:
    """Union two or more arm outcomes into one block's worth of facts.

    Deduplicated by item key, because a checkpoint found both lexically and by
    reference is one item served once -- counting it twice would inflate the
    denominator of a precision the arm did not actually harm.
    """
    seen: dict[str, ContextItemV1] = {}
    exclusions: dict[str, Exclusion] = {}
    truncated = False
    freshest = None
    reasons: list[str] = []
    for outcome in outcomes:
        for item in outcome.items:
            seen.setdefault(item.receipt_item_id.item_key, item)
        for exclusion in outcome.exclusions:
            exclusions.setdefault(exclusion.item_key, exclusion)
        truncated = truncated or outcome.truncated
        if outcome.fresh_as_of is not None and (freshest is None or outcome.fresh_as_of > freshest):
            freshest = outcome.fresh_as_of
        if outcome.degraded_reason:
            reasons.append(outcome.degraded_reason)

    # An item that one arm served and another withheld is served: the withholding
    # arm declined to return it, it did not revoke the other arm's authority to.
    for key in seen:
        exclusions.pop(key, None)

    return ArmOutcome(
        items=ordered_items(tuple(seen.values())),
        exclusions=tuple(exclusions.values()),
        truncated=truncated,
        fresh_as_of=freshest,
        degraded_reason="; ".join(reasons) if reasons else None,
    )


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, with a zero vector scoring zero rather than raising.

    A zero vector is what a stub embedder produces, and it genuinely carries no
    direction -- so it is similar to nothing. Raising here would turn a
    misconfigured embedder into a batch-invalidating infrastructure error, which
    would hide the misconfiguration behind a rerun.
    """
    if len(left) != len(right):
        raise ValueError(f"cannot compare a {len(left)}-dimension vector with a {len(right)}-dimension one")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    if norm == 0.0:
        return 0.0
    return dot / norm


def exact_scan(
    *,
    query: str,
    candidates: Sequence[Candidate],
    embedder: Embedder,
    limit: int = SEMANTIC_LIMIT,
    floor: float = SEMANTIC_FLOOR,
) -> ArmOutcome:
    """Score every authorized candidate against the query, and serve the best.

    The candidate sequence *is* the authorization boundary. Nothing here
    re-checks it, and nothing here can widen it: there is no fallback to a
    broader corpus when the scan finds too few matches, because "too few
    authorized matches" is a correct answer and widening it would answer a
    question the caller was not entitled to ask.
    """
    if not candidates:
        return ArmOutcome()

    vectors = embedder.encode([query, *(c.text for c in candidates)])
    if len(vectors) != len(candidates) + 1:
        raise ValueError(
            f"the embedder returned {len(vectors)} vector(s) for {len(candidates) + 1} text(s); "
            "a scan cannot align scores to candidates it cannot count"
        )
    query_vector = vectors[0]

    scored = [
        (cosine(query_vector, vector), candidate) for vector, candidate in zip(vectors[1:], candidates, strict=True)
    ]
    # Ties break on item key rather than on input order, so two runs over the
    # same authorized set cannot differ by however the rows were fetched.
    scored.sort(key=lambda pair: (-pair[0], pair[1].item_key))

    kept = [candidate for similarity, candidate in scored if similarity >= floor]
    truncated = len(kept) > limit
    return ArmOutcome(
        items=ordered_items(tuple(c.item for c in kept[:limit])),
        truncated=truncated,
    )


def _empty_arm() -> ContextArm:
    async def arm() -> ArmOutcome:
        # Truthfully empty, not failed. The ablation removes the arm's content,
        # not the arm: a failed block would degrade the envelope and measure a
        # broken system rather than a system without workspace memory.
        return ArmOutcome()

    return arm


def workspace_arm_for(
    configuration: str,
    *,
    scenario: Scenario,
    source: WorkspaceSource,
    embedder: Embedder | None = None,
) -> ContextArm:
    """The workspace arm one configuration runs with.

    The one thing that varies across the three runs. Everything else the
    assembler is handed is identical by construction, because it is the same
    mapping with this one key replaced.
    """
    if configuration == CONFIG_BASELINE:
        return _empty_arm()

    if configuration == CONFIG_TREATMENT_A:

        async def lexical_and_reference() -> ArmOutcome:
            return _merge(await source.lexical(scenario), await source.reference(scenario))

        return lexical_and_reference

    if configuration == CONFIG_TREATMENT_B:
        if embedder is None:
            raise ValueError(f"{CONFIG_TREATMENT_B} needs an embedder; a semantic treatment with no model is baseline")
        model = embedder

        async def with_semantic() -> ArmOutcome:
            # The authorized set is resolved before anything is scored, and the
            # scan sees nothing else.
            candidates = await source.authorized_candidates(scenario)
            semantic = exact_scan(query=scenario.term, candidates=candidates, embedder=model)
            return _merge(await source.lexical(scenario), await source.reference(scenario), semantic)

        return with_semantic

    raise ValueError(f"unknown configuration {configuration!r}")


__all__ = [
    "SEMANTIC_FLOOR",
    "SEMANTIC_LIMIT",
    "Candidate",
    "Embedder",
    "WorkspaceSource",
    "cosine",
    "exact_scan",
    "workspace_arm_for",
]
