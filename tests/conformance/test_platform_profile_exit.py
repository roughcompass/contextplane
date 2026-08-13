"""The exit criteria for profile governance, asserted as one suite.

Every property this phase claims is enforced somewhere — in a service, a gate, a
schema constraint. This file is the place that asserts they are all enforced *at
once*, against one corpus, because a set of individually-green suites can still
describe a system whose parts disagree: a write path that refuses what a read path
returns, a grant model the derivative scope does not consult, a migration gate
that blocks activation while a separate path activates anyway.

**The corpus is two tenants, because one tenant cannot exhibit the failures.**
Isolation, cross-organization grants, and derivative scoping are all properties of
a *pair*. A single-tenant corpus produces a suite that passes without ever
constructing the situation the phase exists to make safe.

**Every criterion is named, and the list is closed.** `_EXIT_CRITERIA` is checked
against the tests that claim it, so a criterion with no test fails here rather
than being quietly dropped — which is the failure mode of an exit checklist kept
in a document.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from contextplane.entities.provenance import AUTHORITIES, AssertionProvenance, IncompleteProvenance
from contextplane.entities.write_intent import (
    AUTHORITY_OBSERVED_EVIDENCE,
    INTENT_AUTHORIZED_APPROVAL,
    INTENT_OBSERVATION,
    PROFILE_WRITE_INTENTS,
    ProfileWriteAuthority,
    RefusedProfileWrite,
    effect_of,
    route_profile_write,
)
from contextplane.profile.migration import (
    COLLISION,
    Finding,
    MigrationPlan,
    MigrationRefused,
    empty_inventory,
)
from contextplane.relationships import readiness
from contextplane.relationships.definitions import RelationshipConstraints, RelationshipProjectionError
from contextplane.sharing import grants as grant_writer
from contextplane.sharing.authorization import authorize
from contextplane.sharing.derivatives import StaleDerivative, assert_fresh, scope_for

_NOW = datetime.datetime(2026, 8, 13, 12, 0, tzinfo=datetime.UTC)

#: Two tenants, because isolation, grants and derivative scoping are properties of
#: a pair and a single-tenant corpus cannot exhibit their failures.
_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

#: Every criterion this phase exits on. Closed, and checked against the tests that
#: claim it — a criterion with no test is the failure mode of a checklist kept in
#: a document, and it fails here instead.
_EXIT_CRITERIA: frozenset[str] = frozenset(
    {
        "intent_routing",
        "no_caller_asserted_authority",
        "provenance_completeness",
        "relationship_cardinality",
        "readiness_gating",
        "cross_org_default_deny",
        "derivative_scoping",
        "migration_blocking",
    }
)


def _criterion(name: str) -> pytest.MarkDecorator:
    """Tag a test with the criterion it discharges, so the coverage test can check."""
    return pytest.mark.criterion(name)


def _grant(**overrides: object) -> grant_writer.CrossOrgGrant:
    fields: dict[str, object] = {
        "grant_id": uuid.uuid4(),
        "source_tenant_id": _TENANT_A,
        "destination_tenant_id": _TENANT_B,
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


# --- the criteria ---------------------------------------------------------------------


@_criterion("intent_routing")
def test_an_ordinary_agent_cannot_reach_the_canonical_path() -> None:
    """The safety property the whole generic surface rests on."""
    observation = route_profile_write(
        INTENT_OBSERVATION,
        authority=ProfileWriteAuthority(actor_id="agent-1", origin=AUTHORITY_OBSERVED_EVIDENCE),
    )

    assert observation.effect == effect_of(INTENT_OBSERVATION)
    assert observation.effect != effect_of(INTENT_AUTHORIZED_APPROVAL)

    with pytest.raises(RefusedProfileWrite):
        route_profile_write(
            INTENT_AUTHORIZED_APPROVAL,
            authority=ProfileWriteAuthority(actor_id="agent-1", origin=AUTHORITY_OBSERVED_EVIDENCE),
            approval_reference="borrowed",
        )


@_criterion("intent_routing")
def test_intent_has_no_default_on_any_surface() -> None:
    with pytest.raises(RefusedProfileWrite):
        route_profile_write(
            None, authority=ProfileWriteAuthority(actor_id="agent-1", origin=AUTHORITY_OBSERVED_EVIDENCE)
        )
    assert set(PROFILE_WRITE_INTENTS) == {"observation", "request", "authorized_approval"}


@_criterion("no_caller_asserted_authority")
def test_a_caller_cannot_name_its_own_authority() -> None:
    from contextplane.entities.write_intent import refuse_caller_asserted_authority

    with pytest.raises(RefusedProfileWrite):
        refuse_caller_asserted_authority({"authority": "canonical_owner"})


@_criterion("provenance_completeness")
@pytest.mark.parametrize("authority", sorted(AUTHORITIES))
def test_every_authority_requires_its_own_complete_provenance(authority: str) -> None:
    """A governed assertion names the revision that validated it, whatever asserted it."""
    with pytest.raises(IncompleteProvenance):
        AssertionProvenance(
            tenant_id=_TENANT_A,
            source_system="corpus",
            source_namespace="internal",
            ingested_at=_NOW,
            authority=authority,
            freshness_state="fresh",
            produced_by="corpus",
            validating_profile_revision_id=None,  # type: ignore[arg-type]
        )


@_criterion("relationship_cardinality")
def test_a_relationship_definition_without_a_stated_policy_cannot_be_projected() -> None:
    """Default-deny means a denial somebody wrote down, not one a reader assumed."""
    canonical = {
        "namespace": "core",
        "type_name": "depends_on",
        "source_type": "core:capability",
        "destination_type": "core:capability",
        "direction": "directed",
        "cardinality_scope": "per_source",
        "authority": "canonical_owner",
        "min_cardinality": 0,
        "max_cardinality": None,
        "duplicate_policy": "reject",
        "symmetry": "asymmetric",
        "inverse_view": "read_only",
        "properties": [],
    }

    with pytest.raises(RelationshipProjectionError, match="cross_org_policy"):
        RelationshipConstraints.from_canonical(canonical)


@_criterion("readiness_gating")
def test_a_draft_may_exist_below_its_minimum_and_blocks_activation() -> None:
    """Refusing the write instead would make an entity requiring two impossible to build."""
    below = readiness.readiness_for(observed=1, minimum=2)
    met = readiness.readiness_for(observed=2, minimum=2)

    assert below == readiness.DRAFT
    assert met == readiness.READY
    assert readiness.blocks_activation(below)
    assert not readiness.blocks_activation(met)


@_criterion("cross_org_default_deny")
def test_no_grant_permits_nothing_across_the_tenant_pair() -> None:
    assert not authorize([], operation="read", at=_NOW).permitted
    assert not authorize([_grant(grant_state=grant_writer.PROPOSED)], operation="read", at=_NOW).permitted
    assert not authorize([_grant(allowed_operations=[])], operation="read", at=_NOW).permitted


@_criterion("cross_org_default_deny")
def test_a_complete_grant_permits_the_operation_it_names() -> None:
    """The denials above would all pass for a function that refused everything."""
    decision = authorize(
        [_grant()], operation="read", at=_NOW, profile_type="core:capability", classification="internal"
    )

    assert decision.permitted


@_criterion("derivative_scoping")
def test_revoking_a_grant_makes_its_derivatives_unreachable_at_once() -> None:
    """The scope key changes; no purge has to complete first."""
    before = scope_for(source_tenant_id=_TENANT_A, destination_tenant_id=_TENANT_B, grants=[_grant()], at=_NOW)
    after = scope_for(
        source_tenant_id=_TENANT_A,
        destination_tenant_id=_TENANT_B,
        grants=[_grant(grant_state=grant_writer.REVOKED, revoked_at=_NOW)],
        at=_NOW,
    )

    assert before != after
    with pytest.raises(StaleDerivative):
        assert_fresh(before, after)


@_criterion("derivative_scoping")
def test_a_derivative_built_under_the_current_grants_is_served() -> None:
    scope = scope_for(source_tenant_id=_TENANT_A, destination_tenant_id=_TENANT_B, grants=[_grant()], at=_NOW)

    assert_fresh(scope, scope)


@_criterion("migration_blocking")
def test_an_unresolved_finding_blocks_activation() -> None:
    plan = MigrationPlan(
        inventory=empty_inventory(),
        findings=[Finding(kind=COLLISION, subject="core:capability/x", detail="claimed twice")],
    )

    with pytest.raises(MigrationRefused):
        plan.assert_may_activate(_NOW)


@_criterion("migration_blocking")
def test_a_clean_plan_may_activate() -> None:
    """Without this, a plan that blocked everything would satisfy the criterion above."""
    MigrationPlan(inventory=empty_inventory(), findings=()).assert_may_activate(_NOW)


# --- the checklist cannot lose an entry -----------------------------------------------


def test_every_exit_criterion_is_discharged_by_at_least_one_test(request: pytest.FixtureRequest) -> None:
    """A criterion with no test fails here rather than being quietly dropped.

    This is the difference between an exit checklist and an exit *gate*: the
    checklist in a document goes stale silently, and this does not.
    """
    session = request.session
    claimed: set[str] = set()
    for item in session.items:
        for mark in item.iter_markers(name="criterion"):
            claimed.update(mark.args)

    undischarged = sorted(_EXIT_CRITERIA - claimed)
    assert not undischarged, f"these exit criteria have no test claiming them: {undischarged}"


def test_no_test_claims_a_criterion_the_phase_does_not_define(request: pytest.FixtureRequest) -> None:
    """A claim against an undefined criterion reads as coverage and is not."""
    claimed: set[str] = set()
    for item in request.session.items:
        for mark in item.iter_markers(name="criterion"):
            claimed.update(mark.args)

    undefined = sorted(claimed - _EXIT_CRITERIA)
    assert not undefined, f"these tests claim criteria the phase does not define: {undefined}"
