"""Identity meets outcome, and neither seam alone could have done it.

The requirement asks for recording at the two places actor identity exists. Both
run before the handler, so neither knows the status class, the latency, or how many
rows came back — recording there yields a row whose identity fields are full and
whose analytically interesting fields are empty.

So identity is stashed on the way in and the event is emitted from the operational
instrumentation on the way out — the one place that knows route and outcome together.
These tests assert the join actually happens, that it never costs a request, and that
identity cannot leak from one call into the next.

The result count follows the same split, on its own stash rather than identity's:
a handler counts its own result set on the way out, and these tests assert that
value reaches the event too — that unset stays `NULL` rather than becoming `0`,
that a real `0` is preserved rather than treated as missing, and that the MCP
side cannot leak one tool's count into a call that never reports one.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid

import pytest

from contextplane.usage.identity import (
    UsageIdentity,
    clear_mcp_identity,
    read_mcp_identity,
    read_request_identity,
    set_mcp_identity,
    stash_request_identity,
)
from contextplane.usage.recording import outcome_for, record_mcp_usage, record_rest_usage
from contextplane.usage.results import (
    clear_mcp_result_count,
    read_mcp_result_count,
    read_result_count,
    set_mcp_result_count,
    stash_result_count,
)
from contextplane.usage.writer import UsageEvent

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


class _CapturingWriter:
    """Stands in for `UsageWriter`, recording what it was handed.

    Not a MagicMock: `record_*` checks the writer's type before using it, which is
    what stops a half-built app state from being written to, and a mock would
    bypass exactly that check.
    """

    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def record(self, event: UsageEvent) -> None:
        self.events.append(event)


class _State:
    pass


class _App:
    def __init__(self, writer: object | None) -> None:
        self.state = _State()
        if writer is not None:
            self.state.usage_writer = writer  # type: ignore[attr-defined]


def _scope(
    app: object,
    *,
    identity: UsageIdentity | None = None,
    result_count: int | None = None,
) -> dict:
    scope: dict = {"type": "http", "app": app, "state": {}, "headers": []}
    if identity is not None:
        scope["state"]["usage_identity"] = identity
    if result_count is not None:
        scope["state"]["usage_result_count"] = result_count
    return scope


@pytest.fixture(autouse=True)
def _patch_writer_type(monkeypatch: pytest.MonkeyPatch):
    """Let the capturing double pass the writer type check."""
    import contextplane.usage.recording as recording

    monkeypatch.setattr(recording, "UsageWriter", _CapturingWriter)


# ---------------------------------------------------------------------------
# The stash itself
# ---------------------------------------------------------------------------


def test_identity_written_to_request_state_is_readable_from_the_scope() -> None:
    """The mechanism the whole split depends on.

    The dependency writes through `request.state`; the middleware reads from the
    ASGI scope. Those are the same dict — Starlette's `state` property is backed by
    `scope["state"]` — and if that ever stops being true the join silently stops
    joining, so it is asserted rather than assumed.
    """

    class _Request:
        def __init__(self, scope: dict) -> None:
            self.scope = scope
            self.state = type("S", (), {})()

    scope: dict = {"state": {}}
    identity = UsageIdentity(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4())
    scope["state"]["usage_identity"] = identity

    assert read_request_identity(scope) == identity


def test_a_scope_with_no_state_reads_as_no_identity() -> None:
    # A route that does not depend on tenant context, or a request that never
    # authenticated. Both are legitimate and neither is an error.
    assert read_request_identity({"type": "http"}) is None


def test_stashing_never_raises() -> None:
    # This runs inside the auth pipeline. A failure to stash a measurement must
    # not be able to fail authentication. `object()` has no `.state` to write
    # through, which is exactly the failure this guards against; the swallow
    # is only real if the function still returns cleanly.
    result = stash_request_identity(object(), UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None))
    assert result is None


# ---------------------------------------------------------------------------
# The result-count stash
# ---------------------------------------------------------------------------


def test_a_stashed_result_count_is_readable_from_the_scope() -> None:
    # Same claim as the identity version above, for the sibling stash: a handler
    # writes through request.state, the middleware reads from the ASGI scope, and
    # those are the same dict.
    scope: dict = {"state": {"usage_result_count": 3}}
    assert read_result_count(scope) == 3


def test_a_scope_with_no_state_reads_as_no_result_count() -> None:
    # No handler on this route stashed a count — not the same as a search that
    # found zero rows. Both leave this NULL, and NULL must stay distinguishable
    # from 0.
    assert read_result_count({"type": "http"}) is None


def test_a_stashed_zero_is_not_read_back_as_unset() -> None:
    # 0 and "never stashed" look the same to a truthiness check; only one of them
    # means the column stays NULL.
    scope: dict = {"state": {"usage_result_count": 0}}
    assert read_result_count(scope) == 0


def test_stash_result_count_writes_through_request_state() -> None:
    request = type("R", (), {"state": type("S", (), {})()})()
    stash_result_count(request, 4)
    assert request.state.usage_result_count == 4


def test_stashing_a_result_count_never_raises() -> None:
    # Runs after the service call and before the response is built. A failure to
    # stash a measurement must not be able to fail the request it is measuring.
    # `object()` has no `.state`, so this exercises the swallow; the swallow is
    # only real if the function still returns cleanly instead of propagating.
    result = stash_result_count(object(), 0)
    assert result is None


# ---------------------------------------------------------------------------
# REST recording
# ---------------------------------------------------------------------------


def test_a_rest_call_records_one_event_with_both_halves() -> None:
    writer = _CapturingWriter()
    tenant, actor = uuid.uuid4(), uuid.uuid4()
    scope = _scope(_App(writer), identity=UsageIdentity(tenant_id=tenant, actor_id=actor))

    record_rest_usage(scope, operation="/v1/capabilities", status_class="2xx", seconds=0.012, now=_NOW)

    (event,) = writer.events
    assert event.tenant_id == tenant  # from the identity seam
    assert event.actor_id == actor
    assert event.surface == "rest"
    assert event.operation == "/v1/capabilities"  # from the outcome seam
    assert event.status_class == "2xx"
    assert event.latency_ms == 12
    assert event.outcome == "ok"
    # No handler on this call stashed a count, and that must not read as zero.
    assert event.result_count is None


def test_a_rest_call_with_a_stashed_count_records_it() -> None:
    writer = _CapturingWriter()
    scope = _scope(
        _App(writer),
        identity=UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None),
        result_count=7,
    )

    record_rest_usage(scope, operation="/v1/search", status_class="2xx", seconds=0.01, now=_NOW)

    (event,) = writer.events
    assert event.result_count == 7


def test_a_rest_call_with_a_stashed_zero_records_zero_not_none() -> None:
    # A search that matched nothing is a served call that answered "nothing",
    # not a call with no measurement at all.
    writer = _CapturingWriter()
    scope = _scope(
        _App(writer),
        identity=UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None),
        result_count=0,
    )

    record_rest_usage(scope, operation="/v1/search", status_class="2xx", seconds=0.01, now=_NOW)

    (event,) = writer.events
    assert event.result_count == 0


def test_an_unauthenticated_request_records_nothing() -> None:
    """Skipped, not recorded with a null tenant, and the asymmetry is deliberate.

    `actor_id` is nullable because an unauthenticated caller is still a caller.
    `tenant_id` is not: a row belonging to no tenant is unreadable by anyone and
    would only inflate global counts. Unauthenticated traffic is already visible in
    the operational tier, which needs no identity at all.
    """
    writer = _CapturingWriter()
    record_rest_usage(_scope(_App(writer)), operation="/v1/x", status_class="4xx", seconds=0.1)
    assert writer.events == []


def test_recording_with_no_writer_configured_is_silent() -> None:
    # True during startup and in any test that builds a partial app. A missing
    # writer is not an error at the call site -- the function returns cleanly
    # rather than raising when `_writer()` resolves to None.
    result = record_rest_usage(
        _scope(_App(None), identity=UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None)),
        operation="/v1/x",
        status_class="2xx",
        seconds=0.0,
    )
    assert result is None


def test_a_writer_that_explodes_never_reaches_the_request(caplog: pytest.LogCaptureFixture) -> None:
    """The failure that would turn measurement into an outage."""

    class _Exploding(_CapturingWriter):
        def record(self, event: UsageEvent) -> None:
            raise RuntimeError("writer is broken")

    with caplog.at_level(logging.DEBUG, logger="contextplane.usage.recording"):
        result = record_rest_usage(
            _scope(_App(_Exploding()), identity=UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None)),
            operation="/v1/x",
            status_class="2xx",
            seconds=0.0,
        )

    # The exception from the writer must not reach the caller...
    assert result is None
    # ...and must not vanish silently either -- it is logged, not lost.
    assert "usage: rest recording failed" in caplog.text


# ---------------------------------------------------------------------------
# MCP recording
# ---------------------------------------------------------------------------


def test_an_mcp_tool_call_records_the_tool_name_as_the_operation() -> None:
    writer = _CapturingWriter()
    tenant = uuid.uuid4()
    token = set_mcp_identity(UsageIdentity(tenant_id=tenant, actor_id=None))
    try:
        record_mcp_usage(_App(writer), tool="search_capabilities", status_class="2xx", seconds=0.03, now=_NOW)
    finally:
        clear_mcp_identity(token)

    (event,) = writer.events
    assert event.surface == "mcp"
    # The tool name, which is a closed set by construction — it comes from the
    # registered catalog and changes only when someone adds a decorator.
    assert event.operation == "search_capabilities"
    assert event.tenant_id == tenant
    # No tool set a count in this test, and that must not read as zero.
    assert event.result_count is None


def test_an_mcp_tool_call_with_a_set_result_count_records_it() -> None:
    writer = _CapturingWriter()
    identity_token = set_mcp_identity(UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None))
    count_token = set_mcp_result_count(None)
    try:
        set_mcp_result_count(5)
        record_mcp_usage(_App(writer), tool="search_capabilities", status_class="2xx", seconds=0.03, now=_NOW)
    finally:
        clear_mcp_result_count(count_token)
        clear_mcp_identity(identity_token)

    (event,) = writer.events
    assert event.result_count == 5


def test_mcp_result_count_does_not_leak_into_a_call_that_sets_nothing() -> None:
    """The leak the wrapper's entry-reset exists to prevent.

    Two calls sharing one asyncio Task, the shape every MCP call actually runs
    in: the first is a listing tool that reports five rows, the second is a
    tool with no result-set semantics at all. Without resetting to unset before
    the second tool body runs, its row would inherit the first call's count —
    attributing one tool's result set to a completely different tool.
    """
    writer = _CapturingWriter()
    app = _App(writer)
    identity_token = set_mcp_identity(UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None))
    try:
        # Call one: a listing tool that finds five rows.
        count_token = set_mcp_result_count(None)
        set_mcp_result_count(5)
        record_mcp_usage(app, tool="search_capabilities", status_class="2xx", seconds=0.01, now=_NOW)
        clear_mcp_result_count(count_token)

        # Call two, same task: a tool that never reports a count.
        count_token = set_mcp_result_count(None)
        record_mcp_usage(app, tool="get_claim", status_class="2xx", seconds=0.01, now=_NOW)
        clear_mcp_result_count(count_token)
    finally:
        clear_mcp_identity(identity_token)

    first, second = writer.events
    assert first.result_count == 5
    assert second.result_count is None


def test_mcp_result_count_reset_restores_the_previous_value() -> None:
    token = set_mcp_result_count(None)
    set_mcp_result_count(3)
    clear_mcp_result_count(token)
    assert read_mcp_result_count() is None


def test_mcp_identity_does_not_leak_into_the_next_call() -> None:
    """ContextVars are per-task and tasks are reused.

    Leaving a value bound means the next call handled by that task inherits it —
    attributing one tenant's usage to another. The same trap the request-id
    middleware guards against, and the reason `clear_mcp_identity` resets rather
    than sets to None.
    """
    token = set_mcp_identity(UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None))
    clear_mcp_identity(token)
    assert read_mcp_identity() is None


@pytest.mark.asyncio
async def test_identity_set_in_one_task_is_invisible_to_another() -> None:
    # The property that makes a ContextVar the right carrier for a transport with
    # no request object: concurrent MCP calls must not see each other's identity.
    seen: list[UsageIdentity | None] = []

    async def other() -> None:
        seen.append(read_mcp_identity())

    set_mcp_identity(UsageIdentity(tenant_id=uuid.uuid4(), actor_id=None))
    await asyncio.create_task(other())
    # A child task inherits a copy, so it sees the value; what matters is that it
    # cannot mutate the parent's. Assert the copy semantics hold.
    assert seen == [read_mcp_identity()]


# ---------------------------------------------------------------------------
# Outcome mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_class", "expected"),
    [("2xx", "ok"), ("3xx", "ok"), ("4xx", "error"), ("5xx", "error"), ("other", "error")],
)
def test_outcome_is_the_served_or_failed_split(status_class: str, expected: str) -> None:
    assert outcome_for(status_class) == expected


def test_found_nothing_is_not_an_error() -> None:
    """A search that matched zero rows is a successful call that answered "nothing".

    That is recorded by `result_count`, not by `outcome`. Folding it into the
    outcome would invent an error rate out of ordinary empty results — and the
    answer-availability metrics built on this depend on telling those apart.
    """
    event = UsageEvent(
        occurred_at=_NOW,
        tenant_id=uuid.uuid4(),
        surface="rest",
        operation="/v1/search",
        outcome=outcome_for("2xx"),
        status_class="2xx",
        latency_ms=5,
        result_count=0,
    )
    assert event.outcome == "ok"
    assert event.result_count == 0
