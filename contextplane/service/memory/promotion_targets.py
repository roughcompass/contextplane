"""Where a claim goes when it becomes canonical.

A claim is a predicate and a typed value. The canonical graph stores three kinds of
thing: attributes (typed properties of one entity), edges (typed relationships between
two), and facts (titled prose artifacts). Nothing in the claim itself says which of the
three it should become, so the mapping has to be declared.

**The mapping is deployment-wide, not per-tenant.** The predicate vocabulary is shared
across tenants, so letting a tenant redefine what one of its predicates canonically
*means* would leave the same predicate naming two different things in two tenants --
and every cross-tenant read, every authority comparison, and every audit row would then
be ambiguous about which meaning it recorded. A tenant can already change how well
extraction finds claims, whether a predicate promotes automatically, and what its
review thresholds are. It cannot change what a predicate is.

**The mapping is not derived from the value type.** A URL could reasonably be an
attribute or a fact; an entity reference is only an edge if the predicate actually
denotes a relationship. Deriving the target from the type would be guessing, and the
guess is discovered wrong only after canonical rows exist.

**Every promotion records the mapping version it used.** A mapping found to be wrong
cannot be silently corrected: rows written under the old one still exist and still say
what they said. Recording the version is what makes a later remediation able to find
exactly the rows written under the mistake.

**No shipped predicate maps to a fact.** The only prose predicate is barred from
promotion outright, because prose has no typed canonical target -- which is the same
reason facts are unreachable here. Rather than ship a fact-writing path that nothing
exercises, this module supports the two target kinds the ontology actually reaches.
Adding a fact-valued predicate means adding its mapping entry and the write path
together, with tests, which is the point of keeping the extension explicit.
"""

from __future__ import annotations

import dataclasses
from typing import Final

from contextplane.service.memory.claim_ontology import ONTOLOGY

# Bumped when any entry below changes meaning. Recorded on every promotion so a
# mapping later found wrong can be traced to exactly the rows it produced.
MAPPING_VERSION: Final[int] = 1

TARGET_ATTRIBUTE: Final[str] = "attribute"
TARGET_EDGE: Final[str] = "edge"

# Why a predicate has no canonical target. Distinct reasons, because "we have not
# mapped it yet" and "it can never be mapped" call for different responses from a
# curator looking at an unpromotable claim.
UNMAPPED_PROSE: Final[str] = "prose has no typed canonical target"


@dataclasses.dataclass(frozen=True)
class PromotionTarget:
    """The canonical write a claim's promotion would perform."""

    kind: str
    # For an attribute, the `attributes.key` written. For an edge, the `edges.rel`.
    # Deliberately the predicate's own name in every current case: a rename layer
    # between the two vocabularies would be one more place for them to drift apart.
    key: str
    # Set-valued predicates map to a canonical target that may hold several values at
    # once. A single-valued one supersedes whatever it replaces. Carried here rather
    # than re-read from the ontology so the write path has one source for the
    # decision.
    multi_valued: bool


_TARGET_BY_VALUE_TYPE: Final[dict[str, str]] = {
    "entity_ref": TARGET_EDGE,
}

_BARRED_VALUE_TYPES: Final[frozenset[str]] = frozenset({"prose"})


def _build() -> dict[str, PromotionTarget]:
    mapping: dict[str, PromotionTarget] = {}
    for seed in ONTOLOGY:
        if seed.value_type in _BARRED_VALUE_TYPES:
            continue
        mapping[seed.value] = PromotionTarget(
            kind=_TARGET_BY_VALUE_TYPE.get(seed.value_type, TARGET_ATTRIBUTE),
            key=seed.value,
            multi_valued=seed.value_cardinality == "multi",
        )
    return mapping


TARGETS: Final[dict[str, PromotionTarget]] = _build()


def target_for(predicate: str) -> PromotionTarget | None:
    """The canonical target for a predicate, or None if it has none.

    None is a real answer, not a failure. A claim with no canonical target stays
    useful in staging and in serving; it simply cannot become canonical.
    """
    return TARGETS.get(predicate)


def unmapped_reason(predicate: str) -> str | None:
    """Why a predicate has no target, for a curator reading an unpromotable claim.

    Returns None when the predicate does have a target, so a caller cannot report a
    reason for a claim that is in fact promotable.
    """
    if predicate in TARGETS:
        return None
    for seed in ONTOLOGY:
        if seed.value == predicate and seed.value_type in _BARRED_VALUE_TYPES:
            return UNMAPPED_PROSE
    return f"predicate {predicate!r} is not in the ontology"
