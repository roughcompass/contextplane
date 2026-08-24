"""What the resolver fingerprint must and must not distinguish.

E22-T15. The fingerprint decides whether two runs are comparable, so its failure
modes are symmetric and both are bad. Too sensitive and every prior run is
declared incomparable, which is the same as having no fingerprint — a comparison
surface that always says "these are not comparable" is one nobody consults. Too
insensitive and a configuration change is reported as a quality change, which is
the specific wrong answer this whole loop exists to avoid producing.
"""

from __future__ import annotations

import dataclasses

import pytest

from contextplane.context.evaluation.fingerprint import FINGERPRINT_GENERATION, resolver_fingerprint
from contextplane.context.semantic_workspace import RecallDecision

_DECISION = RecallDecision(
    arm_kind="lexical",
    branch="lexical_only",
    lexical_approved=True,
    limit=20,
    open_review_obligations=(),
    reviewed_on="2026-06-01",
    semantic_approved=False,
    similarity_floor=0.62,
    void_safety_dimensions=(),
)


def _fingerprint(**overrides: object) -> str:
    base: dict[str, object] = {
        "arm_limit": 25,
        "arm_timeout_s": 2.0,
        "decision": _DECISION,
        "embedder_available": False,
        "item_cap": 50,
    }
    base.update(overrides)
    return resolver_fingerprint(**base)  # type: ignore[arg-type]


def test_the_same_deployment_fingerprints_the_same() -> None:
    assert _fingerprint() == _fingerprint()


def test_it_is_a_digest_in_the_one_spelling_the_schema_accepts() -> None:
    """The column checks this shape, so a value the service could produce and the
    database refuse would fail on the first run rather than in review."""
    value = _fingerprint()

    assert value.startswith("sha256:")
    assert len(value) == len("sha256:") + 64


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"arm_limit": 26}, id="arm_limit"),
        pytest.param({"arm_timeout_s": 2.5}, id="arm_timeout_s"),
        pytest.param({"embedder_available": True}, id="embedder"),
        pytest.param({"item_cap": 51}, id="item_cap"),
        pytest.param({"decision": dataclasses.replace(_DECISION, branch="semantic_exact")}, id="branch"),
        pytest.param({"decision": dataclasses.replace(_DECISION, semantic_approved=True)}, id="semantic_approved"),
        pytest.param({"decision": dataclasses.replace(_DECISION, lexical_approved=False)}, id="lexical_approved"),
        pytest.param({"decision": dataclasses.replace(_DECISION, similarity_floor=0.8)}, id="similarity_floor"),
    ],
)
def test_every_fact_that_changes_retrieval_changes_the_fingerprint(overrides: dict[str, object]) -> None:
    """Each of these can change what a resolution returns and none can be set per
    request. A run taken before one moved is not comparable to one taken after."""
    assert _fingerprint(**overrides) != _fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"decision": dataclasses.replace(_DECISION, reviewed_on="2027-01-01")}, id="reviewed_on"),
        pytest.param(
            {"decision": dataclasses.replace(_DECISION, open_review_obligations=("something",))},
            id="open_obligations",
        ),
        pytest.param(
            {"decision": dataclasses.replace(_DECISION, void_safety_dimensions=("pii",))},
            id="void_dimensions",
        ),
    ],
)
def test_a_fact_that_cannot_change_a_resolution_does_not_change_the_fingerprint(
    overrides: dict[str, object],
) -> None:
    """The other failure mode, and the one that makes a fingerprint useless.

    A digest over everything to hand would change when the decision artifact's
    review date was edited, declaring every prior run incomparable over a fact no
    resolution consults.
    """
    assert _fingerprint(**overrides) == _fingerprint()


def test_the_absent_embedder_is_a_third_state_and_not_a_synonym_for_unapproved() -> None:
    """A deployment with no model, one with a model and a branch that forbids
    using it, and one that uses it return three different workspace blocks, so
    they are three fingerprints."""
    approved = dataclasses.replace(_DECISION, branch="semantic_exact", semantic_approved=True)

    no_model = _fingerprint(decision=approved, embedder_available=False)
    with_model = _fingerprint(decision=approved, embedder_available=True)
    not_approved = _fingerprint(decision=_DECISION, embedder_available=True)

    assert len({no_model, with_model, not_approved}) == 3


def test_the_generation_is_in_the_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two generations that happened to agree on every field would otherwise
    compare equal while measuring different things.

    A generation bump makes every prior run visibly incomparable, and that is
    correct rather than unfortunate: a run taken before the product knew a fact
    was relevant cannot say what that fact was.
    """
    before = _fingerprint()

    monkeypatch.setattr(
        "contextplane.context.evaluation.fingerprint.FINGERPRINT_GENERATION",
        FINGERPRINT_GENERATION + 1,
    )

    assert _fingerprint() != before
