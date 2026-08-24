"""The row mapping, and the one field a database seed cannot reach cheaply.

E22-T8. `tests/integration/test_arc_revision_index.py` proves the query — what
it selects, what it scopes to, and that the cursor neither skips nor repeats.
This proves the mapping from a row to a `RevisionRow`, which is where the three
activation fields are decided.

`has_approval_evidence` is here rather than there because `approval_evidence_id`
sits behind a chain of foreign keys — evidence, verifier, signer key — and
seeding ARC's whole approval subsystem to assert one boolean would be a fixture
larger than the thing it tests, and one that fails as a fixture error wearing
the shape of a result.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.arc.service.revision_index import (
    LIFECYCLE_STATES,
    TERMINAL_STATES,
    RevisionIndexService,
)

_NOW = datetime.datetime(2026, 8, 24, 12, 0, tzinfo=datetime.UTC)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "revision_id": uuid.uuid4(),
        "artifact_id": uuid.uuid4(),
        "artifact_slug": "retry-policy",
        "artifact_kind": "policy",
        "lifecycle_state": "draft",
        "source_system": "git",
        "source_revision_locator": "commit:abc123",
        "content_digest": "d" * 64,
        "approval_evidence_id": None,
        "effective_from": _NOW,
        "effective_until": None,
        "review_expires_at": _NOW + datetime.timedelta(days=30),
        "activated_at": None,
        "revoked_at": None,
        "created_at": _NOW,
        "resolutions_under_revision": 0,
    }
    base.update(overrides)
    return base


def test_approval_evidence_is_reported_from_the_column_both_ways() -> None:
    """A boolean derived from a nullable id, and derived rather than stored so
    the two cannot disagree."""
    without = RevisionIndexService._row(_row(), _NOW)
    with_evidence = RevisionIndexService._row(_row(approval_evidence_id=uuid.uuid4()), _NOW)

    assert without.has_approval_evidence is False
    assert with_evidence.has_approval_evidence is True


def test_the_row_carries_no_activation_verdict() -> None:
    """Ten predicates computed as if the caller were activating is the
    per-revision endpoint's answer.

    A list that answered it would be a second, weaker computation wearing the
    same name — and two surfaces disagreeing about whether a revision can
    activate is worse than one surface declining to say.
    """
    row = RevisionIndexService._row(_row(), _NOW)

    for absent in ("can_activate", "eligible", "activation_eligibility", "blocked_reasons"):
        assert not hasattr(row, absent), f"the list row grew {absent!r}; that belongs to the endpoint"


@pytest.mark.parametrize("state", sorted(LIFECYCLE_STATES))
def test_terminality_is_decided_once_for_every_state(state: str) -> None:
    """Every legal state gets an answer, so a state added to the vocabulary
    without being classified here shows up as a failure rather than as a row
    silently reported non-terminal."""
    row = RevisionIndexService._row(_row(lifecycle_state=state), _NOW)

    assert row.is_terminal is (state in TERMINAL_STATES)


def test_the_review_window_is_judged_against_the_clock_and_not_the_row() -> None:
    """`review_expired` is a comparison, so the answer moves with time rather
    than with what was stored — which is what makes it worth reporting."""
    expires = _NOW + datetime.timedelta(days=1)

    assert RevisionIndexService._row(_row(review_expires_at=expires), _NOW).review_expired is False
    assert (
        RevisionIndexService._row(_row(review_expires_at=expires), _NOW + datetime.timedelta(days=2)).review_expired
        is True
    )


def test_a_review_window_closing_exactly_now_is_expired() -> None:
    """The boundary is `<=`. A window that has run out is out, and reporting it
    as open for the instant it closes would be a state nobody can act on."""
    assert RevisionIndexService._row(_row(review_expires_at=_NOW), _NOW).review_expired is True


def test_only_a_draft_reports_as_one() -> None:
    assert RevisionIndexService._row(_row(lifecycle_state="draft"), _NOW).is_draft is True
    assert RevisionIndexService._row(_row(lifecycle_state="active"), _NOW).is_draft is False


def test_the_resolution_count_is_carried_as_an_integer() -> None:
    """The field the two terminal acts differ over. Read as an int rather than
    passed through, because a count arriving as something else would render as
    a string beside a number a reader is comparing."""
    row = RevisionIndexService._row(_row(resolutions_under_revision=42), _NOW)

    assert row.resolutions_under_revision == 42
    assert isinstance(row.resolutions_under_revision, int)
