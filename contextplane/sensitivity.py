"""The handling scale: how sensitive a thing is, ordered from least to most.

One closed, ordered vocabulary, at the bottom of the import graph so every
consumer can reach it. Before this module the same four names were written out
six times in Python and five more in SQL, and the *order* — which a frozenset
cannot carry — was re-declared as a module-level tuple in three separate files
because three consumers could not import the one canonical definition. Nothing
in `tests/` asserted that any of them agreed.

**The order lives in the value.** `TIERS` is the definition; membership and rank
derive from it. A `frozenset` written out beside a `Literal` written out beside
three private tuples is four statements of one fact, and the order is the one a
reader cannot recover from the others.

**This module refuses to rank a name it does not know, and does not decide what
a caller should do about that.** `rank()` raises. Three call sites disagree today
about what an unreadable label means — two treat it as the most sensitive thing,
one refuses to answer — and they are both defensible answers to different
questions: "how should I handle something I cannot classify" and "can I compare
these two". Folding either into the vocabulary would change a redaction decision
and a cross-organization sharing ceiling as a side effect of moving a constant,
so each rule stays at its call site where it is visible.

**This is not ARC's `content_classification`.** That scale's top member is
`regulated`, not `restricted`, and it carries a cross-column CHECK requiring
encrypted storage that this one does not. The two are not spelling variants and
are deliberately not merged: merging would be a breaking change on
`SignalReference.classification` and its siblings, a data migration for stored
`restricted` rows, and the encryption rule gaining reach beyond ARC. Nor is it
ARC's `data_sensitivity`,
which is deliberately open because it is mirrored into a host's signed
attestation and hashed into the manifest-claims digest.

**The error type is local.** `contextplane.exceptions` is a layer above this one,
so raising `RegistryError` here would invert the import contract. That is the
accepted price of bottom-layer placement, and the same answer
`contextplane/ranking.py` reached.
"""

from __future__ import annotations

from typing import Final, Literal

#: The scale, least sensitive first. This tuple is the definition: every other
#: form below is derived from it, so a fifth tier is one edit.
TIERS: Final[tuple[str, ...]] = ("public", "internal", "confidential", "restricted")

#: The same names as a type. Restated rather than derived because a `Literal`
#: cannot be built from a runtime tuple, and a static checker is worth the one
#: duplication -- the conformance test asserts the two agree.
Tier = Literal["public", "internal", "confidential", "restricted"]

#: Membership, derived. Callers asking "is this a tier" want a set lookup and
#: should not pay a scan, but the answer comes from the order rather than from a
#: second hand-written list.
TIER_SET: Final[frozenset[str]] = frozenset(TIERS)

#: The most restrictive tier, derived. A call site that treats an unreadable
#: label as maximally sensitive names this rather than the literal, so adding a
#: tier above `restricted` does not silently leave those call sites one short.
MOST_RESTRICTIVE: Final[str] = TIERS[-1]


class UnknownSensitivityTier(ValueError):
    """A name was offered as a tier and this scale does not have it.

    Deliberately not a subclass of `contextplane.exceptions.RegistryError`: that
    module is a layer above this one, and importing it here would invert the
    import contract. Callers that need an HTTP status map it at their boundary.
    """


def is_tier(value: object) -> bool:
    """Whether `value` is one of the tiers. Total, and answers `False` for anything else."""
    return isinstance(value, str) and value in TIER_SET


def rank(value: str) -> int:
    """How sensitive `value` is, as an index into the scale.

    Raises on a name this scale does not have, rather than substituting one.
    A caller with a rule for unreadable labels applies it before calling, where
    the rule is visible: the two that exist today disagree, and both are right
    about the question they are asking.
    """
    try:
        return TIERS.index(value)
    except ValueError as unknown:
        raise UnknownSensitivityTier(f"{value!r} is not a sensitivity tier; this scale is {list(TIERS)}") from unknown


def at_most(value: str, ceiling: str) -> bool:
    """Whether `value` is no more sensitive than `ceiling`.

    Both must be known. A comparison against a ceiling nobody can rank is not a
    comparison that failed -- it is one that was never posed.
    """
    return rank(value) <= rank(ceiling)


__all__ = [
    "MOST_RESTRICTIVE",
    "TIERS",
    "TIER_SET",
    "Tier",
    "UnknownSensitivityTier",
    "at_most",
    "is_tier",
    "rank",
]
