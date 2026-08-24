"""Which revisions exist, so `/revisions` can be a page rather than four text boxes.

E22-T8. The contract had seven `revisions/{revision_id}` paths and **every one
was keyed by a `revision_id` the caller must already hold** — `activate` ×2,
`revoke` ×2, `invalidate`, `approval-evidence`, `activation-eligibility`. There
was no way to ask which revisions exist.

That is why `RevisionLifecyclePanel.tsx` is four text boxes. Not because the
screen was designed badly, but because nothing could have been designed well
against that surface.

## The field this list exists for

`RevisionLifecyclePanel` argues correctly that the two terminal acts differ in
**reach over the past**, not in reversibility:

- *invalidate* — "every resolution made while this revision was active is now in
  question";
- *revoke* — "everything resolved while this revision was in force stands".

So the number that makes the choice decidable is **how many resolutions were
made under the revision**, and `resolutions_under_revision` carries it from
`arc_receipt_selected_revisions`. Without it a reader picks between two terminal
acts on the strength of a paragraph, which is what they do today.

Omitted selections are excluded from the count. A revision a resolution
*considered and left out* was not something anything was decided under, and
counting it would inflate the number a reader is using to judge blast radius —
in the direction that makes invalidate look worse than it is.

## Activation eligibility is named, not answered

`ActivationService.get_eligibility` reports **ten predicates**, computed as if
the calling principal were the one activating. Running that per row would either
be slow or would be a second, weaker computation wearing the same name — and the
second is worse, because two surfaces would disagree about whether a revision
can activate.

So this list carries three facts that are columns on the row — is it a draft,
does it have approval evidence, has its review window expired — and no verdict.
They answer *"is this one worth opening"*. Whether it can actually activate is
the per-revision endpoint's question and stays there.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.exceptions import ValidationError
from contextplane.types import Clock, TenantContext

#: The most rows one page may carry.
MAX_PAGE_SIZE: Final[int] = 100

#: Lifecycle states from which no further act is possible. `superseded` is here
#: with the other two because a superseded revision is finished even though
#: nobody acted on it directly -- a reader deciding what to do about it needs to
#: know that the answer is "nothing".
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"revoked", "expired", "superseded"})

#: What may be filtered on. Closed, because a state nobody registered would
#: return an empty page that reads as "no revisions are in that state" rather
#: than as "that is not a state".
LIFECYCLE_STATES: Final[tuple[str, ...]] = ("draft", "active", "superseded", "revoked", "expired")

_INDEX = """
SELECT r.revision_id,
       r.artifact_id,
       a.slug AS artifact_slug,
       a.kind AS artifact_kind,
       r.lifecycle_state,
       r.source_system,
       r.source_revision_locator,
       r.content_digest,
       r.approval_evidence_id,
       r.effective_from,
       r.effective_until,
       r.review_expires_at,
       r.activated_at,
       r.revoked_at,
       r.created_at,
       (SELECT count(*)
          FROM arc_receipt_selected_revisions s
         WHERE s.revision_id = r.revision_id
           AND s.was_omitted = FALSE) AS resolutions_under_revision
  FROM arc_revisions r
  JOIN arc_artifacts a ON a.artifact_id = r.artifact_id
 WHERE (r.tenant_id = :tenant OR r.tenant_id IS NULL)
"""

#: Newest first, then by id. Both components are fixed for a row's lifetime, so
#: a keyset over them cannot skip a revision or serve one twice while somebody
#: pages through -- which for a screen offering two irreversible acts is worth
#: more than it costs.
_ORDER = " ORDER BY r.created_at DESC, r.revision_id DESC"


@dataclasses.dataclass(frozen=True)
class RevisionRow:
    """One revision, and what a reader needs before opening it."""

    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    #: The directive this revision belongs to, named rather than only keyed: a
    #: reader choosing between two terminal acts needs to know what they are
    #: acting on, and a UUID is the thing this whole epic is removing.
    artifact_slug: str
    artifact_kind: str
    lifecycle_state: str
    source_system: str
    source_revision_locator: str
    content_digest: str
    approval_evidence_id: uuid.UUID | None
    effective_from: datetime.datetime
    effective_until: datetime.datetime | None
    review_expires_at: datetime.datetime
    activated_at: datetime.datetime | None
    revoked_at: datetime.datetime | None
    created_at: datetime.datetime

    #: How many resolutions were made under this revision. The field the two
    #: terminal acts differ over -- see the module docstring.
    resolutions_under_revision: int

    #: Three columns, no verdict. Whether it *can* activate is the
    #: per-revision eligibility endpoint's answer and is not duplicated here.
    is_draft: bool
    has_approval_evidence: bool
    review_expired: bool

    #: Whether anything further is possible. A reader deciding what to do about
    #: a finished revision needs to be told the answer is "nothing", rather than
    #: inferring it from a state name.
    is_terminal: bool


@dataclasses.dataclass(frozen=True)
class RevisionPage:
    items: tuple[RevisionRow, ...]
    next_cursor: str | None


class RevisionIndexService:
    """The read that makes `/revisions` a page."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def list_revisions(
        self,
        ctx: TenantContext,
        *,
        lifecycle_state: str | None = None,
        artifact_id: uuid.UUID | None = None,
        cursor: tuple[datetime.datetime, uuid.UUID] | None = None,
        page_size: int = 50,
    ) -> RevisionPage:
        """Revisions this tenant can see, newest first.

        Platform-scoped revisions (`tenant_id IS NULL`) are included, because a
        tenant is governed by them and a list that hid them would show a reader
        a partial account of what is in force over them.
        """
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            msg = f"page_size must be between 1 and {MAX_PAGE_SIZE}"
            raise ValidationError(msg)
        if lifecycle_state is not None and lifecycle_state not in LIFECYCLE_STATES:
            msg = (
                f"unknown lifecycle state {lifecycle_state!r}; expected one of "
                f"{sorted(LIFECYCLE_STATES)}. An unrecognised state would return an empty page "
                "that reads as 'none are in that state'."
            )
            raise ValidationError(msg)

        statement = _INDEX
        params: dict[str, Any] = {"tenant": ctx.tenant_id, "limit": page_size + 1}
        if lifecycle_state is not None:
            statement += " AND r.lifecycle_state = :state"
            params["state"] = lifecycle_state
        if artifact_id is not None:
            statement += " AND r.artifact_id = :artifact"
            params["artifact"] = artifact_id
        if cursor is not None:
            statement += " AND (r.created_at, r.revision_id) < (:cursor_created_at, :cursor_id)"
            params["cursor_created_at"], params["cursor_id"] = cursor
        statement += _ORDER + " LIMIT :limit"

        async with self._session_factory() as session:
            rows = (await session.execute(text(statement), params)).mappings().all()

        now = self._clock.now()
        has_more = len(rows) > page_size
        items = tuple(self._row(row, now) for row in rows[:page_size])
        cursor_token: str | None = None
        if has_more and items:
            last = items[-1]
            cursor_token = f"{last.created_at.isoformat()}|{last.revision_id}"
        return RevisionPage(items=items, next_cursor=cursor_token)

    @staticmethod
    def _row(row: RowMapping, now: datetime.datetime) -> RevisionRow:
        state = str(row["lifecycle_state"])
        return RevisionRow(
            revision_id=row["revision_id"],
            artifact_id=row["artifact_id"],
            artifact_slug=row["artifact_slug"],
            artifact_kind=row["artifact_kind"],
            lifecycle_state=state,
            source_system=row["source_system"],
            source_revision_locator=row["source_revision_locator"],
            content_digest=row["content_digest"],
            approval_evidence_id=row["approval_evidence_id"],
            effective_from=row["effective_from"],
            effective_until=row["effective_until"],
            review_expires_at=row["review_expires_at"],
            activated_at=row["activated_at"],
            revoked_at=row["revoked_at"],
            created_at=row["created_at"],
            resolutions_under_revision=int(row["resolutions_under_revision"]),
            is_draft=state == "draft",
            has_approval_evidence=row["approval_evidence_id"] is not None,
            review_expired=row["review_expires_at"] <= now,
            is_terminal=state in TERMINAL_STATES,
        )


def parse_cursor(token: str | None) -> tuple[datetime.datetime, uuid.UUID] | None:
    """The opaque token back into a keyset position.

    Refuses a token this surface did not issue rather than ignoring it: a cursor
    silently discarded restarts at the top, and a reader paging through
    revisions to find one to act on would read that as the list having changed.
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
    "LIFECYCLE_STATES",
    "MAX_PAGE_SIZE",
    "TERMINAL_STATES",
    "RevisionIndexService",
    "RevisionPage",
    "RevisionRow",
    "parse_cursor",
]
