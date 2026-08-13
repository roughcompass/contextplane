"""The one module permitted to write `cross_org_grants`.

A grant is the only thing that lets data cross an organization boundary. A second
writer would produce rows that look identical to approved ones while satisfying
none of the approval evidence a grant is supposed to carry — and unlike a forged
request, a forged *grant* is indistinguishable at read time from one somebody
signed off. So the table has one writer, and
`scripts/check_privileged_writes.py` fails the build when a second appears.

**A grant is proposed, then activated. It is never created active.** Creating one
already in force would make "propose a grant" and "approve a grant" the same act,
which is the whole distinction the approval evidence exists to record.

**Revocation is immediate and is not a state anybody has to notice.** Revoking
sets the state and the time in one statement; the authorization decision re-reads
the row on every check rather than caching a verdict, so a revoked grant stops
working at the next decision rather than at the next cache expiry.

**Nothing here decides anything.** This module records grants;
`sharing/authorization.py` decides whether one permits an operation. Keeping the
two apart is what stops a writer from being able to widen its own grant and
authorize against it in the same breath.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PROPOSED: Final = "proposed"
ACTIVE: Final = "active"
REVOKED: Final = "revoked"
EXPIRED: Final = "expired"

GRANT_STATES: Final[frozenset[str]] = frozenset({PROPOSED, ACTIVE, REVOKED, EXPIRED})
GRANT_KINDS: Final[frozenset[str]] = frozenset({"relationship", "adoption", "learning", "projection", "context"})


class GrantError(RuntimeError):
    """A refusal from the grant writer."""


@dataclasses.dataclass(frozen=True)
class CrossOrgGrant:
    """One grant as a reader sees it."""

    grant_id: uuid.UUID
    source_tenant_id: uuid.UUID
    destination_tenant_id: uuid.UUID
    grant_kind: str
    grant_state: str
    profile_types: Sequence[str]
    relationship_types: Sequence[str]
    allowed_operations: Sequence[str]
    classification_ceiling: str
    effective_from: datetime.datetime
    effective_to: datetime.datetime | None
    approval_evidence: str | None
    revoked_at: datetime.datetime | None

    def is_in_force(self, at: datetime.datetime) -> bool:
        """Whether this grant permits anything at `at`.

        State and interval both, because either alone is insufficient: an active
        grant whose window has closed permits nothing, and a revoked grant inside
        its window permits nothing either.
        """
        if self.grant_state != ACTIVE:
            return False
        if self.effective_from > at:
            return False
        return self.effective_to is None or self.effective_to > at


_COLUMNS: Final = (
    "grant_id, source_tenant_id, destination_tenant_id, grant_kind, grant_state,"
    " profile_types, relationship_types, allowed_operations, classification_ceiling,"
    " effective_from, effective_to, approval_evidence, revoked_at"
)


async def propose(
    session: AsyncSession,
    *,
    source_tenant_id: uuid.UUID,
    destination_tenant_id: uuid.UUID,
    grant_kind: str,
    profile_types: Sequence[str],
    relationship_types: Sequence[str],
    allowed_operations: Sequence[str],
    classification_ceiling: str,
    effective_from: datetime.datetime,
    policy_version: str,
    recorded_by: str,
    recorded_at: datetime.datetime,
    effective_to: datetime.datetime | None = None,
) -> uuid.UUID:
    """Record a proposed grant. It permits nothing until it is activated."""
    if grant_kind not in GRANT_KINDS:
        raise GrantError(f"unknown grant kind {grant_kind!r}; legal: {', '.join(sorted(GRANT_KINDS))}")
    if source_tenant_id == destination_tenant_id:
        raise GrantError("a cross-organization grant joins two different tenants")

    grant_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO cross_org_grants ("
            "  grant_id, source_tenant_id, destination_tenant_id, grant_kind, grant_state,"
            "  profile_types, relationship_types, allowed_operations, classification_ceiling,"
            "  effective_from, effective_to, policy_version, recorded_by, recorded_at"
            ") VALUES (:gid, :src, :dst, :kind, :state,"
            "          CAST(:ptypes AS JSONB), CAST(:rtypes AS JSONB), CAST(:ops AS JSONB), :ceiling,"
            "          :from, :to, :policy, :by, :now)"
        ),
        {
            "gid": grant_id,
            "src": source_tenant_id,
            "dst": destination_tenant_id,
            "kind": grant_kind,
            "state": PROPOSED,
            "ptypes": json.dumps(sorted(profile_types)),
            "rtypes": json.dumps(sorted(relationship_types)),
            "ops": json.dumps(sorted(allowed_operations)),
            "ceiling": classification_ceiling,
            "from": effective_from,
            "to": effective_to,
            "policy": policy_version,
            "by": recorded_by,
            "now": recorded_at,
        },
    )
    return grant_id


async def activate(
    session: AsyncSession,
    *,
    grant_id: uuid.UUID,
    approval_evidence: str,
    approving_authorities: Sequence[str],
) -> None:
    """Put a proposed grant in force, recording what approved it.

    `approval_evidence` is required. A grant activated with nothing recorded is
    indistinguishable from one somebody approved, which is the confusion the
    single-writer rule exists to prevent from the other direction.
    """
    if not approval_evidence.strip():
        raise GrantError(
            "activating a grant records the approval that permitted it; without it an approved grant and an "
            "unreviewed one are the same row"
        )
    result = await session.execute(
        text(
            "UPDATE cross_org_grants"
            "   SET grant_state = :state, approval_evidence = :evidence,"
            "       approving_authorities = CAST(:authorities AS JSONB)"
            " WHERE grant_id = :gid AND grant_state = :proposed"
        ),
        {
            "state": ACTIVE,
            "evidence": approval_evidence,
            "authorities": json.dumps(sorted(approving_authorities)),
            "gid": grant_id,
            "proposed": PROPOSED,
        },
    )
    # `rowcount` is on the cursor result rather than the typed `Result`, so it is
    # read through the attribute rather than declared -- the alternative is a
    # second SELECT that could disagree with the UPDATE it is checking.
    if getattr(result, "rowcount", 0) == 0:
        raise GrantError(f"grant {grant_id} is not in {PROPOSED!r} and cannot be activated")


async def revoke(
    session: AsyncSession,
    *,
    grant_id: uuid.UUID,
    reason: str,
    revoked_by: str,
    revoked_at: datetime.datetime,
) -> None:
    """Withdraw a grant. It stops permitting anything at the next decision.

    Not at the next cache expiry: the authorization decision re-reads the row
    every time, so there is no window in which a revoked grant still works.
    """
    if not reason.strip():
        raise GrantError("a revocation states its reason, or the withdrawal cannot be reviewed")
    await session.execute(
        text(
            "UPDATE cross_org_grants"
            "   SET grant_state = :state, revoked_at = :at, revocation_reason = :reason, revoked_by = :by"
            " WHERE grant_id = :gid"
        ),
        {"state": REVOKED, "at": revoked_at, "reason": reason, "by": revoked_by, "gid": grant_id},
    )


async def grants_between(
    session: AsyncSession,
    *,
    source_tenant_id: uuid.UUID,
    destination_tenant_id: uuid.UUID,
) -> tuple[CrossOrgGrant, ...]:
    """Every grant from one tenant to another, in whatever state.

    Unfiltered: the authorization decision needs to see a revoked grant to refuse
    against it rather than to report that none exists, and the two are different
    answers to an operator asking why an operation failed.
    """
    rows = (
        await session.execute(
            text(
                # `_COLUMNS` is this module's own literal list; values are bound.
                f"SELECT {_COLUMNS} FROM cross_org_grants"  # noqa: S608
                " WHERE source_tenant_id = :src AND destination_tenant_id = :dst"
                " ORDER BY effective_from DESC"
            ),
            {"src": source_tenant_id, "dst": destination_tenant_id},
        )
    ).mappings()
    return tuple(_to_grant(dict(row)) for row in rows)


def _to_grant(row: Mapping[str, Any]) -> CrossOrgGrant:
    return CrossOrgGrant(**{field.name: row[field.name] for field in dataclasses.fields(CrossOrgGrant)})


__all__ = [
    "ACTIVE",
    "EXPIRED",
    "GRANT_KINDS",
    "GRANT_STATES",
    "PROPOSED",
    "REVOKED",
    "CrossOrgGrant",
    "GrantError",
    "activate",
    "grants_between",
    "propose",
    "revoke",
]
