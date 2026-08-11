"""Conditions an operator should meet rather than go looking for.

This is the console's source of operational truth. It exists because the two
alternatives are both wrong:

* **Reading the Prometheus exposition from a browser.** Per-replica, cumulative
  since whichever process answered, and the endpoint is credentialed. A reader
  cannot tell a total from a sample of one, and nothing on screen could tell
  them.
* **Sending the operator to a dashboard tool.** That tool is deployment
  infrastructure. It is optional and frequently absent, so a console that
  depends on it is a blank page wherever it was not installed.

Every value here therefore travels with its own provenance, because the two
kinds of number in this module are trustworthy to different degrees and look
identical once rendered:

``cluster``
    Counted straight from the table, at read time. Correct no matter how many
    replicas are running, and current rather than cumulative. A depth is the
    honest question about a queue — a throughput counter looks the same whether
    the backlog is empty or growing without bound.

``process``
    Read from this replica's in-process counters. Cumulative since it started,
    and a load-balanced read lands on an arbitrary replica — so a zero here does
    **not** prove zero everywhere. These are published anyway because each is
    actionable on any non-zero value, and losing them entirely would be worse
    than showing them with their caveat attached.

The caveat ships in the payload rather than in this docstring. A consumer that
renders a bare number has to work to strip the provenance off first.
"""

from __future__ import annotations

import datetime
import logging
import socket
from dataclasses import dataclass
from typing import Literal

from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.service.memory import curation_queue, promotion

_log = logging.getLogger(__name__)

__all__ = [
    "OperationalHealth",
    "Reading",
    "collect_operational_health",
]

Scope = Literal["cluster", "process"]
Kind = Literal["gauge", "counter"]


@dataclass(frozen=True)
class Reading:
    """One number, and everything needed to read it correctly.

    `scope` and `kind` are not optional and have no defaults. A reading that
    could be constructed without them would eventually be, and the result is a
    figure on a screen that looks like a service-wide total and is one replica's
    count since breakfast.
    """

    key: str
    label: str
    value: float | None
    scope: Scope
    kind: Kind
    #: Which replica answered. Meaningful only for `process` scope; `None`
    #: otherwise, because a cluster count belongs to no particular instance.
    instance: str | None
    #: Why a non-zero value matters, in the operator's terms. Present when the
    #: number is actionable on sight rather than merely informational.
    actionable: str | None = None


@dataclass(frozen=True)
class OperationalHealth:
    """Snapshot of operator-facing metrics from queues and data-quality checks."""

    observed_at: datetime.datetime
    queues: tuple[Reading, ...]
    data_quality: tuple[Reading, ...]


# ---------------------------------------------------------------------------
# Cluster scope — counted from the tables
# ---------------------------------------------------------------------------
#
# Counted here rather than read from `contextplane_worker_queue_depth`. That gauge
# holds whatever the last drain pass on this replica happened to write, so it is
# both stale and process-local — which is the failure this module exists to
# avoid, and it would arrive wearing a `cluster` label.

_QUEUE_COUNTS: tuple[tuple[str, str, str], ...] = (
    (
        "embedding_outbox",
        "Embedding outbox",
        "SELECT COUNT(*) FROM embedding_outbox",
    ),
    (
        "closure_outbox",
        "Closure refresh backlog",
        "SELECT COUNT(*) FROM closure_outbox",
    ),
    (
        "webhook_pending",
        "Webhook deliveries pending",
        "SELECT COUNT(*) FROM notification_deliveries WHERE status = 'pending'",
    ),
    (
        "webhook_failed",
        "Webhook deliveries abandoned",
        "SELECT COUNT(*) FROM notification_deliveries WHERE status = 'failed'",
    ),
    (
        "records_under_legal_hold",
        "Records held past deletion by a legal hold",
        # A count, deliberately, and not the held records themselves. This response
        # is service-global and reachable by a tenant administrator, so it names no
        # tenant, actor or row -- and *whose* records are under legal hold is the
        # more sensitive fact here, not the less. The count answers the operational
        # question ("is anything being kept that would otherwise have gone?")
        # without disclosing that.
        #
        # Counted against the database's own clock rather than the caller's `now`:
        # whether a hold is live is a property of the row, and a reader passing a
        # different instant would get an answer no sweep would act on.
        "SELECT COUNT(*) FROM legal_holds WHERE review_date > now()",
    ),
    (
        "legal_holds_awaiting_review",
        "Legal holds past their review date",
        # The other side, and the one that fails open. A hold stops suspending
        # deletion the moment its review date passes, so each of these is a record
        # the next sweep may delete while the matter that justified holding it is
        # possibly still live. Renewing needs recorded re-justification, which is
        # exactly why a lapse must not be quiet.
        "SELECT COUNT(*) FROM legal_holds WHERE review_date <= now()",
    ),
    (
        "derivative_outbox_pending",
        "Derivative propagation pending",
        # The queue an erasure writes into. Reported as a plain depth: a backlog
        # here is normal between the request and the drain, and only becomes an
        # incident when it stops moving -- which the failed count below is what
        # says.
        "SELECT COUNT(*) FROM derivative_work_outbox WHERE state = 'pending'",
    ),
    (
        "derivative_outbox_failed",
        "Derivative propagation failed",
        "SELECT COUNT(*) FROM derivative_work_outbox WHERE state = 'failed'",
    ),
    (
        "curation_queue_backlog",
        "Curation queue backlog",
        # Calls `curation_queue.py`'s own backlog predicate (unlinked,
        # contested, awaiting a high-impact owner review, or below the
        # tenant's confidence floor) at cluster scope rather than one
        # tenant's: this reading answers "is the loop's curation step
        # backing up anywhere", not "what does one tenant's queue hold".
        # Calling the shared function rather than keeping a second copy of
        # the CASE/JOIN logic is what keeps the two from quietly disagreeing
        # if that module's definition of the backlog ever changes.
        "SELECT COUNT(*)" + curation_queue.backlog_predicate(tenant_filter=False),
    ),
    # `curation_queue_backlog` stays the last entry: its own suite addresses it
    # positionally (`counts[-1]`, `fail_after=len(_QUEUE_COUNTS)`) to pin that an
    # unreadable query renders as `None` at exactly the reading it belongs to.
    # Append a new reading above it, not below.
)

_ACTIONABLE_QUEUES = {
    "derivative_outbox_failed": (
        "An erasure has not reached the artefacts built from the erased person's "
        "records, and nothing will retry it. Where the stalled item is a blocking "
        "one, workspace recall now refuses to serve rather than serve content the "
        "erasure has not caught up with -- so this number reaches a caller as "
        "reads failing, and it is the only place that says why. It does not clear "
        "on its own."
    ),
    "webhook_failed": (
        "These deliveries exhausted their retries and will never arrive. "
        "A subscriber is missing change notifications and has no way to know."
    ),
    "records_under_legal_hold": (
        "Each of these records is past its retention period and still stored, "
        "because a legal hold is suspending its deletion. That is the intended "
        "behaviour, and it is reported rather than silent: a paused clock defeats "
        "fail-closed overdue handling by design, so it must be visible."
    ),
    "legal_holds_awaiting_review": (
        "These holds have passed their review date and have stopped suspending "
        "deletion. The next sweep may delete records the hold was placed to keep. "
        "Renewing one requires recorded re-justification and an escalated approval."
    ),
}


async def _count(session: AsyncSession, sql: str) -> float | None:
    """A count, or `None` when the table could not be read.

    `None` is deliberately not zero. A table that is missing, locked, or
    unreachable is not an empty queue, and rendering it as one would report the
    healthiest possible state at exactly the moment something is wrong.
    """
    try:
        result = await session.execute(text(sql))
        return float(result.scalar_one())
    except Exception:  # noqa: BLE001 - an unreadable count must render as unknown, not zero; see docstring
        _log.warning("operational_health: count query failed sql=%r", sql, exc_info=True)
        return None


async def _oldest_open_proposal_age_seconds(session: AsyncSession, *, now: datetime.datetime) -> float | None:
    """How long the longest-waiting open promotion proposal has been waiting.

    Zero when no proposal is open: an empty review queue is not stale, and
    reporting it as unreadable would train an operator to distrust a number
    that is working exactly as intended. `None` is reserved for the query
    itself failing, matching `_count`'s own convention for the same reason.
    """
    try:
        oldest = await promotion.oldest_open_proposal_created_at(session)
    except Exception:  # noqa: BLE001 - an unreadable query must render as unknown, not zero; see docstring
        _log.warning("operational_health: oldest_open_proposal_created_at query failed", exc_info=True)
        return None
    if oldest is None:
        return 0.0
    return (now - oldest).total_seconds()


# ---------------------------------------------------------------------------
# Process scope — this replica's counters
# ---------------------------------------------------------------------------

_DATA_QUALITY: tuple[tuple[str, str, str, str], ...] = (
    (
        "entitlement_dropped_entries",
        "Dropped entitlement entries",
        "contextplane_entitlement_dropped_entries_total",
        "An entitlement arrived in a shape the parser rejected, so a principal "
        "silently resolved to fewer roles than it was granted.",
    ),
    (
        "entitlement_parse_ignored",
        "Entitlement entries ignored during parse",
        "contextplane_entitlement_parse_ignored_total",
        "Part of an entitlement string was unreadable and was skipped rather " "than failing the request.",
    ),
    (
        "audit_write_failures",
        "Audit write failures",
        "catalog_audit_write_failures_total",
        "An audit row was lost. The compliance record has a hole in it, and the "
        "request that caused it still succeeded.",
    ),
)


def _counter_total(family: str) -> float | None:
    """Sum every labelled series in a counter family, or `None` if absent.

    Summed across labels because the operator's question is "is this happening
    at all", and the per-reason breakdown is a level of detail that belongs in a
    time series rather than in a health summary.

    **A declared family with no samples is zero, not unknown**, and the
    distinction is the whole point. `prometheus_client` emits no series for a
    labelled counter until some label combination is first used, so three of the
    four counters here publish nothing at all on a healthy process — nothing has
    been dropped, so no `reason` label has ever been touched. Reporting that as
    `None` renders "unavailable" for a metric that is working perfectly, which
    trains an operator to ignore the one column that tells them a principal
    silently lost a role.

    `None` is reserved for a family this build does not define at all.
    """
    found = False
    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != family and metric.name != family.removesuffix("_total"):
            continue
        found = True
        for sample in metric.samples:
            if sample.name.endswith("_created"):
                continue
            total += sample.value
    return total if found else None


def _instance() -> str:
    # Which replica answered. Without it, two reads that disagree look like a
    # counter that went backwards rather than two different processes.
    return socket.gethostname()


async def collect_operational_health(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime.datetime,
) -> OperationalHealth:
    """Gather queue depths and data-quality metrics from the database at this instant."""
    instance = _instance()

    collected: list[Reading] = []
    # One session for every count, not one each: this runs on an operator's page
    # load, and opening four connections to answer one screen is a cost the
    # pool notices under a console left open on a wall display.
    async with session_factory() as session:
        for key, label, sql in _QUEUE_COUNTS:
            collected.append(
                Reading(
                    key=key,
                    label=label,
                    value=await _count(session, sql),
                    scope="cluster",
                    kind="gauge",
                    instance=None,
                    actionable=_ACTIONABLE_QUEUES.get(key),
                )
            )
        collected.append(
            Reading(
                key="oldest_open_proposal_age_seconds",
                label="Oldest open promotion proposal, age",
                value=await _oldest_open_proposal_age_seconds(session, now=now),
                scope="cluster",
                kind="gauge",
                instance=None,
            )
        )
    queues = tuple(collected)

    data_quality = tuple(
        Reading(
            key=key,
            label=label,
            value=_counter_total(family),
            scope="process",
            kind="counter",
            instance=instance,
            actionable=why,
        )
        for key, label, family, why in _DATA_QUALITY
    )

    return OperationalHealth(observed_at=now, queues=queues, data_quality=data_quality)
