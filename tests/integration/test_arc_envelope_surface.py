"""The envelope operating surface, over HTTP.

E23-T5. `AutonomyEnvelopeService` has had `grant`, `suspend`, `reinstate` and
`revoke` since E7, wired into the container and reachable from **no transport**.
The control that decides what an agent may do could be read and not operated: an
incident response consisted of editing rows.

`test_arc_autonomy_envelope.py` covers what the service decides — who may
perform each act, what a suspension frees, why two envelopes cannot cover one
window. None of that is repeated here. What this proves is the part that test
cannot: **that the four acts are reachable at all**, that each reaches the method
it names, and that the transport does not quietly widen what the service allows.

**REST only, and one test says so.** An agent able to reinstate its own envelope
is an agent that can end any suspension imposed on it, which is the failure the
envelope exists to prevent, arranged so that the subject of the control operates
it. The MCP surface must not carry these verbs, and the tool registry is where
that is checked.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    bearer_headers,
    patch_validator_for_actor,
)


@pytest_asyncio.fixture
async def surface(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    slug = f"env-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        caller = harness.add_persona(slug, roles=["admin"])
        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(caller)
            with patch_validator_for_actor(caller):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert whoami.status_code == 200, whoami.text
            yield {"caller": caller, "client": client, "harness": harness, "slug": slug}


def _as(surface: dict[str, Any]) -> Any:
    surface["harness"].configure_fetcher_for(surface["caller"])
    return patch_validator_for_actor(surface["caller"])


async def _post(surface: dict[str, Any], path: str, body: dict[str, Any]) -> httpx.Response:
    with _as(surface):
        return await surface["client"].post(path, headers=bearer_headers(tenant_slug=surface["slug"]), json=body)


async def _get(surface: dict[str, Any], path: str) -> httpx.Response:
    with _as(surface):
        return await surface["client"].get(path, headers=bearer_headers(tenant_slug=surface["slug"]))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/arc/admin/envelopes/bindings"),
        ("POST", "/v1/arc/admin/envelopes/bindings/{binding}/suspend"),
        ("POST", "/v1/arc/admin/envelopes/bindings/{binding}/reinstate"),
        ("POST", "/v1/arc/admin/envelopes/bindings/{binding}/revoke"),
        ("GET", "/v1/arc/admin/envelopes/bindings"),
    ],
)
def test_every_envelope_act_is_mounted(method: str, path: str) -> None:
    """The failure this whole task is about: a service method no route reaches.

    Asserted against the mounted route table rather than by calling, because a
    call would also exercise authorization and a 403 would look like a pass. What
    is being checked here is only that the path exists — which for four of these
    five it did not, for as long as the envelope has existed.
    """
    from contextplane.api.routers import arc_envelopes

    mounted = {(list(route.methods or {})[0], route.path) for route in arc_envelopes.router.routes}  # type: ignore[attr-defined]
    expected = path.replace("{binding}", "{binding_id}")

    assert (method, expected) in mounted, f"{method} {expected} is not mounted; {sorted(mounted)}"


def test_no_mcp_tool_carries_an_envelope_verb() -> None:
    """REST only, and the reason is sharper than for the rest of this router.

    An agent able to reinstate its own envelope can end any suspension imposed on
    it — the failure the envelope exists to prevent, arranged so the subject of
    the control operates it. Checked against the committed registry rather than
    against a docstring, because a docstring is not what an agent connects to.
    """
    import json
    from pathlib import Path

    registry = json.loads(
        (Path(__file__).parents[1].parent / "contextplane/api/mcp/tool_registry.json").read_text(encoding="utf-8")
    )
    offenders = sorted(
        entry["name"]
        for entry in registry["tools"]
        if "envelope" in entry["name"] or "envelope" in (entry.get("rest") or "")
    )

    assert not offenders, f"these MCP tools reach an envelope verb: {offenders}"


@pytest.mark.asyncio
async def test_resolving_an_ungoverned_principal_answers_null_rather_than_suspended(
    surface: dict[str, Any],
) -> None:
    """The distinction the whole control rests on.

    `null` means nobody has governed this principal; a suspended binding means
    somebody chose a posture. Collapsing them is what would let an ungoverned
    agent look controlled, which is the reading that stops an operator acting.
    """
    response = await _get(
        surface,
        "/v1/arc/admin/envelopes/bindings?principal_issuer=https%3A%2F%2Fidp&principal_subject=nobody",
    )

    assert response.status_code == 200, response.text
    assert response.json() is None


@pytest.mark.asyncio
async def test_a_flip_with_no_reason_is_refused_on_the_wire(surface: dict[str, Any]) -> None:
    """A binding switched off with no reason leaves the next reader working out
    why an agent stopped being able to act, during the incident where that
    matters most."""
    response = await _post(surface, f"/v1/arc/admin/envelopes/bindings/{uuid.uuid4()}/suspend", {"reason": ""})

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_grant_with_no_principal_is_refused_on_the_wire(surface: dict[str, Any]) -> None:
    """Both halves of a workload identity are opaque strings, and a transposed
    or empty pair would bind an envelope that resolves for nobody — governance
    theatre rather than governance."""
    response = await _post(
        surface,
        "/v1/arc/admin/envelopes/bindings",
        {
            "principal_issuer": "",
            "principal_subject": "agent-1",
            "reason": "governed",
            "revision_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_suspending_a_binding_that_is_not_there_is_not_a_success(
    surface: dict[str, Any],
) -> None:
    """An operator who suspended nothing and was told it worked would stop
    looking, during an incident, at the agent they were trying to stop."""
    response = await _post(
        surface,
        f"/v1/arc/admin/envelopes/bindings/{uuid.uuid4()}/suspend",
        {"reason": "incident 4412"},
    )

    assert response.status_code >= 400, response.text
    assert response.status_code != 500, f"a missing binding is a caller fault: {response.text}"
