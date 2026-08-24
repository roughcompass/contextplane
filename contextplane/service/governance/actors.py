"""Who is consuming this tenant's context, and which of them are agents.

E22-T7, on ADR 0019. `/agents` was an "any principal" screen with a misleading
title and a text box asking for an `Agent actor UUID` — the field the user named
first, placeholder `00000000-0000-0000-0000-000000000000`. There was no roster to
populate a list from because there was no read.

## An agent registers; nothing is inferred

E20 found there is no reliable signal: `upsert_entitlement_actor` takes only
`(session, tenant_id, oidc_subject, display_name)`, and a human in an IDE and an
unattended agent arrive over the identical MCP transport. That is still true,
and it is why `declare` exists rather than a classifier.

**An undeclared principal is `unknown`, never `human`.** Defaulting to human
would make every unregistered agent invisible on the screens built to watch
agents, and the failure would read as *"we have no agents"* rather than as
*"nobody has declared any"*.

**`unknown` rows are returned, not filtered.** The dissent ADR 0019 records is
that integrators will skip the declaration, leaving a roster that is mostly
`unknown` and looks broken. The answer is a requirement rather than optimism:
the roster shows every principal it has, says what it does not know about each,
and the caller can act on that. A roster honest about its gaps is usable; one
that hides them is the failure the objection predicts.

## The guard is here and not in a router

This workspace's standing rule, and the reason is that every service has two
transports: a check on a route is a check the MCP tool does not have. Tenant
scoping and the declaration's authorization live in the service methods below,
so a second transport reaching them cannot arrive without either.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import NotFoundError, ValidationError
from contextplane.types import Clock, TenantContext

MAX_PAGE_SIZE: Final[int] = 100

#: The state most principals are in most of the time, and the one this roster
#: exists to make visible rather than to hide.
KIND_UNKNOWN: Final = "unknown"

#: What a principal may be declared as. `sync_worker` and `system_curator` are
#: declarations too -- made by the code that provisions those principals -- and
#: are not offered here: a human declaring a principal is saying whether a
#: person or an agent is behind it, not adopting one of this service's own
#: internal roles.
DECLARABLE_KINDS: Final[tuple[str, ...]] = ("human", "agent")

#: Every value the column may hold, including the ones this service assigns to
#: itself. Named so a reader of a roster row can tell a declaration from a
#: provisioning artefact.
ALL_KINDS: Final[tuple[str, ...]] = (KIND_UNKNOWN, "human", "agent", "sync_worker", "system_curator")

#: Who may declare what a principal is. The same bar the other governance
#: writes hold: saying "this is an autonomous agent" changes what every screen
#: built to watch agents reports, and that is a decision rather than something
#: a principal's own evidence implies.
_OPERATOR_ROLES: Final[frozenset[str]] = frozenset({"producer", "admin"})

#: An owner has to be somebody a person could actually contact. Twenty
#: characters will not make a bad owner good; it stops "me" and "team" from
#: being answers to "who do I talk to about this agent".
MIN_OWNER: Final[int] = 3
MAX_OWNER: Final[int] = 200

_ROSTER = """
SELECT actor_id, display_name, oidc_subject, actor_kind, owner_principal,
       declared_at, declared_by, created_at
  FROM actors
 WHERE tenant_id = :tenant
"""

_ORDER = " ORDER BY created_at DESC, actor_id DESC"

_DECLARE = """
UPDATE actors
   SET actor_kind = :kind,
       owner_principal = :owner,
       declared_at = :now,
       declared_by = :declarer
 WHERE actor_id = :actor AND tenant_id = :tenant
RETURNING actor_id
"""


@dataclasses.dataclass(frozen=True)
class Principal:
    """One principal in a tenant, and what is known about it.

    `is_declared` is derived from `declared_at` rather than stored, so the two
    cannot disagree — and it is the field a roster reader needs, because
    `actor_kind` alone cannot distinguish "declared human" from "nobody said".
    """

    actor_id: uuid.UUID
    display_name: str | None
    oidc_subject: str
    actor_kind: str
    owner_principal: str | None
    declared_at: datetime.datetime | None
    declared_by: uuid.UUID | None
    created_at: datetime.datetime

    @property
    def is_declared(self) -> bool:
        """Whether anybody has said what this principal is.

        The field a roster reader needs: `actor_kind` alone cannot tell a
        declared human from a principal nobody has spoken about.
        """
        return self.declared_at is not None


@dataclasses.dataclass(frozen=True)
class PrincipalPage:
    """One page of principals, and where the next one starts."""

    items: tuple[Principal, ...]
    next_cursor: str | None


class ActorDirectoryService:
    """The roster, and the declaration that gives a principal a kind."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def list_principals(
        self,
        ctx: TenantContext,
        *,
        actor_kind: str | None = None,
        cursor: tuple[datetime.datetime, uuid.UUID] | None = None,
        page_size: int = 50,
    ) -> PrincipalPage:
        """Every principal in the caller's tenant, newest first.

        Undeclared principals are included and are the point. A filter is
        available for a reader who wants only agents; the default is everybody,
        because a roster that hid what it did not know would answer *"we have no
        agents"* to a deployment that has eleven nobody has declared.
        """
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            msg = f"page_size must be between 1 and {MAX_PAGE_SIZE}"
            raise ValidationError(msg)
        if actor_kind is not None and actor_kind not in ALL_KINDS:
            msg = f"unknown actor kind {actor_kind!r}; expected one of {sorted(ALL_KINDS)}"
            raise ValidationError(msg)

        statement = _ROSTER
        params: dict[str, Any] = {"tenant": ctx.tenant_id, "limit": page_size + 1}
        if actor_kind is not None:
            statement += " AND actor_kind = :kind"
            params["kind"] = actor_kind
        if cursor is not None:
            statement += " AND (created_at, actor_id) < (:cursor_created_at, :cursor_id)"
            params["cursor_created_at"], params["cursor_id"] = cursor
        statement += _ORDER + " LIMIT :limit"

        async with self._session_factory() as session:
            rows = (await session.execute(text(statement), params)).mappings().all()

        has_more = len(rows) > page_size
        items = tuple(_principal(row) for row in rows[:page_size])
        cursor_token: str | None = None
        if has_more and items:
            last = items[-1]
            cursor_token = f"{last.created_at.isoformat()}|{last.actor_id}"
        return PrincipalPage(items=items, next_cursor=cursor_token)

    async def declare(
        self,
        ctx: TenantContext,
        *,
        actor_id: uuid.UUID,
        actor_kind: str,
        owner_principal: str,
    ) -> Principal:
        """Record what a principal is, and who is accountable for it.

        A declaration, not a classification: nothing here reads the principal's
        behaviour, its transport or its event mix. Somebody says what it is, and
        the row records that they said so and when.

        Re-declaring is permitted and overwrites. A principal that was a human's
        session and is now an unattended agent is a real change, and refusing it
        would leave the roster wrong in the direction that matters.
        """
        if not (set(ctx.roles) & _OPERATOR_ROLES):
            msg = f"declaring a principal requires one of {sorted(_OPERATOR_ROLES)}"
            raise PermissionError(msg)
        if actor_kind not in DECLARABLE_KINDS:
            msg = (
                f"a principal is declared as one of {sorted(DECLARABLE_KINDS)}, not {actor_kind!r}. "
                "`sync_worker` and `system_curator` are this service's own provisioning, and "
                "`unknown` is what a principal is before anybody declares it."
            )
            raise ValidationError(msg)
        owner = owner_principal.strip()
        if not MIN_OWNER <= len(owner) <= MAX_OWNER:
            msg = (
                f"an owner must be {MIN_OWNER}-{MAX_OWNER} characters; it answers "
                "'who do I talk to about this agent', and a principal whose owner is unrecorded "
                "is one nobody is accountable for"
            )
            raise ValidationError(msg)

        async with self._session_factory() as session, session.begin():
            written = (
                await session.execute(
                    text(_DECLARE),
                    {
                        "kind": actor_kind,
                        "owner": owner,
                        "now": self._clock.now(),
                        "declarer": ctx.actor_id,
                        "actor": actor_id,
                        "tenant": ctx.tenant_id,
                    },
                )
            ).scalar_one_or_none()
            if written is None:
                msg = "no such principal in this tenant"
                raise NotFoundError(msg)
            row = (
                (
                    await session.execute(
                        text(_ROSTER + " AND actor_id = :actor"),
                        {"tenant": ctx.tenant_id, "actor": actor_id},
                    )
                )
                .mappings()
                .one()
            )
        return _principal(row)


def _principal(row: RowMapping) -> Principal:
    return Principal(
        actor_id=row["actor_id"],
        display_name=row["display_name"],
        oidc_subject=row["oidc_subject"],
        actor_kind=row["actor_kind"],
        owner_principal=row["owner_principal"],
        declared_at=row["declared_at"],
        declared_by=row["declared_by"],
        created_at=row["created_at"],
    )


def parse_cursor(token: str | None) -> tuple[datetime.datetime, uuid.UUID] | None:
    """The opaque token back into a keyset position, refusing one we did not issue."""
    if not token:
        return None
    stamped, _, identifier = token.partition("|")
    try:
        return datetime.datetime.fromisoformat(stamped), uuid.UUID(identifier)
    except ValueError as exc:
        msg = "this cursor was not issued by this surface"
        raise ValidationError(msg) from exc


__all__ = [
    "ALL_KINDS",
    "DECLARABLE_KINDS",
    "KIND_UNKNOWN",
    "MAX_PAGE_SIZE",
    "ActorDirectoryService",
    "Principal",
    "PrincipalPage",
    "parse_cursor",
]
