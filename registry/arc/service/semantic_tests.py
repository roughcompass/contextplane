"""Executes observation-class predicates and freezes their inputs/results.

A semantic test names an `arc_observation_class_predicate_v1` manifest --
the class of task manifests a candidate's applicability rules are supposed
to cover (task kinds, action classes, environments, sensitivity tiers,
capabilities, domains; every dimension optional and set-shaped). Running a
test asks "does at least one of the candidate's applicability rules cover
this class" and freezes the answer next to the manifest that produced it,
in `arc_authoring_semantic_tests`.

**The matching engine is a set-overlap check, not a new algorithm.** A
predicate's declared dimensions and a rule's declared selector dimensions
share the same shape (both optional lists), because `ObservationClassPredicate`
is deliberately the same six dimensions `registry.arc.service.selection.
rule_applies` already matches a *concrete* task manifest against -- this
module's `rule_covers_predicate` is the same wildcard-and-intersection
logic applied to two *class* descriptions instead of one rule and one
instance, since a predicate names a set of possible manifests, not one.
An empty/absent dimension on either side is a wildcard for that dimension
(a rule that never named a sensitivity tier applies regardless of it; a
predicate that never named one is asking about every tier).

**`expected` is always `{"matched": true}`.** The wire contract
(`SemanticTestRequest`) gives the caller no way to assert "this predicate
should NOT be covered" -- there is no `expected` field on the request, only
on the response. A semantic test is therefore always the assertion "the
candidate covers this class of task"; `actual` is what evaluating the
predicate against the candidate's rules really produced, and `passed` is
whether the two agree. The one fixed canonical example this contract ships
(`tests/conformance/snapshots/arc_authoring_examples.json`) shows exactly
this shape.

**Where "the candidate's applicability rules" come from.** `ProposalPatchRequest.
semantics` names the full candidate document, but -- see `provenance.py`'s
own docstring -- no table in this phase's migration has a column to store
it durably against a proposal version. `run()` therefore reads the
version's *reviewed baseline revision*'s live `arc_applicability_rules`
instead: real, already-persisted data, not an invented stand-in, and the
best approximation available of "what this artifact's applicability
currently is" while the edited candidate itself has nowhere to live. A
brand-new proposal with no baseline evaluates against an empty rule set,
which correctly reports every non-wildcard predicate as unmatched rather
than fabricating coverage that was never declared. This is a real
limitation of what this task's schema supports, not a hidden shortcut --
see the module docstring on `provenance.py` and this task's own report for
the missing column that would let a call source the actual edited
candidate instead.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc.service import audit_outbox
from registry.arc.service.authorization import ArcAuthorizationService, ArtifactScope
from registry.arc.service.proposal import ProposalStateConflict
from registry.arc.service.queries import proposal as proposal_queries
from registry.arc.service.queries import provenance as queries
from registry.arc.types import ArcRequestContext, AuthorityScope
from registry.audit import actions
from registry.exceptions import NotFoundError, RegistryError
from registry.types import Clock

# ---------------------------------------------------------------------------
# The matching engine
# ---------------------------------------------------------------------------

#: `(predicate field name, rule field name)` for the six shared selector
#: dimensions. Transcribed once here rather than assumed equal, because the
#: two shapes use different names for the same dimension in three of six
#: cases (`task_kind`/`task_kinds`, `requested_action_classes`/
#: `action_classes`, `environment`/`environments`,
#: `data_sensitivity_tier`/`data_sensitivity_tiers`).
_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("task_kind", "task_kinds"),
    ("requested_action_classes", "action_classes"),
    ("environment", "environments"),
    ("data_sensitivity_tier", "data_sensitivity_tiers"),
    ("capability_ids", "capability_ids"),
    ("domain_ids", "domain_ids"),
)


def _as_set(value: Any) -> frozenset[str] | None:  # noqa: ANN401 - normalizes a JSON-shaped list/None into a comparable set
    if not value:
        return None
    return frozenset(str(v) for v in value)


def rule_covers_predicate(rule: Mapping[str, Any], predicate: Mapping[str, Any]) -> bool:
    """Whether *rule* covers the class of manifests *predicate* describes.

    Per dimension: an absent/empty set on either side is a wildcard for
    that dimension and never blocks a match; when both sides name values,
    they must share at least one common value. A rule covers a predicate
    only when every dimension passes -- one non-overlapping, non-wildcard
    dimension is enough to refuse the whole rule.
    """
    for predicate_key, rule_key in _DIMENSIONS:
        predicate_values = _as_set(predicate.get(predicate_key))
        if predicate_values is None:
            continue
        rule_values = _as_set(rule.get(rule_key))
        if rule_values is None:
            continue
        if not (predicate_values & rule_values):
            return False
    return True


def evaluate_predicate(predicate: Mapping[str, Any], rules: Sequence[Mapping[str, Any]]) -> bool:
    """`True` when at least one of *rules* covers *predicate*.

    An empty *rules* sequence always evaluates to `False`: with nothing
    declared, nothing has been shown to cover anything, regardless of how
    wildcard-shaped the predicate itself is.
    """
    return any(rule_covers_predicate(rule, predicate) for rule in rules)


def _entry_dict(value: Mapping[str, Any] | Any) -> dict[str, Any]:  # noqa: ANN401 - accepts a pydantic model or a plain mapping
    if isinstance(value, Mapping):
        return dict(value)
    return dict(value.model_dump())


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SemanticTestResult:
    test_id: str
    passed: bool
    expected: dict[str, Any]
    actual: dict[str, Any]


def _result(row: queries.SemanticTestRow) -> SemanticTestResult:
    return SemanticTestResult(test_id=row.test_id, passed=row.passed, expected=row.expected, actual=row.actual)


def _scope(tenant_id: uuid.UUID | None) -> ArtifactScope:
    scope = AuthorityScope.GLOBAL if tenant_id is None else AuthorityScope.TENANT
    return ArtifactScope(scope=scope, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SemanticTestService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorization: ArcAuthorizationService,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._clock = clock

    async def run(
        self,
        ctx: ArcRequestContext,
        proposal_id: uuid.UUID,
        proposal_version: int,
        *,
        tests: Sequence[Any],
    ) -> tuple[SemanticTestResult, ...]:
        """Execute each test's predicate and freeze its input/result.

        Legal only while the version is `open`, matching `edit()`'s own
        gate in `provenance.py`: semantic tests verify a candidate before
        submission, not after.
        """
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_write_artifact(ctx, _scope(family.tenant_id))
            if version.state != "open":
                msg = (
                    f"proposal version {proposal_id}/{proposal_version} is not open for semantic tests "
                    f"(state={version.state!r})"
                )
                raise ProposalStateConflict(msg)

            rules: list[dict[str, Any]] = []
            if version.reviewed_baseline_revision_id is not None:
                rule_rows = await queries.load_applicability_rules_for_revision(
                    session, version.reviewed_baseline_revision_id
                )
                rules = [dataclasses.asdict(row) for row in rule_rows]

            results: list[SemanticTestResult] = []
            for case in tests:
                case_dict = _entry_dict(case)
                test_id = case_dict["test_id"]
                manifest = _entry_dict(case_dict["manifest"])
                matched = evaluate_predicate(manifest, rules)
                actual = {"matched": matched}
                expected = {"matched": True}
                passed = actual == expected
                await queries.upsert_semantic_test(
                    session,
                    proposal_id=proposal_id,
                    proposal_version=proposal_version,
                    test_id=test_id,
                    manifest=manifest,
                    passed=passed,
                    expected=expected,
                    actual=actual,
                    executed_at=now,
                )
                results.append(SemanticTestResult(test_id=test_id, passed=passed, expected=expected, actual=actual))

            await audit_outbox.emit(
                session,
                tenant_id=ctx.tenant_id,
                event_type=actions.ARC_SEMANTIC_TESTS_EXECUTED,
                payload={
                    "proposal_id": str(proposal_id),
                    "proposal_version": proposal_version,
                    "test_ids": sorted(r.test_id for r in results),
                    "passed_count": sum(1 for r in results if r.passed),
                },
            )
            return tuple(results)

    async def list_for_version(
        self, ctx: ArcRequestContext, proposal_id: uuid.UUID, proposal_version: int
    ) -> tuple[SemanticTestResult, ...]:
        async with self._session_factory() as session:
            version = await proposal_queries.load_version(session, proposal_id, proposal_version)
            if version is None:
                raise NotFoundError(f"proposal version {proposal_id}/{proposal_version} not found")
            family = await proposal_queries.load_family(session, version.artifact_id)
            if family is None:
                raise RegistryError(f"proposal version {proposal_id}/{proposal_version} references a vanished family")
            self._authorization.assert_can_read_artifact(ctx, _scope(family.tenant_id))
            rows = await queries.load_semantic_tests(session, proposal_id, proposal_version)
        return tuple(_result(row) for row in rows)


__all__ = [
    "SemanticTestResult",
    "SemanticTestService",
    "evaluate_predicate",
    "rule_covers_predicate",
]
