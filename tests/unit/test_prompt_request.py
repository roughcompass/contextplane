"""What a saved prompt may be, and what it may not.

E22-T15. A prompt is stored once and resolved on every run afterwards, so a
shape this accepts and the resolver later refuses fails per prompt, per run,
forever — and the run reports it as a system failure rather than as the bad
prompt it is. Everything here is about refusing at the moment somebody saves.
"""

from __future__ import annotations

import uuid

import pytest

from contextplane.context.evaluation.prompt_request import MAX_ARM_LIMIT, PromptRequestV1
from contextplane.exceptions import ValidationError

_REFERENCE = {
    "classification": "internal",
    "external_authority": "github",
    "external_id": "acme/svc#41",
    "kind": "pull_request",
    "source_namespace": "acme",
    "source_system": "github",
}


def test_a_query_is_the_only_thing_a_prompt_must_have() -> None:
    prompt = PromptRequestV1.of({"query": "the state of the migration"})

    assert prompt.query == "the state of the migration"
    assert prompt.limit == 25
    assert prompt.resolver_arguments()["query"] == "the state of the migration"


@pytest.mark.parametrize("raw", [{}, {"query": ""}, {"query": "   "}, {"query": None}])
def test_a_prompt_with_no_query_is_refused(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="needs a query"):
        PromptRequestV1.of(raw)


def test_an_unknown_field_is_refused_rather_than_dropped() -> None:
    """A caller who misspelled `subject_entity_id` and had it silently dropped
    would get a resolution that ran against a different question than the one
    they saved, on every run, without anything saying so."""
    with pytest.raises(ValidationError, match="subject_entitiy_id"):
        PromptRequestV1.of({"query": "x", "subject_entitiy_id": str(uuid.uuid4())})


@pytest.mark.parametrize("limit", [0, -1, MAX_ARM_LIMIT + 1, "25", 25.0, True])
def test_a_limit_outside_the_contract_is_refused(limit: object) -> None:
    with pytest.raises(ValidationError, match="limit is a whole number"):
        PromptRequestV1.of({"limit": limit, "query": "x"})


@pytest.mark.parametrize("max_age", [0, -1, "60"])
def test_a_nonsense_max_age_is_refused(max_age: object) -> None:
    with pytest.raises(ValidationError, match="max_age_s"):
        PromptRequestV1.of({"max_age_s": max_age, "query": "x"})


@pytest.mark.parametrize("digest", ["abc", "sha256:" + "F" * 64, "sha256:" + "a" * 63])
def test_a_malformed_instruction_digest_is_refused(digest: str) -> None:
    """The same spelling the channel accepts. A prompt carrying a digest that can
    never match a submission would resolve as `declared_unknown` forever, which
    reads as an integration problem rather than as a typo in a saved prompt."""
    with pytest.raises(ValidationError, match="instruction_digest"):
        PromptRequestV1.of({"instruction_digest": digest, "query": "x"})


def test_a_wellformed_instruction_digest_survives_the_round_trip() -> None:
    digest = "sha256:" + "a" * 64

    prompt = PromptRequestV1.of({"instruction_digest": digest, "query": "x"})

    assert prompt.stored()["instruction_digest"] == digest
    assert prompt.resolver_arguments()["instruction_digest"] == digest


def test_a_lifecycle_kind_outside_the_closed_vocabulary_is_refused() -> None:
    """Through the same normalizer the resolver's other callers use. A second
    copy of the vocabulary is a second vocabulary, and two spellings that store
    cleanly and then fail to join is what closing it prevents."""
    with pytest.raises(Exception, match="kind"):
        PromptRequestV1.of({"lifecycle_references": [{**_REFERENCE, "kind": "not_a_stage"}], "query": "x"})


def test_a_reference_missing_a_required_part_says_which() -> None:
    incomplete = {key: value for key, value in _REFERENCE.items() if key != "external_id"}

    with pytest.raises(ValidationError, match="external_id"):
        PromptRequestV1.of({"query": "x", "workspace_reference": incomplete})


def test_the_stored_form_omits_absences_rather_than_writing_nulls() -> None:
    """An omitted optional coming back as an explicit null would be a second
    spelling of one prompt, and two prompts differing only in how their absences
    are written would compare unequal."""
    stored = PromptRequestV1.of({"query": "x"}).stored()

    assert stored == {"limit": 25, "query": "x"}


def test_a_full_prompt_round_trips_through_storage_unchanged() -> None:
    """The property a run depends on: what was saved is what is resolved. A field
    lost in storage would change the question on every run after the first."""
    raw = {
        "arc_receipt_id": str(uuid.uuid4()),
        "instruction_digest": "sha256:" + "b" * 64,
        "intent_ids": [str(uuid.uuid4())],
        "lifecycle_references": [{**_REFERENCE, "kind": "stage"}],
        "limit": 40,
        "max_age_s": 90.0,
        "query": "the state of the migration",
        "subject_entity_id": str(uuid.uuid4()),
        "workspace_reference": _REFERENCE,
        "workspace_term": "migration",
    }

    once = PromptRequestV1.of(raw)
    twice = PromptRequestV1.of(once.stored())

    assert twice == once
    assert twice.stored() == once.stored()


def test_resolver_arguments_name_the_resolver_s_own_parameters() -> None:
    """Checked against the signature rather than against a list, so a resolver
    that gains a parameter fails here instead of a run silently never passing
    it."""
    import inspect

    from contextplane.context.resolve import ContextResolver

    parameters = set(inspect.signature(ContextResolver.resolve).parameters)
    supplied = set(PromptRequestV1.of({"query": "x"}).resolver_arguments())

    assert supplied <= parameters
    # `moment` is the caller's, and `arc` is a per-request authorization context
    # rather than something a saved prompt can carry.
    assert parameters - supplied - {"self", "ctx", "moment", "arc"} == set()
