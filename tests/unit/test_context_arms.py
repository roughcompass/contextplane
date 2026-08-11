"""What each of the four arms returns, and what the assembler makes of it.

The composer's whole job is two decisions per block: which service answers it,
and what trust the answer carries. Both are testable without a database, because
the assembler takes arms as callables — so every path below drives a real
`assemble()` over fake services and asserts on the block that came out.

Block state is asserted rather than the outcome fields, deliberately. The four
states are what a caller reads, and the mapping from an arm's facts to a state
lives in the assembler; asserting `ArmOutcome.truncated` would pass while the
block it produces says `success`.

The failed cases matter most. A composer that swallows a broken service and
returns an empty block turns "this is missing" into "there is none of this",
which is the one reading the whole four-block contract exists to prevent.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from contextplane.context.arms import ContextArms
from contextplane.context.assembler import ArmOutcome, AssemblyResult, assemble
from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_DEGRADED,
    BLOCK_EMPTY,
    BLOCK_FAILED,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_SUCCESS,
    BLOCK_WORKSPACE,
    ENVELOPE_BLOCKED,
    ENVELOPE_COMPLETE,
    ENVELOPE_DEGRADED,
)
from contextplane.service.memory.claim_serving import Citation, ServedClaim
from contextplane.types import EntityRef, FactRef, SearchResult, TenantContext

if TYPE_CHECKING:
    from contextplane.context.assembler import ContextArm
    from contextplane.context.schemas.envelope import ContextEnvelopeV1

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)
_EARLIER = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
_TENANT = uuid.uuid4()
_ACTOR = uuid.uuid4()


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=_ACTOR, roles=["consumer"])


# -- fakes -----------------------------------------------------------------
#
# Hand-written rather than AsyncMock: each one records what it was asked, so a
# test can assert the composer passed the caller's bounds through instead of
# only that something came back.


class _Boom(RuntimeError):
    """What a broken collaborator raises. Distinct so a test cannot pass by
    catching an error the composer itself produced."""


@dataclasses.dataclass
class _FakeRetrieval:
    results: list[SearchResult] = dataclasses.field(default_factory=list)
    raises: Exception | None = None
    asked_top_k: int | None = None

    async def search(self, ctx: TenantContext, q: str, top_k: int, temporal_filter: Any) -> list[SearchResult]:
        self.asked_top_k = top_k
        if self.raises is not None:
            raise self.raises
        return self.results


@dataclasses.dataclass
class _FakeClaims:
    served: tuple[ServedClaim, ...] = ()
    raises: Exception | None = None
    asked_limit: int | None = None

    async def query(self, ctx: TenantContext, spec: Any) -> tuple[ServedClaim, ...]:
        self.asked_limit = spec.limit
        if self.raises is not None:
            raise self.raises
        return self.served


@dataclasses.dataclass
class _FakeReceipts:
    receipt: dict[str, object] | None = None
    raises: Exception | None = None

    async def get_receipt(self, ctx: Any, receipt_id: uuid.UUID) -> dict[str, object]:
        if self.raises is not None:
            raise self.raises
        assert self.receipt is not None
        return self.receipt


@dataclasses.dataclass
class _FakeRecall:
    """Stands in for `WorkspaceRecall`, recording which arm was asked for."""

    outcome: ArmOutcome = dataclasses.field(default_factory=ArmOutcome)
    chosen: str | None = None

    def lexical_arm(self, **kwargs: Any) -> ContextArm:
        self.chosen = "lexical"

        async def arm() -> ArmOutcome:
            return self.outcome

        return arm

    def reference_arm(self, **kwargs: Any) -> ContextArm:
        self.chosen = "reference"

        async def arm() -> ArmOutcome:
            return self.outcome

        return arm


class _NoOverdueSession:
    """A session whose only answer is "nothing is overdue".

    The arms that serve withdrawable content now ask that question before they
    read, so an arm test needs a session even when the arm's own rows come from
    a fake. Answering zero is what a healthy deployment answers, which is the
    state these tests were written against and still assert.

    Deliberately a fake session rather than a guard that skips a falsy factory.
    Making the guard tolerate `None` would weaken enforcement to suit a test
    double, and a fail-closed check that switches itself off when its
    collaborator is missing is not fail-closed. The guard's own behaviour is
    proved elsewhere, against a real database and by removing it.
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        class _Result:
            @staticmethod
            def scalar_one() -> int:
                return 0

        return _Result()

    async def __aenter__(self) -> _NoOverdueSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _arms(
    *,
    retrieval: _FakeRetrieval | None = None,
    claims: _FakeClaims | None = None,
    receipts: _FakeReceipts | None = None,
    recall: _FakeRecall | None = None,
    session_factory: Any = None,
) -> ContextArms:
    return ContextArms(
        # Defaulted, not forced: the one test that supplies its own factory to
        # watch what an arm does with a session keeps overriding this.
        session_factory=session_factory or (lambda: _NoOverdueSession()),
        retrieval=retrieval or _FakeRetrieval(),
        claims=claims or _FakeClaims(),
        arc_receipts=receipts or _FakeReceipts(),
        recall=recall or _FakeRecall(),
    )


# -- fixtures for the rows the arms map ------------------------------------


def _search_result(name: str = "payments-api") -> SearchResult:
    entity_id = uuid.uuid4()
    return SearchResult(
        entity=EntityRef(
            entity_id=entity_id,
            tenant_id=_TENANT,
            entity_type="service",
            name=name,
            external_id="svc-1",
            is_active=True,
            created_at=_EARLIER,
        ),
        matching_facts=[
            FactRef(
                fact_id=uuid.uuid4(),
                tenant_id=_TENANT,
                entity_id=entity_id,
                category="interface_contract",
                body="accepts ISO-4217 currency codes",
                is_authoritative=True,
                is_authoritative_superseded=False,
                sync_run_id=None,
                t_valid_from=_EARLIER,
                t_valid_to=None,
                t_ingested_at=_EARLIER,
                t_invalidated_at=None,
            )
        ],
        score=0.87,
        retrieval_arms={"semantic": 0.5},
    )


def _claim(*, category: str = "interface_contract", human_confirmed: bool = False) -> ServedClaim:
    return ServedClaim(
        claim_id=uuid.uuid4(),
        subject_entity_id=uuid.uuid4(),
        predicate="requires_auth_scope",
        value="payments:write",
        claim_category=category,
        confidence=0.72,
        authority="tier-2-derived",
        valid_from=_EARLIER,
        valid_to=None,
        as_of=_EARLIER,
        human_confirmed=human_confirmed,
        citations=(Citation(kind="session_event", ref=str(uuid.uuid4())),),
    )


def _selected(
    *,
    was_omitted: bool = False,
    omission_reason: str | None = None,
    audience_redacted: bool = False,
) -> dict[str, Any]:
    return {
        "artifact_id": str(uuid.uuid4()),
        "revision_id": str(uuid.uuid4()),
        "directive_id": str(uuid.uuid4()),
        "is_mandatory": True,
        "was_omitted": was_omitted,
        "omission_reason": omission_reason,
        "source_locator": None if audience_redacted else "policies/payments.md",
        "source_revision_locator": None if audience_redacted else "abc123",
        "content_digest": None if audience_redacted else "f" * 64,
        "audience_redacted": audience_redacted,
    }


def _receipt(
    *,
    status: str = "ready",
    selected: list[dict[str, Any]] | None = None,
    attestation_id: str = "att-1",
    integrity_state: str = "valid",
    blocked_reasons: list[str] | None = None,
    degraded_reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "receipt_id": str(uuid.uuid4()),
        "evaluated_at": _EARLIER.isoformat(),
        "resolution_status": status,
        "attestation_id": attestation_id,
        "integrity_state": integrity_state,
        "blocked_reasons": blocked_reasons or [],
        "degraded_reasons": degraded_reasons or [],
        "selected": selected if selected is not None else [_selected()],
    }


async def _assemble(arms: dict[str, ContextArm]) -> AssemblyResult:
    """Assemble over the given arms, filling any absent block with an empty one.

    Every test drives the real assembler so the states asserted below are the
    ones a caller would receive, and so the selection evidence a receipt is
    written from is available to assert on.
    """

    async def _nothing() -> ArmOutcome:
        return ArmOutcome()

    return await assemble({name: arms.get(name, _nothing) for name in BLOCK_NAMES}, now=_NOW)


async def _envelope(arms: dict[str, ContextArm]) -> ContextEnvelopeV1:
    """The envelope alone, for the majority of cases that assert on block state."""
    return (await _assemble(arms)).envelope


# -- the mapping is total --------------------------------------------------


def test_for_request_returns_every_block_and_no_others() -> None:
    """The structural guarantee: an arm the assembler cannot find is reported as
    a failed block, which is indistinguishable from one that ran and broke."""
    arms = _arms().for_request(_ctx(), query="payments", moment=_NOW)

    assert tuple(arms) == BLOCK_NAMES


# -- canonical -------------------------------------------------------------


async def test_the_canonical_arm_returns_catalog_entities_without_trust_metadata() -> None:
    """Canonical items carry no trust by contract; the envelope refuses one that does."""
    retrieval = _FakeRetrieval(results=[_search_result()])
    arm = _arms(retrieval=retrieval).canonical_arm(_ctx(), query="payments", moment=_NOW)

    envelope = await _envelope({BLOCK_CANONICAL: arm})
    block = envelope.block(BLOCK_CANONICAL)

    assert block.state == BLOCK_SUCCESS
    assert [item.trust for item in block.items] == [None]
    assert block.items[0].payload["name"] == "payments-api"


async def test_the_canonical_arm_reads_one_past_its_bound_so_truncation_is_measured() -> None:
    """A full page and a truncated one are different answers; asking for one
    extra row is what tells them apart without a second count query."""
    retrieval = _FakeRetrieval(results=[_search_result(f"svc-{i}") for i in range(4)])
    arm = _arms(retrieval=retrieval).canonical_arm(_ctx(), query="payments", moment=_NOW, limit=3)

    envelope = await _envelope({BLOCK_CANONICAL: arm})
    block = envelope.block(BLOCK_CANONICAL)

    assert retrieval.asked_top_k == 4
    assert len(block.items) == 3
    assert block.state == BLOCK_DEGRADED
    assert "partial read" in (block.reason or "")


async def test_a_canonical_arm_with_nothing_to_say_is_empty_not_degraded() -> None:
    """An empty catalog answer is a complete answer."""
    envelope = await _envelope({BLOCK_CANONICAL: _arms().canonical_arm(_ctx(), query="nothing", moment=_NOW)})

    assert envelope.block(BLOCK_CANONICAL).state == BLOCK_EMPTY
    assert envelope.state == ENVELOPE_COMPLETE


async def test_a_broken_canonical_service_fails_the_block_and_blocks_the_envelope() -> None:
    """The canonical arm failing is the one failure that must stop the response:
    the surrounding context without the thing it surrounds reads as the whole
    picture rather than as a gap."""
    retrieval = _FakeRetrieval(raises=_Boom("catalog is down"))
    arm = _arms(retrieval=retrieval).canonical_arm(_ctx(), query="payments", moment=_NOW)

    envelope = await _envelope({BLOCK_CANONICAL: arm})

    assert envelope.block(BLOCK_CANONICAL).state == BLOCK_FAILED
    assert envelope.block(BLOCK_CANONICAL).items == ()
    assert envelope.state == ENVELOPE_BLOCKED


# -- ARC -------------------------------------------------------------------


async def test_the_arc_arm_serves_the_directives_a_receipt_attested() -> None:
    receipts = _FakeReceipts(receipt=_receipt())
    arm = _arms(receipts=receipts).arc_arm(object(), receipt_id=uuid.uuid4())

    block = (await _envelope({BLOCK_ARC: arm})).block(BLOCK_ARC)

    assert block.state == BLOCK_SUCCESS
    assert block.items[0].trust is not None
    assert block.items[0].trust.trust == "attested"
    assert block.items[0].trust.assertion_kind == "policy"
    assert block.items[0].trust.mutability == "immutable"


async def test_the_arc_arm_reports_the_receipts_instant_not_the_requests() -> None:
    """An attested resolution can be days old. Reporting it as fresh-now would
    defeat every staleness bound a caller sets."""
    receipts = _FakeReceipts(receipt=_receipt())
    arm = _arms(receipts=receipts).arc_arm(object(), receipt_id=uuid.uuid4())

    block = (await _envelope({BLOCK_ARC: arm})).block(BLOCK_ARC)

    assert block.items[0].trust is not None
    assert block.items[0].trust.freshness == _EARLIER


async def test_naming_no_arc_resolution_is_an_empty_block_not_a_failure() -> None:
    """Nothing was asked of ARC and nothing broke."""
    envelope = await _envelope({BLOCK_ARC: _arms().arc_arm(None, receipt_id=None)})

    assert envelope.block(BLOCK_ARC).state == BLOCK_EMPTY
    assert envelope.state == ENVELOPE_COMPLETE


async def test_an_arc_receipt_whose_chain_no_longer_verifies_drops_to_asserted() -> None:
    """The resolution still happened, so the directives are not withheld -- but
    nothing stands behind it any more, so it cannot keep full weight."""
    receipts = _FakeReceipts(receipt=_receipt(integrity_state="integrity_failed"))
    arm = _arms(receipts=receipts).arc_arm(object(), receipt_id=uuid.uuid4())

    block = (await _envelope({BLOCK_ARC: arm})).block(BLOCK_ARC)

    assert block.items[0].trust is not None
    assert block.items[0].trust.trust == "asserted"


@pytest.mark.parametrize(
    ("row", "expected_reason"),
    [
        (_selected(was_omitted=True, omission_reason="over budget"), "over budget"),
        (_selected(audience_redacted=True), "audience does not permit"),
    ],
)
async def test_a_withheld_directive_is_an_exclusion_that_degrades_the_block(
    row: dict[str, Any], expected_reason: str
) -> None:
    """Withheld is not missing. A reader who cannot tell "nothing" from
    "something you may not see" cannot know to ask for access.

    The reason is asserted from the recorded exclusion rather than from the row
    it came from: the two cases produce the same count and the same block state,
    so a count-only assertion passes with them swapped.
    """
    receipts = _FakeReceipts(receipt=_receipt(selected=[row]))
    arm = _arms(receipts=receipts).arc_arm(object(), receipt_id=uuid.uuid4())

    result = await _assemble({BLOCK_ARC: arm})
    block = result.envelope.block(BLOCK_ARC)
    exclusions = next(e.exclusions for e in result.evidence if e.block == BLOCK_ARC)

    assert block.state == BLOCK_DEGRADED
    assert block.items == ()
    assert "withheld" in (block.reason or "")
    assert [exclusion.item_key for exclusion in exclusions] == [row["directive_id"]]
    assert expected_reason in exclusions[0].reason


async def test_an_omitted_directive_with_no_recorded_reason_still_says_so() -> None:
    """A receipt is allowed to omit without explaining. An exclusion carrying an
    empty reason would read as "withheld for no reason", which is a different
    claim from "the record does not say"."""
    row = _selected(was_omitted=True, omission_reason=None)
    receipts = _FakeReceipts(receipt=_receipt(selected=[row]))
    arm = _arms(receipts=receipts).arc_arm(object(), receipt_id=uuid.uuid4())

    result = await _assemble({BLOCK_ARC: arm})
    exclusions = next(e.exclusions for e in result.evidence if e.block == BLOCK_ARC)

    assert "without recording a reason" in exclusions[0].reason


async def test_a_degraded_arc_resolution_degrades_the_block_with_the_receipts_own_reason() -> None:
    """Read from the record, never recomputed: an explanation that contradicts
    its own receipt is worse than none."""
    receipts = _FakeReceipts(receipt=_receipt(status="degraded", degraded_reasons=["one connector was stale"]))
    arm = _arms(receipts=receipts).arc_arm(object(), receipt_id=uuid.uuid4())

    block = (await _envelope({BLOCK_ARC: arm})).block(BLOCK_ARC)

    assert block.state == BLOCK_DEGRADED
    assert "one connector was stale" in (block.reason or "")


async def test_a_broken_arc_read_fails_its_block_but_leaves_the_envelope_answerable() -> None:
    """A non-canonical arm failing degrades the response; it does not block it."""
    receipts = _FakeReceipts(raises=_Boom("receipt store is down"))
    arm = _arms(receipts=receipts).arc_arm(object(), receipt_id=uuid.uuid4())

    envelope = await _envelope({BLOCK_ARC: arm})

    assert envelope.block(BLOCK_ARC).state == BLOCK_FAILED
    assert envelope.state == ENVELOPE_DEGRADED


# -- observed claims -------------------------------------------------------


async def test_an_unconfirmed_claim_is_observed_and_a_confirmed_one_is_asserted() -> None:
    """Confirmation is the difference between something the system noticed and
    something somebody stands behind. Neither is an attestation."""
    claims = _FakeClaims(served=(_claim(human_confirmed=False), _claim(human_confirmed=True)))
    arm = _arms(claims=claims).observed_claims_arm(_ctx(), moment=_NOW)

    block = (await _envelope({BLOCK_OBSERVED_CLAIMS: arm})).block(BLOCK_OBSERVED_CLAIMS)
    levels = {item.trust.trust for item in block.items if item.trust is not None}

    assert block.state == BLOCK_SUCCESS
    assert levels == {"observed", "asserted"}


@pytest.mark.parametrize(
    ("category", "kind"),
    [
        ("interface_contract", "fact"),
        ("dependency", "fact"),
        ("decision_rationale", "intent"),
        ("session_summary", "annotation"),
        ("a_category_nobody_has_defined_yet", "annotation"),
    ],
)
async def test_each_claim_category_states_what_kind_of_thing_it_asserts(category: str, kind: str) -> None:
    """An agent that cannot tell a measurement from an intention plans against a
    wish. An unrecognised category is not promoted to a fact."""
    claims = _FakeClaims(served=(_claim(category=category),))
    arm = _arms(claims=claims).observed_claims_arm(_ctx(), moment=_NOW)

    block = (await _envelope({BLOCK_OBSERVED_CLAIMS: arm})).block(BLOCK_OBSERVED_CLAIMS)

    assert block.items[0].trust is not None
    assert block.items[0].trust.assertion_kind == kind


async def test_a_recalled_claim_is_mutable_because_it_can_still_be_superseded() -> None:
    claims = _FakeClaims(served=(_claim(),))
    arm = _arms(claims=claims).observed_claims_arm(_ctx(), moment=_NOW)

    block = (await _envelope({BLOCK_OBSERVED_CLAIMS: arm})).block(BLOCK_OBSERVED_CLAIMS)

    assert block.items[0].trust is not None
    assert block.items[0].trust.mutability == "mutable"


async def test_the_claims_arm_clamps_an_over_large_bound_rather_than_passing_it_down() -> None:
    claims = _FakeClaims()
    arm = _arms(claims=claims).observed_claims_arm(_ctx(), moment=_NOW, limit=10_000)

    await _envelope({BLOCK_OBSERVED_CLAIMS: arm})

    assert claims.asked_limit == 100


async def test_no_claims_is_an_empty_block() -> None:
    """An entity nobody has recorded a claim about is a complete answer."""
    envelope = await _envelope({BLOCK_OBSERVED_CLAIMS: _arms().observed_claims_arm(_ctx(), moment=_NOW)})

    assert envelope.block(BLOCK_OBSERVED_CLAIMS).state == BLOCK_EMPTY


async def test_a_broken_claim_read_fails_its_block() -> None:
    claims = _FakeClaims(raises=_Boom("claim index is down"))
    arm = _arms(claims=claims).observed_claims_arm(_ctx(), moment=_NOW)

    envelope = await _envelope({BLOCK_OBSERVED_CLAIMS: arm})

    assert envelope.block(BLOCK_OBSERVED_CLAIMS).state == BLOCK_FAILED
    assert envelope.state == ENVELOPE_DEGRADED


# -- workspace -------------------------------------------------------------


def test_a_named_reference_wins_over_a_search_term() -> None:
    """A reference is the most specific thing a caller can supply. Falling back
    to lexical when both are present would answer a question nobody asked."""
    from contextplane.context.schemas.trust import ExternalReferenceV1

    recall = _FakeRecall()
    reference = ExternalReferenceV1(
        source_system="github",
        source_namespace="acme/platform",
        kind="commit",
        external_id="abc123",
        classification="internal",
        external_authority="github",
    )

    _arms(recall=recall).workspace_arm(_ctx(), term="payments", reference=reference, moment=_NOW)

    assert recall.chosen == "reference"


def test_a_search_term_alone_takes_the_lexical_arm() -> None:
    recall = _FakeRecall()

    _arms(recall=recall).workspace_arm(_ctx(), term="payments", moment=_NOW)

    assert recall.chosen == "lexical"


@pytest.mark.parametrize("term", [None, "", "   "])
def test_neither_a_reference_nor_a_usable_term_reads_every_authorized_checkpoint(term: str | None) -> None:
    """A blank term is not a search. Passing it to the lexical arm would match
    everything or nothing depending on how the SQL treats an empty needle."""
    recall = _FakeRecall()

    _arms(recall=recall).workspace_arm(_ctx(), term=term, moment=_NOW)

    assert recall.chosen is None


async def test_a_broken_workspace_read_fails_its_block() -> None:
    """The all-checkpoints path opens its own session, so a database that
    refuses one has to surface as a failed block rather than an empty one."""

    def _session_factory() -> Any:
        raise _Boom("no connection available")

    arm = _arms(session_factory=_session_factory).workspace_arm(_ctx(), moment=_NOW)

    envelope = await _envelope({BLOCK_WORKSPACE: arm})

    assert envelope.block(BLOCK_WORKSPACE).state == BLOCK_FAILED
    assert envelope.state == ENVELOPE_DEGRADED
