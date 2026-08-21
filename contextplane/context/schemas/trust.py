"""What a context item claims about itself, and who stands behind it.

Everything outside the canonical block is somebody else's assertion. The reader
is an agent that will act on it, so "where did this come from and how much weight
does it carry" cannot be optional, and cannot be inferred later from the block a
thing happened to arrive in.

**Trust metadata is complete or the item is invalid.** Not "defaulted", not
"unknown" — invalid, refused at assembly. A partially-described item is the
dangerous shape: it looks usable, and every missing field reads as the
permissive value to whoever is skimming. The one place a field may be absent is
where absence is itself the meaning, and each of those is spelled with an
explicit optional and a reason.

**Canonical items carry no trust metadata**, deliberately. The canonical block is
the registry's own answer; attaching an attribution to it would invite the
question of whether some other authority could have supplied it, and the answer
is no. That asymmetry is the contract, not an oversight.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from typing import Literal

from contextplane.sensitivity import TIER_SET, Tier

# How much weight a reader may place on an item. Ordered from strongest, but
# deliberately not an IntEnum: comparing trust levels arithmetically is how a
# "greater than" creeps in that quietly promotes an observation to an attested
# fact.
TrustLevel = Literal["attested", "asserted", "observed", "derived"]

TRUST_ATTESTED: TrustLevel = "attested"
TRUST_ASSERTED: TrustLevel = "asserted"
TRUST_OBSERVED: TrustLevel = "observed"
TRUST_DERIVED: TrustLevel = "derived"

TRUST_LEVELS: frozenset[str] = frozenset({TRUST_ATTESTED, TRUST_ASSERTED, TRUST_OBSERVED, TRUST_DERIVED})

# What kind of statement the item is making. A measurement and an intention are
# both true in different senses, and an agent that cannot tell them apart will
# plan against a wish.
AssertionKind = Literal["fact", "measurement", "intent", "policy", "annotation"]

ASSERTION_KINDS: frozenset[str] = frozenset({"fact", "measurement", "intent", "policy", "annotation"})

# Whether the underlying thing can still change. An agent caching an immutable
# item is doing something safe; caching a mutable one is not.
Mutability = Literal["immutable", "mutable", "unknown"]

MUTABILITIES: frozenset[str] = frozenset({"immutable", "mutable", "unknown"})

# The handling class the item inherits. Named here rather than imported from the
# scanner so a classification can exist for content the scanner has no detector
# for -- the two vocabularies answer different questions.
#
# Both aliases now, so the scale has one definition. `contextplane.sensitivity`
# is the bottom-layer module every consumer can reach; this file kept its own
# copy only because three others could not import it from here.
Classification = Tier

CLASSIFICATIONS: frozenset[str] = TIER_SET


class InvalidContextItem(ValueError):
    """An item cannot be assembled as described.

    A distinct type because assembly must be able to refuse one item without the
    caller confusing it for a transport failure or a validation error on the
    request. The item is dropped and the block degrades; the response still goes
    out, because a partial answer that says it is partial beats no answer.
    """


def _digest(*parts: str) -> str:
    """One digest algorithm, pinned, for every consumer.

    Length-prefixed so no two different field splits collide: without it,
    `("ab", "c")` and `("a", "bc")` hash identically, and a collision between two
    different references is indistinguishable from the same reference twice.

    SHA-256 because the tree already standardises on it for content addressing,
    and a second algorithm would mean two answers to "is this the same item".
    """
    message = b"".join(len(part.encode()).to_bytes(4, "big") + part.encode() for part in parts)
    return hashlib.sha256(message).hexdigest()


@dataclasses.dataclass(frozen=True)
class TrustMetadataV1:
    """Everything a reader needs to weigh one non-canonical item.

    Frozen and complete. `__post_init__` refuses rather than repairs: a
    silently-corrected trust record is worse than a rejected one, because the
    correction is invisible at the point somebody relies on it.
    """

    trust: TrustLevel
    # Where the statement came from, as a stable system identifier rather than a
    # display name -- display names get renamed and the provenance goes with them.
    source: str
    assertion_kind: AssertionKind
    # Who stands behind it. Distinct from `source`: a gateway may relay a claim
    # its operator never made, and conflating the two is how a relay becomes an
    # endorsement.
    authority: str
    # When the underlying thing was last known to hold. Absent only when the
    # source cannot report it -- and absence is not "now".
    freshness: datetime.datetime | None
    mutability: Mutability
    # The human or system the statement is attributed to, when there is one.
    # Absent for machine-derived items with no attributable actor.
    attribution: str | None
    classification: Classification

    def __post_init__(self) -> None:
        if self.trust not in TRUST_LEVELS:
            raise InvalidContextItem(f"unknown trust level {self.trust!r}; legal values are {sorted(TRUST_LEVELS)}")
        if self.assertion_kind not in ASSERTION_KINDS:
            raise InvalidContextItem(
                f"unknown assertion kind {self.assertion_kind!r}; legal values are {sorted(ASSERTION_KINDS)}"
            )
        if self.mutability not in MUTABILITIES:
            raise InvalidContextItem(f"unknown mutability {self.mutability!r}; legal values are {sorted(MUTABILITIES)}")
        if self.classification not in CLASSIFICATIONS:
            raise InvalidContextItem(
                f"unknown classification {self.classification!r}; legal values are {sorted(CLASSIFICATIONS)}"
            )
        # Empty strings are the failure that passes a `is not None` check. A
        # source or authority nobody can resolve is the same as none at all.
        if not self.source.strip():
            raise InvalidContextItem("trust metadata needs a source; an unattributed item cannot be weighed")
        if not self.authority.strip():
            raise InvalidContextItem("trust metadata needs an authority; a source is not automatically an endorsement")
        if self.freshness is not None and self.freshness.tzinfo is None:
            # A naive timestamp is read as local time by whoever renders it, and
            # freshness compared across timezones is worse than no freshness.
            raise InvalidContextItem("freshness must be timezone-aware; a naive timestamp is unreadable across zones")


@dataclasses.dataclass(frozen=True)
class ExternalReferenceV1:
    """A pointer to something the registry does not own.

    Creates no workflow object here. The reference is a way to name a thing in
    another system, not a way to import it -- importing would mean the registry
    starts answering for a lifecycle it does not control.
    """

    # The owning system, and the namespace inside it. Both, because the same
    # opaque id means different things in two namespaces of one system.
    source_system: str
    source_namespace: str
    kind: str
    external_id: str
    classification: Classification
    # The authority in the *external* system. Not the registry's.
    external_authority: str
    revision: str | None = None
    authorized_uri: str | None = None
    observed_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("source_system", self.source_system),
            ("source_namespace", self.source_namespace),
            ("kind", self.kind),
            ("external_id", self.external_id),
        ):
            if not value.strip():
                raise InvalidContextItem(f"external reference needs a {name}; it is part of the collision scope")
        if self.classification not in CLASSIFICATIONS:
            raise InvalidContextItem(f"unknown classification {self.classification!r}")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise InvalidContextItem("observed_at must be timezone-aware")

    def collision_key(self) -> str:
        """Two references collide only within one source *and* one kind.

        Kind is in the scope on purpose. The same id under `issue` and under
        `pull_request` in one repository is two different things, and a scope of
        source-plus-id alone would merge them -- silently, because both resolve.

        Revision is deliberately *not* in the scope: two revisions of one
        document are the same document, and scoping by revision would make an
        edit look like a new reference every time.
        """
        return _digest(self.source_system, self.source_namespace, self.kind, self.external_id)


@dataclasses.dataclass(frozen=True)
class ReceiptItemIdV1:
    """A stable name for one item inside one resolution.

    Stable means: the same item, resolved twice from unchanged inputs, gets the
    same id. That is what makes a receipt checkable rather than decorative -- a
    reader can point at a line and ask what produced it.

    Deliberately derived from block, source and item identity rather than from a
    counter: a positional id changes when an unrelated item is added, which would
    make every receipt line move whenever the block's contents shift.
    """

    block: str
    source: str
    item_key: str

    def __post_init__(self) -> None:
        for name, value in (("block", self.block), ("source", self.source), ("item_key", self.item_key)):
            if not value.strip():
                raise InvalidContextItem(f"receipt item id needs a {name}")

    def value(self) -> str:
        """The id itself, as a hex digest a receipt line can carry verbatim."""
        return _digest(self.block, self.source, self.item_key)


@dataclasses.dataclass(frozen=True)
class QualityStateV1:
    """How good the answer is, in terms a caller can act on.

    Separate from the block states because the two answer different questions:
    a block state says what happened to one arm, quality says what the caller
    should do about the response as a whole.
    """

    # Arms that were asked for but contributed nothing usable.
    degraded_blocks: tuple[str, ...]
    # Why, in the same order, so a caller can report it without guessing.
    reasons: tuple[str, ...]
    # Whether the answer is safe to cache. A degraded answer is not, because
    # caching it would outlive the failure that caused it.
    cacheable: bool

    def __post_init__(self) -> None:
        if len(self.degraded_blocks) != len(self.reasons):
            raise InvalidContextItem(
                "every degraded block needs its own reason; a mismatched pair means one of them is "
                f"describing a different block ({len(self.degraded_blocks)} blocks, {len(self.reasons)} reasons)"
            )
        if self.degraded_blocks and self.cacheable:
            raise InvalidContextItem("a degraded answer is not cacheable; caching it would outlive the failure")


__all__ = [
    "ASSERTION_KINDS",
    "CLASSIFICATIONS",
    "MUTABILITIES",
    "TRUST_ASSERTED",
    "TRUST_ATTESTED",
    "TRUST_DERIVED",
    "TRUST_LEVELS",
    "TRUST_OBSERVED",
    "AssertionKind",
    "Classification",
    "ExternalReferenceV1",
    "InvalidContextItem",
    "Mutability",
    "QualityStateV1",
    "ReceiptItemIdV1",
    "TrustLevel",
    "TrustMetadataV1",
]
