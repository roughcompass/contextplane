"""Feedback about a served answer, bound to exactly what it is about.

Three shapes, and the difference between them is what may be learned from them:

- **item-specific** cites a receipt *and* an exact item on it;
- **receipt-level** cites a receipt and must not cite an item;
- **diagnostic observation** cites neither and is never learning-eligible.

The union is closed here and again in the schema. That duplication is deliberate
and is not the usual two-places-to-change problem: the database refuses a row
whose shape is wrong no matter which writer produced it, and this module refuses a
*request* with an error a caller can act on. A constraint violation surfacing as a
500 tells an operator something broke; it does not tell the caller they sent
receipt-level feedback with an item on it.

**The exact-item rule is resolved, not trusted.** A caller naming a receipt and an
item is asserting that the item is on that receipt, and the assertion is checked
against the receipt's own rows before anything is written. The database enforces
the same thing through a composite foreign key, so a bug here cannot corrupt the
ledger -- but the check has to happen at this layer anyway, because "that item
belongs to a different receipt" and "that item does not exist" are different
answers to the caller and the constraint gives neither.

**Authorization is the receipt's tenant, resolved before the item.** A receipt
belonging to another tenant answers exactly as a receipt that does not exist,
because a distinguishable refusal turns a receipt id into a cross-tenant existence
oracle -- someone enumerating ids learns which resolutions happened elsewhere. The
tenant predicate is in the read itself rather than compared afterwards: a read that
loads the row and then checks has already loaded a row it may not see, and the
comparison is one refactor from disappearing.

**Nothing here infers a rating.** An implicit external outcome -- a build that
failed, a deployment rolled back -- is an observation and belongs in the signal
ledger. It is not a reporter asserting that an answer was wrong, and this module
accepts no signal id precisely so the two cannot be conflated by a write path that
finds it convenient. A rating arrives because somebody stated it.

**Learning eligibility is bounded here, not merely recorded.** A diagnostic
observation cites nothing, so nothing can check what it refers to; admitting one to
the derivation path would let an unattributable complaint become evidence about a
specific retrieved item. The caller may lower eligibility on the other two shapes
-- withholding feedback from learning is a legitimate thing to ask for -- and may
never raise it on this one.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import uuid
from typing import Any, Final

from prometheus_client import Counter
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.context.models_receipt import ContextReceipt, ContextReceiptItem
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.types import Clock, TenantContext

# The three members. Closed here and in the schema; see the module docstring for
# why both.
KIND_ITEM_SPECIFIC: Final = "item_specific"
KIND_RECEIPT_LEVEL: Final = "receipt_level"
KIND_DIAGNOSTIC: Final = "diagnostic_observation"

FEEDBACK_KINDS: Final[frozenset[str]] = frozenset({KIND_ITEM_SPECIFIC, KIND_RECEIPT_LEVEL, KIND_DIAGNOSTIC})

# The bounded vocabulary. A verdict nobody declared is one no learning or
# evaluation rule accounts for, so an unknown one is refused rather than stored
# and puzzled over later.
RATINGS: Final[frozenset[str]] = frozenset(
    {
        "relevant",
        "irrelevant",
        "missing",
        "stale",
        "incorrect",
        "contradicted",
        "unsafe",
        "selected",
        "ignored",
        "succeeded",
        "failed",
        "rolled_back",
        "needs_human_review",
    }
)

REPORTER_TYPES: Final[frozenset[str]] = frozenset({"human", "agent", "external"})

# Separate metric families, one per question an operator actually asks. A single
# counter with an outcome label would make "how much feedback are we taking" and
# "how often is a caller getting the binding wrong" the same query with a filter,
# and the second is the one worth alerting on: a client that has started sending
# mismatched bindings is broken in a way no amount of accepted feedback offsets.
FEEDBACK_ACCEPTED_TOTAL: Final = Counter(
    "contextplane_feedback_accepted_total",
    "Feedback submissions stored, by kind and whether they may be learned from.",
    ("kind", "learning_eligible"),
)
FEEDBACK_REPLAYED_TOTAL: Final = Counter(
    "contextplane_feedback_replayed_total",
    "Feedback submissions recognised as an exact replay of a stored one, by kind.",
    ("kind",),
)
FEEDBACK_REFUSED_TOTAL: Final = Counter(
    "contextplane_feedback_refused_total",
    "Feedback submissions refused before any write, by the reason they were refused.",
    ("reason",),
)

# Refusal reasons, as metric label values. Named constants rather than inline
# strings because these are the series an operator alerts on, and a typo in one
# creates a second series that reads as zero forever.
_REASON_UNKNOWN_KIND: Final = "unknown_kind"
_REASON_UNKNOWN_RATING: Final = "unknown_rating"
_REASON_UNKNOWN_REPORTER_TYPE: Final = "unknown_reporter_type"
_REASON_SHAPE: Final = "shape_violates_discriminant"
_REASON_RECEIPT_NOT_FOUND: Final = "receipt_not_found_or_not_yours"
_REASON_ITEM_NOT_ON_RECEIPT: Final = "item_not_on_receipt"
_REASON_DIAGNOSTIC_LEARNING: Final = "diagnostic_cannot_be_learning_eligible"
_REASON_IDEMPOTENCY_CONFLICT: Final = "idempotency_conflict"


@dataclasses.dataclass(frozen=True)
class FeedbackSubmissionV1:
    """What a reporter says about a served answer, before any of it is checked."""

    kind: str
    rating: str
    reporter_id: str
    reporter_type: str
    idempotency_key: str
    receipt_id: uuid.UUID | None = None
    receipt_item_id: str | None = None
    note: str | None = None
    # The caller may withhold otherwise-eligible feedback from learning. It may
    # never grant eligibility to a diagnostic observation; see `_resolve_shape`.
    learning_eligible: bool = True


@dataclasses.dataclass(frozen=True)
class RecordedFeedback:
    """What was stored, and whether this call is what stored it."""

    feedback_id: uuid.UUID
    kind: str
    rating: str
    learning_eligible: bool
    receipt_id: uuid.UUID | None
    receipt_item_id: str | None
    content_digest: str
    created_at: datetime.datetime
    replayed: bool


def _canonical_json(value: object) -> str:
    """Stable JSON for digesting: sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def content_digest_for(submission: FeedbackSubmissionV1) -> str:
    """Digest what the submission asserts, so a replay is decidable.

    Covers the fields that make two submissions the same report -- shape, verdict,
    binding, reporter and note. Deliberately excludes the idempotency key: the key
    identifies the submission and the digest identifies its content, and folding
    one into the other would make every retry under a fresh key look like changed
    content.
    """
    return (
        "sha256:"
        + hashlib.sha256(
            _canonical_json(
                {
                    "kind": submission.kind,
                    "rating": submission.rating,
                    "reporter_id": submission.reporter_id,
                    "reporter_type": submission.reporter_type,
                    "receipt_id": str(submission.receipt_id) if submission.receipt_id else None,
                    "receipt_item_id": submission.receipt_item_id,
                    "note": submission.note,
                    "learning_eligible": submission.learning_eligible,
                }
            ).encode()
        ).hexdigest()
    )


def _refuse(reason: str, message: str, *, error: type[Exception] = ValidationError) -> Exception:
    """Count the refusal and build the error, so no refusal path can forget the metric."""
    FEEDBACK_REFUSED_TOTAL.labels(reason=reason).inc()
    return error(message)


def _assert_reporter_is_the_caller(ctx: TenantContext, submission: FeedbackSubmissionV1) -> None:
    """A human or agent reports as itself.

    The reporter id is attribution: the stored row says this participant said
    this. Letting one caller write another's id files a complaint under a name
    with the actual reporter nowhere in the row, which is worse than anonymous
    because it looks attributed. An `external` reporter names a system whose id
    space is its own and unverifiable here, so it is left alone.
    """
    if submission.reporter_type == "external":
        return
    actor = str(ctx.actor_id) if ctx.actor_id is not None else None
    if actor is not None and submission.reporter_id != actor:
        raise _refuse(
            _REASON_UNKNOWN_REPORTER_TYPE,
            "a human or agent reporter may only report as itself",
        )


class FeedbackService:
    """Records feedback, having first resolved what it is about.

    Holds no policy of its own: the session factory and the clock are the whole
    of its construction, so one instance carries nothing a fresh one would not.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def record(self, ctx: TenantContext, submission: FeedbackSubmissionV1) -> RecordedFeedback:
        """Validate the shape, resolve the binding, then store exactly once.

        Order matters and is asserted by the conformance suite: every refusal
        happens before any write, so a rejected submission leaves nothing behind
        -- no row, no partially-resolved binding, and nothing for a derivation
        path to later find and treat as evidence.
        """
        self._validate_vocabulary(submission)
        resolved = self._resolve_shape(submission)
        _assert_reporter_is_the_caller(ctx, resolved)

        async with self._session_factory() as session:
            if resolved.receipt_id is not None:
                await self._resolve_binding(session, ctx, resolved)
            return await self._store(session, ctx, resolved)

    def _validate_vocabulary(self, submission: FeedbackSubmissionV1) -> None:
        if submission.kind not in FEEDBACK_KINDS:
            raise _refuse(
                _REASON_UNKNOWN_KIND,
                f"unknown feedback kind {submission.kind!r}; expected one of {sorted(FEEDBACK_KINDS)}",
            )
        if submission.rating not in RATINGS:
            raise _refuse(
                _REASON_UNKNOWN_RATING,
                f"unknown rating {submission.rating!r}; expected one of {sorted(RATINGS)}",
            )
        if submission.reporter_type not in REPORTER_TYPES:
            raise _refuse(
                _REASON_UNKNOWN_REPORTER_TYPE,
                f"unknown reporter type {submission.reporter_type!r}; expected one of {sorted(REPORTER_TYPES)}",
            )

    def _resolve_shape(self, submission: FeedbackSubmissionV1) -> FeedbackSubmissionV1:
        """Enforce the union, and force diagnostic eligibility to false.

        The diagnostic case is *forced* rather than refused when the caller asks
        for eligibility, because the caller is not asserting something false --
        `learning_eligible` defaults to true and a diagnostic reporter has no
        reason to think about it. Refusing here would make the ordinary diagnostic
        call fail on a field the reporter never set. Asking for it on a shape that
        cites nothing simply cannot be granted.
        """
        if submission.kind == KIND_ITEM_SPECIFIC:
            if submission.receipt_id is None or not submission.receipt_item_id:
                raise _refuse(
                    _REASON_SHAPE,
                    "item-specific feedback needs both a receipt and an exact item on it",
                )
            return submission
        if submission.kind == KIND_RECEIPT_LEVEL:
            if submission.receipt_id is None:
                raise _refuse(_REASON_SHAPE, "receipt-level feedback needs a receipt")
            if submission.receipt_item_id:
                raise _refuse(
                    _REASON_SHAPE,
                    "receipt-level feedback must not name an item; use item_specific to cite one",
                )
            return submission
        if submission.receipt_id is not None or submission.receipt_item_id:
            raise _refuse(
                _REASON_SHAPE,
                "a diagnostic observation cites neither a receipt nor an item",
            )
        if submission.learning_eligible:
            FEEDBACK_REFUSED_TOTAL.labels(reason=_REASON_DIAGNOSTIC_LEARNING).inc()
        return dataclasses.replace(submission, learning_eligible=False)

    async def _resolve_binding(
        self, session: AsyncSession, ctx: TenantContext, submission: FeedbackSubmissionV1
    ) -> None:
        """Prove the receipt is the caller's and the item is on it.

        Two reads, in this order, and the order is the security property: the
        receipt's tenant is established before the item is looked at, so an item
        id can never be probed against a receipt the caller may not see.
        """
        receipt = (
            await session.execute(
                select(ContextReceipt.receipt_id).where(
                    ContextReceipt.receipt_id == submission.receipt_id,
                    ContextReceipt.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if receipt is None:
            # Another tenant's receipt and a nonexistent one answer identically.
            # Distinguishing them turns a receipt id into a cross-tenant
            # existence oracle for anyone willing to enumerate.
            raise _refuse(
                _REASON_RECEIPT_NOT_FOUND,
                "no such receipt",
                error=NotFoundError,
            )

        if submission.receipt_item_id is None:
            return

        item = (
            await session.execute(
                select(ContextReceiptItem.receipt_item_id).where(
                    ContextReceiptItem.receipt_id == submission.receipt_id,
                    ContextReceiptItem.receipt_item_id == submission.receipt_item_id,
                )
            )
        ).scalar_one_or_none()
        if item is None:
            # Named separately from the receipt case: the caller has been
            # authorized for this receipt already, so telling them the item is
            # not on it leaks nothing and is the only message they can act on.
            raise _refuse(
                _REASON_ITEM_NOT_ON_RECEIPT,
                "that item is not on that receipt",
                error=NotFoundError,
            )

    async def _store(
        self, session: AsyncSession, ctx: TenantContext, submission: FeedbackSubmissionV1
    ) -> RecordedFeedback:
        """Insert, or recognise the submission already stored under this key.

        A replay is read *before* the insert is attempted rather than after it
        fails, so the ordinary retry costs one select instead of a constraint
        violation and a rolled-back transaction. The insert still races: two
        concurrent identical submissions can both miss the read, and the unique
        index is what settles it -- the loser re-reads and answers as a replay
        rather than surfacing a database error to a caller who did nothing wrong.
        """
        digest = content_digest_for(submission)

        existing = await self._find_replay(session, ctx, submission, digest)
        if existing is not None:
            FEEDBACK_REPLAYED_TOTAL.labels(kind=submission.kind).inc()
            return existing

        feedback_id = uuid.uuid4()
        created_at = self._clock.now()
        try:
            await session.execute(
                text(
                    "INSERT INTO context_feedback (feedback_id, tenant_id, kind, receipt_id, receipt_item_id,"
                    " rating, learning_eligible, note, reporter_id, reporter_type, idempotency_key,"
                    " content_digest, created_at)"
                    " VALUES (:fid, :tid, :kind, :rid, :iid, :rating, :elig, :note, :rep, :rtype, :idk,"
                    " :dig, :created)"
                ),
                {
                    "fid": feedback_id,
                    "tid": ctx.tenant_id,
                    "kind": submission.kind,
                    "rid": submission.receipt_id,
                    "iid": submission.receipt_item_id,
                    "rating": submission.rating,
                    "elig": submission.learning_eligible,
                    "note": submission.note,
                    "rep": submission.reporter_id,
                    "rtype": submission.reporter_type,
                    "idk": submission.idempotency_key,
                    "dig": digest,
                    "created": created_at,
                },
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raced = await self._find_replay(session, ctx, submission, digest)
            if raced is not None:
                FEEDBACK_REPLAYED_TOTAL.labels(kind=submission.kind).inc()
                return raced
            raise _refuse(
                _REASON_IDEMPOTENCY_CONFLICT,
                "that submission key was used for different feedback",
                error=ConflictError,
            ) from None

        FEEDBACK_ACCEPTED_TOTAL.labels(
            kind=submission.kind, learning_eligible=str(submission.learning_eligible).lower()
        ).inc()
        return RecordedFeedback(
            feedback_id=feedback_id,
            kind=submission.kind,
            rating=submission.rating,
            learning_eligible=submission.learning_eligible,
            receipt_id=submission.receipt_id,
            receipt_item_id=submission.receipt_item_id,
            content_digest=digest,
            created_at=created_at,
            replayed=False,
        )

    async def _find_replay(
        self,
        session: AsyncSession,
        ctx: TenantContext,
        submission: FeedbackSubmissionV1,
        digest: str,
    ) -> RecordedFeedback | None:
        """Return the stored row when this is an exact replay; refuse when it is not.

        Same key and same digest is the same report arriving twice. Same key and a
        *different* digest is a caller reusing a key for something else, and both
        cannot be true -- storing a second row would leave two contradictory
        reports under one identity, and overwriting would silently discard the
        first.
        """
        row = (
            await session.execute(
                text(
                    "SELECT feedback_id, kind, rating, learning_eligible, receipt_id, receipt_item_id,"
                    " content_digest, created_at FROM context_feedback"
                    " WHERE tenant_id = :tid AND reporter_id = :rep AND idempotency_key = :idk"
                ),
                {"tid": ctx.tenant_id, "rep": submission.reporter_id, "idk": submission.idempotency_key},
            )
        ).one_or_none()
        if row is None:
            return None
        if row.content_digest != digest:
            raise _refuse(
                _REASON_IDEMPOTENCY_CONFLICT,
                "that submission key was used for different feedback",
                error=ConflictError,
            )
        return RecordedFeedback(
            feedback_id=row.feedback_id,
            kind=row.kind,
            rating=row.rating,
            learning_eligible=row.learning_eligible,
            receipt_id=row.receipt_id,
            receipt_item_id=row.receipt_item_id,
            content_digest=row.content_digest,
            created_at=row.created_at,
            replayed=True,
        )


def feedback_json(recorded: RecordedFeedback) -> dict[str, Any]:
    """The wire shape both transports return, so neither can drift from the other."""
    return {
        "feedback_id": str(recorded.feedback_id),
        "kind": recorded.kind,
        "rating": recorded.rating,
        "learning_eligible": recorded.learning_eligible,
        "receipt_id": str(recorded.receipt_id) if recorded.receipt_id else None,
        "receipt_item_id": recorded.receipt_item_id,
        "content_digest": recorded.content_digest,
        "created_at": recorded.created_at.isoformat(),
        "replayed": recorded.replayed,
    }


__all__ = [
    "FEEDBACK_ACCEPTED_TOTAL",
    "FEEDBACK_KINDS",
    "FEEDBACK_REFUSED_TOTAL",
    "FEEDBACK_REPLAYED_TOTAL",
    "KIND_DIAGNOSTIC",
    "KIND_ITEM_SPECIFIC",
    "KIND_RECEIPT_LEVEL",
    "RATINGS",
    "REPORTER_TYPES",
    "FeedbackService",
    "FeedbackSubmissionV1",
    "RecordedFeedback",
    "content_digest_for",
    "feedback_json",
]
