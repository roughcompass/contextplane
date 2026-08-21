"""SelectionService: status reduction and purity."""

from __future__ import annotations

import datetime
import inspect as _inspect
import uuid

from contextplane.arc.service import selection as sel
from contextplane.arc.service.selection import (
    BLOCKED_CONFLICT,
    BLOCKED_MISSING_MANDATORY,
    DEGRADED_OPTIONAL_UNAVAILABLE,
    MandatoryObligation,
    SelectionInput,
    select,
    select_and_verify,
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
    ResolutionStatus,
)

_T1 = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
_NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)


def _manifest() -> IntentManifest:
    return IntentManifest(
        session_id="s1",
        intent_kind=IntentKind.CODE_CHANGE,
        requested_action_classes=frozenset({ActionClass.MERGE}),
    )


def _subject(**over: str) -> ConflictSubjectKey:
    fields = {
        "schema_version": "arc_conflict_v1",
        "namespace": "n",
        "subject_selector": "s",
        "operation": "o",
        "action_class": "merge",
        "target_selector": "t",
    }
    fields.update(over)
    return ConflictSubjectKey(**fields)  # type: ignore[arg-type]


def _candidate(
    *,
    mandatory: bool = True,
    modality: str = "require",
    value: str = "approved",
    subject: ConflictSubjectKey | None = None,
    did: uuid.UUID | None = None,
) -> tuple[Directive, ApplicabilityRule, datetime.datetime]:
    rid = uuid.uuid4()
    directive = Directive(
        directive_id=did or uuid.uuid4(),
        revision_id=rid,
        directive_type=DirectiveType.REQUIRE,
        source_anchor="a#1",
        conflict_subject=subject or _subject(),
        constraint=NormalizedConstraint.parse(modality, "equals", value),
    )
    rule = ApplicabilityRule(rule_id=uuid.uuid4(), revision_id=rid, scope=AuthorityScope.GLOBAL, is_mandatory=mandatory)
    return (directive, rule, _NOW)


def _inputs(**over: object) -> SelectionInput:
    fields: dict[str, object] = {"manifest": _manifest(), "tenant_id": _T1, "as_of": _NOW}
    fields.update(over)
    return SelectionInput(**fields)  # type: ignore[arg-type]


# --- status reduction ------------------------------------------------------


def test_no_applicable_directives_is_still_ready() -> None:
    """Nothing owed is a valid answer, not a degradation."""
    assert select(_inputs()).status is ResolutionStatus.READY


def test_conflicting_mandatory_directives_block() -> None:
    subject = _subject()
    result = select(
        _inputs(
            candidates=(
                _candidate(subject=subject, modality="require", value="x"),
                _candidate(subject=subject, modality="prohibit", value="x"),
            )
        )
    )
    assert result.status is ResolutionStatus.BLOCKED
    assert BLOCKED_CONFLICT in result.blocked_reasons
    assert len(result.conflicts) == 1


def test_a_missing_mandatory_obligation_blocks_with_nothing_to_point_at() -> None:
    """The durable tombstone is the whole reason this can be detected."""
    obligation = MandatoryObligation(
        obligation_id=uuid.uuid4(),
        directive_id=uuid.uuid4(),
        obligation_state="missing_revoked",
        applicability_digest="d" * 64,
    )
    result = select(_inputs(obligations=(obligation,)))
    assert result.status is ResolutionStatus.BLOCKED
    assert BLOCKED_MISSING_MANDATORY in result.blocked_reasons
    assert result.mandatory == ()


def test_a_satisfied_obligation_does_not_block() -> None:
    obligation = MandatoryObligation(
        obligation_id=uuid.uuid4(),
        directive_id=uuid.uuid4(),
        obligation_state="satisfied",
        applicability_digest="d" * 64,
    )
    assert select(_inputs(obligations=(obligation,))).status is ResolutionStatus.READY


def test_conflicting_optional_directives_degrade_rather_than_block() -> None:
    """The mandatory set is still complete and still coherent."""
    subject = _subject()
    result = select(
        _inputs(
            candidates=(
                _candidate(mandatory=False, subject=subject, modality="require", value="x"),
                _candidate(mandatory=False, subject=subject, modality="prohibit", value="x"),
            )
        )
    )
    assert result.status is ResolutionStatus.DEGRADED
    assert DEGRADED_OPTIONAL_UNAVAILABLE in result.degraded_reasons


def test_a_mandatory_block_outranks_an_optional_degradation() -> None:
    """Status is reduced once at the end, so a later success cannot upgrade it."""
    subject = _subject()
    optional_subject = _subject(operation="other")
    result = select(
        _inputs(
            candidates=(
                _candidate(subject=subject, modality="require", value="x"),
                _candidate(subject=subject, modality="prohibit", value="x"),
                _candidate(mandatory=False, subject=optional_subject, modality="require", value="y"),
                _candidate(mandatory=False, subject=optional_subject, modality="prohibit", value="y"),
            )
        )
    )
    assert result.status is ResolutionStatus.BLOCKED


def test_reason_codes_are_sorted_and_bounded() -> None:
    """Unsorted reasons would make two identical resolutions differ."""
    subject = _subject()
    obligation = MandatoryObligation(
        obligation_id=uuid.uuid4(),
        directive_id=uuid.uuid4(),
        obligation_state="missing_invalid",
        applicability_digest="d" * 64,
    )
    result = select(
        _inputs(
            candidates=(
                _candidate(subject=subject, modality="require", value="x"),
                _candidate(subject=subject, modality="prohibit", value="x"),
            ),
            obligations=(obligation,),
        )
    )
    assert list(result.blocked_reasons) == sorted(result.blocked_reasons)
    assert all(len(r) <= 64 for r in result.blocked_reasons)


def test_a_non_matching_rule_contributes_nothing() -> None:
    rid = uuid.uuid4()
    directive = Directive(
        directive_id=uuid.uuid4(), revision_id=rid, directive_type=DirectiveType.CITATION_ONLY, source_anchor="a#1"
    )
    rule = ApplicabilityRule(
        rule_id=uuid.uuid4(),
        revision_id=rid,
        scope=AuthorityScope.GLOBAL,
        intent_kinds=frozenset({IntentKind.DEPLOYMENT}),
    )
    result = select(_inputs(candidates=((directive, rule, _NOW),)))
    assert result.mandatory == ()


def test_the_engine_version_is_reported_from_the_input() -> None:
    """A behaviour change must be a version change, so the receipt records which."""
    result = select(_inputs(selection_engine_version="arc_selection_v2"))
    assert result.selection_engine_version == "arc_selection_v2"


# --- purity ----------------------------------------------------------------


def test_select_takes_only_its_input() -> None:
    """One parameter, no session, no clock — the signature is the guarantee."""
    params = list(_inspect.signature(select).parameters)
    assert params == ["inputs"]


def test_the_module_imports_nothing_that_does_io() -> None:
    """Purity is easy to lose by adding one import.

    Asserted structurally so a later change that reaches for a session or a clock
    fails here rather than quietly making determinism untestable.
    """
    forbidden = {"sqlalchemy", "httpx", "requests", "asyncio", "time", "random", "os"}
    imported = {name.split(".")[0] for name, value in vars(sel).items() if _inspect.ismodule(value)}
    assert not (imported & forbidden), f"selection imports {sorted(imported & forbidden)}"


def test_selection_does_not_mutate_its_input() -> None:
    inputs = _inputs(candidates=(_candidate(),))
    before = (inputs.candidates, inputs.exceptions, inputs.obligations)
    select(inputs)
    assert (inputs.candidates, inputs.exceptions, inputs.obligations) == before


def test_repeated_calls_return_equal_results() -> None:
    inputs = _inputs(candidates=(_candidate(), _candidate(mandatory=False)))
    assert select(inputs) == select(inputs)


# --- citation_only cannot conflict, whatever shape the row carries ---------------


def _comparable(directive_type: DirectiveType, value: str) -> Directive:
    """A directive carrying the full comparable shape, of any declared type."""
    subject = ConflictSubjectKey(
        schema_version="arc_conflict_v1",
        namespace="deploy",
        subject_selector="service",
        operation="release",
        action_class="deploy",
        target_selector="prod",
    )
    return Directive(
        directive_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        directive_type=directive_type,
        source_anchor="anchor",
        conflict_subject=subject,
        constraint=NormalizedConstraint.parse("require", "equals", value),
    )


def test_two_citation_only_directives_never_conflict_even_when_comparable() -> None:
    """`citation_only` means "may be cited, cannot make an action blocked".

    `Directive.__post_init__` requires the comparable shape for the
    action-protecting types but does not forbid it here, and the schema's CHECK
    constrains only those types -- so a row can arrive fully comparable while
    declaring itself citation_only. Deciding comparability from the shape alone
    let such a row produce `blocked_conflict`, which would block every matching
    resolution over a directive that was only ever meant to be read.
    """
    assert not sel.directives_conflict(
        _comparable(DirectiveType.CITATION_ONLY, "x"), _comparable(DirectiveType.CITATION_ONLY, "y")
    )


def test_a_citation_only_directive_cannot_conflict_with_an_enforcing_one() -> None:
    """Either side being non-enforcing is enough. A conflict is a statement
    that two directives cannot both be satisfied, and a directive that
    enforces nothing cannot be unsatisfiable."""
    assert not sel.directives_conflict(
        _comparable(DirectiveType.CITATION_ONLY, "x"), _comparable(DirectiveType.REQUIRE, "y")
    )


def test_two_enforcing_directives_still_conflict() -> None:
    """The control. Without this the fix above could pass by disabling
    conflict detection entirely."""
    assert sel.directives_conflict(
        _comparable(DirectiveType.REQUIRE, "x"), _comparable(DirectiveType.PROHIBIT, "x")
    ) or sel.directives_conflict(_comparable(DirectiveType.REQUIRE, "x"), _comparable(DirectiveType.REQUIRE, "y"))


# --- the applicability snapshot must not drift from the rule it records ----------


def test_every_matched_dimension_is_in_the_obligation_snapshot() -> None:
    """A dimension `rule_applies` matches on must survive into the snapshot.

    An obligation outlives the revision that satisfied it, so its snapshot is
    the only record of who it applied to. A dimension present in the rule but
    absent from the snapshot fails in the dangerous direction: an empty
    selector means "matches any", so the rehydrated obligation applies more
    widely than the rule ever did.

    `entity_labels` is the live example. It is in `ApplicabilityRule` and
    in the corpus query, but not in `ApplicabilityDraft` and not in the
    snapshot -- inert only because nothing populates the column and
    `rule_applies` reads `entity_ids` instead. This test is what turns
    that into a failure the moment either changes.
    """
    import dataclasses

    from contextplane.arc.service.artifact_materialisation import ApplicabilityDraft

    draft_fields = {f.name for f in dataclasses.fields(ApplicabilityDraft)}
    snapshot_keys = set(ApplicabilityDraft(scope=AuthorityScope.GLOBAL, effective_from=_NOW).snapshot().keys())

    # Fields that legitimately do not describe *who* a rule applies to.
    not_applicability = {"effective_from", "effective_until", "is_mandatory"}
    must_be_recorded = draft_fields - not_applicability

    missing = must_be_recorded - snapshot_keys
    assert not missing, (
        "these applicability dimensions are writable on a rule but absent from the obligation "
        f"snapshot, so a tombstoned obligation would match more widely than the rule did: {sorted(missing)}"
    )


def test_the_snapshot_records_nothing_the_rule_cannot_express() -> None:
    """The reverse direction. A snapshot key with no corresponding draft field
    is dead weight that a reader would assume is enforced."""
    import dataclasses

    from contextplane.arc.service.artifact_materialisation import ApplicabilityDraft

    draft_fields = {f.name for f in dataclasses.fields(ApplicabilityDraft)}
    snapshot_keys = set(ApplicabilityDraft(scope=AuthorityScope.GLOBAL, effective_from=_NOW).snapshot().keys())

    assert not snapshot_keys - draft_fields


def test_a_draft_and_a_row_produce_the_same_applicability_digest() -> None:
    """The registration path and the obligation refresh must agree exactly.

    Registration digests a draft; the refresh digests the rule rows it reads
    back. The digest is the obligation dedup key, so a divergence does not
    merely look untidy -- it splits one obligation into two, and the tombstone
    the first leaves behind can never be cleared by approving a replacement.

    They were separate implementations of the same shape. This pins the thing
    that made that safe: identical values in, identical digest out, regardless
    of whether the selectors arrived as tuples from a draft or as lists from a
    row -- or as NULL, which is how a row spells an absent selector.
    """
    from contextplane.arc.service.artifact_integrity import applicability_digest, applicability_snapshot
    from contextplane.arc.service.artifact_materialisation import ApplicabilityDraft

    capability = uuid.uuid4()
    tenant = uuid.uuid4()

    draft = ApplicabilityDraft(
        scope=AuthorityScope.TENANT,
        effective_from=_NOW,
        target_tenant_id=tenant,
        entity_ids=(capability,),
        intent_kinds=("deployment",),
        action_classes=("deploy",),
    )
    # What the refresh query hands back: lists, and NULL for anything unset.
    from_row = applicability_snapshot(
        scope="tenant",
        target_tenant_id=tenant,
        entity_ids=[capability],
        domain_ids=None,
        intent_kinds=["deployment"],
        action_classes=["deploy"],
        environments=None,
        data_sensitivity_tiers=None,
    )

    assert draft.snapshot() == from_row
    assert draft.digest() == applicability_digest(from_row)


def test_selector_ordering_does_not_change_the_applicability_digest() -> None:
    """Two rules differing only in the order a selector was written are the
    same rule, so they must not produce two obligations."""
    from contextplane.arc.service.artifact_integrity import applicability_digest, applicability_snapshot

    a, b = "alpha", "beta"
    first = applicability_snapshot(
        scope="global",
        target_tenant_id=None,
        entity_ids=None,
        domain_ids=[a, b],
        intent_kinds=None,
        action_classes=None,
        environments=None,
        data_sensitivity_tiers=None,
    )
    second = applicability_snapshot(
        scope="global",
        target_tenant_id=None,
        entity_ids=None,
        domain_ids=[b, a],
        intent_kinds=None,
        action_classes=None,
        environments=None,
        data_sensitivity_tiers=None,
    )

    assert applicability_digest(first) == applicability_digest(second)


# ---------------------------------------------------------------------------
# select_and_verify: the §6.3 "new context selection" chokepoint.
# ---------------------------------------------------------------------------


class _FakeIntegrity:
    """Stands in for `RevisionIntegrityService`. `outcomes` maps a
    revision_id to its `(valid, reason_code)` pair; any revision not named
    defaults to valid, so a test only has to plant the one revision it
    cares about."""

    def __init__(self, outcomes: dict[uuid.UUID, tuple[bool, str | None]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[uuid.UUID, str]] = []

    async def assess(self, session: object, revision_id: uuid.UUID, purpose: str) -> _FakeAssessment:
        self.calls.append((revision_id, purpose))
        valid, reason_code = self.outcomes.get(revision_id, (True, None))
        return _FakeAssessment(valid=valid, reason_code=reason_code)


class _FakeAssessment:
    def __init__(self, *, valid: bool, reason_code: str | None) -> None:
        self.valid = valid
        self.reason_code = reason_code


async def test_select_and_verify_matches_bare_select_when_every_revision_passes() -> None:
    candidate = _candidate(mandatory=True)
    inputs = _inputs(candidates=(candidate,))
    integrity = _FakeIntegrity()

    result = await select_and_verify(object(), inputs, integrity)

    assert result == select(inputs)
    assert integrity.calls == [(candidate[0].revision_id, sel.PURPOSE_SELECTION)]


async def test_select_and_verify_blocks_the_whole_resolution_when_a_mandatory_revision_fails() -> None:
    """Silently dropping it would let a compromised or revoked mandatory
    obligation vanish from the response and look identical to one that was
    never owed -- so the whole resolution blocks instead, carrying the
    axis's own bounded reason code."""
    candidate = _candidate(mandatory=True)
    revision_id = candidate[0].revision_id
    inputs = _inputs(candidates=(candidate,))
    integrity = _FakeIntegrity({revision_id: (False, "arc_operational_integrity_failed")})

    result = await select_and_verify(object(), inputs, integrity)

    assert result.status is ResolutionStatus.BLOCKED
    assert "arc_operational_integrity_failed" in result.blocked_reasons
    # Named, not dropped: an operator or auditor reading the response can
    # still see which directive was blocked and why.
    assert result.mandatory == select(inputs).mandatory


async def test_select_and_verify_excludes_an_optional_revision_that_fails_and_degrades() -> None:
    """Optional was never required, so narrowing it is safe by
    construction -- unlike the mandatory case, dropping it is exactly the
    right behavior, not a silent weakening."""
    candidate = _candidate(mandatory=False)
    revision_id = candidate[0].revision_id
    inputs = _inputs(candidates=(candidate,))
    integrity = _FakeIntegrity({revision_id: (False, "arc_source_status_unavailable")})

    result = await select_and_verify(object(), inputs, integrity)

    assert result.status is ResolutionStatus.DEGRADED
    assert result.optional == ()
    # The specific bounded axis code, not the generic conflict-only marker
    # -- see `select_and_verify`'s own docstring for why that code is
    # informative rather than a leak.
    assert "arc_source_status_unavailable" in result.degraded_reasons


async def test_select_and_verify_does_not_call_assess_for_a_revision_select_already_dropped() -> None:
    """A candidate `rule_applies` never matched (a different manifest, an
    expired window) never reaches `select_and_verify`'s own integrity pass
    -- there is nothing to serve, so nothing to assess."""
    unrelated_manifest_candidate = _candidate(mandatory=True)
    # A rule that can never match: `intent_kinds` names a kind the fixed
    # `_manifest()` helper never requests.
    from dataclasses import replace

    directive, rule, effective = unrelated_manifest_candidate
    narrowed_rule = replace(rule, intent_kinds=frozenset({IntentKind.DEPLOYMENT}))
    inputs = _inputs(candidates=((directive, narrowed_rule, effective),))
    integrity = _FakeIntegrity()

    result = await select_and_verify(object(), inputs, integrity)

    assert result.mandatory == ()
    assert integrity.calls == []


async def test_select_and_verify_leaves_an_already_blocked_result_reason_set_intact() -> None:
    """A conflict blocks on its own merits; the integrity axis adds its own
    reason rather than replacing the conflict's."""
    subject = _subject()
    conflicting = (
        _candidate(subject=subject, modality="require", value="x"),
        _candidate(subject=subject, modality="prohibit", value="x"),
    )
    revision_id = conflicting[0][0].revision_id
    inputs = _inputs(candidates=conflicting)
    integrity = _FakeIntegrity({revision_id: (False, "arc_operational_integrity_pending")})

    result = await select_and_verify(object(), inputs, integrity)

    assert result.status is ResolutionStatus.BLOCKED
    assert BLOCKED_CONFLICT in result.blocked_reasons
    assert "arc_operational_integrity_pending" in result.blocked_reasons
