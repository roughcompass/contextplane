"""The source-authority ladder: where a claim came from, as an ordered vocabulary.

Authority answers two questions about a claim's provenance: whether the asserting
tenant owns the thing being described, and how reproducible the step from artefact
to typed triple was. It is never supplied by a caller -- a producer that could name
its own authority would name the highest one -- and it is not confidence. Authority
decides which claim supersedes which; confidence says how likely a claim is to be
right. Independent sources agreeing raises the second and never the first.

Its own module because two layers legitimately need it and neither should depend on
the other: the write path derives a tier from an authenticated principal, and
scoring weights a tier. Keeping the ladder here means there is exactly one ordering
over these seven values, which is the property every rule built on it assumes.

**Ordered strongest first, ownership-major.** A claim from a tenant that does not
own the subject can never outrank one from the tenant that does, at any derivation
tier. That ordering is what makes flattening two axes into one ladder lossless
rather than a convenient simplification.

**Cross-tenant supersession must gate on the tenant columns, not on rank.**
"Different tenant" routes a proposal; "lower rank" contests and can supersede. A
single ordinal cannot express both, which is why both tenant columns are persisted
alongside the tier rather than collapsed into it.
"""

from __future__ import annotations

AUTHORITY_OWNER_HUMAN = "owner_human"
AUTHORITY_OWNER_EXTRACTION = "owner_extraction"
AUTHORITY_OWNER_INFERENCE = "owner_inference"
AUTHORITY_OBSERVER_HUMAN = "observer_human"
AUTHORITY_OBSERVER_EXTRACTION = "observer_extraction"
AUTHORITY_OBSERVER_INFERENCE = "observer_inference"
# No subject means no owner to compare the author against, so the standing axis is
# undefined. Saying that is better than guessing a tier which turns out to have
# been wrong once a curator links the claim -- and nothing marks a guess as stale.
AUTHORITY_UNATTRIBUTED = "unattributed"

SOURCE_AUTHORITY_ORDER: tuple[str, ...] = (
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_OWNER_EXTRACTION,
    AUTHORITY_OWNER_INFERENCE,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_INFERENCE,
    AUTHORITY_UNATTRIBUTED,
)

#: Rank 0 is strongest. Compare by rank, never by string order.
SOURCE_AUTHORITY_RANK: dict[str, int] = {
    value: rank for rank, value in enumerate(SOURCE_AUTHORITY_ORDER)
}

# Derivation tiers. Weakest is the highest number so the weakest link across a
# claim's evidence is a plain `max()`.
DERIVATION_HUMAN = "human"
DERIVATION_EXTRACTION = "extraction"
DERIVATION_INFERENCE = "inference"

DERIVATION_RANK: dict[str, int] = {
    DERIVATION_HUMAN: 0,
    DERIVATION_EXTRACTION: 1,
    DERIVATION_INFERENCE: 2,
}
DERIVATION_BY_RANK: dict[int, str] = {rank: name for name, rank in DERIVATION_RANK.items()}

AUTHORITY_BY_AXES: dict[tuple[bool, str], str] = {
    (True, DERIVATION_HUMAN): AUTHORITY_OWNER_HUMAN,
    (True, DERIVATION_EXTRACTION): AUTHORITY_OWNER_EXTRACTION,
    (True, DERIVATION_INFERENCE): AUTHORITY_OWNER_INFERENCE,
    (False, DERIVATION_HUMAN): AUTHORITY_OBSERVER_HUMAN,
    (False, DERIVATION_EXTRACTION): AUTHORITY_OBSERVER_EXTRACTION,
    (False, DERIVATION_INFERENCE): AUTHORITY_OBSERVER_INFERENCE,
}

__all__ = [
    "AUTHORITY_BY_AXES",
    "AUTHORITY_OBSERVER_EXTRACTION",
    "AUTHORITY_OBSERVER_HUMAN",
    "AUTHORITY_OBSERVER_INFERENCE",
    "AUTHORITY_OWNER_EXTRACTION",
    "AUTHORITY_OWNER_HUMAN",
    "AUTHORITY_OWNER_INFERENCE",
    "AUTHORITY_UNATTRIBUTED",
    "DERIVATION_BY_RANK",
    "DERIVATION_EXTRACTION",
    "DERIVATION_HUMAN",
    "DERIVATION_INFERENCE",
    "DERIVATION_RANK",
    "SOURCE_AUTHORITY_ORDER",
    "SOURCE_AUTHORITY_RANK",
]
