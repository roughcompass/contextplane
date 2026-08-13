"""Applicability matching and precedence ordering."""

from __future__ import annotations

import datetime
import uuid

from contextplane.arc.service.selection import (
    ApprovedException,
    ScopedDirective,
    apply_exceptions,
    collapse_successors,
    order_by_precedence,
    rule_applies,
)
from contextplane.arc.types import (
    ActionClass,
    ApplicabilityRule,
    AuthorityScope,
    ConflictSubjectKey,
    Directive,
    DirectiveType,
    IntentKind,
    IntentManifest,
    NormalizedConstraint,
)

_T1 = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
_T2 = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000002")
_CAP = uuid.UUID("cccccccc-0000-4000-8000-000000000001")
_NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)


def _manifest(**over: object) -> IntentManifest:
    fields: dict[str, object] = {
        "session_id": "s1",
        "intent_kind": IntentKind.CODE_CHANGE,
        "requested_action_classes": frozenset({ActionClass.MERGE}),
        "capability_ids": frozenset({_CAP}),
        "domain_ids": frozenset({"payments"}),
        "environment": "production",
        "data_sensitivity": "confidential",
    }
    fields.update(over)
    return IntentManifest(**fields)  # type: ignore[arg-type]


def _rule(scope: AuthorityScope = AuthorityScope.GLOBAL, **over: object) -> ApplicabilityRule:
    fields: dict[str, object] = {"rule_id": uuid.uuid4(), "revision_id": uuid.uuid4(), "scope": scope}
    fields.update(over)
    return ApplicabilityRule(**fields)  # type: ignore[arg-type]


def _subject(**over: str) -> ConflictSubjectKey:
    fields = {
        "schema_version": "arc_conflict_v1",
        "namespace": "n",
        "subject_selector": "s",
        "operation": "o",
        "action_class": "deploy",
        "target_selector": "t",
    }
    fields.update(over)
    return ConflictSubjectKey(**fields)  # type: ignore[arg-type]


def _directive(did: uuid.UUID, rid: uuid.UUID, *, delegable: bool = False, value: str = "approved") -> Directive:
    return Directive(
        directive_id=did,
        revision_id=rid,
        directive_type=DirectiveType.REQUIRE,
        source_anchor="a#1",
        conflict_subject=_subject(),
        constraint=NormalizedConstraint.parse("require", "equals", value),
        delegable_exception=delegable,
    )


# --- matching --------------------------------------------------------------


def test_a_global_rule_with_no_selectors_matches() -> None:
    """Empty selector means no constraint on that dimension."""
    assert rule_applies(_rule(), _manifest(), tenant_id=_T1, as_of=_NOW) is True


def test_task_kind_must_match_when_named() -> None:
    r = _rule(intent_kinds=frozenset({IntentKind.DEPLOYMENT}))
    assert rule_applies(r, _manifest(), tenant_id=_T1, as_of=_NOW) is False
    assert rule_applies(r, _manifest(intent_kind=IntentKind.DEPLOYMENT), tenant_id=_T1, as_of=_NOW) is True


def test_action_classes_match_on_overlap_not_equality() -> None:
    """A deploy obligation is still owed when the manifest also requests merge."""
    r = _rule(action_classes=frozenset({ActionClass.DEPLOY}))
    both = _manifest(requested_action_classes=frozenset({ActionClass.MERGE, ActionClass.DEPLOY}))
    assert rule_applies(r, both, tenant_id=_T1, as_of=_NOW) is True


def test_capability_scope_matches_on_id_overlap() -> None:
    r = _rule(AuthorityScope.CAPABILITY, capability_ids=frozenset({_CAP}))
    assert rule_applies(r, _manifest(), tenant_id=_T1, as_of=_NOW) is True
    other = _manifest(capability_ids=frozenset({uuid.uuid4()}))
    assert rule_applies(r, other, tenant_id=_T1, as_of=_NOW) is False


def test_a_tenant_scoped_rule_only_applies_to_its_own_tenant() -> None:
    """A rule owned by one tenant must never reach another's request."""
    r = _rule(AuthorityScope.TENANT, target_tenant_id=_T1)
    assert rule_applies(r, _manifest(), tenant_id=_T1, as_of=_NOW) is True
    assert rule_applies(r, _manifest(), tenant_id=_T2, as_of=_NOW) is False


def test_environment_and_sensitivity_must_match_when_named() -> None:
    assert rule_applies(_rule(environments=frozenset({"staging"})), _manifest(), tenant_id=_T1, as_of=_NOW) is False
    assert (
        rule_applies(_rule(data_sensitivity_tiers=frozenset({"public"})), _manifest(), tenant_id=_T1, as_of=_NOW)
        is False
    )


def test_a_named_dimension_absent_from_the_manifest_does_not_match() -> None:
    """A rule for production must not apply to a manifest that names no environment."""
    r = _rule(environments=frozenset({"production"}))
    assert rule_applies(r, _manifest(environment=None), tenant_id=_T1, as_of=_NOW) is False


def test_effective_window_is_evaluated_at_as_of_not_now() -> None:
    """Replaying a receipt months later must reach the same answer."""
    r = _rule(effective_from=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC))
    assert rule_applies(r, _manifest(), tenant_id=_T1, as_of=_NOW) is False
    later = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
    assert rule_applies(r, _manifest(), tenant_id=_T1, as_of=later) is True


def test_an_expired_rule_does_not_apply() -> None:
    r = _rule(effective_until=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    assert rule_applies(r, _manifest(), tenant_id=_T1, as_of=_NOW) is False


# --- precedence ------------------------------------------------------------


def _scoped(scope: AuthorityScope, effective: datetime.datetime, did: uuid.UUID) -> ScopedDirective:
    rid = uuid.uuid4()
    rule = _rule(
        scope,
        target_tenant_id=_T1 if scope is AuthorityScope.TENANT else None,
        capability_ids=frozenset({_CAP}) if scope is AuthorityScope.CAPABILITY else frozenset(),
    )
    return ScopedDirective(directive=_directive(did, rid), rule=rule, revision_effective_from=effective)


def test_precedence_orders_global_before_narrower_scopes() -> None:
    items = [
        _scoped(AuthorityScope.INTENT, _NOW, uuid.uuid4()),
        _scoped(AuthorityScope.GLOBAL, _NOW, uuid.uuid4()),
        _scoped(AuthorityScope.TENANT, _NOW, uuid.uuid4()),
    ]
    assert [s.scope for s in order_by_precedence(items)] == [
        AuthorityScope.GLOBAL,
        AuthorityScope.TENANT,
        AuthorityScope.INTENT,
    ]


def test_within_a_scope_earlier_revisions_come_first() -> None:
    early = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    late = datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC)
    items = [_scoped(AuthorityScope.GLOBAL, late, uuid.uuid4()), _scoped(AuthorityScope.GLOBAL, early, uuid.uuid4())]
    assert [s.revision_effective_from for s in order_by_precedence(items)] == [early, late]


def test_directive_id_is_the_final_tiebreak() -> None:
    """Without it, equal scope and timestamp would order by input sequence and the
    same manifest could produce two different receipts."""
    a = uuid.UUID("11111111-0000-4000-8000-000000000001")
    b = uuid.UUID("22222222-0000-4000-8000-000000000002")
    forward = order_by_precedence([_scoped(AuthorityScope.GLOBAL, _NOW, b), _scoped(AuthorityScope.GLOBAL, _NOW, a)])
    reverse = order_by_precedence([_scoped(AuthorityScope.GLOBAL, _NOW, a), _scoped(AuthorityScope.GLOBAL, _NOW, b)])
    assert [s.directive.directive_id for s in forward] == [a, b]
    assert [s.directive.directive_id for s in reverse] == [a, b]


# --- successor collapse ----------------------------------------------------


def test_a_successor_revision_replaces_its_predecessor() -> None:
    """Otherwise both texts enter the bundle and then conflict with each other."""
    did = uuid.uuid4()
    old = ScopedDirective(
        directive=_directive(did, uuid.uuid4(), value="old"),
        rule=_rule(),
        revision_effective_from=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    new = ScopedDirective(
        directive=_directive(did, uuid.uuid4(), value="new"),
        rule=_rule(),
        revision_effective_from=datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC),
    )
    collapsed = collapse_successors([old, new])
    assert len(collapsed) == 1
    assert collapsed[0].directive.constraint is not None
    assert collapsed[0].directive.constraint.values == frozenset({"new"})


def test_distinct_identities_both_survive_collapse() -> None:
    a = ScopedDirective(directive=_directive(uuid.uuid4(), uuid.uuid4()), rule=_rule(), revision_effective_from=_NOW)
    b = ScopedDirective(directive=_directive(uuid.uuid4(), uuid.uuid4()), rule=_rule(), revision_effective_from=_NOW)
    assert len(collapse_successors([a, b])) == 2


# --- exceptions ------------------------------------------------------------


def _exception(did: uuid.UUID, rid: uuid.UUID, **over: object) -> ApprovedException:
    fields: dict[str, object] = {
        "exception_id": uuid.uuid4(),
        "higher_scope_directive_id": did,
        "higher_scope_revision_id": rid,
        "lower_scope_tenant_id": _T1,
        "replacement_constraint": NormalizedConstraint.parse("require", "equals", "relaxed"),
    }
    fields.update(over)
    return ApprovedException(**fields)  # type: ignore[arg-type]


def test_an_exception_applies_only_to_a_delegable_directive() -> None:
    """A stale approval must not keep weakening a control nobody may weaken."""
    did, rid = uuid.uuid4(), uuid.uuid4()
    non_delegable = ScopedDirective(
        directive=_directive(did, rid, delegable=False), rule=_rule(), revision_effective_from=_NOW
    )
    result, used = apply_exceptions([non_delegable], [_exception(did, rid)], tenant_id=_T1, as_of=_NOW)
    assert used == []
    assert result[0].directive.constraint is not None
    assert result[0].directive.constraint.values == frozenset({"approved"})


def test_an_exception_replaces_a_delegable_directives_constraint() -> None:
    did, rid = uuid.uuid4(), uuid.uuid4()
    delegable = ScopedDirective(
        directive=_directive(did, rid, delegable=True), rule=_rule(), revision_effective_from=_NOW
    )
    exc = _exception(did, rid)
    result, used = apply_exceptions([delegable], [exc], tenant_id=_T1, as_of=_NOW)
    assert used == [exc.exception_id]
    assert result[0].directive.constraint is not None
    assert result[0].directive.constraint.values == frozenset({"relaxed"})


def test_another_tenants_exception_does_not_apply() -> None:
    did, rid = uuid.uuid4(), uuid.uuid4()
    delegable = ScopedDirective(
        directive=_directive(did, rid, delegable=True), rule=_rule(), revision_effective_from=_NOW
    )
    exc = _exception(did, rid, lower_scope_tenant_id=_T2)
    _, used = apply_exceptions([delegable], [exc], tenant_id=_T1, as_of=_NOW)
    assert used == []


def test_a_revoked_exception_does_not_apply() -> None:
    did, rid = uuid.uuid4(), uuid.uuid4()
    delegable = ScopedDirective(
        directive=_directive(did, rid, delegable=True), rule=_rule(), revision_effective_from=_NOW
    )
    exc = _exception(did, rid, revoked_at=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC))
    _, used = apply_exceptions([delegable], [exc], tenant_id=_T1, as_of=_NOW)
    assert used == []


def test_a_time_bounded_exception_is_evaluated_at_as_of() -> None:
    did, rid = uuid.uuid4(), uuid.uuid4()
    delegable = ScopedDirective(
        directive=_directive(did, rid, delegable=True), rule=_rule(), revision_effective_from=_NOW
    )
    exc = _exception(did, rid, effective_until=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC))
    _, used = apply_exceptions([delegable], [exc], tenant_id=_T1, as_of=_NOW)
    assert used == []
    earlier = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)
    _, used_earlier = apply_exceptions([delegable], [exc], tenant_id=_T1, as_of=earlier)
    assert used_earlier == [exc.exception_id]


def test_an_exception_naming_a_different_revision_does_not_apply() -> None:
    """It excepts one exact projection, not the whole directive family."""
    did, rid = uuid.uuid4(), uuid.uuid4()
    delegable = ScopedDirective(
        directive=_directive(did, rid, delegable=True), rule=_rule(), revision_effective_from=_NOW
    )
    exc = _exception(did, uuid.uuid4())
    _, used = apply_exceptions([delegable], [exc], tenant_id=_T1, as_of=_NOW)
    assert used == []
