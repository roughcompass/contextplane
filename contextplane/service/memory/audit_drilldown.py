"""Per-actor detail for an auditor, and the record that makes it accountable.

E11-T3. `ROLE_AUDITOR` already exists, so this is an authorization question with
an answer rather than a role to invent. What did not exist is the record: a role
is a door, and a door says nothing about who walked through it or why.

## The justification is written first, in the same transaction

Not afterwards, and not alongside. A justification captured after the read is a
field that is empty exactly when it matters — the read somebody did not want to
explain is the read that completes and leaves no note. So the insert and the
query share one transaction, and the insert goes first: if the record cannot be
written, the caller does not get the data.

`resolve.py` makes the same move about its receipt — *"an answer nobody can
later show they were given is the thing receipts exist to prevent"*. This is
that argument turned around, about the reader instead of the answer.

## Why this is its own surface

`api/routers/learning_reads.py` is owner-facing and deliberately has **no
per-actor path**; `tests/conformance/test_feedback_privacy.py` pins that
structurally, over its whole route table. Adding a per-actor route there would
have deleted the only thing keeping that surface actor-free, and the pin's own
docstring warns that a failure means *"somebody widened this surface"*.

So the drill-down is a separate service and a separate router, behind a
different role. The aggregate surface stays what it says it is, and the
capability that needs accounting for lives where the accounting is.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.types import Clock, TenantContext

#: Matches the database CHECK. Refused here first so a caller gets a sentence
#: naming the field rather than a constraint violation.
MIN_JUSTIFICATION: Final[int] = 20
MAX_JUSTIFICATION: Final[int] = 2000

#: What an auditor may ask about one actor. Closed, and the same vocabulary the
#: aggregate surface uses, so a recorded question reads beside what the tenant
#: figures already show.
METRIC_CLAIMS_AUTHORED: Final[str] = "claims_authored"
METRIC_CASES_DISPOSED: Final[str] = "cases_disposed"
DRILLDOWN_METRICS: Final[tuple[str, ...]] = (METRIC_CLAIMS_AUTHORED, METRIC_CASES_DISPOSED)

_COUNTS: Final[dict[str, str]] = {
    METRIC_CLAIMS_AUTHORED: (
        "SELECT count(*) AS n FROM memory_claims "
        " WHERE author_tenant_id = :tenant AND author_actor_id = :subject "
        "   AND created_at >= :start AND created_at < :end"
    ),
    METRIC_CASES_DISPOSED: (
        "SELECT count(*) AS n FROM curation_cases "
        " WHERE tenant_id = :tenant AND owner_id = CAST(:subject AS TEXT) "
        "   AND resolved_at >= :start AND resolved_at < :end"
    ),
}


@dataclasses.dataclass(frozen=True)
class ActorDetail:
    """One actor's figure, and the id of the record that says why it was read.

    `read_id` is returned rather than kept private: an auditor should be able to
    cite the record of their own question, and a surface that recorded something
    it would not show the caller is a surface people learn to distrust.
    """

    subject_actor_id: uuid.UUID
    metric: str
    window_start: datetime.datetime
    window_end: datetime.datetime
    value: int
    read_id: uuid.UUID


class AuditDrilldownService:
    """Per-actor reads, each one recorded before it is answered."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def read_actor_metric(
        self,
        ctx: TenantContext,
        *,
        subject_actor_id: uuid.UUID,
        metric: str,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        justification: str,
    ) -> ActorDetail:
        """One actor's figure, after writing down why it was asked for.

        Every refusal happens before the transaction opens, so a rejected
        request leaves no record of a read that did not occur — the log is of
        reads, not of attempts, and mixing the two would make it useless for
        both questions.
        """
        if metric not in _COUNTS:
            msg = f"unknown drill-down metric {metric!r}; expected one of {sorted(DRILLDOWN_METRICS)}"
            raise ValidationError(msg)
        cleaned = justification.strip()
        if not MIN_JUSTIFICATION <= len(cleaned) <= MAX_JUSTIFICATION:
            msg = (
                f"a justification must be {MIN_JUSTIFICATION}-{MAX_JUSTIFICATION} characters; "
                "the point is a sentence somebody has to be willing to have read back to them"
            )
            raise ValidationError(msg)
        if window_end <= window_start:
            msg = "window_end must be after window_start"
            raise ValidationError(msg)

        read_id = uuid.uuid4()
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            # First, and in this transaction. If this insert fails the caller
            # gets nothing: a read that could not be recorded is a read that
            # does not happen.
            await session.execute(
                text(
                    "INSERT INTO audit_justified_reads ("
                    "  read_id, tenant_id, auditor_actor_id, subject_actor_id, metric,"
                    "  window_start, window_end, justification, read_at"
                    ") VALUES (:rid, :tenant, :auditor, :subject, :metric, :start, :end, :why, :now)"
                ),
                {
                    "rid": read_id,
                    "tenant": ctx.tenant_id,
                    "auditor": ctx.actor_id,
                    "subject": subject_actor_id,
                    "metric": metric,
                    "start": window_start,
                    "end": window_end,
                    "why": cleaned,
                    "now": now,
                },
            )
            value = int(
                (
                    await session.execute(
                        text(_COUNTS[metric]),
                        {
                            "tenant": ctx.tenant_id,
                            "subject": subject_actor_id,
                            "start": window_start,
                            "end": window_end,
                        },
                    )
                ).scalar_one()
            )

        return ActorDetail(
            subject_actor_id=subject_actor_id,
            metric=metric,
            window_start=window_start,
            window_end=window_end,
            value=value,
            read_id=read_id,
        )

    async def reads_of_subject(
        self,
        ctx: TenantContext,
        *,
        subject_actor_id: uuid.UUID,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Who has looked at this actor, and what they said.

        The direction that makes the record more than bookkeeping. A log only
        its own author can read is a log that disciplines nobody; this is the
        query a subject — or somebody acting for them — actually asks.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT read_id, auditor_actor_id, metric, window_start, window_end, "
                        "       justification, read_at "
                        "  FROM audit_justified_reads "
                        " WHERE tenant_id = :tenant AND subject_actor_id = :subject "
                        " ORDER BY read_at DESC LIMIT :limit"
                    ),
                    {"tenant": ctx.tenant_id, "subject": subject_actor_id, "limit": limit},
                )
            ).all()
        return [
            {
                "read_id": str(row.read_id),
                "auditor_actor_id": str(row.auditor_actor_id),
                "metric": row.metric,
                "window_start": row.window_start,
                "window_end": row.window_end,
                "justification": row.justification,
                "read_at": row.read_at,
            }
            for row in rows
        ]
