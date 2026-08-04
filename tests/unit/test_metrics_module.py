"""The metrics module's vocabularies and their enforcement.

The property worth testing here is not that a counter increments — Prometheus
does that. It is that an unrecognised label value is refused rather than
published, because a metric that quietly accepts a typo creates a series nobody
watches and the mistake surfaces weeks later as a gap in a graph.
"""

from __future__ import annotations

import pytest

from registry import metrics


def test_request_type_vocabulary_is_exactly_the_dashboard_split() -> None:
    # read/write/mcp are the three the dashboard splits latency by; `other` is
    # where probe and scrape traffic goes so it cannot drag those percentiles.
    assert metrics.REQUEST_TYPES == {"read", "write", "mcp", "other"}


def test_status_is_a_class_not_a_code() -> None:
    assert metrics.STATUS_CLASSES == {"2xx", "3xx", "4xx", "5xx", "other"}


def test_streaming_paths_is_closed() -> None:
    # A prefix match would silently absorb a future endpoint under /mcp. This set
    # is what the metric-surface gate can see, so widening it is a visible act.
    assert metrics.STREAMING_PATHS == {"/mcp/sse"}


def test_unknown_route_is_a_constant_not_a_path() -> None:
    # The one value a 404 may produce. Using the raw path here would mint one
    # series per attempted URL, which an attacker can drive deliberately.
    assert metrics.UNKNOWN_ROUTE == "other"


@pytest.mark.parametrize("bad_type", ["READ", "browse", "", "read "])
def test_unknown_request_type_raises(bad_type: str) -> None:
    with pytest.raises(ValueError, match="unknown request type"):
        metrics.observe_request(route="/v1/x", method="GET", status="2xx", request_type=bad_type, seconds=0.1)


@pytest.mark.parametrize("bad_status", ["200", "2XX", "ok", ""])
def test_unknown_status_class_raises(bad_status: str) -> None:
    with pytest.raises(ValueError, match="unknown status class"):
        metrics.observe_request(route="/v1/x", method="GET", status=bad_status, request_type="read", seconds=0.1)


def test_a_streaming_request_is_counted_but_not_timed() -> None:
    # `seconds=None` is the caller saying "this duration would be a lie". The
    # counter still moves, because the request happened.
    labels = {"route": "/mcp/{path}", "method": "GET", "status": "2xx", "type": "mcp"}
    before = _counter_value("catalog_requests_total", **labels)
    metrics.observe_request(route="/mcp/{path}", method="GET", status="2xx", request_type="mcp", seconds=None)
    after = _counter_value("catalog_requests_total", **labels)
    assert after == before + 1

    # And the histogram did not move: a streaming duration is deliberately absent
    # rather than recorded as zero.
    assert _counter_value("http_request_duration_seconds_count", **labels) == 0.0


def test_sse_gauge_tracks_concurrency_in_both_directions() -> None:
    start = metrics.MCP_SSE_CONNECTIONS_ACTIVE._value.get()  # noqa: SLF001
    metrics.sse_connection_opened()
    assert metrics.MCP_SSE_CONNECTIONS_ACTIVE._value.get() == start + 1  # noqa: SLF001
    metrics.sse_connection_closed()
    assert metrics.MCP_SSE_CONNECTIONS_ACTIVE._value.get() == start  # noqa: SLF001


def test_mcp_tool_call_refuses_an_unknown_status_class() -> None:
    with pytest.raises(ValueError, match="unknown status class"):
        metrics.observe_mcp_tool_call(tool="whoami", status="fine", seconds=0.01)


def test_no_metric_in_this_module_carries_an_identity_label() -> None:
    """The rule the whole module exists to hold.

    Checked here as well as in the conformance gate: this one fails the moment
    someone adds the label, without needing a database or a built app.
    """
    forbidden = {
        "tenant",
        "tenant_id",
        "actor",
        "actor_id",
        "entity",
        "entity_id",
        "session",
        "session_id",
        "query",
    }
    for name in dir(metrics):
        obj = getattr(metrics, name)
        labels = getattr(obj, "_labelnames", None)
        if labels:
            assert not (set(labels) & forbidden), f"{name} carries an identity label: {labels}"


def _counter_value(metric: str, **labels: str) -> float:
    from prometheus_client import REGISTRY

    # `_total` first: prometheus_client suffixes counters, and a bare name lookup
    # would silently return None for every counter in this module.
    value = REGISTRY.get_sample_value(f"{metric}_total", labels)
    if value is None:
        value = REGISTRY.get_sample_value(metric, labels)
    return value or 0.0
