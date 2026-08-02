"""Prometheus metric surface for ARC.

`/metrics` (wired in `registry.main`) calls `generate_latest()` with no
registry argument, so it serves the implicit global registry every
`Counter`/`Gauge`/`Histogram` in the process registers itself into at
construction time -- there is no separate ARC registry to assemble. Defining
every ARC metric in one module is what keeps construction a single event per
process: the "Duplicate timeseries" error `prometheus_client` raises comes
from constructing two metric objects under the same name, which happens if a
module that calls `Counter(...)` at import time gets executed twice (an
`importlib.reload`, or two different import paths reaching the same file
under different module names). Every other metric-defining module in this
repo relies on the same guarantee instead of a workaround -- a plain
module-level constructor call, executed exactly once by Python's own import
cache -- so this module follows suit rather than inventing a custom registry
or a reload guard that nothing else here uses.

Cardinality discipline
-----------------------
Every label below resolves to one of a small, enumerable set of values that
is fixed in this module, not computed from request data. ARC's whole job is
handling multi-tenant, attacker-influenceable, free-text governed content --
a tenant id, a receipt id, a raw idempotency key, a nonce, or an exception
message is exactly the kind of value that looks like a reasonable label
right up until the number of tenants, receipts, or attacker-chosen strings
turns one metric into an unbounded number of time series and the TSDB falls
over. None of those appear as a label here:

- No metric is broken out by tenant, here or anywhere else in this module.
  An operator who needs a per-tenant count already has the audit log and the
  receipt table for that; a Prometheus label is not where per-tenant
  cardinality belongs, because the set of tenants only ever grows.
- No metric carries a receipt id, an idempotency key, or a nonce. Every one
  of those identifies a single request, not a class of requests -- the
  wrong shape for a label regardless of how bounded it happens to be.
- The one free-text-adjacent label (`reason` on JIT denials) is restricted
  to `JIT_DENIAL_REASONS`, a closed set of code-defined constants having
  nothing to do with what a caller submitted. `observe_jit_denial` raises
  rather than labelling an unrecognised reason, so a future call site cannot
  widen the set by accident.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from registry.arc.types import ResolutionStatus

# ---------------------------------------------------------------------------
# Closed label vocabularies
# ---------------------------------------------------------------------------

# Exactly `resolve_context`'s three outcomes -- see `ResolutionStatus` in
# `registry.arc.types`. Built from the enum rather than typed out again so a
# future fourth status is a label-surface change made here, deliberately,
# rather than one this module's test silently stops covering.
RESOLUTION_STATUSES: frozenset[str] = frozenset(status.value for status in ResolutionStatus)

# Mirrors the denial-reason constants in `registry.arc.service.jit`
# (`DENIED_REVOKED`, `DENIED_AUDIENCE`, `DENIED_NOT_SELECTED`,
# `DENIED_RECEIPT_UNUSABLE`, `DENIED_CHAIN_BUDGET`) plus `invalid_continuation`,
# the reason recorded when a presented continuation token fails to open
# before a receipt is even loaded. Copied rather than imported: this module
# is meant to be a cheap, dependency-light leaf that every request path
# pulls in just to increment a counter, and `jit.py` pulls in the receipt,
# continuation, and signing modules to do its own job -- importing it here
# would make a metrics-definitions module transitively require the whole ARC
# service graph. `tests/unit/test_arc_metrics.py` asserts this set matches
# `jit.py`'s constants exactly, so the two cannot silently drift apart.
JIT_DENIAL_REASONS: frozenset[str] = frozenset(
    {
        "detail_revoked",
        "detail_audience_denied",
        "detail_not_selected",
        "detail_receipt_unusable",
        "detail_chain_budget_exhausted",
        "invalid_continuation",
    }
)

# ---------------------------------------------------------------------------
# Resolution outcomes
# ---------------------------------------------------------------------------

RESOLUTIONS_TOTAL = Counter(
    "registry_arc_resolutions_total",
    "ARC context resolutions, labeled by outcome status (ready, degraded, blocked).",
    ["status"],
)

RESOLUTION_DURATION_SECONDS = Histogram(
    "registry_arc_resolution_duration_seconds",
    "End-to-end latency of one resolve_context call, including any serialization-failure retries.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def observe_resolution(status: ResolutionStatus) -> None:
    """Count one resolution outcome.

    Takes the enum, not a string. `ResolutionStatus` has exactly three
    members, so mypy rejects a call passing anything outside
    `RESOLUTION_STATUSES` before the process ever runs -- no runtime guard
    needed here the way `observe_jit_denial` needs one for a plain string.
    """
    RESOLUTIONS_TOTAL.labels(status=status.value).inc()


def observe_resolution_latency(seconds: float) -> None:
    """Record one resolve_context call's wall-clock duration, in seconds."""
    RESOLUTION_DURATION_SECONDS.observe(seconds)


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------

# Unlabeled totals, deliberately. A challenge is identified by its own id,
# bound to a caller-chosen idempotency key and a derived nonce -- none of
# those identify a *class* of challenge, so none belong on a label. The
# operational question these answer is "is issuance keeping pace with
# consumption" -- a deployment-wide rate, not a per-challenge fact.
CHALLENGES_ISSUED_TOTAL = Counter(
    "registry_arc_challenges_issued_total",
    "ARC context challenges issued, including a resumed retry under an idempotency key already seen.",
)

CHALLENGES_CONSUMED_TOTAL = Counter(
    "registry_arc_challenges_consumed_total",
    "ARC context challenges marked consumed by a resolution.",
)


def observe_challenge_issued() -> None:
    """Count one challenge issuance (fresh or resumed by an exact retry)."""
    CHALLENGES_ISSUED_TOTAL.inc()


def observe_challenge_consumed() -> None:
    """Count one challenge marked consumed by a resolution."""
    CHALLENGES_CONSUMED_TOTAL.inc()


# ---------------------------------------------------------------------------
# Receipt integrity
# ---------------------------------------------------------------------------

# No labels and no severity split: every failure here means a receipt's hash
# chain -- ARC's evidence record -- did not verify, which is always worth
# paging on regardless of which specific check caught it (gap, fork,
# tampered payload, bad signature, or a head that moved out from under a
# lock). A single unlabeled counter keeps the alert a one-line
# `rate(registry_arc_receipt_integrity_failures_total[5m]) > 0` rather than
# a rule that has to enumerate failure kinds to stay correct.
RECEIPT_INTEGRITY_FAILURES_TOTAL = Counter(
    "registry_arc_receipt_integrity_failures_total",
    "Receipt hash-chain integrity failures. Any nonzero rate means a receipt may have been tampered with.",
)


def observe_receipt_integrity_failure() -> None:
    """Count one receipt whose hash chain failed verification. Alertable."""
    RECEIPT_INTEGRITY_FAILURES_TOTAL.inc()


# ---------------------------------------------------------------------------
# Audit outbox depth
# ---------------------------------------------------------------------------

# Deployment-wide, not per-tenant. `arc_audit_outbox` holds every tenant's
# undrained rows in one table; a `tenant_id` label would turn one gauge into
# one time series per tenant that has ever written an ARC event -- a set
# that only grows as tenants are onboarded and never shrinks as they churn.
# The question this gauge answers, "is the drain worker keeping up," is a
# deployment-wide one; an operator who needs a per-tenant breakdown queries
# the table directly rather than through Prometheus.
AUDIT_OUTBOX_DEPTH = Gauge(
    "registry_arc_audit_outbox_depth",
    "Undrained rows currently in arc_audit_outbox, across every tenant.",
)


def set_audit_outbox_depth(depth: int) -> None:
    """Set the current undrained-row count, as of the caller's last count."""
    AUDIT_OUTBOX_DEPTH.set(depth)


# ---------------------------------------------------------------------------
# JIT detail grants and denials
# ---------------------------------------------------------------------------

JIT_GRANTS_TOTAL = Counter(
    "registry_arc_jit_grants_total",
    "JIT detail-page requests that were granted (at least one item returned, possibly audience-redacted).",
)

JIT_DENIALS_TOTAL = Counter(
    "registry_arc_jit_denials_total",
    "JIT detail-page requests that were denied, labeled by the closed set of reasons in JIT_DENIAL_REASONS.",
    ["reason"],
)


def observe_jit_grant() -> None:
    """Count one granted JIT detail page."""
    JIT_GRANTS_TOTAL.inc()


def observe_jit_denial(reason: str) -> None:
    """Count one JIT denial, refusing a reason outside the closed vocabulary.

    `reason` is a plain string at every call site -- `jit.py`'s denial codes
    are module-level string constants, not an enum -- so unlike
    `observe_resolution` this needs a runtime check. Raising here turns a
    label-cardinality leak into a wiring error caught at the first bad call,
    not a slow accumulation of one-off time series an operator finds in the
    TSDB months later.
    """
    if reason not in JIT_DENIAL_REASONS:
        msg = f"{reason!r} is not in JIT_DENIAL_REASONS; add it there before using it as a metric label value"
        raise ValueError(msg)
    JIT_DENIALS_TOTAL.labels(reason=reason).inc()


__all__ = [
    "AUDIT_OUTBOX_DEPTH",
    "CHALLENGES_CONSUMED_TOTAL",
    "CHALLENGES_ISSUED_TOTAL",
    "JIT_DENIAL_REASONS",
    "JIT_DENIALS_TOTAL",
    "JIT_GRANTS_TOTAL",
    "RECEIPT_INTEGRITY_FAILURES_TOTAL",
    "RESOLUTIONS_TOTAL",
    "RESOLUTION_DURATION_SECONDS",
    "RESOLUTION_STATUSES",
    "observe_challenge_consumed",
    "observe_challenge_issued",
    "observe_jit_denial",
    "observe_jit_grant",
    "observe_receipt_integrity_failure",
    "observe_resolution",
    "observe_resolution_latency",
    "set_audit_outbox_depth",
]
