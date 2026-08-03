"""Assembling the governed corpus that selection decides over.

`select()` is a pure function over `SelectionInput`, which is what makes
determinism testable without holding a database still. The consequence is
that something else has to build that input, and until now nothing did:
every caller was a test that hand-built its candidates, so the query
finding which of the governed corpus applies to a manifest did not exist
in the product at all.

Three collections, and they do **not** have the same contract:

**Candidates are a prefilter.** `rule_applies` is authoritative for rule
matching, and an empty selector there means "no constraint on this
dimension". So a predicate like ``:task_kind = ANY(ar.task_kinds)`` would
drop precisely the broadest rules -- a global rule names no task kind and
must still match everything. This query therefore narrows only on what
selection does *not* re-check: revision lifecycle, the revision's
effective window, and tenant visibility.

**Tenant visibility is enforced here and nowhere else.** `rule_applies`
checks the requesting tenant only for a `tenant`-scoped rule; a `domain`-,
`capability`-, or `task`-scoped rule owned by another tenant carries no
tenant check at all and would apply to this caller if it were ever loaded.
The revision's `tenant_id` is the ownership root -- NULL means global and
applies deployment-wide, anything else must equal the caller's -- and that
predicate is the only thing standing between one tenant's policy and
another tenant's resolution.

**Obligations are authoritative, not a prefilter.** `select()` blocks on
`any(o.is_missing ...)` and applies no applicability filter of its own, so
an obligation loaded here that does not actually apply to this manifest
would block every resolution in the tenant. They are scoped by rehydrating
`applicability_snapshot` into an `ApplicabilityRule` and running the very
same `rule_applies` -- one matcher rather than a second one that drifts
from the first.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service.selection import (
    ApprovedException,
    MandatoryObligation,
    SelectionInput,
    rule_applies,
)
from registry.arc.types import (
    ActionClass,
    ApplicabilityRule,
    AuthorityScope,
    ConflictSubjectKey,
    Directive,
    DirectiveType,
    NormalizedConstraint,
    SatisfactionMode,
    TaskKind,
    TaskManifest,
    VocabularyError,
)

_log = logging.getLogger(__name__)

# Revisions selection may consider. `expired` is included deliberately: a
# revision whose review lapsed still governs, and dropping it here would
# silently release the obligation rather than surface it as degraded.
_SELECTABLE_LIFECYCLE = ("active", "expired")

# Every column the three domain objects need, joined from one snapshot.
_CANDIDATES_SQL = """
SELECT
    r.effective_from                    AS revision_effective_from,
    d.directive_id, d.revision_id, d.directive_type, d.source_anchor,
    d.conflict_key_schema_version, d.conflict_subject_digest,
    d.conflict_key_namespace, d.conflict_key_subject_selector,
    d.conflict_key_operation, d.conflict_key_action_class,
    d.conflict_key_target_selector, d.conflict_key_modality,
    d.conflict_key_constraint_operator, d.conflict_key_constraint_value,
    d.satisfaction_mode, d.verification_max_age_seconds,
    d.accepted_verifier_classes, d.required_evidence_type, d.delegable_exception,
    ar.rule_id, ar.scope, ar.is_mandatory, ar.target_tenant_id,
    ar.capability_ids, ar.capability_labels, ar.domain_ids, ar.task_kinds,
    ar.action_classes, ar.environments, ar.data_sensitivity_tiers,
    ar.effective_from AS rule_effective_from, ar.effective_until AS rule_effective_until
FROM arc_revisions r
JOIN arc_directives d ON d.revision_id = r.revision_id
JOIN arc_applicability_rules ar ON ar.revision_id = r.revision_id
WHERE r.lifecycle_state = ANY(:lifecycle)
  AND r.effective_from <= :as_of
  AND (r.effective_until IS NULL OR r.effective_until > :as_of)
  AND (r.tenant_id IS NULL OR r.tenant_id = :tenant_id)
ORDER BY d.revision_id, d.directive_id, ar.rule_id
"""

# Prefilter. `apply_exceptions` re-checks the tenant and the active window,
# so this only avoids loading another tenant's exceptions and ones already
# revoked -- both of which selection would discard anyway.
_EXCEPTIONS_SQL = """
SELECT exception_id, higher_scope_directive_id, higher_scope_revision_id,
       lower_scope_tenant_id, replacement_conflict_descriptor,
       lower_scope_kind, lower_scope_domain_id, lower_scope_capability_id,
       lower_scope_task_kind, lower_scope_action_class,
       lower_scope_environment, lower_scope_data_sensitivity,
       effective_from, effective_until, revoked_at
FROM arc_approved_exceptions
WHERE lower_scope_tenant_id = :tenant_id
  AND (revoked_at IS NULL OR revoked_at > :as_of)
ORDER BY exception_id
"""

# The artifact join is what scopes an obligation to a tenant: the obligation
# row itself carries no tenant, only the artifact family it belongs to.
_OBLIGATIONS_SQL = """
SELECT o.obligation_id, o.directive_id, o.obligation_state,
       o.applicability_digest, o.applicability_snapshot
FROM arc_mandatory_obligations o
JOIN arc_artifacts a ON a.artifact_id = o.artifact_id
WHERE o.effective_from <= :as_of
  AND (o.effective_until IS NULL OR o.effective_until > :as_of)
  AND (a.tenant_id IS NULL OR a.tenant_id = :tenant_id)
ORDER BY o.obligation_id
"""


def _uuid_set(raw: list[Any] | None) -> frozenset[uuid.UUID]:
    return frozenset(v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in raw or ())


def _str_set(raw: list[str] | None) -> frozenset[str]:
    return frozenset(raw or ())


def _vocab_set(raw: list[str] | None, vocab: type[TaskKind] | type[ActionClass]) -> frozenset[Any]:
    """Closed vocabularies, converted rather than trusted.

    The column already has a CHECK restricting it to the same set, so an
    unconvertible value means the two definitions have drifted -- which is
    worth a hard failure rather than a rule that silently stops matching.
    """
    return frozenset(vocab(v) for v in raw or ())


def _exception_in_scope(row: Any, manifest: TaskManifest) -> bool:  # noqa: ANN401 - SQLAlchemy Row
    """Whether an exception's declared narrowing covers this manifest.

    `apply_exceptions` checks the tenant and the active window and nothing
    else, so without this an exception approved for one narrow situation
    weakens its directive across the whole tenant. The schema goes to some
    trouble to stop a lower-scope exception smuggling in a narrowing it did
    not declare; that guard is only worth having if the read path honours
    the narrowing that *was* declared.

    Each selector is null-means-any, matching how an empty rule selector
    reads: the discriminating CHECK already requires the declared scope's
    selectors to be present, so a null here means the scope did not narrow
    on that dimension rather than that it narrowed to nothing.
    """
    if row.lower_scope_domain_id is not None and row.lower_scope_domain_id not in manifest.domain_ids:
        return False
    if row.lower_scope_capability_id is not None and row.lower_scope_capability_id not in manifest.capability_ids:
        return False
    if row.lower_scope_task_kind is not None and row.lower_scope_task_kind != str(manifest.task_kind):
        return False
    if row.lower_scope_action_class is not None and row.lower_scope_action_class not in {
        str(a) for a in manifest.requested_action_classes
    }:
        return False
    if row.lower_scope_environment is not None and row.lower_scope_environment != manifest.environment:
        return False
    return not (
        row.lower_scope_data_sensitivity is not None
        and row.lower_scope_data_sensitivity != manifest.data_sensitivity
    )


def _governing_rank(row: Any) -> tuple[int, int, str]:  # noqa: ANN401 - SQLAlchemy Row
    """Which of several rules over one directive governs it. Lower wins.

    Mandatory first, because an obligation reached by any mandatory rule is
    owed regardless of what else also reaches it -- treating it as optional
    would let a conflict degrade a bundle that should have blocked. Then
    widest authority, since that is the scope precedence ordering already
    uses. Then `rule_id`, purely so the answer is stable rather than
    dependent on row order.
    """
    return (
        0 if row.is_mandatory else 1,
        AuthorityScope(row.scope).rank,
        str(row.rule_id),
    )


def _directive_from_row(row: Any) -> Directive:  # noqa: ANN401 - SQLAlchemy Row
    """Build the domain directive from one joined row.

    A row that will not convert raises rather than being skipped. Dropping
    an unparseable *mandatory* directive would remove an obligation from the
    bundle and let the resolution come back `ready` -- the precise failure
    the durable tombstones exist to make impossible, arrived at from the
    other direction. The schema's CHECK constraints make this unreachable;
    if it ever fires, a loud failure is the correct outcome.
    """
    subject: ConflictSubjectKey | None = None
    constraint: NormalizedConstraint | None = None
    if row.conflict_subject_digest is not None:
        subject = ConflictSubjectKey(
            schema_version=row.conflict_key_schema_version,
            namespace=row.conflict_key_namespace,
            subject_selector=row.conflict_key_subject_selector,
            operation=row.conflict_key_operation,
            action_class=row.conflict_key_action_class,
            target_selector=row.conflict_key_target_selector,
        )
        constraint = NormalizedConstraint.parse(
            row.conflict_key_modality,
            row.conflict_key_constraint_operator,
            row.conflict_key_constraint_value,
        )

    return Directive(
        directive_id=row.directive_id,
        revision_id=row.revision_id,
        directive_type=DirectiveType(row.directive_type),
        source_anchor=row.source_anchor,
        conflict_subject=subject,
        constraint=constraint,
        satisfaction_mode=(
            SatisfactionMode(row.satisfaction_mode) if row.satisfaction_mode is not None else None
        ),
        verification_max_age_seconds=row.verification_max_age_seconds,
        accepted_verifier_classes=_str_set(row.accepted_verifier_classes),
        required_evidence_type=row.required_evidence_type,
        delegable_exception=row.delegable_exception,
    )


def _rule_from_row(row: Any) -> ApplicabilityRule:  # noqa: ANN401 - SQLAlchemy Row
    return ApplicabilityRule(
        rule_id=row.rule_id,
        revision_id=row.revision_id,
        scope=AuthorityScope(row.scope),
        is_mandatory=row.is_mandatory,
        target_tenant_id=row.target_tenant_id,
        capability_ids=_uuid_set(row.capability_ids),
        capability_labels=_str_set(row.capability_labels),
        domain_ids=_str_set(row.domain_ids),
        task_kinds=_vocab_set(row.task_kinds, TaskKind),
        action_classes=_vocab_set(row.action_classes, ActionClass),
        environments=_str_set(row.environments),
        data_sensitivity_tiers=_str_set(row.data_sensitivity_tiers),
        effective_from=row.rule_effective_from,
        effective_until=row.rule_effective_until,
    )


def _replacement_constraint(descriptor: Any) -> NormalizedConstraint | None:  # noqa: ANN401 - JSONB
    """Read the replacement constraint an exception substitutes in.

    Returns None rather than raising when the descriptor cannot be read.
    The keys are the ones `ExceptionService` persists -- `modality`,
    `constraint_operator`, `constraint_value` -- and none of them is checked
    on the way in.
    """
    if not isinstance(descriptor, dict):
        return None
    modality = descriptor.get("modality")
    operator = descriptor.get("constraint_operator")
    if not isinstance(modality, str) or not isinstance(operator, str):
        return None
    raw_value = descriptor.get("constraint_value")
    if raw_value is not None and not isinstance(raw_value, str):
        return None
    try:
        return NormalizedConstraint.parse(modality, operator, raw_value)
    except VocabularyError:
        return None


def _obligation_rule(snapshot: Any, obligation_id: uuid.UUID) -> ApplicabilityRule | None:  # noqa: ANN401 - JSONB
    """Rehydrate the snapshot into a rule so one matcher decides both.

    Returns None when the snapshot cannot be read. The caller must then treat
    the obligation as applying -- see `_obligations` for why erring the other
    way is the failure these rows exist to prevent.

    The snapshot carries no `capability_labels`, so neither does the rule
    built here, while `_rule_from_row` populates it for a candidate. That
    asymmetry is inert only because `rule_applies` matches on
    `capability_ids` and never reads labels. The day labels become a real
    selector it stops being inert and starts *widening*: an empty
    `capability_ids` makes the capability dimension match everything, so an
    obligation scoped to a label would apply to every capability. Wiring
    labels into matching therefore means adding them to the snapshot too --
    and note that changes the applicability digest, which is the dedup key,
    so existing obligations need migrating rather than just re-derived.
    """
    if not isinstance(snapshot, dict):
        # `applicability_snapshot` is JSONB with no shape constraint, so a
        # scalar or array is storable. Guarded rather than assumed: `.get`
        # on a list raises AttributeError, which is not a VocabularyError
        # and would escape as a 500.
        return None
    scope_raw = snapshot.get("scope")
    if not isinstance(scope_raw, str):
        return None
    try:
        target = snapshot.get("target_tenant_id")
        return ApplicabilityRule(
            # The snapshot records applicability, not identity; these two
            # IDs are unused by `rule_applies` and only need to be stable.
            rule_id=obligation_id,
            revision_id=obligation_id,
            scope=AuthorityScope(scope_raw),
            target_tenant_id=uuid.UUID(target) if isinstance(target, str) else None,
            capability_ids=_uuid_set(snapshot.get("capability_ids")),
            domain_ids=_str_set(snapshot.get("domain_ids")),
            task_kinds=_vocab_set(snapshot.get("task_kinds"), TaskKind),
            action_classes=_vocab_set(snapshot.get("action_classes"), ActionClass),
            environments=_str_set(snapshot.get("environments")),
            data_sensitivity_tiers=_str_set(snapshot.get("data_sensitivity_tiers")),
        )
    except (ValueError, TypeError):
        return None


class CorpusReader:
    """Reads one consistent snapshot of the governed corpus."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def assemble(
        self,
        *,
        tenant_id: uuid.UUID,
        manifest: TaskManifest,
        as_of: datetime.datetime,
    ) -> SelectionInput:
        """Build the input `select()` decides over.

        All three queries run in one session so they observe one snapshot;
        candidates drawn from a corpus that changed midway through would
        make the receipt describe a state that never existed.
        """
        async with self._session_factory() as session:
            candidates = await self._candidates(session, tenant_id=tenant_id, as_of=as_of)
            exceptions = await self._exceptions(
                session, tenant_id=tenant_id, manifest=manifest, as_of=as_of
            )
            obligations = await self._obligations(
                session, tenant_id=tenant_id, manifest=manifest, as_of=as_of
            )

        return SelectionInput(
            manifest=manifest,
            tenant_id=tenant_id,
            as_of=as_of,
            candidates=candidates,
            exceptions=exceptions,
            obligations=obligations,
        )

    async def _candidates(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, as_of: datetime.datetime
    ) -> tuple[tuple[Directive, ApplicabilityRule, datetime.datetime], ...]:
        rows = (
            await session.execute(
                text(_CANDIDATES_SQL),
                {"lifecycle": list(_SELECTABLE_LIFECYCLE), "as_of": as_of, "tenant_id": tenant_id},
            )
        ).all()

        # A revision may carry several directives and several rules, and the
        # join has no column tying one to the other -- so it produces every
        # pair. `collapse_successors` keeps one entry per directive identity
        # and breaks ties on `(revision_effective_from, revision_id)`, which
        # are *identical* for two pairs from the same revision. It therefore
        # keeps whichever arrived first, and `rule_id` is a random UUID.
        #
        # Left alone, that decides by coin flip whether a directive is
        # mandatory or optional, and which authority scope it takes its
        # precedence from. Choosing here makes it deterministic and picks the
        # safe arm: an obligation reached by any mandatory rule is owed, and
        # among equals the widest authority governs.
        governing: dict[tuple[uuid.UUID, uuid.UUID], Any] = {}
        for row in rows:
            key = (row.revision_id, row.directive_id)
            incumbent = governing.get(key)
            if incumbent is None or _governing_rank(row) < _governing_rank(incumbent):
                governing[key] = row

        return tuple(
            (_directive_from_row(row), _rule_from_row(row), row.revision_effective_from)
            for row in (governing[k] for k in sorted(governing, key=lambda k: (str(k[0]), str(k[1]))))
        )

    async def _exceptions(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        manifest: TaskManifest,
        as_of: datetime.datetime,
    ) -> tuple[ApprovedException, ...]:
        rows = (
            await session.execute(text(_EXCEPTIONS_SQL), {"tenant_id": tenant_id, "as_of": as_of})
        ).all()

        found: list[ApprovedException] = []
        for row in rows:
            if not _exception_in_scope(row, manifest):
                continue
            descriptor = row.replacement_conflict_descriptor
            if isinstance(descriptor, str):
                descriptor = json.loads(descriptor)

            replacement = _replacement_constraint(descriptor)
            if replacement is None:
                # Fail toward the stricter constraint. An exception weakens a
                # higher-scope directive, so an unreadable one must not be
                # applied -- the original stays in force, which is the
                # conservative direction. `approve_exception` validates only
                # that the descriptor names the same conflict subject, never
                # that it carries a parseable constraint, so this is a
                # reachable row rather than a defensive nicety.
                _log.warning(
                    "arc.corpus.unparseable_exception_descriptor: exception_id=%s", row.exception_id
                )
                continue

            found.append(
                ApprovedException(
                    exception_id=row.exception_id,
                    higher_scope_directive_id=row.higher_scope_directive_id,
                    higher_scope_revision_id=row.higher_scope_revision_id,
                    lower_scope_tenant_id=row.lower_scope_tenant_id,
                    replacement_constraint=replacement,
                    effective_from=row.effective_from,
                    effective_until=row.effective_until,
                    revoked_at=row.revoked_at,
                )
            )
        return tuple(found)

    async def _obligations(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        manifest: TaskManifest,
        as_of: datetime.datetime,
    ) -> tuple[MandatoryObligation, ...]:
        rows = (
            await session.execute(text(_OBLIGATIONS_SQL), {"tenant_id": tenant_id, "as_of": as_of})
        ).all()

        found: list[MandatoryObligation] = []
        for row in rows:
            snapshot = row.applicability_snapshot
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            rule = _obligation_rule(snapshot, row.obligation_id)
            if rule is None:
                # Kept, not skipped -- and this is the opposite of what an
                # unreadable *exception* gets, deliberately. The two objects
                # move the decision in opposite directions: dropping an
                # exception leaves the stricter original in force, while
                # dropping an obligation removes a blocker. An obligation is
                # a tombstone whose entire purpose is to be detectable when
                # nothing is there, so an unreadable one must still block.
                #
                # This costs nothing in the benign case: `is_missing` is
                # False for a satisfied obligation, so an unreadable but
                # satisfied row still does not block. Only unreadable *and*
                # unsatisfied does, which is the correct reading of "we
                # cannot tell whether this control is in force".
                _log.warning(
                    "arc.corpus.unreadable_obligation_snapshot: obligation_id=%s", row.obligation_id
                )
            elif not rule_applies(rule, manifest, tenant_id=tenant_id, as_of=as_of):
                continue
            found.append(
                MandatoryObligation(
                    obligation_id=row.obligation_id,
                    directive_id=row.directive_id,
                    obligation_state=row.obligation_state,
                    applicability_digest=row.applicability_digest,
                )
            )
        return tuple(found)


__all__ = ["CorpusReader"]
