"""What may promote without a person, and the posture when nobody has said.

The default is that nothing does. A fresh tenant has an empty allowlist, so a claim
that is eligible, uncontested, owner-originated, and not high-impact still waits for a
human. Turning that off is a per-predicate, audited act.

This is the operational form of "machines write to staging, never straight to truth".
The alternative -- promoting by default and letting operators narrow it -- makes the
safe posture depend on somebody knowing to switch something off, and the cost of not
knowing is a graph nobody agreed to.

**Four conditions, all required.** Not high-impact, eligible, owner-originated, and
allowlisted. They are checked separately rather than folded into one predicate so that
removing any single one fails a test rather than quietly widening what promotes.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.audit import actions
from registry.types import Clock

BLOCKED_HIGH_IMPACT: Final[str] = "high-impact claims are never auto-promoted"
BLOCKED_NOT_ALLOWLISTED: Final[str] = "predicate is not on the tenant's auto-promote allowlist"
BLOCKED_NOT_OWNER: Final[str] = "the author is not the subject's owner"
BLOCKED_INELIGIBLE: Final[str] = "the claim is not eligible for promotion"


@dataclasses.dataclass(frozen=True)
class AutoPromoteDecision:
    """Why a claim may or may not skip review.

    Carries every blocking reason rather than the first, because an operator asking
    "why did this not promote" needs the whole answer to decide what to change.
    """

    permitted: bool
    blocked_by: tuple[str, ...]


class GuardrailService:
    """The allowlist, and the decision it feeds."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._factory = factory
        self._clock = clock

    async def allowlist_for(self, tenant_id: uuid.UUID) -> frozenset[str]:
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text("SELECT predicate FROM memory_autopromote_allowlist WHERE tenant_id = :tid"),
                        {"tid": tenant_id},
                    )
                )
                .scalars()
                .all()
            )
        return frozenset(rows)

    async def allow(
        self,
        tenant_id: uuid.UUID,
        predicate: str,
        *,
        actor_id: uuid.UUID,
    ) -> None:
        """Opt one predicate into automatic promotion, audited.

        Widening what may promote without review is a more consequential act than
        most individual promotions, so the configuration change is recorded in the
        same log the promotions are.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_autopromote_allowlist "
                    "  (entry_id, tenant_id, predicate, added_at, added_by) "
                    "VALUES (:eid, :tid, :pred, :now, :actor) "
                    "ON CONFLICT (tenant_id, predicate) DO NOTHING"
                ),
                {
                    "eid": uuid.uuid4(),
                    "tid": tenant_id,
                    "pred": predicate,
                    "now": now,
                    "actor": actor_id,
                },
            )
            await self._audit(
                session,
                action=actions.CLAIM_AUTOPROMOTE_ALLOWED,
                tenant_id=tenant_id,
                actor_id=actor_id,
                payload={"predicate": predicate},
                now=now,
            )

    async def revoke(self, tenant_id: uuid.UUID, predicate: str, *, actor_id: uuid.UUID) -> None:
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM memory_autopromote_allowlist " " WHERE tenant_id = :tid AND predicate = :pred"),
                {"tid": tenant_id, "pred": predicate},
            )
            await self._audit(
                session,
                action=actions.CLAIM_AUTOPROMOTE_REVOKED,
                tenant_id=tenant_id,
                actor_id=actor_id,
                payload={"predicate": predicate},
                now=now,
            )

    async def may_auto_promote(
        self,
        *,
        tenant_id: uuid.UUID,
        predicate: str,
        high_impact: bool,
        eligible: bool,
        author_is_owner: bool,
    ) -> AutoPromoteDecision:
        """All four conditions, each reported separately when it fails."""
        blocked: list[str] = []
        if high_impact:
            blocked.append(BLOCKED_HIGH_IMPACT)
        if not eligible:
            blocked.append(BLOCKED_INELIGIBLE)
        if not author_is_owner:
            blocked.append(BLOCKED_NOT_OWNER)
        if predicate not in await self.allowlist_for(tenant_id):
            blocked.append(BLOCKED_NOT_ALLOWLISTED)
        return AutoPromoteDecision(permitted=not blocked, blocked_by=tuple(blocked))

    async def _audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        payload: dict[str, Any],
        now: datetime.datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES (:audit_id, :tid, :aid, :action, 'tenant', :tid, NULL, "
                "        CAST(:after AS JSONB), :now, NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tid": tenant_id,
                "aid": actor_id,
                "action": action,
                "after": json.dumps(payload, sort_keys=True),
                "now": now,
            },
        )
