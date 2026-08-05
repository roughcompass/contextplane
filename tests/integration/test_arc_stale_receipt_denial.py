"""Stale-receipt denial: the fourth ARC eval metric, measured against a live database.

The other three ARC selection metrics (mandatory-inclusion recall,
prohibited-inclusion rate, precedence-conflict detection) are measured in
`tests/unit/test_arc_selection_eval_gate.py` against a pure function and need
no database at all. This one is different in kind: it is about what happens
to a receipt's evidentiary value *after* the world has moved on from the
moment it was issued, and "the world moved on" here specifically means a
revision's row in `arc_revisions` changed underneath a receipt that already
cited it. That is a live-database concern -- `JitService.retrieve` re-reads
current lifecycle state on every detail request rather than trusting what
the receipt recorded -- so it belongs here, not beside the pure-function
gate.

What the metric measures: of the JIT detail requests that cite a directive
whose revision has since been revoked, what fraction are denied. The
threshold is exact (1.0) for the same reason the selection metrics are exact
-- `JitService.retrieve`'s revocation re-check is deterministic given the
revision's current lifecycle_state, so a scenario that misses is either a
scenario bug or a real regression, never noise to average away.

A note on "revoked" versus "expired". The task that motivates this file
describes the invariant as "a receipt whose required revision is
revoked/expired must be denied", but those two lifecycle states are not
equivalent in this codebase, on purpose: `JitService.retrieve` treats
`lifecycle_state in ("active", "expired")` as still servable (see
`registry.arc.service.detail_retrieval`'s module docstring and its `DENIED_REVOKED`
check), matching `corpus.py`'s own comment that "a revision whose review
lapsed still governs, and dropping it here would silently release the
obligation rather than surface it as degraded". Only `revoked` triggers
denial. `test_an_expired_but_not_revoked_revision_still_serves_detail` below
locks in that distinction explicitly, so a future change that conflates the
two states fails a test that says why, instead of silently changing
behaviour.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.arc.service.continuation import ContinuationTokenProvider
from registry.arc.service.detail_retrieval import DENIED_REVOKED, DetailDenied, DetailRequest, JitService
from registry.arc.service.receipt import ReceiptService, SelectedDirective, SelectedRevision, preallocate_receipt_id
from registry.arc.types import ArcRequestContext
from registry.types import TenantContext
from tests.helpers.arc_fixtures import (
    ARC_NOW,
    ArcSeed,
    consume_challenge,
    provenance,
    ready_bundle,
    replay_envelope,
    seed_arc,
    seed_challenge,
    signing_provider,
)
from tests.helpers.clock import FakeClock

_HANDLE = "handle-stale-receipt"
_TOKEN_KEY = "ct-stale-receipt"

# Exact for the same reason the pure-selection metrics are exact: the
# revocation re-check in JitService.retrieve is deterministic given the
# revision's lifecycle_state, so a fixture set that cannot hit 1.0 means a
# scenario is wrong or the engine regressed.
_STALE_RECEIPT_DENIAL_THRESHOLD = 1.0


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(ARC_NOW)


@pytest.fixture
def tokens() -> ContinuationTokenProvider:
    return ContinuationTokenProvider({_TOKEN_KEY: b"k" * 32}, active_key_id=_TOKEN_KEY)


@pytest.fixture
def receipts(clock: FakeClock) -> ReceiptService:
    return ReceiptService(signing_provider(), clock)


@pytest.fixture
def jit(
    factory: async_sessionmaker[AsyncSession],
    receipts: ReceiptService,
    tokens: ContinuationTokenProvider,
    clock: FakeClock,
) -> JitService:
    return JitService(factory, receipts=receipts, tokens=tokens, clock=clock)


@pytest_asyncio.fixture
async def seed_stale(factory: async_sessionmaker[AsyncSession]) -> ArcSeed:
    return await seed_arc(factory, slug_prefix="arc-stale")


def _ctx(seed: ArcSeed) -> ArcRequestContext:
    tenant = TenantContext(tenant_id=seed.tenant_id, actor_id=seed.actor_id, roles=["consumer"], oidc_subject="s")
    return ArcRequestContext.from_validated_claims(tenant, {"iss": "https://idp.example.test"}, host_id="host-1")


def _request(receipt_id: uuid.UUID) -> DetailRequest:
    return DetailRequest(
        receipt_id=receipt_id,
        context_handle=_HANDLE,
        request_kind="directive",
        selector={},
        idempotency_key=uuid.uuid4().hex,
    )


async def _seed_receipt_citing_the_seeded_directive(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, seed: ArcSeed
) -> uuid.UUID:
    """A committed receipt whose selected-directive row points at `seed.revision_id`.

    Shared by every scenario below -- what differs per scenario is only the
    lifecycle_state the revision is moved to *after* this receipt exists,
    which is the whole point: the receipt is fixed evidence, and detail
    retrieval re-checks the world as it stands now, not as it stood when the
    receipt was written.
    """
    receipt_id = preallocate_receipt_id()
    challenge_id = await seed_challenge(factory, tenant_id=seed.tenant_id)
    async with factory() as session, session.begin():
        await receipts.create_receipt(
            session,
            receipt_id=receipt_id,
            challenge_id=challenge_id,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            host_id="host-1",
            session_id="sess-1",
            manifest_fingerprint="f" * 64,
            attestation_id=f"att-{receipt_id}",
            bundle=ready_bundle(1),
            provenance=provenance(),
            replay=replay_envelope(),
            evaluated_at=ARC_NOW,
            freshness_basis="revision_pinned_only",
            selected_revisions=(
                SelectedRevision(revision_id=seed.revision_id, artifact_id=seed.artifact_id, is_mandatory=True),
            ),
            selected_directives=(
                SelectedDirective(
                    revision_id=seed.revision_id,
                    directive_id=seed.directive_id,
                    artifact_id=seed.artifact_id,
                    is_mandatory=True,
                    visibility_decision_id="vd-1",
                    source_locator="loc://a",
                    source_revision_locator="loc://a@1",
                    content_digest="e" * 64,
                    obligation_fields={},
                    context_handle_digest=hashlib.sha256(_HANDLE.encode()).hexdigest(),
                ),
            ),
        )
        await consume_challenge(session, challenge_id)
    return receipt_id


async def _set_lifecycle_state(
    factory: async_sessionmaker[AsyncSession], revision_id: uuid.UUID, *, state: str
) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE arc_revisions SET lifecycle_state = :state, "
                "  revoked_at = CASE WHEN :state = 'revoked' THEN :now ELSE revoked_at END "
                "WHERE revision_id = :rid"
            ),
            {"rid": revision_id, "state": state, "now": ARC_NOW},
        )


# --- the individual scenarios, named so a failure is immediately legible ---------------


@pytest.mark.asyncio
async def test_a_revoked_revision_denies_detail_retrieval(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed_stale: ArcSeed
) -> None:
    """The positive case: revocation after the receipt was written must deny."""
    receipt_id = await _seed_receipt_citing_the_seeded_directive(factory, receipts, seed_stale)
    await _set_lifecycle_state(factory, seed_stale.revision_id, state="revoked")

    with pytest.raises(DetailDenied) as exc:
        await jit.retrieve(_ctx(seed_stale), _request(receipt_id))
    assert exc.value.reason_code == DENIED_REVOKED


@pytest.mark.asyncio
async def test_an_active_revision_still_serves_detail(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed_stale: ArcSeed
) -> None:
    """The negative control: nothing changed, so detail must still be served.

    Without this, the harness could report a perfect denial rate merely
    because it always expects a denial -- this proves the check is not
    vacuously true.
    """
    receipt_id = await _seed_receipt_citing_the_seeded_directive(factory, receipts, seed_stale)

    page = await jit.retrieve(_ctx(seed_stale), _request(receipt_id))
    assert page.complete is True
    assert len(page.items) == 1


@pytest.mark.asyncio
async def test_an_expired_but_not_revoked_revision_still_serves_detail(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService, seed_stale: ArcSeed
) -> None:
    """'expired' governs on until an operator revokes it; it is not itself a denial trigger.

    This is the distinction called out in the module docstring: only
    'revoked' denies. Folding 'expired' into the same bucket as 'revoked'
    would silently release an obligation whose review lapsed rather than
    surfacing it -- exactly the failure corpus.py's own comment says this
    lifecycle split exists to avoid.
    """
    receipt_id = await _seed_receipt_citing_the_seeded_directive(factory, receipts, seed_stale)
    await _set_lifecycle_state(factory, seed_stale.revision_id, state="expired")

    page = await jit.retrieve(_ctx(seed_stale), _request(receipt_id))
    assert page.complete is True
    assert len(page.items) == 1


# --- the measured metric ------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Scenario:
    """One (lifecycle transition, expected outcome) pair the metric sweeps over."""

    scenario_id: str
    lifecycle_state: str | None  # None means "leave it active, no transition"
    must_be_denied: bool


_SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario("revoked-after-resolution", "revoked", must_be_denied=True),
    _Scenario("still-active", None, must_be_denied=False),
    _Scenario("expired-not-revoked", "expired", must_be_denied=False),
)


@pytest.mark.asyncio
async def test_stale_receipt_denial_rate_is_1_0(
    factory: async_sessionmaker[AsyncSession], receipts: ReceiptService, jit: JitService
) -> None:
    """Of the receipts that cite a since-revoked revision, what fraction are denied.

    Each scenario gets its own seeded tenant/artifact/revision/receipt so the
    lifecycle mutation in one scenario cannot bleed into another. On failure
    this lists the exact scenario_ids that produced the wrong outcome, not
    just a shifted fraction.
    """
    denied_correctly = 0
    should_have_denied = 0
    wrong_scenarios: list[str] = []

    for scenario in _SCENARIOS:
        scenario_seed = await seed_arc(factory, slug_prefix=f"arc-rate-{scenario.scenario_id}")
        receipt_id = await _seed_receipt_citing_the_seeded_directive(factory, receipts, scenario_seed)
        if scenario.lifecycle_state is not None:
            await _set_lifecycle_state(factory, scenario_seed.revision_id, state=scenario.lifecycle_state)

        denied = False
        try:
            await jit.retrieve(_ctx(scenario_seed), _request(receipt_id))
        except DetailDenied as exc:
            denied = True
            if exc.reason_code != DENIED_REVOKED:
                wrong_scenarios.append(f"{scenario.scenario_id} (denied for {exc.reason_code}, not revocation)")
                continue

        if scenario.must_be_denied:
            should_have_denied += 1
            if denied:
                denied_correctly += 1
            else:
                wrong_scenarios.append(f"{scenario.scenario_id} (should have been denied, was served)")
        elif denied:
            wrong_scenarios.append(f"{scenario.scenario_id} (should have been served, was denied)")

    rate = denied_correctly / should_have_denied if should_have_denied else 1.0
    assert rate == _STALE_RECEIPT_DENIAL_THRESHOLD and not wrong_scenarios, (
        f"stale-receipt denial rate = {rate:.3f} ({denied_correctly}/{should_have_denied}); "
        f"scenarios that behaved unexpectedly: {wrong_scenarios}"
    )
