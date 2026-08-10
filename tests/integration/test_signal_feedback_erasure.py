"""Erasing signals and feedback against a real Postgres, bindings and all.

The unit suite fakes the session, so it proves the module *intends* the right
statements in the right order. It cannot prove the rows go, and for this table the
gap is the whole point: `context_reference_bindings` names its subject
polymorphically with no foreign key, so a delete that named the wrong subject type
succeeds, affects nothing, and reports success. Only a real row disappearing
distinguishes those.

So this suite plants signals through the real ingest service — which is what creates
the bindings, and what satisfies the CHECKs a hand-written INSERT would have to
guess at — then erases and looks.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.retention import holds, policies, tombstones
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION
from contextplane.service.memory.source_governance import SourceGovernanceService
from contextplane.signals import erasure
from contextplane.signals.envelope import ExternalSignalEnvelopeV1, normalize_references
from contextplane.signals.ingest import SUBJECT_EXTERNAL_SIGNAL, SignalIngestService
from contextplane.types import SystemClock, TenantContext

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)
_KEY_ID = "k1"

_REFERENCE: dict[str, Any] = {
    "source_system": "GitHub",
    "source_namespace": "roughcompass/contextplane",
    "kind": "pull_request",
    "external_id": "918",
    "classification": "internal",
    "external_authority": "platform-team",
}


class _FrozenClock:
    def now(self) -> datetime.datetime:
        return _NOW


def _salts() -> tombstones.KeyedTenantSalt:
    return tombstones.KeyedTenantSalt({_KEY_ID: b"\x07" * 32}, active_key_id=_KEY_ID)


@pytest_asyncio.fixture
async def erasure_fixture(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """A tenant, an actor, a declared source, and the real ingest service.

    The source is real because signal ingest checks its declared authority, and the
    actor is real so the ids under test are the ones a live path would produce. Note
    that neither signals nor feedback carry a foreign key to `actors`: both record who
    by the originator's own id, as text, which is exactly why the erasure predicate
    matches on that id and its origin type rather than on an actor column.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer", "admin"])
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'signal erasure')"),
                {"t": tenant_id, "s": f"sfe-{tenant_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'erasure-test-actor', :sub, now())"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name, "
                    "                          config, is_active, created_at) "
                    "VALUES (:sid, :tid, 'github', 'ci', '{}'::jsonb, TRUE, :now)"
                ),
                {"sid": source_id, "tid": tenant_id, "now": _NOW},
            )

        # `source_tombstones` has a foreign key to `retention_policies`, and NOTHING in
        # production populates that table: the approved dispositions live in Python, so
        # the rows the key points at have to be projected from them by something, and no
        # such writer exists yet. Seeded here so this suite tests erasure rather than
        # re-discovering that gap on every assertion.
        async with factory() as session, session.begin():
            for record_class in (policies.RECORD_EXTERNAL_SIGNAL, policies.RECORD_CONTEXT_FEEDBACK):
                disposition = policies.disposition(record_class)
                await session.execute(
                    text(
                        "INSERT INTO retention_policies (policy_version, record_class, legal_basis,"
                        "  retention_days, erasure_mode, minimization_action, tombstone_behaviour,"
                        "  verifier_disclosure)"
                        " VALUES (:v, :c, :basis, :days, :mode, :minimize, :tombstone, :disclosure)"
                        " ON CONFLICT DO NOTHING"
                    ),
                    {
                        "v": policies.POLICY_VERSION,
                        "c": record_class,
                        "basis": disposition.legal_basis,
                        "days": disposition.retention_days,
                        "mode": disposition.erasure_mode,
                        "minimize": disposition.minimization_action,
                        "tombstone": disposition.tombstone_behaviour,
                        "disclosure": disposition.verifier_disclosure,
                    },
                )

        governance = SourceGovernanceService(factory, clock=SystemClock())
        await governance.declare(ctx, source_id=source_id, authority_tier=AUTHORITY_OBSERVER_EXTRACTION)

        yield {
            "factory": factory,
            "service": SignalIngestService(factory, clock=SystemClock(), governance=governance),
            "ctx": ctx,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "source_id": source_id,
        }
    finally:
        await engine.dispose()


def _envelope(fixture: dict[str, Any], *, producer_type: str, producer_id: str) -> ExternalSignalEnvelopeV1:
    unique = uuid.uuid4().hex[:12]
    return ExternalSignalEnvelopeV1(
        source_id=fixture["source_id"],
        source_system="github",
        source_event_id=f"github:workflow_run:{unique}",
        producer_id=producer_id,
        producer_type=producer_type,
        idempotency_key=f"delivery-{unique}",
        classification="internal",
        schema_version="external_signal.v1",
        event_time=_NOW,
        observed_time=_NOW,
        references=normalize_references((_REFERENCE,)),
        payload={"conclusion": "success"},
    )


async def _plant_signal(fixture: dict[str, Any], *, producer_type: str, producer_id: str) -> uuid.UUID:
    """Ingest one signal through the real service and return its id."""
    stored = await fixture["service"].ingest(
        fixture["ctx"], _envelope(fixture, producer_type=producer_type, producer_id=producer_id)
    )
    return uuid.UUID(str(stored.signal_id))


async def _plant_feedback(fixture: dict[str, Any], *, note: str) -> uuid.UUID:
    """One diagnostic feedback row, which needs no receipt to exist."""
    feedback_id = uuid.uuid4()
    async with fixture["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_feedback "
                "  (feedback_id, tenant_id, kind, rating, learning_eligible, note, "
                "   reporter_id, reporter_type, idempotency_key, content_digest, created_at) "
                "VALUES (:f, :t, 'diagnostic_observation', 'irrelevant', FALSE, :note, "
                "        :rid, 'human', :idem, :dig, :now)"
            ),
            {
                "f": feedback_id,
                "t": fixture["tenant_id"],
                "note": note,
                # The reporter is the actor, recorded as text: there is no actor_id
                # column on this table.
                "rid": str(fixture["actor_id"]),
                "idem": f"fb-{feedback_id.hex[:12]}",
                "dig": f"sha256:{feedback_id.hex}",
                "now": _NOW,
            },
        )
    return feedback_id


async def _scalar(fixture: dict[str, Any], sql: str, params: dict[str, Any]) -> Any:
    async with fixture["factory"]() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _binding_count(fixture: dict[str, Any], signal_id: uuid.UUID) -> int:
    return int(
        await _scalar(
            fixture,
            "SELECT count(*) FROM context_reference_bindings "
            "WHERE tenant_id = :t AND subject_type = :st AND subject_id = :s",
            {"t": fixture["tenant_id"], "st": SUBJECT_EXTERNAL_SIGNAL, "s": signal_id},
        )
        or 0
    )


@pytest.mark.asyncio
async def test_erasing_an_actor_removes_their_signals_and_the_bindings_with_them(
    erasure_fixture: dict[str, Any],
) -> None:
    """The property a faked session cannot show. The binding table has no foreign key to
    its subject, so a surviving row is not merely untidy: a reverse lookup asking what
    cites this reference returns the erased signal's id, and the reference reads as
    still-cited by something that no longer exists.
    """
    fixture = erasure_fixture
    actor_id = fixture["actor_id"]
    signal_id = await _plant_signal(fixture, producer_type="human", producer_id=str(actor_id))

    # The binding exists before the erasure, or the assertion afterwards proves nothing.
    assert await _binding_count(fixture, signal_id) == 1

    counts = await erasure.SignalErasure(fixture["factory"], _salts(), clock=_FrozenClock()).erase_actor(
        fixture["ctx"], actor_id
    )

    assert counts["signals"] == 1
    assert await _binding_count(fixture, signal_id) == 0
    assert (
        await _scalar(
            fixture,
            "SELECT count(*) FROM external_signals WHERE tenant_id = :t AND signal_id = :s",
            {"t": fixture["tenant_id"], "s": signal_id},
        )
        == 0
    )
    # The reference itself survives: it is shared material, and another subject may
    # still cite it. Only the binding to this signal goes.
    assert (
        await _scalar(
            fixture,
            "SELECT count(*) FROM context_external_references WHERE tenant_id = :t",
            {"t": fixture["tenant_id"]},
        )
        or 0
    ) >= 1


@pytest.mark.asyncio
async def test_an_external_producers_signal_survives_an_actor_erasure(erasure_fixture: dict[str, Any]) -> None:
    """An `external` producer is a system, not a person. Erasing an actor whose id
    happens to match a producer id must not delete a vendor's feed."""
    fixture = erasure_fixture
    actor_id = fixture["actor_id"]
    external_signal = await _plant_signal(fixture, producer_type="external", producer_id=str(actor_id))

    counts = await erasure.SignalErasure(fixture["factory"], _salts(), clock=_FrozenClock()).erase_actor(
        fixture["ctx"], actor_id
    )

    assert counts["signals"] == 0
    assert await _binding_count(fixture, external_signal) == 1
    assert (
        await _scalar(
            fixture,
            "SELECT count(*) FROM external_signals WHERE signal_id = :s",
            {"s": external_signal},
        )
        == 1
    )


@pytest.mark.asyncio
async def test_erasure_writes_one_tombstone_per_signal_and_schedules_propagation(
    erasure_fixture: dict[str, Any],
) -> None:
    """The tombstone authorises the removal and the outbox carries it. Both land in the
    same commit, so a tombstone without scheduled work cannot exist."""
    fixture = erasure_fixture
    actor_id = fixture["actor_id"]
    first = await _plant_signal(fixture, producer_type="human", producer_id=str(actor_id))
    second = await _plant_signal(fixture, producer_type="agent", producer_id=str(actor_id))
    assert first != second

    await erasure.SignalErasure(fixture["factory"], _salts(), clock=_FrozenClock()).erase_actor(
        fixture["ctx"], actor_id
    )

    tombstoned = await _scalar(
        fixture,
        "SELECT count(*) FROM source_tombstones WHERE tenant_id = :t AND record_class = :c",
        {"t": fixture["tenant_id"], "c": policies.RECORD_EXTERNAL_SIGNAL},
    )
    assert tombstoned == 2
    # The proof is stored and is not the subject id in disguise, which is what makes it
    # a proof rather than a restatement of what was erased.
    proof = await _scalar(
        fixture,
        "SELECT proof_hmac FROM source_tombstones WHERE tenant_id = :t AND subject_id = :s",
        {"t": fixture["tenant_id"], "s": first},
    )
    assert proof and str(first) not in str(proof)


@pytest.mark.asyncio
async def test_erasing_twice_changes_nothing_the_second_time(erasure_fixture: dict[str, Any]) -> None:
    """A retry after a partial failure is the expected recovery path, so the second run
    must be free rather than an amplifier: the tombstone conflicts away and the outbox's
    own uniqueness refuses the duplicate."""
    fixture = erasure_fixture
    actor_id = fixture["actor_id"]
    await _plant_signal(fixture, producer_type="human", producer_id=str(actor_id))
    participant = erasure.SignalErasure(fixture["factory"], _salts(), clock=_FrozenClock())

    first = await participant.erase_actor(fixture["ctx"], actor_id)
    second = await participant.erase_actor(fixture["ctx"], actor_id)

    assert first["signals"] == 1
    assert second["signals"] == 0
    assert (
        await _scalar(
            fixture,
            "SELECT count(*) FROM source_tombstones WHERE tenant_id = :t AND record_class = :c",
            {"t": fixture["tenant_id"], "c": policies.RECORD_EXTERNAL_SIGNAL},
        )
        == 1
    )


@pytest.mark.asyncio
async def test_feedback_is_minimized_and_its_structure_survives(erasure_fixture: dict[str, Any]) -> None:
    """The note is what somebody wrote; the discriminant and rating are what every
    aggregate counts. Deleting the row would change those answers retroactively while
    looking like data that was never there."""
    fixture = erasure_fixture
    feedback_id = await _plant_feedback(fixture, note="this was wrong and here is why")

    counts = await erasure.SignalErasure(fixture["factory"], _salts(), clock=_FrozenClock()).erase_actor(
        fixture["ctx"], fixture["actor_id"]
    )

    assert counts["feedback_notes_minimized"] == 1
    async with fixture["factory"]() as session:
        row = (
            await session.execute(
                text("SELECT note, kind, rating, learning_eligible FROM context_feedback WHERE feedback_id = :f"),
                {"f": feedback_id},
            )
        ).one()
    assert row.note is None
    assert (row.kind, row.rating) == ("diagnostic_observation", "irrelevant")
    assert row.learning_eligible is False


@pytest.mark.asyncio
async def test_revoking_stamps_the_signal_and_enqueues_under_its_own_trigger(
    erasure_fixture: dict[str, Any],
) -> None:
    """`revoked_at` had no writer anywhere in the tree before this. A stamp without an
    enqueue would leave a signal marked withdrawn whose derivatives still answer."""
    fixture = erasure_fixture
    signal_id = await _plant_signal(fixture, producer_type="external", producer_id="vendor-feed")

    await erasure.revoke_signal(fixture["factory"], _salts(), ctx=fixture["ctx"], signal_id=signal_id, now=_NOW)

    assert (
        await _scalar(fixture, "SELECT revoked_at FROM external_signals WHERE signal_id = :s", {"s": signal_id})
        is not None
    )
    assert (
        await _scalar(
            fixture,
            "SELECT count(*) FROM source_tombstones WHERE tenant_id = :t AND subject_id = :s AND reason = :r",
            {"t": fixture["tenant_id"], "s": signal_id, "r": "revocation"},
        )
        == 1
    )


@pytest.mark.asyncio
async def test_revoking_the_same_signal_twice_refuses_and_writes_nothing(erasure_fixture: dict[str, Any]) -> None:
    """Refused, and nothing added. A second revocation that wrote another tombstone
    would enqueue the same work under a second authorisation."""
    fixture = erasure_fixture
    signal_id = await _plant_signal(fixture, producer_type="external", producer_id="vendor-feed")
    await erasure.revoke_signal(fixture["factory"], _salts(), ctx=fixture["ctx"], signal_id=signal_id, now=_NOW)

    before = await _scalar(
        fixture, "SELECT count(*) FROM source_tombstones WHERE tenant_id = :t", {"t": fixture["tenant_id"]}
    )

    with pytest.raises(erasure.SignalErasureRefused):
        await erasure.revoke_signal(fixture["factory"], _salts(), ctx=fixture["ctx"], signal_id=signal_id, now=_NOW)

    after = await _scalar(
        fixture, "SELECT count(*) FROM source_tombstones WHERE tenant_id = :t", {"t": fixture["tenant_id"]}
    )
    assert after == before


@pytest.mark.asyncio
async def test_revoking_another_tenants_signal_refuses_and_writes_nothing(erasure_fixture: dict[str, Any]) -> None:
    """Tenant scoping on the write itself, not on a prior read: a check-then-write would
    leave a window, and the refusal must not disclose whether the id exists elsewhere."""
    fixture = erasure_fixture
    signal_id = await _plant_signal(fixture, producer_type="external", producer_id="vendor-feed")
    foreign = TenantContext(tenant_id=uuid.uuid4(), actor_id=fixture["actor_id"], roles=["admin"])

    with pytest.raises(erasure.SignalErasureRefused):
        await erasure.revoke_signal(fixture["factory"], _salts(), ctx=foreign, signal_id=signal_id, now=_NOW)

    assert (
        await _scalar(fixture, "SELECT revoked_at FROM external_signals WHERE signal_id = :s", {"s": signal_id}) is None
    )
    assert (
        await _scalar(
            fixture,
            "SELECT count(*) FROM source_tombstones WHERE tenant_id = :t",
            {"t": foreign.tenant_id},
        )
        == 0
    )


@pytest.mark.asyncio
async def test_payload_expiry_clears_the_observation_and_leaves_the_envelope(
    erasure_fixture: dict[str, Any],
) -> None:
    """Two clocks on one row, and this is the earlier. The envelope is what keeps every
    claim derived from the signal auditable after its payload is gone."""
    fixture = erasure_fixture
    signal_id = await _plant_signal(fixture, producer_type="external", producer_id="vendor-feed")

    # Age the row past its payload clock rather than mocking the deadline: the statement
    # compares against a real column, and moving `now` forward would not exercise it.
    async with fixture["factory"]() as session, session.begin():
        await session.execute(
            text("UPDATE external_signals SET ingested_at = :old WHERE signal_id = :s"),
            {"old": _NOW - datetime.timedelta(days=4000), "s": signal_id},
        )

    expiry = erasure.SignalExpiry(fixture["factory"], holds.NoHoldStorage())
    assert await expiry.minimize_signal_payloads(fixture["ctx"], now=_NOW) == 1

    async with fixture["factory"]() as session:
        row = (
            await session.execute(
                text(
                    "SELECT payload, evidence_handle, source_event_id, content_digest "
                    "FROM external_signals WHERE signal_id = :s"
                ),
                {"s": signal_id},
            )
        ).one()
    # Replaced by a content-free marker rather than set to NULL: the schema requires
    # every signal to say where its body is (exactly one of payload/evidence_handle),
    # so clearing both is not expressible without a migration. What matters is that the
    # observation is gone and what remains describes only the reduction.
    assert row.evidence_handle is None
    assert row.payload == erasure.MINIMIZED_PAYLOAD
    assert "conclusion" not in row.payload
    # The envelope is untouched, which is the half that has to survive.
    assert row.source_event_id and row.content_digest

    # A second pass finds nothing left to do, so a repeated sweep is free.
    assert await expiry.minimize_signal_payloads(fixture["ctx"], now=_NOW) == 0
