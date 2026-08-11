"""Selecting governed context for the lifecycle placement a caller declares.

Three rules are proved here, and each one fails in a different direction.

**The kind vocabulary is closed.** A reference's kind is part of its collision
scope, so a misspelled kind is not inert data -- it stores, it binds, and it
then fails to join to the work it names, which reads downstream as "no result
yet" rather than as an error. The refusal is asserted directly rather than
inferred from a missing join, because the missing join is precisely the symptom
nobody recognises.

**Silence is not a mismatch.** An item that never recorded where it applies is
kept. Getting this backwards would mean a caller who describes themselves more
precisely receives *less* governed context than one who says nothing -- material
hidden as a side effect of a correct request.

**A disagreement is reported, not swallowed.** Withheld items come back as
exclusions, which is what makes a narrowed block distinguishable from a block
whose sources had nothing to say.

The rule is exercised over a placement table rather than through a database:
the decision under test is a comparison between two placements, and a fixture
that has to insert a claim to ask about one would fail for reasons that have
nothing to do with the comparison.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from contextplane.context.lifecycle import (
    LIFECYCLE_REFERENCE_KINDS,
    LifecycleProfile,
    UnknownLifecycleReferenceKind,
    narrow,
    normalize_reference_kind,
    partition,
    placement_of,
)
from contextplane.context.schemas.trust import ExternalReferenceV1
from contextplane.service.memory.derivation import CrossStageApplicability
from contextplane.types import TenantContext

_TENANT = uuid.uuid4()
_OBSERVED = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, actor_id=uuid.uuid4(), roles=["consumer"])


def _ref(kind: str, external_id: str) -> ExternalReferenceV1:
    return ExternalReferenceV1(
        source_system="control-plane",
        source_namespace="acme",
        kind=kind,
        external_id=external_id,
        classification="internal",
        external_authority="acme/delivery",
        observed_at=_OBSERVED,
    )


def _placed(**dimensions: str) -> str:
    """One stored applicability field, written the way derivation writes it."""
    return CrossStageApplicability(**dimensions).as_field()


# -- the closed vocabulary -----------------------------------------------------


def test_the_vocabulary_is_exactly_the_ten_agreed_kinds() -> None:
    """Pinned as a set, so widening it is a decision somebody makes on purpose.

    The control-plane translation enforces the same constant. A kind added here
    without being agreed there would be accepted on one write path and refused
    on the other, which is the asymmetry a shared constant exists to prevent.
    """
    assert set(LIFECYCLE_REFERENCE_KINDS) == {
        "run",
        "stage",
        "work_item",
        "repository",
        "artifact",
        "action",
        "build",
        "deployment",
        "incident",
        "outcome",
    }
    assert len(LIFECYCLE_REFERENCE_KINDS) == len(set(LIFECYCLE_REFERENCE_KINDS)), "a duplicate kind is a typo"


@pytest.mark.parametrize("kind", LIFECYCLE_REFERENCE_KINDS)
def test_every_agreed_kind_is_accepted(kind: str) -> None:
    assert normalize_reference_kind(kind) == kind


@pytest.mark.parametrize("spelling", ["Deployment", "DEPLOYMENT", "  deployment  "])
def test_case_and_padding_are_folded_rather_than_refused(spelling: str) -> None:
    """A differently-cased kind means the kind it spells.

    Folded rather than refused because case cannot cause a wrong join once it is
    normalized, and refusing it would be pedantry that a real control plane
    trips over on its first request.
    """
    assert normalize_reference_kind(spelling) == "deployment"


@pytest.mark.parametrize("misspelling", ["deploymnet", "deployments", "release", ""])
def test_a_kind_outside_the_vocabulary_is_refused_rather_than_accepted(misspelling: str) -> None:
    """The negative control, and the reason this vocabulary is closed at all.

    An unrecognised kind must not be stored. It would bind cleanly and then
    never join to the receipt that cited the correct spelling for the same
    external id -- surfacing not as an error but as an absence, which is the one
    failure shape that gets read as "nothing happened yet".
    """
    with pytest.raises(UnknownLifecycleReferenceKind) as raised:
        normalize_reference_kind(misspelling)

    assert "collision scope" in str(raised.value), "the refusal must say why an unknown kind is not merely ignored"


def test_a_profile_refuses_an_unknown_kind_at_construction() -> None:
    """Enforced where the profile is built, not only where a string is normalized.

    Nothing downstream re-checks, so a profile that exists must be one whose
    kinds are legal.
    """
    with pytest.raises(UnknownLifecycleReferenceKind):
        LifecycleProfile.of([_ref("stage", "implementation"), _ref("deploymnet", "prod-42")])


# -- what a profile selects on -------------------------------------------------


def test_a_profile_takes_its_placement_from_the_references_it_was_given() -> None:
    """Derived by the same function that recorded the placement being compared.

    Sharing the derivation is the point: a selector with its own copy of "which
    reference kind means which dimension" is correct until one of the two is
    edited, and the failure is a filter that silently matches nothing.
    """
    profile = LifecycleProfile.of(
        [_ref("repository", "acme/web"), _ref("stage", "implementation"), _ref("work_item", "WEB-41")]
    )

    assert dict(profile.placement) == {
        "repository": "acme/web",
        "stage": "implementation",
        "work_type": "work_item",
    }
    assert profile.selects()


def test_a_profile_whose_references_place_nothing_narrows_nothing() -> None:
    """A build id is a real reference and not a placement.

    Reported as "selects nothing" rather than as an empty filter so the arm can
    skip a read whose result could not change the answer.
    """
    profile = LifecycleProfile.of([_ref("build", "ci-9931")])

    assert dict(profile.placement) == {}
    assert not profile.selects()


def test_a_profile_records_its_references_for_the_receipt_with_the_parts_behind_them() -> None:
    """The references, not the placement, because placement is re-derivable.

    A receipt storing only the conclusion could not show the input it was
    reached from, and the collision key is what makes two spellings of one
    reference compare equal.
    """
    profile = LifecycleProfile.of([_ref("stage", "implementation")])

    (recorded,) = profile.record()
    assert recorded["kind"] == "stage"
    assert recorded["external_id"] == "implementation"
    assert recorded["collision_key"] == _ref("stage", "implementation").collision_key()


# -- the rule ------------------------------------------------------------------


def test_an_item_placed_at_another_stage_is_withheld_and_the_reason_names_both() -> None:
    profile = LifecycleProfile.of([_ref("stage", "implementation")])

    reason = profile.excludes(placement_of(_placed(stage="deployment")))

    assert reason is not None
    assert "deployment" in reason, "the reason must name where the item said it applies"
    assert "implementation" in reason, "and where this request is placed"


def test_an_item_placed_at_the_callers_stage_is_kept() -> None:
    profile = LifecycleProfile.of([_ref("stage", "implementation")])

    assert profile.excludes(placement_of(_placed(stage="implementation"))) is None


def test_placement_comparison_ignores_case_and_padding() -> None:
    """Two spellings of one stage name are one stage.

    The control plane owns the stage vocabulary and nothing here normalizes it
    on the way in, so the comparison is where a harmless difference must not
    become a silent exclusion.
    """
    profile = LifecycleProfile.of([_ref("stage", "Implementation")])

    assert profile.excludes(placement_of(_placed(stage=" implementation "))) is None


def test_an_item_that_recorded_no_placement_is_kept() -> None:
    """Silence is not a mismatch, and this is the assertion that pins it.

    Free-text applicability predates dimensions entirely. Reading its silence as
    "applies nowhere" would empty the block for any caller who supplied a
    profile, which is governed material hidden as a side effect of a more
    precise request.
    """
    profile = LifecycleProfile.of([_ref("stage", "implementation")])

    assert profile.excludes(placement_of("wherever the payments team works")) is None
    assert profile.excludes(placement_of(_placed(repository="acme/web"))) is None


def test_a_caller_who_does_not_narrow_a_dimension_keeps_items_placed_on_it() -> None:
    """The rule is symmetric: an unnamed dimension is not a filter of its own."""
    profile = LifecycleProfile.of([_ref("repository", "acme/web")])

    assert profile.excludes(placement_of(_placed(repository="acme/web", stage="deployment"))) is None


def test_disagreement_on_any_selected_dimension_is_enough_to_withhold() -> None:
    profile = LifecycleProfile.of([_ref("repository", "acme/web"), _ref("stage", "implementation")])

    assert profile.excludes(placement_of(_placed(repository="acme/api", stage="implementation"))) is not None
    assert profile.excludes(placement_of(_placed(repository="acme/web", stage="review"))) is not None
    assert profile.excludes(placement_of(_placed(repository="acme/web", stage="implementation"))) is None


def test_work_type_is_compared_as_the_kind_and_not_as_the_identifier() -> None:
    """ "Learned against an incident" is the dimension; which incident is evidence.

    Asserted because the two are easy to conflate, and conflating them would
    make every incident-derived conclusion apply to exactly one incident.
    """
    profile = LifecycleProfile.of([_ref("incident", "INC-9")])

    assert dict(profile.placement)["work_type"] == "incident"
    assert profile.excludes(placement_of(_placed(work_type="incident"))) is None
    assert profile.excludes(placement_of(_placed(work_type="work_item"))) is not None


def test_partition_keeps_and_withholds_by_the_same_rule() -> None:
    """The arm-facing split, over a table of placements rather than claims."""
    profile = LifecycleProfile.of([_ref("stage", "implementation")])

    kept, withheld = partition(
        profile,
        [
            ("here", _placed(stage="implementation")),
            ("elsewhere", _placed(stage="deployment")),
            ("unplaced", "free text"),
        ],
    )

    assert kept == frozenset({"here", "unplaced"})
    assert set(withheld) == {"elsewhere"}


# -- narrowing a served set ----------------------------------------------------


class _Claim:
    """A served claim reduced to the field selection reads off it."""

    def __init__(self, claim_id: uuid.UUID) -> None:
        self.claim_id = claim_id


class _PlacementSession:
    """A session that answers the placement read and nothing else."""

    def __init__(self, rows: list[tuple[uuid.UUID | None, str]]) -> None:
        self._rows = rows
        self.executed = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        self.executed += 1
        rows = self._rows

        class _Result:
            @staticmethod
            def all() -> list[tuple[uuid.UUID | None, str]]:
                return rows

        return _Result()

    async def __aenter__(self) -> _PlacementSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_narrow_returns_the_kept_claims_and_an_exclusion_for_each_withheld_one() -> None:
    here, elsewhere, unplaced = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    claims = [_Claim(here), _Claim(elsewhere), _Claim(unplaced)]
    session = _PlacementSession([(here, _placed(stage="implementation")), (elsewhere, _placed(stage="deployment"))])
    profile = LifecycleProfile.of([_ref("stage", "implementation")])

    kept, exclusions = await narrow(lambda: session, profile, claims, _ctx())

    assert [claim.claim_id for claim in kept] == [here, unplaced], "a claim with no placement row is kept"
    assert [exclusion.item_key for exclusion in exclusions] == [str(elsewhere)]
    assert "deployment" in exclusions[0].reason


@pytest.mark.asyncio
async def test_narrow_asks_nothing_of_the_database_when_there_are_no_claims() -> None:
    """An empty served set has no placements to look up.

    Worth pinning: the read builds an `IN` over claim ids, and an empty one is
    both pointless and a shape some drivers render as a query matching nothing
    in a way that is slower than not asking.
    """
    session = _PlacementSession([])

    kept, exclusions = await narrow(lambda: session, LifecycleProfile.of([_ref("stage", "x")]), [], _ctx())

    assert kept == ()
    assert exclusions == ()
    assert session.executed == 0


@pytest.mark.asyncio
async def test_exclusions_come_back_in_a_stable_order() -> None:
    """Two identical resolutions must produce identical evidence.

    The withheld set is built as a mapping, and a mapping's iteration order is
    not something a receipt can be checked against.
    """
    ids = sorted((uuid.uuid4() for _ in range(4)), key=str)
    session = _PlacementSession([(claim_id, _placed(stage="deployment")) for claim_id in reversed(ids)])
    profile = LifecycleProfile.of([_ref("stage", "implementation")])

    _kept, exclusions = await narrow(lambda: session, profile, [_Claim(i) for i in ids], _ctx())

    assert [exclusion.item_key for exclusion in exclusions] == [str(claim_id) for claim_id in ids]
