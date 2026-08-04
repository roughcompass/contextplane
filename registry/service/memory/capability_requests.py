"""What consuming teams need, routed to whoever can act on it.

Every phase before this one moved information outward: observe a session, extract a
claim, score it, settle it, promote it, serve it. This is the first path that runs the
other way -- from the team using a capability back to the team that owns it.

**A request is not a claim.** A claim asserts what is true and is scored, decayed,
consolidated, and possibly promoted. A request expresses a need. It is never true or
false, so scoring it would be meaningless; and consolidating two requests would erase
the fact that two teams asked independently, which is the most useful thing about the
second one.

**The requester cannot decide their own request.** Authority follows the subject's owner,
by the same resolution promotion uses. Without that, the surface is a suggestion box the
suggester can also stamp.

**A declined request is kept.** Three declined requests for the same thing is
information about the capability. Deleting them makes the owner's position look
unanimous, and leaves the requester unable to tell refusal from neglect.

**Status is visible to the requester, including the reason.** An invisible queue is
indistinguishable from being ignored, and being ignored is what drives teams back to
out-of-band channels -- which is the failure this whole surface exists to prevent.
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
from registry.exceptions import RegistryError

STATUS_RAISED: Final[str] = "raised"
STATUS_ACKNOWLEDGED: Final[str] = "acknowledged"
STATUS_ACCEPTED: Final[str] = "accepted"
STATUS_DECLINED: Final[str] = "declined"
STATUS_DUPLICATE: Final[str] = "duplicate"
STATUS_RESOLVED: Final[str] = "resolved"

# The closed lifecycle. Written as an adjacency map rather than a flat set of statuses
# because the illegal transitions are the point: a request cannot go from declined back
# to raised, and one that could would let a requester reopen a decision by retrying.
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    STATUS_RAISED: frozenset({STATUS_ACKNOWLEDGED, STATUS_DECLINED, STATUS_DUPLICATE}),
    STATUS_ACKNOWLEDGED: frozenset({STATUS_ACCEPTED, STATUS_DECLINED, STATUS_DUPLICATE}),
    STATUS_ACCEPTED: frozenset({STATUS_RESOLVED}),
    # Terminal. A declined request stays declined and stays readable; reopening is
    # raising a new request, which keeps the original refusal on the record.
    STATUS_DECLINED: frozenset(),
    STATUS_DUPLICATE: frozenset(),
    STATUS_RESOLVED: frozenset(),
}

# Statuses that require the actor to say why. Declining without a reason reads as
# neglect; marking something a duplicate without saying of what is worse than silence.
REASON_REQUIRED: Final[frozenset[str]] = frozenset({STATUS_DECLINED, STATUS_DUPLICATE})

# Who may act on a request in the owning tenant. The same pair promotion uses, so a
# capability has one set of people who speak for it rather than two.
DECIDE_ROLES: Final[frozenset[str]] = frozenset({"producer", "admin"})

REQUEST_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "interface_change",
        "new_capability",
        "documentation",
        "operational",
        "defect",
    }
)


class RequestError(RegistryError):
    """A request operation was refused. The message says what and why."""


@dataclasses.dataclass(frozen=True)
class CapabilityRequest:
    request_id: uuid.UUID
    owner_tenant_id: uuid.UUID
    requester_tenant_id: uuid.UUID
    subject_entity_id: uuid.UUID
    request_category: str
    title: str
    body: str
    status: str
    decision_reason: str | None
    resulting_promotion_id: uuid.UUID | None
    created_at: datetime.datetime

    @property
    def is_open(self) -> bool:
        return self.status in {STATUS_RAISED, STATUS_ACKNOWLEDGED}


@dataclasses.dataclass(frozen=True)
class Transition:
    from_status: str
    to_status: str
    reason: str | None
    occurred_at: datetime.datetime


class CapabilityRequestService:
    """Raise, route, decide, and read back."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, clock: Any) -> None:
        self._factory = factory
        self._clock = clock

    # --- raising --------------------------------------------------------------

    async def raise_request(
        self,
        ctx: Any,
        *,
        subject_entity_id: uuid.UUID,
        request_category: str,
        title: str,
        body: str,
    ) -> CapabilityRequest:
        """Raise a request against a capability, routed to whoever owns it.

        The owner is resolved from the subject rather than supplied, so a requester
        cannot address their request to a tenant of their choosing -- which would be
        a way to get a decision from somebody with no standing over the capability.
        """
        if request_category not in REQUEST_CATEGORIES:
            raise RequestError(f"request_category must be one of {sorted(REQUEST_CATEGORIES)}")
        for field, value in (("title", title), ("body", body)):
            if not value.strip():
                raise RequestError(f"{field} must not be empty")

        now = self._clock.now()
        async with self._factory() as session, session.begin():
            owner = (
                await session.execute(
                    text("SELECT tenant_id FROM entities WHERE entity_id = :eid AND is_active"),
                    {"eid": subject_entity_id},
                )
            ).scalar_one_or_none()
            if owner is None:
                # Absent and invisible are the same answer, as everywhere else: the
                # existence of another tenant's capability is not public information.
                raise RequestError("no such capability")

            request_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO memory_capability_request "
                    "  (request_id, owner_tenant_id, requester_tenant_id, "
                    "   requester_actor_id, subject_entity_id, request_category, "
                    "   title, body, status, created_at, updated_at) "
                    "VALUES (:rid, :owner, :requester, :actor, :sid, :cat, :title, "
                    "        :body, 'raised', :now, :now)"
                ),
                {
                    "rid": request_id,
                    "owner": owner,
                    "requester": ctx.tenant_id,
                    "actor": ctx.actor_id,
                    "sid": subject_entity_id,
                    "cat": request_category,
                    "title": title,
                    "body": body,
                    "now": now,
                },
            )
            await self._audit(
                session,
                action=actions.REQUEST_RAISED,
                tenant_id=owner,
                actor_id=ctx.actor_id,
                request_id=request_id,
                payload={
                    "requester_tenant_id": str(ctx.tenant_id),
                    "subject_entity_id": str(subject_entity_id),
                    "request_category": request_category,
                    "cross_tenant": str(owner) != str(ctx.tenant_id),
                },
                now=now,
            )
        return CapabilityRequest(
            request_id=request_id,
            owner_tenant_id=owner,
            requester_tenant_id=ctx.tenant_id,
            subject_entity_id=subject_entity_id,
            request_category=request_category,
            title=title,
            body=body,
            status=STATUS_RAISED,
            decision_reason=None,
            resulting_promotion_id=None,
            created_at=now,
        )

    # --- deciding -------------------------------------------------------------

    async def transition(
        self,
        ctx: Any,
        *,
        request_id: uuid.UUID,
        to_status: str,
        reason: str | None = None,
    ) -> CapabilityRequest:
        """Move a request along its lifecycle, audited.

        Three separate checks, kept separate so removing any one fails its own test:
        the actor must be in the owning tenant, must hold a deciding role, and the
        transition must be legal from where the request is now.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            row = await self._locked(session, request_id)

            if row["owner_tenant_id"] != ctx.tenant_id:
                raise RequestError("only the tenant that owns the capability may act on this request")
            if not (set(ctx.roles) & DECIDE_ROLES):
                raise RequestError("acting on a request requires the producer or admin role")

            allowed = ALLOWED_TRANSITIONS.get(row["status"], frozenset())
            if to_status not in allowed:
                raise RequestError(
                    f"a {row['status']} request cannot become {to_status}; "
                    f"allowed: {sorted(allowed) or 'none, this status is terminal'}"
                )
            if to_status in REASON_REQUIRED and not (reason or "").strip():
                raise RequestError(f"a {to_status} decision requires a reason")

            await session.execute(
                text(
                    "UPDATE memory_capability_request "
                    "   SET status = :to, decided_by = :actor, decided_at = :now, "
                    "       decision_reason = COALESCE(:reason, decision_reason), "
                    "       updated_at = :now "
                    " WHERE request_id = :rid"
                ),
                {
                    "to": to_status,
                    "actor": ctx.actor_id,
                    "now": now,
                    "reason": reason,
                    "rid": request_id,
                },
            )
            # Append-only history. A lifecycle whose past could be rewritten would let
            # a request declined after a month read as answered promptly.
            await session.execute(
                text(
                    "INSERT INTO memory_request_transition "
                    "  (transition_id, request_id, from_status, to_status, reason, "
                    "   actor_id, occurred_at) "
                    "VALUES (:tid, :rid, :frm, :to, :reason, :actor, :now)"
                ),
                {
                    "tid": uuid.uuid4(),
                    "rid": request_id,
                    "frm": row["status"],
                    "to": to_status,
                    "reason": reason,
                    "actor": ctx.actor_id,
                    "now": now,
                },
            )
            await self._audit(
                session,
                action=_AUDIT_BY_STATUS.get(to_status, actions.REQUEST_TRANSITIONED),
                tenant_id=row["owner_tenant_id"],
                actor_id=ctx.actor_id,
                request_id=request_id,
                payload={"from": row["status"], "to": to_status, "reason": reason},
                now=now,
            )
        loaded = await self.get(ctx, request_id)
        if loaded is None:  # pragma: no cover - written in this transaction
            raise RequestError("request vanished mid-transition")
        return loaded

    async def link_to_promotion(self, ctx: Any, *, request_id: uuid.UUID, promotion_id: uuid.UUID) -> None:
        """Record that a request produced a canonical change.

        This is what closes the loop visibly. "Accepted" tells a requester somebody
        agreed; a link to the change tells them it happened, which is the part they
        actually wanted.
        """
        now = self._clock.now()
        async with self._factory() as session, session.begin():
            row = await self._locked(session, request_id)
            if row["owner_tenant_id"] != ctx.tenant_id:
                raise RequestError("only the owning tenant may link a request to a change")
            if row["status"] not in {STATUS_ACCEPTED, STATUS_RESOLVED}:
                raise RequestError(
                    f"a {row['status']} request cannot point at a change; " "only an accepted or resolved one can"
                )
            await session.execute(
                text(
                    "UPDATE memory_capability_request "
                    "   SET resulting_promotion_id = :pid, updated_at = :now "
                    " WHERE request_id = :rid"
                ),
                {"pid": promotion_id, "now": now, "rid": request_id},
            )
            await self._audit(
                session,
                action=actions.REQUEST_LINKED_TO_CHANGE,
                tenant_id=row["owner_tenant_id"],
                actor_id=ctx.actor_id,
                request_id=request_id,
                payload={"promotion_id": str(promotion_id)},
                now=now,
            )

    # --- reading --------------------------------------------------------------

    async def get(self, ctx: Any, request_id: uuid.UUID) -> CapabilityRequest | None:
        """One request, if the caller is either its owner or the team that raised it.

        Anybody else gets None. A request names a capability and a need, and both are
        things the two parties involved may see and nobody else needs to.
        """
        async with self._factory() as session:
            row = (
                (await session.execute(text(_SELECT + " WHERE request_id = :rid"), {"rid": request_id}))
                .mappings()
                .first()
            )
        if row is None:
            return None
        if ctx.tenant_id not in {row["owner_tenant_id"], row["requester_tenant_id"]}:
            return None
        return _to_request(row)

    async def for_owner(self, ctx: Any, *, open_only: bool = True, limit: int = 100) -> tuple[CapabilityRequest, ...]:
        """What is waiting on this tenant, oldest first."""
        clause = " AND status IN ('raised', 'acknowledged')" if open_only else ""
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(f"{_SELECT} WHERE owner_tenant_id = :tid{clause} ORDER BY created_at LIMIT :limit"),
                        {"tid": ctx.tenant_id, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_to_request(r) for r in rows)

    async def raised_by(self, ctx: Any, *, limit: int = 100) -> tuple[CapabilityRequest, ...]:
        """What this tenant has asked for, and where each has got to."""
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(f"{_SELECT} WHERE requester_tenant_id = :tid ORDER BY created_at DESC LIMIT :limit"),
                        {"tid": ctx.tenant_id, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_to_request(r) for r in rows)

    async def for_subject(self, ctx: Any, subject_entity_id: uuid.UUID) -> tuple[CapabilityRequest, ...]:
        """Requests about one capability, for showing alongside the claims about it.

        Scoped to the caller's own involvement: an owner sees every request against
        their capability, and a consumer sees the ones they raised. A third tenant
        sees none, because what somebody else asked for is their business.
        """
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"{_SELECT} WHERE subject_entity_id = :sid "
                            "   AND (owner_tenant_id = :tid OR requester_tenant_id = :tid) "
                            " ORDER BY created_at"
                        ),
                        {"sid": subject_entity_id, "tid": ctx.tenant_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_to_request(r) for r in rows)

    async def history(self, ctx: Any, request_id: uuid.UUID) -> tuple[Transition, ...]:
        """Every transition, in order. Empty if the caller may not see the request."""
        if await self.get(ctx, request_id) is None:
            return ()
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT from_status, to_status, reason, occurred_at "
                            "  FROM memory_request_transition WHERE request_id = :rid "
                            " ORDER BY seq"
                        ),
                        {"rid": request_id},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            Transition(
                from_status=r["from_status"],
                to_status=r["to_status"],
                reason=r["reason"],
                occurred_at=r["occurred_at"],
            )
            for r in rows
        )

    # --- internals ------------------------------------------------------------

    async def _locked(self, session: AsyncSession, request_id: uuid.UUID) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT request_id, owner_tenant_id, requester_tenant_id, status "
                        "  FROM memory_capability_request WHERE request_id = :rid FOR UPDATE"
                    ),
                    {"rid": request_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RequestError("no such request")
        return dict(row)

    async def _audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        request_id: uuid.UUID,
        payload: dict[str, Any],
        now: datetime.datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO audit_log "
                "  (audit_id, tenant_id, actor_id, action, target_type, target_id, "
                "   before_jsonb, after_jsonb, ts, request_id, error_code) "
                "VALUES (:audit_id, :tid, :aid, :action, 'memory_capability_request', "
                "        :target, NULL, CAST(:after AS JSONB), :now, NULL, NULL)"
            ),
            {
                "audit_id": uuid.uuid4(),
                "tid": tenant_id,
                "aid": actor_id,
                "action": action,
                "target": request_id,
                "after": json.dumps(payload, sort_keys=True, default=str),
                "now": now,
            },
        )


_AUDIT_BY_STATUS: Final[dict[str, str]] = {
    STATUS_ACKNOWLEDGED: actions.REQUEST_ACKNOWLEDGED,
    STATUS_ACCEPTED: actions.REQUEST_ACCEPTED,
    STATUS_DECLINED: actions.REQUEST_DECLINED,
    STATUS_DUPLICATE: actions.REQUEST_MARKED_DUPLICATE,
    STATUS_RESOLVED: actions.REQUEST_RESOLVED,
}

_SELECT = """
SELECT request_id, owner_tenant_id, requester_tenant_id, subject_entity_id,
       request_category, title, body, status, decision_reason,
       resulting_promotion_id, created_at
  FROM memory_capability_request
"""


def _to_request(row: Any) -> CapabilityRequest:
    return CapabilityRequest(
        request_id=row["request_id"],
        owner_tenant_id=row["owner_tenant_id"],
        requester_tenant_id=row["requester_tenant_id"],
        subject_entity_id=row["subject_entity_id"],
        request_category=row["request_category"],
        title=row["title"],
        body=row["body"],
        status=row["status"],
        decision_reason=row["decision_reason"],
        resulting_promotion_id=row["resulting_promotion_id"],
        created_at=row["created_at"],
    )
