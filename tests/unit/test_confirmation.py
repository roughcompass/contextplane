"""Unit tests for ConfirmationService (contextplane.service.memory.confirmation).

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` session
router, mirroring `tests/unit/test_promotion.py`'s pattern -- no Postgres
required. `ClaimService` is a bare `MagicMock` with `stage_confirmation`
patched as an `AsyncMock`: its own module has its own unit suite, and this
file only asserts *how* `ConfirmationService` calls it, never re-deriving its
insert SQL.

Coverage:
- `confirm()`: the human-actor-only gate (refuses a service principal before
  the claim itself is ever loaded); not-found and already-superseded guards;
  the link-it-first refusal for a claim with no resolved subject; the
  owner/observer authority split; the confirmed-bucket transition and its
  bounded decay-hold window; the call into `stage_confirmation`; and the two
  audit rows (`CLAIM_CONFIRMED`, `CLAIM_SUPERSEDED`) it writes.
- `adjudicate()`: the `ValidationError` pin for an unknown verdict and for an
  out-of-range `observed_confidence` -- both were rebased off a bare
  `ValueError` and this suite pins the *type*, not just that something is
  raised, which is what a test asserting a bare `Exception` would miss. Also
  not-found, the calibration-observation insert (with its `uncalibrated`
  fallback), and the note-presence-not-content audit payload.

Two guards here are mutation-tested (see the report accompanying this
change): the human-only gate in `confirm()`, and the `ValidationError` type
on `adjudicate()`'s unknown-verdict path.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_HUMAN, AUTHORITY_OWNER_HUMAN
from contextplane.service.memory.confidence import BUCKET_CONFIRMED, CONFIRMED_CONFIDENCE, ConfidencePolicy
from contextplane.service.memory.confirmation import ConfirmationService
from tests.helpers.clock import FakeClock
from tests.helpers.context import tenant_context

_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _session_factory(execute: Any) -> MagicMock:
    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory


def _claims_service(new_claim_id: uuid.UUID | None = None) -> MagicMock:
    claims = MagicMock()
    claims.stage_confirmation = AsyncMock(return_value=new_claim_id or uuid.uuid4())
    return claims


def _original_row(**overrides: Any) -> MagicMock:
    tid = overrides.pop("owning_tenant_id", uuid.uuid4())
    base: dict[str, Any] = {
        "owning_tenant_id": tid,
        "author_tenant_id": tid,
        "subject_entity_id": uuid.uuid4(),
        "subject_reference": "github:acme/billing",
        "predicate": "owned_by_team",
        "value_type": "string",
        "claim_category": "ownership_stewardship",
        "value_jsonb": "platform",
        "value_cardinality": "single",
        "value_entity_id": None,
        "asserted_valid_from": _NOW - datetime.timedelta(days=1),
        "asserted_valid_to": None,
        "visibility": "tenant-shared",
        "size_bytes": 12,
        "namespace": None,
        "strategy_id": None,
        "status": "staged",
        "superseded_by": None,
    }
    base.update(overrides)
    return MagicMock(**base)


def _confirm_session(
    *,
    original: Any | None,
    actor_kind: str | None = "human",
) -> tuple[MagicMock, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {"audit": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        result = MagicMock()
        if "SELECT actor_kind FROM actors" in sql:
            result.scalar_one_or_none = MagicMock(return_value=actor_kind)
            return result
        if "FROM memory_claims WHERE claim_id" in sql and "FOR UPDATE" in sql:
            result.one_or_none = MagicMock(return_value=original)
            return result
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    return _session_factory(_execute), calls


def _service(
    *, factory: MagicMock, claims: MagicMock | None = None, clock: FakeClock | None = None
) -> ConfirmationService:
    return ConfirmationService(factory, claims or _claims_service(), clock=clock or FakeClock(_NOW))


# ---------------------------------------------------------------------------
# confirm() -- the human-actor-only gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_refuses_a_service_principal_before_loading_the_claim() -> None:
    """The gate runs before the claim is even fetched: a worker calling this
    must never learn anything about the claim it was refused for."""
    factory, calls = _confirm_session(original=None, actor_kind="sync_worker")
    claims = _claims_service()
    service = _service(factory=factory, claims=claims)

    with pytest.raises(PermissionError, match="only a human principal"):
        await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())

    claims.stage_confirmation.assert_not_awaited()
    assert calls["audit"] == []


@pytest.mark.asyncio
async def test_confirm_proceeds_when_the_actor_kind_is_human() -> None:
    """The positive side of the same gate: a human actor is not refused."""
    original = _original_row()
    factory, _ = _confirm_session(original=original, actor_kind="human")
    service = _service(factory=factory)

    result = await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())

    assert result is not None


# ---------------------------------------------------------------------------
# confirm() -- not-found / already-superseded / unlinked guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_raises_not_found_for_a_missing_claim() -> None:
    factory, _ = _confirm_session(original=None, actor_kind="human")
    service = _service(factory=factory)

    with pytest.raises(NotFoundError):
        await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_confirm_refuses_a_claim_already_superseded() -> None:
    original = _original_row(superseded_by=uuid.uuid4())
    factory, _ = _confirm_session(original=original, actor_kind="human")
    service = _service(factory=factory)

    with pytest.raises(ConflictError, match="already superseded"):
        await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_confirm_refuses_an_unlinked_claim_asking_that_it_be_linked_first() -> None:
    """A claim with no resolved subject has nothing to confirm *about* -- the
    link-it-first refusal, distinct from not-found or already-superseded."""
    original = _original_row(subject_entity_id=None)
    factory, calls = _confirm_session(original=original, actor_kind="human")
    claims = _claims_service()
    service = _service(factory=factory, claims=claims)

    with pytest.raises(ConflictError, match="link it first"):
        await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())

    claims.stage_confirmation.assert_not_awaited()
    assert calls["audit"] == []


# ---------------------------------------------------------------------------
# confirm() -- authority split, confidence/bucket, decay hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_by_the_owning_tenant_earns_owner_human_authority() -> None:
    owner_tid = uuid.uuid4()
    original = _original_row(owning_tenant_id=owner_tid)
    factory, _ = _confirm_session(original=original, actor_kind="human")
    service = _service(factory=factory)

    result = await service.confirm(tenant_context(tenant_id=owner_tid, roles=["admin"]), claim_id=uuid.uuid4())

    assert result.source_authority == AUTHORITY_OWNER_HUMAN


@pytest.mark.asyncio
async def test_confirm_by_a_non_owning_tenant_earns_observer_human_authority() -> None:
    """Standing decides the tier, not merely being human -- a non-owner
    confirmation is real and recorded, but it cannot outrank an owner's."""
    owner_tid = uuid.uuid4()
    original = _original_row(owning_tenant_id=owner_tid)
    factory, _ = _confirm_session(original=original, actor_kind="human")
    service = _service(factory=factory)

    result = await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())  # fresh, different tenant

    assert result.source_authority == AUTHORITY_OBSERVER_HUMAN


@pytest.mark.asyncio
async def test_confirm_sets_confidence_to_the_policys_confirmed_value_and_its_bucket() -> None:
    original = _original_row()
    factory, _ = _confirm_session(original=original, actor_kind="human")
    service = _service(factory=factory)

    result = await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())

    assert result.confidence == CONFIRMED_CONFIDENCE
    assert result.bucket == BUCKET_CONFIRMED


@pytest.mark.asyncio
async def test_confirm_holds_decay_off_for_the_configured_number_of_days() -> None:
    """`ownership_stewardship`'s own half-life (270 days) is well above the
    180-day default configured hold, so the configured value governs."""
    original = _original_row(claim_category="ownership_stewardship")
    factory, _ = _confirm_session(original=original, actor_kind="human")
    service = _service(factory=factory)

    result = await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())

    assert result.hold_until == _NOW + datetime.timedelta(days=180)


@pytest.mark.asyncio
async def test_confirm_hold_is_capped_by_a_fast_moving_categorys_own_half_life() -> None:
    """`interface_contract` decays on a 90-day half-life -- shorter than the
    configured 180-day hold -- so the category caps it. Confirming a
    fast-moving claim must not hold decay off longer than the claim's own
    volatility would justify."""
    original = _original_row(claim_category="interface_contract")
    factory, _ = _confirm_session(original=original, actor_kind="human")
    service = _service(factory=factory)

    result = await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4())

    assert result.hold_until == _NOW + datetime.timedelta(days=90)


@pytest.mark.asyncio
async def test_confirm_a_custom_policy_hold_window_is_still_capped_by_the_category() -> None:
    original = _original_row(claim_category="interface_contract")
    factory, _ = _confirm_session(original=original, actor_kind="human")
    service = _service(factory=factory)
    policy = ConfidencePolicy(confirmation_hold_days=30)

    result = await service.confirm(tenant_context(roles=["admin"]), claim_id=uuid.uuid4(), policy=policy)

    # The shorter of the two: the configured 30 days, not the category's 90.
    assert result.hold_until == _NOW + datetime.timedelta(days=30)


# ---------------------------------------------------------------------------
# confirm() -- delegation to stage_confirmation, and the two audit rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_delegates_the_write_to_stage_confirmation_with_derived_fields() -> None:
    owner_tid = uuid.uuid4()
    actor_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    new_claim_id = uuid.uuid4()
    original = _original_row(owning_tenant_id=owner_tid)
    factory, _ = _confirm_session(original=original, actor_kind="human")
    claims = _claims_service(new_claim_id)
    service = _service(factory=factory, claims=claims)

    result = await service.confirm(
        tenant_context(tenant_id=owner_tid, actor_id=actor_id, roles=["admin"]), claim_id=claim_id
    )

    claims.stage_confirmation.assert_awaited_once()
    kwargs = claims.stage_confirmation.await_args.kwargs
    assert kwargs["confirms_claim_id"] == claim_id
    assert kwargs["authority"] == AUTHORITY_OWNER_HUMAN
    assert kwargs["confidence"] == CONFIRMED_CONFIDENCE
    assert kwargs["confirming_tenant_id"] == owner_tid
    assert kwargs["confirming_actor_id"] == actor_id
    assert kwargs["now"] == _NOW
    inputs = json.loads(kwargs["confidence_inputs"])
    assert inputs["is_confirmed"] is True
    assert result.claim_id == new_claim_id


@pytest.mark.asyncio
async def test_confirm_writes_a_confirmed_row_and_a_superseded_row() -> None:
    claim_id = uuid.uuid4()
    new_claim_id = uuid.uuid4()
    owner_tid = uuid.uuid4()
    original = _original_row(owning_tenant_id=owner_tid)
    factory, calls = _confirm_session(original=original, actor_kind="human")
    claims = _claims_service(new_claim_id)
    service = _service(factory=factory, claims=claims)

    await service.confirm(tenant_context(tenant_id=owner_tid, roles=["admin"]), claim_id=claim_id)

    assert len(calls["audit"]) == 2
    confirmed, superseded = calls["audit"]
    assert confirmed["action"] == actions.CLAIM_CONFIRMED
    assert confirmed["target"] == new_claim_id
    confirmed_payload = json.loads(confirmed["after"])
    assert confirmed_payload["confirms_claim_id"] == str(claim_id)
    assert confirmed_payload["source_authority"] == AUTHORITY_OWNER_HUMAN

    assert superseded["action"] == actions.CLAIM_SUPERSEDED
    assert superseded["target"] == claim_id
    superseded_payload = json.loads(superseded["after"])
    assert superseded_payload["superseded_by"] == str(new_claim_id)


# ---------------------------------------------------------------------------
# adjudicate() -- ValidationError pin (rebased off ValueError), not-found,
# the calibration-observation write, and the note-presence audit payload.
# ---------------------------------------------------------------------------


def _adjudicate_session(*, claim: Any | None) -> tuple[MagicMock, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {"adjudication": [], "audit": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        result = MagicMock()
        if "FROM memory_claims WHERE claim_id" in sql:
            result.one_or_none = MagicMock(return_value=claim)
            return result
        if "INSERT INTO memory_claim_adjudication" in sql:
            calls["adjudication"].append(params or {})
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    return _session_factory(_execute), calls


def _claim_for_adjudication(**overrides: Any) -> MagicMock:
    base: dict[str, Any] = {
        "calibration_version": None,
        "provider_confidence": 0.7,
        "source_authority": "owner_inference",
    }
    base.update(overrides)
    return MagicMock(**base)


@pytest.mark.asyncio
async def test_adjudicate_rejects_an_unknown_verdict_with_validation_error() -> None:
    """Pinned as `ValidationError`, not a bare `ValueError` -- the type this
    task's rebase produced. A test that only checked `Exception` would still
    pass if this regressed back to `ValueError`."""
    factory, calls = _adjudicate_session(claim=_claim_for_adjudication())
    service = _service(factory=factory)

    with pytest.raises(ValidationError, match="unknown verdict"):
        await service.adjudicate(
            tenant_context(roles=["admin"]),
            claim_id=uuid.uuid4(),
            verdict="maybe",
            observed_confidence=0.5,
        )
    assert calls["adjudication"] == []


@pytest.mark.asyncio
async def test_adjudicate_rejects_an_unknown_verdict_is_not_a_bare_value_error() -> None:
    """Guards against a regression to the pre-rebase type: `ValidationError`
    is not `ValueError`, so this must not raise the old type."""
    factory, _ = _adjudicate_session(claim=_claim_for_adjudication())
    service = _service(factory=factory)

    with pytest.raises(ValidationError) as exc_info:
        await service.adjudicate(
            tenant_context(roles=["admin"]),
            claim_id=uuid.uuid4(),
            verdict="maybe",
            observed_confidence=0.5,
        )
    assert not isinstance(exc_info.value, ValueError)
    assert type(exc_info.value) is ValidationError


@pytest.mark.asyncio
async def test_adjudicate_rejects_an_out_of_range_confidence_with_validation_error() -> None:
    factory, calls = _adjudicate_session(claim=_claim_for_adjudication())
    service = _service(factory=factory)

    with pytest.raises(ValidationError, match=r"\[0,1\]"):
        await service.adjudicate(
            tenant_context(roles=["admin"]),
            claim_id=uuid.uuid4(),
            verdict="correct",
            observed_confidence=1.5,
        )
    assert calls["adjudication"] == []


@pytest.mark.asyncio
async def test_adjudicate_rejects_a_negative_confidence_with_validation_error() -> None:
    factory, calls = _adjudicate_session(claim=_claim_for_adjudication())
    service = _service(factory=factory)

    with pytest.raises(ValidationError):
        await service.adjudicate(
            tenant_context(roles=["admin"]),
            claim_id=uuid.uuid4(),
            verdict="incorrect",
            observed_confidence=-0.01,
        )
    assert calls["adjudication"] == []


@pytest.mark.asyncio
async def test_adjudicate_raises_not_found_for_a_missing_claim() -> None:
    factory, _ = _adjudicate_session(claim=None)
    service = _service(factory=factory)

    with pytest.raises(NotFoundError):
        await service.adjudicate(
            tenant_context(roles=["admin"]),
            claim_id=uuid.uuid4(),
            verdict="correct",
            observed_confidence=0.5,
        )


@pytest.mark.asyncio
async def test_adjudicate_writes_the_calibration_observation_with_the_uncalibrated_fallback() -> None:
    claim_id = uuid.uuid4()
    claim = _claim_for_adjudication(calibration_version=None, provider_confidence=0.71, source_authority="owner_human")
    factory, calls = _adjudicate_session(claim=claim)
    service = _service(factory=factory)

    await service.adjudicate(
        tenant_context(roles=["admin"]),
        claim_id=claim_id,
        verdict="correct",
        observed_confidence=0.876,
    )

    assert len(calls["adjudication"]) == 1
    row = calls["adjudication"][0]
    assert row["cid"] == claim_id
    assert row["verdict"] == "correct"
    assert row["conf"] == 0.876
    assert row["calib"] == "uncalibrated"
    assert row["prov"] == 0.71
    assert row["auth"] == "owner_human"


@pytest.mark.asyncio
async def test_adjudicate_keeps_an_existing_calibration_version_rather_than_the_fallback() -> None:
    claim = _claim_for_adjudication(calibration_version="calib-2026-08")
    factory, calls = _adjudicate_session(claim=claim)
    service = _service(factory=factory)

    await service.adjudicate(
        tenant_context(roles=["admin"]),
        claim_id=uuid.uuid4(),
        verdict="undecidable",
        observed_confidence=0.4,
    )

    assert calls["adjudication"][0]["calib"] == "calib-2026-08"


@pytest.mark.asyncio
async def test_adjudicate_audits_note_presence_not_its_text() -> None:
    """The audit trail records *that* a note was left, never its content."""
    factory, calls = _adjudicate_session(claim=_claim_for_adjudication())
    service = _service(factory=factory)

    await service.adjudicate(
        tenant_context(roles=["admin"]),
        claim_id=uuid.uuid4(),
        verdict="incorrect",
        observed_confidence=0.2,
        note="the endpoint was retired last quarter",
    )

    payload = json.loads(calls["audit"][0]["after"])
    assert payload["note_present"] is True
    assert "retired" not in calls["audit"][0]["after"]


@pytest.mark.asyncio
async def test_adjudicate_without_a_note_records_note_present_false() -> None:
    factory, calls = _adjudicate_session(claim=_claim_for_adjudication())
    service = _service(factory=factory)

    await service.adjudicate(
        tenant_context(roles=["admin"]),
        claim_id=uuid.uuid4(),
        verdict="correct",
        observed_confidence=0.9,
    )

    payload = json.loads(calls["audit"][0]["after"])
    assert payload["note_present"] is False
