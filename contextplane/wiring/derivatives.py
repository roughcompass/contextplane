"""Which handler owns which derivative kind, wired into a drain that runs.

Six families build derivatives and six families know how to remove their own.
None of them registers itself: a family that registered from its own module would
decide coverage at import time, and which kinds a deployment actually covers would
then depend on which modules something happened to import. So registration
happens here, once, where the composition root can be read top to bottom and the
coverage list is a list.

**This module is the coverage list, and a gate reads it.** `build_handler_registry`
is what the scheduler constructs and what the conformance pin builds — the same
function, not a second copy assembled to look like it. A pin that rebuilt the
registry from its own idea of the families would pass while the deployment shipped
a kind nothing handles, which is the one failure the pin exists to catch.

**Its own module rather than a section of `jobs.py`.** The scheduler wiring is
already close to this package's file-size ceiling, and it has been split for that
reason twice. Registration also changes for a different cause than job scheduling
does — a new derivative kind versus a new interval — so the two do not belong in
one file merely because the drain is scheduled like every other worker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from contextplane.context.derivative_handlers import ReceiptLinkHandler
from contextplane.retention import derivatives, holds, policies
from contextplane.service.memory.derivative_handlers import ClaimDerivativeHandler
from contextplane.service.operations.derivative_handlers import CARRIER_HANDLERS
from contextplane.service.retrieval.derivative_handlers import retrieval_derivative_handlers
from contextplane.signals.erasure import SignalExpiry
from contextplane.workers.derivative_propagation import DerivativePropagationWorker
from contextplane.workers.retention_expiry import ExpiryMinimizer, RetentionExpiryWorker
from contextplane.workspaces.derivative_handlers import SummaryDerivativeHandler

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from contextplane.retention import tombstones
    from contextplane.types import Clock


def build_handler_registry(salts: tombstones.TenantSaltResolver) -> derivatives.HandlerRegistry:
    """Register every family's handlers, and return the registry the drain reads.

    `salts` is threaded in rather than constructed here because the receipt family
    minimizes item keys into tenant-keyed markers, and the key material is a
    deployment's, not this module's. Every other handler is stateless: what they
    remove is addressed entirely by the locator on the registration.

    The registry refuses a second handler for one kind, so this function is also
    where a duplicate registration fails — loudly, at construction, rather than by
    whichever import order happened to win.
    """
    registry = derivatives.HandlerRegistry()

    # Receipts: item keys are minimized rather than deleted, because the rows are
    # evidence other rows point at.
    registry.register(ReceiptLinkHandler(salts))
    # Task head summaries: prose an agent wrote about the person.
    registry.register(SummaryDerivativeHandler())
    # The retrieval side — vector, chunk, full-text document, closure cache. A
    # tuple from the family rather than four names here: which facets one
    # `embeddings` row presents is the retrieval package's fact, not wiring's.
    for handler in retrieval_derivative_handlers():
        registry.register(handler)
    # What an extractor concluded from the person's sessions, and the excerpts it
    # quoted to say so.
    registry.register(ClaimDerivativeHandler())
    # The carriers: queued payloads, log projections, exports. Two of the three
    # refuse rather than delete — see their own docstrings for why a refusal is the
    # truthful answer there and a zero would not be.
    for handler in CARRIER_HANDLERS:
        registry.register(handler)

    return registry


def _deadline_from(started_at: str, record_class: str) -> str:
    """The SQL for "this record's period ends", as start plus the approved duration.

    Reads the duration from the approved dispositions rather than restating it, so
    a policy change moves the report with it instead of leaving a second number
    here to disagree quietly.
    """
    days = policies.disposition(record_class).retention_days
    if days is None:  # pragma: no cover - only event-bounded classes, none of which are mapped here
        msg = f"{record_class} has no duration to compute a deadline from"
        raise policies.NoComputableExpiry(msg)
    return f"{started_at} + make_interval(days => {days})"


#: Where each held record class's own retention deadline is read from, for the
#: held-overdue report. Only the classes an expiry path actually consults the hold
#: seam for appear here; the two shapes differ because the underlying clocks do —
#: a derivative stores its deadline outright, while a signal's is its ingestion
#: time plus the policy's duration.
_HELD_RECORD_SOURCES: dict[str, holds.HeldRecordSource] = {
    policies.RECORD_DERIVATIVE: holds.HeldRecordSource(
        table="derivative_registrations",
        id_column="derivative_id",
        due_at_sql="t.expires_at",
    ),
    policies.RECORD_EXTERNAL_SIGNAL: holds.HeldRecordSource(
        table="external_signals",
        id_column="signal_id",
        due_at_sql=_deadline_from("t.ingested_at", policies.RECORD_EXTERNAL_SIGNAL),
    ),
    policies.RECORD_CONTEXT_FEEDBACK: holds.HeldRecordSource(
        table="context_feedback",
        id_column="feedback_id",
        due_at_sql=_deadline_from("t.created_at", policies.RECORD_CONTEXT_FEEDBACK),
    ),
}


def build_propagation_worker(
    session_factory: async_sessionmaker[AsyncSession],
    salts: tombstones.TenantSaltResolver,
) -> DerivativePropagationWorker:
    """The drain, over the full registry. One call, so nothing constructs half of it.

    Building the worker and building the registry are one step on purpose: a
    caller that could construct the drain with a partial registry would get a
    queue that looks served while items for the missing kinds fail one by one.
    """
    return DerivativePropagationWorker(session_factory, build_handler_registry(salts))


def build_retention_expiry_worker(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> RetentionExpiryWorker:
    """The retention sweep, with the family reductions it cannot import itself.

    `signals` sits above `workers` in the import contract, so the sweep takes its
    per-class reductions as callables and this is the only place allowed to name
    them. Assembled here rather than in the scheduler wiring for the reason that
    module is already split: what gets registered changes when a family gains a
    clock, which is a different cause from an interval changing.

    The hold store is the real one, reading the `legal_holds` table. One instance
    is shared between the signal batches and the sweep deliberately: two stores
    would be two different answers to "is this record held?".

    Its deadline sources are supplied here for the same reason the minimizers are.
    `retention` sits below the families whose records it holds, so it cannot name
    their tables itself; the report needs to know when a held record was due, and
    this is the one place allowed to say where that is read from. A class absent
    from this map is reported as overdue rather than skipped — a hold nothing can
    date is the one an operator most needs to see.
    """
    hold_store = holds.PostgresHoldStore(session_factory, _HELD_RECORD_SOURCES)
    signal_expiry = SignalExpiry(session_factory, hold_store)
    return RetentionExpiryWorker(
        session_factory,
        hold_store,
        minimizers=(
            # The payload clock: a signal's content goes long before its envelope.
            ExpiryMinimizer(
                record_class=policies.RECORD_EXTERNAL_SIGNAL,
                reduce=lambda ctx, now: signal_expiry.minimize_signal_payloads(ctx, now=now),
            ),
            # The record clock: the envelope itself, with its bindings.
            ExpiryMinimizer(
                record_class=policies.RECORD_EXTERNAL_SIGNAL,
                reduce=lambda ctx, now: signal_expiry.delete_expired_signals(ctx, now=now),
            ),
            # Feedback free text. The row survives: every aggregate over this table
            # counts the discriminant and rating, which are not the personal part.
            ExpiryMinimizer(
                record_class=policies.RECORD_CONTEXT_FEEDBACK,
                reduce=lambda ctx, now: signal_expiry.minimize_feedback_notes(ctx, now=now),
            ),
        ),
        clock=clock,
    )


__all__ = [
    "build_handler_registry",
    "build_propagation_worker",
    "build_retention_expiry_worker",
]
