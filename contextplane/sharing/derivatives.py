"""Bind every derived artefact to the grants that produced it, and fail closed when stale.

A derivative is anything computed from rows the caller may not all see: a search
index, a closure cache, a ranking, a count. Each is a place where hidden data can
leak without ever being returned — a record nobody is allowed to read still
changes a rank, still increments a count, still shortens a traversal, and each of
those is observable.

So a derivative is not scoped to a tenant. It is scoped to a **pair** of tenants
and to the exact grant set in force between them, because two callers in the same
destination tenant may legitimately see different things, and the same caller sees
different things before and after a revocation. A cache keyed by tenant alone
would serve one caller's grants to another.

**Revocation invalidates by key, not by sweep.** The scope key changes the moment
the grant set changes, so a revoked grant's derivatives become unreachable
immediately — no purge has to complete first. The purge still runs, to reclaim
the space, but correctness does not wait for it.

**Reads fail closed while stale.** A derivative whose recorded scope no longer
matches the grants in force is not served, and not silently recomputed either:
recomputing inline would make a revocation's cost land on whichever unlucky
request noticed, and serving it would hand back a result built under permissions
that have been withdrawn.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from collections.abc import Sequence
from typing import Final

from contextplane.sharing.grants import CrossOrgGrant

#: Bumped when the derivation itself changes shape. Two artefacts built by
#: different algorithms are not interchangeable even under identical grants, and
#: a version in the key is what stops the newer one being served from the older
#: one's slot after a deploy.
DERIVATION_VERSION: Final = "derivative-scope-1"


class StaleDerivative(RuntimeError):
    """A derivative was built under a grant set that is no longer in force.

    Raised rather than returning empty: an empty result is indistinguishable from
    "nothing matched", and a caller that cannot tell those apart will cache the
    emptiness.
    """


@dataclasses.dataclass(frozen=True)
class DerivativeScope:
    """The identity of one derived artefact: whose data, for whom, under what.

    Frozen and compared by value. A scope that could be mutated after a lookup is
    a scope that could be widened between the check and the read, which is the
    same defect as an unlocked count-then-write in a different costume.
    """

    source_tenant_id: uuid.UUID
    destination_tenant_id: uuid.UUID
    grant_digest: str
    derivation_version: str = DERIVATION_VERSION

    @property
    def cache_key(self) -> str:
        """A stable key for this scope.

        Both tenant ids appear, in fixed order, so a key cannot collide across a
        reversed pair — `A shares to B` and `B shares to A` are different grants
        and must not share a cache slot.
        """
        return f"{self.derivation_version}:" f"{self.source_tenant_id}:{self.destination_tenant_id}:{self.grant_digest}"


def grant_digest(grants: Sequence[CrossOrgGrant], *, at: datetime.datetime) -> str:
    """A digest over exactly the grants in force at `at`.

    Over the *contents*, not the ids: a grant whose operations or types were
    narrowed keeps its id, and a digest over ids alone would let the old
    derivative survive a narrowing. Sorted first, so the digest is a property of
    the grant set and not of the order a query returned it in.

    The empty set has a digest too, rather than being a sentinel: "this pair has
    no grants" is a real scope that a derivative can legitimately be built for
    (it contains nothing shared), and giving it a value means the key never has to
    be read as three-valued.
    """
    material = sorted(
        "|".join(
            [
                str(grant.grant_id),
                grant.grant_kind,
                ",".join(sorted(grant.profile_types)),
                ",".join(sorted(grant.relationship_types)),
                ",".join(sorted(grant.allowed_operations)),
                grant.classification_ceiling,
            ]
        )
        for grant in grants
        if grant.is_in_force(at)
    )
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def scope_for(
    *,
    source_tenant_id: uuid.UUID,
    destination_tenant_id: uuid.UUID,
    grants: Sequence[CrossOrgGrant],
    at: datetime.datetime,
) -> DerivativeScope:
    """The scope a derivative built now, for this pair, would carry."""
    return DerivativeScope(
        source_tenant_id=source_tenant_id,
        destination_tenant_id=destination_tenant_id,
        grant_digest=grant_digest(grants, at=at),
    )


def assert_fresh(recorded: DerivativeScope, current: DerivativeScope) -> None:
    """Refuse to serve a derivative whose scope no longer matches.

    Fails closed rather than recomputing inline. Recomputing would put a
    revocation's cost on whichever request happened to notice first, and serving
    the stale artefact would hand back a result built under permissions somebody
    has withdrawn — which is the leak this whole module exists to prevent, arriving
    through the cache instead of through the query.
    """
    if recorded != current:
        msg = (
            "this derivative was built under a grant set that is no longer in force; it is not served and not "
            "recomputed inline — the rebuild is scheduled, and until it lands this read fails closed"
        )
        raise StaleDerivative(msg)


def is_fresh(recorded: DerivativeScope, current: DerivativeScope) -> bool:
    """Whether a derivative may be served, for a caller that wants to branch."""
    return recorded == current


__all__ = [
    "DERIVATION_VERSION",
    "DerivativeScope",
    "StaleDerivative",
    "assert_fresh",
    "grant_digest",
    "is_fresh",
    "scope_for",
]
