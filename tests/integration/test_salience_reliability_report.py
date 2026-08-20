"""The salience reliability report, over whatever the deployment actually has.

Wired into `make eval` and asserts almost nothing about the numbers, because
there are none to assert against yet: a fresh database has no scored claims and
no receipts, and the honest output for that is a report saying so. What it does
assert is that the join is the one it claims to be, proven by seeding a claim,
serving it through a resolution, and checking it comes back marked retrieved.

**The label is the weak one and the report says so on every line of output.**
Salience is about whether a claim will be *used*; this measures whether it was
*served*. Serving is necessary for use and nowhere near sufficient, so a reader
who takes this figure for the stronger one will overrate the weighting. The
stronger label needs a citation-to-outcome join that does not exist.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.arms import BLOCK_OBSERVED_CLAIMS
from contextplane.service.memory.salience_reliability import (
    MIN_BUCKET_OBSERVATIONS,
    Observation,
    measure,
    render,
)

_NOW = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)

#: What "success" meant. Carried into every report so the figure cannot be read
#: against the label it is not measuring.
_LABEL = "served in at least one resolution (weaker than 'cited on a later turn'; that join does not exist yet)"

_OBSERVATIONS_SQL = """
SELECT c.salience AS salience,
       EXISTS (
           SELECT 1 FROM context_receipt_items i
           WHERE i.block = :block AND i.item_key = c.claim_id::text
       ) AS was_retrieved
FROM memory_claims c
WHERE c.salience IS NOT NULL
"""


async def _observations(factory: async_sessionmaker[Any]) -> list[Observation]:
    async with factory() as session:
        rows = (await session.execute(text(_OBSERVATIONS_SQL), {"block": BLOCK_OBSERVED_CLAIMS})).all()
    return [Observation(salience=float(row.salience), was_retrieved=bool(row.was_retrieved)) for row in rows]


@pytest.mark.asyncio
async def test_salience_reliability_report(pg_container: str) -> None:
    """Report the curve over this deployment's own claims. Asserts no figures.

    An empty population is the expected result on any database that has not been
    used, and it is reported as an absence rather than as a flat curve. That is
    the whole point of running it now: the shape of the report is fixed before
    there is a number anybody wants to defend.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        report = measure(await _observations(factory), label=_LABEL)
        print("\n" + render(report))
        if report.total_observations < MIN_BUCKET_OBSERVATIONS:
            print(
                "  Nothing to record in eval/EVAL.md yet: this deployment has fewer scored claims than\n"
                "  one bucket needs. The report is wired so the figure appears the moment there is one."
            )
        else:
            print("  Record these figures in eval/EVAL.md. No threshold consumes salience yet.")
        # The only assertion: the report describes the population it was handed,
        # so a join that silently returned nothing cannot read as a clean result.
        assert report.total_observations == len(await _observations(factory))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_join_marks_a_served_claim_as_retrieved(pg_container: str) -> None:
    """The join, proven rather than assumed.

    Without this the report above passes on every empty database forever and
    would keep passing if `item_key` stopped holding a claim id — reporting a
    retrieval rate of zero for a corpus that was being retrieved constantly.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    served, unserved = uuid.uuid4(), uuid.uuid4()

    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                    "VALUES (:t, :s, :s, :now, TRUE)"
                ),
                {"t": tenant_id, "s": f"salience-{tenant_id.hex[:8]}", "now": _NOW},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, created_at) "
                    "VALUES (:a, :t, :sub, 'reliability', :now)"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:8]}", "now": _NOW},
            )
            # Unlinked, unattributed, and owned by nobody -- the three travel
            # together by CHECK constraint, and it is the one shape whose
            # confidence may be NULL. This test is about salience rather than
            # confidence, and the shape demonstrates something true besides:
            # salience is a property of the episode and does not wait for a
            # subject to resolve.
            for claim_id, salience in ((served, 0.9), (unserved, 0.2)):
                await session.execute(
                    text(
                        "INSERT INTO memory_claims (claim_id, author_tenant_id, "
                        "  author_actor_id, subject_reference, predicate, value_type, claim_category, "
                        "  value_jsonb, asserted_valid_from, status, visibility, source_authority, "
                        "  size_bytes, salience, salience_signals, salience_weights_id, created_at) "
                        "VALUES (:cid, :t, :a, 'svc:x', 'owned_by_team', 'string', "
                        "  'ownership_stewardship', CAST('\"platform\"' AS JSONB), :now, 'unlinked', "
                        "  'private', 'unattributed', 9, CAST(:sal AS NUMERIC), "
                        "  CAST('{}' AS JSONB), 'salience-weights@1', :now)"
                    ),
                    {"cid": claim_id, "t": tenant_id, "a": actor_id, "sal": salience, "now": _NOW},
                )

            receipt_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO context_receipts (receipt_id, tenant_id, state, cacheable, "
                    "  requested_by, resolved_at) "
                    "VALUES (:r, :t, 'complete', TRUE, :who, :now)"
                ),
                {"r": receipt_id, "t": tenant_id, "who": str(actor_id), "now": _NOW},
            )
            await session.execute(
                text(
                    # `trust` is required outside the canonical block by CHECK:
                    # a contextual item without it took a path the contract
                    # object never touched.
                    "INSERT INTO context_receipt_items (item_row_id, receipt_id, receipt_item_id, "
                    "  block, source, item_key, trust) "
                    "VALUES (gen_random_uuid(), :r, :rid, :block, 'living-memory', :key, 'observed')"
                ),
                {
                    "r": receipt_id,
                    "rid": f"digest-{served.hex[:8]}",
                    "block": BLOCK_OBSERVED_CLAIMS,
                    "key": str(served),
                },
            )

        async with factory() as session:
            rows = (
                await session.execute(
                    text(_OBSERVATIONS_SQL + " AND c.author_tenant_id = :tid"),
                    {"block": BLOCK_OBSERVED_CLAIMS, "tid": tenant_id},
                )
            ).all()

        by_salience = {float(row.salience): bool(row.was_retrieved) for row in rows}
        assert by_salience == {0.9: True, 0.2: False}, (
            "the receipt join must mark exactly the claim a resolution served; "
            "if both are False the join is broken and the report would show a flat zero curve"
        )
    finally:
        await engine.dispose()
