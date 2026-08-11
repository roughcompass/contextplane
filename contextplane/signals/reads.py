"""Feedback reads: aggregate quality and bounded reconnect annotations.

These answer the questions an owner has about the system — is context going stale,
is it being reused, do handoffs succeed — and they answer them over people's
reports, which is what makes the floors load-bearing rather than defensive. The
floors, the suppression rule and the partial-total rule are imported from the
learning-read module rather than restated here: two definitions that agree today is
not the same as one rule enforced uniformly, and this is exactly the pair that would
drift.

**Diagnostic observations are excluded structurally, not filtered late.** A
diagnostic cites neither a receipt nor an item and is never learning-eligible: it is
a report *about the system's plumbing*, not a verdict on served context. Counting it
as a quality signal would let a burst of plumbing reports read as a collapse in
context quality. Every aggregate excludes the kind in its own WHERE clause. The
resume read is instead joined to one receipt; the diagnostic discriminant forces
``receipt_id`` NULL, so a diagnostic cannot enter that set in the first place.

**No cell is per-actor and no cohort is finer than the tenant.** The reporter id is
read by aggregate statements only to count *distinct* reporters, which is what the
actor floor is tested against. The resume projection does not select reporter id,
reporter type, or note at all. None reaches a value, a label, or a response.

**Outcomes that bound but never joined are surfaced, because nothing else would.**
An outcome reaches the receipt it belongs to only through a shared external-reference
row: both sides bind the same reference, and that is the entire mediation. An outcome
citing a correctly spelled kind with the wrong external id therefore binds cleanly to
a legitimate reference row of its own, joins nothing, and is indistinguishable
downstream from an outcome that never arrived. Submission cannot catch it -- the id
belongs to the other system and this one has no way to know which of its values was
meant. The read below is the compensating control: it names outcomes that have sat
bound and unjoined for longer than an operator-supplied age, which is the only signal
this side of the boundary that separates a typo from a receipt that has not been
written yet.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.memory.learning_reads import (
    Breakdown,
    Cell,
    Floors,
    build_breakdown,
)
from contextplane.signals.feedback import KIND_DIAGNOSTIC
from contextplane.types import TenantContext

METRIC_CONTEXT_QUALITY = "context_quality"
METRIC_REUSE = "reuse"
METRIC_HANDOFF_SUCCESS = "handoff_success"
METRIC_ADEQUACY = "adequacy"

#: Every metric this module serves, closed so the router cannot advertise one
#: nothing computes and the conformance gate can assert the whole set.
FEEDBACK_METRICS: tuple[str, ...] = (
    METRIC_CONTEXT_QUALITY,
    METRIC_REUSE,
    METRIC_HANDOFF_SUCCESS,
    METRIC_ADEQUACY,
)

#: Which ratings each metric is computed over. Explicit per metric rather than
#: "everything else": a rating added to the vocabulary must be assigned to a metric
#: deliberately, and one assigned to none is visibly unreported rather than silently
#: folded into whichever aggregate happened to use a negation.
_RATINGS_FOR: dict[str, tuple[str, ...]] = {
    METRIC_CONTEXT_QUALITY: ("missing", "stale", "incorrect", "contradicted", "unsafe"),
    METRIC_REUSE: ("selected", "ignored"),
    METRIC_HANDOFF_SUCCESS: ("succeeded", "failed", "rolled_back"),
    METRIC_ADEQUACY: ("relevant", "irrelevant", "needs_human_review"),
}

# One statement for all four metrics: they differ only in which ratings they group
# over. `reporter_id` appears once, inside count(DISTINCT ...), which is the only
# use of it this module has.
_RATING_BREAKDOWN_SQL = """
SELECT
    rating AS label,
    count(*) AS event_count,
    count(DISTINCT reporter_id) AS actor_count
FROM context_feedback
WHERE tenant_id = :tenant
  AND kind <> :diagnostic_kind
  AND rating = ANY(:ratings)
  AND created_at >= :window_start
  AND created_at < :window_end
GROUP BY rating
"""


@dataclasses.dataclass(frozen=True)
class ResumeFeedback:
    """One minimized feedback annotation about the last resolution.

    Resume needs the verdict and evidence state, not who supplied it or the
    reporter's free text. ``note``, ``reporter_id`` and ``reporter_type`` are
    therefore absent from both this value and the SELECT below: reconnecting to
    work is not a reason to redistribute personal or attribution data.

    ``consumed`` never hides the row. It records that one or more same-tenant
    derivations produced a claim from the exact receipt/item locus, allowing a
    caller to distinguish unresolved feedback without erasing history.
    """

    feedback_id: uuid.UUID
    kind: str
    receipt_id: uuid.UUID
    receipt_item_id: str | None
    rating: str
    learning_eligible: bool
    created_at: datetime.datetime
    consumed: bool


@dataclasses.dataclass(frozen=True)
class ResumeFeedbackPage:
    """Bounded feedback for one receipt, unresolved rows first."""

    items: tuple[ResumeFeedback, ...]
    truncated: bool


class FeedbackReadService:
    """Aggregate feedback reads for one tenant, floored at construction.

    Constructed per request from the session factory and the floors, like the
    learning-read service it shares its rules with. No method returns an unfloored
    figure, so there is no ordering of calls that produces one.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        floors: Floors | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._floors = floors or Floors()

    @property
    def floors(self) -> Floors:
        """The floors in force, so a caller can serve them beside the figures."""
        return self._floors

    async def breakdown(
        self,
        ctx: TenantContext,
        metric: str,
        *,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Breakdown:
        """One metric's cells, already suppressed, combined and partial-totalled."""
        ratings = _RATINGS_FOR[metric]
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(_RATING_BREAKDOWN_SQL),
                    {
                        "tenant": ctx.tenant_id,
                        "diagnostic_kind": KIND_DIAGNOSTIC,
                        "ratings": list(ratings),
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).all()

        cells = [
            Cell.measured(
                str(row.label),
                actor_count=int(row.actor_count),
                event_count=int(row.event_count),
                value=int(row.event_count),
                floors=self._floors,
            )
            for row in rows
        ]
        return build_breakdown(
            metric,
            window_start=window_start,
            window_end=window_end,
            cells=cells,
            floors=self._floors,
        )

    async def resume_page(
        self,
        ctx: TenantContext,
        *,
        receipt_id: uuid.UUID,
        bound: int,
    ) -> ResumeFeedbackPage:
        """Feedback on one receipt with exact evidence consumption annotated.

        A row is consumed only when a derivation from this tenant cites the same
        ``(receipt_id, receipt_item_id)`` locus and has produced a claim. Receipt-
        level feedback therefore matches only receipt-level evidence (both item
        ids NULL), never an arbitrary item from the receipt. Evidence links do
        not carry a tenant id, so the tenant boundary is deliberately supplied
        by ``claim_derivations`` inside the join rather than checked afterwards.

        Consumed rows remain in the result. Unconsumed rows sort first, followed
        by newest creation time and id for deterministic reconnects.
        """
        if bound < 1:
            raise ValueError(f"bound must be at least 1, got {bound}")

        stmt = text(
            """
            WITH consumption AS (
                SELECT feedback.feedback_id
                FROM context_feedback AS feedback
                JOIN derivation_evidence_links AS evidence
                  ON evidence.receipt_id = feedback.receipt_id
                 AND (
                    (feedback.kind = 'item_specific'
                     AND evidence.evidence_kind = 'receipt_item'
                     AND evidence.receipt_item_id = feedback.receipt_item_id)
                     OR (feedback.kind = 'receipt_level'
                     AND evidence.evidence_kind = 'receipt'
                     AND evidence.receipt_item_id IS NULL)
                 )
                JOIN claim_derivations AS derivation
                  ON derivation.derivation_id = evidence.derivation_id
                 AND derivation.tenant_id = feedback.tenant_id
                 AND derivation.created_claim_id IS NOT NULL
                WHERE feedback.tenant_id = :tenant_id
                  AND feedback.receipt_id = :receipt_id
                GROUP BY feedback.feedback_id
            ), resume_feedback AS (
                SELECT
                    feedback.feedback_id,
                    feedback.kind,
                    feedback.receipt_id,
                    feedback.receipt_item_id,
                    feedback.rating,
                    feedback.learning_eligible,
                    feedback.created_at,
                    consumption.feedback_id IS NOT NULL AS consumed
                FROM context_feedback AS feedback
                JOIN context_receipts AS receipt
                  ON receipt.receipt_id = feedback.receipt_id
                 AND receipt.tenant_id = feedback.tenant_id
                LEFT JOIN consumption ON consumption.feedback_id = feedback.feedback_id
                WHERE feedback.tenant_id = :tenant_id
                  AND feedback.receipt_id = :receipt_id
            )
            SELECT *
            FROM resume_feedback
            ORDER BY consumed ASC, created_at DESC, feedback_id
            LIMIT :limit
            """
        )
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        stmt,
                        {"tenant_id": ctx.tenant_id, "receipt_id": receipt_id, "limit": bound + 1},
                    )
                ).all()
            )

        truncated = len(rows) > bound
        rows = rows[:bound]
        return ResumeFeedbackPage(
            items=tuple(
                ResumeFeedback(
                    feedback_id=row.feedback_id,
                    kind=str(row.kind),
                    receipt_id=row.receipt_id,
                    receipt_item_id=row.receipt_item_id,
                    rating=str(row.rating),
                    learning_eligible=bool(row.learning_eligible),
                    created_at=row.created_at,
                    consumed=bool(row.consumed),
                )
                for row in rows
            ),
            truncated=truncated,
        )


#: What an outcome binds under, and what a receipt binds under. The join that
#: matters is between these two subject types over one reference row, so both
#: spellings live here rather than inline in the statement: a subject type
#: renamed in one half of the pair and not the other would silently report every
#: outcome as unjoined, which reads as a catastrophe rather than as a bug.
SUBJECT_OUTCOME = "external_signal"
SUBJECT_RECEIPT = "context_item"


@dataclasses.dataclass(frozen=True)
class UnjoinedOutcome:
    """One outcome that bound to a reference nothing else cites.

    The external id and kind are carried because they are what an operator acts
    on: the repair is to compare them against the work the receipt named, and a
    signal id alone would send them back to the database to find out what the
    row was even about. No payload, producer, or authority is projected -- this
    is a reconciliation aid, not a second read path onto signal content.
    """

    signal_id: uuid.UUID
    reference_id: uuid.UUID
    kind: str
    external_id: str
    bound_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class UnjoinedOutcomePage:
    """Bounded unjoined outcomes, oldest binding first.

    ``truncated`` is what makes an empty page unambiguous. A page with no items
    and ``truncated`` false is the query having run and found nothing; a failing
    query raises instead of arriving here, so "nothing is wrong" and "nothing
    could be read" are never the same value.
    """

    items: tuple[UnjoinedOutcome, ...]
    truncated: bool


class UnjoinedOutcomeReadService:
    """Outcomes bound for longer than an age with no receipt on the other side.

    Separate from the feedback service above because it answers an operator's
    question about plumbing rather than an owner's question about quality, and
    because its floors do not apply: suppressing a small count here would hide
    exactly the single stuck outcome the read exists to find.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def unjoined(
        self,
        ctx: TenantContext,
        *,
        older_than: datetime.timedelta,
        now: datetime.datetime,
        bound: int,
    ) -> UnjoinedOutcomePage:
        """Outcomes bound before ``now - older_than`` that no receipt cites.

        ``older_than`` is required rather than defaulted. The right age is a
        property of the submitting orchestrator's latency, not of this service,
        and a default here would be read as a recommendation nothing measured.

        A joined outcome is excluded however old it is: age is a filter applied
        to the unjoined set, never a reason to report something that met its
        receipt. The exclusion is ``NOT EXISTS`` over the same reference rather
        than a ``LEFT JOIN ... IS NULL`` so that a reference cited by several
        receipts cannot multiply the row and then survive the null test.
        """
        if bound < 1:
            raise ValueError(f"bound must be at least 1, got {bound}")
        if older_than < datetime.timedelta(0):
            raise ValueError(f"older_than must not be negative, got {older_than!r}")

        # Both subject types are bound rather than interpolated. They are module
        # constants, not caller input, so this is not an injection fix -- it keeps
        # the statement a constant string, which is what lets the SQL gate read it
        # without an exemption.
        stmt = text(
            """
            SELECT
                binding.subject_id AS signal_id,
                reference.reference_id,
                reference.kind,
                reference.external_id,
                binding.bound_at
            FROM context_reference_bindings AS binding
            JOIN context_external_references AS reference
              ON reference.reference_id = binding.reference_id
             AND reference.tenant_id = binding.tenant_id
            WHERE binding.tenant_id = :tenant
              AND binding.subject_type = :outcome_subject
              AND binding.bound_at < :bound_before
              AND NOT EXISTS (
                  SELECT 1
                  FROM context_reference_bindings AS receipt_binding
                  WHERE receipt_binding.tenant_id = binding.tenant_id
                    AND receipt_binding.reference_id = binding.reference_id
                    AND receipt_binding.subject_type = :receipt_subject
              )
            ORDER BY binding.bound_at, binding.subject_id
            LIMIT :limit
            """
        )
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        stmt,
                        {
                            "tenant": ctx.tenant_id,
                            "outcome_subject": SUBJECT_OUTCOME,
                            "receipt_subject": SUBJECT_RECEIPT,
                            "bound_before": now - older_than,
                            "limit": bound + 1,
                        },
                    )
                ).all()
            )

        truncated = len(rows) > bound
        return UnjoinedOutcomePage(
            items=tuple(
                UnjoinedOutcome(
                    signal_id=row.signal_id,
                    reference_id=row.reference_id,
                    kind=str(row.kind),
                    external_id=str(row.external_id),
                    bound_at=row.bound_at,
                )
                for row in rows[:bound]
            ),
            truncated=truncated,
        )


__all__ = [
    "FEEDBACK_METRICS",
    "METRIC_ADEQUACY",
    "METRIC_CONTEXT_QUALITY",
    "METRIC_HANDOFF_SUCCESS",
    "METRIC_REUSE",
    "SUBJECT_OUTCOME",
    "SUBJECT_RECEIPT",
    "FeedbackReadService",
    "ResumeFeedback",
    "ResumeFeedbackPage",
    "UnjoinedOutcome",
    "UnjoinedOutcomePage",
    "UnjoinedOutcomeReadService",
]
