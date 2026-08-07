"""Parametrized SQL for submission's materialisation transaction: inserting
the draft `arc_revisions` row a proposal version submits into, the
compare-and-swap that freezes the version and writes the bijection link to
it, and -- once that compare-and-swap is won -- the candidate's own
`directives[]`/`applicability[]` elements into `arc_directives`/
`arc_applicability_rules`. Without the last two, a revision authored,
approved, and activated through this surface would have nothing for corpus
assembly or selection to serve; `artifact_materialisation.py`'s own
`_MaterialisationMixin` already writes both tables for the older, disjoint
"already-approved upstream revision" path, and this module's `insert_
directive`/`insert_applicability_rule` are this surface's own writers for
the same two tables, not a second implementation of that one's logic --
see `submission.py::ArtifactMaterialisationService._directive_row`'s own
docstring for the one field each writer must derive rather than copy, and
for why this surface's candidate shape cannot simply be adapted to call
the older writer's own dataclasses directly.

Sibling of `queries/proposal.py`, deliberately not added to it: that module
owns the proposal aggregate (`arc_authoring_proposals`, `arc_authoring_
proposal_versions`) and is under active development for candidate-semantics
storage. This module owns every write that crosses from a proposal version
into `arc_revisions` and its two child tables, so the two files never need
to change for the same reason. Every function here takes an already-open
`AsyncSession` and commits nothing itself, matching `queries/proposal.py`'s
own convention: the caller controls the transaction boundary.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclasses.dataclass(frozen=True)
class DraftRevision:
    """The columns `insert_draft_revision` writes, named once so the
    service module building them and the SQL consuming them cannot drift
    apart silently through a growing keyword-argument list."""

    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    tenant_id: uuid.UUID | None
    source_system: str
    source_canonical_locator: str
    source_revision_locator: str
    content_digest: str
    effective_from: datetime.datetime
    review_expires_at: datetime.datetime
    detail_audience: str
    freshness_basis: str
    content_classification: str
    content_retention_until: datetime.datetime
    created_at: datetime.datetime


async def insert_draft_revision(session: AsyncSession, draft: DraftRevision) -> None:
    """Insert the one `arc_revisions` row a submission materialises.

    `lifecycle_state` is not a parameter: every row this function writes is
    a fresh draft, and the column's own DEFAULT already says so -- naming it
    explicitly here would just be a second place that could someday say
    something else. `content_storage_mode` is always `'none'`: the
    candidate this row was materialised from lives on `arc_authoring_
    proposal_versions.semantics`, read through the bijection this same
    transaction writes, not duplicated into a body column here.
    """
    await session.execute(
        text(
            "INSERT INTO arc_revisions ("
            "  revision_id, artifact_id, tenant_id, source_system, source_canonical_locator,"
            "  source_revision_locator, content_digest, effective_from, review_expires_at,"
            "  detail_audience, freshness_basis, content_classification, content_retention_until,"
            "  content_storage_mode, created_at"
            ") VALUES ("
            "  :revision_id, :artifact_id, :tenant_id, :source_system, :source_canonical_locator,"
            "  :source_revision_locator, :content_digest, :effective_from, :review_expires_at,"
            "  :detail_audience, :freshness_basis, :content_classification, :content_retention_until,"
            "  'none', :created_at"
            ")"
        ),
        {
            "revision_id": draft.revision_id,
            "artifact_id": draft.artifact_id,
            "tenant_id": draft.tenant_id,
            "source_system": draft.source_system,
            "source_canonical_locator": draft.source_canonical_locator,
            "source_revision_locator": draft.source_revision_locator,
            "content_digest": draft.content_digest,
            "effective_from": draft.effective_from,
            "review_expires_at": draft.review_expires_at,
            "detail_audience": draft.detail_audience,
            "freshness_basis": draft.freshness_basis,
            "content_classification": draft.content_classification,
            "content_retention_until": draft.content_retention_until,
            "created_at": draft.created_at,
        },
    )


@dataclasses.dataclass(frozen=True)
class FrozenVersion:
    """What `freeze_and_link` hands back on a won compare-and-swap."""

    proposal_id: uuid.UUID
    proposal_version: int
    state: str
    revision_id: uuid.UUID
    frozen_at: datetime.datetime


async def freeze_and_link(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    revision_id: uuid.UUID,
    now: datetime.datetime,
) -> FrozenVersion | None:
    """The compare-and-swap: `open` with no prior freeze, to `submitted`
    with the bijection link set, in the same statement that decides it.

    Mirrors `queries.proposal.transition_version`'s own shape -- a bare
    `UPDATE ... WHERE ... RETURNING`, no separate `SELECT ... FOR UPDATE` --
    for the same reason: the `WHERE` clause's row lock at execution time is
    the whole mechanism, and a caller racing this statement against another
    submit always resolves to exactly one winner. Returns `None` on a lost
    race or an already-frozen row; the caller decides what that means.
    """
    row = (
        await session.execute(
            text(
                "UPDATE arc_authoring_proposal_versions SET "
                "  state = 'submitted', frozen_at = :now, revision_id = :revision_id "
                "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version "
                "  AND state = 'open' AND frozen_at IS NULL "
                "RETURNING proposal_id, proposal_version, state, revision_id, frozen_at"
            ),
            {
                "proposal_id": proposal_id,
                "proposal_version": proposal_version,
                "revision_id": revision_id,
                "now": now,
            },
        )
    ).one_or_none()
    if row is None:
        return None
    return FrozenVersion(
        proposal_id=row.proposal_id,
        proposal_version=row.proposal_version,
        state=row.state,
        revision_id=row.revision_id,
        frozen_at=row.frozen_at,
    )


@dataclasses.dataclass(frozen=True)
class MaterialisedDirective:
    """One `arc_directives` row plus the `arc_directive_identities` row its
    foreign key requires, both written by `insert_directive` in one call.

    Named once, matching `DraftRevision`'s own convention, so the service
    module building one of these and the SQL consuming it cannot drift
    apart silently through a growing keyword-argument list.
    `registry.arc.service.submission.ArtifactMaterialisationService.
    _directive_row` is the one place that builds this from a candidate's
    `directives[]` element -- see that method's own docstring for two
    fields (`conflict_key_schema_version`, `directive_type`/`satisfaction_
    mode`) that are not a plain copy.
    """

    directive_id: uuid.UUID
    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    directive_type: str
    compact_statement_plaintext: str
    source_anchor: str
    conflict_key_schema_version: str | None
    conflict_key_namespace: str | None
    conflict_key_subject_selector: str | None
    conflict_key_operation: str | None
    conflict_key_action_class: str | None
    conflict_key_target_selector: str | None
    conflict_key_modality: str | None
    conflict_key_constraint_operator: str | None
    conflict_key_constraint_value: str | None
    conflict_subject_digest: str | None
    satisfaction_mode: str | None
    verification_max_age_seconds: int | None
    accepted_verifier_classes: tuple[str, ...] | None
    accepted_verifier_ids: tuple[uuid.UUID, ...] | None
    required_evidence_type: str | None
    delegable_exception: bool


async def insert_directive(session: AsyncSession, directive: MaterialisedDirective) -> None:
    """Write one candidate directive into `arc_directives`, creating its
    stable identity row first (`ON CONFLICT DO NOTHING`, matching
    `artifact_materialisation.py`'s own `_insert_directive`: a directive
    keeps one identity across revisions, so a later revision naming the
    same `directive_id` must find the identity row already there rather
    than fail on it).

    `tenant_id` is read back from the just-inserted `arc_revisions` row
    (`SELECT ... FROM arc_revisions r WHERE r.revision_id = :revision_id`)
    rather than taken as its own parameter: the composite foreign key
    `fk_arc_directives_revision_tenant` requires `(revision_id, tenant_id)`
    to match the parent revision exactly, and reading it back structurally
    guarantees that rather than trusting a second, independently supplied
    value not to have drifted from it.
    """
    await session.execute(
        text(
            "INSERT INTO arc_directive_identities (directive_id, artifact_id) "
            "VALUES (:directive_id, :artifact_id) ON CONFLICT (directive_id) DO NOTHING"
        ),
        {"directive_id": directive.directive_id, "artifact_id": directive.artifact_id},
    )
    await session.execute(
        text(
            "INSERT INTO arc_directives ("
            "  directive_id, revision_id, tenant_id, directive_type, compact_statement_plaintext,"
            "  source_anchor, conflict_key_schema_version, conflict_key_namespace,"
            "  conflict_key_subject_selector, conflict_key_operation, conflict_key_action_class,"
            "  conflict_key_target_selector, conflict_key_modality, conflict_key_constraint_operator,"
            "  conflict_key_constraint_value, conflict_subject_digest, satisfaction_mode,"
            "  verification_max_age_seconds, accepted_verifier_classes, accepted_verifier_ids,"
            "  required_evidence_type, delegable_exception"
            ") SELECT :directive_id, :revision_id, r.tenant_id, :directive_type, :compact_statement_plaintext,"
            "         :source_anchor, :conflict_key_schema_version, :conflict_key_namespace,"
            "         :conflict_key_subject_selector, :conflict_key_operation, :conflict_key_action_class,"
            "         :conflict_key_target_selector, :conflict_key_modality, :conflict_key_constraint_operator,"
            "         :conflict_key_constraint_value, :conflict_subject_digest, :satisfaction_mode,"
            "         :verification_max_age_seconds, :accepted_verifier_classes, :accepted_verifier_ids,"
            "         :required_evidence_type, :delegable_exception "
            "  FROM arc_revisions r WHERE r.revision_id = :revision_id"
        ),
        {
            "directive_id": directive.directive_id,
            "revision_id": directive.revision_id,
            "directive_type": directive.directive_type,
            "compact_statement_plaintext": directive.compact_statement_plaintext,
            "source_anchor": directive.source_anchor,
            "conflict_key_schema_version": directive.conflict_key_schema_version,
            "conflict_key_namespace": directive.conflict_key_namespace,
            "conflict_key_subject_selector": directive.conflict_key_subject_selector,
            "conflict_key_operation": directive.conflict_key_operation,
            "conflict_key_action_class": directive.conflict_key_action_class,
            "conflict_key_target_selector": directive.conflict_key_target_selector,
            "conflict_key_modality": directive.conflict_key_modality,
            "conflict_key_constraint_operator": directive.conflict_key_constraint_operator,
            "conflict_key_constraint_value": directive.conflict_key_constraint_value,
            "conflict_subject_digest": directive.conflict_subject_digest,
            "satisfaction_mode": directive.satisfaction_mode,
            "verification_max_age_seconds": directive.verification_max_age_seconds,
            "accepted_verifier_classes": (
                list(directive.accepted_verifier_classes) if directive.accepted_verifier_classes else None
            ),
            "accepted_verifier_ids": (
                list(directive.accepted_verifier_ids) if directive.accepted_verifier_ids else None
            ),
            "required_evidence_type": directive.required_evidence_type,
            "delegable_exception": directive.delegable_exception,
        },
    )


@dataclasses.dataclass(frozen=True)
class MaterialisedApplicabilityRule:
    """One `arc_applicability_rules` row, direct field-for-field from the
    candidate's own `applicability[]` element -- no vocabulary mismatch
    here, unlike `MaterialisedDirective`'s `directive_type`/`satisfaction_
    mode`: the candidate's `scope` enum already matches this table's own
    closed CHECK exactly.
    """

    rule_id: uuid.UUID
    revision_id: uuid.UUID
    scope: str
    target_tenant_id: uuid.UUID | None
    capability_ids: tuple[uuid.UUID, ...] | None
    capability_labels: tuple[str, ...] | None
    domain_ids: tuple[str, ...] | None
    task_kinds: tuple[str, ...] | None
    action_classes: tuple[str, ...] | None
    environments: tuple[str, ...] | None
    data_sensitivity_tiers: tuple[str, ...] | None
    effective_from: datetime.datetime
    effective_until: datetime.datetime | None
    is_mandatory: bool


async def insert_applicability_rule(session: AsyncSession, rule: MaterialisedApplicabilityRule) -> None:
    """Write one candidate applicability rule into `arc_applicability_rules`.

    `tenant_id` is read back from `arc_revisions` for the identical reason
    `insert_directive` reads it back rather than taking its own parameter:
    `fk_arc_rules_revision_tenant` is the same shape of composite foreign
    key, over the same parent row.
    """
    await session.execute(
        text(
            "INSERT INTO arc_applicability_rules ("
            "  rule_id, revision_id, tenant_id, scope, target_tenant_id, capability_ids, capability_labels,"
            "  domain_ids, task_kinds, action_classes, environments, data_sensitivity_tiers,"
            "  effective_from, effective_until, is_mandatory"
            ") SELECT :rule_id, :revision_id, r.tenant_id, :scope, :target_tenant_id, :capability_ids,"
            "         :capability_labels, :domain_ids, :task_kinds, :action_classes, :environments,"
            "         :data_sensitivity_tiers, :effective_from, :effective_until, :is_mandatory "
            "  FROM arc_revisions r WHERE r.revision_id = :revision_id"
        ),
        {
            "rule_id": rule.rule_id,
            "revision_id": rule.revision_id,
            "scope": rule.scope,
            "target_tenant_id": rule.target_tenant_id,
            "capability_ids": list(rule.capability_ids) if rule.capability_ids else None,
            "capability_labels": list(rule.capability_labels) if rule.capability_labels else None,
            "domain_ids": list(rule.domain_ids) if rule.domain_ids else None,
            "task_kinds": list(rule.task_kinds) if rule.task_kinds else None,
            "action_classes": list(rule.action_classes) if rule.action_classes else None,
            "environments": list(rule.environments) if rule.environments else None,
            "data_sensitivity_tiers": (list(rule.data_sensitivity_tiers) if rule.data_sensitivity_tiers else None),
            "effective_from": rule.effective_from,
            "effective_until": rule.effective_until,
            "is_mandatory": rule.is_mandatory,
        },
    )


__all__ = [
    "DraftRevision",
    "FrozenVersion",
    "MaterialisedApplicabilityRule",
    "MaterialisedDirective",
    "freeze_and_link",
    "insert_applicability_rule",
    "insert_directive",
    "insert_draft_revision",
]
