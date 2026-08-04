"""Background workers report that they ran, how long, and how far behind they are.

A worker failure is the one failure nobody receives. Nothing is on a request
path, so no client sees an error and no latency percentile moves; the only
symptom is work quietly not happening — a notification never delivered, a
closure cache drifting out of date — noticed days later by whoever depended on
it.

Depth is measured as well as throughput because throughput alone cannot answer
the question operators actually ask. A worker draining fifty rows a tick looks
exactly the same whether the backlog behind it is empty or growing without
bound.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from registry import metrics


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_a_completed_run_is_counted_and_timed() -> None:
    before = _sample("registry_worker_runs_total", worker="probe", outcome="ok")
    with metrics.observe_worker_run("probe"):
        pass
    assert _sample("registry_worker_runs_total", worker="probe", outcome="ok") == before + 1
    assert _sample("registry_worker_run_duration_seconds_count", worker="probe") >= 1


def test_a_failing_run_is_counted_as_an_error_and_still_raises() -> None:
    before = _sample("registry_worker_runs_total", worker="probe", outcome="error")
    with pytest.raises(RuntimeError, match="worker died"), metrics.observe_worker_run("probe"):
        raise RuntimeError("worker died")

    assert _sample("registry_worker_runs_total", worker="probe", outcome="error") == before + 1
    # Timed either way: a run that failed after ten minutes is a different
    # problem from one that failed immediately, and only the histogram says
    # which happened.
    assert _sample("registry_worker_run_duration_seconds_count", worker="probe") >= 2


def test_a_failing_run_is_not_also_counted_as_a_success() -> None:
    # The two outcomes are read as a ratio; a run counted twice would put the
    # success rate above what actually happened.
    before_ok = _sample("registry_worker_runs_total", worker="ratio", outcome="ok")
    with pytest.raises(ValueError, match="boom"), metrics.observe_worker_run("ratio"):
        raise ValueError("boom")
    assert _sample("registry_worker_runs_total", worker="ratio", outcome="ok") == before_ok


def test_queue_depth_is_a_gauge_that_can_fall() -> None:
    # A counter cannot express a backlog draining. Set, not increment.
    metrics.observe_queue_depth(queue="probe_q", depth=41)
    assert _sample("registry_worker_queue_depth", queue="probe_q") == 41
    metrics.observe_queue_depth(queue="probe_q", depth=0)
    assert _sample("registry_worker_queue_depth", queue="probe_q") == 0


def test_dead_lettered_rows_are_counted() -> None:
    before = _sample("registry_worker_dead_lettered_total", queue="probe_q")
    metrics.observe_dead_lettered(queue="probe_q", count=3)
    assert _sample("registry_worker_dead_lettered_total", queue="probe_q") == before + 3


def test_no_worker_metric_carries_an_identity_label() -> None:
    for metric in (
        metrics.WORKER_RUNS_TOTAL,
        metrics.WORKER_RUN_DURATION_SECONDS,
        metrics.WORKER_QUEUE_DEPTH,
        metrics.WORKER_DEAD_LETTERED_TOTAL,
    ):
        labels = set(metric._labelnames)
        assert not (labels & {"tenant", "tenant_id", "actor", "actor_id", "entity_id"})


@pytest.mark.parametrize(
    ("module", "cls", "method"),
    [
        ("registry.workers.webhook_delivery", "WebhookDeliveryWorker", "run_once"),
        ("registry.workers.closure_refresh", "ClosureRefreshWorker", "run_once"),
        ("registry.workers.workspace_expiry", "WorkspaceExpiryWorker", "run"),
        ("registry.workers.memory_expiry", "MemoryExpiryWorker", "run"),
    ],
)
def test_every_worker_entry_point_is_instrumented(module: str, cls: str, method: str) -> None:
    """Asserted per worker, by name.

    A worker added later is not covered by this list, which is the honest
    limit: unlike the MCP tool surface there is no registration seam to hook,
    because each worker is constructed and scheduled independently.
    """
    import importlib
    import inspect

    obj = getattr(importlib.import_module(module), cls)
    assert hasattr(obj, f"_{method}_inner"), f"{cls}.{method} has no instrumented wrapper"
    source = inspect.getsource(getattr(obj, method))
    assert "observe_worker_run" in source
