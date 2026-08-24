"""What a reporter said, read back to the reporter who said it.

E22-T9. `/v1/context/feedback` was `post`-only. A reader who judged a served
context item as irrelevant had that judgement consumed into floored cohort cells
and could not see what their own assessment did — to the item, to the
resolution, or to anything. **The one loop the product has for "how might this
be improved" was open at the far end.**

## Why this is not the differencing attack, and what it would take to become one

The decision governing aggregate reads is that an explorer which recomputes is a
differencing attack: two figures for the same cell, computed either side of an
erasure, subtract to name one person's contribution. Every floor holds while it
happens. See
[the explorer decision](../../.develop/adr/0013-an-explorer-that-recomputes-is-the-attack.md).

**This read is not a cell.** It returns *rows the caller wrote*, filtered on
`reporter_id = the caller`, and reading your own rows twice returns your own
rows twice. There is no population, so there is no remainder to subtract, and
the aggregates surface's defences are neither weakened nor duplicated here.

Two scopes are therefore permitted and a third is refused:

- **your own feedback**, which is attribution read back to its author;
- **feedback on one receipt**, which is a resolution the caller can already
  read in full — the judgements attached to it disclose nothing the receipt
  did not;
- **anything wider** — another reporter's rows, or a count over a population —
  is refused, and `RefusedScope` says which rule refused it rather than
  returning an empty page that reads as "no feedback exists".

That last refusal is the epic's own rule applied to the surface that tempted it
first: a governance property is never dropped to make a screen nicer.

## The note is returned, and that is a decision

`context_feedback.note` is free text and 0041 calls it *"the field most likely
to carry something personal"*. It is returned here **only to its own author**,
which is the one reader for whom it discloses nothing new. A surface that showed
notes across reporters would be the per-actor view the aggregates surface
refuses, arriving through a door marked evaluation.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.types import TenantContext

#: The most rows one page may carry. A reader scanning their own judgements is
#: reading, not exporting; a page this size is a screen and the cursor is how
#: somebody reaches the rest.
MAX_PAGE_SIZE: Final[int] = 100

#: Why a scope was refused, as a code rather than as prose. A caller's remedy
#: differs: `not_your_feedback` is a request for somebody else's attribution and
#: `population_scope` is an aggregate asking to be served as rows.
REFUSAL_NOT_YOURS: Final = "not_your_feedback"
REFUSAL_POPULATION: Final = "population_scope"


class RefusedScope(ValidationError):
    """A scope this read will not serve, with the reason it will not.

    Its own type so a transport can carry the code. An empty page would be the
    alternative and it is the wrong answer twice over: it reads as "no feedback
    exists", and it teaches a caller that the scope is permitted.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclasses.dataclass(frozen=True)
class RecordedJudgement:
    """One judgement, as it was recorded.

    `note` is present because this row's author is the one asking. See the
    module docstring on why that is the only reader it is returned to.
    """

    feedback_id: uuid.UUID
    kind: str
    rating: str
    learning_eligible: bool
    receipt_id: uuid.UUID | None
    receipt_item_id: str | None
    note: str | None
    created_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class JudgementPage:
    """One page of judgements, and where the next one starts."""

    items: tuple[RecordedJudgement, ...]
    next_cursor: str | None


#: Newest first, because a reader checking what their judgement did is asking
#: about the one they just made. Keyset on `(created_at, feedback_id)` so a row
#: written mid-pagination cannot displace one the caller has not seen.
_MINE = """
SELECT feedback_id, kind, rating, learning_eligible, receipt_id, receipt_item_id, note, created_at
  FROM context_feedback
 WHERE tenant_id = :tenant AND reporter_id = :reporter
 ORDER BY created_at DESC, feedback_id DESC
 LIMIT :limit
"""

_MINE_AFTER = """
SELECT feedback_id, kind, rating, learning_eligible, receipt_id, receipt_item_id, note, created_at
  FROM context_feedback
 WHERE tenant_id = :tenant AND reporter_id = :reporter
   AND (created_at, feedback_id) < (:cursor_created_at, :cursor_id)
 ORDER BY created_at DESC, feedback_id DESC
 LIMIT :limit
"""

#: Every judgement attached to one receipt, whoever made it -- but without the
#: note, and without the reporter. The receipt is a resolution the caller can
#: already read; what somebody thought of it is a fact about the resolution,
#: and *who* thought it is a fact about them.
_FOR_RECEIPT = """
SELECT feedback_id, kind, rating, learning_eligible, receipt_id, receipt_item_id,
       NULL AS note, created_at
  FROM context_feedback
 WHERE tenant_id = :tenant AND receipt_id = :receipt
 ORDER BY created_at DESC, feedback_id DESC
 LIMIT :limit
"""


class FeedbackReadService:
    """The read half of context feedback, scoped so it cannot become an explorer."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def mine(
        self,
        ctx: TenantContext,
        *,
        cursor: tuple[datetime.datetime, uuid.UUID] | None = None,
        page_size: int = 50,
    ) -> JudgementPage:
        """Every judgement this caller recorded, newest first.

        The reporter is taken from the caller's own context and never from an
        argument. `_assert_reporter_is_the_caller` already enforces that a human
        or agent writes as itself; accepting a reporter id here would reopen on
        the read side exactly what that check closes on the write side.
        """
        reporter = self._reporter(ctx)
        limit = self._limit(page_size)

        params: dict[str, Any] = {"tenant": ctx.tenant_id, "reporter": reporter, "limit": limit + 1}
        statement = _MINE
        if cursor is not None:
            statement = _MINE_AFTER
            params["cursor_created_at"], params["cursor_id"] = cursor

        async with self._session_factory() as session:
            rows = (await session.execute(text(statement), params)).mappings().all()
        return self._page(rows, limit)

    async def for_receipt(
        self,
        ctx: TenantContext,
        *,
        receipt_id: uuid.UUID,
        page_size: int = 50,
    ) -> JudgementPage:
        """What was judged about one resolution, without saying by whom.

        The receipt is something the caller can already read in full, so the
        ratings attached to it disclose nothing further about the resolution.
        The reporter and the note are withheld: those are facts about a person,
        and a surface that returned them across reporters would be the per-actor
        view the aggregates surface refuses, arriving through a door marked
        evaluation.
        """
        limit = self._limit(page_size)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(_FOR_RECEIPT),
                        {"tenant": ctx.tenant_id, "receipt": receipt_id, "limit": limit + 1},
                    )
                )
                .mappings()
                .all()
            )
        return self._page(rows, limit)

    @staticmethod
    def _reporter(ctx: TenantContext) -> str:
        if ctx.actor_id is None:
            msg = (
                "this read returns the caller's own judgements and the caller has no actor identity; "
                "there is nothing it could scope to"
            )
            raise RefusedScope(REFUSAL_NOT_YOURS, msg)
        return str(ctx.actor_id)

    @staticmethod
    def _limit(page_size: int) -> int:
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            msg = f"page_size must be between 1 and {MAX_PAGE_SIZE}"
            raise ValidationError(msg)
        return page_size

    @staticmethod
    def _page(rows: Sequence[Any], limit: int) -> JudgementPage:
        """Rows into a page, with the cursor drawn from the last row served.

        Fetched `limit + 1` so "is there more" is answered without a second
        query, and the extra row is dropped rather than served.
        """
        has_more = len(rows) > limit
        kept = rows[:limit]
        items = tuple(
            RecordedJudgement(
                feedback_id=row["feedback_id"],
                kind=row["kind"],
                rating=row["rating"],
                learning_eligible=bool(row["learning_eligible"]),
                receipt_id=row["receipt_id"],
                receipt_item_id=row["receipt_item_id"],
                note=row["note"],
                created_at=row["created_at"],
            )
            for row in kept
        )
        cursor: str | None = None
        if has_more and items:
            last = items[-1]
            cursor = f"{last.created_at.isoformat()}|{last.feedback_id}"
        return JudgementPage(items=items, next_cursor=cursor)


def parse_cursor(token: str | None) -> tuple[datetime.datetime, uuid.UUID] | None:
    """The opaque token back into a keyset position.

    Refuses a malformed token rather than ignoring it. A cursor silently
    discarded restarts the page at the beginning, which a caller paging through
    their own judgements would read as the list having changed under them.
    """
    if not token:
        return None
    stamped, _, identifier = token.partition("|")
    try:
        return datetime.datetime.fromisoformat(stamped), uuid.UUID(identifier)
    except ValueError as exc:
        msg = "this cursor was not issued by this surface"
        raise ValidationError(msg) from exc


__all__ = [
    "MAX_PAGE_SIZE",
    "REFUSAL_NOT_YOURS",
    "REFUSAL_POPULATION",
    "FeedbackReadService",
    "JudgementPage",
    "RecordedJudgement",
    "RefusedScope",
    "parse_cursor",
]
