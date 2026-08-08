"""The per-field refusal rules on the context contracts.

The conformance suite pins the envelope *contract* — four blocks, arm-failure
combinations, fixtures. This covers the layer underneath it: what each value
object refuses on its own, one rule per test, with no envelope assembled.

The split is the pyramid rather than duplication. These are pure-Python
validation rules with no database and no assembly, so a failure here names the
field that broke; the same failure surfacing only through a conformance test
would name the envelope and leave the field to be found.
"""

from __future__ import annotations

import datetime

import pytest

from contextplane.context.schemas.envelope import (
    BLOCK_ARC,
    BLOCK_CANONICAL,
    BLOCK_EMPTY,
    BLOCK_FAILED,
    BLOCK_NAMES,
    BLOCK_SUCCESS,
    BLOCK_WORKSPACE,
    ENVELOPE_COMPLETE,
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

_WHEN = datetime.datetime(2026, 8, 8, tzinfo=datetime.UTC)


def _trust(**overrides: object) -> TrustMetadataV1:
    base: dict[str, object] = {
        "trust": "observed",
        "source": "session-events",
        "assertion_kind": "measurement",
        "authority": "extraction-worker",
        "freshness": _WHEN,
        "mutability": "mutable",
        "attribution": None,
        "classification": "internal",
    }
    base.update(overrides)
    return TrustMetadataV1(**base)  # type: ignore[arg-type]


# --- trust metadata -----------------------------------------------------------


def test_a_complete_trust_record_is_accepted() -> None:
    record = _trust()
    assert record.trust == "observed"
    assert record.attribution is None


def test_attribution_may_be_absent_because_some_items_have_no_actor() -> None:
    """A machine-derived item has nothing to attribute, and inventing an actor
    would be worse than saying there isn't one."""
    assert _trust(attribution=None).attribution is None


def test_freshness_may_be_absent_but_absence_is_not_now() -> None:
    assert _trust(freshness=None).freshness is None


@pytest.mark.parametrize("level", ["attested", "asserted", "observed", "derived"])
def test_every_declared_trust_level_is_accepted(level: str) -> None:
    assert _trust(trust=level).trust == level


@pytest.mark.parametrize("kind", ["fact", "measurement", "intent", "policy", "annotation"])
def test_every_declared_assertion_kind_is_accepted(kind: str) -> None:
    """A measurement and an intent are both true in different senses, and an
    agent that cannot tell them apart plans against a wish."""
    assert _trust(assertion_kind=kind).assertion_kind == kind


@pytest.mark.parametrize("classification", ["public", "internal", "confidential", "restricted"])
def test_every_declared_classification_is_accepted(classification: str) -> None:
    assert _trust(classification=classification).classification == classification


@pytest.mark.parametrize("mutability", ["immutable", "mutable", "unknown"])
def test_every_declared_mutability_is_accepted(mutability: str) -> None:
    assert _trust(mutability=mutability).mutability == mutability


def test_a_whitespace_source_is_refused_because_it_passes_a_none_check() -> None:
    with pytest.raises(InvalidContextItem, match="needs a source"):
        _trust(source="   ")


def test_source_and_authority_are_separate_because_relaying_is_not_endorsing() -> None:
    record = _trust(source="acme-gateway", authority="platform-team")
    assert record.source != record.authority


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


@pytest.mark.parametrize("field", ["source_system", "source_namespace", "kind", "external_id"])
def test_every_collision_scope_field_is_required(field: str) -> None:
    with pytest.raises(InvalidContextItem, match="collision scope"):
        _reference(**{field: " "})


def test_a_reference_carries_optional_revision_and_uri_without_them_being_identity() -> None:
    reference = _reference(revision="v3", authorized_uri="https://example.invalid/x", observed_at=_WHEN)
    assert reference.revision == "v3"
    assert reference.collision_key() == _reference().collision_key()


def test_a_naive_observed_at_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="timezone-aware"):
        _reference(observed_at=datetime.datetime(2026, 8, 8))


def test_an_unknown_classification_on_a_reference_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="unknown classification"):
        _reference(classification="top-secret")


def test_the_external_authority_is_the_other_systems_not_ours() -> None:
    assert _reference(external_authority="gitlab").external_authority == "gitlab"


# --- receipt item ids ---------------------------------------------------------


@pytest.mark.parametrize("field", ["block", "source", "item_key"])
def test_a_receipt_item_id_requires_every_part(field: str) -> None:
    parts = {"block": BLOCK_ARC, "source": "github", "item_key": "k"}
    parts[field] = "  "
    with pytest.raises(InvalidContextItem, match="receipt item id needs"):
        ReceiptItemIdV1(**parts)


def test_two_sources_in_one_block_produce_different_ids() -> None:
    left = ReceiptItemIdV1(block=BLOCK_ARC, source="github", item_key="k").value()
    right = ReceiptItemIdV1(block=BLOCK_ARC, source="gitlab", item_key="k").value()
    assert left != right


# --- quality state ------------------------------------------------------------


def test_a_clean_answer_is_cacheable() -> None:
    assert QualityStateV1(degraded_blocks=(), reasons=(), cacheable=True).cacheable is True


def test_a_clean_answer_may_also_be_declared_uncacheable() -> None:
    """Nothing forces caching; the rule is one-directional."""
    assert QualityStateV1(degraded_blocks=(), reasons=(), cacheable=False).cacheable is False


# --- blocks and items ---------------------------------------------------------


def test_a_canonical_item_needs_no_trust_metadata() -> None:
    item = ContextItemV1(
        receipt_item_id=ReceiptItemIdV1(block=BLOCK_CANONICAL, source="registry", item_key="e1"),
        payload={"name": "checkout"},
    )
    block = ContextBlockV1(name=BLOCK_CANONICAL, state=BLOCK_SUCCESS, items=(item,))
    assert block.items[0].trust is None


def test_an_unknown_block_name_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="unknown block"):
        ContextBlockV1(name="sidecar", state=BLOCK_EMPTY)


def test_an_unknown_block_state_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="unknown block state"):
        ContextBlockV1(name=BLOCK_ARC, state="partial")


def test_a_block_accessor_raises_for_a_block_that_does_not_exist() -> None:
    blocks = tuple(ContextBlockV1(name=name, state=BLOCK_EMPTY) for name in BLOCK_NAMES)
    envelope = ContextEnvelopeV1(
        blocks=blocks,
        quality=QualityStateV1(degraded_blocks=(), reasons=(), cacheable=True),
        state=derive_envelope_state(blocks),
    )
    assert envelope.block(BLOCK_WORKSPACE).name == BLOCK_WORKSPACE
    with pytest.raises(InvalidContextItem, match="unknown block"):
        envelope.block("sidecar")


def test_an_all_empty_envelope_is_complete() -> None:
    blocks = tuple(ContextBlockV1(name=name, state=BLOCK_EMPTY) for name in BLOCK_NAMES)
    assert derive_envelope_state(blocks) == ENVELOPE_COMPLETE


def test_a_failed_block_states_its_reason() -> None:
    block = ContextBlockV1(name=BLOCK_ARC, state=BLOCK_FAILED, reason="upstream unreachable")
    assert block.reason == "upstream unreachable"
    assert block.items == ()
