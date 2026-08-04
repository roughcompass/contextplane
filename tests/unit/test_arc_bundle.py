"""Bundle assembly and budget enforcement.

The assertion that matters most: a result cannot become ready by dropping a
mandatory item.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from registry.arc.service.bundle import (
    BLOCKED_BUDGET_EXCEEDED,
    CAP_FACTS_BUDGET_BYTES,
    OMITTED_CAP_FACTS_OVER_BUDGET,
    OMITTED_CAP_FACTS_UNAVAILABLE,
    CapFact,
    assemble,
)
from registry.arc.service.selection import (
    ConflictFinding,
    ScopedDirective,
    SelectionResult,
)
from registry.arc.types import (
    ApplicabilityRule,
    AuthorityScope,
    ConflictSubjectKey,
    Directive,
    DirectiveType,
    NormalizedConstraint,
    ResolutionStatus,
)

_NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
_BIG = 1_000_000


def _scoped(anchor: str = "a#1", *, mandatory: bool = True) -> ScopedDirective:
    rid = uuid.uuid4()
    directive = Directive(
        directive_id=uuid.uuid4(),
        revision_id=rid,
        directive_type=DirectiveType.REQUIRE,
        source_anchor=anchor,
        conflict_subject=ConflictSubjectKey(
            schema_version="arc_conflict_v1",
            namespace="n",
            subject_selector="s",
            operation="o",
            action_class="merge",
            target_selector="t",
        ),
        constraint=NormalizedConstraint.parse("require", "equals", "approved"),
    )
    rule = ApplicabilityRule(rule_id=uuid.uuid4(), revision_id=rid, scope=AuthorityScope.GLOBAL, is_mandatory=mandatory)
    return ScopedDirective(directive=directive, rule=rule, revision_effective_from=_NOW)


def _selection(
    *,
    mandatory: tuple[ScopedDirective, ...] = (),
    optional: tuple[ScopedDirective, ...] = (),
    status: ResolutionStatus = ResolutionStatus.READY,
    blocked: tuple[str, ...] = (),
    degraded: tuple[str, ...] = (),
    conflicts: tuple[ConflictFinding, ...] = (),
) -> SelectionResult:
    return SelectionResult(
        status=status,
        mandatory=mandatory,
        optional=optional,
        blocked_reasons=blocked,
        degraded_reasons=degraded,
        conflicts=conflicts,
        applied_exception_ids=(),
        selection_engine_version="arc_selection_v1",
    )


def _fact(cid: str) -> CapFact:
    return CapFact(capability_id=cid, owner="payments", lifecycle="ga", version="1.2.3")


# --- the central guarantee -------------------------------------------------


def test_a_result_cannot_become_ready_by_dropping_a_mandatory_item() -> None:
    """The single most important property here.

    A truncated obligation list that still says ready is the worst output ARC
    could produce: the agent believes it knows what it must do.
    """
    directives = tuple(_scoped(f"anchor-{i}" + "x" * 200) for i in range(20))
    tight = assemble(_selection(mandatory=directives), budget_limit_bytes=200)
    assert tight.status is ResolutionStatus.BLOCKED
    assert BLOCKED_BUDGET_EXCEEDED in tight.blocked_reasons
    assert tight.directives == (), "no partial mandatory set may be returned"


def test_over_budget_names_the_offending_revisions_without_leaking_content() -> None:
    """An operator needs to know which artifact to shrink; nobody needs its text."""
    scoped = _scoped("anchor" + "y" * 400)
    bundle = assemble(_selection(mandatory=(scoped,)), budget_limit_bytes=100)
    assert bundle.offending_artifact_ids == (str(scoped.directive.revision_id),)
    assert "y" * 400 not in str(bundle.offending_artifact_ids)


def test_the_block_decision_ignores_optional_content_size() -> None:
    """Otherwise whether the mandatory set fits would depend on unrelated extras."""
    mandatory = (_scoped(),)
    huge_optional = tuple(_scoped(f"o-{i}" + "z" * 500, mandatory=False) for i in range(20))
    with_optional = assemble(_selection(mandatory=mandatory, optional=huge_optional), budget_limit_bytes=600)
    assert with_optional.status is ResolutionStatus.READY


# --- CAP facts -------------------------------------------------------------


def test_cap_facts_are_included_when_they_fit() -> None:
    bundle = assemble(_selection(mandatory=(_scoped(),)), budget_limit_bytes=_BIG, cap_facts=(_fact("c1"),))
    assert len(bundle.cap_facts) == 1
    assert bundle.omission_reasons == ()


def test_unavailable_cap_facts_are_omitted_and_do_not_change_the_status() -> None:
    """Two parts of the spec disagreed; "CAP facts never change the status" wins.

    A requirement elsewhere said this degrades. It does not: the status is derived
    from the mandatory set alone, and an informational gap is an omission.
    """
    bundle = assemble(_selection(mandatory=(_scoped(),)), budget_limit_bytes=_BIG, cap_facts_available=False)
    assert bundle.status is ResolutionStatus.READY
    assert bundle.degraded_reasons == ()
    assert OMITTED_CAP_FACTS_UNAVAILABLE in bundle.omission_reasons


def test_oversized_cap_facts_are_omitted_and_do_not_change_the_status() -> None:
    many = tuple(_fact(f"c{i}" + "w" * 100) for i in range(200))
    bundle = assemble(_selection(mandatory=(_scoped(),)), budget_limit_bytes=_BIG, cap_facts=many)
    assert bundle.status is ResolutionStatus.READY
    assert bundle.cap_facts == ()
    assert OMITTED_CAP_FACTS_OVER_BUDGET in bundle.omission_reasons


def test_cap_facts_cannot_crowd_out_an_obligation() -> None:
    """Their separate allowance is what guarantees it."""
    many = tuple(_fact(f"c{i}" + "w" * 100) for i in range(200))
    bundle = assemble(_selection(mandatory=(_scoped(),)), budget_limit_bytes=_BIG, cap_facts=many)
    assert len(bundle.directives) == 1


def test_cap_facts_have_their_own_smaller_allowance() -> None:
    assert 0 < CAP_FACTS_BUDGET_BYTES < _BIG


# --- upstream status pass-through -----------------------------------------


def test_an_upstream_block_is_reported_not_re_decided() -> None:
    bundle = assemble(
        _selection(status=ResolutionStatus.BLOCKED, blocked=("blocked_conflict",)),
        budget_limit_bytes=_BIG,
    )
    assert bundle.status is ResolutionStatus.BLOCKED
    assert bundle.blocked_reasons == ("blocked_conflict",)
    assert bundle.cap_facts == ()


def test_an_upstream_block_cites_the_conflicting_revisions() -> None:
    a, b = _scoped(), _scoped()
    finding = ConflictFinding(
        subject=a.directive.conflict_subject,  # type: ignore[arg-type]
        left=a.directive,
        right=b.directive,
    )
    bundle = assemble(
        _selection(status=ResolutionStatus.BLOCKED, blocked=("blocked_conflict",), conflicts=(finding,)),
        budget_limit_bytes=_BIG,
    )
    assert str(a.directive.revision_id) in bundle.offending_artifact_ids


def test_upstream_degradation_is_preserved() -> None:
    bundle = assemble(
        _selection(
            mandatory=(_scoped(),), status=ResolutionStatus.DEGRADED, degraded=("degraded_optional_unavailable",)
        ),
        budget_limit_bytes=_BIG,
    )
    assert bundle.status is ResolutionStatus.DEGRADED
    assert bundle.degraded_reasons == ("degraded_optional_unavailable",)


# --- accounting and inputs -------------------------------------------------


def test_byte_count_is_recorded_for_the_receipt() -> None:
    bundle = assemble(_selection(mandatory=(_scoped(),)), budget_limit_bytes=_BIG)
    assert bundle.rendered_content_bytes > 0
    assert bundle.budget_limit_bytes == _BIG


def test_a_non_positive_budget_is_rejected() -> None:
    """A zero budget would block every bundle, which is a misconfiguration."""
    with pytest.raises(ValueError, match="must be positive"):
        assemble(_selection(), budget_limit_bytes=0)


def test_assembly_is_deterministic() -> None:
    selection = _selection(mandatory=(_scoped("a"), _scoped("b")), optional=(_scoped("c", mandatory=False),))
    first = assemble(selection, budget_limit_bytes=_BIG, cap_facts=(_fact("c1"),))
    second = assemble(selection, budget_limit_bytes=_BIG, cap_facts=(_fact("c1"),))
    assert first == second


def test_directive_content_carries_no_source_body() -> None:
    """Full text reaches a caller only through JIT detail, redacted by audience."""
    bundle = assemble(_selection(mandatory=(_scoped(),)), budget_limit_bytes=_BIG)
    keys = set(bundle.directives[0])
    assert keys == {
        "directive_id",
        "revision_id",
        "directive_type",
        "scope",
        "source_anchor",
        "constraint",
    }
    assert not {k for k in keys if "body" in k or "plaintext" in k or "ciphertext" in k}
