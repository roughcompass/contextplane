"""The pilot's own recorded situations, replayed against the delivered surfaces.

Each lifecycle mechanism already has a module that proves it with data built to
prove it. This one is different on purpose: it drives the **frozen pilot
corpus** — six changes that actually ran — back through the shipped surfaces and
asks whether what the pilot recorded still happens.

That distinction is the whole value of an exit gate. A purpose-built test proves
a mechanism can work. It cannot notice that the situations the mechanism was
built for have stopped resolving the way they did, because it never contained
one. The corpus does, so a regression that only shows up on the shapes real work
takes has somewhere to fail.

**The scenarios drive the assertions rather than decorating them.** A scenario
recording `asserted` on the claims block is seeded with a confirmed claim and
must come back `asserted`; one recording `observed` is seeded unconfirmed and
must come back `observed`. Flatten the trust mapping in the serving path and the
corpus splits — which is the property a corpus of six identical happy paths
could never have.

**What this module deliberately does not re-derive.** The canonical and ARC
blocks' coverage is recorded in the corpus but not reproduced here: standing up
an attested resolution per scenario would prove the attestation path, which owns
its own gate, and would say nothing further about the lifecycle surfaces. The
blocks this drives are the two the profile actually narrows and the two whose
trust labels the pilot's participants read. That boundary is stated here rather
than left for a reader to infer from which assertions exist.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.api.mcp import context as mcp_context
from contextplane.context.schemas.envelope import BLOCK_OBSERVED_CLAIMS
from contextplane.context.schemas.trust import TRUST_ASSERTED
from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    patch_validator_for_actor,
)

_CORPUS = pathlib.Path(__file__).parent.parent / "fixtures" / "lifecycle_context_pilot"

_SEED_MOMENT = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

#: The stage a scenario is asked about when it needs one it does not cover.
#: Chosen because every scenario in the corpus covers implementation, so this is
#: reliably a point none of them is at.
_ELSEWHERE = "release_freeze"

#: The authority tiers that make a served claim count as human-confirmed, and
#: therefore `asserted` rather than `observed`. Named here so the seeding below
#: is choosing a documented input rather than reaching for a value that happens
#: to work.
_HUMAN_AUTHORITY = "observer_human"
_EXTRACTION_AUTHORITY = "observer_extraction"

type _Surface = dict[str, Any]


def _scenarios() -> list[tuple[str, dict[str, Any]]]:
    """The corpus, refusing to run this gate against a corpus that is not there.

    An exit gate that silently covered zero scenarios would be the most
    misleading green in the suite.
    """
    paths = sorted(_CORPUS.glob("*.json"))
    if len(paths) < 5:
        raise AssertionError(
            f"the exit gate found {len(paths)} pilot scenario(s) at {_CORPUS} and needs at least 5; "
            "an exit gate covering nothing passes"
        )
    return [(path.stem, json.loads(path.read_text(encoding="utf-8"))) for path in paths]


_SCENARIOS = _scenarios()
_IDS = [name for name, _ in _SCENARIOS]


@pytest_asyncio.fixture
async def surface(pg_container: str) -> AsyncIterator[_Surface]:
    """One pilot tenant, one tenant that was never in the pilot, both real."""
    slug = f"exit-{uuid.uuid4().hex[:8]}"
    outsider_slug = f"nonpilot-{uuid.uuid4().hex[:8]}"
    async with EntitlementAuthHarness(pg_container) as harness:
        pilot = harness.add_persona(slug, roles=["producer", "consumer"])
        outsider = harness.add_persona(outsider_slug, roles=["producer", "consumer"])

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(pilot)
            with patch_validator_for_actor(pilot):
                resp = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert resp.status_code == 200, resp.text
                tenant_id = uuid.UUID(resp.json()["tenant_id"])
                actor_id = str(resp.json()["actor_id"])

            # The non-pilot tenant has to exist before anyone can authenticate as
            # it; a denial against an identity that was never created would prove
            # only that the tenant is unknown.
            harness.configure_fetcher_for(outsider)
            with patch_validator_for_actor(outsider):
                other = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=outsider_slug))
                assert other.status_code == 200, other.text

            yield {
                "client": client,
                "harness": harness,
                "pilot": pilot,
                "outsider": outsider,
                "slug": slug,
                "outsider_slug": outsider_slug,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "pg_url": pg_container,
            }


def _as(surface: _Surface, persona: TenantPersona) -> Any:
    surface["harness"].configure_fetcher_for(persona)
    return patch_validator_for_actor(persona)


@contextlib.contextmanager
def _mcp_request(surface: _Surface, persona: TenantPersona, slug: str) -> Any:
    """Populate the ContextVars an MCP tool reads, the way the SSE handler does."""
    surface["harness"].configure_fetcher_for(persona)
    tokens = [
        mcp_context._request_token.set("harness.dummy.jwt"),
        mcp_context._request_app.set(surface["harness"].app),
        mcp_context._request_x_tenant_id.set(slug),
    ]
    try:
        with patch_validator_for_actor(persona):
            yield
    finally:
        for var, token in zip(
            (mcp_context._request_token, mcp_context._request_app, mcp_context._request_x_tenant_id),
            tokens,
            strict=True,
        ):
            var.reset(token)


def _lifecycle_reference(kind: str, external_id: str) -> dict[str, Any]:
    return {
        "source_system": "control-plane",
        "source_namespace": "acme",
        "kind": kind,
        "external_id": external_id,
        "classification": "internal",
        "external_authority": "acme/delivery",
    }


async def _resolve(surface: _Surface, **body: Any) -> httpx.Response:
    payload: dict[str, Any] = {"query": "what do I need to know about this change"}
    payload.update(body)
    with _as(surface, surface["pilot"]):
        return await surface["client"].post(
            "/v1/context/resolve",
            headers=bearer_headers(tenant_slug=surface["slug"]),
            json=payload,
        )


async def _seed_placed_claim(
    pg_url: str,
    *,
    tenant_id: uuid.UUID,
    actor_id: str,
    stage: str,
    predicate: str,
    authority: str,
) -> uuid.UUID:
    """One servable claim recorded as applying at `stage`.

    `authority` decides whether the served claim reads as human-confirmed, which
    is what the trust label turns on. It is a parameter rather than a constant
    because the corpus records both labels and a seed that could only produce
    one of them would make half the corpus unfalsifiable.
    """
    claim_id, entity_id = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO entities (entity_id, tenant_id, entity_type, name, is_active) "
                    "VALUES (:e, :t, 'capability', :n, TRUE)"
                ),
                {"e": entity_id, "t": tenant_id, "n": f"cap-{entity_id.hex[:8]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claims ("
                    "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
                    "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
                    "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
                    "  consolidated_at, created_at, confidence, confidence_scored_at, confidence_inputs,"
                    "  scorer_version, calibration_version, decay_half_life_days"
                    ") VALUES ("
                    "  :cid, :t, :t, :a, :e, 'subject-ref', :pred, 'prose',"
                    "  'operational_lifecycle', CAST(:val AS JSONB), :now, 'staged', 'private',"
                    "  :auth, 9, :now, :now, 0.700, :now, CAST(:inputs AS JSONB),"
                    "  'scorer.v1', 'calib.v1', 30"
                    ")"
                ),
                {
                    "cid": claim_id,
                    "t": tenant_id,
                    "a": actor_id,
                    "e": entity_id,
                    "pred": predicate,
                    "val": json.dumps(f"learned during {stage}"),
                    "now": _SEED_MOMENT,
                    "auth": authority,
                    "inputs": json.dumps({"seed": True}),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref) "
                    "VALUES (:cid, 'connector_run', :ref)"
                ),
                {"cid": claim_id, "ref": f"seed:{claim_id}"},
            )
            await session.execute(
                text(
                    "INSERT INTO claim_derivations ("
                    "  derivation_id, tenant_id, profile, profile_version, status, applicability,"
                    "  assertion_digest, source_authority, classification, created_claim_id, created_at"
                    ") VALUES (:d, :t, 'observer_extraction', 'v1', 'staged', :app,"
                    "  :digest, 'observer_extraction', 'internal', :cid, :now)"
                ),
                {
                    "d": uuid.uuid4(),
                    "t": tenant_id,
                    "app": json.dumps({"stage": stage}, separators=(",", ":"), sort_keys=True),
                    "digest": uuid.uuid4().hex,
                    "cid": claim_id,
                    "now": _SEED_MOMENT,
                },
            )
    finally:
        await engine.dispose()
    return claim_id


def _claims_block(body: dict[str, Any]) -> dict[str, Any]:
    return next(block for block in body["blocks"] if block["name"] == BLOCK_OBSERVED_CLAIMS)


def _authority_for(scenario: dict[str, Any]) -> str:
    """The authority tier that produces the trust label this scenario recorded."""
    recorded = scenario["trust_labels"][BLOCK_OBSERVED_CLAIMS]
    return _HUMAN_AUTHORITY if recorded == TRUST_ASSERTED else _EXTRACTION_AUTHORITY


# --- The surfaces the pilot ran on are still mounted --------------------------


@pytest.mark.asyncio
async def test_the_lifecycle_surfaces_are_reachable_on_both_transports(surface: _Surface) -> None:
    """A release that dropped one of these routers would end the pilot silently.

    Both transports, because a tool nobody registers is as unreachable as a
    router nobody mounts and neither failure looks wrong from inside its own
    module.
    """
    from contextplane.api.mcp.server import create_contextplane_mcp_server

    resolved = await _resolve(surface)
    assert resolved.status_code == 200, resolved.text

    with _as(surface, surface["pilot"]):
        resumed = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [["control-plane", "acme", "run", "never-existed"]]},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )
    assert resumed.status_code == 200, resumed.text

    app = surface["harness"].app
    server = create_contextplane_mcp_server(
        retrieval=app.state.retrieval,
        catalog=app.state.catalog,
        session_factory=app.state.session_factory,
        clock=app.state.clock,
        workspace_service=app.state.workspace_service,
    )
    names = {tool.name for tool in await server.list_tools()}
    assert {"registry_resolve_context", "resume_context", "find_receipts_by_reference"} <= names


# --- Every frozen scenario still resolves the way it was recorded -------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
async def test_a_scenario_narrows_to_the_points_it_covered(
    surface: _Surface, name: str, scenario: dict[str, Any]
) -> None:
    """Context placed where the change was is served; context placed elsewhere is withheld and said so.

    The withholding half is the one worth having. A caller who receives a
    shorter list has to be able to tell "narrowed" from "there was nothing",
    and those are the same response body if the withheld item is dropped
    quietly.
    """
    authority = _authority_for(scenario)
    stage = scenario["stages_covered"][0]

    here = await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage=stage,
        predicate=f"{name}_here",
        authority=authority,
    )
    await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage=_ELSEWHERE,
        predicate=f"{name}_elsewhere",
        authority=authority,
    )

    resp = await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", stage)])
    assert resp.status_code == 200, resp.text

    block = _claims_block(resp.json())
    served = [item["receipt_item_id"]["item_key"] for item in block["items"]]
    assert served == [str(here)], f"{name} served {served} for stage {stage!r}"

    # Withheld, and said so. A shorter list with a `success` state would be
    # indistinguishable from a stage that simply had less recorded against it.
    assert block["state"] == "degraded", f"{name} narrowed silently, reporting state {block['state']!r}"
    assert "withheld" in (block["reason"] or ""), f"{name} degraded with reason {block['reason']!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
async def test_a_scenario_reaches_the_coverage_it_recorded(
    surface: _Surface, name: str, scenario: dict[str, Any]
) -> None:
    """The block state the corpus recorded is the block state the surface returns.

    Nothing is placed elsewhere here, which is the difference from the narrowing
    test above: this is the scenario as the pilot ran it, and the recorded
    coverage is only evidence if it is what the surface actually produces.
    """
    expected = scenario["expected_source_coverage"][BLOCK_OBSERVED_CLAIMS]
    stage = scenario["stages_covered"][0]

    await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage=stage,
        predicate=f"{name}_coverage",
        authority=_authority_for(scenario),
    )

    resp = await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", stage)])
    assert resp.status_code == 200, resp.text

    block = _claims_block(resp.json())
    assert (
        block["state"] == expected
    ), f"{name} recorded the claims block as {expected!r} and the surface returned {block['state']!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
async def test_a_scenario_carries_the_trust_label_it_recorded(
    surface: _Surface, name: str, scenario: dict[str, Any]
) -> None:
    """The label the pilot's participants read comes back unchanged.

    Seeded from the corpus and asserted against the corpus, and the two are not
    the same statement: the seed chooses an authority tier, the serving path
    decides a label, and this fails if that path stops distinguishing a claim
    somebody stood behind from one the system merely noticed.
    """
    expected = scenario["trust_labels"][BLOCK_OBSERVED_CLAIMS]
    stage = scenario["stages_covered"][0]

    await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage=stage,
        predicate=f"{name}_trust",
        authority=_authority_for(scenario),
    )

    resp = await _resolve(surface, lifecycle_references=[_lifecycle_reference("stage", stage)])
    assert resp.status_code == 200, resp.text

    block = _claims_block(resp.json())
    # State before count, the same order `test_context_resolve_surfaces.py`
    # settled on and for the same reason. `/v1/context/resolve` runs its arms
    # under a 2s per-arm timeout and a timeout *degrades* the block rather than
    # failing the response -- so an arm that never answered and an arm that
    # answered wrongly both arrive as an empty `items`, and "served no claim"
    # says nothing about which.
    #
    # This has now fired in CI under eight parallel workers on three separate
    # pull requests, each time against a change that could not have caused it.
    # Asserting the state first makes the next occurrence name itself as load.
    # Still a failure and not a skip: an arm that systematically timed out would
    # otherwise pass unnoticed, which is the vacuous-gate failure this suite is
    # careful about elsewhere.
    assert block["state"] != "degraded", (
        f"{name}: the claims arm did not answer ({block['reason']}), so this scenario could not "
        "run. That is an infrastructure signal -- the arm exceeded its 2s budget -- not a "
        "serving regression."
    )
    items = block["items"]
    assert items, f"{name} served no claim, so its trust label proves nothing"
    labels = {item["trust"]["trust"] for item in items}
    assert labels == {expected}, f"{name} recorded {expected!r} and the surface returned {sorted(labels)}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
async def test_a_scenario_is_invisible_to_a_tenant_that_was_never_in_the_pilot(
    surface: _Surface, name: str, scenario: dict[str, Any]
) -> None:
    """Every scenario, not one representative of them.

    Isolation is checked per scenario because the thing that leaks is a row, and
    a scenario differs from its neighbours in exactly the placement and
    authority values a visibility predicate reads.
    """
    stage = scenario["stages_covered"][0]
    await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage=stage,
        predicate=f"{name}_isolation",
        authority=_authority_for(scenario),
    )

    with _as(surface, surface["outsider"]):
        resp = await surface["client"].post(
            "/v1/context/resolve",
            headers=bearer_headers(tenant_slug=surface["outsider_slug"]),
            json={
                "query": "what do I need to know about this change",
                "lifecycle_references": [_lifecycle_reference("stage", stage)],
            },
        )

    assert resp.status_code == 200, resp.text
    block = _claims_block(resp.json())
    assert block["items"] == [], f"the non-pilot tenant was served {len(block['items'])} item(s) from {name}"
    # `empty`, never `degraded`. A withholding notice would be its own leak: it
    # would tell a tenant that was never in the pilot that something was held
    # back from it, and therefore that something is there.
    assert block["state"] == "empty", (
        f"{name} answered a non-pilot tenant with state {block['state']!r}; "
        "anything but empty tells the outsider material exists"
    )


# --- The failures the pilot recorded still happen -----------------------------


@pytest.mark.asyncio
async def test_the_misspelled_kind_the_pilot_hit_is_refused_at_the_boundary(surface: _Surface) -> None:
    """The join failure that cost the pilot two days cannot be re-entered.

    Two scenarios in the corpus carry unjoined outcomes, all four from one
    window in which the reference kind was spelled with a hyphen. The envelopes
    stored cleanly, bound cleanly, and never joined. This asserts the spelling
    is now refused where the pilot accepted it — so the corpus records a failure
    the surface no longer permits, which is what a fixed defect looks like.
    """
    unjoined = sum(scenario["cardinality"]["outcomes_unjoined"] for _, scenario in _SCENARIOS)
    assert unjoined == 4, f"the corpus carries {unjoined} unjoined outcome(s); the pilot recorded four"

    resp = await _resolve(surface, lifecycle_references=[_lifecycle_reference("workflow-run", "8891234501")])

    assert resp.status_code == 422, (
        "the hyphenated spelling was accepted; it stores cleanly and then never joins, "
        "which reads downstream as an outcome that never arrived"
    )


@pytest.mark.asyncio
async def test_a_scenario_that_recorded_an_interruption_resumes_rather_than_erroring(surface: _Surface) -> None:
    """A reconnect against work this tenant has none of is an answer, not an error.

    The corpus's interrupted scenarios resumed; the property that made that
    possible is that a reconnect is a read which reports what it found. A 404
    here would make a caller retry a request that was fine, and the pilot's
    interruptions were exactly the moments nobody had attention to spare for a
    spurious error.
    """
    interrupted = [name for name, scenario in _SCENARIOS if _has_kind(scenario, "interruption")]
    assert interrupted, "no scenario in the corpus recorded an interruption"

    with _as(surface, surface["pilot"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": [["control-plane", "acme", "run", f"run-{uuid.uuid4()}"]]},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "empty"
    assert resp.json()["intent_id"] is None


@pytest.mark.asyncio
async def test_a_resume_with_no_references_is_still_refused(surface: _Surface) -> None:
    """The bound the pilot leaned on: a reconnect must name what it is reconnecting to.

    An unbounded resume would return whatever the tenant has, which is the
    silent-fallback shape the availability contract exists to prevent.
    """
    with _as(surface, surface["pilot"]):
        resp = await surface["client"].post(
            "/v1/context/resume",
            json={"references": []},
            headers=bearer_headers(tenant_slug=surface["slug"]),
        )

    assert resp.status_code == 422, resp.text


# --- Both transports still answer the same way --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "scenario"), _SCENARIOS, ids=_IDS)
async def test_both_transports_narrow_a_scenario_identically(
    surface: _Surface, name: str, scenario: dict[str, Any]
) -> None:
    """One answer, two transports, for every scenario the pilot ran.

    Parity is checked per scenario rather than once because the profile is the
    input the two transports have to agree about, and a divergence would show up
    on the shapes that carry a placement rather than on an empty request.
    """
    stage = scenario["stages_covered"][0]
    served = await _seed_placed_claim(
        surface["pg_url"],
        tenant_id=surface["tenant_id"],
        actor_id=surface["actor_id"],
        stage=stage,
        predicate=f"{name}_parity",
        authority=_authority_for(scenario),
    )

    profile = [_lifecycle_reference("stage", stage)]
    rest = (await _resolve(surface, lifecycle_references=profile)).json()

    from contextplane.api.mcp.tools.context import registry_resolve_context

    app = surface["harness"].app
    with _mcp_request(surface, surface["pilot"], surface["slug"]):
        raw = await registry_resolve_context(
            "what do I need to know about this change",
            lifecycle_references=profile,
            session_factory=app.state.session_factory,
            clock=app.state.clock,
        )
    mcp = json.loads(raw)

    rest_keys = [item["receipt_item_id"]["item_key"] for item in _claims_block(rest)["items"]]
    mcp_keys = [item["receipt_item_id"]["item_key"] for item in _claims_block(mcp)["items"]]

    assert rest_keys == [str(served)], f"{name} REST served {rest_keys}"
    assert mcp_keys == rest_keys, f"{name} diverged between transports: REST {rest_keys}, MCP {mcp_keys}"


def _has_kind(scenario: dict[str, Any], kind: str) -> bool:
    return any(event["kind"] == kind for event in scenario["refusal_or_degradation"])


# --- The corpus is evidence about a lifecycle, checked here too ---------------


def test_the_corpus_this_gate_runs_on_covers_a_pilot_shaped_set() -> None:
    """The exit criteria that are properties of the corpus rather than of a request.

    Five or more changes, more than one team, a pre-code point and a later one
    on each. Asserted here as well as in the corpus's own gate because this is
    the module that would otherwise report an exit gate as passed while running
    over a corpus that had quietly stopped meeting the bar it was frozen to.
    """
    assert len(_SCENARIOS) >= 5

    teams = {scenario["work"]["team"] for _, scenario in _SCENARIOS}
    assert len(teams) >= 2, f"the corpus covers {sorted(teams)}"

    for name, scenario in _SCENARIOS:
        stages = set(scenario["stages_covered"])
        assert "implementation" in stages, f"{name} used no context before code existed"
        assert stages - {"implementation"}, f"{name} used no context after implementation"

    confirmed = [instance for _, scenario in _SCENARIOS for instance in scenario["prior_learning"]["instances"]]
    assert confirmed, "no change in the corpus retrieved reviewed learning from an earlier one"
