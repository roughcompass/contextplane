"""Directive and rule domain types: closed vocabularies, normalized constraints."""

from __future__ import annotations

import uuid

import pytest

from registry.arc.types import (
    ActionClass,
    ApplicabilityRule,
    ArcVocabularyError,
    AuthorityScope,
    ConflictSubjectKey,
    ConstraintOperator,
    Directive,
    DirectiveType,
    NormalizedConstraint,
    SatisfactionMode,
    TaskKind,
    parse_action_class,
    parse_task_kind,
)

_D = uuid.UUID("11111111-1111-1111-1111-111111111111")
_R = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _subject(**over: str) -> ConflictSubjectKey:
    fields = {
        "schema_version": "arc_conflict_v1",
        "namespace": "deploy",
        "subject_selector": "service:payments",
        "operation": "release",
        "action_class": "deploy",
        "target_selector": "env:production",
    }
    fields.update(over)
    return ConflictSubjectKey(**fields)  # type: ignore[arg-type]


def _constraint(op: str = "equals", value: str | None = "approved") -> NormalizedConstraint:
    return NormalizedConstraint.parse("require", op, value)


# --- closed vocabularies ---------------------------------------------------


def test_the_closed_vocabularies_have_the_specified_members() -> None:
    assert len(list(TaskKind)) == 7
    assert len(list(ActionClass)) == 5
    assert len(list(AuthorityScope)) == 5


def test_an_unknown_task_kind_is_rejected() -> None:
    """A host naming a lower-risk kind must not escape a matching obligation."""
    with pytest.raises(ArcVocabularyError, match="unknown task kind"):
        parse_task_kind("mostly_harmless")


def test_an_unknown_action_class_is_rejected() -> None:
    with pytest.raises(ArcVocabularyError, match="unknown action class"):
        parse_action_class("gentle_deploy")


def test_authority_scope_ranks_widest_to_narrowest() -> None:
    """Precedence depends on this order, so it is asserted rather than assumed."""
    ranks = [
        s.rank
        for s in (
            AuthorityScope.GLOBAL,
            AuthorityScope.TENANT,
            AuthorityScope.DOMAIN,
            AuthorityScope.CAPABILITY,
            AuthorityScope.TASK,
        )
    ]
    assert ranks == sorted(ranks) == [0, 1, 2, 3, 4]


def test_only_citation_only_is_non_action_protecting() -> None:
    for kind in DirectiveType:
        expected = kind is not DirectiveType.CITATION_ONLY
        assert kind.is_action_protecting is expected


# --- conflict subject key --------------------------------------------------


def test_subject_key_identity_is_the_canonical_tuple_not_the_digest() -> None:
    """A digest may index the subject; it must never define it."""
    a = _subject()
    assert a.canonical_tuple() == (
        "arc_conflict_v1",
        "deploy",
        "service:payments",
        "release",
        "deploy",
        "env:production",
    )
    assert len(a.digest()) == 64


def test_equal_subjects_share_a_digest_and_differing_ones_do_not() -> None:
    assert _subject().digest() == _subject().digest()
    assert _subject().digest() != _subject(operation="rollback").digest()


def test_field_boundaries_cannot_be_shifted_to_forge_a_matching_digest() -> None:
    """Length-prefixed: moving a character across a field boundary must not collide.

    Two directives colliding on digest but differing in subject would be compared
    as though they governed the same thing.
    """
    a = _subject(namespace="ab", subject_selector="c")
    b = _subject(namespace="a", subject_selector="bc")
    assert a.digest() != b.digest()


def test_subject_keys_are_orderable_for_deterministic_lock_ordering() -> None:
    """Activation locks conflict domains in a canonical order to avoid deadlock."""
    keys = [_subject(operation="b"), _subject(operation="a")]
    assert [k.operation for k in sorted(keys)] == ["a", "b"]


# --- constraint normalization ---------------------------------------------


def test_equals_normalizes_to_a_single_member_set() -> None:
    c = _constraint("equals", "approved")
    assert c.operator is ConstraintOperator.EQUALS
    assert c.values == frozenset({"approved"})


def test_in_set_ignores_member_order_and_whitespace() -> None:
    """ "a, b" and "b,a" are the same constraint and must compare equal."""
    assert NormalizedConstraint.parse("require", "in_set", "a, b") == (
        NormalizedConstraint.parse("require", "in_set", "b,a")
    )


def test_present_takes_no_value() -> None:
    c = NormalizedConstraint.parse("require", "present", None)
    assert c.values == frozenset()


def test_present_with_a_value_is_rejected() -> None:
    """Accepting and discarding it would silently change the constraint's meaning."""
    with pytest.raises(ArcVocabularyError, match="takes no value"):
        NormalizedConstraint.parse("require", "present", "something")


@pytest.mark.parametrize("op", ["equals", "in_set", "not_in_set"])
def test_a_value_requiring_operator_rejects_an_empty_value(op: str) -> None:
    with pytest.raises(ArcVocabularyError, match="requires a"):
        NormalizedConstraint.parse("require", op, "")


def test_a_set_operator_rejects_a_value_that_normalizes_to_nothing() -> None:
    with pytest.raises(ArcVocabularyError, match="at least one member"):
        NormalizedConstraint.parse("require", "in_set", " , , ")


def test_bad_modality_or_operator_is_rejected() -> None:
    with pytest.raises(ArcVocabularyError):
        NormalizedConstraint.parse("encourage", "equals", "x")
    with pytest.raises(ArcVocabularyError):
        NormalizedConstraint.parse("require", "resembles", "x")


def test_every_operator_normalizes_to_a_set_so_intersection_is_one_path() -> None:
    for op, value in (("equals", "a"), ("in_set", "a,b"), ("not_in_set", "c"), ("present", None)):
        assert isinstance(NormalizedConstraint.parse("require", op, value).values, frozenset)


# --- directive invariants --------------------------------------------------


def test_an_action_protecting_directive_without_the_comparable_shape_is_rejected() -> None:
    """Otherwise it would look enforceable while having nothing to enforce."""
    with pytest.raises(ArcVocabularyError, match="citation_only"):
        Directive(
            directive_id=_D,
            revision_id=_R,
            directive_type=DirectiveType.REQUIRE,
            source_anchor="a#1",
        )


def test_citation_only_may_omit_the_comparable_shape() -> None:
    d = Directive(
        directive_id=_D,
        revision_id=_R,
        directive_type=DirectiveType.CITATION_ONLY,
        source_anchor="a#1",
    )
    assert d.is_enforceable is False


def test_signed_result_without_accepted_verifiers_is_rejected() -> None:
    """Nothing could ever satisfy it, so it would block the action forever."""
    with pytest.raises(ArcVocabularyError, match="nothing could ever satisfy"):
        Directive(
            directive_id=_D,
            revision_id=_R,
            directive_type=DirectiveType.VERIFY,
            source_anchor="a#1",
            conflict_subject=_subject(),
            constraint=_constraint(),
            satisfaction_mode=SatisfactionMode.SIGNED_RESULT,
        )


def test_signed_result_with_verifiers_and_evidence_type_is_accepted() -> None:
    d = Directive(
        directive_id=_D,
        revision_id=_R,
        directive_type=DirectiveType.VERIFY,
        source_anchor="a#1",
        conflict_subject=_subject(),
        constraint=_constraint(),
        satisfaction_mode=SatisfactionMode.SIGNED_RESULT,
        accepted_verifier_classes=frozenset({"registered_gateway"}),
        required_evidence_type="scan_result",
    )
    assert d.is_enforceable is True


def test_directives_are_frozen() -> None:
    d = Directive(
        directive_id=_D,
        revision_id=_R,
        directive_type=DirectiveType.CITATION_ONLY,
        source_anchor="a#1",
    )
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        d.source_anchor = "changed"  # type: ignore[misc]


# --- rule invariants -------------------------------------------------------


def test_a_tenant_scoped_rule_must_name_its_target_tenant() -> None:
    """Without it the rule would match every tenant, which is a leak not a default."""
    with pytest.raises(ArcVocabularyError, match="names no target tenant"):
        ApplicabilityRule(rule_id=_D, revision_id=_R, scope=AuthorityScope.TENANT)


def test_a_capability_scoped_rule_must_name_a_capability() -> None:
    with pytest.raises(ArcVocabularyError, match="names no capability"):
        ApplicabilityRule(rule_id=_D, revision_id=_R, scope=AuthorityScope.CAPABILITY)


def test_a_capability_rule_may_use_labels_instead_of_ids() -> None:
    rule = ApplicabilityRule(
        rule_id=_D,
        revision_id=_R,
        scope=AuthorityScope.CAPABILITY,
        capability_labels=frozenset({"payments"}),
    )
    assert rule.capability_labels == frozenset({"payments"})


def test_global_and_task_scopes_need_no_selector() -> None:
    for scope in (AuthorityScope.GLOBAL, AuthorityScope.DOMAIN, AuthorityScope.TASK):
        assert ApplicabilityRule(rule_id=_D, revision_id=_R, scope=scope).scope is scope


def test_rules_differing_only_in_selector_order_are_equal() -> None:
    """Frozensets, so ordering cannot make two identical rules compare different."""
    a = ApplicabilityRule(rule_id=_D, revision_id=_R, scope=AuthorityScope.GLOBAL, domain_ids=frozenset({"x", "y"}))
    b = ApplicabilityRule(rule_id=_D, revision_id=_R, scope=AuthorityScope.GLOBAL, domain_ids=frozenset({"y", "x"}))
    assert a == b
