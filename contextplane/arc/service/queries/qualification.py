"""Parametrized SQL for `arc_observation_qualifications`, plus the two
cross-cutting reads `qualification.py` needs that have no other natural
home: the live D2 approving principal and whether any persisted semantic
test for a candidate failed. Split from `queries/observation.py` along the
same service-ownership boundary `qualification.py` itself observes; see
that module's own docstring.

Every function takes an already-open `AsyncSession` and controls no
transaction boundary of its own, matching every other queries module here.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession


def _json(value: list[dict[str, Any]]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclasses.dataclass(frozen=True)
class QualificationRow:
    qualification_id: uuid.UUID
    idempotency_key_digest: str
    candidate_review_package_digest: str
    candidate_revision_id: uuid.UUID
    proposal_id: uuid.UUID
    proposal_version: int
    risk_classification: str
    risk_algorithm_version: str
    baseline_revision_id: uuid.UUID | None
    selection_engine_version: str
    engine_configuration_version: str
    cohort_id: uuid.UUID
    cohort_digest: str
    window_started_at: datetime.datetime
    window_ended_at: datetime.datetime
    eligible_count: int
    observed_count: int
    expected_impact_envelope_digest: str
    counters_by_delta_code: list[dict[str, Any]]
    unexplained_count: int
    out_of_envelope_count: int
    replay_corpus_digest: str | None
    replay_result_digest: str | None
    qualification_algorithm_version: str
    computed_decision: str
    computed_at: datetime.datetime
    reason_codes: list[str]
    accepted_by_issuer: str | None
    accepted_by_subject: str | None
    accepted_by_role: str | None
    accepted_at: datetime.datetime | None
    acceptance_audit_reference: str | None
    expires_at: datetime.datetime | None


_QUAL_COLS = (
    "qualification_id, idempotency_key_digest, candidate_review_package_digest, candidate_revision_id, "
    "proposal_id, proposal_version, risk_classification, risk_algorithm_version, baseline_revision_id, "
    "selection_engine_version, engine_configuration_version, cohort_id, cohort_digest, window_started_at, "
    "window_ended_at, eligible_count, observed_count, expected_impact_envelope_digest, counters_by_delta_code, "
    "unexplained_count, out_of_envelope_count, replay_corpus_digest, replay_result_digest, "
    "qualification_algorithm_version, computed_decision, computed_at, reason_codes, accepted_by_issuer, "
    "accepted_by_subject, accepted_by_role, accepted_at, acceptance_audit_reference, expires_at"
)


def _qualification_row(row: Row[Any]) -> QualificationRow:
    return QualificationRow(
        qualification_id=row.qualification_id,
        idempotency_key_digest=row.idempotency_key_digest,
        candidate_review_package_digest=row.candidate_review_package_digest,
        candidate_revision_id=row.candidate_revision_id,
        proposal_id=row.proposal_id,
        proposal_version=row.proposal_version,
        risk_classification=row.risk_classification,
        risk_algorithm_version=row.risk_algorithm_version,
        baseline_revision_id=row.baseline_revision_id,
        selection_engine_version=row.selection_engine_version,
        engine_configuration_version=row.engine_configuration_version,
        cohort_id=row.cohort_id,
        cohort_digest=row.cohort_digest,
        window_started_at=row.window_started_at,
        window_ended_at=row.window_ended_at,
        eligible_count=row.eligible_count,
        observed_count=row.observed_count,
        expected_impact_envelope_digest=row.expected_impact_envelope_digest,
        counters_by_delta_code=list(row.counters_by_delta_code),
        unexplained_count=row.unexplained_count,
        out_of_envelope_count=row.out_of_envelope_count,
        replay_corpus_digest=row.replay_corpus_digest,
        replay_result_digest=row.replay_result_digest,
        qualification_algorithm_version=row.qualification_algorithm_version,
        computed_decision=row.computed_decision,
        computed_at=row.computed_at,
        reason_codes=list(row.reason_codes),
        accepted_by_issuer=row.accepted_by_issuer,
        accepted_by_subject=row.accepted_by_subject,
        accepted_by_role=row.accepted_by_role,
        accepted_at=row.accepted_at,
        acceptance_audit_reference=row.acceptance_audit_reference,
        expires_at=row.expires_at,
    )


async def upsert_qualification(
    session: AsyncSession,
    *,
    qualification_id: uuid.UUID,
    idempotency_key_digest: str,
    candidate_review_package_digest: str,
    candidate_revision_id: uuid.UUID,
    proposal_id: uuid.UUID,
    proposal_version: int,
    risk_classification: str,
    risk_algorithm_version: str,
    baseline_revision_id: uuid.UUID | None,
    selection_engine_version: str,
    engine_configuration_version: str,
    cohort_id: uuid.UUID,
    cohort_digest: str,
    window_started_at: datetime.datetime,
    window_ended_at: datetime.datetime,
    eligible_count: int,
    observed_count: int,
    expected_impact_envelope_digest: str,
    counters_by_delta_code: list[dict[str, Any]],
    unexplained_count: int,
    out_of_envelope_count: int,
    replay_corpus_digest: str | None,
    replay_result_digest: str | None,
    qualification_algorithm_version: str,
    computed_decision: str,
    computed_at: datetime.datetime,
    reason_codes: list[str],
) -> QualificationRow:
    """Insert a fresh computation, or -- for an exact retry against the
    same eight-column binding tuple -- refresh the existing unaccepted row
    in place rather than fail on the `UNIQUE NULLS NOT DISTINCT` constraint.

    An already-*accepted* row is never touched: `ON CONFLICT ... WHERE
    accepted_at IS NULL` scopes the upsert to the unaccepted case only, so
    recomputing after acceptance cannot silently rewrite a decision a
    human already signed off on. An attempted recompute against an
    accepted tuple falls through to `DO NOTHING`, and the caller re-reads
    the existing row by binding tuple to hand back the accepted record
    unchanged.
    """
    await session.execute(
        text(
            f"INSERT INTO arc_observation_qualifications ({_QUAL_COLS}) VALUES "  # noqa: S608 - module constant
            "(:qualification_id, :idempotency_key_digest, :candidate_review_package_digest, "
            " :candidate_revision_id, :pid, :pv, :risk_classification, :risk_algorithm_version, "
            " :baseline_revision_id, :selection_engine_version, :engine_configuration_version, :cohort_id, "
            " :cohort_digest, :window_started_at, :window_ended_at, :eligible_count, :observed_count, "
            " :expected_impact_envelope_digest, :counters_by_delta_code, :unexplained_count, "
            " :out_of_envelope_count, :replay_corpus_digest, :replay_result_digest, "
            " :qualification_algorithm_version, :computed_decision, :computed_at, :reason_codes, "
            " NULL, NULL, NULL, NULL, NULL, NULL) "
            "ON CONFLICT ON CONSTRAINT uq_arc_observation_qualifications_binding DO UPDATE SET "
            "  window_started_at = EXCLUDED.window_started_at, window_ended_at = EXCLUDED.window_ended_at, "
            "  eligible_count = EXCLUDED.eligible_count, observed_count = EXCLUDED.observed_count, "
            "  counters_by_delta_code = EXCLUDED.counters_by_delta_code, "
            "  unexplained_count = EXCLUDED.unexplained_count, "
            "  out_of_envelope_count = EXCLUDED.out_of_envelope_count, "
            "  computed_decision = EXCLUDED.computed_decision, computed_at = EXCLUDED.computed_at, "
            "  reason_codes = EXCLUDED.reason_codes "
            "WHERE arc_observation_qualifications.accepted_at IS NULL"
        ),
        {
            "qualification_id": qualification_id,
            "idempotency_key_digest": idempotency_key_digest,
            "candidate_review_package_digest": candidate_review_package_digest,
            "candidate_revision_id": candidate_revision_id,
            "pid": proposal_id,
            "pv": proposal_version,
            "risk_classification": risk_classification,
            "risk_algorithm_version": risk_algorithm_version,
            "baseline_revision_id": baseline_revision_id,
            "selection_engine_version": selection_engine_version,
            "engine_configuration_version": engine_configuration_version,
            "cohort_id": cohort_id,
            "cohort_digest": cohort_digest,
            "window_started_at": window_started_at,
            "window_ended_at": window_ended_at,
            "eligible_count": eligible_count,
            "observed_count": observed_count,
            "expected_impact_envelope_digest": expected_impact_envelope_digest,
            "counters_by_delta_code": _json(counters_by_delta_code),
            "unexplained_count": unexplained_count,
            "out_of_envelope_count": out_of_envelope_count,
            "replay_corpus_digest": replay_corpus_digest,
            "replay_result_digest": replay_result_digest,
            "qualification_algorithm_version": qualification_algorithm_version,
            "computed_decision": computed_decision,
            "computed_at": computed_at,
            "reason_codes": reason_codes,
        },
    )
    existing = await load_qualification_by_binding(
        session,
        candidate_review_package_digest=candidate_review_package_digest,
        baseline_revision_id=baseline_revision_id,
        selection_engine_version=selection_engine_version,
        engine_configuration_version=engine_configuration_version,
        cohort_digest=cohort_digest,
        expected_impact_envelope_digest=expected_impact_envelope_digest,
        replay_corpus_digest=replay_corpus_digest,
        qualification_algorithm_version=qualification_algorithm_version,
    )
    if existing is None:  # pragma: no cover - the statement above just inserted or matched exactly this row
        msg = "upsert_qualification: no row found immediately after insert/update -- binding-tuple read is out of sync"
        raise RuntimeError(msg)
    return existing


async def load_qualification_by_binding(
    session: AsyncSession,
    *,
    candidate_review_package_digest: str,
    baseline_revision_id: uuid.UUID | None,
    selection_engine_version: str,
    engine_configuration_version: str,
    cohort_digest: str,
    expected_impact_envelope_digest: str,
    replay_corpus_digest: str | None,
    qualification_algorithm_version: str,
) -> QualificationRow | None:
    """Read back by the exact eight-column binding tuple, `NULL`-aware.
    Postgres `=` never matches `NULL`, so a plain equality clause would
    silently miss a row whose `baseline_revision_id` or `replay_corpus_
    digest` is `NULL`; `IS NOT DISTINCT FROM` is the read-side mirror of
    the constraint's own `NULLS NOT DISTINCT`."""
    row = (
        await session.execute(
            text(
                f"SELECT {_QUAL_COLS} FROM arc_observation_qualifications "  # noqa: S608 - module constant
                "WHERE candidate_review_package_digest = :crpd "
                "  AND baseline_revision_id IS NOT DISTINCT FROM :brid "
                "  AND selection_engine_version = :sev AND engine_configuration_version = :ecv "
                "  AND cohort_digest = :cd AND expected_impact_envelope_digest = :eied "
                "  AND replay_corpus_digest IS NOT DISTINCT FROM :rcd "
                "  AND qualification_algorithm_version = :qav"
            ),
            {
                "crpd": candidate_review_package_digest,
                "brid": baseline_revision_id,
                "sev": selection_engine_version,
                "ecv": engine_configuration_version,
                "cd": cohort_digest,
                "eied": expected_impact_envelope_digest,
                "rcd": replay_corpus_digest,
                "qav": qualification_algorithm_version,
            },
        )
    ).one_or_none()
    return None if row is None else _qualification_row(row)


async def load_qualification(session: AsyncSession, qualification_id: uuid.UUID) -> QualificationRow | None:
    row = (
        await session.execute(
            text(f"SELECT {_QUAL_COLS} FROM arc_observation_qualifications WHERE qualification_id = :id"),  # noqa: S608
            {"id": qualification_id},
        )
    ).one_or_none()
    return None if row is None else _qualification_row(row)


async def load_latest_qualification_for_version(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int
) -> QualificationRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_QUAL_COLS} FROM arc_observation_qualifications "  # noqa: S608 - module constant
                "WHERE proposal_id = :pid AND proposal_version = :pv ORDER BY computed_at DESC LIMIT 1"
            ),
            {"pid": proposal_id, "pv": proposal_version},
        )
    ).one_or_none()
    return None if row is None else _qualification_row(row)


async def accept_qualification(
    session: AsyncSession,
    *,
    qualification_id: uuid.UUID,
    accepted_by_issuer: str,
    accepted_by_subject: str,
    accepted_by_role: str,
    accepted_at: datetime.datetime,
    expires_at: datetime.datetime,
    acceptance_audit_reference: str,
) -> bool:
    """Compare-and-swap acceptance: `WHERE accepted_at IS NULL`. A second
    acceptance attempt against an already-accepted row loses -- the
    submitter/approver/activator identity rules are the caller's job
    before this write; this is the backstop against two concurrent accept
    calls both succeeding."""
    result = await session.execute(
        text(
            "UPDATE arc_observation_qualifications SET accepted_by_issuer = :abi, accepted_by_subject = :abs, "
            "  accepted_by_role = :abr, accepted_at = :aa, expires_at = :ea, acceptance_audit_reference = :aar "
            "WHERE qualification_id = :qid AND accepted_at IS NULL"
        ),
        {
            "qid": qualification_id,
            "abi": accepted_by_issuer,
            "abs": accepted_by_subject,
            "abr": accepted_by_role,
            "aa": accepted_at,
            "ea": expires_at,
            "aar": acceptance_audit_reference,
        },
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def load_approving_principal(session: AsyncSession, revision_id: uuid.UUID) -> tuple[str, str] | None:
    """The live D2 approver's `(issuer, subject)` for *revision_id*, if
    any. A dedicated read here rather than importing `queries/approval.py`:
    that module is a sibling task's concurrent path scope this task must
    not edit, and this is a three-line, single-purpose read."""
    row = (
        await session.execute(
            text(
                "SELECT approving_principal_issuer, approving_principal_subject "
                "FROM arc_projection_approval_evidence WHERE revision_id = :rid AND revoked_at IS NULL"
            ),
            {"rid": revision_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return (row.approving_principal_issuer, row.approving_principal_subject)


async def has_failed_semantic_test(session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM arc_authoring_semantic_tests "
                "WHERE proposal_id = :pid AND proposal_version = :pv AND passed = false LIMIT 1"
            ),
            {"pid": proposal_id, "pv": proposal_version},
        )
    ).one_or_none()
    return row is not None


__all__ = [
    "QualificationRow",
    "accept_qualification",
    "has_failed_semantic_test",
    "load_approving_principal",
    "load_latest_qualification_for_version",
    "load_qualification",
    "load_qualification_by_binding",
    "upsert_qualification",
]
