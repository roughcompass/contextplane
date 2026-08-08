"""Conformance gate for the task-memory contract.

Task memory is read by an agent resuming work it did not start, so the two
shapes here decide who may read a task and whether what they read is
attributable. This gate pins both against checked-in fixtures before any table,
service or route exists, so the later slices in this family cannot quietly
widen a field they inherit.

Three properties are asserted that the dataclasses cannot self-certify:

- a participant grant refuses to be self-granted, which is the only spoof
  available to somebody who can already write grants;
- a checkpoint payload cannot carry any server-derived field, and the refusal
  names which one rather than silently overriding it;
- the canonical digest is verified on construction, so a checkpoint that
  misnames its own content cannot enter the predecessor chain.

Every negative case is a checked-in fixture rather than an inline literal on
purpose: the migration and service tasks in this family load the same files, and
a refusal that exists only in this test's source is one the next task
re-invents.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import uuid
from typing import Any

import pytest

from contextplane.context.schemas.trust import ExternalReferenceV1, InvalidContextItem
from contextplane.workspaces.schemas.task_memory import (
    CLIENT_FIELDS,
    PARTICIPANT_ROLES,
    SERVER_DERIVED_FIELDS,
    TaskCheckpointV1,
    TaskParticipantGrantV1,
    checkpoint_digest,
    checkpoint_from_client_payload,
)

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "context" / "task_memory"

_TASK_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_CHECKPOINT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_PREDECESSOR_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_NOW = datetime.datetime(2026, 8, 8, tzinfo=datetime.UTC)


def _fixture(name: str) -> dict[str, Any]:
    path = FIXTURES / name
    assert path.is_file(), f"missing fixture {name}"
    parsed: dict[str, Any] = json.loads(path.read_text())
    return parsed


def _grant(name: str) -> TaskParticipantGrantV1:
    raw = _fixture(name)
    expires = raw["expires_at"]
    return TaskParticipantGrantV1(
        task_id=uuid.UUID(raw["task_id"]),
        actor_id=raw["actor_id"],
        role=raw["role"],
        granted_by=raw["granted_by"],
        granted_at=datetime.datetime.fromisoformat(raw["granted_at"]),
        expires_at=None if expires is None else datetime.datetime.fromisoformat(expires),
        resolver_version=raw["resolver_version"],
    )


def _reference() -> ExternalReferenceV1:
    raw = _fixture("reference-normalized.json")
    return ExternalReferenceV1(
        source_system=raw["source_system"],
        source_namespace=raw["source_namespace"],
        kind=raw["kind"],
        external_id=raw["external_id"],
        classification=raw["classification"],
        external_authority=raw["external_authority"],
        revision=raw["revision"],
        authorized_uri=raw["authorized_uri"],
        observed_at=datetime.datetime.fromisoformat(raw["observed_at"]),
    )


def _checkpoint(
    payload_name: str = "checkpoint-payload-valid.json",
    *,
    sequence: int = 1,
    predecessor_id: uuid.UUID | None = None,
    evidence: tuple[ExternalReferenceV1, ...] = (),
) -> TaskCheckpointV1:
    return checkpoint_from_client_payload(
        _fixture(payload_name),
        checkpoint_id=_CHECKPOINT_ID,
        task_id=_TASK_ID,
        sequence=sequence,
        predecessor_id=predecessor_id,
        author="svc:api",
        recorded_at=_NOW,
        retention_policy="standard-90d",
        evidence=evidence,
    )


# --- participation is granted, never asserted --------------------------------


def test_every_declared_role_is_constructible() -> None:
    """A role that is legal in the vocabulary and rejected by the constructor is
    worse than an illegal one: the failure surfaces only for whoever holds it."""
    for role in sorted(PARTICIPANT_ROLES):
        grant = dataclasses.replace(_grant("grant-valid.json"), role=role)  # type: ignore[arg-type]
        assert grant.role == role


def test_a_grant_records_its_temporal_evidence_and_resolver() -> None:
    """A grant without them cannot be re-evaluated later: nothing says when it
    started applying, or which rule decided it was legitimate."""
    grant = _grant("grant-valid.json")
    assert grant.granted_at.tzinfo is not None
    assert grant.expires_at is not None
    assert grant.resolver_version


def test_a_grant_with_no_expiry_lasts_as_long_as_the_task() -> None:
    """Absent expiry is a decision somebody made, not a gap. It has to be
    representable, or every grant acquires an arbitrary end date."""
    grant = _grant("grant-perpetual.json")
    assert grant.expires_at is None
    assert grant.is_active_at(_NOW + datetime.timedelta(days=3650))


def test_a_grant_stops_conferring_after_it_expires() -> None:
    grant = _grant("grant-valid.json")
    assert grant.is_active_at(_NOW)
    assert not grant.is_active_at(_NOW - datetime.timedelta(days=1))
    assert grant.expires_at is not None
    assert not grant.is_active_at(grant.expires_at)


def test_a_self_grant_is_refused() -> None:
    """The spoof this shape exists to prevent. An actor who can name themselves
    a participant has asserted access rather than been granted it, and once
    stored the record is indistinguishable from a real grant."""
    with pytest.raises(InvalidContextItem, match="cannot grant themselves"):
        _grant("grant-self-granted.json")


def test_a_grant_that_expired_before_it_was_granted_is_refused() -> None:
    """It never conferred anything, so storing it would show the task an
    audience it never had."""
    with pytest.raises(InvalidContextItem, match="never applied"):
        _grant("grant-expired-before-granted.json")


def test_an_unknown_role_is_refused_and_names_the_legal_set() -> None:
    with pytest.raises(InvalidContextItem, match="unknown participant role") as exc:
        _grant("grant-unknown-role.json")
    for role in PARTICIPANT_ROLES:
        assert role in str(exc.value)


def test_a_naive_timestamp_cannot_be_evaluated_against_a_grant() -> None:
    """Comparing an aware grant window to a naive moment silently reads the
    moment as local time, which is how a grant appears active in one deployment
    and expired in another."""
    with pytest.raises(InvalidContextItem, match="naive timestamp"):
        _grant("grant-valid.json").is_active_at(datetime.datetime(2026, 8, 8))


# --- a checkpoint is what the server observed --------------------------------


def test_a_checkpoint_keeps_its_structure_rather_than_flattening_to_prose() -> None:
    """Resume treats these differently: an open question is work remaining, a
    completed check is work that need not repeat, an assumption is something a
    later agent may have to invalidate. Flattened, all three read as narrative."""
    checkpoint = _checkpoint()
    assert checkpoint.goal
    assert checkpoint.decisions
    assert checkpoint.assumptions
    assert checkpoint.completed_checks
    assert checkpoint.open_questions
    assert checkpoint.next_action


def test_a_finished_checkpoint_says_so_with_absence_not_an_empty_string() -> None:
    """`null` says nothing is left to do. An empty string says nobody said."""
    assert _checkpoint("checkpoint-payload-final.json").next_action is None


def test_the_first_checkpoint_has_no_predecessor_and_later_ones_must() -> None:
    """Resume walks the chain backwards, so a missing link anywhere but the
    start is indistinguishable from the beginning of the task."""
    assert _checkpoint(sequence=1, predecessor_id=None).predecessor_id is None

    with pytest.raises(InvalidContextItem, match="no predecessor"):
        _checkpoint(sequence=1, predecessor_id=_PREDECESSOR_ID)
    with pytest.raises(InvalidContextItem, match="names no predecessor"):
        _checkpoint(sequence=2, predecessor_id=None)


def test_evidence_references_are_normalized() -> None:
    """Two references to one external thing are a duplicate, not corroboration,
    and a reader counting sources would over-weight it."""
    reference = _reference()
    assert _checkpoint(evidence=(reference,)).evidence == (reference,)

    with pytest.raises(InvalidContextItem, match="normalized"):
        _checkpoint(evidence=(reference, reference))


def test_a_payload_cannot_supply_the_author_or_the_time() -> None:
    """The refusal is the point. Overriding would leave the field wrong and
    invisibly so, and a later reader could not tell an attributed checkpoint
    from a forged one."""
    with pytest.raises(InvalidContextItem, match="server-derived") as exc:
        _checkpoint("checkpoint-payload-spoofs-author.json")
    assert "author" in str(exc.value)
    assert "recorded_at" in str(exc.value)


def test_a_payload_cannot_supply_its_own_identity_or_digest() -> None:
    """Choosing your own checkpoint id overwrites somebody else's row; choosing
    your own digest makes the content unverifiable against it."""
    with pytest.raises(InvalidContextItem, match="server-derived") as exc:
        _checkpoint("checkpoint-payload-spoofs-identity.json")
    assert "checkpoint_id" in str(exc.value)
    assert "digest" in str(exc.value)


def test_an_unknown_field_is_refused_rather_than_dropped() -> None:
    """A silently-dropped field is indistinguishable from one the server
    understood and acted on."""
    with pytest.raises(InvalidContextItem, match="unknown checkpoint field") as exc:
        _checkpoint("checkpoint-payload-unknown-field.json")
    assert "confidence" in str(exc.value)


def test_the_client_and_server_field_sets_do_not_overlap() -> None:
    """If a name were in both, the closedness check and the spoofing check would
    disagree about it and the more permissive one would win."""
    assert not (CLIENT_FIELDS & SERVER_DERIVED_FIELDS)


# --- the digest is checked, not trusted --------------------------------------


def test_a_checkpoint_verifies_its_own_digest_on_construction() -> None:
    checkpoint = _checkpoint()
    assert checkpoint.digest == checkpoint.compute_digest()


def test_a_digest_that_does_not_match_its_content_is_refused() -> None:
    """A checkpoint that misnames itself breaks the predecessor chain resume
    depends on, so it is refused at construction rather than at read time."""
    checkpoint = _checkpoint()
    with pytest.raises(InvalidContextItem, match="digest does not match"):
        dataclasses.replace(checkpoint, digest="0" * 64)


def test_changing_any_content_field_changes_the_digest() -> None:
    """Otherwise two different checkpoints share an identity and the chain
    cannot distinguish them.

    Driven through `checkpoint_digest` rather than by building instances,
    because a checkpoint refuses to exist with a digest that does not match its
    own content -- which is the property the test above pins.
    """
    baseline = dict(
        checkpoint_id=_CHECKPOINT_ID,
        task_id=_TASK_ID,
        sequence=1,
        predecessor_id=None,
        goal="ship it",
        decisions=("use X",),
        assumptions=("Y holds",),
        evidence=(),
        completed_checks=("suite green",),
        open_questions=("what about Z?",),
        next_action="do Z",
        author="svc:api",
        retention_policy="standard-90d",
    )
    original = checkpoint_digest(**baseline)  # type: ignore[arg-type]

    for field, value in (
        ("goal", "something else"),
        ("decisions", ("a different decision",)),
        ("assumptions", ()),
        ("completed_checks", ()),
        ("open_questions", ("a new question",)),
        ("next_action", None),
        ("author", "actor:someone-else"),
        ("retention_policy", "legal-hold"),
        ("evidence", (_reference(),)),
    ):
        moved = checkpoint_digest(**{**baseline, field: value})  # type: ignore[arg-type]
        assert moved != original, field

    # Identity and position too: the same content at a different point in the
    # chain is a different checkpoint.
    assert checkpoint_digest(**{**baseline, "sequence": 2, "predecessor_id": _PREDECESSOR_ID}) != original  # type: ignore[arg-type]
    assert checkpoint_digest(**{**baseline, "checkpoint_id": _PREDECESSOR_ID}) != original  # type: ignore[arg-type]


def test_the_same_content_recorded_twice_has_one_digest() -> None:
    """The clock is deliberately outside the digest: folding it in would make
    every retry of one checkpoint look like new work."""
    first = _checkpoint()
    second = checkpoint_from_client_payload(
        _fixture("checkpoint-payload-valid.json"),
        checkpoint_id=_CHECKPOINT_ID,
        task_id=_TASK_ID,
        sequence=1,
        predecessor_id=None,
        author="svc:api",
        recorded_at=_NOW + datetime.timedelta(hours=1),
        retention_policy="standard-90d",
    )
    assert first.digest == second.digest


def test_a_checkpoint_binds_the_retention_policy_that_governed_it() -> None:
    """Bound at write time so a later policy change cannot retroactively decide
    how long this was kept."""
    assert _checkpoint().retention_policy == "standard-90d"
