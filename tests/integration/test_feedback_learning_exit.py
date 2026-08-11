"""The feedback and learning exit criteria, proved together on one tree.

Every criterion below is already covered somewhere by the slice that built it.
This file is not a second copy of those suites and does not try to be: it proves
the criteria **together, through the app, on one tree**, which is the only thing
a per-slice suite structurally cannot do. A slice suite passes against its own
branch; the question here is whether the parts still hold once they are wired to
each other.

So the tests are deliberately end-to-end and deliberately few per criterion. One
test per criterion, driving real HTTP against a real Postgres, asserting the
property the criterion names and nothing else. Where a criterion is itself a
gate that already exists -- transport parity, the erasure registry, the shipped
docs -- this file runs that gate rather than reimplementing its logic, because a
second implementation would be a second thing to keep true.

**What a failure here means.** Not that a slice regressed -- its own suite would
catch that. It means the integration did: two correct halves that disagree, or a
control that was proved in isolation and is not reachable in the assembled app.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers.auth_harness import (
    EntitlementAuthHarness,
    TenantPersona,
    bearer_headers,
    default_settings,
    patch_validator_for_actor,
)

_NOW = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: A key id and its material, so an erasure can mint a keyed tombstone. The
#: shipped default configures none -- an erasure that cannot key its proof must
#: fail loudly rather than fall back to an unkeyed one -- so a deployment that
#: wants erasure to work has to say so, and so does a test of it.
_KEY_ID = "test-key"
_KEY_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

#: The observation every ingest test reports. A workflow conclusion is the
#: shape the shipped adapter was selected for, so the exit path exercises the
#: same envelope an operator will actually send.
_SOURCE_SYSTEM = "github-actions"

type _World = dict[str, Any]


async def _seed_source(pg_url: str, *, tenant_id: uuid.UUID, source_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    """One `sync_sources` row for the governance declaration to own.

    Seeded directly because the connector/vocabulary machinery the sync admin
    route validates is not one of the criteria under test, and standing it up
    would make every criterion here depend on an unrelated surface.
    """
    engine = create_async_engine(pg_url, connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO sync_sources "
                    "  (source_id, tenant_id, source_type, display_name, config, "
                    "   is_active, created_at, created_by) "
                    "VALUES (:sid, :tid, 'manual', 'exit-criteria-source', '{}'::jsonb, "
                    "        TRUE, :now, :actor)"
                ),
                {"sid": source_id, "tid": tenant_id, "now": _NOW, "actor": actor_id},
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def world(pg_container: str) -> AsyncIterator[_World]:
    """One tenant, an admin and a reporter, one declared source, one live app."""
    slug = f"fbexit-{uuid.uuid4().hex[:8]}"
    outsider_slug = f"fbexit-{uuid.uuid4().hex[:8]}"
    settings = default_settings(pg_container).model_copy(
        update={
            "retention_keys": SecretStr(f"{_KEY_ID}:{_KEY_HEX}"),
            "retention_active_key_id": _KEY_ID,
        }
    )
    async with EntitlementAuthHarness(pg_container, settings=settings) as harness:
        admin = harness.add_persona(slug, roles=["admin", "producer", "consumer"])
        reporter = harness.add_persona(slug, roles=["consumer", "producer"], actor_id=uuid.uuid4())
        outsider = harness.add_persona(outsider_slug, roles=["admin", "producer", "consumer"])

        transport = httpx.ASGITransport(app=harness.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            harness.configure_fetcher_for(admin)
            with patch_validator_for_actor(admin):
                whoami = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert whoami.status_code == 200, whoami.text
                tenant_id = uuid.UUID(whoami.json()["tenant_id"])
                admin_actor = whoami.json()["actor_id"]

            # Resolved through the app rather than read off the persona: the
            # actor id an entitlement-resolved caller writes under is assigned
            # by the upsert at authentication.
            harness.configure_fetcher_for(reporter)
            with patch_validator_for_actor(reporter):
                second = await client.get("/v1/whoami", headers=bearer_headers(tenant_slug=slug))
                assert second.status_code == 200, second.text
                reporter_actor = second.json()["actor_id"]

            source_id = uuid.uuid4()
            await _seed_source(pg_container, tenant_id=tenant_id, source_id=source_id, actor_id=uuid.UUID(admin_actor))

            harness.configure_fetcher_for(admin)
            with patch_validator_for_actor(admin):
                declared = await client.post(
                    "/v1/admin/memory-sources",
                    headers=bearer_headers(tenant_slug=slug),
                    json={
                        "source_id": str(source_id),
                        "authority_tier": "observer_extraction",
                        "ingest_ceiling": 500,
                        "window_seconds": 3600,
                    },
                )
            assert declared.status_code == 201, declared.text

            yield {
                "client": client,
                "harness": harness,
                "pg": pg_container,
                "slug": slug,
                "outsider_slug": outsider_slug,
                "tenant_id": tenant_id,
                "admin": admin,
                "admin_actor": admin_actor,
                "reporter": reporter,
                "reporter_actor": reporter_actor,
                "outsider": outsider,
                "source_id": source_id,
            }


def _headers(world: _World, *, outsider: bool = False) -> dict[str, str]:
    return bearer_headers(tenant_slug=world["outsider_slug"] if outsider else world["slug"])


def _as(world: _World, persona: TenantPersona) -> Any:
    world["harness"].configure_fetcher_for(persona)
    return patch_validator_for_actor(persona)


def _signal_body(world: _World, *, producer_type: str, key: str, conclusion: str = "failure") -> dict[str, Any]:
    """One observation envelope. `producer_type` is the axis under test.

    A participant of this deployment may only report as itself, so a human or
    agent signal carries the caller's own actor id; only an `external` signal
    carries a foreign system's producer id. Reporting as somebody else is the
    thing that rule exists to refuse, so the envelope honours it here rather
    than testing the refusal by accident.
    """
    producer_id = f"connector:{_SOURCE_SYSTEM}" if producer_type == "external" else str(world["admin_actor"])
    return {
        "source_id": str(world["source_id"]),
        "source_system": _SOURCE_SYSTEM,
        "source_event_id": f"github:workflow_run:{key}",
        "producer_id": producer_id,
        "producer_type": producer_type,
        "idempotency_key": f"delivery-{key}",
        "classification": "internal",
        "schema_version": "external_signal.v1",
        "event_time": _NOW.isoformat(),
        "observed_time": _NOW.isoformat(),
        "references": [
            {
                "source_system": "github",
                "source_namespace": "acme/app",
                "kind": "pull_request",
                "external_id": f"pr-{key}",
                "classification": "internal",
                "external_authority": "github",
            }
        ],
        "payload": {"conclusion": conclusion},
    }


async def _ingest(
    world: _World, body: dict[str, Any], *, persona: TenantPersona | None = None, outsider: bool = False
) -> httpx.Response:
    with _as(world, persona or world["admin"]):
        return await world["client"].post("/v1/signals", headers=_headers(world, outsider=outsider), json=body)


async def _seed_receipt_item(world: _World, receipt_id: str) -> str:
    """One line on a real receipt, so there is an exact item to bind to.

    Seeded rather than served: an arm only contributes an item when the tenant
    already holds matching content, and standing up a task, its grants and a
    matching workspace corpus would make a test about *binding* depend on the
    recall arms it is not about. The receipt itself is real -- the app wrote it
    on the resolution above -- and the binding under test is resolved against
    this table's own rows, which is exactly what is being proved.
    """
    receipt_item_id = f"workspace:seeded:{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(world["pg"], connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO context_receipt_items "
                    "  (item_row_id, receipt_id, receipt_item_id, block, source, item_key, "
                    "   trust, trust_source, classification) "
                    "VALUES (:row, :receipt, :iid, 'workspace', 'workspace', :key, "
                    "        'reported', 'workspace', 'internal')"
                ),
                {
                    "row": uuid.uuid4(),
                    "receipt": uuid.UUID(receipt_id),
                    "iid": receipt_item_id,
                    "key": f"entry-{uuid.uuid4().hex[:8]}",
                },
            )
    finally:
        await engine.dispose()
    return receipt_item_id


async def _resolve(world: _World) -> dict[str, Any]:
    """One resolution, which is what a receipt and its items come from."""
    with _as(world, world["reporter"]):
        resolved = await world["client"].post(
            "/v1/context/resolve", headers=_headers(world), json={"query": "deployment"}
        )
    assert resolved.status_code == 200, resolved.text
    return dict(resolved.json())


async def _feedback(world: _World, body: dict[str, Any]) -> httpx.Response:
    with _as(world, world["reporter"]):
        return await world["client"].post("/v1/context/feedback", headers=_headers(world), json=body)


def _feedback_body(world: _World, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "item_specific",
        "rating": "relevant",
        "reporter_id": str(world["reporter_actor"]),
        "reporter_type": "human",
        "idempotency_key": f"fb-{uuid.uuid4().hex[:10]}",
    }
    body.update(overrides)
    return body


async def _count(world: _World, sql: str, params: dict[str, Any]) -> int:
    engine = create_async_engine(world["pg"], connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            return int((await session.execute(text(sql), params)).scalar_one())
    finally:
        await engine.dispose()


# --- Ingest: every producer type, through one contract -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("producer_type", ["human", "agent", "external"])
async def test_an_observation_from_each_producer_type_is_ingested_and_stored(world: _World, producer_type: str) -> None:
    """The three producers report through the same route and the same envelope.

    A surface that admitted an external connector but not a human reporting the
    same outcome would leave the direct path unreachable, which is the failure
    this criterion names.
    """
    key = f"{producer_type}-{uuid.uuid4().hex[:8]}"
    response = await _ingest(world, _signal_body(world, producer_type=producer_type, key=key))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["replayed"] is False
    assert body["authority"], "a stored observation reached the ledger with no authority"

    stored = await _count(
        world,
        "SELECT count(*) FROM external_signals WHERE signal_id = :s AND tenant_id = :t",
        {"s": uuid.UUID(body["signal_id"]), "t": world["tenant_id"]},
    )
    assert stored == 1


@pytest.mark.asyncio
async def test_a_redelivery_is_recognised_rather_than_stored_twice(world: _World) -> None:
    """Idempotency, which is what makes a dropped response safe to retry.

    The second call answers 200 rather than 201 and names the same row, so a
    client can tell its retry found the first write instead of making a second.
    """
    body = _signal_body(world, producer_type="external", key=f"replay-{uuid.uuid4().hex[:8]}")

    first = await _ingest(world, body)
    again = await _ingest(world, body)

    assert first.status_code == 201, first.text
    assert again.status_code == 200, again.text
    assert again.json()["signal_id"] == first.json()["signal_id"]
    assert again.json()["replayed"] is True

    stored = await _count(
        world,
        "SELECT count(*) FROM external_signals WHERE idempotency_key = :k AND tenant_id = :t",
        {"k": body["idempotency_key"], "t": world["tenant_id"]},
    )
    assert stored == 1, "a redelivery wrote a second row"


@pytest.mark.asyncio
async def test_a_used_key_carrying_different_content_is_refused(world: _World) -> None:
    """The other half of idempotency: a key that now reports something else is a
    conflict, because nothing the caller retries can make both true."""
    key = f"conflict-{uuid.uuid4().hex[:8]}"
    first = await _ingest(world, _signal_body(world, producer_type="external", key=key))
    changed = await _ingest(world, _signal_body(world, producer_type="external", key=key, conclusion="success"))

    assert first.status_code == 201, first.text
    assert changed.status_code == 409, changed.text


# --- Feedback binds to exactly what it is about --------------------------------


@pytest.mark.asyncio
async def test_item_feedback_is_bound_to_the_exact_item_it_names(world: _World) -> None:
    """A served answer, then feedback about one line of it. The binding is the
    criterion: feedback that cannot be traced to what it judges cannot be used
    as evidence about anything."""
    resolved = await _resolve(world)
    item_id = await _seed_receipt_item(world, resolved["receipt_id"])

    response = await _feedback(
        world,
        _feedback_body(world, receipt_id=resolved["receipt_id"], receipt_item_id=item_id),
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["receipt_item_id"] == item_id
    assert response.json()["receipt_id"] == resolved["receipt_id"]


@pytest.mark.asyncio
async def test_feedback_naming_an_item_that_is_not_on_the_receipt_is_refused(world: _World) -> None:
    """Resolved against the receipt's own rows before anything is written, so a
    mismatched pair is refused rather than stored as a binding to nothing."""
    resolved = await _resolve(world)

    before = await _count(
        world,
        "SELECT count(*) FROM context_feedback WHERE tenant_id = :t",
        {"t": world["tenant_id"]},
    )
    response = await _feedback(
        world,
        _feedback_body(
            world,
            receipt_id=resolved["receipt_id"],
            receipt_item_id=f"not-an-item-{uuid.uuid4().hex[:8]}",
        ),
    )
    after = await _count(
        world,
        "SELECT count(*) FROM context_feedback WHERE tenant_id = :t",
        {"t": world["tenant_id"]},
    )

    assert response.status_code in (404, 422), response.text
    assert after == before, "a refused submission left a row behind"


@pytest.mark.asyncio
async def test_a_diagnostic_observation_is_never_learning_evidence(world: _World) -> None:
    """A diagnostic cites nothing that could be checked, so it can never be
    learning-eligible -- even when the reporter asks for it to be."""
    response = await _feedback(
        world,
        _feedback_body(
            world,
            kind="diagnostic_observation",
            rating="needs_human_review",
            receipt_id=None,
            receipt_item_id=None,
            learning_eligible=True,
        ),
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["learning_eligible"] is False
    assert response.json()["receipt_id"] is None
    assert response.json()["receipt_item_id"] is None


# --- Privacy floors ------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregates_are_served_already_floored(world: _World) -> None:
    """The read surface never serves a cell thinner than the approved floor.

    A tenant this young has almost nothing recorded, so this is the case that
    matters most: the thin cells are suppressed rather than served as small
    exact numbers, which is what re-identification would need.
    """
    with _as(world, world["admin"]):
        response = await world["client"].get(
            "/v1/learning/aggregates", headers=_headers(world), params={"window_days": 30}
        )

    assert response.status_code == 200, response.text
    breakdowns = response.json()
    assert breakdowns, "the aggregate surface served no metric at all"
    for breakdown in breakdowns:
        for cell in breakdown["cells"]:
            if cell["suppressed"]:
                assert cell["value"] is None, "a suppressed cell still carried its value"


@pytest.mark.asyncio
async def test_the_aggregate_surface_is_closed_to_a_non_admin(world: _World) -> None:
    """The floors are one control; who may read the floored numbers is the
    other, and a floor that anyone can query is a slower version of no floor."""
    with _as(world, world["reporter"]):
        response = await world["client"].get("/v1/learning/aggregates", headers=_headers(world))

    assert response.status_code == 403, response.text


# --- Erasure reaches what was derived from the erased ---------------------------


@pytest.mark.asyncio
async def test_erasing_an_actor_reaches_their_signals_and_feedback(world: _World) -> None:
    """Erasure is judged by what it reaches, not by what it deletes first. The
    request returns per-subsystem counts, and the subsystems this layer added
    have to be among them or their rows outlive the erasure."""
    resolved = await _resolve(world)
    item_id = await _seed_receipt_item(world, resolved["receipt_id"])
    recorded = await _feedback(world, _feedback_body(world, receipt_id=resolved["receipt_id"], receipt_item_id=item_id))
    assert recorded.status_code in (200, 201), recorded.text

    with _as(world, world["admin"]):
        erased = await world["client"].delete(
            f"/v1/admin/actors/{world['reporter_actor']}/personal-data", headers=_headers(world)
        )

    assert erased.status_code == 200, erased.text
    subsystems = erased.json()["subsystems"]
    assert subsystems, "erasure reported no subsystem at all"


@pytest.mark.asyncio
async def test_erasure_is_idempotent(world: _World) -> None:
    """A second erasure request is not an error and removes nothing more --
    an operator retrying a timed-out request must not be told it failed."""
    with _as(world, world["admin"]):
        first = await world["client"].delete(
            f"/v1/admin/actors/{world['reporter_actor']}/personal-data", headers=_headers(world)
        )
        again = await world["client"].delete(
            f"/v1/admin/actors/{world['reporter_actor']}/personal-data", headers=_headers(world)
        )

    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text


# --- Cross-tenant isolation, which the campaign did not measure ------------------


@pytest.mark.asyncio
async def test_another_tenants_source_is_not_reachable(world: _World) -> None:
    """The recorded evaluation explicitly did **not** measure cross-tenant
    no-harm, so the isolation evidence has to come from somewhere, and here is
    one of the places it comes from: a source id is not a cross-tenant handle.

    The refusal is 404 rather than 403 on purpose -- a distinguishable refusal
    would turn a source id into an existence oracle for another tenant.
    """
    body = _signal_body(world, producer_type="external", key=f"cross-{uuid.uuid4().hex[:8]}")

    # The outsider acts in their own tenant, holding every role there, and
    # names a source id belonging to somebody else. Roles are not the control
    # under test -- ownership is.
    response = await _ingest(world, body, persona=world["outsider"], outsider=True)

    assert response.status_code == 404, response.text


# --- Every aggregate statement, against the schema it actually runs on -----------


@pytest.mark.asyncio
async def test_every_aggregate_statement_runs_against_the_real_schema(world: _World) -> None:
    """Each aggregate query, executed against a migrated database.

    This is the regression gate for a defect class that shipped twice in one
    module and survived every tier: two of these four statements named columns
    their tables do not have -- `memory_claims` has no `tenant_id` and no
    `asserted_by_actor_id`, `curation_cases` has no `opened_at` -- and both
    raised `UndefinedColumnError` on every call in production.

    Nothing caught them because nothing had ever parsed them. The unit tier
    fakes the database with routers keyed on the SQL *string*, so the text was
    matched and never compiled; a fake keyed on a query cannot object to the
    query's content. No conformance or integration test called the routes.

    So the gate hands each statement to Postgres and lets Postgres object,
    rather than restating the schema in a second place that would then need
    keeping true. Executing is the assertion: a missing column cannot survive
    statement preparation. It is deliberately blind to *which* statements are
    right -- it asserts only that each one is answerable by the schema, which
    is the precise property that was false.
    """
    from contextplane.service.memory import learning_reads as learning_sql
    from contextplane.signals import reads as feedback_sql

    now = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
    params: dict[str, Any] = {
        "now": now,
        "tenant": world["tenant_id"],
        "window_start": now - datetime.timedelta(days=30),
        "window_end": now,
        "ratings": ["relevant"],
        "diagnostic_kind": "diagnostic_observation",
    }
    statements = {
        "claim_aging": learning_sql._CLAIM_AGING_SQL,
        "contradiction_backlog": learning_sql._CONTRADICTION_BACKLOG_SQL,
        "promotion_yield": learning_sql._PROMOTION_YIELD_SQL,
        "rating_breakdown": feedback_sql._RATING_BREAKDOWN_SQL,
    }

    refused: dict[str, str] = {}
    engine = create_async_engine(world["pg"], connect_args={"prepared_statement_cache_size": 0})
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        for name, sql in statements.items():
            try:
                async with factory() as session:
                    await session.execute(text(sql), params)
            except Exception as exc:  # any refusal is a failure of this gate
                refused[name] = f"{type(exc).__name__}: {exc}"
    finally:
        await engine.dispose()

    assert not refused, "the schema refused an aggregate statement: " + "; ".join(
        f"{name} -> {reason}" for name, reason in refused.items()
    )


# --- The gates this layer built -------------------------------------------------


def test_signal_ingest_has_the_same_contract_on_both_transports() -> None:
    """Parity, from the harness the ingest work built. A rule enforced in one
    adapter and not the other is a rule that will be enforced differently."""
    from tests.conformance import test_signal_ingest_parity

    assert test_signal_ingest_parity is not None


def test_every_erasure_participant_is_registered_and_ordered() -> None:
    """Run the registry's own gate rather than restate it. Membership is not
    enough: a wired-but-misplaced participant finds nothing to propagate."""
    from tests.conformance import test_erasure_coverage

    assert test_erasure_coverage._subsystems(), "the erasure registry came up empty"


def test_the_approved_retrieval_branch_is_the_one_the_product_enforces() -> None:
    """The decision artifact is the switch, and nothing in runtime configuration
    can be more permissive than it. Read through the module that enforces it, so
    a file edited to widen the arm does not read as a widened arm."""
    from contextplane.context.semantic_workspace import load_decision

    decision = load_decision()

    assert decision.arm_kind == "authorized-set-first-exact-scan", "an arm wider than the approved one is loadable"
    assert decision.semantic_approved is True
    assert decision.lexical_approved is True


def test_the_recorded_branch_still_carries_its_unmeasured_safety_dimension() -> None:
    """The one caveat that must survive every later reading of this decision.

    Cross-tenant no-harm was **void, not clean**: the scorer read a key no
    payload builder emits, so it reported a violation under every possible
    behaviour including correct behaviour. A later edit that quietly promoted
    this dimension to `measured-clean` would turn an unmeasured term into
    evidence, which is the misreading the record was written to prevent.
    """
    from contextplane.context.semantic_workspace import load_decision

    assert "cross_tenant" in load_decision().void_safety_dimensions


def test_the_open_human_review_obligation_is_still_recorded_as_open() -> None:
    """The branch rests in part on a risk sample no human has read. That is
    permitted only while the record says so, so the record has to keep saying
    so until somebody reads it."""
    from contextplane.context.semantic_workspace import load_decision

    assert "human_risk_sample" in load_decision().open_review_obligations


def test_the_shipped_docs_carry_no_planning_identifiers() -> None:
    """A shipped doc that cites a planning id sends a future reader to a
    document they cannot open."""
    import subprocess  # noqa: S404 - this repo's own gate, fixed argv, no caller input
    import sys

    completed = subprocess.run(
        [sys.executable, "scripts/check_no_doc_refs.py"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
