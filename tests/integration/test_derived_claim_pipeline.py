"""What a derivation is allowed to read, and what a derived claim may carry.

Two obligations converge here, both carried forward from earlier tasks because
neither could be discharged where it was found.

The first is eligibility. The derivation service validates evidence it is handed
and cannot know where the caller got it, so the filter has to live in whatever
assembles the chain — and the test that matters is not "eligible rows are
selected" but **"an ineligible row never reaches a derivation attempt"**. The
second is the authority ceiling, which no CHECK can express because the ordering
lives in the governance ladder rather than in the database.

Run against Postgres because both are properties of what the queries return, and
a fake would let the predicate agree with itself.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from contextplane.service.governance.authority import (
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OWNER_HUMAN,
)
from contextplane.service.memory.derivation import (
    Assertion,
    DerivationProfile,
    DerivationRefused,
    DerivationService,
    Evidence,
)
from contextplane.service.memory.evidence import (
    EvidenceAssembler,
    EvidenceRefused,
    as_provenance,
    ceiling_for,
    validate_chain,
)
from contextplane.types import SystemClock, TenantContext

_PROFILE = DerivationProfile(name="outcome-extractor", version="1.4.0")


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


@pytest.fixture
def tenant_id(sync_engine: Engine) -> uuid.UUID:
    tid = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'derived claim test')"),
            {"t": tid, "s": f"dc-{tid.hex[:8]}"},
        )
    return tid


def _ctx(tenant_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["producer"])


def _run(pg_container: str, coro_factory: Any) -> Any:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def run() -> Any:
        engine = create_async_engine(pg_container)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await coro_factory(factory)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _seed_receipt(engine: Engine, tenant_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    receipt_id = uuid.uuid4()
    item_id = f"item-{uuid.uuid4().hex[:10]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_receipts (receipt_id, tenant_id, state, cacheable, requested_by)"
                " VALUES (:r, :t, 'complete', TRUE, 'tester')"
            ),
            {"r": receipt_id, "t": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO context_receipt_items (receipt_id, receipt_item_id, block, source, item_key)"
                " VALUES (:r, :i, 'canonical', 'catalog', 'key-1')"
            ),
            {"r": receipt_id, "i": item_id},
        )
    return receipt_id, item_id


def _seed_feedback(
    engine: Engine,
    tenant_id: uuid.UUID,
    *,
    kind: str,
    learning_eligible: bool,
    receipt_id: uuid.UUID | None = None,
    receipt_item_id: str | None = None,
) -> uuid.UUID:
    feedback_id = uuid.uuid4()
    unique = uuid.uuid4().hex[:10]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_feedback (feedback_id, tenant_id, kind, receipt_id, receipt_item_id,"
                " rating, learning_eligible, reporter_id, reporter_type, idempotency_key, content_digest)"
                " VALUES (:f, :t, :k, :r, :i, 'stale', :elig, 'user:tester', 'human', :idk, :dig)"
            ),
            {
                "f": feedback_id,
                "t": tenant_id,
                "k": kind,
                "r": receipt_id,
                "i": receipt_item_id,
                "elig": learning_eligible,
                "idk": f"fb-{unique}",
                "dig": f"sha256:{unique}",
            },
        )
    return feedback_id


def _seed_signal(engine: Engine, tenant_id: uuid.UUID, *, revoked: bool = False, superseded: bool = False) -> uuid.UUID:
    signal_id = uuid.uuid4()
    unique = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id, producer_type,"
                " source_event_id, idempotency_key, content_digest, authority, classification, schema_version,"
                " payload, revoked_at, superseded_for_learning)"
                " VALUES (:s, :t, 'github-actions', 'p:test', 'external', :ev, :idk, :dig,"
                " 'github-actions:workflow-conclusion', 'internal', 'external_signal.v1',"
                ' CAST(\'{"conclusion": "failure"}\' AS JSONB), :rev, :sup)'
            ),
            {
                "s": signal_id,
                "t": tenant_id,
                "ev": f"github:workflow_run:x:{unique}:1",
                "idk": f"d-{unique}",
                "dig": f"sha256:{unique}",
                "rev": "2026-08-09T12:00:00+00:00" if revoked else None,
                "sup": superseded,
            },
        )
    return signal_id


# --- Eligibility: the carried obligation --------------------------------------


def test_only_learning_eligible_feedback_is_selected(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """The filter is a predicate, not a promise the caller makes."""
    receipt_id, item_id = _seed_receipt(sync_engine, tenant_id)
    eligible = _seed_feedback(
        sync_engine,
        tenant_id,
        kind="item_specific",
        learning_eligible=True,
        receipt_id=receipt_id,
        receipt_item_id=item_id,
    )
    withheld = _seed_feedback(
        sync_engine,
        tenant_id,
        kind="receipt_level",
        learning_eligible=False,
        receipt_id=receipt_id,
    )

    selected = _run(
        pg_container,
        lambda factory: EvidenceAssembler(factory).eligible_feedback(_ctx(tenant_id)),
    )
    ids = {row.feedback_id for row in selected}
    assert eligible in ids
    assert withheld not in ids, "feedback the reporter withheld from learning was selected anyway"


def test_a_diagnostic_observation_is_never_selected(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Excluded by the predicate as well as by the schema.

    The two fail differently: the schema stops a bad row existing, and this stops
    a bad row being *selected* if one ever does. An unattributable complaint must
    not become evidence about a specific retrieved item.
    """
    diagnostic = _seed_feedback(sync_engine, tenant_id, kind="diagnostic_observation", learning_eligible=False)
    selected = _run(
        pg_container,
        lambda factory: EvidenceAssembler(factory).eligible_feedback(_ctx(tenant_id)),
    )
    assert diagnostic not in {row.feedback_id for row in selected}


def test_another_tenants_feedback_is_never_selected(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    other = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'other')"),
            {"t": other, "s": f"ot-{other.hex[:8]}"},
        )
    # Their own receipt: receipt-level feedback must cite one, which the schema
    # enforces and this seed has to respect.
    their_receipt, _their_item = _seed_receipt(sync_engine, other)
    theirs = _seed_feedback(sync_engine, other, kind="receipt_level", learning_eligible=True, receipt_id=their_receipt)

    selected = _run(
        pg_container,
        lambda factory: EvidenceAssembler(factory).eligible_feedback(_ctx(tenant_id)),
    )
    assert theirs not in {row.feedback_id for row in selected}


def test_revoked_and_superseded_signals_are_not_eligible(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Different states, the same exclusion here and different meanings elsewhere.

    A revoked signal was withdrawn; a superseded one is still true but overtaken.
    Collapsing them would lose which happened.
    """
    live = _seed_signal(sync_engine, tenant_id)
    revoked = _seed_signal(sync_engine, tenant_id, revoked=True)
    superseded = _seed_signal(sync_engine, tenant_id, superseded=True)

    selected = set(_run(pg_container, lambda factory: EvidenceAssembler(factory).eligible_signals(_ctx(tenant_id))))
    assert live in selected
    assert revoked not in selected
    assert superseded not in selected


def test_an_ineligible_row_never_reaches_a_derivation_attempt(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """The obligation as stated, end to end.

    A withheld feedback row and a revoked signal both exist; the assembler selects
    neither, so a derivation built from what it returns cannot cite them. Asserted
    against the stored evidence links rather than the selection alone — the
    selection is the mechanism, but "never reaches an attempt" is the property.
    """
    receipt_id, _item = _seed_receipt(sync_engine, tenant_id)
    _seed_feedback(sync_engine, tenant_id, kind="receipt_level", learning_eligible=False, receipt_id=receipt_id)
    revoked_signal = _seed_signal(sync_engine, tenant_id, revoked=True)
    live_signal = _seed_signal(sync_engine, tenant_id)

    eligible = _run(pg_container, lambda factory: EvidenceAssembler(factory).eligible_signals(_ctx(tenant_id)))
    chain = [
        Evidence(
            kind="signal",
            source_authority=AUTHORITY_OBSERVER_EXTRACTION,
            classification="internal",
            signal_id=signal_id,
        )
        for signal_id in eligible
    ]
    recorded = _run(
        pg_container,
        lambda factory: DerivationService(factory, clock=SystemClock()).derive(
            _ctx(tenant_id),
            profile=_PROFILE,
            assertion=Assertion(
                subject_reference=f"capability:{uuid.uuid4().hex[:8]}",
                predicate="context_was_stale",
                value={"observed": "a step was missing"},
                applicability="repo:roughcompass/contextplane",
            ),
            evidence=chain,
        ),
    )

    with sync_engine.connect() as conn:
        cited = {
            row.signal_id
            for row in conn.execute(
                text("SELECT signal_id FROM derivation_evidence_links WHERE derivation_id = :d"),
                {"d": recorded.derivation_id},
            ).all()
        }
    assert live_signal in cited
    assert revoked_signal not in cited, "a revoked signal reached a derivation attempt"


# --- The chain, and what it licenses ------------------------------------------


def test_an_empty_chain_is_refused() -> None:
    with pytest.raises(EvidenceRefused, match="nothing in it"):
        validate_chain([])


def test_provenance_spells_a_receipt_item_as_the_pair() -> None:
    """The item id means nothing without the receipt it is on.

    A ref that cannot be resolved back is provenance in name only.
    """
    receipt_id = uuid.uuid4()
    provenance = as_provenance(
        [
            Evidence(
                kind="receipt_item",
                source_authority=AUTHORITY_OWNER_HUMAN,
                classification="internal",
                receipt_id=receipt_id,
                receipt_item_id="item-7",
            )
        ]
    )
    assert provenance[0].kind == "receipt_item"
    assert provenance[0].ref == f"receipt_item:{receipt_id}:item-7"


def test_provenance_carries_the_excerpt_and_nothing_more() -> None:
    excerpt = "step 4 refers to a deleted section"
    provenance = as_provenance(
        [
            Evidence(
                kind="checkpoint",
                source_authority=AUTHORITY_OWNER_HUMAN,
                classification="internal",
                checkpoint_id=uuid.uuid4(),
                checkpoint_digest="sha256:" + "b" * 64,
                excerpt=excerpt,
            )
        ]
    )
    assert provenance[0].excerpt == excerpt


def test_the_ceiling_is_the_same_number_the_extractor_computed() -> None:
    """One implementation of the ceiling, not two.

    Two would eventually disagree, and the disagreement would show up as a claim
    carrying an authority the other implementation would have refused.
    """
    chain = [
        Evidence(
            kind="signal", source_authority=AUTHORITY_OWNER_HUMAN, classification="internal", signal_id=uuid.uuid4()
        ),
        Evidence(
            kind="signal",
            source_authority=AUTHORITY_OBSERVER_EXTRACTION,
            classification="internal",
            signal_id=uuid.uuid4(),
        ),
    ]
    assert ceiling_for(chain) == AUTHORITY_OBSERVER_EXTRACTION


def test_a_derivation_from_an_empty_chain_is_refused(pg_container: str, tenant_id: uuid.UUID) -> None:
    with pytest.raises(DerivationRefused):
        _run(
            pg_container,
            lambda factory: DerivationService(factory, clock=SystemClock()).derive(
                _ctx(tenant_id),
                profile=_PROFILE,
                assertion=Assertion(
                    subject_reference="capability:x",
                    predicate="context_was_stale",
                    value={},
                    applicability="repo:x",
                ),
                evidence=[],
            ),
        )
