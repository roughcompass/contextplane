"""The metrics middleware: what it observes, and what it must never break.

Driven as raw ASGI rather than through a client, because the behaviours that
matter are about the protocol boundary — a response that never starts, a handler
that raises, a connection that streams — and a test client normalises exactly
those away.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from registry import metrics
from registry.api.middleware.metrics import MetricsMiddleware
from registry.usage.writer import UsageEvent, UsageWriter


class _App:
    routes: list = []


def _scope(path: str, method: str = "GET") -> dict:
    return {
        "type": "http",
        "path": path,
        "method": method,
        "path_params": {},
        "root_path": "",
        "headers": [],
        "app": _App(),
    }


async def _receive() -> dict:  # pragma: no cover - never consumed by these apps
    return {"type": "http.request", "body": b"", "more_body": False}


class _Sender:
    """Collects what the middleware let through, in order."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.sent.append(message)


def _sample(name: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return value or 0.0


def _requests(route: str, method: str, status: str, request_type: str) -> float:
    return _sample(
        "catalog_requests_total",
        route=route,
        method=method,
        status=status,
        type=request_type,
    )


def _durations(route: str, method: str, status: str, request_type: str) -> float:
    return _sample(
        "http_request_duration_seconds_count",
        route=route,
        method=method,
        status=status,
        type=request_type,
    )


async def _ok(scope: dict, receive: object, send: object) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


@pytest.mark.asyncio
async def test_a_normal_request_is_counted_and_timed() -> None:
    before_c = _requests(metrics.UNKNOWN_ROUTE, "GET", "2xx", "read")
    before_d = _durations(metrics.UNKNOWN_ROUTE, "GET", "2xx", "read")

    await MetricsMiddleware(_ok)(_scope("/v1/thing"), _receive, _Sender())

    assert _requests(metrics.UNKNOWN_ROUTE, "GET", "2xx", "read") == before_c + 1
    assert _durations(metrics.UNKNOWN_ROUTE, "GET", "2xx", "read") == before_d + 1


@pytest.mark.asyncio
async def test_a_throttled_request_is_observed() -> None:
    """The one case the outside-the-rate-limiter placement exists for.

    The rate limiter sends a 429 itself and never calls downstream, so this is
    the only response nested instrumentation would not see. The error-rate panel
    would then under-report exactly when the service is shedding load.
    """

    async def throttled(scope: dict, receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 429, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    before = _requests(metrics.UNKNOWN_ROUTE, "GET", "4xx", "read")
    await MetricsMiddleware(throttled)(_scope("/v1/thing"), _receive, _Sender())
    assert _requests(metrics.UNKNOWN_ROUTE, "GET", "4xx", "read") == before + 1


@pytest.mark.asyncio
async def test_an_unauthenticated_401_is_observed() -> None:
    # A request carrying no bearer token is passed through by the rate limiter
    # and the auth layer turns it into a 401. Not an ordering discriminator —
    # both placements see it — but it is traffic an operator wants counted.
    async def unauthenticated(scope: dict, receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    before = _requests(metrics.UNKNOWN_ROUTE, "POST", "4xx", "write")
    await MetricsMiddleware(unauthenticated)(_scope("/v1/thing", "POST"), _receive, _Sender())
    assert _requests(metrics.UNKNOWN_ROUTE, "POST", "4xx", "write") == before + 1


@pytest.mark.asyncio
async def test_a_handler_exception_is_still_counted_then_re_raised() -> None:
    async def boom(scope: dict, receive: object, send: object) -> None:
        raise RuntimeError("handler failed")

    before = _requests(metrics.UNKNOWN_ROUTE, "GET", "other", "read")
    with pytest.raises(RuntimeError, match="handler failed"):
        await MetricsMiddleware(boom)(_scope("/v1/thing"), _receive, _Sender())

    # Counted, because the request happened. Status is `other` rather than 5xx:
    # no response start was ever sent, so inventing a 500 would report a status
    # the client never received.
    assert _requests(metrics.UNKNOWN_ROUTE, "GET", "other", "read") == before + 1


@pytest.mark.asyncio
async def test_a_streaming_request_moves_the_gauge_and_not_the_histogram() -> None:
    before_gauge = metrics.MCP_SSE_CONNECTIONS_ACTIVE._value.get()  # noqa: SLF001
    before_hist = _durations(metrics.UNKNOWN_ROUTE, "GET", "2xx", "mcp")
    before_count = _requests(metrics.UNKNOWN_ROUTE, "GET", "2xx", "mcp")

    await MetricsMiddleware(_ok)(_scope("/mcp/sse"), _receive, _Sender())

    # The connection is counted and its lifetime is not recorded: an hour-long
    # stream in a histogram whose top bucket is ten seconds would be read as
    # latency by the panel that queries it.
    assert _requests(metrics.UNKNOWN_ROUTE, "GET", "2xx", "mcp") == before_count + 1
    assert _durations(metrics.UNKNOWN_ROUTE, "GET", "2xx", "mcp") == before_hist
    # Balanced: opened on entry, closed in the finally, so a disconnect cannot
    # leak a permanently-elevated gauge.
    assert metrics.MCP_SSE_CONNECTIONS_ACTIVE._value.get() == before_gauge  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_non_streaming_mcp_request_is_timed() -> None:
    # The negative half of the exclusion: it must not widen to the whole mount.
    before = _durations(metrics.UNKNOWN_ROUTE, "POST", "2xx", "mcp")
    await MetricsMiddleware(_ok)(_scope("/mcp/messages/", "POST"), _receive, _Sender())
    assert _durations(metrics.UNKNOWN_ROUTE, "POST", "2xx", "mcp") == before + 1


@pytest.mark.asyncio
async def test_a_failing_metric_never_reaches_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instrumentation that breaks a request is an outage it caused itself."""

    def explode(**_: object) -> None:
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(metrics, "observe_request", explode)

    sender = _Sender()
    await MetricsMiddleware(_ok)(_scope("/v1/thing"), _receive, sender)

    sent = sender.sent
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"{}"


@pytest.mark.asyncio
async def test_a_non_http_scope_passes_straight_through() -> None:
    seen: list[str] = []

    async def lifespan(scope: dict, receive: object, send: object) -> None:
        seen.append(scope["type"])

    await MetricsMiddleware(lifespan)({"type": "lifespan"}, _receive, _Sender())
    assert seen == ["lifespan"]


# ---------------------------------------------------------------------------
# Response size, for the usage tier
# ---------------------------------------------------------------------------
#
# Counted from the body chunks the middleware forwards, because a streaming or
# chunked response sets no Content-Length to read it off. Driven as raw ASGI for the
# same reason as everything above: a test client would coalesce the chunks.


class _UsageWriter(UsageWriter):
    """A real writer that captures instead of enqueueing.

    Subclassed rather than mocked because `record_rest_usage` isinstance-checks what
    it found on app state — the check that stops a half-built app from being written
    to — and a stand-in would be silently skipped, leaving these tests green with
    nothing recorded.
    """

    def __init__(self) -> None:
        super().__init__(session_factory=None)  # type: ignore[arg-type]
        self.events: list[UsageEvent] = []

    def record(self, event: UsageEvent) -> None:
        self.events.append(event)


def _usage_scope(path: str) -> tuple[dict, _UsageWriter]:
    """A scope carrying stashed identity, so recording is actually reached.

    Written straight into `scope["state"]`, which is where Starlette keeps request
    state and therefore where the middleware reads it from. Going through
    `stash_request_identity` would need a `Request`, and building one here would test
    Starlette rather than this middleware.
    """
    import uuid as _uuid

    from registry.usage.identity import _REQUEST_ATTR, UsageIdentity

    writer = _UsageWriter()
    scope = _scope(path)
    scope["app"].state = type("_S", (), {"usage_writer": writer})()  # type: ignore[attr-defined]
    scope["state"] = {_REQUEST_ATTR: UsageIdentity(tenant_id=_uuid.uuid4(), actor_id=_uuid.uuid4())}
    return scope, writer


@pytest.mark.asyncio
async def test_response_bytes_are_summed_across_chunks() -> None:
    async def chunked(scope: dict, receive: object, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"abc", "more_body": True})
        await send({"type": "http.response.body", "body": b"de", "more_body": False})

    scope, writer = _usage_scope("/v1/thing")
    await MetricsMiddleware(chunked)(scope, _receive, _Sender())

    (event,) = writer.events
    assert event.payload_bytes == 5


@pytest.mark.asyncio
async def test_an_empty_response_records_zero_bytes_not_null() -> None:
    # Zero is a real measurement: a 204 sent no bytes. Null means "not measured",
    # which is what a streaming connection gets, and conflating them would make the
    # average payload size wrong rather than absent.
    async def empty(scope: dict, receive: object, send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope, writer = _usage_scope("/v1/thing")
    await MetricsMiddleware(empty)(scope, _receive, _Sender())

    (event,) = writer.events
    assert event.payload_bytes == 0


@pytest.mark.asyncio
async def test_a_streaming_connection_reports_no_payload_size() -> None:
    """An SSE total is how long it stayed open, not the size of an answer.

    Summing the two into one column would make every average derived from it
    meaningless, so a streaming path is recorded as unmeasured rather than as a
    very large response.
    """
    streaming_path = next(iter(metrics.STREAMING_PATHS))

    async def stream(scope: dict, receive: object, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(3):
            await send({"type": "http.response.body", "body": b"data: x\n\n", "more_body": True})

    scope, writer = _usage_scope(streaming_path)
    await MetricsMiddleware(stream)(scope, _receive, _Sender())

    (event,) = writer.events
    assert event.payload_bytes is None
