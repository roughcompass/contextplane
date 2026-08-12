"""Per-field refusal rules for the task-memory contract.

The conformance gate pins the contract against checked-in fixtures — the shapes
a caller actually sends. This file covers the other half: each individual reason
a grant or a checkpoint refuses to exist, one test per rule, so a later change
that relaxes one of them fails on the rule it relaxed rather than on whichever
fixture happened to exercise it.

These are the refusals, not the happy path. Every one of them is a case where
accepting the value would store something that looks correct afterwards and
is not.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.context.schemas.trust import ExternalReferenceV1, InvalidContextItem
from contextplane.workspaces.schemas.intent_memory import (
    IntentParticipantGrantV1,
    checkpoint_from_client_payload,
)

_TASK_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_CHECKPOINT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_NOW = datetime.datetime(2026, 8, 8, tzinfo=datetime.UTC)


def _grant(**overrides: object) -> IntentParticipantGrantV1:
    fields: dict[str, object] = {
        "intent_id": _TASK_ID,
        "actor_id": "actor:alice",
        "role": "reader",
        "granted_by": "actor:bob",
        "granted_at": _NOW,
        "expires_at": None,
        "resolver_version": "participant-resolver-v1",
    }
    fields.update(overrides)
    return IntentParticipantGrantV1(**fields)  # type: ignore[arg-type]


def _checkpoint(**overrides: object):  # type: ignore[no-untyped-def]
    fields: dict[str, object] = {
        "checkpoint_id": _CHECKPOINT_ID,
        "intent_id": _TASK_ID,
        "sequence": 1,
        "predecessor_id": None,
        "author": "svc:api",
        "recorded_at": _NOW,
        "retention_policy": "standard-90d",
    }
    payload = overrides.pop("payload", {"goal": "ship it"})
    fields.update(overrides)
    return checkpoint_from_client_payload(payload, **fields)  # type: ignore[arg-type]


def _reference(external_id: str = "412") -> ExternalReferenceV1:
    return ExternalReferenceV1(
        source_system="github",
        source_namespace="roughcompass/contextplane",
        kind="pull_request",
        external_id=external_id,
        classification="internal",
        external_authority="platform-team",
    )


# --- grant refusals -----------------------------------------------------------


@pytest.mark.parametrize("field", ["actor_id", "granted_by", "resolver_version"])
def test_a_blank_grant_field_is_refused(field: str) -> None:
    """An empty string passes an `is not None` check and identifies nobody. A
    grant naming an unresolvable actor confers access to whoever later claims
    that name."""
    with pytest.raises(InvalidContextItem, match=f"needs a {field}"):
        _grant(**{field: "   "})


def test_a_naive_granted_at_is_refused() -> None:
    """Rendered as local time by whoever reads it, so the same grant window
    means different things in two deployments."""
    with pytest.raises(InvalidContextItem, match="granted_at must be timezone-aware"):
        _grant(granted_at=datetime.datetime(2026, 8, 8))


def test_a_naive_expires_at_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="expires_at must be timezone-aware"):
        _grant(expires_at=datetime.datetime(2026, 9, 8))


def test_an_expiry_equal_to_the_grant_time_is_refused() -> None:
    """It conferred nothing for any instant, which is not the same as a grant
    that has since lapsed — and stored, the two are indistinguishable."""
    with pytest.raises(InvalidContextItem, match="never applied"):
        _grant(expires_at=_NOW)


def test_a_grant_is_inactive_before_it_was_granted() -> None:
    """Backdating a read against a grant that did not yet exist is how an
    audit reconstructs an audience the task never had at that moment."""
    assert not _grant().is_active_at(_NOW - datetime.timedelta(seconds=1))
    assert _grant().is_active_at(_NOW)


# --- checkpoint refusals ------------------------------------------------------


def test_a_sequence_below_one_is_refused() -> None:
    """Positions are 1-based because the chain is walked backwards from the
    head; a zeroth checkpoint has no meaning to resume."""
    with pytest.raises(InvalidContextItem, match="sequence starts at 1"):
        _checkpoint(sequence=0)


@pytest.mark.parametrize("field", ["goal", "author", "retention_policy"])
def test_a_blank_required_checkpoint_field_is_refused(field: str) -> None:
    """A checkpoint with no goal cannot be resumed against, one with no author
    cannot be attributed, and one with no retention policy has no rule saying
    when it may be deleted."""
    overrides: dict[str, object] = {"payload": {"goal": "ship it"}}
    if field == "goal":
        overrides["payload"] = {"goal": "  "}
    else:
        overrides[field] = "  "
    with pytest.raises(InvalidContextItem, match=f"needs a {field}"):
        _checkpoint(**overrides)


def test_an_empty_next_action_is_refused() -> None:
    """Absent means finished; an empty string reads as finished to a renderer
    and as unset to a reader, and nothing tells them apart."""
    with pytest.raises(InvalidContextItem, match="next_action is absent or says something"):
        _checkpoint(payload={"goal": "ship it", "next_action": "   "})


def test_a_naive_recorded_at_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="recorded_at must be timezone-aware"):
        _checkpoint(recorded_at=datetime.datetime(2026, 8, 8))


def test_two_references_to_one_external_thing_are_refused() -> None:
    """They read as two independent sources supporting one claim."""
    reference = _reference()
    with pytest.raises(InvalidContextItem, match="normalized"):
        _checkpoint(evidence=(reference, reference))


def test_two_references_to_different_things_are_kept() -> None:
    """The normalization rule is per external thing, not a cap on evidence."""
    checkpoint = _checkpoint(evidence=(_reference("412"), _reference("413")))
    assert len(checkpoint.evidence) == 2


# --- what a client may send ---------------------------------------------------


def test_a_bare_string_where_a_list_belongs_is_refused() -> None:
    """A string is iterable, so accepting it would record one decision per
    character and look like a very thorough checkpoint."""
    with pytest.raises(InvalidContextItem, match="a bare string would be read one character at a time"):
        _checkpoint(payload={"goal": "ship it", "decisions": "use X"})


def test_a_non_string_entry_in_a_list_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="entries are strings"):
        _checkpoint(payload={"goal": "ship it", "decisions": ["fine", 7]})


def test_a_non_string_goal_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="goal is a string"):
        _checkpoint(payload={"goal": 7})


def test_a_non_string_next_action_is_refused() -> None:
    with pytest.raises(InvalidContextItem, match="next_action is a string or absent"):
        _checkpoint(payload={"goal": "ship it", "next_action": 7})


def test_the_refusal_names_every_offending_field_at_once() -> None:
    """One field at a time would mean a caller fixes, resubmits, and is refused
    again — and each round trip carries the same rejected payload."""
    with pytest.raises(InvalidContextItem) as exc:
        _checkpoint(payload={"goal": "ship it", "confidence": 0.9, "reviewer": "alice"})
    assert "confidence" in str(exc.value)
    assert "reviewer" in str(exc.value)
