"""Translating a reported delivery outcome, and refusing the ones that cannot mean anything.

The adapter is a pure function, so everything it decides is testable without a
database — which matters here more than usual, because the failures it prevents
are all *silent* at the layer below. The envelope surface accepts an outcome
citing nothing and an outcome citing a misspelled kind; both store, both return
success, and both are then permanently unreachable from the receipt they belong
to. A test that only exercised the happy path would prove the translation works
and say nothing about the property the module exists for.

Every refusal below is driven from a fixture rather than an inline dict. The
fixtures are what a reader checks the contract against, and each refused one
carries its own note saying why a submission that looks entirely reasonable is
not one.
"""

from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from contextplane.context.lifecycle import UnknownLifecycleReferenceKind
from contextplane.context.schemas.reference import normalize_reference
from contextplane.signals.adapters.control_plane import (
    OUTCOME_CONCLUSIONS,
    OutcomeRejected,
    checked_references,
    control_plane_outcome_envelope,
    outcome_payload,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "lifecycle_outcomes"

#: The registered seat an outcome arrives under. A value the test supplies rather
#: than one the adapter owns: the module admits no source of its own, and a
#: constant here would quietly become one.
_SOURCE_ID = uuid.uuid4()
_SOURCE_SYSTEM = "github-actions"
_PRODUCER = "acme-ci-seat"


def _fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def _envelope_from(fixture: dict[str, Any], **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "source_id": _SOURCE_ID,
        "source_system": _SOURCE_SYSTEM,
        "producer_id": _PRODUCER,
        "outcome": fixture["outcome"],
        "references": tuple(normalize_reference(dict(ref)) for ref in fixture["references"]),
        "concluded_at": datetime.datetime.fromisoformat(fixture["concluded_at"]),
        "received_at": datetime.datetime.fromisoformat(fixture["received_at"]),
        "attempt": fixture["attempt"],
    }
    kwargs.update(overrides)
    return control_plane_outcome_envelope(**kwargs)


# -- the shapes that translate -------------------------------------------------


@pytest.mark.parametrize("name", ["workflow_run_success", "workflow_run_failure", "deployment_success"])
def test_every_accepted_fixture_translates(name: str) -> None:
    """The corpus is the contract, so each accepted shape has to actually pass.

    Parametrized over the files rather than asserted one at a time: a fixture
    added later is covered by this test the moment it lands, instead of sitting
    in the directory attesting to nothing.
    """
    envelope = _envelope_from(_fixture(name))

    assert envelope.source_system == _SOURCE_SYSTEM
    assert envelope.references, "an accepted outcome always cites the work it concluded"


def test_the_occurrence_id_is_namespaced_by_the_source_system_and_qualified_by_attempt() -> None:
    """Two properties in one id, and each prevents a different collision.

    The ledger's uniqueness is over tenant, producer and occurrence id with no
    source column, so a bare object id could collide across two sources sharing
    a producer convention. And a re-execution has to be a new occurrence rather
    than a mutation of the one something may already cite.
    """
    envelope = _envelope_from(_fixture("workflow_run_failure"))

    assert envelope.source_event_id == f"{_SOURCE_SYSTEM}:workflow_run:8891234502:2"


def test_the_stored_attempt_agrees_with_the_one_in_the_occurrence_id() -> None:
    """One attempt number, in both places it is recorded.

    The submitted body carries an attempt and so does the argument that builds
    the occurrence id. If those two could disagree, a stored row would describe
    one execution while being identified as another -- and the row would look
    entirely self-consistent to anyone reading only one of the two fields.
    """
    fixture = _fixture("workflow_run_success")
    fixture["outcome"]["attempt"] = 7

    envelope = _envelope_from(fixture, attempt=3)

    assert envelope.source_event_id.endswith(":3")
    assert envelope.payload is not None
    assert envelope.payload["attempt"] == 3, "the body must not claim an attempt the identity contradicts"


def test_a_rerun_is_a_second_occurrence_rather_than_an_overwrite() -> None:
    first = _envelope_from(_fixture("workflow_run_success"), attempt=1)
    second = _envelope_from(_fixture("workflow_run_success"), attempt=2)

    assert first.source_event_id != second.source_event_id


def test_the_two_times_are_kept_apart() -> None:
    """Lag between the work concluding and the submitter hearing about it is a
    measure, and collapsing the two instants is what destroys the evidence of it."""
    envelope = _envelope_from(_fixture("workflow_run_success"))

    assert envelope.event_time != envelope.observed_time
    assert envelope.observed_time > envelope.event_time


def test_times_are_normalized_to_utc_so_a_replay_digests_identically() -> None:
    """The one refusal that looks like tidiness and is not.

    The stored content digest is taken over the ISO rendering of these instants,
    so the same moment replayed from a queue holding a different offset would
    digest differently and be refused as a conflicting reuse of its own key --
    a redelivery answering with a conflict instead of converging.
    """
    fixture = _fixture("workflow_run_success")
    utc = datetime.datetime.fromisoformat(fixture["concluded_at"])
    elsewhere = utc.astimezone(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    assert utc == elsewhere, "the two spellings must be the same instant for this test to mean anything"

    from_utc = _envelope_from(fixture, concluded_at=utc)
    from_offset = _envelope_from(fixture, concluded_at=elsewhere)

    assert from_utc.event_time.isoformat() == from_offset.event_time.isoformat()


def test_the_payload_keeps_only_the_allowlist() -> None:
    """A field an orchestrator adds later must not arrive by default.

    The raw submission never reaches storage, which is what keeps logs and free
    text out of the ledger rather than relying on the admission floor to catch
    them after the fact.
    """
    payload = outcome_payload(
        {
            "object": "workflow_run",
            "object_id": "1",
            "conclusion": "success",
            "raw_log": "Bearer sk-not-a-real-secret",
            "reviewer_note": "free text nobody reviewed",
        }
    )

    assert set(payload) == {"object", "object_id", "conclusion"}


def test_the_object_and_conclusion_are_folded_rather_than_refused_on_case() -> None:
    payload = outcome_payload({"object": "Workflow_Run", "object_id": "1", "conclusion": "SUCCESS"})

    assert payload["object"] == "workflow_run"
    assert payload["conclusion"] == "success"


# -- the shapes that must be refused -------------------------------------------


def test_an_outcome_citing_no_work_is_refused_rather_than_stored_unjoinable() -> None:
    """The refusal the envelope surface cannot make.

    An outcome with no reference is accepted below this layer and stored, and it
    can never be reached from the receipt that preceded the change. The failure
    is invisible: it looks exactly like an outcome that has not been submitted.
    """
    fixture = _fixture("unjoinable_no_references")

    with pytest.raises(OutcomeRejected) as raised:
        _envelope_from(fixture)

    assert "unjoinable" in str(raised.value)


def test_an_outcome_citing_a_misspelled_kind_is_refused_rather_than_silently_unjoined() -> None:
    """The negative control, and the failure it prevents is the subtle one.

    `kind` is part of a reference's collision scope. A misspelling does not
    fail — it binds to a second reference row that nothing else will ever look
    at, so the outcome is stored, correct-looking, and permanently invisible to
    the receipt citing the correct spelling for the same external id.
    """
    fixture = _fixture("misspelled_kind")

    with pytest.raises(UnknownLifecycleReferenceKind):
        _envelope_from(fixture)


def test_a_requested_action_has_no_spelling_and_is_refused() -> None:
    """A triggered action is not a completed one.

    Asserted against the vocabulary as well as the refusal: if a value meaning
    "requested" were ever added, this submission would start being accepted and
    "the deployment was asked for" would become indistinguishable from "the
    deployment succeeded" everywhere downstream.
    """
    assert "requested" not in OUTCOME_CONCLUSIONS
    assert "queued" not in OUTCOME_CONCLUSIONS

    with pytest.raises(OutcomeRejected) as raised:
        _envelope_from(_fixture("requested_not_concluded"))

    assert "not an outcome" in str(raised.value)


@pytest.mark.parametrize("missing", ["object", "object_id", "conclusion"])
def test_an_outcome_missing_what_concluded_or_how_is_refused(missing: str) -> None:
    outcome = {"object": "workflow_run", "object_id": "1", "conclusion": "success"}
    del outcome[missing]

    with pytest.raises(OutcomeRejected):
        outcome_payload(outcome)


def test_an_unknown_object_is_refused() -> None:
    with pytest.raises(OutcomeRejected):
        outcome_payload({"object": "pipeline", "object_id": "1", "conclusion": "success"})


def test_a_naive_instant_is_refused_rather_than_assumed_local() -> None:
    """The ambiguity is exactly the offset nobody recorded.

    Storing it would put a guess into one of the recorded times, and every later
    comparison would treat that guess as exact.
    """
    fixture = _fixture("workflow_run_success")

    with pytest.raises(OutcomeRejected) as raised:
        _envelope_from(fixture, concluded_at=datetime.datetime(2026, 8, 9, 11, 52, 19))

    assert "no timezone" in str(raised.value)


def test_an_attempt_below_one_is_refused() -> None:
    with pytest.raises(OutcomeRejected):
        _envelope_from(_fixture("workflow_run_success"), attempt=0)


def test_an_empty_source_system_is_refused_because_it_namespaces_the_occurrence() -> None:
    with pytest.raises(OutcomeRejected):
        _envelope_from(_fixture("workflow_run_success"), source_system="   ")


# -- what the adapter must not do ----------------------------------------------


def test_the_envelope_carries_no_authority_ingestion_time_or_digest() -> None:
    """The three fields an adapter must never fill in.

    Asserted structurally rather than by reading the module: an adapter that
    could carry any of them would be one a caller could use to decide it, and
    the ledger derives all three from the registration and its own clock.
    """
    envelope = _envelope_from(_fixture("workflow_run_success"))

    for forbidden in ("authority", "ingested_at", "content_digest"):
        assert not hasattr(envelope, forbidden), f"an adapter must not be able to supply {forbidden}"


def test_checked_references_returns_them_unchanged_when_every_kind_is_legal() -> None:
    """Translation, not mutation. The adapter checks and passes through; a
    reference it rewrote would be one the submitter could not recognise."""
    references = tuple(normalize_reference(dict(ref)) for ref in _fixture("workflow_run_success")["references"])

    assert checked_references(references) == references
