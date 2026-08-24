"""Time to first memory: from what, to what, measured how.

E7-T4. The epic quotes a figure; the entry says a figure needs a definition
before it can be quoted, *"undefined it becomes a marketing figure"*, and asks
for it to be a scripted path a test executes so the claim and its evidence are
the same artifact.

## The definition

**From** an authenticated MCP session on the default surface — the eight core
verbs a connection is handed without asking for anything wider. **To** the first
recall that returns something the agent itself remembered. **Measured** as the
wall clock across exactly those two calls, in-process, against a real database.

## What it deliberately excludes, and why

*The operator's setup.* Cloning, installing, migrating and starting the service
are somebody else's minutes, and `scripts/prove_quickstart.py` already runs and
times that sequence from a genuinely clean clone. Folding it in here would
measure a laptop's Docker pull and call it an agent's experience.

*Authentication.* The clock starts on a session that has already resolved, for
the same reason: token issuance is the deployment's identity provider, and a
number that moved when somebody changed OIDC caching would not be about this
service.

*Retrieval quality.* This says the loop closes, not that what comes back is
good. `recall@10` and the multi-session recall fixtures are the metrics for
that, and conflating them would let a fast, useless loop report well.

So the honest sentence this number supports is: **once an agent is connected,
remembering something and getting it back takes about this long** — and nothing
beyond it.

## What the test asserts, as against what it reports

It asserts the loop *closes*: the event comes back, with its body, in the
session it was written to. That is a correctness property and it fails.

It **reports** the duration rather than asserting a threshold. A first
measurement with no prior distribution is a number, not a bar, and the
extraction ground truth already set that discipline here — *"report first"*. A
threshold invented alongside the first measurement is a threshold chosen to
pass.
"""

from __future__ import annotations

import datetime
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from contextplane.api.mcp.context import _request_app, _request_token, _request_x_tenant_id
from contextplane.api.mcp.server import SURFACE_CORE, core_tool_names, create_contextplane_mcp_server
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)

#: What the agent remembers, and then asks for back.
_MEMORY = "we decided to retry the payments gateway three times, then page the on-call"

type _Loop = tuple[EntitlementAuthHarness, object, TenantPersona]


@pytest_asyncio.fixture
async def loop(pg_container: str) -> AsyncIterator[_Loop]:
    """An authenticated agent on the **default** surface.

    `surface=SURFACE_CORE` rather than the full server every other MCP test
    builds, because the figure is about what an agent is handed without asking
    for anything wider. Measuring the loop on a surface no default connection
    receives would be measuring a different product.
    """
    slug = f"ttfm-{secrets.token_hex(4)}"
    async with EntitlementAuthHarness(pg_container) as harness:
        persona = harness.add_persona(slug, roles=["producer"], actor_id=uuid.uuid4())
        harness.configure_fetcher_for(persona)
        mcp = create_contextplane_mcp_server(
            retrieval=harness.app.state.retrieval,
            catalog=harness.app.state.catalog,
            session_factory=harness.app.state.session_factory,
            workspace_service=harness.app.state.workspace_service,
            clock=FakeClock(_NOW),
            surface=SURFACE_CORE,
        )
        # The session has to be resolved before the clock starts; see the module
        # docstring on why authentication is outside the measurement.
        transport = ASGITransport(app=harness.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch_validator_for_actor(persona):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
            assert whoami.status_code == 200, whoami.text
        yield harness, mcp, persona


async def _call(loop: _Loop, tool: str, args: dict[str, object]) -> object:
    harness, mcp, persona = loop
    harness.configure_fetcher_for(persona)
    cv_token = _request_token.set("harness.dummy.jwt")
    cv_app = _request_app.set(harness.app)
    cv_tenant = _request_x_tenant_id.set(persona.slug)
    try:
        with patch_validator_for_actor(persona):
            return await mcp.call_tool(tool, args)  # type: ignore[attr-defined]
    finally:
        _request_x_tenant_id.reset(cv_tenant)
        _request_app.reset(cv_app)
        _request_token.reset(cv_token)


def _texts(result: object) -> list[str]:
    """FastMCP's `(content_blocks, meta)` pair, flattened to the text it carries."""
    blocks = result[0] if isinstance(result, tuple) else result
    return [getattr(block, "text", "") for block in blocks]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_the_two_call_loop_closes_and_how_long_it_takes(loop: _Loop) -> None:
    """Remember, then recall. The assertion is that it closed; the print is the figure."""
    session_id = f"ttfm-{secrets.token_hex(4)}"

    started = time.perf_counter()
    await _call(
        loop,
        "record_session_event",
        {"session_id": session_id, "kind": "user_message", "body": _MEMORY},
    )
    recalled = await _call(loop, "list_session_events", {"session_id": session_id})
    elapsed_ms = (time.perf_counter() - started) * 1000

    bodies = " ".join(_texts(recalled))
    assert _MEMORY in bodies, (
        "the loop did not close: the event the agent recorded was not returned by the recall "
        "in the same session. A duration measured over a loop that does not close is not a "
        "time to first memory."
    )

    # Reported, not asserted. See the module docstring on why a threshold
    # invented alongside a first measurement is a threshold chosen to pass.
    print(f"\ntime-to-first-memory: {elapsed_ms:.0f} ms (record_session_event + list_session_events)")


@pytest.mark.asyncio
async def test_the_loop_uses_only_verbs_a_default_connection_is_handed(loop: _Loop) -> None:
    """The figure is only meaningful if the path is the default one.

    If either verb were extended, the number would describe a loop an agent has
    to negotiate an envelope to run — and quoting it as the time to first memory
    would be quoting the wrong thing.
    """
    assert {"record_session_event", "list_session_events"} <= core_tool_names()


@pytest.mark.asyncio
async def test_the_safe_path_is_the_one_the_default_surface_offers(loop: _Loop) -> None:
    """E7-T4's other half: nothing makes the safe path the default an agent gets
    without asking for it.

    It does now, and by subtraction rather than by routing: the default surface
    carries exactly one write, and that write goes through admission and the
    autonomy envelope. There is no faster, unscanned way to record something
    because there is no other way at all.
    """
    writes = {name for name in core_tool_names() if name.startswith(("record_", "assert_", "ingest_"))}

    assert writes == {"record_session_event"}, (
        f"the default surface now offers {sorted(writes)}. Every write an agent reaches without "
        "asking for a wider surface has to carry the same floors, and each new one is a "
        "decision about that."
    )


@pytest.mark.asyncio
async def test_the_recall_returns_the_agents_own_memory_and_not_another_sessions(
    loop: _Loop,
) -> None:
    """The loop closing on the *wrong* memory would still produce a duration.

    A session carries no visibility setting and no sharing mode, so the
    credential is the only thing scoping it; this asserts the recall is scoped to
    the session the agent wrote to rather than returning whatever is nearest.
    """
    mine, theirs = f"ttfm-{secrets.token_hex(4)}", f"ttfm-{secrets.token_hex(4)}"
    await _call(loop, "record_session_event", {"session_id": mine, "kind": "user_message", "body": _MEMORY})
    await _call(
        loop,
        "record_session_event",
        {"session_id": theirs, "kind": "user_message", "body": "something else entirely"},
    )

    recalled = " ".join(_texts(await _call(loop, "list_session_events", {"session_id": mine})))

    assert _MEMORY in recalled
    assert "something else entirely" not in recalled


@pytest.mark.asyncio
async def test_the_recorded_event_carries_a_sequence_the_agent_can_resume_from(
    loop: _Loop,
) -> None:
    """A memory an agent cannot order is a memory it cannot resume from, which
    is the whole point of the loop being two calls rather than one."""
    session_id = f"ttfm-{secrets.token_hex(4)}"

    written = _texts(
        await _call(
            loop,
            "record_session_event",
            {"session_id": session_id, "kind": "user_message", "body": _MEMORY},
        )
    )

    assert written, "record_session_event returned nothing an agent could read"
    assert "seq" in json.loads(written[0])
