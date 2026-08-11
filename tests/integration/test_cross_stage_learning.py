"""Whether what one change learned can be found by the next one.

The pilot's claim is that governed learning is reusable across lifecycle stages:
something concluded while implementing a change should be findable when a later
change reaches review or deployment. That only works if a conclusion records
*where it applies* in terms a later reader can select on, and if those terms
survive the trip through the database unchanged.

Run against Postgres rather than a fake for both halves. The applicability field
is `Text`, so whether structured dimensions round-trip through it is a property
of the column and the driver, not of the dataclass -- a fake would hand back the
Python object that went in and prove nothing. And the digest's job is to collapse
two identical conclusions into one stored attempt, which is a uniqueness
behaviour of the stored row.

**What this file does not claim.** Nothing here is evidence that reuse *helped*.
It proves a conclusion is addressable by the dimensions it recorded and that a
non-matching one is not returned; whether an agent given that conclusion made a
better change is a question about people and is measured in the pilot, not in a
test. Selection inside the retrieval arms is a different task's surface, so this
selects over the stored rows directly -- one layer below where a caller will.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION
from contextplane.service.memory.derivation import (
    Assertion,
    CrossStageApplicability,
    DerivationProfile,
    DerivationService,
    Evidence,
    applicability_dimensions,
    applicability_from_references,
)
from contextplane.types import SystemClock, TenantContext

_PROFILE = DerivationProfile(name="outcome-extractor", version="1.4.0")

#: The repository both changes happen in, and one that neither does. The second
#: is what makes a positive result mean anything: a selector that returns
#: everything would satisfy "the earlier claim is retrievable" perfectly.
_REPOSITORY = "repo:roughcompass/contextplane"
_OTHER_REPOSITORY = "repo:roughcompass/unrelated"


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container), future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def tenant_id(sync_engine: Engine) -> uuid.UUID:
    new_id = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'cross-stage')"),
            {"t": new_id, "s": f"xstage-{new_id.hex[:10]}"},
        )
    return new_id


def _ctx(tenant_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["admin"])


def _run(pg_container: str, coro_factory: Any) -> Any:
    """One event loop per call, over a factory this test owns."""

    async def _main() -> Any:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
        try:
            return await coro_factory(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(_main())


def _seed_signal(engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    """One live signal for a chain to cite. Same shape the pipeline suite seeds."""
    signal_id = uuid.uuid4()
    unique = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id, producer_type,"
                " source_event_id, idempotency_key, content_digest, authority, classification, schema_version,"
                " payload)"
                " VALUES (:s, :t, 'github-actions', 'p:test', 'external', :ev, :idk, :dig,"
                " 'github-actions:workflow-conclusion', 'internal', 'external_signal.v1',"
                ' CAST(\'{"conclusion": "failure"}\' AS JSONB))'
            ),
            {
                "s": signal_id,
                "t": tenant_id,
                "ev": f"github:workflow_run:x:{unique}:1",
                "idk": f"d-{unique}",
                "dig": f"sha256:{unique}",
            },
        )
    return signal_id


def _derive(
    pg_container: str,
    tenant_id: uuid.UUID,
    signal_id: uuid.UUID,
    *,
    applicability: str,
    subject: str,
    value: str = "the runbook step was missing",
) -> Any:
    return _run(
        pg_container,
        lambda factory: DerivationService(factory, clock=SystemClock()).derive(
            _ctx(tenant_id),
            profile=_PROFILE,
            assertion=Assertion(
                subject_reference=subject,
                predicate="runbook_step_missing",
                value={"observed": value},
                applicability=applicability,
            ),
            evidence=[
                Evidence(
                    kind="signal",
                    source_authority=AUTHORITY_OBSERVER_EXTRACTION,
                    classification="internal",
                    signal_id=signal_id,
                )
            ],
        ),
    )


def _stored_applicability(engine: Engine, derivation_id: uuid.UUID) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT applicability FROM claim_derivations WHERE derivation_id = :d"),
                {"d": derivation_id},
            ).scalar_one()
        )


def _derivations_matching(engine: Engine, tenant_id: uuid.UUID, **dimensions: str) -> set[uuid.UUID]:
    """Every stored attempt whose recorded dimensions include all of these.

    Selection in Python over the tenant's rows, deliberately. The dimensions live
    inside a text field precisely because no consumer needs SQL to filter them
    yet, and writing a JSON predicate here would prove a query this system does
    not run. What is under test is whether the stored field can answer the
    question, not which language asks it.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT derivation_id, applicability FROM claim_derivations WHERE tenant_id = :t"),
            {"t": tenant_id},
        ).all()
    return {
        row.derivation_id
        for row in rows
        if all(applicability_dimensions(row.applicability).get(key) == value for key, value in dimensions.items())
    }


def test_an_earlier_changes_conclusion_is_found_by_a_later_one_through_its_dimensions(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """The reuse claim, as far as a test can carry it.

    Two conclusions are stored: one learned in this repository while
    implementing, one learned somewhere else. A later change in this repository
    asks for what applies to it and gets the first and not the second.

    The negative half is the half that matters. "The earlier claim is
    retrievable" is satisfied by a selector that returns every row, and that
    selector is useless to the agent it is meant to help.
    """
    here = _derive(
        pg_container,
        tenant_id,
        _seed_signal(sync_engine, tenant_id),
        applicability=applicability_from_references(
            [
                SimpleNamespace(kind="repository", external_id=_REPOSITORY),
                SimpleNamespace(kind="stage", external_id="implementation"),
                SimpleNamespace(kind="work_item", external_id="TICKET-1"),
            ],
            capability="payments",
            environment="staging",
        ).as_field(),
        subject="capability:payments",
    )
    elsewhere = _derive(
        pg_container,
        tenant_id,
        _seed_signal(sync_engine, tenant_id),
        applicability=CrossStageApplicability(repository=_OTHER_REPOSITORY, stage="implementation").as_field(),
        subject="capability:billing",
        value="an unrelated step was missing",
    )

    found = _derivations_matching(sync_engine, tenant_id, repository=_REPOSITORY)

    assert here.derivation_id in found, "the earlier change's conclusion was not retrievable by repository"
    assert elsewhere.derivation_id not in found, "a conclusion from another repository was returned as applicable"


def test_a_conclusion_is_selectable_by_the_stage_that_produced_it(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Cross-stage means the stage is recorded, not that it is ignored.

    Both conclusions are from this repository, so repository alone cannot
    separate them: only the stage dimension can, which is the point of recording
    it separately rather than folding everything into one scope string.
    """
    implementing = _derive(
        pg_container,
        tenant_id,
        _seed_signal(sync_engine, tenant_id),
        applicability=CrossStageApplicability(repository=_REPOSITORY, stage="implementation").as_field(),
        subject="capability:payments",
    )
    deploying = _derive(
        pg_container,
        tenant_id,
        _seed_signal(sync_engine, tenant_id),
        applicability=CrossStageApplicability(repository=_REPOSITORY, stage="deployment").as_field(),
        subject="capability:payments-deploy",
        value="a deployment constraint was undocumented",
    )

    at_deployment = _derivations_matching(sync_engine, tenant_id, repository=_REPOSITORY, stage="deployment")

    assert deploying.derivation_id in at_deployment
    assert implementing.derivation_id not in at_deployment


def test_the_dimensions_survive_the_column_unchanged(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Read back from Postgres, not from the object that was written.

    The field is `Text`. Whether structured applicability survives it is a
    property of the column and the driver, and asserting against the dataclass
    that went in would prove only that Python remembers its own values.
    """
    recorded = _derive(
        pg_container,
        tenant_id,
        _seed_signal(sync_engine, tenant_id),
        applicability=CrossStageApplicability(
            repository=_REPOSITORY,
            capability="payments",
            environment="staging",
            stage="integration-test",
            work_type="work_item",
            scope="the retry path only",
        ).as_field(),
        subject="capability:payments",
    )

    assert applicability_dimensions(_stored_applicability(sync_engine, recorded.derivation_id)) == {
        "repository": _REPOSITORY,
        "capability": "payments",
        "environment": "staging",
        "stage": "integration-test",
        "work_type": "work_item",
        "scope": "the retry path only",
    }


def test_the_same_conclusion_placed_the_same_way_is_one_attempt_not_two(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Digest stability, asserted against the stored row rather than the string.

    `assertion_digest` hashes the applicability field, so structuring that field
    put the digest's collapsing behaviour at risk: if two identical conclusions
    serialized differently, every re-derivation would store a second attempt and
    the replay path would stop finding anything. The dimensions are built in a
    different order each time here for exactly that reason.
    """
    signal_id = _seed_signal(sync_engine, tenant_id)
    first = _derive(
        pg_container,
        tenant_id,
        signal_id,
        applicability=CrossStageApplicability(repository=_REPOSITORY, stage="implementation").as_field(),
        subject="capability:payments",
    )
    second = _derive(
        pg_container,
        tenant_id,
        signal_id,
        applicability=CrossStageApplicability(stage="implementation", repository=_REPOSITORY).as_field(),
        subject="capability:payments",
    )

    assert second.replayed, "the same conclusion stored a second attempt instead of replaying the first"
    assert second.derivation_id == first.derivation_id
    assert second.assertion_digest == first.assertion_digest


def test_a_conclusion_that_recorded_no_dimensions_is_not_returned_as_applicable(
    pg_container: str, sync_engine: Engine, tenant_id: uuid.UUID
) -> None:
    """Free-text applicability is still legal and must not match by accident.

    Most stored claims predate dimensions and carry prose. Reading one as
    applicable to a repository it never named would be the widening this design
    refuses -- an unrecorded dimension is absent, not a wildcard.
    """
    prose = _derive(
        pg_container,
        tenant_id,
        _seed_signal(sync_engine, tenant_id),
        applicability=_REPOSITORY,
        subject="capability:payments",
    )

    assert prose.derivation_id not in _derivations_matching(sync_engine, tenant_id, repository=_REPOSITORY)
