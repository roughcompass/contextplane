"""`make dev-seed` demonstrates the living-memory loop, not just the catalog.

Runs the real CLI path -- `scripts/bootstrap_dev_tenant.py` then
`scripts/seed.py`, exactly as `make dev-seed` does -- against a fresh
testcontainers Postgres, then reads the memory-loop scenario back through the
services that own it (`CurationQueueService`, `PromotionService`,
`CapabilityRequestService`, `GuardrailService`, `MemoryService`) rather than
raw SQL, because those read paths are the contract a curator or reviewer
actually exercises. Finishes by re-running the seed a second time and
checking nothing this section wrote duplicated -- the memory-loop section
handler (`scripts/seed.py::apply_memory_loop_section`) is not itself backed
by the ON-CONFLICT-DO-NOTHING pattern the rest of the loader leans on, so
this is the test that actually proves its idempotency claim.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.memory.capability_requests import CapabilityRequestService
from registry.service.memory.claim_writer import ClaimService
from registry.service.memory.curation_queue import CurationQueueService
from registry.service.memory.promotion import PromotionService
from registry.service.memory.promotion_guardrails import GuardrailService
from registry.service.memory.session_events import MemoryService
from registry.types import SystemClock, TenantContext

_REPO_ROOT = Path(__file__).parent.parent.parent
_BOOTSTRAP_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap_dev_tenant.py"
_SEED_SCRIPT = _REPO_ROOT / "scripts" / "seed.py"


def _run(database_url: str, script: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *extra],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": database_url},
        cwd=str(_REPO_ROOT),
        check=False,
    )


async def _resolve_dev_ids(pg_container: str) -> tuple[uuid.UUID, uuid.UUID]:
    """The dev tenant and dev-admin actor ids, read directly -- test setup, not
    a stand-in for the services under test."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        async with engine.connect() as conn:
            tenant_row = (await conn.execute(text("SELECT tenant_id FROM tenants WHERE slug = 'dev'"))).one()
            tenant_id = uuid.UUID(str(tenant_row[0]))
            actor_row = (
                await conn.execute(
                    text(
                        "SELECT actor_id FROM actors "
                        "WHERE tenant_id = :tid AND display_name = 'dev-admin' AND actor_kind = 'human'"
                    ),
                    {"tid": tenant_id},
                )
            ).one()
            actor_id = uuid.UUID(str(actor_row[0]))
    finally:
        await engine.dispose()
    return tenant_id, actor_id


@pytest.mark.asyncio
async def test_dev_seed_drives_the_memory_loop_through_the_real_services(pg_container: str) -> None:
    bootstrap = _run(
        pg_container,
        _BOOTSTRAP_SCRIPT,
        "--tenant-slug",
        "dev",
        "--actor-display-name",
        "dev-admin",
        "--skip-mock-seed",
    )
    assert bootstrap.returncode == 0, bootstrap.stderr

    # `pg_container` is session-scoped and shared with `test_seed.py`, which
    # also runs `scripts/seed.py` against this same `dev` tenant -- so by the
    # time this runs, the memory-loop scenario may already be seeded. That is
    # exactly the case this test has to tolerate: the assertions below check
    # the landed state, not this particular invocation's own summary output.
    first_run = _run(pg_container, _SEED_SCRIPT)
    assert first_run.returncode == 0, first_run.stderr

    tenant_id, actor_id = await _resolve_dev_ids(pg_container)
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer", "admin"], oidc_subject="s")

    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _assert_scenario_landed(factory, ctx)

        # --- idempotency: re-run must not duplicate anything this section wrote ---
        second_run = _run(pg_container, _SEED_SCRIPT)
        assert second_run.returncode == 0, second_run.stderr
        await _assert_scenario_landed(factory, ctx)
    finally:
        await engine.dispose()


async def _assert_scenario_landed(factory: async_sessionmaker[AsyncSession], ctx: TenantContext) -> None:
    """Every assertion in this helper is re-run verbatim after the second seed
    pass, so a duplicate anywhere shows up as a changed count, not just a
    present-vs-absent check."""
    tenant_id = ctx.tenant_id

    # --- claims: linked, unlinked, and the contested pair, via the curation queue ---
    queue = CurationQueueService(factory)
    items = await queue.items_for(tenant_id, page_size=200)
    assert len(items) == len(set(item.claim_id for item in items)), "curation queue returned duplicate claim rows"

    unlinked = [i for i in items if i.predicate == "owned_by_team" and i.value == "checkout-team"]
    assert len(unlinked) == 1, f"expected exactly one unlinked mobile-checkout claim, got {unlinked}"
    assert unlinked[0].reason == "unlinked"
    assert unlinked[0].subject_entity_id is None

    contested = [i for i in items if i.predicate == "lifecycle_state"]
    assert len(contested) == 2, f"expected the contested pair (ga vs deprecated), got {contested}"
    assert {i.value for i in contested} == {"ga", "deprecated"}
    assert all(i.reason == "contested" for i in contested)
    assert all(i.subject_entity_id is not None for i in contested), "the contested pair's subject must have resolved"

    # The linked claim is not high-impact, so it never reaches the general
    # curation queue (`_QUEUE_BASE`'s `awaiting_owner` arm is high-impact
    # proposals only) -- it is visible through the promotion-review surface
    # instead, checked below.
    linked_in_queue = [i for i in items if i.predicate == "owned_by_team" and i.value == "platform-team"]
    assert linked_in_queue == [], "the linked, proposed claim should not surface in the general curation queue"

    # --- the open proposal, via the promotion-review read path ---
    promotion = PromotionService(factory, claims=ClaimService(factory, clock=SystemClock()), clock=SystemClock())
    open_proposals = await promotion.proposals_for(tenant_id, state="open", page_size=200)
    ours = [p for p in open_proposals if p.predicate == "owned_by_team" and p.proposed_value == "platform-team"]
    assert len(ours) == 1, f"expected exactly one open owned_by_team proposal, got {ours}"
    assert ours[0].subject_entity_id is not None

    # --- the capability request, via the owner's own request queue ---
    capability_requests = CapabilityRequestService(factory, clock=SystemClock())
    owner_requests = await capability_requests.for_owner(ctx, open_only=True, page_size=200)
    matching = [r for r in owner_requests if r.title == "Document memory-loop-demo's retry policy"]
    assert len(matching) == 1, f"expected exactly one raised capability request, got {matching}"
    assert matching[0].status == "raised"
    assert matching[0].request_category == "documentation"

    # --- the allowlist entry, via GuardrailService's own read path ---
    guardrails = GuardrailService(factory, clock=SystemClock())
    allowlist = await guardrails.allowlist_for(tenant_id)
    assert "runbook_url" in allowlist

    # --- the session events, via MemoryService's own replay ---
    memory = MemoryService(factory, clock=SystemClock())
    events = await memory.list_events(ctx, session_id="memory-loop-demo-session")
    assert len(events) == 3, f"expected the scenario's 3 session events, got {len(events)}"
    assert [e.kind for e in events] == ["user_message", "tool_invocation", "agent_action"]
