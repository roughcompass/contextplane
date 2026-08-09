"""Derivation attempts as they actually land, against a real database.

The unit suite proves the decisions; this one proves the storage — that an attempt
and its evidence links persist together, that the same conclusion twice is one
attempt, and that what reaches the tables is an excerpt and a pointer rather than
a copy of anything.

The last of those is the property worth running against Postgres rather than a
fake: "no workspace body or checkpoint payload was copied" is a statement about
what is in the columns, and only the columns can answer it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from contextplane.service.governance.authority import (
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OWNER_HUMAN,
)
from contextplane.service.memory.derivation import (
    STATUS_PENDING,
    Assertion,
    DerivationProfile,
    DerivationRefused,
    DerivationService,
    Evidence,
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
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'derivation test')"),
            {"t": tid, "s": f"dv-{tid.hex[:8]}"},
        )
    return tid


def _ctx(tenant_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["producer"])


def _derive(pg_container: str, ctx: TenantContext, **kwargs: Any) -> Any:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def run() -> Any:
        engine = create_async_engine(pg_container)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await DerivationService(factory, clock=SystemClock()).derive(ctx, **kwargs)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _assertion(**overrides: Any) -> Assertion:
    fields: dict[str, Any] = {
        "subject_reference": f"capability:{uuid.uuid4().hex[:8]}",
        "predicate": "context_was_stale",
        "value": {"observed": "the runbook referenced a removed step"},
        "applicability": "repo:roughcompass/contextplane",
    }
    fields.update(overrides)
    return Assertion(**fields)


def _seed_signal(engine: Engine, tenant_id: uuid.UUID, *, superseded: bool = False) -> uuid.UUID:
    """One real signal row for evidence to point at.

    Invented ids do not work here, and that is the schema doing its job: an
    evidence link naming a signal that does not exist is evidence of nothing, and
    the foreign key refuses it.
    """
    signal_id = uuid.uuid4()
    unique = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id, producer_type,"
                " source_event_id, idempotency_key, content_digest, authority, classification, schema_version,"
                " payload, superseded_for_learning)"
                " VALUES (:s, :t, 'github-actions', 'signal-producer:test', 'external', :ev, :idk, :dig,"
                " 'github-actions:workflow-conclusion', 'internal', 'external_signal.v1',"
                " CAST(:pl AS JSONB), :sup)"
            ),
            {
                "s": signal_id,
                "t": tenant_id,
                "ev": f"github:workflow_run:test:{unique}:1",
                "idk": f"delivery-{unique}",
                "dig": f"sha256:{unique}",
                "pl": '{"conclusion": "failure"}',
                "sup": superseded,
            },
        )
    return signal_id


def _signal_evidence(
    engine: Engine,
    tenant_id: uuid.UUID,
    authority: str = AUTHORITY_OBSERVER_EXTRACTION,
    **overrides: Any,
) -> Evidence:
    fields: dict[str, Any] = {
        "kind": "signal",
        "source_authority": authority,
        "classification": "internal",
        "signal_id": _seed_signal(engine, tenant_id, superseded=bool(overrides.get("superseded_for_learning"))),
    }
    fields.update(overrides)
    return Evidence(**fields)


def test_an_attempt_and_its_evidence_persist_together(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    ctx = _ctx(tenant_id)
    recorded = _derive(
        pg_container,
        ctx,
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[
            _signal_evidence(sync_engine, tenant_id, AUTHORITY_OWNER_HUMAN),
            _signal_evidence(sync_engine, tenant_id, AUTHORITY_OBSERVER_EXTRACTION, excerpt="step 4 no longer exists"),
        ],
    )
    assert recorded.replayed is False
    assert recorded.evidence_count == 2

    with sync_engine.connect() as conn:
        attempt = conn.execute(
            text("SELECT status, source_authority FROM claim_derivations WHERE derivation_id = :d"),
            {"d": recorded.derivation_id},
        ).one()
        links = conn.execute(
            text("SELECT count(*) FROM derivation_evidence_links WHERE derivation_id = :d"),
            {"d": recorded.derivation_id},
        ).scalar_one()

    # Stored pending: an extractor that staged its own output would be approving
    # its own work.
    assert attempt.status == STATUS_PENDING
    # The ceiling, not the strongest input.
    assert attempt.source_authority == AUTHORITY_OBSERVER_EXTRACTION
    assert links == 2


def test_the_stored_authority_is_the_weakest_source_not_the_strongest(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """The rule SQL cannot hold, checked where it actually lands.

    An owner-human input beside an observer-extraction one produces an attempt
    carrying the weaker of the two, because that is all the evidence licenses.
    """
    ctx = _ctx(tenant_id)
    recorded = _derive(
        pg_container,
        ctx,
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[
            _signal_evidence(sync_engine, tenant_id, AUTHORITY_OWNER_HUMAN),
            _signal_evidence(sync_engine, tenant_id, AUTHORITY_OBSERVER_HUMAN),
        ],
    )
    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT source_authority FROM claim_derivations WHERE derivation_id = :d"),
            {"d": recorded.derivation_id},
        ).scalar_one()
    assert stored == AUTHORITY_OBSERVER_HUMAN


def test_an_attempt_claiming_more_than_its_evidence_stores_nothing(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Refused before any write: a rejected assertion leaves no attempt behind."""
    ctx = _ctx(tenant_id)
    assertion = _assertion()
    with pytest.raises(DerivationRefused):
        _derive(
            pg_container,
            ctx,
            profile=_PROFILE,
            assertion=assertion,
            evidence=[_signal_evidence(sync_engine, tenant_id, AUTHORITY_OBSERVER_EXTRACTION)],
            claimed_authority=AUTHORITY_OWNER_HUMAN,
        )
    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT count(*) FROM claim_derivations WHERE tenant_id = :t AND applicability = :a"),
            {"t": tenant_id, "a": assertion.applicability},
        ).scalar_one()
    assert stored == 0


def test_the_same_conclusion_twice_is_one_attempt(pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    ctx = _ctx(tenant_id)
    assertion = _assertion()

    def run() -> Any:
        return _derive(
            pg_container,
            ctx,
            profile=_PROFILE,
            assertion=assertion,
            evidence=[_signal_evidence(sync_engine, tenant_id)],
        )

    first, second = run(), run()
    assert second.replayed is True
    assert second.derivation_id == first.derivation_id

    with sync_engine.connect() as conn:
        attempts = conn.execute(
            text("SELECT count(*) FROM claim_derivations WHERE assertion_digest = :d"),
            {"d": first.assertion_digest},
        ).scalar_one()
    assert attempts == 1


def test_a_later_extractor_version_records_its_own_attempt(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """An upgrade is not a replay: the version is part of the identity."""
    ctx = _ctx(tenant_id)
    assertion = _assertion()
    first = _derive(
        pg_container, ctx, profile=_PROFILE, assertion=assertion, evidence=[_signal_evidence(sync_engine, tenant_id)]
    )
    later = _derive(
        pg_container,
        ctx,
        profile=DerivationProfile(name=_PROFILE.name, version="1.5.0"),
        assertion=assertion,
        evidence=[_signal_evidence(sync_engine, tenant_id)],
    )
    assert later.replayed is False
    assert later.derivation_id != first.derivation_id


def test_a_derivation_writes_no_claim(pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """The one-writer rule survives extraction.

    This service produces what the claim path would need and records that it did;
    staging a claim is that path's decision, made with its own evidence rules.
    """
    ctx = _ctx(tenant_id)
    before = _claim_count(sync_engine, tenant_id)
    recorded = _derive(
        pg_container, ctx, profile=_PROFILE, assertion=_assertion(), evidence=[_signal_evidence(sync_engine, tenant_id)]
    )
    assert _claim_count(sync_engine, tenant_id) == before

    with sync_engine.connect() as conn:
        created = conn.execute(
            text("SELECT created_claim_id FROM claim_derivations WHERE derivation_id = :d"),
            {"d": recorded.derivation_id},
        ).scalar_one()
    assert created is None


def test_no_body_or_payload_column_exists_to_copy_into(sync_engine: Engine) -> None:
    """The absence is the design, so it is asserted rather than left to review.

    A column for a workspace body or a full checkpoint payload would make copying
    the path of least resistance for the next extractor, and it would look
    entirely reasonable in the diff that added it.
    """
    columns = {c["name"] for c in inspect(sync_engine).get_columns("derivation_evidence_links")}
    forbidden = {"workspace_id", "workspace_body", "body", "content", "payload", "checkpoint_payload", "entry_body"}
    assert not (columns & forbidden), f"a copy path reached derivation evidence: {sorted(columns & forbidden)}"
    assert "excerpt" in columns


def test_only_a_bounded_excerpt_reaches_storage(pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID) -> None:
    """What lands is a quotation and a pointer, never the thing quoted."""
    ctx = _ctx(tenant_id)
    excerpt = "step 4 refers to a runbook section that was deleted"
    recorded = _derive(
        pg_container,
        ctx,
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[
            Evidence(
                kind="checkpoint",
                source_authority=AUTHORITY_OWNER_HUMAN,
                classification="internal",
                checkpoint_id=uuid.uuid4(),
                checkpoint_digest="sha256:" + "a" * 64,
                excerpt=excerpt,
            )
        ],
    )
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT excerpt, checkpoint_id, checkpoint_digest FROM derivation_evidence_links"
                " WHERE derivation_id = :d"
            ),
            {"d": recorded.derivation_id},
        ).one()
    assert row.excerpt == excerpt
    assert row.checkpoint_id is not None
    assert row.checkpoint_digest is not None


def test_an_attempt_with_no_evidence_is_refused(pg_container: str, tenant_id: uuid.UUID) -> None:
    with pytest.raises(DerivationRefused, match="no evidence"):
        _derive(pg_container, _ctx(tenant_id), profile=_PROFILE, assertion=_assertion(), evidence=[])


def test_an_attempt_on_wholly_superseded_evidence_is_still_recorded(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Recorded and flagged rather than dropped.

    The derivation was made; losing that record would hide it. What it must not do
    is support promotion, which `may_promote` refuses on this shape.
    """
    ctx = _ctx(tenant_id)
    recorded = _derive(
        pg_container,
        ctx,
        profile=_PROFILE,
        assertion=_assertion(),
        evidence=[_signal_evidence(sync_engine, tenant_id, superseded_for_learning=True)],
    )
    assert recorded.superseded_only is True
    with sync_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT count(*) FROM claim_derivations WHERE derivation_id = :d"),
            {"d": recorded.derivation_id},
        ).scalar_one()
    assert exists == 1


def _claim_count(engine: Engine, tenant_id: uuid.UUID) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM memory_claims WHERE author_tenant_id = :t"),
                {"t": tenant_id},
            ).scalar_one()
        )
