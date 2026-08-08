"""Parametrized SQL for `arc_authoring_field_provenance` and
`arc_authoring_semantic_tests`.

Sibling of `provenance.py` and `semantic_tests.py`, matching `queries/
proposal.py`'s own convention: every function takes an already-open
`AsyncSession` and issues exactly the statements its name promises, so the
caller controls what commits together.

Both tables share this one queries module rather than getting one each --
`provenance.py`'s and `semantic_tests.py`'s own path scope names only this
file, and the two tables are the same proposal-version aggregate `queries/
proposal.py` already treats as one cohesive unit.

**Per-field survival is the point of `upsert_field_provenance`.** It writes
exactly one `(proposal_id, proposal_version, field_path)` row via `INSERT
... ON CONFLICT ... DO UPDATE`, never a delete-then-reinsert of the whole
set for a version. A `PATCH` that only names field B must not disturb
field A's already-recorded row -- a "replace everything" write would lose
field A's provenance the moment a caller edits one field at a time, which
is exactly the loss the primary key's per-field granularity exists to
prevent.

**Frozen semantic-test rows follow the same shape for a different reason.**
`upsert_semantic_test` also keys on `(proposal_id, proposal_version,
test_id)` and overwrites in place -- but here the point is the opposite of
survival: re-running `test_id` with a *changed* `manifest` must overwrite
the prior frozen `expected`/`actual`/`passed` with the new computation, not
leave a stale row a later read could mistake for still describing the new
input. Freezing means "this row is exactly what this input computed to",
not "this row, once written, never changes".
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FieldProvenanceRow:
    proposal_id: uuid.UUID
    proposal_version: int
    field_path: str
    provenance_class: str
    source_evidence_id: uuid.UUID | None
    source_anchor: str | None
    excerpt_digest: str | None
    author_issuer: str | None
    author_subject: str | None
    author_role: str | None
    derivation_profile: str | None
    created_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class SemanticTestRow:
    proposal_id: uuid.UUID
    proposal_version: int
    test_id: str
    manifest: dict[str, Any]
    passed: bool
    expected: dict[str, Any]
    actual: dict[str, Any]
    executed_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class ApplicabilityRuleRow:
    """Read-only projection of one live `arc_applicability_rules` row.

    Not part of the provenance/semantic-test aggregate -- this table belongs
    to the already-materialized artifact/revision surface (`contextplane/arc/
    models.py`'s `ArcApplicabilityRule`). `semantic_tests.py` reads it,
    never writes it, to source the best currently-available stand-in for
    "the candidate's applicability rules" while a PATCHed candidate has
    nowhere durable to live -- see that module's docstring for why.
    """

    rule_id: uuid.UUID
    scope: str
    target_tenant_id: uuid.UUID | None
    capability_ids: list[uuid.UUID] | None
    capability_labels: list[str] | None
    domain_ids: list[str] | None
    task_kinds: list[str] | None
    action_classes: list[str] | None
    environments: list[str] | None
    data_sensitivity_tiers: list[str] | None


_FIELD_PROVENANCE_COLUMNS = (
    "proposal_id, proposal_version, field_path, provenance_class, source_evidence_id, source_anchor, "
    "excerpt_digest, author_issuer, author_subject, author_role, derivation_profile, created_at"
)


def _field_provenance_row(row: Any) -> FieldProvenanceRow:  # noqa: ANN401 - a raw SQLAlchemy Row has no narrower public type
    return FieldProvenanceRow(
        proposal_id=row.proposal_id,
        proposal_version=row.proposal_version,
        field_path=row.field_path,
        provenance_class=row.provenance_class,
        source_evidence_id=row.source_evidence_id,
        source_anchor=row.source_anchor,
        excerpt_digest=row.excerpt_digest,
        author_issuer=row.author_issuer,
        author_subject=row.author_subject,
        author_role=row.author_role,
        derivation_profile=row.derivation_profile,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# arc_authoring_field_provenance
# ---------------------------------------------------------------------------


async def upsert_field_provenance(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    field_path: str,
    provenance_class: str,
    source_evidence_id: uuid.UUID | None,
    source_anchor: str | None,
    excerpt_digest: str | None,
    author_issuer: str | None,
    author_subject: str | None,
    author_role: str | None,
    derivation_profile: str | None,
    created_at: datetime.datetime,
) -> None:
    """Write exactly one field's provenance row, in place.

    `ON CONFLICT (proposal_id, proposal_version, field_path) DO UPDATE`
    replaces this one row's own columns -- rows for every other
    `field_path` on this version are untouched by construction, since
    they are never named in the `WHERE` this statement resolves against.
    """
    await session.execute(
        text(
            "INSERT INTO arc_authoring_field_provenance ("
            "  proposal_id, proposal_version, field_path, provenance_class, source_evidence_id,"
            "  source_anchor, excerpt_digest, author_issuer, author_subject, author_role,"
            "  derivation_profile, created_at"
            ") VALUES ("
            "  :proposal_id, :proposal_version, :field_path, :provenance_class, :source_evidence_id,"
            "  :source_anchor, :excerpt_digest, :author_issuer, :author_subject, :author_role,"
            "  :derivation_profile, :created_at"
            ") ON CONFLICT (proposal_id, proposal_version, field_path) DO UPDATE SET"
            "  provenance_class = EXCLUDED.provenance_class,"
            "  source_evidence_id = EXCLUDED.source_evidence_id,"
            "  source_anchor = EXCLUDED.source_anchor,"
            "  excerpt_digest = EXCLUDED.excerpt_digest,"
            "  author_issuer = EXCLUDED.author_issuer,"
            "  author_subject = EXCLUDED.author_subject,"
            "  author_role = EXCLUDED.author_role,"
            "  derivation_profile = EXCLUDED.derivation_profile,"
            "  created_at = EXCLUDED.created_at"
        ),
        {
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "field_path": field_path,
            "provenance_class": provenance_class,
            "source_evidence_id": source_evidence_id,
            "source_anchor": source_anchor,
            "excerpt_digest": excerpt_digest,
            "author_issuer": author_issuer,
            "author_subject": author_subject,
            "author_role": author_role,
            "derivation_profile": derivation_profile,
            "created_at": created_at,
        },
    )


async def load_field_provenance(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int
) -> list[FieldProvenanceRow]:
    rows = await session.execute(
        text(
            f"SELECT {_FIELD_PROVENANCE_COLUMNS} FROM arc_authoring_field_provenance "  # noqa: S608 - constant column list, bound parameters below
            "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version ORDER BY field_path"
        ),
        {"proposal_id": proposal_id, "proposal_version": proposal_version},
    )
    return [_field_provenance_row(row) for row in rows]


async def load_one_field_provenance(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int, field_path: str
) -> FieldProvenanceRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_FIELD_PROVENANCE_COLUMNS} FROM arc_authoring_field_provenance "  # noqa: S608 - constant column list, bound parameters below
                "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version AND field_path = :field_path"
            ),
            {"proposal_id": proposal_id, "proposal_version": proposal_version, "field_path": field_path},
        )
    ).one_or_none()
    if row is None:
        return None
    return _field_provenance_row(row)


# ---------------------------------------------------------------------------
# arc_authoring_semantic_tests
# ---------------------------------------------------------------------------


async def upsert_semantic_test(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    proposal_version: int,
    test_id: str,
    manifest: dict[str, Any],
    passed: bool,
    expected: dict[str, Any],
    actual: dict[str, Any],
    executed_at: datetime.datetime,
) -> None:
    """Freeze one test's input/result, in place.

    Overwrites the prior row for this exact `test_id` -- see this module's
    own docstring for why that is the correct behavior for a *frozen*
    record: the freeze is of "this input produced this result", and a
    changed `manifest` must produce a new frozen pair, not silently keep
    reporting the old `actual` under a stale, now-mismatched input.
    """
    await session.execute(
        text(
            "INSERT INTO arc_authoring_semantic_tests ("
            "  proposal_id, proposal_version, test_id, manifest, passed, expected, actual, executed_at"
            ") VALUES ("
            "  :proposal_id, :proposal_version, :test_id, CAST(:manifest AS JSONB), :passed,"
            "  CAST(:expected AS JSONB), CAST(:actual AS JSONB), :executed_at"
            ") ON CONFLICT (proposal_id, proposal_version, test_id) DO UPDATE SET"
            "  manifest = EXCLUDED.manifest,"
            "  passed = EXCLUDED.passed,"
            "  expected = EXCLUDED.expected,"
            "  actual = EXCLUDED.actual,"
            "  executed_at = EXCLUDED.executed_at"
        ),
        {
            "proposal_id": proposal_id,
            "proposal_version": proposal_version,
            "test_id": test_id,
            "manifest": _json(manifest),
            "passed": passed,
            "expected": _json(expected),
            "actual": _json(actual),
            "executed_at": executed_at,
        },
    )


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


async def load_semantic_tests(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int
) -> list[SemanticTestRow]:
    rows = await session.execute(
        text(
            "SELECT proposal_id, proposal_version, test_id, manifest, passed, expected, actual, executed_at "
            "FROM arc_authoring_semantic_tests "
            "WHERE proposal_id = :proposal_id AND proposal_version = :proposal_version ORDER BY test_id"
        ),
        {"proposal_id": proposal_id, "proposal_version": proposal_version},
    )
    return [
        SemanticTestRow(
            proposal_id=row.proposal_id,
            proposal_version=row.proposal_version,
            test_id=row.test_id,
            manifest=row.manifest,
            passed=row.passed,
            expected=row.expected,
            actual=row.actual,
            executed_at=row.executed_at,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# arc_applicability_rules -- read-only; see `ApplicabilityRuleRow`'s
# docstring for why `semantic_tests.py` reads this table.
# ---------------------------------------------------------------------------


async def load_applicability_rules_for_revision(
    session: AsyncSession, revision_id: uuid.UUID
) -> list[ApplicabilityRuleRow]:
    rows = await session.execute(
        text(
            "SELECT rule_id, scope, target_tenant_id, capability_ids, capability_labels, domain_ids, "
            "       task_kinds, action_classes, environments, data_sensitivity_tiers "
            "FROM arc_applicability_rules WHERE revision_id = :revision_id"
        ),
        {"revision_id": revision_id},
    )
    return [
        ApplicabilityRuleRow(
            rule_id=row.rule_id,
            scope=row.scope,
            target_tenant_id=row.target_tenant_id,
            capability_ids=row.capability_ids,
            capability_labels=row.capability_labels,
            domain_ids=row.domain_ids,
            task_kinds=row.task_kinds,
            action_classes=row.action_classes,
            environments=row.environments,
            data_sensitivity_tiers=row.data_sensitivity_tiers,
        )
        for row in rows
    ]


__all__ = [
    "ApplicabilityRuleRow",
    "FieldProvenanceRow",
    "SemanticTestRow",
    "load_applicability_rules_for_revision",
    "load_field_provenance",
    "load_one_field_provenance",
    "load_semantic_tests",
    "upsert_field_provenance",
    "upsert_semantic_test",
]
