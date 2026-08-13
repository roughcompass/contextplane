"""A grant is the only way data crosses an organization boundary, and omission denies.

Every test here is about a *default*. The dangerous failure in a sharing system is
not a wrong rule — it is a rule that was never stated and got read as permission:
an empty selector list treated as "all types", a missing operation treated as
allowed, an unknown classification ranked below the ceiling. Each of those turns a
half-filled grant into an unlimited one, and each looks like an ordinary green
test suite from the outside.

So the cases below assert what happens when something is *absent*, and the
positive cases exist only to keep the negatives honest: a decision function that
refused everything would satisfy every denial test in this file.

**A denial reveals nothing.** The reasons are a closed, coarse set and none of them
distinguishes "no such entity" from "not shared with you". A caller that could
tell those apart could enumerate a neighbour's identifiers by watching which
denials changed, so `Decision` refuses to carry a grant id on a denial at all.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.sharing import grants as grant_writer
from contextplane.sharing.authorization import (
    CLASSIFICATION_ABOVE_CEILING,
    DENIAL_REASONS,
    NO_GRANT,
    OPERATION_NOT_GRANTED,
    TYPE_NOT_GRANTED,
    Decision,
    authorize,
)
from scripts.check_privileged_writes import RULES

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)


def _grant(**overrides: object) -> grant_writer.CrossOrgGrant:
    fields: dict[str, object] = {
        "grant_id": uuid.uuid4(),
        "source_tenant_id": uuid.uuid4(),
        "destination_tenant_id": uuid.uuid4(),
        "grant_kind": "relationship",
        "grant_state": grant_writer.ACTIVE,
        "profile_types": ["core:capability"],
        "relationship_types": ["core:depends_on"],
        "allowed_operations": ["read"],
        "classification_ceiling": "internal",
        "effective_from": _NOW - datetime.timedelta(days=1),
        "effective_to": None,
        "approval_evidence": "review-1",
        "revoked_at": None,
    }
    fields.update(overrides)
    return grant_writer.CrossOrgGrant(**fields)  # type: ignore[arg-type]


# --- omission denies ------------------------------------------------------------------


def test_no_grant_at_all_denies() -> None:
    assert authorize([], operation="read", at=_NOW) == Decision(permitted=False, reason=NO_GRANT)


def test_an_operation_the_grant_does_not_name_is_denied() -> None:
    """Silence is not permission: the author did not say yes to this."""
    decision = authorize([_grant(allowed_operations=["read"])], operation="write", at=_NOW)

    assert not decision.permitted
    assert decision.reason == OPERATION_NOT_GRANTED


def test_an_empty_operation_list_permits_nothing() -> None:
    """The reading that turns a half-filled grant into an unlimited one."""
    decision = authorize([_grant(allowed_operations=[])], operation="read", at=_NOW)

    assert not decision.permitted


def test_an_empty_profile_type_list_reaches_no_type() -> None:
    decision = authorize([_grant(profile_types=[])], operation="read", at=_NOW, profile_type="core:capability")

    assert not decision.permitted
    assert decision.reason == TYPE_NOT_GRANTED


def test_a_type_outside_the_grant_is_denied() -> None:
    decision = authorize(
        [_grant(profile_types=["core:capability"])], operation="read", at=_NOW, profile_type="core:secret"
    )

    assert not decision.permitted
    assert decision.reason == TYPE_NOT_GRANTED


def test_a_relationship_type_outside_the_grant_is_denied() -> None:
    decision = authorize([_grant()], operation="read", at=_NOW, relationship_type="core:reads_from")

    assert not decision.permitted


# --- state and time -------------------------------------------------------------------


def test_a_proposed_grant_permits_nothing() -> None:
    """Proposing and approving are two acts; a proposal that permitted things would merge them."""
    decision = authorize([_grant(grant_state=grant_writer.PROPOSED)], operation="read", at=_NOW)

    assert not decision.permitted
    assert decision.reason == NO_GRANT


def test_a_revoked_grant_stops_permitting_immediately() -> None:
    """Immediate at the decision, because the decision re-reads rather than caching."""
    decision = authorize([_grant(grant_state=grant_writer.REVOKED, revoked_at=_NOW)], operation="read", at=_NOW)

    assert not decision.permitted
    assert decision.reason == NO_GRANT


def test_a_grant_whose_window_has_closed_permits_nothing() -> None:
    """An active state inside a closed window is still nothing."""
    decision = authorize([_grant(effective_to=_NOW - datetime.timedelta(hours=1))], operation="read", at=_NOW)

    assert not decision.permitted


def test_a_grant_that_has_not_started_permits_nothing() -> None:
    decision = authorize([_grant(effective_from=_NOW + datetime.timedelta(hours=1))], operation="read", at=_NOW)

    assert not decision.permitted


# --- classification is a ceiling, not a filter -----------------------------------------


def test_content_above_the_ceiling_is_refused_rather_than_redacted() -> None:
    decision = authorize(
        [_grant(classification_ceiling="internal")],
        operation="read",
        at=_NOW,
        classification="restricted",
    )

    assert not decision.permitted
    assert decision.reason == CLASSIFICATION_ABOVE_CEILING


def test_content_at_the_ceiling_is_permitted() -> None:
    decision = authorize(
        [_grant(classification_ceiling="internal")], operation="read", at=_NOW, classification="internal"
    )

    assert decision.permitted


def test_an_unknown_classification_is_refused_rather_than_ranked() -> None:
    """Treating an unrecognised value as lowest would let a typo carry restricted content."""
    decision = authorize([_grant()], operation="read", at=_NOW, classification="probably-fine")

    assert not decision.permitted
    assert decision.reason == CLASSIFICATION_ABOVE_CEILING


# --- the positives that keep the negatives honest --------------------------------------


def test_a_grant_that_names_everything_permits_the_operation() -> None:
    """Without this, a function refusing everything would pass every test above."""
    decision = authorize(
        [_grant()],
        operation="read",
        at=_NOW,
        profile_type="core:capability",
        relationship_type="core:depends_on",
        classification="internal",
    )

    assert decision.permitted
    assert decision.reason is None
    assert decision.grant_id is not None


def test_two_narrow_grants_together_permit_what_neither_does_alone() -> None:
    """Grants are additive, so an organization can add rather than revoke and re-issue."""
    read_only = _grant(allowed_operations=["read"], profile_types=["core:capability"])
    write_only = _grant(allowed_operations=["write"], profile_types=["core:capability"])

    decision = authorize([read_only, write_only], operation="write", at=_NOW, profile_type="core:capability")

    assert decision.permitted


# --- a denial reveals nothing ----------------------------------------------------------


def test_a_denial_carries_no_grant_identifier() -> None:
    """Naming the grant that failed would confirm to the caller that one exists."""
    with pytest.raises(ValueError, match="reveals that one exists"):
        Decision(permitted=False, reason=NO_GRANT, grant_id=uuid.uuid4())


def test_every_denial_reason_is_from_the_closed_set() -> None:
    """A free-text reason is where a helpful message becomes an enumeration oracle."""
    with pytest.raises(ValueError, match="unknown denial reason"):
        Decision(permitted=False, reason="entity 42 belongs to acme and is not shared")


def test_no_denial_reason_distinguishes_absent_from_unshared() -> None:
    """The four reasons all describe the *grant*, never the thing being reached for.

    Asserted over the closed set rather than by inspecting messages, so a reason
    added later has to be checked against this rule rather than slipping in.
    """
    for reason in DENIAL_REASONS:
        assert "not_found" not in reason
        assert "missing" not in reason
        assert "exists" not in reason


def test_a_permitted_decision_carries_no_reason() -> None:
    with pytest.raises(ValueError, match="carries no denial reason"):
        Decision(permitted=True, reason=NO_GRANT)


# --- the single writer -----------------------------------------------------------------


def test_cross_org_grants_is_governed_with_one_permitted_writer() -> None:
    """No behavioural test can show nothing *else* writes the table.

    A second writer produces rows indistinguishable from approved ones while
    carrying none of the approval evidence, so this is asserted against the same
    rule set `make privileged-writes` enforces.
    """
    rule = next((r for r in RULES if r.table == "cross_org_grants"), None)

    assert rule is not None, "cross_org_grants must be a governed table"
    assert rule.allowed_callers == frozenset({"contextplane/sharing/grants.py"})


def test_the_grant_writer_cannot_create_a_grant_already_active() -> None:
    """Proposing and approving stay two acts, which is what the evidence records.

    Checked on the module surface: `propose` writes the proposed state and there
    is no parameter through which a caller can ask for another.
    """
    import inspect

    signature = inspect.signature(grant_writer.propose)

    assert "grant_state" not in signature.parameters
    assert "approval_evidence" not in signature.parameters
