"""Parametrized SQL for `arc_observation_cohorts`, `arc_observation_cohort_
members`, and `arc_observation_results` -- the tables `shadow.py` and
`observation_window_evaluator.py`/`observation_fingerprint_reaper.py` read
and write. The sibling tables `arc_observation_replay_corpora` and
`arc_observation_qualifications` have their own queries siblings,
`queries/replay_corpus.py` and `queries/qualification.py`, split from this
module along the same service-ownership boundary `replay_corpus.py` and
`qualification.py` themselves observe -- see either sibling's own module
docstring for why one shared file for all five tables would have crossed
this repo's 800-line-per-file ceiling with no cohesion gain to show for it.

Every function takes an already-open `AsyncSession` and controls no
transaction boundary of its own, matching every other queries module here.

**The aggregate-counter read is the leak-prevention chokepoint.**
`load_aggregate_counters` is the *only* function that reads
`arc_observation_results` across every member tenant of a cohort, and its
`SELECT` list never names `tenant_id` -- there is no parameter or branch
that could make it start projecting one. A global qualification computes
its decision from this function alone; a tenant-scoped read of the same
cohort goes through `load_tenant_counters`, which requires a `tenant_id`
and only ever returns that one tenant's row. Two functions for two
authorization postures, rather than one function with an optional filter,
because an optional filter is one forgotten `if` away from an accidental
global read.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# arc_observation_cohorts
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CohortRow:
    cohort_id: uuid.UUID
    proposal_id: uuid.UUID
    proposal_version: int
    candidate_revision_id: uuid.UUID
    risk_classification: str
    scope_predicate_digest: str
    tenant_membership_digest: str
    eligibility_predicate_digest: str
    frozen_at: datetime.datetime
    window_started_at: datetime.datetime
    window_deadline: datetime.datetime
    window_ended_at: datetime.datetime | None
    closed_at: datetime.datetime | None


_COHORT_COLS = (
    "cohort_id, proposal_id, proposal_version, candidate_revision_id, risk_classification, "
    "scope_predicate_digest, tenant_membership_digest, eligibility_predicate_digest, frozen_at, "
    "window_started_at, window_deadline, window_ended_at, closed_at"
)


def _cohort_row(row: Row[Any]) -> CohortRow:
    return CohortRow(
        cohort_id=row.cohort_id,
        proposal_id=row.proposal_id,
        proposal_version=row.proposal_version,
        candidate_revision_id=row.candidate_revision_id,
        risk_classification=row.risk_classification,
        scope_predicate_digest=row.scope_predicate_digest,
        tenant_membership_digest=row.tenant_membership_digest,
        eligibility_predicate_digest=row.eligibility_predicate_digest,
        frozen_at=row.frozen_at,
        window_started_at=row.window_started_at,
        window_deadline=row.window_deadline,
        window_ended_at=row.window_ended_at,
        closed_at=row.closed_at,
    )


async def load_cohort_by_version(
    session: AsyncSession, proposal_id: uuid.UUID, proposal_version: int
) -> CohortRow | None:
    row = (
        await session.execute(
            text(
                f"SELECT {_COHORT_COLS} FROM arc_observation_cohorts "  # noqa: S608 - module constant
                "WHERE proposal_id = :pid AND proposal_version = :pv"
            ),
            {"pid": proposal_id, "pv": proposal_version},
        )
    ).one_or_none()
    return None if row is None else _cohort_row(row)


async def load_cohort(session: AsyncSession, cohort_id: uuid.UUID) -> CohortRow | None:
    row = (
        await session.execute(
            text(f"SELECT {_COHORT_COLS} FROM arc_observation_cohorts WHERE cohort_id = :cid"),  # noqa: S608
            {"cid": cohort_id},
        )
    ).one_or_none()
    return None if row is None else _cohort_row(row)


async def list_open_cohorts(session: AsyncSession, *, limit: int) -> list[CohortRow]:
    """Cohorts still accepting observations (`closed_at IS NULL`), oldest
    window first -- what `observation_window_evaluator.py` sweeps each pass."""
    rows = await session.execute(
        text(
            f"SELECT {_COHORT_COLS} FROM arc_observation_cohorts "  # noqa: S608 - module constant
            "WHERE closed_at IS NULL ORDER BY window_started_at ASC LIMIT :limit"
        ),
        {"limit": limit},
    )
    return [_cohort_row(row) for row in rows]


async def insert_cohort(
    session: AsyncSession,
    *,
    cohort_id: uuid.UUID,
    proposal_id: uuid.UUID,
    proposal_version: int,
    candidate_revision_id: uuid.UUID,
    risk_classification: str,
    scope_predicate_digest: str,
    tenant_membership_digest: str,
    eligibility_predicate_digest: str,
    frozen_at: datetime.datetime,
    window_started_at: datetime.datetime,
    window_deadline: datetime.datetime,
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_observation_cohorts "
            "(cohort_id, proposal_id, proposal_version, candidate_revision_id, risk_classification, "
            " scope_predicate_digest, tenant_membership_digest, eligibility_predicate_digest, frozen_at, "
            " window_started_at, window_deadline) "
            "VALUES (:cohort_id, :pid, :pv, :candidate_revision_id, :risk_classification, "
            "        :scope_predicate_digest, :tenant_membership_digest, :eligibility_predicate_digest, "
            "        :frozen_at, :window_started_at, :window_deadline)"
        ),
        {
            "cohort_id": cohort_id,
            "pid": proposal_id,
            "pv": proposal_version,
            "candidate_revision_id": candidate_revision_id,
            "risk_classification": risk_classification,
            "scope_predicate_digest": scope_predicate_digest,
            "tenant_membership_digest": tenant_membership_digest,
            "eligibility_predicate_digest": eligibility_predicate_digest,
            "frozen_at": frozen_at,
            "window_started_at": window_started_at,
            "window_deadline": window_deadline,
        },
    )


async def close_cohort(
    session: AsyncSession, cohort_id: uuid.UUID, *, window_ended_at: datetime.datetime, closed_at: datetime.datetime
) -> bool:
    """Compare-and-swap the closing boundary: `WHERE closed_at IS NULL`.
    Returns whether *this* call was the one that closed it -- see the
    migration's own docstring for why a window closing twice, or closing
    with two different `window_ended_at` values, is the failure this
    guards against."""
    result = await session.execute(
        text(
            "UPDATE arc_observation_cohorts SET window_ended_at = :window_ended_at, closed_at = :closed_at "
            "WHERE cohort_id = :cid AND closed_at IS NULL"
        ),
        {"cid": cohort_id, "window_ended_at": window_ended_at, "closed_at": closed_at},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# arc_observation_cohort_members
# ---------------------------------------------------------------------------


async def insert_cohort_members(
    session: AsyncSession, *, cohort_id: uuid.UUID, tenant_ids: list[uuid.UUID], added_at: datetime.datetime
) -> None:
    for tenant_id in tenant_ids:
        await session.execute(
            text(
                "INSERT INTO arc_observation_cohort_members (cohort_id, tenant_id, added_at) "
                "VALUES (:cid, :tid, :added_at)"
            ),
            {"cid": cohort_id, "tid": tenant_id, "added_at": added_at},
        )


async def list_active_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Every tenant eligible to be a global cohort's member at freeze time.
    `disabled_at IS NULL` catches an operator-disabled tenant `is_active`
    alone would miss."""
    rows = await session.execute(
        text("SELECT tenant_id FROM tenants WHERE is_active = true AND disabled_at IS NULL ORDER BY tenant_id")
    )
    return [row.tenant_id for row in rows]


# ---------------------------------------------------------------------------
# arc_observation_results -- see this module's own docstring for why the
# aggregate read below must never project `tenant_id`.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ResultCounters:
    eligible_count: int
    observed_count: int
    unexplained_count: int
    out_of_envelope_count: int
    counters_by_delta_code: dict[str, dict[str, int]]


async def ensure_result_row(
    session: AsyncSession, *, cohort_id: uuid.UUID, tenant_id: uuid.UUID, now: datetime.datetime
) -> None:
    await session.execute(
        text(
            "INSERT INTO arc_observation_results (cohort_id, tenant_id, updated_at) "
            "VALUES (:cid, :tid, :now) ON CONFLICT (cohort_id, tenant_id) DO NOTHING"
        ),
        {"cid": cohort_id, "tid": tenant_id, "now": now},
    )


async def record_observation(
    session: AsyncSession,
    *,
    cohort_id: uuid.UUID,
    tenant_id: uuid.UUID,
    eligible_delta: int,
    observed_delta: int,
    unexplained_delta: int,
    out_of_envelope_delta: int,
    delta_code: str,
    explained: bool,
    fingerprint_digest: str,
    now: datetime.datetime,
) -> bool:
    """Fold one shadow-evaluated request into this tenant's row for the
    cohort. The `NOT EXISTS` clause refuses once the cohort has closed --
    the write-side mirror of `close_cohort`'s own guard: a result accepted
    after `closed_at` would move an already-fixed denominator. Returns
    whether the write applied."""
    result = await session.execute(
        text(
            "UPDATE arc_observation_results SET "
            "  eligible_count = eligible_count + :ed, observed_count = observed_count + :od, "
            "  unexplained_count = unexplained_count + :ud, "
            "  out_of_envelope_count = out_of_envelope_count + :oed, "
            "  counters_by_delta_code = jsonb_set(counters_by_delta_code, ARRAY[:delta_code, :bucket], "
            "      to_jsonb(coalesce((counters_by_delta_code -> :delta_code ->> :bucket)::int, 0) + 1), true), "
            "  fingerprint_digests = fingerprint_digests || to_jsonb(ARRAY[:fp]::text[]), updated_at = :now "
            "WHERE cohort_id = :cid AND tenant_id = :tid AND NOT EXISTS ("
            "    SELECT 1 FROM arc_observation_cohorts c "
            "    WHERE c.cohort_id = arc_observation_results.cohort_id AND c.closed_at IS NOT NULL"
            "  )"
        ),
        {
            "cid": cohort_id,
            "tid": tenant_id,
            "ed": eligible_delta,
            "od": observed_delta,
            "ud": unexplained_delta,
            "oed": out_of_envelope_delta,
            "delta_code": delta_code,
            "bucket": "explained" if explained else "unexplained",
            "fp": fingerprint_digest,
            "now": now,
        },
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def load_tenant_counters(
    session: AsyncSession, *, cohort_id: uuid.UUID, tenant_id: uuid.UUID
) -> ResultCounters | None:
    """Tenant-scoped detail read. Requires the caller's own `tenant_id`; never returns another tenant's row."""
    row = (
        await session.execute(
            text(
                "SELECT eligible_count, observed_count, unexplained_count, out_of_envelope_count, "
                "       counters_by_delta_code "
                "FROM arc_observation_results WHERE cohort_id = :cid AND tenant_id = :tid"
            ),
            {"cid": cohort_id, "tid": tenant_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return ResultCounters(
        eligible_count=row.eligible_count,
        observed_count=row.observed_count,
        unexplained_count=row.unexplained_count,
        out_of_envelope_count=row.out_of_envelope_count,
        counters_by_delta_code=dict(row.counters_by_delta_code),
    )


async def load_aggregate_counters(session: AsyncSession, cohort_id: uuid.UUID) -> ResultCounters:
    """The **only** cross-tenant read in this module. Sums every member
    tenant's row for *cohort_id* and never selects `tenant_id` -- there is
    no per-tenant breakdown anywhere in the returned shape, by
    construction rather than by caller discipline. No rows yet (nothing
    shadow-evaluated) returns all-zero counters, not `None`: "no rows" and
    "no requests observed" are the same fact for a `SUM()`."""
    row = (
        await session.execute(
            text(
                "SELECT coalesce(sum(eligible_count), 0) AS eligible_count, "
                "       coalesce(sum(observed_count), 0) AS observed_count, "
                "       coalesce(sum(unexplained_count), 0) AS unexplained_count, "
                "       coalesce(sum(out_of_envelope_count), 0) AS out_of_envelope_count "
                "FROM arc_observation_results WHERE cohort_id = :cid"
            ),
            {"cid": cohort_id},
        )
    ).one()
    delta_rows = await session.execute(
        text(
            "SELECT key AS delta_code, coalesce(sum((value ->> 'explained')::int), 0) AS explained, "
            "       coalesce(sum((value ->> 'unexplained')::int), 0) AS unexplained "
            "FROM arc_observation_results, jsonb_each(counters_by_delta_code) WHERE cohort_id = :cid GROUP BY key"
        ),
        {"cid": cohort_id},
    )
    by_delta_code = {
        r.delta_code: {"explained": int(r.explained), "unexplained": int(r.unexplained)} for r in delta_rows
    }
    return ResultCounters(
        eligible_count=int(row.eligible_count),
        observed_count=int(row.observed_count),
        unexplained_count=int(row.unexplained_count),
        out_of_envelope_count=int(row.out_of_envelope_count),
        counters_by_delta_code=by_delta_code,
    )


async def reap_fingerprints(session: AsyncSession, *, closed_before: datetime.datetime, now: datetime.datetime) -> int:
    """Clear `fingerprint_digests` for every result row whose cohort closed
    at or before *closed_before* and carries no legal hold. Aggregate
    counters are untouched -- "keep only counters and signed digests" is
    exactly what survives this. Returns the number of rows cleared.

    `<=`, not `<`: the caller passes `closed_before = now - retention_
    window`, so a cohort that closed *exactly* `retention_window` ago has
    its appeal window closed as of this instant, not one tick later --
    matching the "closes exactly at the boundary" standard this package's
    other time-boundary workers hold to.
    """
    result = await session.execute(
        text(
            "UPDATE arc_observation_results r SET fingerprint_digests = '[]'::jsonb, fingerprints_reaped_at = :now "
            "FROM arc_observation_cohorts c "
            "WHERE r.cohort_id = c.cohort_id AND c.closed_at IS NOT NULL AND c.closed_at <= :closed_before "
            "  AND r.legal_hold_at IS NULL AND r.fingerprint_digests != '[]'::jsonb"
        ),
        {"closed_before": closed_before, "now": now},
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def place_legal_hold(
    session: AsyncSession, *, cohort_id: uuid.UUID, tenant_id: uuid.UUID, placed_at: datetime.datetime
) -> None:
    await session.execute(
        text(
            "UPDATE arc_observation_results SET legal_hold_at = :placed_at WHERE cohort_id = :cid AND tenant_id = :tid"
        ),
        {"cid": cohort_id, "tid": tenant_id, "placed_at": placed_at},
    )


__all__ = [
    "CohortRow",
    "ResultCounters",
    "close_cohort",
    "ensure_result_row",
    "insert_cohort",
    "insert_cohort_members",
    "list_active_tenant_ids",
    "list_open_cohorts",
    "load_aggregate_counters",
    "load_cohort",
    "load_cohort_by_version",
    "load_tenant_counters",
    "place_legal_hold",
    "reap_fingerprints",
    "record_observation",
]
