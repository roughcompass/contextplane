"""Conflict detection over normalized constraints.

Two directives conflict when they share a `conflict_subject_key` and their
normalized constraints admit no common assignment. Both halves matter: same
subject alone is not a conflict — two directives may add compatible controls over
the same thing — and incompatible constraints over *different* subjects are simply
unrelated.

The implementation is a set intersection over an explicit model of what each
constraint permits, not a table of operator pairs. With four operators and two
modalities there are 64 ordered combinations, and a pairwise table is exactly
where the one nobody thought about hides. Reducing each constraint to "the set of
values this permits" collapses all of them into one comparison.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import uuid
from dataclasses import dataclass

from registry.arc.types import (
    ApplicabilityRule,
    AuthorityScope,
    ConflictSubjectKey,
    ConstraintOperator,
    Directive,
    DirectiveType,
    Modality,
    NormalizedConstraint,
    ResolutionStatus,
    TaskManifest,
)

#: The engine identity a receipt records. Bumped when a change to this
#: module could make the same inputs resolve differently -- that is what
#: lets a replay years later distinguish tampering from a newer engine.
SELECTION_ENGINE_VERSION = "arc_selection_v1"


@dataclass(frozen=True)
class _Permitted:
    """What a constraint allows, as either a finite set or a complement of one.

    `is_complement=False` — exactly these values are allowed.
    `is_complement=True`  — everything except these values is allowed.
    """

    values: frozenset[str]
    is_complement: bool

    def intersects(self, other: _Permitted) -> bool:
        """Whether some assignment satisfies both."""
        if not self.is_complement and not other.is_complement:
            # Two finite sets: they must share a member.
            return bool(self.values & other.values)
        if self.is_complement and other.is_complement:
            # Two complements of finite sets over an unbounded universe always
            # leave something out of both.
            return True
        finite, complement = (self.values, other.values) if not self.is_complement else (other.values, self.values)
        # A finite set intersects a complement unless every member is excluded.
        return bool(finite - complement)


def _permitted(constraint: NormalizedConstraint) -> _Permitted:
    """Reduce a constraint to the set of values it permits.

    This is the step that removes the operator table. Once both sides are
    expressed as permitted-value sets, `require`/`prohibit` and all four operators
    compare the same way.
    """
    op = constraint.operator
    prohibit = constraint.modality is Modality.PROHIBIT

    if op is ConstraintOperator.PRESENT:
        # `require present` permits any value; `prohibit present` permits none,
        # which is the empty finite set and therefore intersects nothing.
        return _Permitted(frozenset(), is_complement=not prohibit)

    if op is ConstraintOperator.EQUALS or op is ConstraintOperator.IN_SET:
        # require: only these. prohibit: anything but these.
        return _Permitted(constraint.values, is_complement=prohibit)

    # NOT_IN_SET inverts the sense, so a prohibition of "not in S" permits S.
    return _Permitted(constraint.values, is_complement=not prohibit)


def constraints_are_compatible(left: NormalizedConstraint, right: NormalizedConstraint) -> bool:
    """Whether some assignment satisfies both constraints."""
    return _permitted(left).intersects(_permitted(right))


def directives_conflict(left: Directive, right: Directive) -> bool:
    """Whether two directives conflict.

    A `citation_only` directive never conflicts: it carries no constraint, so
    there is nothing for another directive to be incompatible with. Directives
    with different subjects are additive — ARC makes no attempt to infer semantic
    contradiction from prose, because guessing there would either block valid work
    or, worse, silently permit an action two directives forbid.
    """
    if not left.directive_type.is_action_protecting or not right.directive_type.is_action_protecting:
        # Decided on the declared type, not only on whether a comparable shape
        # happens to be present. The write path refuses a `citation_only`
        # directive carrying a conflict key, but the schema's CHECK does not --
        # it constrains only the action-protecting types -- so a row predating
        # that refusal can still arrive here fully comparable. Reading the type
        # is what makes this function's first sentence true of every row rather
        # than of every row someone remembered to validate.
        return False
    if left.constraint is None or right.constraint is None:
        return False
    if left.conflict_subject is None or right.conflict_subject is None:
        return False
    if left.conflict_subject != right.conflict_subject:
        return False
    if left.directive_id == right.directive_id:
        # Same stable identity: a successor revision replaces its predecessor
        # rather than conflicting with it.
        return False
    return not constraints_are_compatible(left.constraint, right.constraint)


@dataclass(frozen=True)
class ConflictFinding:
    """One incompatible pair, with both sides cited.

    Both revisions are named because `blocked_conflict` must let an operator see
    what disagreed. Picking a winner by revision timestamp would resolve the
    block invisibly and is exactly what ARC does not do.
    """

    subject: ConflictSubjectKey
    left: Directive
    right: Directive

    def revision_pair(self) -> tuple[str, str]:
        return (str(self.left.revision_id), str(self.right.revision_id))


def find_conflicts(directives: list[Directive]) -> list[ConflictFinding]:
    """Every conflicting pair, in a deterministic order.

    Grouped by subject first so the comparison is quadratic only within a subject
    rather than across the whole selection. Output is sorted by subject and then
    by directive id, because a nondeterministic reason list would make two
    identical resolutions produce different receipts.
    """
    by_subject: dict[ConflictSubjectKey, list[Directive]] = {}
    for directive in directives:
        if directive.conflict_subject is None or directive.constraint is None:
            continue
        by_subject.setdefault(directive.conflict_subject, []).append(directive)

    findings: list[ConflictFinding] = []
    for subject in sorted(by_subject):
        group = sorted(by_subject[subject], key=lambda d: str(d.directive_id))
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                if directives_conflict(left, right):
                    findings.append(ConflictFinding(subject=subject, left=left, right=right))
    return findings


# ---------------------------------------------------------------------------
# Applicability matching
# ---------------------------------------------------------------------------


def _matches_any(rule_values: frozenset[object], manifest_values: frozenset[object]) -> bool:
    """An empty rule selector means "no constraint on this dimension".

    Empty-means-any is the only workable reading — a global rule names no
    capability and must still match — but it also means a rule that *meant* to
    name something and named nothing matches everything. That is why
    `ApplicabilityRule.__post_init__` rejects a tenant- or capability-scoped rule
    with no selector: the dangerous cases are refused at construction rather than
    silently widened here.
    """
    if not rule_values:
        return True
    return bool(rule_values & manifest_values)


def _matches_scalar(rule_values: frozenset[str], value: str | None) -> bool:
    if not rule_values:
        return True
    return value is not None and value in rule_values


def rule_applies(
    rule: ApplicabilityRule,
    manifest: TaskManifest,
    *,
    tenant_id: uuid.UUID,
    as_of: datetime.datetime,
) -> bool:
    """Whether `rule` matches this manifest at `as_of`.

    `as_of` is a parameter, not a clock read, because two evaluations of the same
    manifest at the same `as_of` must agree — including one replayed months later
    while verifying a receipt.
    """
    if rule.effective_from is not None and as_of < rule.effective_from:
        return False
    if rule.effective_until is not None and as_of > rule.effective_until:
        return False

    # A tenant-scoped rule targets exactly one tenant. Matching on the requesting
    # tenant rather than on the rule's owning scope is deliberate: a rule owned by
    # a tenant can only ever apply to that tenant's own requests.
    if rule.scope is AuthorityScope.TENANT and rule.target_tenant_id != tenant_id:
        return False

    if not _matches_any(rule.task_kinds, frozenset({manifest.task_kind})):
        return False
    # Action classes match on overlap: a rule protecting `deploy` applies to a
    # manifest requesting deploy *and* merge, because the deploy obligation is
    # still owed.
    if not _matches_any(rule.action_classes, manifest.requested_action_classes):
        return False
    if not _matches_any(rule.capability_ids, manifest.capability_ids):
        return False
    if not _matches_any(rule.domain_ids, manifest.domain_ids):
        return False
    if not _matches_scalar(rule.environments, manifest.environment):
        return False
    return _matches_scalar(rule.data_sensitivity_tiers, manifest.data_sensitivity)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopedDirective:
    """A directive together with the rule that made it apply.

    Precedence is a property of the *rule's* scope, not of the directive, so the
    two travel together from matching through ordering.
    """

    directive: Directive
    rule: ApplicabilityRule
    revision_effective_from: datetime.datetime

    @property
    def scope(self) -> AuthorityScope:
        return self.rule.scope

    @property
    def is_mandatory(self) -> bool:
        return self.rule.is_mandatory


@dataclass(frozen=True)
class ApprovedException:
    """An approved lower-scope weakening of a named higher-scope directive."""

    exception_id: uuid.UUID
    higher_scope_directive_id: uuid.UUID
    higher_scope_revision_id: uuid.UUID
    lower_scope_tenant_id: uuid.UUID
    replacement_constraint: NormalizedConstraint
    effective_from: datetime.datetime | None = None
    effective_until: datetime.datetime | None = None
    revoked_at: datetime.datetime | None = None

    def is_active_at(self, moment: datetime.datetime) -> bool:
        if self.revoked_at is not None and moment >= self.revoked_at:
            return False
        if self.effective_from is not None and moment < self.effective_from:
            return False
        return not (self.effective_until is not None and moment > self.effective_until)


def apply_exceptions(
    scoped: list[ScopedDirective],
    exceptions: list[ApprovedException],
    *,
    tenant_id: uuid.UUID,
    as_of: datetime.datetime,
) -> tuple[list[ScopedDirective], list[uuid.UUID]]:
    """Apply approved exceptions, returning the result and the ones used.

    An exception only takes effect when the directive it names declares
    `delegable_exception`. That check is here rather than only at write time
    because an exception approved against a directive that was later replaced by a
    non-delegable successor must stop applying — otherwise a stale approval would
    keep weakening a control nobody may weaken.
    """
    by_target = {
        (e.higher_scope_directive_id, e.higher_scope_revision_id): e
        for e in exceptions
        if e.lower_scope_tenant_id == tenant_id and e.is_active_at(as_of)
    }

    result: list[ScopedDirective] = []
    used: list[uuid.UUID] = []
    for item in scoped:
        exception = by_target.get((item.directive.directive_id, item.directive.revision_id))
        if exception is None or not item.directive.delegable_exception:
            result.append(item)
            continue
        replaced = dataclasses.replace(item.directive, constraint=exception.replacement_constraint)
        result.append(dataclasses.replace(item, directive=replaced))
        used.append(exception.exception_id)
    return result, sorted(used, key=str)


def order_by_precedence(scoped: list[ScopedDirective]) -> list[ScopedDirective]:
    """Deterministic precedence order.

    Authority scope first (global before tenant before domain before capability
    before task), then revision effective time, then directive id as the final
    tiebreak. The last key matters more than it looks: without it two directives
    sharing a scope and an effective timestamp would order by input sequence, and
    the same manifest could produce two different receipts.
    """
    return sorted(
        scoped,
        key=lambda s: (
            s.scope.rank,
            s.revision_effective_from,
            str(s.directive.directive_id),
        ),
    )


def collapse_successors(scoped: list[ScopedDirective]) -> list[ScopedDirective]:
    """Keep one projection per stable directive identity — the latest approved.

    A successor revision replaces its predecessor rather than accumulating beside
    it. Without this, an artifact whose directive was revised would contribute the
    old and the new text to the same bundle, and a conflict check would then find
    them disagreeing with each other.
    """
    latest: dict[uuid.UUID, ScopedDirective] = {}
    for item in scoped:
        current = latest.get(item.directive.directive_id)
        if current is None or (
            item.revision_effective_from,
            str(item.directive.revision_id),
        ) > (current.revision_effective_from, str(current.directive.revision_id)):
            latest[item.directive.directive_id] = item
    return [latest[k] for k in sorted(latest, key=str)]


# ---------------------------------------------------------------------------
# SelectionService — a pure function
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MandatoryObligation:
    """A family-level tombstone: an obligation that exists but is unsatisfied.

    Carried separately from the directives because its whole purpose is to be
    detectable when nothing is there. A revoked mandatory projection simply stops
    appearing, and a bundle missing an obligation would otherwise look identical
    to one that never had it.
    """

    obligation_id: uuid.UUID
    directive_id: uuid.UUID
    obligation_state: str
    applicability_digest: str

    @property
    def is_missing(self) -> bool:
        return self.obligation_state != "satisfied"


@dataclass(frozen=True)
class SelectionInput:
    """Everything selection reads. No handles to a session, engine, or clock.

    Assembled by the caller from one database snapshot at one `as_of`, so the
    function below can be pure — and so determinism is testable without holding a
    database still.
    """

    manifest: TaskManifest
    tenant_id: uuid.UUID
    as_of: datetime.datetime
    candidates: tuple[tuple[Directive, ApplicabilityRule, datetime.datetime], ...] = ()
    exceptions: tuple[ApprovedException, ...] = ()
    obligations: tuple[MandatoryObligation, ...] = ()
    selection_engine_version: str = SELECTION_ENGINE_VERSION
    selection_config_digest: str = ""


@dataclass(frozen=True)
class SelectionResult:
    """The ordered outcome. Reason codes are bounded and sorted."""

    status: ResolutionStatus
    mandatory: tuple[ScopedDirective, ...]
    optional: tuple[ScopedDirective, ...]
    blocked_reasons: tuple[str, ...]
    degraded_reasons: tuple[str, ...]
    conflicts: tuple[ConflictFinding, ...]
    applied_exception_ids: tuple[uuid.UUID, ...]
    selection_engine_version: str

    @property
    def is_ready(self) -> bool:
        return self.status is ResolutionStatus.READY


def selection_config_digest() -> str:
    """A digest of the configuration selection ran under.

    There is no tunable config here -- the engine is a pure function -- so
    what actually determines the outcome is the engine version together with
    the closed vocabularies it matches against. Narrowing `TaskKind` or
    adding an `AuthorityScope` changes which directives apply and in what
    order, and a receipt whose provenance did not move would claim the same
    configuration produced both results.

    Length-prefixed per member so no two different vocabularies can be
    concatenated into the same bytes.
    """
    parts: list[str] = [SELECTION_ENGINE_VERSION]
    for vocabulary in (AuthorityScope, ConstraintOperator, DirectiveType, Modality):
        for member in sorted(str(v) for v in vocabulary):
            parts.append(f"{len(member)}:{member}")
    material = "|".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


BLOCKED_CONFLICT = "blocked_conflict"
BLOCKED_MISSING_MANDATORY = "blocked_missing_mandatory"
DEGRADED_OPTIONAL_UNAVAILABLE = "degraded_optional_unavailable"


def select(inputs: SelectionInput) -> SelectionResult:
    """Resolve a manifest against active revisions. Pure.

    Same `(manifest, candidates, exceptions, obligations, as_of, version)` in,
    byte-identical result out — independent of input ordering and of anything
    outside the argument. That is the whole reason this is a function and not a
    method on a session-holding service: NF1.1 becomes a property test rather
    than an integration test that has to keep a database still.

    Status is reduced exactly once, at the end. Any mandatory blocking reason
    yields `blocked`; otherwise optional degradation yields `degraded`; otherwise
    `ready`. Reducing early and then "upgrading" is how a later success ends up
    overwriting an earlier failure.
    """
    matched = [
        ScopedDirective(directive=directive, rule=rule, revision_effective_from=effective)
        for directive, rule, effective in inputs.candidates
        if rule_applies(rule, inputs.manifest, tenant_id=inputs.tenant_id, as_of=inputs.as_of)
    ]

    collapsed = collapse_successors(matched)
    with_exceptions, applied = apply_exceptions(
        collapsed, list(inputs.exceptions), tenant_id=inputs.tenant_id, as_of=inputs.as_of
    )
    ordered = order_by_precedence(with_exceptions)

    mandatory = tuple(s for s in ordered if s.is_mandatory)
    optional = tuple(s for s in ordered if not s.is_mandatory)

    blocking: set[str] = set()
    degrading: set[str] = set()

    conflicts = tuple(find_conflicts([s.directive for s in mandatory]))
    if conflicts:
        blocking.add(BLOCKED_CONFLICT)

    # A durable tombstone blocks even though nothing is present to point at —
    # which is exactly why it is durable.
    if any(o.is_missing for o in inputs.obligations):
        blocking.add(BLOCKED_MISSING_MANDATORY)

    # An optional directive that conflicts degrades rather than blocks: the
    # mandatory set is still complete and still coherent.
    if find_conflicts([s.directive for s in optional]):
        degrading.add(DEGRADED_OPTIONAL_UNAVAILABLE)

    if blocking:
        status = ResolutionStatus.BLOCKED
    elif degrading:
        status = ResolutionStatus.DEGRADED
    else:
        status = ResolutionStatus.READY

    return SelectionResult(
        status=status,
        mandatory=mandatory,
        optional=optional,
        blocked_reasons=tuple(sorted(blocking)),
        degraded_reasons=tuple(sorted(degrading)),
        conflicts=conflicts,
        applied_exception_ids=tuple(applied),
        selection_engine_version=inputs.selection_engine_version,
    )
