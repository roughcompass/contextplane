"""Shared state vocabulary, digests, and row-loading primitives for the
artifact write paths.

Both halves of `ArtifactService` -- the lifecycle transitions in
`artifact.py` and the revision-creation path in
`artifact_materialisation.py` -- need to agree on the same closed set of
`lifecycle_state` values, the same rule for which transitions between them
are legal, and the same answer to "does this approval evidence actually name
this revision." Defining any of those twice, once per file, is how the two
definitions drift apart; keeping one authoritative version here, with both
halves importing it, is what keeps them from being able to.

This module is deliberately the base of the dependency graph: it imports
nothing from `artifact.py` or `artifact_materialisation.py`, so neither of
those two risks a cycle by depending on it. `artifact.py` re-exports the
lifecycle vocabulary (`LIFECYCLE_*`, `ArtifactLifecycleError`) because
callers outside this package -- workers, other services, tests -- reasonably
expect "the lifecycle module's vocabulary" to come from the lifecycle
module, even though it is defined here for that acyclic-import reason.

`applicability_snapshot`/`applicability_digest` compute the dedup key for a
mandatory obligation. Two independent producers must build byte-identical
output for the same applicability: the revision-creation path builds it
from a draft rule before anything is written, and the activation path
(`artifact.py`'s obligation refresh) rebuilds it from the rule rows it just
read back. One shared implementation is what makes that agreement possible
instead of merely assumed.

`_load_artifact` and `_lock_family` are the row-loading and family-locking
primitives every write in either half depends on: there is no way to check
whether a transition is legal, an approval binds the right revision, or an
applicability digest is stale without first having the authoritative,
correctly locked row to check it against.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from registry.arc.service.authorization import ArtifactScope
from registry.arc.types import AuthorityScope
from registry.exceptions import LifecycleError, NotFoundError, RegistryError, ValidationError

# The lifecycle states a revision can hold, and the only transitions allowed
# between them. Expressed as data rather than a chain of `if` statements so
# an illegal transition is a lookup failure rather than a branch someone
# forgot to write.
LIFECYCLE_DRAFT = "draft"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_SUPERSEDED = "superseded"
LIFECYCLE_REVOKED = "revoked"
LIFECYCLE_EXPIRED = "expired"

_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    LIFECYCLE_DRAFT: frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_REVOKED}),
    # An active revision can be superseded by a successor, revoked outright,
    # or expire. It can never go back to draft: agents have already been told
    # to obey it, and un-telling them is not a state change.
    LIFECYCLE_ACTIVE: frozenset({LIFECYCLE_SUPERSEDED, LIFECYCLE_REVOKED, LIFECYCLE_EXPIRED}),
    LIFECYCLE_SUPERSEDED: frozenset({LIFECYCLE_REVOKED}),
    LIFECYCLE_EXPIRED: frozenset({LIFECYCLE_REVOKED, LIFECYCLE_ACTIVE}),
    # Terminal. A revoked revision is evidence of what was once in force and
    # must stay readable, but it can never bind anything again.
    LIFECYCLE_REVOKED: frozenset(),
}


class ArtifactLifecycleError(LifecycleError):
    """A transition the state machine does not permit."""


# The only `evidence_type` a revision may be bound to through
# `attach_approval_evidence`. Every other value -- most importantly
# `artifact_activation`, which is what this restriction exists to constrain
# -- has no first-party writer in this deployment (`ExceptionService` is the
# only production code that inserts into `arc_approval_evidence`, and it
# hardcodes this exact value). A row of any other type can only exist
# through something other than a writer this system trusts, so binding it to
# a revision would let that origin buy activation eligibility it was never
# granted. Widening this set is a statement that a new evidence_type has
# gained a real writer, not a convenience change.
ATTACHABLE_EVIDENCE_TYPES = frozenset({"exception_approval"})


class EvidenceTypeNotWritableError(RegistryError):
    """Refused an `attach_approval_evidence` call naming untrusted evidence.

    Distinct from `ArtifactLifecycleError`: this is not about the revision's
    own state machine, it is about the evidence row's *type* never being
    attachable through this call, regardless of what revision it targets or
    what state that revision is in.
    """


def applicability_snapshot(
    *,
    scope: str,
    target_tenant_id: uuid.UUID | str | None,
    capability_ids: Iterable[object] | None,
    domain_ids: Iterable[object] | None,
    task_kinds: Iterable[object] | None,
    action_classes: Iterable[object] | None,
    environments: Iterable[object] | None,
    data_sensitivity_tiers: Iterable[object] | None,
) -> dict[str, object]:
    """The canonical applicability form, from wherever the values came.

    Two producers need this and must agree exactly: the registration path
    builds it from a draft, and the obligation refresh builds it from the rule
    rows it just read back. The digest over this form is the obligation dedup
    key, so any divergence between them does not merely look untidy -- it
    splits one obligation into two, and the tombstone left behind by the first
    can never be cleared by approving a replacement.

    They were separate implementations of the same shape until this existed.
    """

    def _sorted_strs(value: Iterable[object] | None) -> list[str]:
        # Tuples from a draft, lists or NULL from a row -- normalised to one
        # sorted list of strings so the two producers cannot differ on ordering
        # or on how an absent selector is spelled.
        return sorted(str(v) for v in (value or ()))

    return {
        "scope": scope,
        "target_tenant_id": str(target_tenant_id) if target_tenant_id else None,
        "capability_ids": _sorted_strs(capability_ids),
        "domain_ids": _sorted_strs(domain_ids),
        "task_kinds": _sorted_strs(task_kinds),
        "action_classes": _sorted_strs(action_classes),
        "environments": _sorted_strs(environments),
        "data_sensitivity_tiers": _sorted_strs(data_sensitivity_tiers),
    }


def applicability_digest(snapshot: dict[str, object]) -> str:
    """The dedup key for an obligation. Sorted keys and no whitespace, so two
    equal snapshots cannot digest differently."""
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_transition(current: str, target: str) -> None:
    allowed = _LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        msg = f"cannot move a revision from {current!r} to {target!r}"
        raise ArtifactLifecycleError(msg)


async def _assert_evidence_approves(session: AsyncSession, evidence_id: uuid.UUID, revision_id: uuid.UUID) -> None:
    """The evidence must be about *this* revision.

    Without it a revision could borrow an approval granted to something
    else, which is the whole failure approval evidence exists to prevent.
    One implementation, called from every path that can bind the two, so
    the check cannot be present on one route and absent on another --
    which is exactly how it came to be enforced at attach time and not at
    registration.
    """
    approved = (
        await session.execute(
            text("SELECT approved_revision_id FROM arc_approval_evidence WHERE evidence_id = :eid"),
            {"eid": evidence_id},
        )
    ).scalar_one_or_none()
    if approved is None:
        msg = f"approval evidence {evidence_id} not found"
        raise NotFoundError(msg)
    if approved != revision_id:
        msg = f"approval evidence {evidence_id} does not approve revision {revision_id}"
        raise ValidationError(msg)


async def _load_artifact(session: AsyncSession, artifact_id: uuid.UUID) -> ArtifactScope:
    row = (
        await session.execute(
            text("SELECT artifact_id, tenant_id FROM arc_artifacts WHERE artifact_id = :aid"),
            {"aid": artifact_id},
        )
    ).one_or_none()
    if row is None:
        msg = f"artifact {artifact_id} not found"
        raise NotFoundError(msg)
    # A NULL tenant means deployment-wide governance; every other row
    # belongs to exactly one tenant.
    if row.tenant_id is None:
        return ArtifactScope(scope=AuthorityScope.GLOBAL)
    return ArtifactScope(scope=AuthorityScope.TENANT, tenant_id=row.tenant_id)


async def _lock_family(session: AsyncSession, revision_id: uuid.UUID) -> Row[Any]:
    """Lock every revision of this revision's artifact, then return it.

    The lock is taken on the family rather than the single row because
    activation reads *and* writes siblings — it supersedes the current
    active revision — and locking only the target would leave that read
    unserialized.
    """
    target = (
        await session.execute(
            text("SELECT artifact_id FROM arc_revisions WHERE revision_id = :rid"),
            {"rid": revision_id},
        )
    ).one_or_none()
    if target is None:
        msg = f"revision {revision_id} not found"
        raise NotFoundError(msg)

    await session.execute(
        text("SELECT revision_id FROM arc_revisions WHERE artifact_id = :aid ORDER BY revision_id FOR UPDATE"),
        {"aid": target.artifact_id},
    )
    row = (
        await session.execute(
            text(
                "SELECT revision_id, artifact_id, lifecycle_state, review_expires_at, "
                "       approval_evidence_id FROM arc_revisions WHERE revision_id = :rid"
            ),
            {"rid": revision_id},
        )
    ).one()
    return row


__all__ = [
    "ATTACHABLE_EVIDENCE_TYPES",
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_DRAFT",
    "LIFECYCLE_EXPIRED",
    "LIFECYCLE_REVOKED",
    "LIFECYCLE_SUPERSEDED",
    "ArtifactLifecycleError",
    "EvidenceTypeNotWritableError",
    "applicability_digest",
    "applicability_snapshot",
]
