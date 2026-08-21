"""Decide whether a grant permits one cross-organization operation.

Separated from `sharing/grants.py` on purpose. A module that could both widen a
grant and authorize against it could approve its own request in one breath, and no
row afterwards would show that it had.

**Every verdict is re-resolved from the row, never taken from a caller.** The
decision function takes the grants it was handed by a fresh read and computes an
answer; there is no parameter through which a caller can assert that it is
already permitted. That is what makes revocation immediate — the next decision
reads the revoked row rather than a cached verdict.

**Omission denies.** A grant that does not name the operation, the profile type or
the relationship type does not permit it. This is not defensive coding: the
selectors are documents whose shape the profile defines, so an absent entry means
the grant's author did not say yes, and treating silence as permission would make
every unstated case an accidental grant.

**A denial says nothing about what exists.** `Decision.reason` is a fixed code
from a closed set, and none of them distinguish "no such entity" from "not shared
with you". A caller that could tell those apart could enumerate another
organization's identifiers by watching which denials changed.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence
from typing import Final

from contextplane.sensitivity import at_most, is_tier
from contextplane.sharing.grants import CrossOrgGrant

#: The closed set of denial reasons. Deliberately coarse: a reason precise enough
#: to be useful to an operator is precise enough to enumerate a neighbour's data,
#: so the detail belongs in the audit record and not in the response.
NO_GRANT: Final = "no_active_grant"
OPERATION_NOT_GRANTED: Final = "operation_not_granted"
TYPE_NOT_GRANTED: Final = "type_not_granted"
CLASSIFICATION_ABOVE_CEILING: Final = "classification_above_ceiling"

DENIAL_REASONS: Final[frozenset[str]] = frozenset(
    {NO_GRANT, OPERATION_NOT_GRANTED, TYPE_NOT_GRANTED, CLASSIFICATION_ABOVE_CEILING}
)

#: Classification order, least to most sensitive. A ceiling is a refusal, not a
#: filter: content above it is not shared at all rather than shared redacted.


@dataclasses.dataclass(frozen=True)
class Decision:
    """Whether one cross-organization operation is permitted, and under which grant.

    `grant_id` is present only when permitted. A denial that named a grant would
    tell the caller a grant exists, which is itself information about the other
    organization.
    """

    permitted: bool
    reason: str | None = None
    grant_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.permitted and self.reason is not None:
            msg = "a permitted decision carries no denial reason"
            raise ValueError(msg)
        if not self.permitted:
            if self.reason not in DENIAL_REASONS:
                msg = f"unknown denial reason {self.reason!r}; legal: {', '.join(sorted(DENIAL_REASONS))}"
                raise ValueError(msg)
            if self.grant_id is not None:
                msg = "a denial names no grant; saying which one failed reveals that one exists"
                raise ValueError(msg)


def authorize(
    grants: Sequence[CrossOrgGrant],
    *,
    operation: str,
    at: datetime.datetime,
    profile_type: str | None = None,
    relationship_type: str | None = None,
    classification: str | None = None,
) -> Decision:
    """The one place a cross-organization operation is permitted or refused.

    Takes grants rather than reading them, so the caller controls the transaction
    the read happens in — a decision made against rows read outside the
    transaction that acts on them is a decision about a state that may already
    have changed.

    The first grant that permits the operation wins. Grants are additive by
    nature: two narrow grants together permit what neither does alone, and
    requiring one grant to cover everything would make an organization revoke and
    re-issue rather than add.
    """
    if not grants:
        return Decision(permitted=False, reason=NO_GRANT)

    in_force = [grant for grant in grants if grant.is_in_force(at)]
    if not in_force:
        return Decision(permitted=False, reason=NO_GRANT)

    # Tracked so the denial names the *nearest* failure rather than always the
    # first check — an operator reading the audit record needs to know whether the
    # grant was too narrow or the content too sensitive.
    nearest = NO_GRANT
    for grant in in_force:
        if operation not in grant.allowed_operations:
            nearest = OPERATION_NOT_GRANTED
            continue
        if not _type_permitted(grant, profile_type=profile_type, relationship_type=relationship_type):
            nearest = TYPE_NOT_GRANTED
            continue
        if not _within_ceiling(grant.classification_ceiling, classification):
            nearest = CLASSIFICATION_ABOVE_CEILING
            continue
        return Decision(permitted=True, grant_id=grant.grant_id)

    return Decision(permitted=False, reason=nearest)


def _type_permitted(grant: CrossOrgGrant, *, profile_type: str | None, relationship_type: str | None) -> bool:
    """Whether this grant reaches the type in question.

    An empty selector list means the grant names no types and therefore reaches
    none. The opposite reading — empty means all — is the one that turns a
    half-filled grant into an unlimited one.
    """
    if profile_type is not None and profile_type not in grant.profile_types:
        return False
    return not (relationship_type is not None and relationship_type not in grant.relationship_types)


def _within_ceiling(ceiling: str, classification: str | None) -> bool:
    """Whether content of this classification may cross under this ceiling.

    An unknown classification is refused rather than ranked. A value nobody
    recognises cannot be compared, and treating it as the lowest would let a typo
    carry restricted content across a boundary.
    """
    if classification is None:
        return True
    # An unreadable label refuses rather than ranking. That rule stays here
    # rather than in the vocabulary: this is a cross-organization sharing
    # ceiling, and "I cannot compare these" is the right answer to it even
    # though `context/evaluation/judge.py` is right to answer the opposite
    # question by assuming the worst.
    if not is_tier(ceiling) or not is_tier(classification):
        return False
    return at_most(classification, ceiling)


__all__ = [
    "CLASSIFICATION_ABOVE_CEILING",
    "DENIAL_REASONS",
    "NO_GRANT",
    "OPERATION_NOT_GRANTED",
    "TYPE_NOT_GRANTED",
    "Decision",
    "authorize",
]
