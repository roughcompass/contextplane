"""A withdrawn source reaching the claim derived from it, against a real database.

The unit suite proves the decisions. This one proves the thing they add up to and
nothing smaller can: that registering an attempt as a derivative of every record it
read is what lets a revoked signal's propagation find it at all, and that when the
drain applies that work the quotations are gone from every table while the rows
somebody would audit are still there.

Three properties need real Postgres rather than a fake. Whether the excerpt columns are
empty is a statement about columns. Whether the claim still serves is a statement about
a query that filters on four of them at once. And whether the shell survives is a
statement about what the CHECK constraints permitted the reduction to write — the
`rejected` shape is legal for a linked claim and for an unlinked one, and only the
database can confirm the reduction did not quietly need a constraint it does not have.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from contextplane.retention import derivatives, policies
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION
from contextplane.service.memory.claim_serving import ClaimServingService
from contextplane.service.memory.derivation import (
    Assertion,
    DerivationProfile,
    DerivationService,
    Evidence,
)
from contextplane.service.memory.derivative_handlers import (
    AUDIENCE_PARTITION,
    CLAIM_STATUS_CLOSED,
    STATUS_INVALIDATED,
    ClaimDerivativeHandler,
    locator_for,
)
from contextplane.types import SystemClock, TenantContext
from contextplane.workers.derivative_propagation import DerivativePropagationWorker, PropagationReport

_PROFILE = DerivationProfile(name="outcome-extractor", version="2.0.0")
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

#: The person's own sentence, carried into the attempt and into the claim's citations.
#: What the reduction has to remove from both.
_QUOTED = "I told the on-call that the failover runbook was wrong"


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


@pytest.fixture
def tenant_id(sync_engine: Engine) -> uuid.UUID:
    """One tenant, with the retention policy row a tombstone's foreign key needs.

    Seeded here rather than assumed: nothing in the tree projects `retention/policies.py`
    into `retention_policies` yet, and `source_tombstones` holds a foreign key into it,
    so a tombstone cannot be written at all without this row. The projection is somebody
    else's task; this test needs the rows to exist, not to exist by that route.
    """
    tid = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'claim derivative test')"),
            {"t": tid, "s": f"cd-{tid.hex[:8]}"},
        )
        _seed_retention_policy(conn, policies.RECORD_EXTERNAL_SIGNAL)
    return tid


def _seed_retention_policy(conn: Connection, record_class: str) -> None:
    disposition = policies.disposition(record_class)
    conn.execute(
        text(
            "INSERT INTO retention_policies (policy_version, record_class, legal_basis, retention_days,"
            " erasure_mode, minimization_action, tombstone_behaviour, verifier_disclosure)"
            " VALUES (:v, :cls, :basis, :days, :mode, :action, :tomb, :disclosure)"
            " ON CONFLICT DO NOTHING"
        ),
        {
            "v": policies.POLICY_VERSION,
            "cls": record_class,
            "basis": disposition.legal_basis,
            "days": disposition.retention_days,
            "mode": disposition.erasure_mode,
            "action": disposition.minimization_action,
            "tomb": disposition.tombstone_behaviour,
            "disclosure": disposition.verifier_disclosure,
        },
    )


def _seed_signal(conn: Connection, tenant_id: uuid.UUID) -> uuid.UUID:
    signal_id = uuid.uuid4()
    unique = uuid.uuid4().hex[:12]
    conn.execute(
        text(
            "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id, producer_type,"
            " source_event_id, idempotency_key, content_digest, authority, classification, schema_version,"
            " payload, ingested_at)"
            " VALUES (:s, :t, 'github-actions', 'signal-producer:test', 'external', :ev, :idk, :dig,"
            " 'github-actions:workflow-conclusion', 'internal', 'external_signal.v1',"
            " CAST(:pl AS JSONB), :ingested)"
        ),
        {
            "s": signal_id,
            "t": tenant_id,
            "ev": f"evt-{unique}",
            "idk": f"idk-{unique}",
            "dig": f"sha256:{unique}",
            "pl": json.dumps({"conclusion": "failure"}),
            "ingested": _NOW,
        },
    )
    return signal_id


def _seed_claim(conn: Connection, tenant_id: uuid.UUID, *, signal_id: uuid.UUID) -> uuid.UUID:
    """A staged, consolidated, scored claim with one citation carrying the quotation.

    Everything the serving query filters on is satisfied on purpose: a claim that was
    never servable would make "it serves nothing afterwards" true for the wrong reason.
    """
    actor_id, entity_id, claim_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at)"
            " VALUES (:a, :t, 'author', :sub, :now)"
        ),
        {"a": actor_id, "t": tenant_id, "sub": f"cd-{actor_id.hex[:12]}", "now": _NOW},
    )
    conn.execute(
        text("INSERT INTO entities (entity_id, tenant_id, entity_type, name) VALUES (:e, :t, 'capability', :n)"),
        {"e": entity_id, "t": tenant_id, "n": f"cap-{entity_id.hex[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO memory_claims ("
            "  claim_id, owning_tenant_id, author_tenant_id, author_actor_id, subject_entity_id,"
            "  subject_reference, predicate, value_type, claim_category, value_jsonb,"
            "  asserted_valid_from, status, visibility, source_authority, size_bytes,"
            "  consolidated_at, created_at, confidence, confidence_scored_at, confidence_inputs,"
            "  scorer_version, calibration_version, decay_half_life_days"
            ") VALUES ("
            "  :cid, :t, :t, :a, :e, 'subject-ref', 'observed_behavior', 'prose',"
            "  'operational_lifecycle', CAST(:val AS JSONB), :now, 'staged', 'private',"
            "  'observer_extraction', 9, :now, :now, 0.700, :now, CAST(:inputs AS JSONB),"
            "  'scorer.v1', 'calib.v1', 30"
            ")"
        ),
        {
            "cid": claim_id,
            "t": tenant_id,
            "a": actor_id,
            "e": entity_id,
            "val": json.dumps("the failover runbook is wrong"),
            "now": _NOW,
            "inputs": json.dumps({"seed": True}),
        },
    )
    conn.execute(
        text(
            "INSERT INTO memory_claim_provenance (claim_id, evidence_kind, evidence_ref, evidence_excerpt)"
            " VALUES (:cid, 'connector_run', :ref, :excerpt)"
        ),
        {"cid": claim_id, "ref": f"signal:{signal_id}", "excerpt": _QUOTED},
    )
    return claim_id


def _ctx(tenant_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["producer"])


def _run(pg_container: str, work: Any) -> Any:
    """Run one coroutine factory against a fresh async engine and dispose of it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def go() -> Any:
        engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await work(factory)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _derive(pg_container: str, ctx: TenantContext, *, signal_id: uuid.UUID, excerpt: str | None = _QUOTED) -> Any:
    return _run(
        pg_container,
        lambda factory: DerivationService(factory, clock=SystemClock()).derive(
            ctx,
            profile=_PROFILE,
            assertion=Assertion(
                subject_reference="capability:failover",
                predicate="context_was_incorrect",
                value={"observed": "the runbook named a removed step"},
                applicability="repo:roughcompass/contextplane",
            ),
            evidence=[
                Evidence(
                    kind="signal",
                    source_authority=AUTHORITY_OBSERVER_EXTRACTION,
                    classification="internal",
                    signal_id=signal_id,
                    excerpt=excerpt,
                )
            ],
        ),
    )


def _drain(pg_container: str) -> PropagationReport:
    registry = derivatives.HandlerRegistry()
    registry.register(ClaimDerivativeHandler())
    return _run(pg_container, lambda factory: DerivativePropagationWorker(factory, registry).run_once())


def _serve(pg_container: str, ctx: TenantContext, claim_id: uuid.UUID) -> Any:
    return _run(
        pg_container,
        lambda factory: ClaimServingService(factory, clock=SystemClock()).get(ctx, claim_id),
    )


def _plant_revocation(
    conn: Connection, tenant_id: uuid.UUID, *, signal_id: uuid.UUID, derivative_id: uuid.UUID
) -> None:
    """Withdraw the signal and queue the work its withdrawal owes.

    Planted rather than enqueued through the source's own revocation path: that path
    belongs to the signal subsystem and lands separately. What this test is about is
    what happens once the item exists, and planting it is what keeps the two changes
    independently verifiable.
    """
    tombstone_id = uuid.uuid4()
    conn.execute(
        text("UPDATE external_signals SET revoked_at = :now WHERE signal_id = :s"),
        {"now": _NOW, "s": signal_id},
    )
    conn.execute(
        text(
            "INSERT INTO source_tombstones (tombstone_id, tenant_id, record_class, subject_id, policy_version,"
            " request_authority, reason, effective_at, proof_hmac)"
            " VALUES (:id, :t, :cls, :subject, :v, 'operator', :reason, :now, 'hmac-placeholder')"
        ),
        {
            "id": tombstone_id,
            "t": tenant_id,
            "cls": policies.RECORD_EXTERNAL_SIGNAL,
            "subject": signal_id,
            "v": policies.POLICY_VERSION,
            "reason": derivatives.TRIGGER_REVOCATION,
            "now": _NOW,
        },
    )
    conn.execute(
        text(
            "INSERT INTO derivative_work_outbox (tenant_id, derivative_id, operation, trigger, tombstone_id,"
            " available_at) VALUES (:t, :d, :op, :trigger, :tomb, :now)"
        ),
        {
            "t": tenant_id,
            "d": derivative_id,
            "op": derivatives.OPERATION_DELETE,
            "trigger": derivatives.TRIGGER_REVOCATION,
            "tomb": tombstone_id,
            "now": _NOW,
        },
    )


def _registration(conn: Connection, tenant_id: uuid.UUID, derivation_id: uuid.UUID) -> Any:
    return conn.execute(
        text(
            "SELECT derivative_id, derivative_kind, audience_partition, blocking, expires_at,"
            " delete_handler_version, redact_handler_version"
            " FROM derivative_registrations WHERE tenant_id = :t AND storage_locator = :loc"
        ),
        {"t": tenant_id, "loc": locator_for(derivation_id)},
    ).one_or_none()


# --- the registrar ------------------------------------------------------------


def test_a_stored_attempt_is_registered_as_a_derivative_of_the_records_it_read(
    sync_engine: Engine, pg_container: str, tenant_id: uuid.UUID
) -> None:
    """Without this row nothing connects the attempt to the signal, and an erasure of
    the signal sweeps every artefact except the one holding the quotation."""
    with sync_engine.begin() as conn:
        signal_id = _seed_signal(conn, tenant_id)

    recorded = _derive(pg_container, _ctx(tenant_id), signal_id=signal_id)

    with sync_engine.begin() as conn:
        registration = _registration(conn, tenant_id, recorded.derivation_id)
        assert registration is not None
        assert registration.derivative_kind == derivatives.KIND_CLAIM_DERIVATIVE
        assert registration.audience_partition == AUDIENCE_PARTITION
        # A claim built from a withdrawn source is exactly the read that must fail
        # closed while its propagation is outstanding.
        assert registration.blocking is True
        assert registration.delete_handler_version == registration.redact_handler_version

        links = conn.execute(
            text(
                "SELECT source_record_class, source_id, source_expires_at FROM derivative_source_links"
                " WHERE derivative_id = :d"
            ),
            {"d": registration.derivative_id},
        ).all()
    assert [(row.source_record_class, row.source_id) for row in links] == [(policies.RECORD_EXTERNAL_SIGNAL, signal_id)]
    # Copied from the source's own clock at registration, not recomputed later: the
    # classes store their anchors in as many places as there are classes.
    assert links[0].source_expires_at == policies.expiry_deadline(policies.RECORD_EXTERNAL_SIGNAL, _NOW)


def test_the_registration_expires_no_later_than_the_claim_classes_payload_clock(
    sync_engine: Engine, pg_container: str, tenant_id: uuid.UUID
) -> None:
    """The excerpts an attempt holds quote a source payload, and the approved policy
    reduces those on a clock of their own — earlier, here, than the signal's own
    retention, so the fallback is a ceiling rather than a default."""
    with sync_engine.begin() as conn:
        signal_id = _seed_signal(conn, tenant_id)

    recorded = _derive(pg_container, _ctx(tenant_id), signal_id=signal_id)

    with sync_engine.begin() as conn:
        registration = _registration(conn, tenant_id, recorded.derivation_id)
    assert registration is not None
    assert registration.expires_at < policies.expiry_deadline(policies.RECORD_EXTERNAL_SIGNAL, _NOW)


def test_a_replay_registers_nothing_new(sync_engine: Engine, pg_container: str, tenant_id: uuid.UUID) -> None:
    """The same conclusion twice is one attempt, so it is one artefact and one
    registration. A second registration would be a second row the erasure has to find,
    and the one it missed would be whichever the sweep did not reach."""
    with sync_engine.begin() as conn:
        signal_id = _seed_signal(conn, tenant_id)

    first = _derive(pg_container, _ctx(tenant_id), signal_id=signal_id)
    second = _derive(pg_container, _ctx(tenant_id), signal_id=signal_id)
    assert second.replayed is True
    assert second.derivation_id == first.derivation_id

    with sync_engine.begin() as conn:
        registrations = conn.execute(
            text("SELECT count(*) FROM derivative_registrations WHERE tenant_id = :t"),
            {"t": tenant_id},
        ).scalar_one()
        links = conn.execute(
            text(
                "SELECT count(*) FROM derivative_source_links l"
                " JOIN derivative_registrations r ON r.derivative_id = l.derivative_id"
                " WHERE r.tenant_id = :t"
            ),
            {"t": tenant_id},
        ).scalar_one()
    assert registrations == 1
    assert links == 1


# --- the reduction, end to end ------------------------------------------------


def test_revoking_a_signal_invalidates_the_claim_derived_from_it(
    sync_engine: Engine, pg_container: str, tenant_id: uuid.UUID
) -> None:
    """The whole path: register at derivation, withdraw the source, drain the queue.

    Four assertions afterwards, and they are four rather than one because they can fail
    independently — the quotations can go while the claim keeps serving, and the claim
    can stop serving while the row is destroyed.
    """
    ctx = _ctx(tenant_id)
    with sync_engine.begin() as conn:
        signal_id = _seed_signal(conn, tenant_id)
        claim_id = _seed_claim(conn, tenant_id, signal_id=signal_id)

    recorded = _derive(pg_container, ctx, signal_id=signal_id)

    with sync_engine.begin() as conn:
        # The link a staging path will write. Nothing in the tree writes it yet, so the
        # claim half of the reduction is set up here rather than produced.
        conn.execute(
            text("UPDATE claim_derivations SET status = 'staged', created_claim_id = :c WHERE derivation_id = :d"),
            {"c": claim_id, "d": recorded.derivation_id},
        )
        registration = _registration(conn, tenant_id, recorded.derivation_id)
        assert registration is not None

    assert _serve(pg_container, ctx, claim_id) is not None, "the claim has to be servable before it stops being"

    with sync_engine.begin() as conn:
        _plant_revocation(conn, tenant_id, signal_id=signal_id, derivative_id=registration.derivative_id)

    report = _drain(pg_container)
    assert report.applied == 1
    assert report.failed == 0
    assert report.artefacts > 0

    with sync_engine.begin() as conn:
        excerpts = (
            conn.execute(
                text("SELECT excerpt FROM derivation_evidence_links WHERE derivation_id = :d"),
                {"d": recorded.derivation_id},
            )
            .scalars()
            .all()
        )
        citations = conn.execute(
            text(
                "SELECT evidence_kind, evidence_ref, evidence_excerpt FROM memory_claim_provenance WHERE claim_id = :c"
            ),
            {"c": claim_id},
        ).all()
        attempt = conn.execute(
            text("SELECT status FROM claim_derivations WHERE derivation_id = :d"),
            {"d": recorded.derivation_id},
        ).scalar_one()
        claim = conn.execute(
            text("SELECT status, value_jsonb, t_invalidated_at FROM memory_claims WHERE claim_id = :c"),
            {"c": claim_id},
        ).one()

    assert excerpts == [None], "the attempt's evidence link still quotes the withdrawn signal"
    assert [row.evidence_excerpt for row in citations] == [None]
    # The link survives without its quotation: what the derivation read is still
    # answerable, which is what makes the shell worth retaining.
    assert [(row.evidence_kind, row.evidence_ref) for row in citations] == [("connector_run", f"signal:{signal_id}")]
    assert attempt == STATUS_INVALIDATED
    assert claim.status == CLAIM_STATUS_CLOSED
    assert claim.t_invalidated_at is None
    assert claim.value_jsonb is not None, "the shell is retained for audit, not destroyed"
    assert _serve(pg_container, ctx, claim_id) is None


def test_a_second_propagation_for_the_same_attempt_finds_nothing_left_to_do(
    sync_engine: Engine, pg_container: str, tenant_id: uuid.UUID
) -> None:
    """Idempotence, at the level that matters: a retried or re-triggered item reduces an
    already-reduced attempt to itself. A reduction that counted work every time would
    make the drain's own report useless for telling a retry from a second erasure."""
    ctx = _ctx(tenant_id)
    with sync_engine.begin() as conn:
        signal_id = _seed_signal(conn, tenant_id)
        claim_id = _seed_claim(conn, tenant_id, signal_id=signal_id)

    recorded = _derive(pg_container, ctx, signal_id=signal_id)
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE claim_derivations SET status = 'staged', created_claim_id = :c WHERE derivation_id = :d"),
            {"c": claim_id, "d": recorded.derivation_id},
        )
        registration = _registration(conn, tenant_id, recorded.derivation_id)
        assert registration is not None
        _plant_revocation(conn, tenant_id, signal_id=signal_id, derivative_id=registration.derivative_id)

    assert _drain(pg_container).artefacts > 0

    # A different cause for the same artefact: the expiry sweep arriving after the
    # revocation already reduced it. The schema admits it as a separate item, and the
    # handler has to report it as the no-op it is.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO derivative_work_outbox (tenant_id, derivative_id, operation, trigger, available_at)"
                " VALUES (:t, :d, :op, :trigger, :now)"
            ),
            {
                "t": tenant_id,
                "d": registration.derivative_id,
                "op": derivatives.OPERATION_REDACT,
                "trigger": derivatives.TRIGGER_EXPIRY,
                "now": _NOW,
            },
        )

    second = _drain(pg_container)
    assert second.applied == 1
    assert second.failed == 0
    assert second.artefacts == 0
    assert _serve(pg_container, ctx, claim_id) is None
