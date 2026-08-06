"""Unit tests for `operational_chain.py`.

No database: the `queries.operational_chain` module `OperationalChainService`
calls is monkeypatched with an in-memory fake faithful enough to the real
relational shape (a head row per revision, a compare-and-swap on advance, a
`(revision_id, idempotency_key_digest)` index) to exercise every branch --
genesis vs continuation, exact retry vs idempotency conflict, the
IntegrityError race-recovery path, and every `verify_chain` failure mode --
without Postgres. Signing itself is real: every event this suite appends is
actually canonicalized, digested, and Ed25519-signed, so a test that tampers
with a stored field and expects `verify_chain` to notice is testing the real
cryptography, not a stand-in for it.

What a fake cannot prove -- that the database's own locks and CHECK
constraints hold under a real concurrent race, and that Postgres itself
refuses the two genesis-shape violations -- is
`tests/integration/test_arc_operational_chain.py`'s job.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.exc import IntegrityError

from registry.arc.schemas.authoring_profiles import canonicalize_operational_event_v1
from registry.arc.service import operational_chain as oc
from registry.arc.service.queries.operational_chain import EventRow, ExistingEvent, HeadRow

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_ARTIFACT_ID = uuid.uuid4()
_REVISION_ID = uuid.uuid4()


class _FakeClock:
    def __init__(self, moment: datetime.datetime = _NOW) -> None:
        self._moment = moment

    def now(self) -> datetime.datetime:
        return self._moment


# ---------------------------------------------------------------------------
# Session doubles -- `append_event` needs `session.begin_nested()`; nothing
# else here touches a real connection.
# ---------------------------------------------------------------------------


class _NestedCM:
    def __init__(self, session: _NullSession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False  # never suppress -- a raised exception must propagate


class _NullSession:
    def begin_nested(self) -> _NestedCM:
        return _NestedCM(self)


# ---------------------------------------------------------------------------
# In-memory fake for the eight `queries.operational_chain` functions
# `OperationalChainService` calls.
# ---------------------------------------------------------------------------


class FakeChainQueries:
    def __init__(self) -> None:
        self.events: dict[uuid.UUID, list[EventRow]] = {}
        self.heads: dict[uuid.UUID, HeadRow] = {}
        self.checkpoints: list[dict[str, Any]] = []
        # Keyed exactly like the real UNIQUE constraint.
        self.idempotency: dict[tuple[uuid.UUID, str], ExistingEvent] = {}
        # Set by a test to make the next `insert_event` behave like a lost
        # race: raise IntegrityError instead of succeeding.
        self.raise_on_next_insert = False

    async def find_by_idempotency(
        self, _session: object, revision_id: uuid.UUID, idempotency_key_digest: str
    ) -> ExistingEvent | None:
        return self.idempotency.get((revision_id, idempotency_key_digest))

    async def lock_head(self, _session: object, revision_id: uuid.UUID) -> HeadRow | None:
        return self.heads.get(revision_id)

    async def load_head(self, _session: object, revision_id: uuid.UUID) -> HeadRow | None:
        return self.heads.get(revision_id)

    async def load_events(self, _session: object, revision_id: uuid.UUID) -> list[EventRow]:
        return list(self.events.get(revision_id, []))

    async def insert_event(self, _session: object, **kwargs: Any) -> None:
        if self.raise_on_next_insert:
            self.raise_on_next_insert = False
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        revision_id = kwargs["revision_id"]
        row = EventRow(
            event_id=kwargs["event_id"],
            artifact_id=kwargs["artifact_id"],
            sequence=kwargs["sequence"],
            event_type=kwargs["event_type"],
            event_payload=dict(kwargs["payload"]),
            actor_issuer=kwargs["actor_issuer"],
            actor_subject=kwargs["actor_subject"],
            actor_role=kwargs["actor_role"],
            authorization_decision_reference=kwargs["authorization_decision_reference"],
            authority_evidence_digest=kwargs["authority_evidence_digest"],
            idempotency_key_digest=kwargs["idempotency_key_digest"],
            previous_event_digest=kwargs["previous_digest"],
            signer_key_id=kwargs["signer_key_id"],
            event_digest=kwargs["digest"],
            signature=kwargs["signature"],
            created_at=kwargs["created_at"],
        )
        self.events.setdefault(revision_id, []).append(row)
        self.idempotency[(revision_id, row.idempotency_key_digest)] = ExistingEvent(
            event_id=row.event_id,
            revision_id=revision_id,
            sequence=row.sequence,
            event_digest=row.event_digest,
            request_payload_digest=kwargs["request_digest"],
        )

    async def insert_head(
        self,
        _session: object,
        *,
        revision_id: uuid.UUID,
        next_sequence: int,
        last_event_digest: str,
        updated_at: datetime.datetime,
    ) -> None:
        self.heads[revision_id] = HeadRow(next_sequence=next_sequence, last_event_digest=last_event_digest)

    async def advance_head(
        self,
        _session: object,
        *,
        revision_id: uuid.UUID,
        expected_previous: str | None,
        next_sequence: int,
        digest: str,
        updated_at: datetime.datetime,
    ) -> int:
        head = self.heads.get(revision_id)
        if head is None or head.last_event_digest != expected_previous:
            return 0
        self.heads[revision_id] = HeadRow(next_sequence=next_sequence, last_event_digest=digest)
        return 1

    async def insert_checkpoint(self, _session: object, **kwargs: Any) -> None:
        self.checkpoints.append(kwargs)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeChainQueries:
    f = FakeChainQueries()
    monkeypatch.setattr(oc, "queries", f)
    return f


def _service(clock: _FakeClock | None = None) -> oc.OperationalChainService:
    return oc.OperationalChainService(clock=clock or _FakeClock(), deployment_id="unit-test")


def _payload(**overrides: Any) -> dict[str, Any]:
    return oc.build_event_payload(**overrides)


# ---------------------------------------------------------------------------
# build_event_payload -- the closed shape, every key present.
# ---------------------------------------------------------------------------


def test_build_event_payload_names_every_closed_key_even_when_unset() -> None:
    payload = oc.build_event_payload()
    assert set(payload) == {
        "initial_freshness_basis",
        "retention_floor_days",
        "legal_hold_active",
        "artifact_semantics_digest",
        "hold_id",
        "reason_code",
        "authority_evidence_digest",
        "placed_at",
        "released_at",
        "prior_deadline",
        "later_deadline",
    }
    assert all(v is None for v in payload.values())


def test_build_event_payload_formats_timestamps_as_rfc3339_with_z() -> None:
    moment = datetime.datetime(2026, 3, 4, 5, 6, 7, tzinfo=datetime.UTC)
    payload = oc.build_event_payload(placed_at=moment)
    assert payload["placed_at"] == "2026-03-04T05:06:07Z"
    assert "+00:00" not in payload["placed_at"]


# ---------------------------------------------------------------------------
# append_event -- genesis vs continuation.
# ---------------------------------------------------------------------------


async def test_genesis_writes_sequence_zero_with_no_predecessor(fake: FakeChainQueries) -> None:
    service = _service()
    result = await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(initial_freshness_basis="connector_verified", retention_floor_days=730),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )

    assert result.sequence == 0
    row = fake.events[_REVISION_ID][0]
    assert row.previous_event_digest is None
    assert row.event_type == oc.EVENT_INITIALIZED
    assert fake.heads[_REVISION_ID].next_sequence == 1
    assert fake.heads[_REVISION_ID].last_event_digest == result.event_digest
    # The checkpoint outbox row is written in the same call.
    assert len(fake.checkpoints) == 1
    assert fake.checkpoints[0]["sequence"] == 0


async def test_continuation_advances_the_existing_head(fake: FakeChainQueries) -> None:
    service = _service()
    genesis = await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )

    continuation = await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_FRESHNESS_DOWNGRADED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(initial_freshness_basis="revision_pinned_only"),
        authorization_decision_reference="test:downgrade",
        authority_evidence_digest="2" * 64,
        idempotency_key="downgrade-key",
    )

    assert continuation.sequence == 1
    row = fake.events[_REVISION_ID][1]
    assert row.previous_event_digest == genesis.event_digest
    assert fake.heads[_REVISION_ID].next_sequence == 2
    assert len(fake.checkpoints) == 2


async def test_a_second_genesis_on_an_existing_chain_is_refused(fake: FakeChainQueries) -> None:
    service = _service()
    await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )

    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.append_event(
            _NullSession(),
            artifact_id=_ARTIFACT_ID,
            revision_id=_REVISION_ID,
            event_type=oc.EVENT_INITIALIZED,
            actor=oc.SYSTEM_ACTOR,
            payload=_payload(),
            authorization_decision_reference="test:genesis-again",
            authority_evidence_digest="1" * 64,
            idempotency_key="genesis-key-2",
        )
    assert exc_info.value.reason_code == "chain_link_violation"
    # Refused before any write -- the chain is exactly as it was.
    assert len(fake.events[_REVISION_ID]) == 1


async def test_a_continuation_event_on_a_revision_with_no_chain_yet_is_refused(fake: FakeChainQueries) -> None:
    service = _service()
    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.append_event(
            _NullSession(),
            artifact_id=_ARTIFACT_ID,
            revision_id=_REVISION_ID,
            event_type=oc.EVENT_FRESHNESS_DOWNGRADED,
            actor=oc.SYSTEM_ACTOR,
            payload=_payload(),
            authorization_decision_reference="test:no-genesis",
            authority_evidence_digest="1" * 64,
            idempotency_key="no-genesis-key",
        )
    assert exc_info.value.reason_code == "chain_link_violation"
    assert _REVISION_ID not in fake.events


# ---------------------------------------------------------------------------
# Idempotency: exact retry vs changed payload vs a lost race.
# ---------------------------------------------------------------------------


async def test_an_exact_retry_returns_the_original_identity_without_writing_again(fake: FakeChainQueries) -> None:
    service = _service()
    first = await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(initial_freshness_basis="connector_verified"),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )

    retry = await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(initial_freshness_basis="connector_verified"),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )

    assert retry == first
    assert len(fake.events[_REVISION_ID]) == 1
    assert len(fake.checkpoints) == 1


async def test_a_changed_payload_under_the_same_idempotency_key_is_refused(fake: FakeChainQueries) -> None:
    service = _service()
    await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(initial_freshness_basis="connector_verified"),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )

    with pytest.raises(oc.OperationalChainIdempotencyConflict):
        await service.append_event(
            _NullSession(),
            artifact_id=_ARTIFACT_ID,
            revision_id=_REVISION_ID,
            event_type=oc.EVENT_INITIALIZED,
            actor=oc.SYSTEM_ACTOR,
            payload=_payload(initial_freshness_basis="revision_pinned_only"),  # changed
            authorization_decision_reference="test:genesis",
            authority_evidence_digest="1" * 64,
            idempotency_key="genesis-key",
        )
    assert len(fake.events[_REVISION_ID]) == 1


async def test_a_lost_race_resolves_to_the_winners_identity(fake: FakeChainQueries) -> None:
    """Simulates two concurrent callers both passing the pre-check (neither
    sees the other's row yet) by forcing the *second* caller's own insert to
    collide -- the shape a real `UNIQUE(revision_id, idempotency_key_digest)`
    violation takes. Resolution must return the row that actually won, not
    raise, and not write a second one."""
    service = _service()
    genesis = await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )
    # Seed the "winner" as if a concurrent caller already committed it,
    # then force this call's own insert to collide the way a real UNIQUE
    # violation would.
    fake.raise_on_next_insert = True

    result = await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )

    assert result == genesis
    assert len(fake.events[_REVISION_ID]) == 1


async def test_a_genuine_sequence_collision_with_no_matching_idempotency_key_signals_contention(
    fake: FakeChainQueries,
) -> None:
    """Unlike the race above, this collision is *not* explained by an
    idempotency retry -- the fallback must say so distinctly rather than
    silently returning someone else's identity."""
    service = _service()
    await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )
    fake.raise_on_next_insert = True

    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.append_event(
            _NullSession(),
            artifact_id=_ARTIFACT_ID,
            revision_id=_REVISION_ID,
            event_type=oc.EVENT_FRESHNESS_DOWNGRADED,
            actor=oc.SYSTEM_ACTOR,
            payload=_payload(),
            authorization_decision_reference="test:downgrade",
            authority_evidence_digest="2" * 64,
            idempotency_key="a-completely-different-key",  # never matches
        )
    assert exc_info.value.reason_code == "sequence_contention"


# ---------------------------------------------------------------------------
# verify_chain -- every failure mode this module can detect on its own,
# without a sink (see the integration suite for the checkpoint half).
# ---------------------------------------------------------------------------


async def _build_chain(service: oc.OperationalChainService, fake: FakeChainQueries, length: int) -> None:
    await service.append_event(
        _NullSession(),
        artifact_id=_ARTIFACT_ID,
        revision_id=_REVISION_ID,
        event_type=oc.EVENT_INITIALIZED,
        actor=oc.SYSTEM_ACTOR,
        payload=_payload(initial_freshness_basis="connector_verified"),
        authorization_decision_reference="test:genesis",
        authority_evidence_digest="1" * 64,
        idempotency_key="genesis-key",
    )
    for i in range(1, length):
        await service.append_event(
            _NullSession(),
            artifact_id=_ARTIFACT_ID,
            revision_id=_REVISION_ID,
            event_type=oc.EVENT_FRESHNESS_DOWNGRADED,
            actor=oc.SYSTEM_ACTOR,
            payload=_payload(reason_code=f"step-{i}"),
            authorization_decision_reference=f"test:step-{i}",
            authority_evidence_digest=f"{i}" * 64,
            idempotency_key=f"step-{i}-key",
        )


async def test_a_clean_chain_verifies(fake: FakeChainQueries) -> None:
    service = _service()
    await _build_chain(service, fake, length=3)

    await service.verify_chain(_NullSession(), _REVISION_ID)  # must not raise

    # A concrete assertion beyond "did not raise": the chain this built is
    # exactly the length asked for, so a clean pass here is proving
    # something over real content, not a vacuously empty chain.
    assert len(fake.events[_REVISION_ID]) == 3
    assert fake.heads[_REVISION_ID].next_sequence == 3


async def test_an_empty_chain_fails_with_sequence_gap(fake: FakeChainQueries) -> None:
    service = _service()
    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.verify_chain(_NullSession(), _REVISION_ID)
    assert exc_info.value.reason_code == "sequence_gap"


async def test_a_missing_sequence_fails_with_sequence_gap(fake: FakeChainQueries) -> None:
    service = _service()
    await _build_chain(service, fake, length=3)
    fake.events[_REVISION_ID] = [
        dataclasses.replace(row, sequence=row.sequence + 1) if row.sequence == 1 else row
        for row in fake.events[_REVISION_ID]
    ]

    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.verify_chain(_NullSession(), _REVISION_ID)
    assert exc_info.value.reason_code == "sequence_gap"


async def test_a_changed_predecessor_link_is_caught(fake: FakeChainQueries) -> None:
    service = _service()
    await _build_chain(service, fake, length=3)
    fake.events[_REVISION_ID][2] = dataclasses.replace(fake.events[_REVISION_ID][2], previous_event_digest="f" * 64)

    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.verify_chain(_NullSession(), _REVISION_ID)
    assert exc_info.value.reason_code == "changed_predecessor"


async def test_a_tampered_payload_is_caught_by_the_recomputed_digest(fake: FakeChainQueries) -> None:
    """The stronger proof: this tamper changes neither `previous_event_
    digest` nor the stored `event_digest` -- only the payload content. It
    is caught because `verify_chain` recomputes the digest from the row's
    other fields and compares, not because a link looks wrong."""
    service = _service()
    await _build_chain(service, fake, length=2)
    tampered = dict(fake.events[_REVISION_ID][1].event_payload)
    tampered["reason_code"] = "attacker-supplied-value"
    fake.events[_REVISION_ID][1] = dataclasses.replace(fake.events[_REVISION_ID][1], event_payload=tampered)

    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.verify_chain(_NullSession(), _REVISION_ID)
    assert exc_info.value.reason_code == "changed_predecessor"


async def test_a_tampered_signature_is_caught(fake: FakeChainQueries) -> None:
    service = _service()
    await _build_chain(service, fake, length=2)
    fake.events[_REVISION_ID][0] = dataclasses.replace(fake.events[_REVISION_ID][0], signature="00" * 64)

    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.verify_chain(_NullSession(), _REVISION_ID)
    assert exc_info.value.reason_code == "signature_invalid"


async def test_an_event_signed_by_an_unregistered_key_is_caught(fake: FakeChainQueries) -> None:
    """Unlike the signature-tamper test above, this row is *internally
    consistent* -- its digest genuinely was recomputed to include the new
    `signer_key_id`, and its signature genuinely verifies under that key's
    own public half. What makes it fail is that this process never
    registered that key at all: an event another process (or another
    signing key generation) produced, that this verifier cannot vouch for."""
    service = _service()
    await _build_chain(service, fake, length=1)
    row = fake.events[_REVISION_ID][0]
    other_key_id = "a-key-this-process-never-generated"
    other_private_key = Ed25519PrivateKey.generate()
    canonical_obj: dict[str, Any] = {
        "profile": "arc_operational_event_v1",
        "event_id": str(row.event_id),
        "artifact_id": str(row.artifact_id),
        "revision_id": str(_REVISION_ID),
        "sequence": row.sequence,
        "event_type": row.event_type,
        "event_payload": row.event_payload,
        "actor_issuer": row.actor_issuer,
        "actor_subject": row.actor_subject,
        "actor_role": row.actor_role,
        "authorization_decision_reference": row.authorization_decision_reference,
        "authority_evidence_digest": row.authority_evidence_digest,
        "idempotency_key_digest": row.idempotency_key_digest,
        "previous_event_digest": row.previous_event_digest,
        "signer_key_id": other_key_id,
        "created_at": oc._rfc3339(row.created_at),
    }
    digest = hashlib.sha256(canonicalize_operational_event_v1(canonical_obj)).hexdigest()
    signature = other_private_key.sign(oc._SIGNATURE_DOMAIN + bytes.fromhex(digest))
    fake.events[_REVISION_ID][0] = dataclasses.replace(
        row, signer_key_id=other_key_id, event_digest=digest, signature=signature.hex()
    )

    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.verify_chain(_NullSession(), _REVISION_ID)
    assert exc_info.value.reason_code == "signature_invalid"


async def test_a_stale_head_that_does_not_match_the_chains_end_is_caught(fake: FakeChainQueries) -> None:
    service = _service()
    await _build_chain(service, fake, length=3)
    fake.heads[_REVISION_ID] = dataclasses.replace(fake.heads[_REVISION_ID], next_sequence=2)

    with pytest.raises(oc.OperationalChainIntegrityError) as exc_info:
        await service.verify_chain(_NullSession(), _REVISION_ID)
    assert exc_info.value.reason_code == "stale_head"


async def test_recovery_never_rewrites_the_row_it_just_flagged(fake: FakeChainQueries) -> None:
    """The security property in code: detecting a tamper raises -- it does
    not touch the row. The byte-identical row before and after is the proof
    that `verify_chain` is read-only."""
    service = _service()
    await _build_chain(service, fake, length=2)
    before = dataclasses.replace(fake.events[_REVISION_ID][0])
    fake.events[_REVISION_ID][0] = dataclasses.replace(fake.events[_REVISION_ID][0], signature="00" * 64)
    tampered = dataclasses.replace(fake.events[_REVISION_ID][0])

    with pytest.raises(oc.OperationalChainIntegrityError):
        await service.verify_chain(_NullSession(), _REVISION_ID)

    after = fake.events[_REVISION_ID][0]
    assert after == tampered
    assert after != before  # the tamper is real, not a no-op fixture bug
