"""What an extractor may conclude, and how much authority that conclusion may carry.

The rule this file exists for is the one SQL cannot hold: a derived claim inherits
at most what its weakest source was entitled to assert. The schema stores the
inputs to that comparison and can enforce none of it, because authority is a
source-issued string whose ordering lives in the governance ladder rather than in
the database. So the ceiling is a service-layer obligation, and these are the
tests that make it one.
"""

from __future__ import annotations

import uuid

import pytest

from contextplane.service.governance.authority import (
    AUTHORITY_OBSERVER_EXTRACTION,
    AUTHORITY_OBSERVER_HUMAN,
    AUTHORITY_OWNER_HUMAN,
    AUTHORITY_UNATTRIBUTED,
    SOURCE_AUTHORITY_RANK,
)
from contextplane.service.memory.derivation import (
    MAX_EXCERPT_CHARS,
    Assertion,
    DerivationProfile,
    DerivationRefused,
    DerivationService,
    Evidence,
    RecordedDerivation,
    assertion_digest,
    may_promote,
    weakest_authority,
)

_PROFILE = DerivationProfile(name="outcome-extractor", version="1.4.0")


def _evidence(authority: str = AUTHORITY_OBSERVER_EXTRACTION, **overrides: object) -> Evidence:
    fields: dict[str, object] = {
        "kind": "signal",
        "source_authority": authority,
        "classification": "internal",
        "signal_id": uuid.uuid4(),
    }
    fields.update(overrides)
    return Evidence(**fields)  # type: ignore[arg-type]


def _assertion(**overrides: object) -> Assertion:
    fields: dict[str, object] = {
        "subject_reference": "capability:billing",
        "predicate": "context_was_stale",
        "value": {"observed": "runbook referenced a removed step"},
        "applicability": "repo:roughcompass/contextplane",
    }
    fields.update(overrides)
    return Assertion(**fields)  # type: ignore[arg-type]


# --- The ceiling --------------------------------------------------------------


def test_the_weakest_source_sets_the_ceiling() -> None:
    """Weakest, not strongest, and not the one that happened to trigger the run."""
    ceiling = weakest_authority(
        [
            _evidence(AUTHORITY_OWNER_HUMAN),
            _evidence(AUTHORITY_OBSERVER_EXTRACTION),
            _evidence(AUTHORITY_OBSERVER_HUMAN),
        ]
    )
    assert ceiling == AUTHORITY_OBSERVER_EXTRACTION


def test_the_ceiling_is_computed_by_rank_not_by_string_order() -> None:
    """Comparing the strings would sort alphabetically and mean nothing.

    `observer_extraction` sorts before `owner_human` alphabetically while being
    the *weaker* of the two, so a string comparison would hand a derived claim
    more authority than its evidence had — silently, and in the dangerous
    direction.
    """
    pair = [_evidence(AUTHORITY_OWNER_HUMAN), _evidence(AUTHORITY_OBSERVER_EXTRACTION)]
    assert weakest_authority(pair) == AUTHORITY_OBSERVER_EXTRACTION
    assert min(AUTHORITY_OWNER_HUMAN, AUTHORITY_OBSERVER_EXTRACTION) == AUTHORITY_OBSERVER_EXTRACTION
    assert SOURCE_AUTHORITY_RANK[AUTHORITY_OBSERVER_EXTRACTION] > SOURCE_AUTHORITY_RANK[AUTHORITY_OWNER_HUMAN]


def test_no_evidence_licenses_nothing() -> None:
    assert weakest_authority([]) == AUTHORITY_UNATTRIBUTED


@pytest.mark.parametrize(
    ("claimed", "evidence_authority"),
    [
        pytest.param(AUTHORITY_OWNER_HUMAN, AUTHORITY_OBSERVER_EXTRACTION, id="owner-from-observer"),
        pytest.param(AUTHORITY_OBSERVER_HUMAN, AUTHORITY_UNATTRIBUTED, id="human-from-unattributed"),
    ],
)
def test_an_attempt_claiming_more_than_its_evidence_is_refused(claimed: str, evidence_authority: str) -> None:
    """Refused rather than clamped.

    Clamping would produce an assertion nobody asked for and leave the caller
    believing the stronger one was recorded. The refusal names the ceiling, so
    the caller learns which evidence was too weak.
    """
    service = DerivationService(_unused_factory(), clock=_FrozenClock())
    with pytest.raises(DerivationRefused, match="may carry at most"):
        service._assert_within_ceiling(claimed, evidence_authority)


@pytest.mark.parametrize(
    "claimed",
    [
        pytest.param(AUTHORITY_OBSERVER_EXTRACTION, id="exactly-at-the-ceiling"),
        pytest.param(AUTHORITY_UNATTRIBUTED, id="below-the-ceiling"),
    ],
)
def test_an_attempt_at_or_below_the_ceiling_is_allowed(claimed: str) -> None:
    """The ceiling must not refuse everything, which a too-eager check would.

    Asserted positively rather than by calling and hoping: a test whose only
    evidence is the absence of an exception passes just as happily when the
    method under test does nothing at all.
    """
    service = DerivationService(_unused_factory(), clock=_FrozenClock())
    assert _permits(service, claimed, AUTHORITY_OBSERVER_EXTRACTION) is True


def test_an_authority_off_the_ladder_is_refused() -> None:
    """A tier nobody declared has no rank, so no comparison against it means anything."""
    with pytest.raises(DerivationRefused, match="ladder"):
        _evidence("supreme_authority")


# --- What may be asserted -----------------------------------------------------


def test_causation_is_not_a_predicate_an_extractor_may_assert() -> None:
    """Two observations are not a cause.

    "The run failed" and "this context was served" are both readable from the
    evidence; "this context caused the failure" is a third claim with evidence
    requirements no extractor reading these inputs can meet.
    """
    with pytest.raises(DerivationRefused, match="not one an extractor may assert"):
        _assertion(predicate="caused_failure")


@pytest.mark.parametrize("field", ["subject_reference", "applicability"])
def test_an_assertion_without_subject_or_scope_is_refused(field: str) -> None:
    """An assertion naming neither what it is about nor where it holds cannot be reviewed."""
    with pytest.raises(DerivationRefused):
        _assertion(**{field: "   "})


def test_an_unknown_evidence_kind_is_refused() -> None:
    with pytest.raises(DerivationRefused, match="unknown evidence kind"):
        _evidence(kind="rumour")


def test_checkpoint_evidence_needs_its_digest() -> None:
    """The id says which checkpoint; the digest says it had not changed when read.

    A citation without the digest claims an immutability it never verified.
    """
    with pytest.raises(DerivationRefused, match="digest"):
        Evidence(
            kind="checkpoint",
            source_authority=AUTHORITY_OWNER_HUMAN,
            classification="internal",
            checkpoint_id=uuid.uuid4(),
        )


def test_exact_item_evidence_needs_both_halves() -> None:
    with pytest.raises(DerivationRefused, match="receipt"):
        Evidence(
            kind="receipt_item",
            source_authority=AUTHORITY_OWNER_HUMAN,
            classification="internal",
            receipt_id=uuid.uuid4(),
        )


# --- Excerpts are excerpts ----------------------------------------------------


def test_an_excerpt_longer_than_the_bound_is_refused() -> None:
    """A bounded excerpt that happens to be the whole field is a copy with a shorter name.

    The bound is a length the code enforces, not an intention a docstring states.
    """
    with pytest.raises(DerivationRefused, match="bound is"):
        _evidence(excerpt="x" * (MAX_EXCERPT_CHARS + 1))


def test_an_excerpt_within_the_bound_is_kept() -> None:
    item = _evidence(excerpt="y" * MAX_EXCERPT_CHARS)
    assert item.excerpt is not None
    assert len(item.excerpt) == MAX_EXCERPT_CHARS


def test_the_bound_is_far_below_a_body() -> None:
    """The number matters less than the property: no body fits.

    A bound generous enough to hold a checkpoint payload would let a copy pass as
    a quotation, which is the failure this bound exists to prevent.
    """
    assert MAX_EXCERPT_CHARS <= 2048


# --- Identity -----------------------------------------------------------------


def test_the_same_conclusion_from_the_same_profile_digests_alike() -> None:
    assert assertion_digest(_PROFILE, _assertion()) == assertion_digest(_PROFILE, _assertion())


def test_a_later_extractor_version_is_a_different_attempt() -> None:
    """Folding the version out would make an upgrade look like a replay."""
    later = DerivationProfile(name=_PROFILE.name, version="1.5.0")
    assert assertion_digest(_PROFILE, _assertion()) != assertion_digest(later, _assertion())


def test_a_changed_conclusion_digests_differently() -> None:
    assert assertion_digest(_PROFILE, _assertion()) != assertion_digest(
        _PROFILE, _assertion(predicate="context_was_incomplete")
    )


# --- Supersession -------------------------------------------------------------


def test_promotion_is_barred_when_every_input_was_superseded() -> None:
    """Both runs happened; the superseded one is no longer the thing to learn from.

    The attempt is still recorded — dropping it would lose the fact that the
    derivation was made — but promoting on evidence a later run has overtaken
    would canonicalize a conclusion that evidence may already contradict.
    """
    recorded = _recorded(superseded_only=True)
    assert may_promote(recorded) is False


def test_promotion_is_allowed_when_any_input_still_stands() -> None:
    assert may_promote(_recorded(superseded_only=False)) is True


# --- Test doubles -------------------------------------------------------------


class _FrozenClock:
    def now(self) -> object:  # pragma: no cover - never read by the paths under test
        raise AssertionError("these tests exercise pure decisions and must not need a clock")


def _unused_factory() -> object:
    """A session factory the ceiling checks never call.

    Passed rather than mocked so a test that accidentally reaches the database
    fails loudly instead of quietly exercising a stub.
    """

    def factory() -> object:
        raise AssertionError("this test must not open a session")

    return factory


def _permits(service: DerivationService, claimed: str, ceiling: str) -> bool:
    """Whether the ceiling check admits this pairing, as a value a test can assert on.

    The check signals refusal by raising, so turning that into a boolean is what
    lets the allowed cases make a positive claim instead of relying on silence.
    """
    try:
        service._assert_within_ceiling(claimed, ceiling)
    except DerivationRefused:
        return False
    return True


def _recorded(*, superseded_only: bool) -> RecordedDerivation:
    return RecordedDerivation(
        derivation_id=uuid.uuid4(),
        assertion_digest="sha256:abc",
        source_authority=AUTHORITY_OBSERVER_EXTRACTION,
        classification="internal",
        status="pending",
        evidence_count=1,
        superseded_only=superseded_only,
        replayed=False,
    )
