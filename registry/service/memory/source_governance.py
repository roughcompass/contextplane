"""What a source's claims are worth, and how many of them it may write.

Two controls that only make sense together. Declaring authority without a rate ceiling
lets a correctly-labelled source flood the store; a ceiling without a declared authority
governs volume while leaving every claim's weight to whatever the code happened to pass.

**Authority is declared before the first write, not inferred.** A Confluence page is not
an owner's OpenAPI sync, and the difference decides conflicts for the lifetime of every
claim the source produces. There is no default tier: registering a source without
choosing one is refused, because a default would be a decision nobody made that then
looks like one somebody did.

**The ceiling exists because valid claims can still ruin a store.** Every claim a runbook
connector produces might be individually correct and the staging queue still becomes
something no curator will ever work through. That is a real failure even though nothing
is wrong with any single row, which is why the limit is enforced rather than advisory.

**The breaker's state is in the database.** A breaker held in memory reopens on every
deploy, which turns a rate limit into a rate limit between restarts. Persisting it also
means an operator can see that a source is cut off, and why, without reading logs.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any, Final

from prometheus_client import Counter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.audit import actions
from registry.exceptions import NotFoundError, ValidationError
from registry.service.governance.authority import SOURCE_AUTHORITY_RANK
from registry.types import Clock, TenantContext

# How long a tripped breaker stays open. Long enough that a runaway connector stops
# mattering, short enough that a transient burst does not need an operator.
BREAKER_COOLDOWN_SECONDS: Final[int] = 900

# Labelled by source only, never by tenant. A tenant label turns one metric into
# one time series per tenant, so the series count grows with adoption and the
# Prometheus that dies from it dies in production, months after the line shipped.
# A registered source is a bounded set an operator provisions; a tenant is not.
# Per-tenant attribution for these events lives in the audit trail, which is
# queryable, access-controlled, and retained on purpose -- none of which is true
# of the scrape surface.
_BREACHES = Counter(
    "registry_source_ingest_breach_total",
    "Ingest-ceiling breaches, by source. A breach means the circuit "
    "opened and claims were refused rather than the store absorbing them.",
    ["source_id"],
)

_ADMITTED = Counter(
    "registry_source_ingest_admitted_total",
    "Claims a source was permitted to write. Paired with the breach counter so a "
    "dashboard can show the ratio rather than only the failures.",
    ["source_id"],
)


@dataclasses.dataclass(frozen=True)
class Admission:
    """Whether a source may write now, and why not when it may not."""

    permitted: bool
    reason: str | None = None
    remaining: int = 0


@dataclasses.dataclass(frozen=True)
class SourcePolicy:
    """Governance policy for one ingest source, including breaker state."""

    source_id: uuid.UUID
    tenant_id: uuid.UUID
    authority_tier: str
    ingest_ceiling: int
    window_seconds: int
    breaker_open_until: datetime.datetime | None
    breach_count: int
    # Off by default: an unresolved connector subject lands `unlinked` for the
    # curation queue unless an operator opts this source into provisioning an
    # entity for it. See `SourceIngestService.ingest` for the write this gates.
    may_provision_entities: bool = False


class SourceGovernanceService:
    """Declare, patch, and read source governance policies; controls breaker and rate limits."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._factory = factory
        self._clock = clock

    async def declare(
        self,
        ctx: TenantContext,
        *,
        source_id: uuid.UUID,
        authority_tier: str,
        ingest_ceiling: int = 1000,
        window_seconds: int = 3600,
        may_provision_entities: bool = False,
    ) -> SourcePolicy:
        """Register what a source's claims are worth. Required before it may write.

        The tier is validated against the authority ladder rather than accepted as a
        string, because a typo would otherwise register a tier that ranks below
        everything -- silently making the source's claims worthless instead of
        failing.

        `may_provision_entities` defaults false on every call, including a
        re-declaration: a `PATCH` that omits it must not silently flip it on, so
        the router merges the current row's value in itself before calling here
        -- this default only governs a source's first declaration.
        """
        if authority_tier not in SOURCE_AUTHORITY_RANK:
            raise ValidationError(f"authority_tier must be one of {sorted(SOURCE_AUTHORITY_RANK)}")
        if ingest_ceiling <= 0 or window_seconds <= 0:
            raise ValidationError("ingest_ceiling and window_seconds must be positive")

        now = self._clock.now()
        async with self._factory() as session, session.begin():
            owner = (
                await session.execute(
                    text("SELECT tenant_id FROM sync_sources WHERE source_id = :sid"),
                    {"sid": source_id},
                )
            ).scalar_one_or_none()
            if owner is None:
                raise NotFoundError("no such source")
            if owner != ctx.tenant_id:
                raise PermissionError("only the owning tenant may govern a source")

            await session.execute(
                text(
                    "INSERT INTO memory_source_governance "
                    "  (source_id, tenant_id, authority_tier, ingest_ceiling, "
                    "   window_seconds, may_provision_entities, window_started_at, "
                    "   window_count, updated_at, updated_by) "
                    "VALUES (:sid, :tid, :tier, :ceiling, :window, :provision, :now, 0, "
                    "        :now, :actor) "
                    "ON CONFLICT (source_id) DO UPDATE SET "
                    "  authority_tier = EXCLUDED.authority_tier, "
                    "  ingest_ceiling = EXCLUDED.ingest_ceiling, "
                    "  window_seconds = EXCLUDED.window_seconds, "
                    "  may_provision_entities = EXCLUDED.may_provision_entities, "
                    "  updated_at = EXCLUDED.updated_at"
                ),
                {
                    "sid": source_id,
                    "tid": ctx.tenant_id,
                    "tier": authority_tier,
                    "ceiling": ingest_ceiling,
                    "window": window_seconds,
                    "provision": may_provision_entities,
                    "now": now,
                    "actor": ctx.actor_id,
                },
            )
            await self._audit(
                session,
                action=actions.SOURCE_AUTHORITY_DECLARED,
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                source_id=source_id,
                payload={
                    "authority_tier": authority_tier,
                    "ingest_ceiling": ingest_ceiling,
                    "window_seconds": window_seconds,
                    "may_provision_entities": may_provision_entities,
                },
                now=now,
            )
        policy = await self.policy_for(source_id)
        if policy is None:  # pragma: no cover - written in this transaction
            raise NotFoundError("source governance vanished after declaration")
        return policy

    async def policy_for(self, source_id: uuid.UUID) -> SourcePolicy | None:
        """Load the policy for one source; None if the source has not been declared."""
        async with self._factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT source_id, tenant_id, authority_tier, ingest_ceiling, "
                            "       window_seconds, breaker_open_until, breach_count, "
                            "       may_provision_entities "
                            "  FROM memory_source_governance WHERE source_id = :sid"
                        ),
                        {"sid": source_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return SourcePolicy(
            source_id=row["source_id"],
            tenant_id=row["tenant_id"],
            authority_tier=row["authority_tier"],
            ingest_ceiling=int(row["ingest_ceiling"]),
            window_seconds=int(row["window_seconds"]),
            breaker_open_until=row["breaker_open_until"],
            breach_count=int(row["breach_count"]),
            may_provision_entities=bool(row["may_provision_entities"]),
        )

    async def policies_for_tenant(self, tenant_id: uuid.UUID) -> tuple[SourcePolicy, ...]:
        """Every source this tenant has declared, for the admin surface's list view.

        `policy_for` answers "what does this one source look like" and takes a
        `source_id` the caller must already have; an operator auditing a
        tenant's governance posture needs the other direction -- every source
        that tenant has declared authority and a ceiling for, without knowing
        each id up front. Ordered by `source_id` so the list is stable across
        calls rather than following whatever order the table happens to
        return rows in.
        """
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT source_id, tenant_id, authority_tier, ingest_ceiling, "
                            "       window_seconds, breaker_open_until, breach_count, "
                            "       may_provision_entities "
                            "  FROM memory_source_governance WHERE tenant_id = :tid "
                            " ORDER BY source_id"
                        ),
                        {"tid": tenant_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            SourcePolicy(
                source_id=row["source_id"],
                tenant_id=row["tenant_id"],
                authority_tier=row["authority_tier"],
                ingest_ceiling=int(row["ingest_ceiling"]),
                window_seconds=int(row["window_seconds"]),
                breaker_open_until=row["breaker_open_until"],
                breach_count=int(row["breach_count"]),
                may_provision_entities=bool(row["may_provision_entities"]),
            )
            for row in rows
        )

    async def admit(self, source_id: uuid.UUID, *, count: int = 1) -> Admission:
        """May this source write `count` more claims right now?

        Refuses an undeclared source outright. That is the enforcement behind
        "declare authority before you may write": the check is not a lint on
        registration, it is the gate on every ingest.

        The window is fixed rather than sliding. A sliding window is a better rate
        limiter and needs per-claim timestamps to compute; a fixed window is
        auditable from the row itself, and the purpose here is to stop a runaway
        connector rather than to shape traffic precisely.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT tenant_id, authority_tier, ingest_ceiling, window_seconds, "
                            "       breaker_open_until, window_started_at, window_count "
                            "  FROM memory_source_governance WHERE source_id = :sid FOR UPDATE"
                        ),
                        {"sid": source_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return Admission(
                    permitted=False,
                    reason="the source has not declared an authority tier and may not write",
                )

            labels = {"source_id": str(source_id)}

            if row["breaker_open_until"] is not None and now < row["breaker_open_until"]:
                return Admission(
                    permitted=False,
                    reason=f"circuit open until {row['breaker_open_until'].isoformat()}",
                )

            window_age = (now - row["window_started_at"]).total_seconds()
            expired = window_age >= row["window_seconds"]
            used = 0 if expired else int(row["window_count"])
            ceiling = int(row["ingest_ceiling"])

            if used + count > ceiling:
                # Open the circuit rather than admit a partial batch. A connector that
                # got half its claims through would leave the store holding an
                # arbitrary prefix of a document, which is worse than none of it.
                await session.execute(
                    text(
                        "UPDATE memory_source_governance "
                        "   SET breaker_open_until = :until, breach_count = breach_count + 1, "
                        "       updated_at = :now "
                        " WHERE source_id = :sid"
                    ),
                    {
                        "until": now + datetime.timedelta(seconds=BREAKER_COOLDOWN_SECONDS),
                        "now": now,
                        "sid": source_id,
                    },
                )
                await self._audit(
                    session,
                    action=actions.SOURCE_BREAKER_OPENED,
                    tenant_id=row["tenant_id"],
                    actor_id=None,
                    source_id=source_id,
                    payload={"used": used, "requested": count, "ceiling": ceiling},
                    now=now,
                )
                _BREACHES.labels(**labels).inc()
                return Admission(
                    permitted=False,
                    reason=f"ingest ceiling of {ceiling} per {row['window_seconds']}s reached",
                )

            await session.execute(
                text(
                    "UPDATE memory_source_governance "
                    "   SET window_started_at = CASE WHEN :expired THEN :now "
                    "                                ELSE window_started_at END, "
                    "       window_count = CASE WHEN :expired THEN :count "
                    "                           ELSE window_count + :count END, "
                    "       updated_at = :now "
                    " WHERE source_id = :sid"
                ),
                {"expired": expired, "now": now, "count": count, "sid": source_id},
            )
            _ADMITTED.labels(**labels).inc(count)
            return Admission(permitted=True, remaining=ceiling - (used + count))

    async def reset_breaker(self, ctx: TenantContext, source_id: uuid.UUID) -> None:
        """Close a tripped breaker early.

        Kept separate from `declare` so an operator clearing a breaker does not have
        to restate the tier and ceiling -- restating them is how a cleanup silently
        changes a policy.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            # RETURNING rather than rowcount: the tenant clause is the authorization
            # check, so "nothing matched" and "not yours" must be one answer, and a
            # returned id is the only way to know which row was actually touched.
            reset = (
                await session.execute(
                    text(
                        "UPDATE memory_source_governance "
                        "   SET breaker_open_until = NULL, window_started_at = :now, "
                        "       window_count = 0, updated_at = :now "
                        " WHERE source_id = :sid AND tenant_id = :tid "
                        " RETURNING source_id"
                    ),
                    {"now": now, "sid": source_id, "tid": ctx.tenant_id},
                )
            ).first()
            if reset is None:
                # The tenant clause folded into the same query is the authorisation
                # check, so "nothing matched" and "not yours" produce the one
                # answer -- deliberately a single raise, so remapping its type
                # cannot split them back into two distinguishable responses.
                raise PermissionError("no such source in this tenant")

    async def _audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        source_id: uuid.UUID,
        payload: dict[str, Any],
        now: datetime.datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES (:audit_id, :tid, :aid, :action, 'sync_source', :target, NULL, "
                "        CAST(:after AS JSONB), :now, NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tid": tenant_id,
                "aid": actor_id,
                "action": action,
                "target": source_id,
                "after": json.dumps(payload, sort_keys=True, default=str),
                "now": now,
            },
        )
