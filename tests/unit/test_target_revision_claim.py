"""A caller's claim about what it composed against is checked, and reported.

`TargetRevisionV1` was declared with a stated purpose -- "a body composed against
one revision and validated against another passes or fails for reasons the caller
cannot see" -- and neither of its fields was read by anything. These tests pin the
check that closes that, and the three decisions that shape it.

**It is a violation, not a refusal.** The binding decides the mode, which is the
first rule `contextplane/entities/validation.py` states about itself. A router
refusing on a caller-supplied string would be a caller choosing its own
enforcement level.

**Both halves are checked.** A revision id alone does not identify what validates
a write: a tenant can rebind to a different extension set at the same
`profile_revision_id`, which is the rollback case
`0060_binding_extension_members` was written for. A check that compared only the
revision would return green in exactly that case.

**Absent means unchecked, never agreed.** A caller that cannot attest to a value
is better off saying nothing than inventing one, and the unread field was the
original defect precisely because every client invented something.
"""

from __future__ import annotations

import uuid

import pytest

from contextplane.entities.validation import (
    STALE_TARGET_REVISION,
    GoverningProfile,
    TargetRevisionClaim,
    target_revision_violations,
)

_REVISION = uuid.UUID("11111111-1111-4111-8111-111111111111")
_OTHER = uuid.UUID("22222222-2222-4222-8222-222222222222")
_DIGEST = "sha256:aaaa"


def _governing(*, revision: uuid.UUID = _REVISION, digest: str = _DIGEST) -> GoverningProfile:
    return GoverningProfile(revision_id=revision, state="active", document="{}", extension_set_digest=digest)


def _codes(claim: TargetRevisionClaim | None) -> list[str]:
    return [violation.code for violation in target_revision_violations(claim, _governing())]


def test_a_claim_agreeing_with_the_binding_says_nothing() -> None:
    claim = TargetRevisionClaim(profile_revision=str(_REVISION), binding_revision=_DIGEST)

    assert _codes(claim) == []


def test_a_stale_revision_is_reported_by_name() -> None:
    claim = TargetRevisionClaim(profile_revision=str(_OTHER))

    assert _codes(claim) == [STALE_TARGET_REVISION]


def test_a_stale_extension_set_is_reported_even_when_the_revision_agrees() -> None:
    """The case a revision-only check would pass.

    A rebind can drop or add an extension while pointing at the same revision --
    the rollback case the binding lifecycle exists for -- so the revision
    agreeing is not enough.
    """
    claim = TargetRevisionClaim(profile_revision=str(_REVISION), binding_revision="sha256:bbbb")

    assert _codes(claim) == [STALE_TARGET_REVISION]


def test_both_halves_stale_are_reported_separately() -> None:
    """One violation per disagreement, so the detail names which half moved."""
    claim = TargetRevisionClaim(profile_revision=str(_OTHER), binding_revision="sha256:bbbb")

    violations = target_revision_violations(claim, _governing())
    assert [v.code for v in violations] == [STALE_TARGET_REVISION, STALE_TARGET_REVISION]
    assert "profile revision" in violations[0].detail
    assert "extension set" in violations[1].detail


def test_an_absent_claim_is_unchecked_rather_than_agreed() -> None:
    assert _codes(None) == []


def test_an_absent_half_is_unchecked_while_the_other_is_still_read() -> None:
    """A caller that can attest to one and not the other gets the check it earned."""
    assert _codes(TargetRevisionClaim(profile_revision=str(_REVISION))) == []
    assert _codes(TargetRevisionClaim(binding_revision="sha256:bbbb")) == [STALE_TARGET_REVISION]


def test_the_detail_names_both_values_so_a_caller_can_act_on_it() -> None:
    """A refusal that says only "stale" leaves the reader to go and look."""
    claim = TargetRevisionClaim(profile_revision=str(_OTHER))

    detail = target_revision_violations(claim, _governing())[0].detail
    assert str(_OTHER) in detail
    assert str(_REVISION) in detail


def test_the_code_is_not_the_publication_time_one() -> None:
    """`incompatible_target_revision` is a compile conflict over content digests,
    emitted from three profile schema modules and asserted exhaustively by
    `tests/conformance/test_profile_schema_entity.py`. One code meaning two things
    on two surfaces is a code a reader cannot act on."""
    assert STALE_TARGET_REVISION == "stale_target_revision"
    assert STALE_TARGET_REVISION != "incompatible_target_revision"


@pytest.mark.parametrize("state", ["active", "validating"])
def test_the_check_does_not_decide_the_mode(state: str) -> None:
    """It returns violations and nothing else. What they cost is the binding's
    call: mandatory refuses, advisory reports."""
    governing = GoverningProfile(revision_id=_REVISION, state=state, document="{}", extension_set_digest=_DIGEST)

    violations = target_revision_violations(TargetRevisionClaim(profile_revision="nope"), governing)

    assert [v.code for v in violations] == [STALE_TARGET_REVISION]
