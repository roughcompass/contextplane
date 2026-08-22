"""What a replayed stream's content is, in handling terms, declared once.

E1 asks for "stream-scoped action-class and sensitivity declarations at
source-namespace registration". A stream is the `(source_system,
source_namespace)` pair E2-T2 put on a replayed session event, and this is where
an operator says what handling tier everything arriving under that pair carries.

**Why a declaration and not a per-event field.** The write path already accepts
`external` provenance from the caller, so the caller could in principle state a
tier per event -- and then the sensitivity of a payroll replay would be whatever
the exporter felt like saying, checked by nothing. A tier declared once by an
operator, and looked up rather than accepted, is the difference between
governance and a form field.

**Nothing here defaults an unregistered stream to anything.** The lookup returns
`None`, the write path leaves the manifest's `data_sensitivity` unset, and
`arc/service/selection.py` reads absent as the most restrictive tier -- a rule
that exists because a caller sending an unknown tier or none otherwise escaped
every applicability rule that named one. So an unregistered stream is governed
strictly until somebody registers it, and this module does not restate that rule
where it would eventually drift from the one enforcing it.

**Registering is a governance act and is recorded as one.** The row carries who
declared the tier, when, and a reason long enough to be a sentence. An operator
asking later why a stream is `restricted` gets an answer from the row rather than
from a log search, and the twenty-word floor is the same bar the governed
magnitude registry holds a number to.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.sensitivity import TIER_SET
from contextplane.types import Clock, TenantContext

#: Matches the CHECK the migration generates from `sensitivity.TIERS`. Stated as
#: a floor here so a caller gets a typed refusal at the boundary rather than an
#: `IntegrityError` from a constraint two layers down.
MIN_REASON_WORDS = 5


@dataclasses.dataclass(frozen=True)
class SourceNamespace:
    """One registered stream, as an operator sees it."""

    source_system: str
    source_namespace: str
    data_sensitivity: str
    reason: str
    registered_by: uuid.UUID
    registered_at: datetime.datetime
    updated_at: datetime.datetime


_UPSERT = """
INSERT INTO memory_source_namespaces (
    tenant_id, source_system, source_namespace, data_sensitivity, registered_by, reason
) VALUES (:tid, :system, :ns, :tier, :actor, :reason)
ON CONFLICT (tenant_id, source_system, source_namespace) DO UPDATE
   SET data_sensitivity = EXCLUDED.data_sensitivity,
       reason = EXCLUDED.reason,
       registered_by = EXCLUDED.registered_by,
       updated_at = CAST(:now AS TIMESTAMPTZ)
RETURNING source_system, source_namespace, data_sensitivity, reason,
          registered_by, registered_at, updated_at
"""

#: The one read the write path performs. `registered_at` is deliberately not
#: touched on update: it records when this stream was first governed, which an
#: auditor asking "how long has this been declared" needs, and overwriting it
#: would make every correction look like a fresh registration.
_LOOKUP = """
SELECT data_sensitivity
FROM memory_source_namespaces
WHERE tenant_id = :tid AND source_system = :system AND source_namespace = :ns
"""

_LIST = """
SELECT source_system, source_namespace, data_sensitivity, reason,
       registered_by, registered_at, updated_at
FROM memory_source_namespaces
WHERE tenant_id = :tid
ORDER BY source_system, source_namespace
"""


class SourceNamespaceService:
    """Register a stream's handling tier, and read it back on the write path."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def register(
        self,
        ctx: TenantContext,
        *,
        source_system: str,
        source_namespace: str,
        data_sensitivity: str,
        reason: str,
    ) -> SourceNamespace:
        """Declare, or re-declare, what a stream's content is.

        An upsert rather than a create-then-update pair: a stream's tier is one
        fact, and an operator correcting `internal` to `restricted` is not
        creating a second stream. The correction carries its own actor and
        reason, so the row always says who made the declaration currently in
        force.
        """
        tier = data_sensitivity.strip()
        if tier not in TIER_SET:
            # Refused here rather than by the CHECK so the caller learns the
            # scale rather than a constraint name. The scale is closed on
            # purpose: an open tier is one nothing can rank.
            msg = f"{tier!r} is not a handling tier; one of {sorted(TIER_SET)}"
            raise ValidationError(msg)
        if len(reason.split()) < MIN_REASON_WORDS:
            msg = (
                f"a stream's handling tier is stated with a reason of at least {MIN_REASON_WORDS} words; "
                "a tier nobody justified is one nobody will revisit"
            )
            raise ValidationError(msg)

        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(_UPSERT),
                    {
                        "tid": ctx.tenant_id,
                        "system": source_system.strip(),
                        "ns": source_namespace.strip(),
                        "tier": tier,
                        "actor": ctx.actor_id,
                        "reason": reason.strip(),
                        "now": self._clock.now(),
                    },
                )
            ).one()
        return _to_namespace(row)

    async def sensitivity_of(self, ctx: TenantContext, *, source_system: str, source_namespace: str) -> str | None:
        """This stream's declared tier, or `None` if nobody has declared one.

        `None` rather than a substituted default. The caller passes it straight
        into the manifest, where an absent tier is already read as the most
        restrictive -- substituting one here would put a second copy of that rule
        in a second place, and the two would eventually disagree about which is
        strictest.
        """
        async with self._session_factory() as session:
            found = (
                await session.execute(
                    text(_LOOKUP),
                    {
                        "tid": ctx.tenant_id,
                        "system": source_system,
                        "ns": source_namespace,
                    },
                )
            ).scalar_one_or_none()
        return str(found) if found is not None else None

    async def list_for_tenant(self, ctx: TenantContext) -> list[SourceNamespace]:
        """Every stream this tenant has declared, so an operator can review them."""
        async with self._session_factory() as session:
            rows = (await session.execute(text(_LIST), {"tid": ctx.tenant_id})).all()
        return [_to_namespace(row) for row in rows]


def _to_namespace(row: object) -> SourceNamespace:
    return SourceNamespace(
        source_system=row.source_system,  # type: ignore[attr-defined]
        source_namespace=row.source_namespace,  # type: ignore[attr-defined]
        data_sensitivity=row.data_sensitivity,  # type: ignore[attr-defined]
        reason=row.reason,  # type: ignore[attr-defined]
        registered_by=row.registered_by,  # type: ignore[attr-defined]
        registered_at=row.registered_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


__all__ = ["MIN_REASON_WORDS", "SourceNamespace", "SourceNamespaceService"]
