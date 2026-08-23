"""Versioned agent instructions: proposed, activated against evidence, reversible.

E20-T7, the write half of the retraining loop. A failure-pattern report says
what an agent keeps getting wrong; an instruction is somebody's answer to it,
and this module governs the lineage of those answers rather than their content.

**Shaped after `CalibrationService.publish`.** Demote the current active row and
activate the new one inside one transaction, so there is never an instant with
two active versions or none. The partial unique index makes the first of those
unrepresentable; the transaction makes the second impossible to observe.

**Activation requires citing a report, and the check is here twice on purpose.**
The database refuses `status='active'` with a null `motivated_by_report_id`, and
this service refuses it first with a message naming what is missing. The DB
constraint is what makes the rule true for every writer; the service check is
what makes the failure legible instead of an opaque constraint violation. The
same doubling `CalibrationService` uses for its measured-error gate.

**Rollback is the same primitive run backwards**, not a second mechanism. It
supersedes the active version and reactivates the most recently superseded one.
An agent's first version has no predecessor, and that returns `None` rather than
raising -- "there is nothing to roll back to" is an answer a caller acts on, not
an error.

**`content` is untouched free text.** This module does not read it, validate it,
template it or diff it. What a better instruction says is a human decision; what
this owns is which version is in force, what evidence justified it, and how to
get back.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.types import TenantContext

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_REJECTED = "rejected"


@dataclasses.dataclass(frozen=True)
class Instruction:
    """One version of an agent's instructions."""

    instruction_id: uuid.UUID
    author_actor_id: uuid.UUID
    version: int
    content: str
    motivated_by_report_id: uuid.UUID | None
    status: str
    activated_at: datetime.datetime | None
    superseded_at: datetime.datetime | None


_SELECT = """
SELECT instruction_id, author_actor_id, version, content, motivated_by_report_id,
       status, activated_at, superseded_at
  FROM agent_instruction
"""


def _row_to_instruction(row: object) -> Instruction:
    return Instruction(
        instruction_id=row.instruction_id,  # type: ignore[attr-defined]
        author_actor_id=row.author_actor_id,  # type: ignore[attr-defined]
        version=int(row.version),  # type: ignore[attr-defined]
        content=str(row.content),  # type: ignore[attr-defined]
        motivated_by_report_id=row.motivated_by_report_id,  # type: ignore[attr-defined]
        status=str(row.status),  # type: ignore[attr-defined]
        activated_at=row.activated_at,  # type: ignore[attr-defined]
        superseded_at=row.superseded_at,  # type: ignore[attr-defined]
    )


class AgentInstructionService:
    """Proposes, activates and rolls back one agent's instruction versions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def propose(
        self,
        ctx: TenantContext,
        *,
        author_actor_id: uuid.UUID,
        version: int,
        content: str,
        motivated_by_report_id: uuid.UUID,
    ) -> uuid.UUID:
        """Record a new version. There is no path from here to active.

        Proposing and activating are separate calls because they are separate
        decisions: writing a candidate instruction is cheap and reversible,
        putting it in force is neither.

        The report is resolved before the insert, and **scoped to the same
        author**. A version citing another agent's failure report would satisfy
        the database's foreign key and the activation CHECK while being
        evidence about somebody else's work -- the one way to get a
        well-formed, fully-constrained row that means nothing.
        """
        if not content.strip():
            raise ValidationError("an instruction needs content; an empty version is not a decision")

        async with self._session_factory() as session, session.begin():
            report_actor = (
                await session.execute(
                    text(
                        "SELECT author_actor_id FROM agent_failure_pattern_report "
                        " WHERE report_id = :r AND tenant_id = :t"
                    ),
                    {"r": motivated_by_report_id, "t": ctx.tenant_id},
                )
            ).scalar_one_or_none()
            if report_actor is None:
                raise NotFoundError(
                    f"failure-pattern report {motivated_by_report_id} not found in this tenant; "
                    "an instruction cites the evidence that motivated it, so the evidence has to exist"
                )
            if report_actor != author_actor_id:
                raise ValidationError(
                    f"report {motivated_by_report_id} is about actor {report_actor}, not {author_actor_id}; "
                    "an instruction must cite a report about the agent it governs"
                )

            inserted = (
                await session.execute(
                    text(
                        "INSERT INTO agent_instruction "
                        "  (tenant_id, author_actor_id, version, content, motivated_by_report_id, status, created_at) "
                        "VALUES (:t, :a, :v, :c, :r, 'superseded', now()) "
                        "RETURNING instruction_id"
                    ),
                    {
                        "t": ctx.tenant_id,
                        "a": author_actor_id,
                        "v": version,
                        "c": content,
                        "r": motivated_by_report_id,
                    },
                )
            ).scalar_one()
            # `scalar_one` is typed `Any`; the column is a UUID primary key.
            # `cast` rather than an `assert`, which would be stripped under -O.
            return cast("uuid.UUID", inserted)

    async def activate(self, ctx: TenantContext, *, instruction_id: uuid.UUID, now: datetime.datetime) -> None:
        """Put this version in force, demoting whatever held it.

        One transaction, demote then activate -- `CalibrationService.publish`'s
        shape. Doing it the other way round would momentarily violate the
        partial unique index, and doing it in two transactions would leave a
        window with no active version at all.
        """
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT author_actor_id, status, motivated_by_report_id FROM agent_instruction "
                        " WHERE instruction_id = :i AND tenant_id = :t FOR UPDATE"
                    ),
                    {"i": instruction_id, "t": ctx.tenant_id},
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError(f"instruction {instruction_id} not found")
            if row.status == STATUS_ACTIVE:
                raise ConflictError(f"instruction {instruction_id} is already active")
            if row.motivated_by_report_id is None:
                # Redundant with the database CHECK, and better: this says which
                # field is missing rather than which constraint fired.
                raise ValidationError(
                    f"instruction {instruction_id} cites no failure-pattern report and cannot be activated; "
                    "an instruction in force has to say what evidence justified it"
                )

            await self._demote_active(session, author_actor_id=row.author_actor_id, now=now)
            await session.execute(
                text(
                    "UPDATE agent_instruction "
                    "   SET status = 'active', activated_at = CAST(:now AS TIMESTAMPTZ), superseded_at = NULL "
                    " WHERE instruction_id = :i"
                ),
                {"i": instruction_id, "now": now},
            )

    async def rollback(
        self, ctx: TenantContext, *, author_actor_id: uuid.UUID, now: datetime.datetime
    ) -> uuid.UUID | None:
        """Return to the previously active version. Returns which, or None.

        `None` when there is no predecessor -- an agent's first version has
        nothing behind it. An error would be wrong: "there is nothing to roll
        back to" is a fact the caller acts on, not a failure of the request.

        The predecessor is the most recently *superseded* version by
        `activated_at`, which is deliberately not the highest version number: a
        rollback then a re-activation can leave those in different orders, and
        what a caller wants back is what was in force before, not whatever is
        numerically adjacent.
        """
        async with self._session_factory() as session, session.begin():
            predecessor = (
                await session.execute(
                    text(
                        "SELECT instruction_id FROM agent_instruction "
                        " WHERE tenant_id = :t AND author_actor_id = :a "
                        "   AND status = 'superseded' AND activated_at IS NOT NULL "
                        " ORDER BY activated_at DESC LIMIT 1"
                    ),
                    {"t": ctx.tenant_id, "a": author_actor_id},
                )
            ).scalar_one_or_none()
            if predecessor is None:
                return None

            await self._demote_active(session, author_actor_id=author_actor_id, now=now)
            await session.execute(
                text(
                    "UPDATE agent_instruction "
                    "   SET status = 'active', activated_at = CAST(:now AS TIMESTAMPTZ), superseded_at = NULL "
                    " WHERE instruction_id = :i"
                ),
                {"i": predecessor, "now": now},
            )
            return cast("uuid.UUID", predecessor)

    async def active_instruction(self, ctx: TenantContext, *, author_actor_id: uuid.UUID) -> Instruction | None:
        """Which version is in force, or None when none ever was."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(f"{_SELECT} WHERE tenant_id = :t AND author_actor_id = :a AND status = 'active'"),
                    {"t": ctx.tenant_id, "a": author_actor_id},
                )
            ).one_or_none()
        return None if row is None else _row_to_instruction(row)

    async def history(self, ctx: TenantContext, *, author_actor_id: uuid.UUID) -> tuple[Instruction, ...]:
        """Every version, newest first. Rejected ones included.

        A rejected proposal is part of the record: "we considered this and did
        not ship it" is what stops the same change being proposed twice.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(f"{_SELECT} WHERE tenant_id = :t AND author_actor_id = :a ORDER BY version DESC"),
                    {"t": ctx.tenant_id, "a": author_actor_id},
                )
            ).all()
        return tuple(_row_to_instruction(row) for row in rows)

    @staticmethod
    async def _demote_active(session: AsyncSession, *, author_actor_id: uuid.UUID, now: datetime.datetime) -> None:
        """Supersede whichever version is in force. A no-op when none is."""
        await session.execute(
            text(
                "UPDATE agent_instruction "
                "   SET status = 'superseded', superseded_at = CAST(:now AS TIMESTAMPTZ) "
                " WHERE author_actor_id = :a AND status = 'active'"
            ),
            {"a": author_actor_id, "now": now},
        )


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_REJECTED",
    "STATUS_SUPERSEDED",
    "AgentInstructionService",
    "Instruction",
]
