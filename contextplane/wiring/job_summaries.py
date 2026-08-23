"""One-line summaries of what each scheduled job did.

Extracted from `wiring/jobs.py`, which had two lines of headroom under its
800-line ceiling and could not take another job registration without one. The
ceiling's purpose is that adding a job touches an area's wiring rather than the
composition root, so the thing to move was whatever is not composition -- and
seventeen report formatters are not composition. They take a worker's result and
return the line the scheduler logs, and none of them knows a scheduler exists.

Every one returns `None` when there was nothing to say, so a quiet job stays
quiet and a log reader can trust that a line means work happened. The two that
log unconditionally say so at their own definition: their line *is* the audit
trail's summary rather than a "there was work" signal, and a silent run is
exactly what an auditor would need to see recorded.
"""

from __future__ import annotations

# Through the `contextplane.arc` front door rather than at the worker modules,
# which the import-linter contract "ARC internals are private" refuses. The
# result types are part of ARC's published surface precisely so a consumer like
# this one never has to know which worker module defines which.
from contextplane.arc import (
    CheckpointExportResult,
    CleanupResult,
    DrainResult,
    ObservationFingerprintReaperResult,
    ObservationWindowEvaluatorResult,
    ReviewExpiryResult,
    SourceStatusRefreshResult,
)
from contextplane.service.memory.trust_transitions import SweepReport as TrustSweepReport
from contextplane.signals.aggregates import PrivacyAggregateReport
from contextplane.workers.calibration_refit import CalibrationRefitReport
from contextplane.workers.consolidation_sweep import SweepReport
from contextplane.workers.derivative_propagation import PropagationReport
from contextplane.workers.extraction_drain import DrainReport
from contextplane.workers.memory_expiry import MemoryExpiryResult
from contextplane.workers.promotion_sweep import SweepReport as PromotionSweepReport
from contextplane.workers.retention_expiry import RetentionExpiryReport
from contextplane.workers.usage_expiry import UsageExpiryResult
from contextplane.workers.workspace_expiry import ExpiryResult


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


def _describe_derivative_propagation(report: PropagationReport) -> str | None:
    # `failed` is called out separately because it is the only field here that is
    # an incident: an item nobody will retry means erased content is still in the
    # artefact it was scheduled to be removed from.
    if not report.had_work:
        return None
    return (
        f"derivative_propagation.run: claimed={report.claimed} applied={report.applied} "
        f"artefacts={report.artefacts} retried={report.retried} failed={report.failed}"
    )


def _describe_retention_expiry(report: RetentionExpiryReport) -> str | None:
    # `held` is logged even on an otherwise empty tick: records past their period
    # that a hold is keeping are the state an operator has to be able to see, and
    # a tick that reported nothing would hide exactly that.
    if not report.had_work and not report.held:
        return None
    return (
        f"retention_expiry.run: tenants={report.tenants} minimized={report.minimized} "
        f"enqueued={report.enqueued} held={report.held} truncated={report.truncated}"
    )


def _describe_privacy_aggregates(report: PrivacyAggregateReport) -> str | None:
    # `withheld` is called out because it is the differencing defence firing: a
    # cell whose recompute disagreed with the published figure. An operator seeing
    # that number move should be able to attribute it to the erasure behind it.
    if not report.had_work:
        return None
    return (
        f"privacy_aggregates.run: tenants={report.tenants} written={report.written} "
        f"withheld={report.withheld} recomputed={report.recomputed}"
    )


def _describe_consolidation_sweep(report: SweepReport) -> str | None:
    if not report.had_work:
        return None
    return (
        f"consolidation_sweep.run: considered={report.considered} decided={report.decided} "
        f"already_settled={report.already_settled} failed={report.failed}"
    )


def _describe_promotion_sweep(report: PromotionSweepReport) -> str | None:
    if not report.had_work:
        return None
    return (
        f"promotion_sweep.run: considered={report.considered} auto_promoted={report.auto_promoted} "
        f"awaiting_review={report.awaiting_review} not_eligible={report.not_eligible} failed={report.failed}"
    )


def _describe_calibration_refit(report: CalibrationRefitReport) -> str | None:
    if not report.had_work:
        return None
    return (
        f"calibration_refit.run: considered={report.considered} activated={report.activated} "
        f"stored_failed={report.stored_failed} below_minimum={report.below_minimum} failed={report.failed}"
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


def _describe_arc_source_status_refresh(result: SourceStatusRefreshResult) -> str | None:
    if not result.due:
        return None
    return (
        f"arc_source_status_refresh.run: due={result.due} refreshed={result.refreshed} "
        f"integrity_pending={result.integrity_pending} failed={result.failed}"
    )


def _describe_arc_checkpoint_exporter(result: CheckpointExportResult) -> str | None:
    if not result.due:
        return None
    return (
        f"arc_checkpoint_exporter.run: due={result.due} exported={result.exported} "
        f"sink_unavailable={result.sink_unavailable} integrity_failed={result.integrity_failed}"
    )


def _describe_arc_observation_window_evaluator(result: ObservationWindowEvaluatorResult) -> str | None:
    if not result.checked:
        return None
    return f"arc_observation_window_evaluator.run: checked={result.checked} closed={result.closed}"


def _describe_arc_observation_fingerprint_reaper(result: ObservationFingerprintReaperResult) -> str | None:
    if not result.reaped:
        return None
    return f"arc_observation_fingerprint_reaper.run: reaped={result.reaped}"


def _describe_trust_transitions(report: TrustSweepReport) -> str | None:
    # Logged whenever anything fell, and silent otherwise. The ratio is the
    # signal: a pass that examines many and records none is a healthy store, and
    # one that records most of what it sees means a half-life is wrong.
    if not report.recorded:
        return None
    return f"trust_transition_sweep.run: examined={report.examined} recorded={report.recorded}"


__all__ = [
    "_describe_trust_transitions",
    "_describe_arc_audit_drain",
    "_describe_arc_challenge_cleanup",
    "_describe_arc_checkpoint_exporter",
    "_describe_arc_observation_fingerprint_reaper",
    "_describe_arc_observation_window_evaluator",
    "_describe_arc_review_expiry",
    "_describe_arc_source_status_refresh",
    "_describe_calibration_refit",
    "_describe_consolidation_sweep",
    "_describe_derivative_propagation",
    "_describe_extraction_drain",
    "_describe_memory_expiry",
    "_describe_privacy_aggregates",
    "_describe_promotion_sweep",
    "_describe_retention_expiry",
    "_describe_usage_expiry",
    "_describe_workspace_expiry",
]
