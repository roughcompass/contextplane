"""Two entry points, one digest — pinned so they cannot drift apart.

`ApplicabilityDraft.digest()` and the obligations-refresh path both funnel
through `applicability_snapshot` + `applicability_digest`, one from a draft's
fields and one from database rows. Nothing currently calls the draft method in
production, but the approval-evidence work that will needs the two to be
byte-identical for the same applicability — a digest that differs by entry
point is a signature that verifies against one reading of the rule and not the
other, discovered at exactly the wrong time. This test builds the same
applicability both ways and pins the equality, including the field shapes
where drift is likeliest: a row's array columns come back as lists while the
draft carries tuples, and the scope arrives as an enum on one side and a bare
string on the other.
"""

from __future__ import annotations

import datetime
import uuid

from registry.arc.service.artifact_integrity import applicability_digest, applicability_snapshot
from registry.arc.service.artifact_materialisation import ApplicabilityDraft
from registry.arc.types import AuthorityScope

_EFFECTIVE = datetime.datetime(2026, 8, 4, tzinfo=datetime.UTC)


def _row_shaped_digest(
    *,
    scope: str,
    target_tenant_id: uuid.UUID | None,
    capability_ids: list[str],
    domain_ids: list[str],
    task_kinds: list[str],
    action_classes: list[str],
    environments: list[str],
    data_sensitivity_tiers: list[str],
) -> str:
    """The obligations-refresh recompute, shape for shape: string scope, list
    columns — exactly what a database row hands back."""
    snapshot = applicability_snapshot(
        scope=scope,
        target_tenant_id=target_tenant_id,
        capability_ids=capability_ids,
        domain_ids=domain_ids,
        task_kinds=task_kinds,
        action_classes=action_classes,
        environments=environments,
        data_sensitivity_tiers=data_sensitivity_tiers,
    )
    return applicability_digest(snapshot)


def test_draft_and_row_recompute_agree_on_a_full_applicability() -> None:
    tenant = uuid.uuid4()
    cap_a, cap_b = uuid.uuid4(), uuid.uuid4()
    draft = ApplicabilityDraft(
        scope=AuthorityScope.TENANT,
        effective_from=_EFFECTIVE,
        target_tenant_id=tenant,
        capability_ids=(cap_a, cap_b),
        domain_ids=("payments",),
        task_kinds=("code_change",),
        action_classes=("write",),
        environments=("production",),
        data_sensitivity_tiers=("restricted",),
    )
    assert draft.digest() == _row_shaped_digest(
        scope=str(AuthorityScope.TENANT),
        target_tenant_id=tenant,
        capability_ids=[cap_a, cap_b],
        domain_ids=["payments"],
        task_kinds=["code_change"],
        action_classes=["write"],
        environments=["production"],
        data_sensitivity_tiers=["restricted"],
    )


def test_draft_and_row_recompute_agree_on_the_sparse_global_case() -> None:
    draft = ApplicabilityDraft(
        scope=AuthorityScope.GLOBAL,
        effective_from=_EFFECTIVE,
        target_tenant_id=None,
        capability_ids=(),
        domain_ids=(),
        task_kinds=(),
        action_classes=(),
        environments=(),
        data_sensitivity_tiers=(),
    )
    assert draft.digest() == _row_shaped_digest(
        scope=str(AuthorityScope.GLOBAL),
        target_tenant_id=None,
        capability_ids=[],
        domain_ids=[],
        task_kinds=[],
        action_classes=[],
        environments=[],
        data_sensitivity_tiers=[],
    )


def test_reach_fields_change_the_digest() -> None:
    """The digest must cover reach, not just content-adjacent fields — two
    applicabilities differing only in capability set must not collide."""
    base = dict(
        scope=str(AuthorityScope.TENANT),
        target_tenant_id=uuid.uuid4(),
        capability_ids=[uuid.uuid4()],
        domain_ids=[],
        task_kinds=[],
        action_classes=[],
        environments=[],
        data_sensitivity_tiers=[],
    )
    widened = dict(base, capability_ids=[*base["capability_ids"], uuid.uuid4()])  # type: ignore[list-item, misc]
    assert _row_shaped_digest(**base) != _row_shaped_digest(**widened)  # type: ignore[arg-type]
