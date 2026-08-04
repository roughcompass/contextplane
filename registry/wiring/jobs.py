"""The background scheduler and every job registered on it.

`build_scheduler` constructs the `AsyncIOScheduler` (durable job store,
falling back to an in-memory one for tests) and every worker + `add_job`
call the app needs; `start` begins the scheduler and kicks off the jobs
that need to run once immediately (the audit-partition check) or need the
scheduler already running (per-source sync cron jobs).

This is grouped as one module, rather than split alongside the services
each job drains, because the jobs share one scheduler and one set of
`max_instances=1` / `coalesce=True` conventions — reading them together is
how an operator answers "what runs on an interval in this process, and how
often." Every job here that is just "call this worker's `run_once` on an
interval and log the outcome" is registered through
`registry.workers.base.register_periodic`, which is where that shared shape
now lives instead of in a hand-written closure per job. A job that needs
keyword arguments passed to its target, or a trigger other than a plain
interval, calls `scheduler.add_job()` directly instead — each such case has
a comment explaining why it does not fit the helper.
"""

from __future__ import annotations

import datetime
import functools
import logging
import re

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from prometheus_client import Gauge
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.workers.audit_drain import AuditDrainWorker, DrainResult
from registry.arc.workers.challenge_cleanup import ChallengeCleanupWorker, CleanupResult
from registry.arc.workers.review_expiry import ReviewExpiryResult, ReviewExpiryWorker
from registry.config import Settings
from registry.extraction.factory import build_provider as build_extraction_provider
from registry.extraction.service import ExtractionService
from registry.service.catalog import CatalogService
from registry.service.claims import ClaimService
from registry.service.consolidation import ConsolidationService
from registry.service.embedding_drain import drain_outbox
from registry.types import Clock, Embedder
from registry.workers.base import register_periodic
from registry.workers.closure_refresh import ClosureRefreshWorker
from registry.workers.consolidation_sweep import ConsolidationSweepWorker, SweepReport
from registry.workers.extraction_drain import DrainReport, ExtractionDrainWorker
from registry.workers.memory_expiry import MemoryExpiryResult, MemoryExpiryWorker
from registry.workers.usage_expiry import UsageExpiryResult, UsageExpiryWorker
from registry.workers.usage_rollup import UsageRollupWorker
from registry.workers.webhook_delivery import WebhookDeliveryWorker
from registry.workers.workspace_expiry import ExpiryResult, WorkspaceExpiryWorker
from sync.runner import create_scheduler, register_sync_jobs

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit-partition-archival job: gauge, predicate, and the job body itself.
# Runs hourly and once at startup; see docs/runbook-ops.md for the operator
# archival procedure this gauge exists to trigger.
# ---------------------------------------------------------------------------

# Prometheus gauge: number of audit_log child partitions eligible for archival
# (lower range bound older than 24 months).  Operator should run the detach
# procedure in docs/runbook-ops.md when this is > 0.
_AUDIT_ARCHIVAL_GAUGE: Gauge = Gauge(
    "catalog_audit_partitions_eligible_for_archival",
    "Number of audit_log monthly partitions whose lower bound is older than 24 months",
)

# Regex to extract YYYY_MM from partition names like audit_log_2024_03
_PARTITION_NAME_RE = re.compile(r"audit_log_(\d{4})_(\d{2})$")


def audit_partitions_eligible_for_archival(
    partition_names: list[str],
    reference_date: datetime.date | None = None,
    retention_months: int = 24,
) -> list[str]:
    """Return partition names whose lower bound is older than *retention_months*.

    Accepts bare partition names like ``audit_log_2024_03`` and extracts the
    year/month from the suffix.  Partitions that don't match the expected
    pattern are silently skipped (forward partitions created by the migration
    follow the same naming scheme but will not be older than 24 months).

    Args:
        partition_names: List of pg_class relnames for audit_log children.
        reference_date: Date to compute age from; defaults to today (UTC).
        retention_months: Window in months; partitions older than this are eligible.

    Returns:
        Sorted list of eligible partition names.
    """
    ref = reference_date or datetime.date.today()
    cutoff_year = ref.year
    cutoff_month = ref.month - retention_months
    # Normalise month into valid (year, month) pair
    while cutoff_month <= 0:
        cutoff_month += 12
        cutoff_year -= 1
    cutoff = datetime.date(cutoff_year, cutoff_month, 1)

    eligible: list[str] = []
    for name in partition_names:
        m = _PARTITION_NAME_RE.match(name)
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        try:
            partition_start = datetime.date(year, month, 1)
        except ValueError:
            continue
        if partition_start < cutoff:
            eligible.append(name)
    return sorted(eligible)


async def check_audit_partition_ages(session_factory: object) -> None:
    """Async job: query pg_inherits, update gauge, emit WARNING if needed.

    Runs under ``AsyncIOScheduler`` (which awaits coroutine jobs) using the
    project's async session factory.  When the factory has no bind (unit-test
    mocks), the function sets the gauge to 0 and returns.
    """
    try:
        bind = getattr(session_factory, "kw", {}).get("bind") or getattr(session_factory, "kw_args", {}).get("bind")
        if bind is None:
            _log.debug("audit_partition_check: no async engine available — skipping")
            _AUDIT_ARCHIVAL_GAUGE.set(0)
            return

        async with bind.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT c.relname
                    FROM   pg_inherits i
                    JOIN   pg_class c ON c.oid = i.inhrelid
                    JOIN   pg_class p ON p.oid = i.inhparent
                    WHERE  p.relname = 'audit_log'
                    """
                )
            )
            rows = result.fetchall()
        names = [row[0] for row in rows]
    except Exception as exc:
        _log.warning("audit_partition_check: failed to query pg_inherits: %s", exc)
        return

    eligible = audit_partitions_eligible_for_archival(names)
    count = len(eligible)
    _AUDIT_ARCHIVAL_GAUGE.set(count)
    if count > 0:
        _log.warning(
            "audit_partition_check: %d audit_log partition(s) eligible for archival "
            "(older than 24 months): %s — run the detach procedure in docs/runbook-ops.md",
            count,
            ", ".join(eligible),
        )
    else:
        _log.debug("audit_partition_check: no partitions eligible for archival")


# All the "hourly" jobs below share this literal rather than each spelling
# `hours=1` (APScheduler accepts either) — `register_periodic` takes seconds,
# so the conversion needs to happen somewhere, and one named constant reads
# better at each call site than a bare `3600`.
_HOUR_S: int = 3600


def _describe_workspace_expiry(result: ExpiryResult) -> str | None:
    # Unconditional: this line is the audit trail's own summary, not just a
    # "there was work" signal, so it is logged whether or not anything expired.
    return f"workspace_expiry.run: expired={result.expired_count} batch_ts={result.batch_ts}"


def _describe_memory_expiry(result: MemoryExpiryResult) -> str | None:
    if not result.expired_count:
        return None
    return f"memory_expiry.run: expired={result.expired_count} batches={result.batches} truncated={result.truncated}"


def _describe_usage_expiry(result: UsageExpiryResult) -> str | None:
    if not result.deleted_count:
        return None
    return (
        f"usage_expiry.run: deleted={result.deleted_count} batches={result.batches} "
        f"truncated={result.truncated} cutoff={result.cutoff.isoformat()}"
    )


def _describe_consolidation_sweep(report: SweepReport) -> str | None:
    if not report.had_work:
        return None
    return (
        f"consolidation_sweep.run: considered={report.considered} decided={report.decided} "
        f"already_settled={report.already_settled} failed={report.failed}"
    )


def _describe_extraction_drain(report: DrainReport) -> str | None:
    if not report.had_work:
        return None
    return (
        f"extraction_drain.run: claimed={report.claimed} staged_claims={report.staged_claims} "
        f"retried={report.retried} dead_lettered={report.dead_lettered} refusals={report.refusals}"
    )


def _describe_arc_audit_drain(result: DrainResult) -> str | None:
    if not result.claimed:
        return None
    return f"arc_audit_drain.run: claimed={result.claimed} drained={result.drained} failed={result.failed}"


def _describe_arc_challenge_cleanup(result: CleanupResult) -> str | None:
    if not result.deleted:
        return None
    return f"arc_challenge_cleanup.run: deleted={result.deleted}"


def _describe_arc_review_expiry(result: ReviewExpiryResult) -> str | None:
    if not result.expired_revisions:
        return None
    return (
        f"arc_review_expiry.run: expired={result.expired_revisions} "
        f"obligations_tombstoned={result.tombstoned_obligations}"
    )


def build_scheduler(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    embedder: Embedder,
) -> tuple[AsyncIOScheduler, WebhookDeliveryWorker]:
    """Construct the scheduler and register every background job on it.

    Returns the scheduler (started later, in `start`, once the app's
    lifespan begins) and the webhook worker (whose HTTP client `lifespan`
    closes on shutdown — the only one of these workers that owns a
    resource its caller has to release).
    """
    # create_scheduler() uses SQLAlchemyJobStore (durable across restarts) and
    # falls back to MemoryJobStore when
    # settings.scheduler_use_memory_jobstore=True (unit tests / no sync driver).
    scheduler = create_scheduler(settings)

    # drain_outbox is a standalone function, not one of the worker classes
    # register_periodic targets, and it needs `kwargs=` to pass session_factory,
    # embedder, and settings through to a plain function rather than a bound
    # method closing over them — outside what the helper covers.
    scheduler.add_job(
        drain_outbox,
        trigger="interval",
        seconds=settings.outbox_poll_interval_s,
        kwargs={
            "session_factory": session_factory,
            "embedder": embedder,
            "settings": settings,
        },
        max_instances=1,
        coalesce=True,
        id="embedding_drain",
        replace_existing=True,
    )

    # The extraction drain runs on the same scheduler as every other background
    # job. Registered unconditionally, including with the no-op provider: a tick
    # that finds an empty queue costs one indexed count, and making registration
    # conditional would mean a deployment that later configures a provider needs
    # a restart before anything is extracted.
    # Session-observation extraction. The provider is selected by configuration
    # and defaults to one that proposes nothing, so constructing this is safe on
    # a deployment that has no LLM and wants none. Built here rather than
    # alongside the other services because the scheduler needs it, and the
    # scheduler is assembled before them.
    extraction_provider = build_extraction_provider(settings)
    extraction = ExtractionService(session_factory, ClaimService(session_factory, clock=clock))
    extraction_drain = ExtractionDrainWorker(session_factory, extraction_provider, extraction, clock=clock)

    # Registered unconditionally, including with the no-op provider: a tick that
    # finds an empty queue costs one indexed count, and making registration
    # conditional would mean a deployment that later configures a provider needs
    # a restart before anything is extracted.
    # Reconciling staged claims against each other. Registered unconditionally: it is
    # not conditional on an LLM provider, because most decisions are made from typed
    # values alone and need no model at all.
    consolidation = ConsolidationService(session_factory, clock=clock)
    consolidation_sweep = ConsolidationSweepWorker(session_factory, consolidation, clock=clock)
    register_periodic(
        scheduler,
        consolidation_sweep.run_once,
        job_id="consolidation_sweep",
        interval_seconds=settings.consolidation_sweep_interval_s,
        log=_log,
        describe=_describe_consolidation_sweep,
    )

    # The closure cache's only writer-after-startup. Edge mutations enqueue
    # into closure_outbox; without this drain the cache never refreshes and
    # every traversal after the first edge change pays the CTE fallback.
    closure_refresh = ClosureRefreshWorker(session_factory)
    register_periodic(
        scheduler,
        closure_refresh.run_once,
        job_id="closure_refresh",
        interval_seconds=settings.closure_refresh_interval_s,
        log=_log,
        describe=lambda processed: (f"refreshed {processed} closure row(s)" if processed else None),
    )

    register_periodic(
        scheduler,
        extraction_drain.run_once,
        job_id="extraction_drain",
        interval_seconds=settings.outbox_poll_interval_s,
        log=_log,
        describe=_describe_extraction_drain,
    )

    # Hourly check for audit_log partitions eligible for archival (> 24 months old).
    # First run fires at startup so operators see the warning without waiting;
    # subsequent runs follow the interval trigger every hour. Not one of the
    # run_once workers register_periodic targets — it is a standalone function
    # that already owns its own try/except and gauge update, and needs
    # `kwargs=` to pass session_factory through — so it keeps calling
    # `add_job()` directly.
    scheduler.add_job(
        check_audit_partition_ages,
        trigger="interval",
        hours=1,
        kwargs={"session_factory": session_factory},
        max_instances=1,
        coalesce=True,
        id="audit_partition_check",
        replace_existing=True,
    )

    # Drain pending notification_deliveries rows on an interval so the webhook
    # fan-out SLO ("< 30s from triggering write") is met at runtime. The
    # worker instance is constructed here so it binds to the same event loop
    # the scheduler runs on.
    webhook_http_client = httpx.AsyncClient(timeout=settings.webhook_request_timeout_s)
    webhook_worker = WebhookDeliveryWorker(
        session_factory=session_factory,
        clock=clock,
        http_client=webhook_http_client,
    )
    register_periodic(
        scheduler,
        # run_once takes a batch_size keyword argument, so it is bound here
        # into a zero-argument callable before register_periodic ever sees it.
        functools.partial(webhook_worker.run_once, batch_size=settings.webhook_batch_size),
        job_id="webhook_delivery_drain",
        interval_seconds=settings.webhook_drain_interval_s,
        log=_log,
    )

    # Hourly soft-invalidation of workspace entries whose expires_at has passed.
    # The worker runs across all tenants in one pass; entries are retained for
    # audit linkage and RTBF — only t_invalidated_at is set, no physical delete.
    expiry_worker = WorkspaceExpiryWorker(
        session_factory=session_factory,
        clock=clock,
    )
    register_periodic(
        scheduler,
        expiry_worker.run,
        job_id="workspace_expiry",
        interval_seconds=_HOUR_S,
        log=_log,
        describe=_describe_workspace_expiry,
    )

    # Session-memory retention. Hourly, which is well inside the 24-hour
    # bound the requirement sets, and the sweep is soft -- events leave the
    # read path but stay addressable for audit.
    memory_expiry = MemoryExpiryWorker(session_factory=session_factory, clock=clock)
    register_periodic(
        scheduler,
        memory_expiry.run,
        job_id="memory_expiry",
        interval_seconds=_HOUR_S,
        log=_log,
        describe=_describe_memory_expiry,
    )

    # Usage retention. Hourly, comfortably inside the 24-hour boundary the
    # unlike the session sweep above this one is a hard delete: these rows are not
    # evidence, so a soft flag would keep personal data in the table while
    # pretending it was gone.
    usage_expiry = UsageExpiryWorker(
        session_factory=session_factory,
        retention_days=settings.usage_retention_days,
        clock=clock,
    )
    register_periodic(
        scheduler,
        usage_expiry.run,
        job_id="usage_expiry",
        interval_seconds=_HOUR_S,
        log=_log,
        describe=_describe_usage_expiry,
    )

    # Usage rollups. Hourly, covering yesterday and today — yesterday because a
    # day is only complete once it is over, today because a dashboard that shows
    # nothing until tomorrow is one nobody opens. Idempotent, so re-rolling is free.
    usage_rollup = UsageRollupWorker(session_factory=session_factory, clock=clock)
    register_periodic(
        scheduler,
        usage_rollup.run,
        job_id="usage_rollup",
        interval_seconds=_HOUR_S,
        log=_log,
        # No describe: the worker already logs its own summary line
        # internally when a rollup actually touches rows; a second one here
        # would just repeat it.
    )

    # ARC background workers. Each owns one bounded, idempotent pass; the
    # scheduler decides how often, and `max_instances=1` plus `coalesce`
    # means a slow pass delays the next rather than overlapping with it.
    arc_audit_drain = AuditDrainWorker(session_factory=session_factory, clock=clock)
    arc_challenge_cleanup = ChallengeCleanupWorker(session_factory=session_factory, clock=clock)
    arc_review_expiry = ReviewExpiryWorker(session_factory=session_factory, clock=clock)

    # Frequent: audit rows are evidence, and the gauge an operator watches is
    # depth, so a long interval would make a healthy system look backed up.
    register_periodic(
        scheduler,
        arc_audit_drain.run_once,
        job_id="arc_audit_drain",
        interval_seconds=30,
        log=_log,
        describe=_describe_arc_audit_drain,
    )
    # Hourly: challenges are deleted a day after expiry, so nothing is
    # gained by checking more often than the granularity of that window.
    register_periodic(
        scheduler,
        arc_challenge_cleanup.run_once,
        job_id="arc_challenge_cleanup",
        interval_seconds=_HOUR_S,
        log=_log,
        describe=_describe_arc_challenge_cleanup,
    )
    # Hourly: review dates are set in days. Running this often would be
    # churn, but running it daily would leave stale governance binding
    # agents for most of a day after it expired.
    register_periodic(
        scheduler,
        arc_review_expiry.run_once,
        job_id="arc_review_expiry",
        interval_seconds=_HOUR_S,
        log=_log,
        describe=_describe_arc_review_expiry,
    )

    return scheduler, webhook_worker


async def start(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    catalog: CatalogService,
    settings: Settings,
) -> None:
    """Start the scheduler, fire the immediate audit check, register sync jobs."""
    scheduler.start()
    # Fire the audit partition age check once at startup so operators see
    # the WARNING immediately without waiting up to an hour.
    await check_audit_partition_ages(session_factory=session_factory)
    # Register sync-source cron jobs after scheduler is running.
    await register_sync_jobs(
        scheduler=scheduler,
        session_factory=session_factory,
        catalog=catalog,
        settings=settings,
    )
