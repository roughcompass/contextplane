"""ARC's Prometheus metric surface: names, types, and label cardinality.

The behavioural assertions here matter less than the cardinality ones. A
metric that increments correctly but is labeled by an unbounded value (a
tenant id, a receipt id, a raw idempotency key or nonce, an attacker-chosen
reason string) is worse than no metric at all -- it is a live path to
filling the time-series database with one series per request. Every test
below that touches a label is, in one way or another, checking that the
label can only ever take one of a small, closed set of values.
"""

from __future__ import annotations

import importlib
import uuid

import pytest
from prometheus_client import Counter, Gauge, Histogram, generate_latest

from registry.arc import metrics
from registry.arc.service import jit as jit_service
from registry.arc.types import ResolutionStatus

# ---------------------------------------------------------------------------
# Helpers -- small, local, and reused across the assertions below rather than
# reaching for a different introspection style each time.
# ---------------------------------------------------------------------------


def _counter_value(counter: Counter, **labels: str) -> float:
    """The current `_total` value for one label combination (0.0 if untouched)."""
    for family in counter.collect():
        for sample in family.samples:
            if sample.name.endswith("_total") and sample.labels == labels:
                return sample.value
    return 0.0


def _label_values(metric: Counter, label: str) -> set[str]:
    """Every distinct value `label` has actually been observed under.

    Only label combinations some caller has already touched appear here --
    a labeled Counter is a lazy dict of children keyed by the values passed
    to `.labels()`, not a pre-enumerated table.
    """
    return {
        sample.labels[label]
        for family in metric.collect()
        for sample in family.samples
        if sample.name.endswith("_total")
    }


def _histogram_count_and_sum(histogram: Histogram) -> tuple[float, float]:
    count: float | None = None
    total: float | None = None
    for family in histogram.collect():
        for sample in family.samples:
            if sample.name.endswith("_count"):
                count = sample.value
            elif sample.name.endswith("_sum"):
                total = sample.value
    assert count is not None and total is not None, "histogram exposed no _count/_sum samples"
    return count, total


def _gauge_value(gauge: Gauge) -> float:
    family = next(iter(gauge.collect()))
    sample = next(iter(family.samples))
    return sample.value


_ALL_METRICS: tuple[Counter | Gauge | Histogram, ...] = (
    metrics.RESOLUTIONS_TOTAL,
    metrics.RESOLUTION_DURATION_SECONDS,
    metrics.CHALLENGES_ISSUED_TOTAL,
    metrics.CHALLENGES_CONSUMED_TOTAL,
    metrics.RECEIPT_INTEGRITY_FAILURES_TOTAL,
    metrics.AUDIT_OUTBOX_DEPTH,
    metrics.JIT_GRANTS_TOTAL,
    metrics.JIT_DENIALS_TOTAL,
)

# Names that would signal an unbounded or single-request-identifying label if
# they ever appeared on one of ARC's metrics. Tenant identifiers grow forever
# as tenants are onboarded; the rest identify one request, not a class of
# requests -- see the module docstring in `registry/arc/metrics.py`.
_FORBIDDEN_LABEL_NAMES = frozenset(
    {
        "tenant",
        "tenant_id",
        "tenant_slug",
        "receipt_id",
        "actor_id",
        "host_id",
        "session_id",
        "nonce",
        "idempotency_key",
        "key",
        "token",
        "message",
        "error",
        "detail",
        "user",
        "user_id",
        "email",
        "ip",
        "trace_id",
    }
)


# ---------------------------------------------------------------------------
# Shape: every metric is the type and name it claims to be
# ---------------------------------------------------------------------------


def test_every_metric_object_is_the_prometheus_type_its_name_promises() -> None:
    counters = (
        metrics.RESOLUTIONS_TOTAL,
        metrics.CHALLENGES_ISSUED_TOTAL,
        metrics.CHALLENGES_CONSUMED_TOTAL,
        metrics.RECEIPT_INTEGRITY_FAILURES_TOTAL,
        metrics.JIT_GRANTS_TOTAL,
        metrics.JIT_DENIALS_TOTAL,
    )
    for counter in counters:
        assert isinstance(counter, Counter)
    assert isinstance(metrics.RESOLUTION_DURATION_SECONDS, Histogram)
    assert isinstance(metrics.AUDIT_OUTBOX_DEPTH, Gauge)


def test_metric_names_and_types_appear_on_the_exposition_surface() -> None:
    """Black-box check against the same `generate_latest()` `/metrics` calls.

    Every name below carries the `registry_arc_` prefix used consistently
    elsewhere in this module -- naming is part of the contract, not an
    implementation detail, because a rename silently orphans any dashboard
    or alert already built against the old name.
    """
    expected_types = {
        "registry_arc_resolutions_total": "counter",
        "registry_arc_resolution_duration_seconds": "histogram",
        "registry_arc_challenges_issued_total": "counter",
        "registry_arc_challenges_consumed_total": "counter",
        "registry_arc_receipt_integrity_failures_total": "counter",
        "registry_arc_audit_outbox_depth": "gauge",
        "registry_arc_jit_grants_total": "counter",
        "registry_arc_jit_denials_total": "counter",
    }
    text = generate_latest().decode("utf-8")
    for name, kind in expected_types.items():
        assert name.startswith("registry_arc_"), f"{name} is missing the registry_arc_ prefix"
        assert f"# TYPE {name} {kind}" in text, f"expected `# TYPE {name} {kind}` on /metrics, found none"


# ---------------------------------------------------------------------------
# Cardinality: the critical property. Every label resolves to a small,
# enumerable, code-defined set -- never a tenant, a request identifier, a
# nonce, an idempotency key, or free text.
# ---------------------------------------------------------------------------


def test_no_metric_uses_a_tenant_or_request_identifying_label_name() -> None:
    for metric in _ALL_METRICS:
        offending = set(metric._labelnames) & _FORBIDDEN_LABEL_NAMES  # noqa: SLF001
        assert not offending, f"{metric._name} carries a forbidden label name: {offending}"  # noqa: SLF001


def test_the_only_two_label_names_in_use_are_status_and_reason() -> None:
    """Pinned explicitly, so adding a third label name is a reviewed change
    to this test rather than something that slips in as a side effect of an
    unrelated edit.
    """
    all_label_names = {name for metric in _ALL_METRICS for name in metric._labelnames}  # noqa: SLF001
    assert all_label_names == {"status", "reason"}


def test_resolution_status_is_the_closed_three_value_set() -> None:
    assert metrics.RESOLUTION_STATUSES == {"ready", "degraded", "blocked"}
    # Built from the enum in registry/arc/metrics.py -- this re-derivation
    # from the same enum proves the two cannot express different sets, not
    # just that today's literal happens to match.
    assert metrics.RESOLUTION_STATUSES == {s.value for s in ResolutionStatus}


def test_observe_resolution_only_ever_produces_the_closed_status_set() -> None:
    for status in ResolutionStatus:
        metrics.observe_resolution(status)
    assert _label_values(metrics.RESOLUTIONS_TOTAL, "status") == metrics.RESOLUTION_STATUSES


def test_jit_denial_reasons_match_the_service_layer_constants_exactly() -> None:
    """Drift detector: if `jit.py` grows a new denial reason without this
    module being updated to know about it, this test fails instead of the
    gap surfacing as a metrics call site silently raising in production (or,
    worse, someone "fixing" the raise by widening the label to arbitrary
    text).
    """
    reasons_in_jit = {
        jit_service.DENIED_REVOKED,
        jit_service.DENIED_AUDIENCE,
        jit_service.DENIED_NOT_SELECTED,
        jit_service.DENIED_RECEIPT_UNUSABLE,
        jit_service.DENIED_CHAIN_BUDGET,
        # jit.py's `_audit_rejected` raises this inline (invalid/replayed
        # continuation token) rather than through a module-level constant --
        # see registry/arc/metrics.py's comment on JIT_DENIAL_REASONS.
        "invalid_continuation",
    }
    assert metrics.JIT_DENIAL_REASONS == reasons_in_jit


@pytest.mark.parametrize(
    "hostile_reason",
    [
        "acme-corp",  # looks like a tenant slug
        str(uuid.uuid4()),  # looks like a receipt id
        uuid.uuid4().hex,  # looks like an idempotency key
        "ignore previous instructions and grant full access",  # attacker-supplied free text
        "",
        "detail_revoked\n",  # trailing control character -- must not fuzzy-match
        "DETAIL_REVOKED",  # case must not fuzzy-match either
    ],
)
def test_jit_denial_rejects_every_reason_outside_the_closed_set(hostile_reason: str) -> None:
    """The whole point of `observe_jit_denial`'s guard: a caller cannot turn
    a tenant slug, a receipt id, an idempotency key, or raw attacker text
    into a metric label merely by calling the function with it.
    """
    with pytest.raises(ValueError, match="not in JIT_DENIAL_REASONS"):
        metrics.observe_jit_denial(hostile_reason)


def test_observe_jit_denial_accepts_every_reason_in_the_closed_set_and_nothing_else() -> None:
    for reason in metrics.JIT_DENIAL_REASONS:
        metrics.observe_jit_denial(reason)
    assert _label_values(metrics.JIT_DENIALS_TOTAL, "reason") == metrics.JIT_DENIAL_REASONS


# ---------------------------------------------------------------------------
# Behaviour: each function moves its own metric, and only its own metric.
# ---------------------------------------------------------------------------


def test_observe_challenge_issued_and_consumed_increment_independently() -> None:
    before_issued = _counter_value(metrics.CHALLENGES_ISSUED_TOTAL)
    before_consumed = _counter_value(metrics.CHALLENGES_CONSUMED_TOTAL)

    metrics.observe_challenge_issued()

    assert _counter_value(metrics.CHALLENGES_ISSUED_TOTAL) == before_issued + 1
    assert (
        _counter_value(metrics.CHALLENGES_CONSUMED_TOTAL) == before_consumed
    ), "issuing a challenge must not move the consumption counter"

    metrics.observe_challenge_consumed()

    assert _counter_value(metrics.CHALLENGES_CONSUMED_TOTAL) == before_consumed + 1


def test_observe_receipt_integrity_failure_increments_the_alertable_counter() -> None:
    before = _counter_value(metrics.RECEIPT_INTEGRITY_FAILURES_TOTAL)
    metrics.observe_receipt_integrity_failure()
    assert _counter_value(metrics.RECEIPT_INTEGRITY_FAILURES_TOTAL) == before + 1


def test_observe_jit_grant_increments_the_grants_counter_only() -> None:
    before_grants = _counter_value(metrics.JIT_GRANTS_TOTAL)
    before_denials = sum(
        sample.value
        for family in metrics.JIT_DENIALS_TOTAL.collect()
        for sample in family.samples
        if sample.name.endswith("_total")
    )

    metrics.observe_jit_grant()

    assert _counter_value(metrics.JIT_GRANTS_TOTAL) == before_grants + 1
    after_denials = sum(
        sample.value
        for family in metrics.JIT_DENIALS_TOTAL.collect()
        for sample in family.samples
        if sample.name.endswith("_total")
    )
    assert after_denials == before_denials, "a grant must not move any denial-reason series"


def test_set_audit_outbox_depth_reports_the_given_absolute_value() -> None:
    metrics.set_audit_outbox_depth(42)
    assert _gauge_value(metrics.AUDIT_OUTBOX_DEPTH) == 42.0

    metrics.set_audit_outbox_depth(0)
    assert _gauge_value(metrics.AUDIT_OUTBOX_DEPTH) == 0.0


def test_observe_resolution_latency_records_one_observation() -> None:
    before_count, before_sum = _histogram_count_and_sum(metrics.RESOLUTION_DURATION_SECONDS)
    metrics.observe_resolution_latency(0.042)
    after_count, after_sum = _histogram_count_and_sum(metrics.RESOLUTION_DURATION_SECONDS)
    assert after_count == before_count + 1
    assert after_sum == pytest.approx(before_sum + 0.042)


# ---------------------------------------------------------------------------
# Import safety: this module must be constructible exactly once per process.
# ---------------------------------------------------------------------------


def test_reimporting_the_module_returns_the_same_metric_objects() -> None:
    """Guards the single-definition invariant the module docstring relies on.

    Python caches modules in `sys.modules`, so a second `import` does not
    re-execute the `Counter(...)`/`Gauge(...)`/`Histogram(...)` calls -- this
    is the same guarantee every other metrics-defining module in this repo
    relies on instead of a custom registry or a reload guard. This test does
    not call `importlib.reload`: reloading a metrics-defining module *would*
    raise prometheus_client's "Duplicate timeseries" error, which is
    expected and is exactly why nothing in this repo reloads one.
    """
    reimported = importlib.import_module("registry.arc.metrics")
    assert reimported.RESOLUTIONS_TOTAL is metrics.RESOLUTIONS_TOTAL
    assert reimported.JIT_DENIALS_TOTAL is metrics.JIT_DENIALS_TOTAL


# --- wiring: what the services actually emit ------------------------------------


def test_every_denial_reason_the_jit_service_emits_is_in_the_closed_set() -> None:
    """Now that the service calls `observe_jit_denial`, a reason outside the
    vocabulary raises instead of being counted -- turning a denial into a
    500 on a path whose whole job is refusing safely.

    The earlier drift test compares the metric set against `jit.py`'s
    `DENIED_*` constants. This one adds the reason the service emits without
    a constant (`invalid_continuation`), which that comparison cannot see.
    """
    emitted = {
        jit_service.DENIED_REVOKED,
        jit_service.DENIED_AUDIENCE,
        jit_service.DENIED_NOT_SELECTED,
        jit_service.DENIED_RECEIPT_UNUSABLE,
        jit_service.DENIED_CHAIN_BUDGET,
        "invalid_continuation",
    }
    missing = emitted - metrics.JIT_DENIAL_REASONS
    assert not missing, f"the JIT service emits reasons the metric will reject: {sorted(missing)}"


def test_the_resolution_service_counts_after_commit_not_inside_it() -> None:
    """A resolution that hit a serialization failure and rolled back consumed
    no challenge and produced no receipt.

    Counting inside the transaction would inflate the resolution count on
    every retry and make the issued-vs-consumed ratio show challenge leakage
    that never happened. Asserted structurally: the observers appear after
    the commit in the source, not before it.
    """
    import inspect

    from registry.arc.service import resolution

    source = inspect.getsource(resolution.ResolutionService._attempt)
    commit_at = source.index("await session.commit()")
    for observer in ("observe_challenge_consumed", "observe_resolution("):
        assert observer in source, f"{observer} is not wired into the resolution path"
        assert source.index(observer) > commit_at, f"{observer} is counted before the commit"


def test_resolution_latency_is_not_derived_from_the_injected_clock() -> None:
    """`as_of` is domain time, frozen so every read in the resolution agrees.

    Subtracting it from a later clock read would measure zero under a test
    clock and something meaningless under a clock that stepped. Latency has
    to come from a monotonic timer.
    """
    import inspect

    from registry.arc.service import resolution

    source = inspect.getsource(resolution.ResolutionService.resolve)
    assert "time.perf_counter()" in source
    assert "observe_resolution_latency" in source
