"""Request correlation: generation, sanitization, echo, and actual rendering.

The load-bearing test here is the rendered-log one. Binding a context variable
succeeds whether or not anything is configured to read it, so a test that
asserts "bind was called" passes against a build where every log line silently
omits the id — which is the failure this whole mechanism exists to avoid.
"""

from __future__ import annotations

import json
import logging

import pytest

from contextplane.api.middleware.request_id import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    sanitize_request_id,
)
from contextplane.config import Settings
from contextplane.logging_config import configure_logging

_JSON_SETTINGS = Settings(
    database_url="postgresql://x/y",
    pgbouncer_url="postgresql://x/y",
    scheduler_jobstore_url="postgresql://x/y",
    log_format="json",
    log_level=logging.INFO,
)


@pytest.fixture(autouse=True)
def _restore_root_handlers():
    # configure_logging clears root handlers, which would disarm the next test's
    # capsys capture.
    original = logging.root.handlers[:]
    original_level = logging.root.level
    yield
    logging.root.handlers[:] = original
    logging.root.setLevel(original_level)


class _Sender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.sent.append(message)

    def header(self, name: str) -> str | None:
        for message in self.sent:
            if message.get("type") == "http.response.start":
                for key, value in message.get("headers", []):
                    if key.lower() == name.encode():
                        return value.decode()
        return None


def _scope(*headers: tuple[bytes, bytes]) -> dict:
    return {"type": "http", "path": "/v1/thing", "method": "GET", "headers": list(headers)}


async def _receive() -> dict:  # pragma: no cover - never consumed
    return {"type": "http.request", "body": b"", "more_body": False}


def _responder(status: int = 200, extra_headers: list | None = None):
    async def app(scope: dict, receive: object, send: object) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": extra_headers or [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    return app


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        'abc"} {"level":"critical","event":"forged',  # JSON escape into a new field
        "abc\ndef",  # a second log line
        "abc\r\ndef",  # header splitting
        "abc\x00def",
        "abc\x1b[31m",  # ANSI, for whoever is tailing this in a terminal
        "abc def",
        "id/../../etc",
        "x" * 129,
    ],
)
def test_a_hostile_inbound_id_is_replaced_not_salvaged(hostile: str) -> None:
    # Replaced wholesale rather than stripped: keeping the surviving prefix of a
    # hostile value would leave an attacker-chosen string in every log line the
    # request emits.
    cleaned = sanitize_request_id(hostile)
    assert cleaned != hostile
    assert len(cleaned) == 36  # a fresh uuid4


@pytest.mark.parametrize("ok", ["req-123", "A.b_c-1", "x" * 128, "00000000-0000-4000-8000-0"])
def test_a_well_formed_inbound_id_is_preserved(ok: str) -> None:
    # The whole point of accepting an inbound id: a caller that already has a
    # trace id can make the two systems' logs join.
    assert sanitize_request_id(ok) == ok


def test_an_absent_id_is_generated() -> None:
    first = sanitize_request_id(None)
    assert sanitize_request_id("") != ""
    assert sanitize_request_id(None) != first  # not a constant


# ---------------------------------------------------------------------------
# Echo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_id_is_echoed_on_a_success() -> None:
    sender = _Sender()
    await RequestIdMiddleware(_responder())(_scope((b"x-request-id", b"req-abc")), _receive, sender)
    assert sender.header(REQUEST_ID_HEADER) == "req-abc"


@pytest.mark.asyncio
async def test_the_id_is_echoed_on_an_error_response() -> None:
    # The correlation case that matters most. A 4xx is where someone starts
    # looking, and it is emitted by middleware that never reaches a handler.
    sender = _Sender()
    await RequestIdMiddleware(_responder(429))(_scope(), _receive, sender)
    echoed = sender.header(REQUEST_ID_HEADER)
    assert echoed and len(echoed) == 36


@pytest.mark.asyncio
async def test_a_downstream_id_header_is_replaced_not_duplicated() -> None:
    sender = _Sender()
    await RequestIdMiddleware(_responder(200, [(b"x-request-id", b"downstream")]))(
        _scope((b"x-request-id", b"inbound")), _receive, sender
    )
    start = next(m for m in sender.sent if m["type"] == "http.response.start")
    ids = [v for k, v in start["headers"] if k.lower() == b"x-request-id"]
    # Two headers with different values leaves it unspecified which one a client
    # reads, and the two systems then disagree about the same request.
    assert ids == [b"inbound"]


@pytest.mark.asyncio
async def test_a_non_http_scope_passes_through() -> None:
    seen: list[str] = []

    async def lifespan(scope: dict, receive: object, send: object) -> None:
        seen.append(scope["type"])

    await RequestIdMiddleware(lifespan)({"type": "lifespan"}, _receive, _Sender())
    assert seen == ["lifespan"]


# ---------------------------------------------------------------------------
# Rendering — the half that proves the processor-chain edit landed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rendered_log_line_carries_the_id(capsys) -> None:
    """Asserted against real stdout, not against a mock of the bind call.

    `merge_contextvars` is what copies the bound variable into the event dict.
    Omit it and every assertion about binding still passes while no log line
    ever shows an id.
    """
    configure_logging(_JSON_SETTINGS)

    async def app(scope: dict, receive: object, send: object) -> None:
        logging.getLogger("test.request_id").info("handled")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await RequestIdMiddleware(app)(_scope((b"x-request-id", b"req-render")), _receive, _Sender())

    line = next(x for x in capsys.readouterr().out.splitlines() if x.strip())
    assert json.loads(line)["request_id"] == "req-render"


@pytest.mark.asyncio
async def test_the_id_does_not_leak_into_the_next_request(capsys) -> None:
    """ASGI tasks are reused, and a bound context variable outlives the request.

    Without an unbind, a request that sets no id of its own inherits whichever
    one the task handled previously — so two unrelated requests share an id and
    the correlation is worse than useless: it is confidently wrong.
    """
    configure_logging(_JSON_SETTINGS)

    async def app(scope: dict, receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await RequestIdMiddleware(app)(_scope((b"x-request-id", b"req-first")), _receive, _Sender())
    capsys.readouterr()

    logging.getLogger("test.after").info("outside any request")
    line = next(x for x in capsys.readouterr().out.splitlines() if x.strip())
    assert "request_id" not in json.loads(line)
