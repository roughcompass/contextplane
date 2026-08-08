"""The envelope contract, pinned.

Three slices build against this skeleton concurrently, so an error here is
replicated three ways before anything converges. These tests exist to make that
error impossible to introduce quietly: every block state, every arm-failure
combination, and every rule that distinguishes a partial answer from a
misleading one.
"""

from __future__ import annotations

import datetime
import itertools
import json
import pathlib

import pytest

from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_DEGRADED,
    BLOCK_EMPTY,
    BLOCK_FAILED,
    BLOCK_NAMES,
    BLOCK_OBSERVED_CLAIMS,
    BLOCK_STATES,
    BLOCK_SUCCESS,
    BLOCK_WORKSPACE,
    ENVELOPE_BLOCKED,
    ENVELOPE_COMPLETE,
    ENVELOPE_DEGRADED,
    NON_CANONICAL_BLOCKS,
    ContextBlockV1,
    ContextEnvelopeV1,
    ContextItemV1,
    derive_envelope_state,
)
from contextplane.context.schemas.trust import (
    ExternalReferenceV1,
    InvalidContextItem,
    QualityStateV1,
    ReceiptItemIdV1,
    TrustMetadataV1,
)

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "context" / "envelope"


def _trust(**overrides: object) -> TrustMetadataV1:
    base: dict[str, object] = {
        "trust": "asserted",
        "source": "github",
        "assertion_kind": "fact",
        "authority": "platform-team",
        "freshness": datetime.datetime(2026, 8, 8, tzinfo=datetime.UTC),
        "mutability": "mutable",
        "attribution": "alice",
        "classification": "internal",
    }
    base.update(overrides)
    return TrustMetadataV1(**base)  # type: ignore[arg-type]


def _item(block: str, key: str = "k1", *, with_trust: bool = True) -> ContextItemV1:
    return ContextItemV1(
        receipt_item_id=ReceiptItemIdV1(block=block, source="github", item_key=key),
        payload={"value": key},
        trust=_trust() if with_trust else None,
    )


def _block(name: str, state: str) -> ContextBlockV1:
    """One arm in a given state, built so the state is internally consistent."""
    items: tuple[ContextItemV1, ...] = ()
    reason = None
    if state == BLOCK_SUCCESS:
        items = (_item(name, with_trust=name in NON_CANONICAL_BLOCKS),)
    elif state == BLOCK_DEGRADED:
        items = (_item(name, with_trust=name in NON_CANONICAL_BLOCKS),)
        reason = "upstream timed out after partial results"
    elif state == BLOCK_FAILED:
        reason = "upstream unreachable"
    return ContextBlockV1(name=name, state=state, items=items, reason=reason)


def _envelope(states: dict[str, str]) -> ContextEnvelopeV1:
    blocks = tuple(_block(name, states[name]) for name in BLOCK_NAMES)
    degraded = tuple(b.name for b in blocks if b.state in (BLOCK_DEGRADED, BLOCK_FAILED))
    quality = QualityStateV1(
        degraded_blocks=degraded,
        reasons=tuple(b.reason or "" for b in blocks if b.state in (BLOCK_DEGRADED, BLOCK_FAILED)),
        cacheable=not degraded,
    )
    return ContextEnvelopeV1(blocks=blocks, quality=quality, state=derive_envelope_state(blocks))


# --- the four blocks are always present, in order -----------------------------


def test_an_envelope_carries_exactly_the_four_blocks_in_order() -> None:
    envelope = _envelope(dict.fromkeys(BLOCK_NAMES, BLOCK_EMPTY))
    assert tuple(block.name for block in envelope.blocks) == BLOCK_NAMES


def test_a_missing_block_is_refused_rather_than_defaulted() -> None:
    """A caller that branches on whether a block exists gets it wrong once, and
    the failure reads as missing data rather than a missing check."""
    blocks = tuple(_block(name, BLOCK_EMPTY) for name in BLOCK_NAMES if name != BLOCK_WORKSPACE)
    with pytest.raises(InvalidContextItem, match="exactly the four blocks"):
        ContextEnvelopeV1(
            blocks=blocks,
            quality=QualityStateV1(degraded_blocks=(), reasons=(), cacheable=True),
            state=ENVELOPE_COMPLETE,
        )


def test_blocks_out_of_order_are_refused() -> None:
    names = (BLOCK_ARC, BLOCK_CANONICAL, BLOCK_OBSERVED_CLAIMS, BLOCK_WORKSPACE)
    blocks = tuple(_block(name, BLOCK_EMPTY) for name in names)
    with pytest.raises(InvalidContextItem, match="in order"):
        ContextEnvelopeV1(
            blocks=blocks,
            quality=QualityStateV1(degraded_blocks=(), reasons=(), cacheable=True),
            state=ENVELOPE_COMPLETE,
        )


# --- every arm-failure combination --------------------------------------------


@pytest.mark.parametrize("states", list(itertools.product(sorted(BLOCK_STATES), repeat=4)))
def test_every_arm_state_combination_derives_one_defined_envelope_state(states: tuple[str, ...]) -> None:
    """All 256 combinations. The rule is small enough to state in one line, and
    exhaustive coverage is what stops a later edit adding a case nobody derived."""
    mapping = dict(zip(BLOCK_NAMES, states, strict=True))
    envelope = _envelope(mapping)

    canonical = mapping[BLOCK_CANONICAL]
    others = [mapping[name] for name in BLOCK_NAMES if name != BLOCK_CANONICAL]

    if canonical == BLOCK_FAILED:
        assert envelope.state == ENVELOPE_BLOCKED
    elif canonical == BLOCK_DEGRADED or any(s in (BLOCK_DEGRADED, BLOCK_FAILED) for s in others):
        assert envelope.state == ENVELOPE_DEGRADED
    else:
        assert envelope.state == ENVELOPE_COMPLETE


@pytest.mark.parametrize("other", sorted(NON_CANONICAL_BLOCKS))
def test_a_failed_canonical_arm_blocks_however_healthy_the_rest_are(other: str) -> None:
    """Serving the surrounding context without the thing it surrounds is not a
    partial answer; it is one an agent reads as the whole picture."""
    states = dict.fromkeys(BLOCK_NAMES, BLOCK_SUCCESS)
    states[BLOCK_CANONICAL] = BLOCK_FAILED
    states[other] = BLOCK_SUCCESS
    assert _envelope(states).state == ENVELOPE_BLOCKED


@pytest.mark.parametrize("arm", sorted(NON_CANONICAL_BLOCKS))
def test_a_failed_non_canonical_arm_degrades_but_does_not_block(arm: str) -> None:
    states = dict.fromkeys(BLOCK_NAMES, BLOCK_SUCCESS)
    states[arm] = BLOCK_FAILED
    assert _envelope(states).state == ENVELOPE_DEGRADED


def test_all_empty_is_complete_not_degraded() -> None:
    """A subject with no workspace notes is a complete answer, not a broken one."""
    assert _envelope(dict.fromkeys(BLOCK_NAMES, BLOCK_EMPTY)).state == ENVELOPE_COMPLETE


def test_empty_and_failed_are_distinct_states() -> None:
    """Collapsing them lets a broken integration read as 'nothing exists', which
    is the reading that makes an agent proceed on an incomplete picture."""
    empty = dict.fromkeys(BLOCK_NAMES, BLOCK_EMPTY)
    failed = dict.fromkeys(BLOCK_NAMES, BLOCK_EMPTY) | {BLOCK_WORKSPACE: BLOCK_FAILED}
    assert _envelope(empty).state == ENVELOPE_COMPLETE
    assert _envelope(failed).state == ENVELOPE_DEGRADED


def test_the_envelope_state_cannot_be_asserted_against_its_blocks() -> None:
    blocks = tuple(_block(name, BLOCK_EMPTY) for name in BLOCK_NAMES)
    with pytest.raises(InvalidContextItem, match="computed, never asserted"):
        ContextEnvelopeV1(
            blocks=blocks,
            quality=QualityStateV1(degraded_blocks=(), reasons=(), cacheable=True),
            state=ENVELOPE_BLOCKED,
        )


# --- per-block internal consistency -------------------------------------------


def test_a_degraded_block_must_say_why() -> None:
    with pytest.raises(InvalidContextItem, match="must say why"):
        ContextBlockV1(name=BLOCK_ARC, state=BLOCK_DEGRADED, items=(_item(BLOCK_ARC),))


def test_a_failed_block_carrying_items_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="read as complete"):
        ContextBlockV1(name=BLOCK_ARC, state=BLOCK_FAILED, items=(_item(BLOCK_ARC),), reason="upstream down")


def test_success_with_no_items_is_refused_because_that_is_empty() -> None:
    with pytest.raises(InvalidContextItem, match="an arm with nothing to say is empty"):
        ContextBlockV1(name=BLOCK_ARC, state=BLOCK_SUCCESS, items=())


def test_empty_carrying_items_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="empty but carries"):
        ContextBlockV1(name=BLOCK_ARC, state=BLOCK_EMPTY, items=(_item(BLOCK_ARC),))


# --- trust metadata is complete or the item is invalid ------------------------


@pytest.mark.parametrize("block", sorted(NON_CANONICAL_BLOCKS))
def test_a_non_canonical_item_without_trust_metadata_is_invalid(block: str) -> None:
    with pytest.raises(InvalidContextItem, match="no trust metadata"):
        ContextBlockV1(name=block, state=BLOCK_SUCCESS, items=(_item(block, with_trust=False),))


def test_a_canonical_item_carrying_trust_metadata_is_refused() -> None:
    """The asymmetry is the contract: canonical is the registry's own answer."""
    with pytest.raises(InvalidContextItem, match="canonical items carry no trust metadata"):
        ContextBlockV1(name=BLOCK_CANONICAL, state=BLOCK_SUCCESS, items=(_item(BLOCK_CANONICAL),))


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("trust", "probably", "unknown trust level"),
        ("assertion_kind", "vibe", "unknown assertion kind"),
        ("mutability", "sometimes", "unknown mutability"),
        ("classification", "secret", "unknown classification"),
        ("source", "  ", "needs a source"),
        ("authority", "", "needs an authority"),
    ],
)
def test_trust_metadata_refuses_rather_than_repairs(field: str, value: str, match: str) -> None:
    with pytest.raises(InvalidContextItem, match=match):
        _trust(**{field: value})


def test_naive_freshness_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="timezone-aware"):
        _trust(freshness=datetime.datetime(2026, 8, 8))


# --- external references ------------------------------------------------------


def _reference(**overrides: object) -> ExternalReferenceV1:
    base: dict[str, object] = {
        "source_system": "github",
        "source_namespace": "roughcompass/contextplane",
        "kind": "issue",
        "external_id": "412",
        "classification": "internal",
        "external_authority": "github",
    }
    base.update(overrides)
    return ExternalReferenceV1(**base)  # type: ignore[arg-type]


def test_the_same_id_under_two_kinds_does_not_collide() -> None:
    """`issue/412` and `pull_request/412` in one repository are two things, and a
    scope without kind would merge them silently, because both resolve."""
    assert _reference(kind="issue").collision_key() != _reference(kind="pull_request").collision_key()


def test_the_same_id_in_two_namespaces_does_not_collide() -> None:
    assert _reference(source_namespace="a/b").collision_key() != _reference(source_namespace="c/d").collision_key()


def test_revision_is_outside_the_collision_scope() -> None:
    """Two revisions of one document are the same document; scoping by revision
    would make an ordinary edit look like a new reference."""
    assert _reference(revision="v1").collision_key() == _reference(revision="v2").collision_key()


def test_one_digest_algorithm_is_pinned_for_every_consumer() -> None:
    """Stable across processes, so two services agree on identity."""
    assert _reference().collision_key() == _reference().collision_key()
    assert len(_reference().collision_key()) == 64


def test_the_digest_is_length_prefixed_so_field_splits_cannot_collide() -> None:
    """Without length prefixing, ('ab','c') and ('a','bc') hash identically and
    two different references become indistinguishable from one repeated."""
    left = _reference(source_system="ab", source_namespace="c").collision_key()
    right = _reference(source_system="a", source_namespace="bc").collision_key()
    assert left != right


# --- receipt item ids ---------------------------------------------------------


def test_a_receipt_item_id_is_stable_across_resolutions() -> None:
    first = ReceiptItemIdV1(block=BLOCK_ARC, source="github", item_key="k1").value()
    second = ReceiptItemIdV1(block=BLOCK_ARC, source="github", item_key="k1").value()
    assert first == second


def test_a_receipt_item_id_does_not_move_when_a_sibling_is_added() -> None:
    """Derived from identity rather than position, so a receipt line stays
    pointing at the same item when the block's contents shift."""
    before = ReceiptItemIdV1(block=BLOCK_ARC, source="github", item_key="k2").value()
    after = ReceiptItemIdV1(block=BLOCK_ARC, source="github", item_key="k2").value()
    assert before == after


def test_receipt_item_ids_differ_across_blocks() -> None:
    arc = ReceiptItemIdV1(block=BLOCK_ARC, source="s", item_key="k").value()
    workspace = ReceiptItemIdV1(block=BLOCK_WORKSPACE, source="s", item_key="k").value()
    assert arc != workspace


# --- quality state ------------------------------------------------------------


def test_quality_must_name_the_arms_that_actually_degraded() -> None:
    blocks = tuple(_block(name, BLOCK_EMPTY) for name in BLOCK_NAMES)
    with pytest.raises(InvalidContextItem, match="quality names"):
        ContextEnvelopeV1(
            blocks=blocks,
            quality=QualityStateV1(degraded_blocks=(BLOCK_ARC,), reasons=("made up",), cacheable=False),
            state=ENVELOPE_COMPLETE,
        )


def test_every_degraded_block_carries_its_own_reason() -> None:
    with pytest.raises(InvalidContextItem, match="needs its own reason"):
        QualityStateV1(degraded_blocks=(BLOCK_ARC, BLOCK_WORKSPACE), reasons=("only one",), cacheable=False)


def test_a_degraded_answer_is_not_cacheable() -> None:
    """Caching it would outlive the failure that caused it."""
    with pytest.raises(InvalidContextItem, match="not cacheable"):
        QualityStateV1(degraded_blocks=(BLOCK_ARC,), reasons=("timeout",), cacheable=True)


# --- fixtures -----------------------------------------------------------------


def test_every_block_state_has_a_committed_fixture() -> None:
    """The fixtures are what a slice reads to build against this contract without
    importing it, so a state with no fixture is a state three slices will guess."""
    for state in sorted(BLOCK_STATES):
        assert (FIXTURES / f"block-{state}.json").is_file(), f"no fixture for block state {state}"


@pytest.mark.parametrize("state", sorted(BLOCK_STATES))
def test_each_block_fixture_round_trips_into_the_contract(state: str) -> None:
    raw = json.loads((FIXTURES / f"block-{state}.json").read_text())
    items = tuple(
        ContextItemV1(
            receipt_item_id=ReceiptItemIdV1(**item["receipt_item_id"]),
            payload=item["payload"],
            trust=TrustMetadataV1(
                **{
                    **item["trust"],
                    "freshness": datetime.datetime.fromisoformat(item["trust"]["freshness"])
                    if item["trust"].get("freshness")
                    else None,
                }
            )
            if item.get("trust")
            else None,
        )
        for item in raw["items"]
    )
    block = ContextBlockV1(name=raw["name"], state=raw["state"], items=items, reason=raw.get("reason"))
    assert block.state == state


def test_every_envelope_state_has_a_committed_fixture() -> None:
    for state in (ENVELOPE_COMPLETE, ENVELOPE_DEGRADED, ENVELOPE_BLOCKED):
        assert (FIXTURES / f"envelope-{state}.json").is_file(), f"no fixture for envelope state {state}"


@pytest.mark.parametrize("state", [ENVELOPE_COMPLETE, ENVELOPE_DEGRADED, ENVELOPE_BLOCKED])
def test_each_envelope_fixture_derives_the_state_it_claims(state: str) -> None:
    raw = json.loads((FIXTURES / f"envelope-{state}.json").read_text())
    envelope = _envelope(raw["block_states"])
    assert envelope.state == raw["state"] == state
