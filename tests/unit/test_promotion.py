"""Unit tests for PromotionService: propose/accept/reject/reverse's state
transitions and guard conditions, and the journal bookkeeping reversal
depends on.

All DB interaction is mocked via an SQL-string-keyed `AsyncMock` session
router -- no Postgres required, mirroring `test_promotion_sweep_worker.py`'s
pattern. `ClaimService` is a bare `MagicMock` (its own module has its own
unit suite; this file only asserts *how* `PromotionService` calls it).
`attribute_writes.write_attribute`/`write_edge` are monkeypatched rather than
driven through their own SQL: they are a delegated write path with no
dedicated unit suite of their own yet, and re-deriving their internal SQL
here would test the wrong module's boundary.

The sharpest edge in this file is `reverse()`. A promotion's canonical row
can stop being reversible in two different ways: the row itself was closed
outright (`still_live` is false), or a *later* promotion was built on top of
it, which narrows its interval without invalidating it (`built_on` is
true) -- a still-live check alone would miss exactly that stacked case and
let a reversal silently overwrite a later, unrelated change. Both guard
conditions are pinned independently below, including the stacked case where
the row is still live *and* something is built on it.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.audit import actions
from contextplane.exceptions import ConflictError, NotFoundError, ValidationError
from contextplane.service.memory import promotion as promotion_module
from contextplane.service.memory.promotion import (
    _ACCEPTED,
    STATE_ACCEPTED,
    STATE_AMENDED,
    STATE_OPEN,
    PromotionService,
    value_digest,
)
from contextplane.service.memory.promotion_targets import TARGET_ATTRIBUTE, TARGET_EDGE
from contextplane.types import PiiMatchResult, PiiScanResponse
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 4, 12, 0, 0, tzinfo=datetime.UTC)

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


def _mapping_first(row: dict[str, Any] | None) -> MagicMock:
    result = MagicMock()
    result.mappings = MagicMock(return_value=MagicMock(first=MagicMock(return_value=row)))
    return result


def _bare_first(value: Any) -> MagicMock:
    result = MagicMock()
    result.first = MagicMock(return_value=value)
    return result


def _scalar_one(value: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=value)
    return result


def _no_pii_scanner() -> MagicMock:
    scanner = MagicMock()
    scanner.scan = MagicMock(return_value=PiiScanResponse(matched_patterns=[], action_taken="advisory"))
    return scanner


def _claims_service() -> MagicMock:
    claims = MagicMock()
    claims.set_promotion_state = AsyncMock()
    return claims


def _session_factory(execute: Any) -> MagicMock:
    def _new_session() -> AsyncMock:
        session = AsyncMock()
        session.execute = execute
        session.begin = MagicMock(return_value=_AsyncCM(None))
        return session

    factory = MagicMock()
    factory.side_effect = lambda: _AsyncCM(_new_session())
    return factory


# ---------------------------------------------------------------------------
# propose()
# ---------------------------------------------------------------------------


def _claim_row(**overrides: Any) -> dict[str, Any]:
    owning = overrides.pop("owning_tenant_id", uuid.uuid4())
    base: dict[str, Any] = {
        "claim_id": uuid.uuid4(),
        "subject_entity_id": uuid.uuid4(),
        "predicate": "owned_by_team",
        "value": "platform",
        "owning_tenant_id": owning,
        "author_tenant_id": owning,
        "author_actor_id": uuid.uuid4(),
        "status": "staged",
        "is_contested": False,
        "confidence": 0.9,
        "source_authority": "owner_human",
        "consolidated_at": _NOW,
        "promotion_state": None,
        "asserted_valid_from": _NOW,
        "asserted_valid_to": None,
    }
    base.update(overrides)
    return base


def _propose_session(
    *,
    claim: dict[str, Any] | None,
    policy_row: dict[str, Any] | None = None,
    rejection_row: dict[str, Any] | None = None,
    blast_radius: int = 0,
    supersedes_confirmation: bool = False,
    current_value_row: tuple[Any, ...] | None = None,
) -> tuple[MagicMock, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {"proposal_insert": [], "audit": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        if "FROM memory_claims WHERE claim_id" in sql and "t_invalidated_at IS NULL" in sql:
            return _mapping_first(claim)
        if "FROM memory_promotion_policy" in sql:
            return _mapping_first(policy_row)
        if "FROM memory_promotion_rejection" in sql:
            return _mapping_first(rejection_row)
        if "FROM edges WHERE dst_entity_id" in sql:
            return _scalar_one(blast_radius)
        if "SELECT 1 FROM memory_claims" in sql:
            return _bare_first((1,) if supersedes_confirmation else None)
        if "SELECT value FROM attributes" in sql:
            return _bare_first(current_value_row)
        if "SELECT dst_entity_id FROM edges WHERE src_entity_id" in sql:
            return _bare_first(current_value_row)
        if "INSERT INTO memory_promotion_proposal" in sql:
            calls["proposal_insert"].append(params or {})
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    return _session_factory(_execute), calls


def _promotion_service(
    *,
    factory: MagicMock,
    claims: MagicMock | None = None,
    pii_scanner: MagicMock | None = None,
) -> PromotionService:
    return PromotionService(
        factory,
        claims=claims or _claims_service(),
        clock=FakeClock(_NOW),
        pii_scanner=pii_scanner or _no_pii_scanner(),
    )


@pytest.mark.asyncio
async def test_propose_returns_none_when_the_claim_does_not_exist() -> None:
    factory, calls = _propose_session(claim=None)
    service = _promotion_service(factory=factory)

    result = await service.propose(uuid.uuid4())

    assert result is None
    assert calls["proposal_insert"] == []


@pytest.mark.asyncio
async def test_propose_returns_none_when_the_claim_is_ineligible() -> None:
    factory, calls = _propose_session(claim=_claim_row(is_contested=True))
    service = _promotion_service(factory=factory)

    result = await service.propose(uuid.uuid4())

    assert result is None
    assert calls["proposal_insert"] == []


@pytest.mark.asyncio
async def test_propose_returns_none_when_the_same_assertion_was_already_rejected_at_equal_authority() -> None:
    claim = _claim_row(source_authority="owner_extraction")
    rejection = {"rejected_authority": "owner_extraction"}
    factory, calls = _propose_session(claim=claim, rejection_row=rejection)
    service = _promotion_service(factory=factory)

    result = await service.propose(claim["claim_id"])

    assert result is None
    assert calls["proposal_insert"] == []


@pytest.mark.asyncio
async def test_propose_proceeds_when_arriving_authority_is_strictly_stronger_than_the_refused_one() -> None:
    """`owner_human` outranks the `owner_inference` tier that was refused --
    this is how a human overturns a rejection of a machine's claim."""
    claim = _claim_row(source_authority="owner_human")
    rejection = {"rejected_authority": "owner_inference"}
    factory, calls = _propose_session(claim=claim, rejection_row=rejection)
    service = _promotion_service(factory=factory)

    result = await service.propose(claim["claim_id"])

    assert result is not None
    assert len(calls["proposal_insert"]) == 1


@pytest.mark.asyncio
async def test_propose_never_scans_for_pii_staging_is_not_the_canonical_boundary() -> None:
    """PII scanning happens on the way into the canonical graph (`accept`),
    never on the way into staging."""
    claim = _claim_row()
    factory, _ = _propose_session(claim=claim)
    pii = _no_pii_scanner()
    service = _promotion_service(factory=factory, pii_scanner=pii)

    await service.propose(claim["claim_id"])

    pii.scan.assert_not_called()


@pytest.mark.asyncio
async def test_propose_same_tenant_claim_is_audited_as_promotion_proposed() -> None:
    owner = uuid.uuid4()
    claim = _claim_row(owning_tenant_id=owner)
    claim["author_tenant_id"] = owner
    factory, calls = _propose_session(claim=claim)
    service = _promotion_service(factory=factory)

    proposal = await service.propose(claim["claim_id"])

    assert proposal is not None
    assert calls["audit"][0]["action"] == actions.CLAIM_PROMOTION_PROPOSED


@pytest.mark.asyncio
async def test_propose_cross_tenant_claim_is_audited_as_proposal_routed() -> None:
    """A claim about another tenant's capability never writes to their
    graph -- it becomes a proposal addressed to them, and the audit trail
    must say so distinctly from an ordinary same-tenant proposal."""
    claim = _claim_row(owning_tenant_id=uuid.uuid4())
    claim["author_tenant_id"] = uuid.uuid4()
    factory, calls = _propose_session(claim=claim)
    service = _promotion_service(factory=factory)

    proposal = await service.propose(claim["claim_id"])

    assert proposal is not None
    assert calls["audit"][0]["action"] == actions.CLAIM_PROPOSAL_ROUTED


@pytest.mark.asyncio
async def test_propose_writes_the_claims_own_fields_onto_the_proposal_row() -> None:
    claim = _claim_row(predicate="owned_by_team", value="platform-team")
    factory, calls = _propose_session(claim=claim)
    service = _promotion_service(factory=factory)

    proposal = await service.propose(claim["claim_id"])

    assert proposal is not None
    row = calls["proposal_insert"][0]
    assert row["cid"] == claim["claim_id"]
    assert row["owner"] == claim["owning_tenant_id"]
    assert row["author"] == claim["author_tenant_id"]
    assert row["sid"] == claim["subject_entity_id"]
    assert row["pred"] == "owned_by_team"
    assert row["kind"] == TARGET_ATTRIBUTE
    assert row["prop"] == '"platform-team"'
    assert proposal.proposed_value == "platform-team"
    assert proposal.state == STATE_OPEN


@pytest.mark.asyncio
async def test_propose_reads_the_current_canonical_attribute_value_for_a_reviewer_to_compare_against() -> None:
    claim = _claim_row(predicate="owned_by_team")
    factory, calls = _propose_session(claim=claim, current_value_row=("previous-team",))
    service = _promotion_service(factory=factory)

    proposal = await service.propose(claim["claim_id"])

    assert proposal is not None
    assert proposal.current_value == "previous-team"
    assert calls["proposal_insert"][0]["cur"] == '"previous-team"'


@pytest.mark.asyncio
async def test_propose_reads_the_current_canonical_edge_destination_for_a_reviewer_to_compare_against() -> None:
    existing_dst = uuid.uuid4()
    claim = _claim_row(predicate="depends_on", value=str(uuid.uuid4()))
    factory, calls = _propose_session(claim=claim, current_value_row=(existing_dst,))
    service = _promotion_service(factory=factory)

    proposal = await service.propose(claim["claim_id"])

    assert proposal is not None
    assert proposal.current_value == str(existing_dst)
    assert calls["proposal_insert"][0]["kind"] == TARGET_EDGE


@pytest.mark.asyncio
async def test_propose_marks_the_claim_proposed_through_the_claims_service() -> None:
    claim = _claim_row()
    factory, _ = _propose_session(claim=claim)
    claims = _claims_service()
    service = _promotion_service(factory=factory, claims=claims)

    await service.propose(claim["claim_id"])

    claims.set_promotion_state.assert_awaited_once()
    call = claims.set_promotion_state.await_args
    assert call.kwargs["claim_id"] == claim["claim_id"]
    assert call.kwargs["state"] == "proposed"


# ---------------------------------------------------------------------------
# accept() -- _assert_may_review's two independent guard conditions
# ---------------------------------------------------------------------------


def _proposal_row(**overrides: Any) -> dict[str, Any]:
    owner = overrides.pop("owner_tenant_id", uuid.uuid4())
    base: dict[str, Any] = {
        "proposal_id": uuid.uuid4(),
        "claim_id": uuid.uuid4(),
        "owner_tenant_id": owner,
        "author_tenant_id": owner,
        "subject_entity_id": uuid.uuid4(),
        "predicate": "owned_by_team",
        "target_kind": TARGET_ATTRIBUTE,
        "target_key": "owned_by_team",
        "proposed_value": "platform",
        "valid_from": _NOW,
        "valid_to": None,
        "state": STATE_OPEN,
    }
    base.update(overrides)
    return base


def _accept_session(
    *,
    proposal: dict[str, Any] | None,
) -> tuple[MagicMock, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {"journal_insert": [], "proposal_update": [], "audit": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        if "FROM memory_promotion_proposal WHERE proposal_id" in sql:
            return _mapping_first(proposal)
        if "INSERT INTO memory_promotion_journal" in sql:
            calls["journal_insert"].append(params or {})
            return MagicMock()
        if "UPDATE memory_promotion_proposal" in sql:
            calls["proposal_update"].append(params or {})
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    return _session_factory(_execute), calls


@pytest.mark.asyncio
async def test_accept_raises_not_found_for_an_unknown_proposal() -> None:
    factory, calls = _accept_session(proposal=None)
    service = _promotion_service(factory=factory)

    with pytest.raises(NotFoundError):
        await service.accept(
            uuid.uuid4(), actor_tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=frozenset({"admin"})
        )
    assert calls["journal_insert"] == []


@pytest.mark.asyncio
async def test_accept_raises_conflict_for_a_proposal_that_is_no_longer_open() -> None:
    proposal = _proposal_row(state=STATE_ACCEPTED)
    factory, calls = _accept_session(proposal=proposal)
    service = _promotion_service(factory=factory)

    with pytest.raises(ConflictError, match="already accepted"):
        await service.accept(
            proposal["proposal_id"],
            actor_tenant_id=proposal["owner_tenant_id"],
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
        )
    assert calls["journal_insert"] == []


@pytest.mark.asyncio
async def test_accept_refuses_a_reviewer_from_a_different_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Right role, wrong tenant. `_assert_may_review` checks tenant and role
    as two separate conditions -- collapsing them would let this pass by
    accident."""
    monkeypatch.setattr(
        promotion_module.attribute_writes, "write_attribute", AsyncMock(side_effect=AssertionError("must not write"))
    )
    proposal = _proposal_row()
    factory, calls = _accept_session(proposal=proposal)
    service = _promotion_service(factory=factory)

    with pytest.raises(PermissionError, match="owns the subject"):
        await service.accept(
            proposal["proposal_id"],
            actor_tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
        )
    assert calls["journal_insert"] == []


@pytest.mark.asyncio
async def test_accept_refuses_the_owning_tenant_without_a_review_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Right tenant, wrong role. The independent counterpart of the test
    above."""
    monkeypatch.setattr(
        promotion_module.attribute_writes, "write_attribute", AsyncMock(side_effect=AssertionError("must not write"))
    )
    owner = uuid.uuid4()
    proposal = _proposal_row(owner_tenant_id=owner)
    factory, calls = _accept_session(proposal=proposal)
    service = _promotion_service(factory=factory)

    with pytest.raises(PermissionError, match="producer or admin role"):
        await service.accept(
            proposal["proposal_id"],
            actor_tenant_id=owner,
            actor_id=uuid.uuid4(),
            roles=frozenset({"viewer"}),
        )
    assert calls["journal_insert"] == []


@pytest.mark.asyncio
async def test_accept_writes_the_unamended_proposed_value_and_journals_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid.uuid4()
    proposal = _proposal_row(owner_tenant_id=owner, proposed_value="platform")
    factory, calls = _accept_session(proposal=proposal)
    created_id, superseded_id, superseded_valid_to = uuid.uuid4(), uuid.uuid4(), _NOW
    write_attribute = AsyncMock(return_value=(created_id, superseded_id, superseded_valid_to))
    monkeypatch.setattr(promotion_module.attribute_writes, "write_attribute", write_attribute)
    claims = _claims_service()
    service = _promotion_service(factory=factory, claims=claims)

    actor_id = uuid.uuid4()
    promotion_id = await service.accept(
        proposal["proposal_id"],
        actor_tenant_id=owner,
        actor_id=actor_id,
        roles=frozenset({"producer"}),
    )

    write_attribute.assert_awaited_once()
    assert write_attribute.await_args.kwargs["value"] == "platform"

    journal_row = calls["journal_insert"][0]
    assert journal_row["pid"] == promotion_id
    assert journal_row["cid"] == proposal["claim_id"]
    assert journal_row["tid"] == owner
    assert journal_row["created"] == created_id
    assert journal_row["superseded"] == superseded_id
    assert journal_row["sv"] == superseded_valid_to

    proposal_update = calls["proposal_update"][0]
    assert proposal_update["state"] == STATE_ACCEPTED
    assert proposal_update["amended"] is None

    claims.set_promotion_state.assert_awaited_once()
    assert claims.set_promotion_state.await_args.kwargs["state"] == "promoted"

    assert calls["audit"][0]["action"] == actions.CLAIM_PROMOTED


@pytest.mark.asyncio
async def test_accept_with_an_amendment_writes_the_amended_value_and_marks_state_amended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid.uuid4()
    proposal = _proposal_row(owner_tenant_id=owner, proposed_value="platform")
    factory, calls = _accept_session(proposal=proposal)
    write_attribute = AsyncMock(return_value=(uuid.uuid4(), None, None))
    monkeypatch.setattr(promotion_module.attribute_writes, "write_attribute", write_attribute)
    service = _promotion_service(factory=factory)

    await service.accept(
        proposal["proposal_id"],
        actor_tenant_id=owner,
        actor_id=uuid.uuid4(),
        roles=frozenset({"admin"}),
        amended_value="platform-team-2",
    )

    assert write_attribute.await_args.kwargs["value"] == "platform-team-2"
    proposal_update = calls["proposal_update"][0]
    assert proposal_update["state"] == STATE_AMENDED
    assert proposal_update["amended"] == '"platform-team-2"'


@pytest.mark.asyncio
async def test_accept_scans_the_amended_value_for_pii_not_the_original_proposed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewer correcting a value must not be able to introduce PII that
    was never in the claim -- the scan must see what is actually about to be
    written, not what the machine originally proposed."""
    owner = uuid.uuid4()
    proposal = _proposal_row(owner_tenant_id=owner, proposed_value="clean-value")
    factory, _ = _accept_session(proposal=proposal)
    monkeypatch.setattr(
        promotion_module.attribute_writes,
        "write_attribute",
        AsyncMock(side_effect=AssertionError("must not reach the write path")),
    )

    def _scan(text: str, **_kwargs: Any) -> PiiScanResponse:
        if text == "contains-pii":
            return PiiScanResponse(
                matched_patterns=[PiiMatchResult(name="email", offset=0, length=5, category="CONTACT")],
                action_taken="block",
            )
        return PiiScanResponse(matched_patterns=[], action_taken="advisory")

    pii = MagicMock()
    pii.scan = MagicMock(side_effect=_scan)
    service = _promotion_service(factory=factory, pii_scanner=pii)

    with pytest.raises(ValidationError):
        await service.accept(
            proposal["proposal_id"],
            actor_tenant_id=owner,
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
            amended_value="contains-pii",
        )

    # The proposed value alone (never scanned as "contains-pii") would not
    # have blocked -- proving the block came from the amendment, not from
    # some blanket refusal.
    assert pii.scan.call_args_list[0].args[0] == "contains-pii"


@pytest.mark.asyncio
async def test_accept_a_non_string_value_is_never_sent_to_the_pii_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A boolean-valued claim (e.g. `is_publicly_callable`) has nothing a
    pattern scanner can match against -- the scan is skipped outright rather
    than stringified and scanned anyway."""
    owner = uuid.uuid4()
    proposal = _proposal_row(owner_tenant_id=owner, predicate="is_publicly_callable", proposed_value=False)
    factory, _ = _accept_session(proposal=proposal)
    monkeypatch.setattr(
        promotion_module.attribute_writes, "write_attribute", AsyncMock(return_value=(uuid.uuid4(), None, None))
    )
    pii = _no_pii_scanner()
    service = _promotion_service(factory=factory, pii_scanner=pii)

    await service.accept(
        proposal["proposal_id"], actor_tenant_id=owner, actor_id=uuid.uuid4(), roles=frozenset({"admin"})
    )

    pii.scan.assert_not_called()


@pytest.mark.asyncio
async def test_accept_auto_promoted_is_the_callers_explicit_signal_not_inferred_from_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A human admin and the sweep's system-curator identity can both
    present `roles={"admin"}` -- `_assert_may_review` only checks
    membership, never identity. `auto_promoted` must come from the caller,
    or a human review would be misattributed as automatic."""
    owner = uuid.uuid4()
    monkeypatch.setattr(
        promotion_module.attribute_writes, "write_attribute", AsyncMock(return_value=(uuid.uuid4(), None, None))
    )

    before_auto = _ACCEPTED.labels(auto_promoted="true")._value.get()
    before_human = _ACCEPTED.labels(auto_promoted="false")._value.get()

    factory, _ = _accept_session(proposal=_proposal_row(owner_tenant_id=owner))
    service = _promotion_service(factory=factory)
    await service.accept(
        uuid.uuid4(),
        actor_tenant_id=owner,
        actor_id=uuid.uuid4(),
        roles=frozenset({"admin"}),
        auto_promoted=True,
    )
    assert _ACCEPTED.labels(auto_promoted="true")._value.get() == before_auto + 1
    assert _ACCEPTED.labels(auto_promoted="false")._value.get() == before_human

    factory2, _ = _accept_session(proposal=_proposal_row(owner_tenant_id=owner))
    service2 = _promotion_service(factory=factory2)
    await service2.accept(
        uuid.uuid4(),
        actor_tenant_id=owner,
        actor_id=uuid.uuid4(),
        roles=frozenset({"admin"}),
    )
    assert _ACCEPTED.labels(auto_promoted="false")._value.get() == before_human + 1
    assert _ACCEPTED.labels(auto_promoted="true")._value.get() == before_auto + 1


@pytest.mark.asyncio
async def test_accept_delegates_edge_valued_proposals_to_write_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = uuid.uuid4()
    dst = uuid.uuid4()
    proposal = _proposal_row(owner_tenant_id=owner, target_kind=TARGET_EDGE, proposed_value=str(dst))
    factory, _ = _accept_session(proposal=proposal)
    write_edge = AsyncMock(return_value=(uuid.uuid4(), None, None))
    monkeypatch.setattr(promotion_module.attribute_writes, "write_edge", write_edge)
    service = _promotion_service(factory=factory)

    await service.accept(
        proposal["proposal_id"],
        actor_tenant_id=owner,
        actor_id=uuid.uuid4(),
        roles=frozenset({"admin"}),
    )

    write_edge.assert_awaited_once()
    assert write_edge.await_args.kwargs["dst_entity_id"] == dst


@pytest.mark.asyncio
async def test_accept_an_edge_valued_proposal_with_an_unresolvable_value_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid.uuid4()
    proposal = _proposal_row(owner_tenant_id=owner, target_kind=TARGET_EDGE, proposed_value="not-a-uuid")
    factory, _ = _accept_session(proposal=proposal)
    monkeypatch.setattr(
        promotion_module.attribute_writes, "write_edge", AsyncMock(side_effect=AssertionError("must not reach write"))
    )
    service = _promotion_service(factory=factory)

    with pytest.raises(ValidationError, match="resolved entity"):
        await service.accept(
            proposal["proposal_id"],
            actor_tenant_id=owner,
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
        )


# ---------------------------------------------------------------------------
# reject()
# ---------------------------------------------------------------------------


def _reject_session(
    *,
    proposal: dict[str, Any] | None,
    claim_authority: str = "owner_human",
) -> tuple[MagicMock, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {"proposal_update": [], "rejection_insert": [], "audit": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        if "FROM memory_promotion_proposal WHERE proposal_id" in sql:
            return _mapping_first(proposal)
        if "SELECT source_authority FROM memory_claims" in sql:
            return _scalar_one(claim_authority)
        if "UPDATE memory_promotion_proposal" in sql:
            calls["proposal_update"].append(params or {})
            return MagicMock()
        if "INSERT INTO memory_promotion_rejection" in sql:
            calls["rejection_insert"].append(params or {})
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    return _session_factory(_execute), calls


@pytest.mark.asyncio
async def test_reject_rejects_an_unknown_reason_before_touching_the_session() -> None:
    factory, calls = _reject_session(proposal=_proposal_row())
    service = _promotion_service(factory=factory)

    with pytest.raises(ValidationError, match="rejection reason"):
        await service.reject(
            uuid.uuid4(),
            actor_tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
            reason="not_a_real_reason",
        )
    assert calls["proposal_update"] == []


@pytest.mark.asyncio
async def test_reject_raises_not_found_for_an_unknown_proposal() -> None:
    factory, calls = _reject_session(proposal=None)
    service = _promotion_service(factory=factory)

    with pytest.raises(NotFoundError):
        await service.reject(
            uuid.uuid4(),
            actor_tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
            reason="incorrect",
        )
    assert calls["proposal_update"] == []


@pytest.mark.asyncio
async def test_reject_refuses_a_reviewer_outside_the_owning_tenant() -> None:
    proposal = _proposal_row()
    factory, calls = _reject_session(proposal=proposal)
    service = _promotion_service(factory=factory)

    with pytest.raises(PermissionError):
        await service.reject(
            proposal["proposal_id"],
            actor_tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
            reason="incorrect",
        )
    assert calls["proposal_update"] == []


@pytest.mark.asyncio
async def test_reject_records_the_rejection_keyed_by_value_digest_and_authority() -> None:
    owner = uuid.uuid4()
    proposal = _proposal_row(owner_tenant_id=owner, proposed_value="platform")
    factory, calls = _reject_session(proposal=proposal, claim_authority="owner_extraction")
    claims = _claims_service()
    service = _promotion_service(factory=factory, claims=claims)

    await service.reject(
        proposal["proposal_id"],
        actor_tenant_id=owner,
        actor_id=uuid.uuid4(),
        roles=frozenset({"producer"}),
        reason="incorrect",
    )

    rejection_row = calls["rejection_insert"][0]
    assert rejection_row["digest"] == value_digest("platform")
    assert rejection_row["auth"] == "owner_extraction"
    assert rejection_row["reason"] == "incorrect"

    proposal_update = calls["proposal_update"][0]
    assert proposal_update["reason"] == "incorrect"

    claims.set_promotion_state.assert_awaited_once()
    assert claims.set_promotion_state.await_args.kwargs["state"] == "rejected"

    assert calls["audit"][0]["action"] == actions.CLAIM_PROMOTION_REJECTED


# ---------------------------------------------------------------------------
# reverse() -- the still_live / built_on guard pair, including the stacked
# case, and predecessor-interval restoration.
# ---------------------------------------------------------------------------


def _journal_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "promotion_id": uuid.uuid4(),
        "claim_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "target_kind": TARGET_ATTRIBUTE,
        "created_row_id": uuid.uuid4(),
        "superseded_row_id": None,
        "superseded_valid_to": None,
        "reversed_at": None,
    }
    base.update(overrides)
    return base


def _reverse_session(
    *,
    journal: dict[str, Any] | None,
    still_live: bool,
    built_on: bool,
) -> tuple[MagicMock, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {
        "close_created": [],
        "restore_predecessor": [],
        "journal_update": [],
        "audit": [],
        "executed": [],
    }

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        calls["executed"].append(sql)
        if "FROM memory_promotion_journal WHERE promotion_id" in sql and "FOR UPDATE" in sql:
            return _mapping_first(journal)
        if "t_invalidated_at IS NULL" in sql and ("FROM attributes" in sql or "FROM edges" in sql):
            return _bare_first((1,) if still_live else None)
        if "FROM memory_promotion_journal WHERE superseded_row_id" in sql:
            return _bare_first((1,) if built_on else None)
        if "SET t_invalidated_at = :now" in sql:
            calls["close_created"].append(params or {})
            return MagicMock()
        if "SET t_valid_to = :vt, t_invalidated_at = NULL" in sql:
            calls["restore_predecessor"].append(params or {})
            return MagicMock()
        if "UPDATE memory_promotion_journal" in sql and "SET reversed_at" in sql:
            calls["journal_update"].append(params or {})
            return MagicMock()
        if "INSERT INTO audit_log" in sql:
            calls["audit"].append(params or {})
            return MagicMock()
        raise AssertionError(f"unexpected SQL in test session: {sql}")

    return _session_factory(_execute), calls


@pytest.mark.asyncio
async def test_reverse_raises_not_found_for_an_unknown_promotion_id() -> None:
    factory, calls = _reverse_session(journal=None, still_live=True, built_on=False)
    service = _promotion_service(factory=factory)

    with pytest.raises(NotFoundError):
        await service.reverse(
            uuid.uuid4(), actor_tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), roles=frozenset({"admin"}), reason="oops"
        )
    assert calls["journal_update"] == []


@pytest.mark.asyncio
async def test_reverse_refuses_a_promotion_already_reversed() -> None:
    journal = _journal_row(reversed_at=_NOW)
    factory, calls = _reverse_session(journal=journal, still_live=True, built_on=False)
    service = _promotion_service(factory=factory)

    with pytest.raises(ConflictError, match="already reversed"):
        await service.reverse(
            journal["promotion_id"],
            actor_tenant_id=journal["tenant_id"],
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
            reason="oops",
        )
    assert calls["journal_update"] == []


@pytest.mark.asyncio
async def test_reverse_refuses_an_actor_outside_the_owning_tenant() -> None:
    journal = _journal_row()
    factory, calls = _reverse_session(journal=journal, still_live=True, built_on=False)
    service = _promotion_service(factory=factory)

    with pytest.raises(PermissionError):
        await service.reverse(
            journal["promotion_id"],
            actor_tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
            reason="oops",
        )
    assert calls["journal_update"] == []


@pytest.mark.asyncio
async def test_reverse_refuses_an_actor_without_a_review_role() -> None:
    journal = _journal_row()
    factory, calls = _reverse_session(journal=journal, still_live=True, built_on=False)
    service = _promotion_service(factory=factory)

    with pytest.raises(PermissionError):
        await service.reverse(
            journal["promotion_id"],
            actor_tenant_id=journal["tenant_id"],
            actor_id=uuid.uuid4(),
            roles=frozenset({"viewer"}),
            reason="oops",
        )
    assert calls["journal_update"] == []


@pytest.mark.asyncio
async def test_reverse_refuses_when_the_created_row_was_closed_outright() -> None:
    """The ordinary invalidation case: the row this promotion wrote no
    longer exists as a live row at all."""
    journal = _journal_row()
    factory, calls = _reverse_session(journal=journal, still_live=False, built_on=False)
    service = _promotion_service(factory=factory)

    with pytest.raises(ConflictError, match="no longer live"):
        await service.reverse(
            journal["promotion_id"],
            actor_tenant_id=journal["tenant_id"],
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
            reason="oops",
        )
    assert calls["journal_update"] == []


@pytest.mark.asyncio
async def test_reverse_refuses_the_stacked_case_even_though_the_row_is_still_live() -> None:
    """The case a still-live check alone would miss: a later promotion
    narrowed this row's interval (superseded it) without invalidating it,
    so `t_invalidated_at` is still NULL -- `still_live` alone would say
    "yes, reverse it" and silently clobber the later change. `built_on`
    is what actually catches this."""
    journal = _journal_row()
    factory, calls = _reverse_session(journal=journal, still_live=True, built_on=True)
    service = _promotion_service(factory=factory)

    with pytest.raises(ConflictError, match="reverse the later change first"):
        await service.reverse(
            journal["promotion_id"],
            actor_tenant_id=journal["tenant_id"],
            actor_id=uuid.uuid4(),
            roles=frozenset({"admin"}),
            reason="oops",
        )
    assert calls["close_created"] == []
    assert calls["journal_update"] == []


@pytest.mark.asyncio
async def test_reverse_built_on_query_excludes_later_promotions_that_were_themselves_reversed() -> None:
    """Pinned at the SQL-text level, since a real "later promotion was
    itself reversed" scenario needs Postgres to exercise for real: the
    stacked-case guard's own query must filter on `reversed_at IS NULL`, or
    a later promotion that was undone would still block this one forever."""
    journal = _journal_row()
    factory, calls = _reverse_session(journal=journal, still_live=True, built_on=False)
    service = _promotion_service(factory=factory)

    await service.reverse(
        journal["promotion_id"],
        actor_tenant_id=journal["tenant_id"],
        actor_id=uuid.uuid4(),
        roles=frozenset({"admin"}),
        reason="oops",
    )

    built_on_sql = next(s for s in calls["executed"] if "FROM memory_promotion_journal WHERE superseded_row_id" in s)
    assert "reversed_at IS NULL" in built_on_sql


@pytest.mark.asyncio
async def test_reverse_closes_the_created_row_and_journals_the_reversal_when_permitted() -> None:
    journal = _journal_row(superseded_row_id=None)
    factory, calls = _reverse_session(journal=journal, still_live=True, built_on=False)
    claims = _claims_service()
    service = _promotion_service(factory=factory, claims=claims)

    actor_id = uuid.uuid4()
    await service.reverse(
        journal["promotion_id"],
        actor_tenant_id=journal["tenant_id"],
        actor_id=actor_id,
        roles=frozenset({"admin"}),
        reason="wrong subject",
    )

    close_params = calls["close_created"][0]
    assert close_params["rid"] == journal["created_row_id"]
    assert calls["restore_predecessor"] == []

    journal_update = calls["journal_update"][0]
    assert journal_update["pid"] == journal["promotion_id"]
    assert journal_update["actor"] == actor_id
    assert journal_update["reason"] == "wrong subject"

    claims.set_promotion_state.assert_awaited_once()
    assert claims.set_promotion_state.await_args.kwargs["claim_id"] == journal["claim_id"]
    assert claims.set_promotion_state.await_args.kwargs["state"] == "reversed"

    assert calls["audit"][0]["action"] == actions.CLAIM_PROMOTION_REVERSED


@pytest.mark.asyncio
async def test_reverse_restores_the_predecessors_interval_when_one_was_superseded() -> None:
    """Not merely the predecessor's value -- its interval, so an `as_of`
    query spanning the promotion sees what it saw before, without the
    reversal itself showing up as a gap."""
    predecessor_id = uuid.uuid4()
    predecessor_valid_to = _NOW - datetime.timedelta(days=1)
    journal = _journal_row(superseded_row_id=predecessor_id, superseded_valid_to=predecessor_valid_to)
    factory, calls = _reverse_session(journal=journal, still_live=True, built_on=False)
    service = _promotion_service(factory=factory)

    await service.reverse(
        journal["promotion_id"],
        actor_tenant_id=journal["tenant_id"],
        actor_id=uuid.uuid4(),
        roles=frozenset({"admin"}),
        reason="wrong subject",
    )

    restore_params = calls["restore_predecessor"][0]
    assert restore_params["rid"] == predecessor_id
    assert restore_params["vt"] == predecessor_valid_to


@pytest.mark.asyncio
async def test_reverse_uses_the_edges_table_for_an_edge_valued_promotion() -> None:
    journal = _journal_row(target_kind=TARGET_EDGE)
    factory, calls = _reverse_session(journal=journal, still_live=True, built_on=False)
    service = _promotion_service(factory=factory)

    await service.reverse(
        journal["promotion_id"],
        actor_tenant_id=journal["tenant_id"],
        actor_id=uuid.uuid4(),
        roles=frozenset({"admin"}),
        reason="wrong subject",
    )

    still_live_sql = next(s for s in calls["executed"] if "t_invalidated_at IS NULL" in s and "FROM edges" in s)
    assert "edge_id = :rid" in still_live_sql
    close_sql = next(s for s in calls["executed"] if "SET t_invalidated_at = :now" in s)
    assert "UPDATE edges" in close_sql


# ---------------------------------------------------------------------------
# get_proposal / proposals_for / journal_for -- the read paths
# ---------------------------------------------------------------------------


def _full_proposal_row(**overrides: Any) -> dict[str, Any]:
    """Matches `_PROPOSAL_SELECT`'s column list exactly -- `get_proposal` and
    `proposals_for` share one query and one row-to-dataclass mapping."""
    owner = overrides.pop("owner_tenant_id", uuid.uuid4())
    base: dict[str, Any] = {
        "proposal_id": uuid.uuid4(),
        "claim_id": uuid.uuid4(),
        "owner_tenant_id": owner,
        "author_tenant_id": owner,
        "subject_entity_id": uuid.uuid4(),
        "predicate": "owned_by_team",
        "target_kind": TARGET_ATTRIBUTE,
        "target_key": "owned_by_team",
        "current_value": None,
        "proposed_value": "platform",
        "valid_from": _NOW,
        "valid_to": None,
        "high_impact_reasons": [],
        "state": STATE_OPEN,
        "created_at": _NOW,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_get_proposal_returns_none_for_an_unknown_id() -> None:
    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        return _mapping_first(None)

    service = _promotion_service(factory=_session_factory(_execute))

    assert await service.get_proposal(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_proposal_maps_the_row_including_high_impact_reasons() -> None:
    row = _full_proposal_row(high_impact_reasons=["blast radius exceeds the tenant threshold"])

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        return _mapping_first(row)

    service = _promotion_service(factory=_session_factory(_execute))

    proposal = await service.get_proposal(row["proposal_id"])

    assert proposal is not None
    assert proposal.proposal_id == row["proposal_id"]
    assert proposal.high_impact_reasons == ("blast radius exceeds the tenant threshold",)
    assert proposal.high_impact is True


@pytest.mark.asyncio
async def test_proposals_for_filters_by_tenant_and_state_and_orders_oldest_first() -> None:
    tenant_id = uuid.uuid4()
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        executed.append(" ".join(str(stmt).split()))
        result = MagicMock()
        result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    service = _promotion_service(factory=_session_factory(_execute))

    await service.proposals_for(tenant_id, state="open")

    sql = executed[0]
    assert "owner_tenant_id = :tid" in sql
    assert "state = CAST(:state AS TEXT)" in sql
    assert "ORDER BY created_at, proposal_id" in sql


@pytest.mark.asyncio
async def test_proposals_for_fetches_one_extra_row_to_signal_another_page_without_a_count_query() -> None:
    tenant_id = uuid.uuid4()
    captured: dict[str, Any] = {}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        captured["params"] = params
        result = MagicMock()
        result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    service = _promotion_service(factory=_session_factory(_execute))

    await service.proposals_for(tenant_id, page_size=25)

    assert captured["params"]["limit"] == 26


@pytest.mark.asyncio
async def test_proposals_for_applies_the_keyset_cursor_condition_when_given() -> None:
    tenant_id = uuid.uuid4()
    cursor_id = uuid.uuid4()
    executed: list[str] = []
    captured: dict[str, Any] = {}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        executed.append(" ".join(str(stmt).split()))
        captured["params"] = params
        result = MagicMock()
        result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    service = _promotion_service(factory=_session_factory(_execute))

    await service.proposals_for(tenant_id, cursor=(_NOW, cursor_id))

    sql = executed[0]
    assert "(created_at, proposal_id) > (:cursor_created_at, :cursor_proposal_id)" in sql
    assert captured["params"]["cursor_created_at"] == _NOW
    assert captured["params"]["cursor_proposal_id"] == cursor_id


@pytest.mark.asyncio
async def test_proposals_for_without_a_cursor_issues_no_cursor_condition() -> None:
    tenant_id = uuid.uuid4()
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        executed.append(" ".join(str(stmt).split()))
        result = MagicMock()
        result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    service = _promotion_service(factory=_session_factory(_execute))

    await service.proposals_for(tenant_id)

    assert "cursor_created_at" not in executed[0]


@pytest.mark.asyncio
async def test_journal_for_orders_by_promoted_at_and_maps_reversal_fields() -> None:
    claim_id = uuid.uuid4()
    row = {
        "promotion_id": uuid.uuid4(),
        "proposal_id": uuid.uuid4(),
        "claim_id": claim_id,
        "tenant_id": uuid.uuid4(),
        "target_kind": TARGET_ATTRIBUTE,
        "created_row_id": uuid.uuid4(),
        "superseded_row_id": None,
        "superseded_valid_to": None,
        "promoted_at": _NOW,
        "promoted_by": uuid.uuid4(),
        "reversed_at": None,
        "reversed_by": None,
        "reversal_reason": None,
    }
    executed: list[str] = []

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        executed.append(" ".join(str(stmt).split()))
        result = MagicMock()
        result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[row])))
        return result

    service = _promotion_service(factory=_session_factory(_execute))

    entries = await service.journal_for(claim_id)

    assert "ORDER BY promoted_at" in executed[0]
    assert len(entries) == 1
    assert entries[0].promotion_id == row["promotion_id"]
    assert entries[0].is_reversed is False


# ---------------------------------------------------------------------------
# oldest_open_proposal_created_at -- the bare, cross-tenant read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oldest_open_proposal_created_at_reads_across_every_tenant() -> None:
    """No tenant filter in the query at all -- the one read on this module
    that is deliberately not scoped to the caller's own queue."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=_NOW)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)

    result = await promotion_module.oldest_open_proposal_created_at(session)

    assert result == _NOW
    sql = " ".join(str(session.execute.await_args.args[0]).split())
    assert sql == "SELECT min(created_at) FROM memory_promotion_proposal WHERE state = 'open'"


# ---------------------------------------------------------------------------
# erase_promotion_artifacts -- the reversal-shaped restoration erasure needs,
# including its own stacked-case guard.
# ---------------------------------------------------------------------------


def _erase_router(
    *,
    journals: list[dict[str, Any]],
    occupied_by_created_row_id: dict[uuid.UUID, bool],
    delete_rowcount: int = 1,
    reopen_rowcount: int = 1,
) -> tuple[AsyncMock, dict[str, list[dict[str, Any]]]]:
    calls: dict[str, list[dict[str, Any]]] = {"delete_canonical": [], "reopen_predecessor": []}

    async def _execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        sql = " ".join(str(stmt).split())
        # Order matters: the bulk `memory_promotion_*` deletes and the
        # per-journal canonical-table delete/update all start with
        # "DELETE FROM"/"UPDATE", so each branch below matches on the one
        # substring that is unique to it, checked most-specific-first.
        if "FROM memory_promotion_journal WHERE claim_id = ANY(:cids)" in sql and "FOR UPDATE" in sql:
            result = MagicMock()
            result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=journals)))
            return result
        if "SELECT 1 FROM memory_promotion_journal WHERE superseded_row_id" in sql:
            rid = (params or {})["rid"]
            occupied = occupied_by_created_row_id.get(rid, False)
            result = MagicMock()
            result.first = MagicMock(return_value=(1,) if occupied else None)
            return result
        if "DELETE FROM memory_promotion_rejection" in sql:
            result = MagicMock()
            result.rowcount = 3
            return result
        if "DELETE FROM memory_promotion_journal WHERE claim_id = ANY(:cids)" in sql:
            result = MagicMock()
            result.rowcount = 2
            return result
        if "DELETE FROM memory_promotion_proposal WHERE claim_id = ANY(:cids)" in sql:
            result = MagicMock()
            result.rowcount = 4
            return result
        if sql.startswith("UPDATE") and "t_valid_to" in sql:
            calls["reopen_predecessor"].append(params or {})
            result = MagicMock()
            result.rowcount = reopen_rowcount
            return result
        if sql.startswith("DELETE FROM"):
            calls["delete_canonical"].append(params or {})
            result = MagicMock()
            result.rowcount = delete_rowcount
            return result
        raise AssertionError(f"unexpected SQL: {sql}")

    session = AsyncMock()
    session.execute = _execute
    return session, calls


@pytest.mark.asyncio
async def test_erase_promotion_artifacts_short_circuits_on_an_empty_claim_list() -> None:
    session = AsyncMock()

    result = await promotion_module.erase_promotion_artifacts(session, [])

    assert result == {
        "canonical_rows_deleted": 0,
        "canonical_rows_reopened": 0,
        "journal_rows_deleted": 0,
        "proposals_deleted": 0,
        "rejections_deleted": 0,
    }
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_erase_promotion_artifacts_reopens_the_predecessor_when_nothing_is_built_on_it() -> None:
    """The ordinary case: this promotion's predecessor was never superseded
    again, so erasing the promotion's row restores the predecessor exactly
    as a reversal would."""
    created_row_id = uuid.uuid4()
    predecessor_id = uuid.uuid4()
    predecessor_valid_to = _NOW
    journal = {
        "promotion_id": uuid.uuid4(),
        "target_kind": TARGET_ATTRIBUTE,
        "created_row_id": created_row_id,
        "superseded_row_id": predecessor_id,
        "superseded_valid_to": predecessor_valid_to,
    }
    session, calls = _erase_router(journals=[journal], occupied_by_created_row_id={created_row_id: False})

    result = await promotion_module.erase_promotion_artifacts(session, [journal["promotion_id"]])

    assert calls["delete_canonical"][0]["rid"] == created_row_id
    assert calls["reopen_predecessor"][0]["rid"] == predecessor_id
    assert calls["reopen_predecessor"][0]["vt"] == predecessor_valid_to
    assert result["canonical_rows_deleted"] == 1
    assert result["canonical_rows_reopened"] == 1


@pytest.mark.asyncio
async def test_erase_promotion_artifacts_leaves_a_later_promotion_as_the_head_when_the_slot_is_occupied() -> None:
    """The stacked case, from erasure's own side: a later, un-reversed
    promotion is already built on the row being erased. That later row is
    someone else's claim and must stay the live head -- the erased row's own
    predecessor must NOT be reopened underneath it."""
    created_row_id = uuid.uuid4()
    predecessor_id = uuid.uuid4()
    journal = {
        "promotion_id": uuid.uuid4(),
        "target_kind": TARGET_ATTRIBUTE,
        "created_row_id": created_row_id,
        "superseded_row_id": predecessor_id,
        "superseded_valid_to": _NOW,
    }
    session, calls = _erase_router(journals=[journal], occupied_by_created_row_id={created_row_id: True})

    result = await promotion_module.erase_promotion_artifacts(session, [journal["promotion_id"]])

    assert calls["delete_canonical"][0]["rid"] == created_row_id
    assert calls["reopen_predecessor"] == []
    assert result["canonical_rows_deleted"] == 1
    assert result["canonical_rows_reopened"] == 0


@pytest.mark.asyncio
async def test_erase_promotion_artifacts_with_no_predecessor_never_attempts_a_reopen() -> None:
    """The first promotion in a claim's history has nothing to restore."""
    created_row_id = uuid.uuid4()
    journal = {
        "promotion_id": uuid.uuid4(),
        "target_kind": TARGET_EDGE,
        "created_row_id": created_row_id,
        "superseded_row_id": None,
        "superseded_valid_to": None,
    }
    session, calls = _erase_router(journals=[journal], occupied_by_created_row_id={})

    result = await promotion_module.erase_promotion_artifacts(session, [journal["promotion_id"]])

    assert calls["reopen_predecessor"] == []
    assert result["canonical_rows_reopened"] == 0


@pytest.mark.asyncio
async def test_erase_promotion_artifacts_reports_rowcounts_from_the_final_bulk_deletes() -> None:
    session, _ = _erase_router(journals=[], occupied_by_created_row_id={})

    result = await promotion_module.erase_promotion_artifacts(session, [uuid.uuid4()])

    assert result["journal_rows_deleted"] == 2
    assert result["proposals_deleted"] == 4
    assert result["rejections_deleted"] == 3
