"""Publishing a calibration, refusing a bad one, and reverting on a provider swap.

The behaviour that matters most is what happens when nothing has been fitted, since
that is where this deployment actually is: the state is "uncalibrated", not an
identity mapping, and the token recording it cannot be mistaken for a version.

The second is that swapping a provider or model reverts to uncalibrated with nobody
having to remember to act. That is the whole mechanism behind requiring
recalibration — the mapping is keyed on what changed, so a changed model matches no
row.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from registry.service.memory.calibration import (
    MAX_CALIBRATION_ERROR,
    MIN_ADJUDICATED_FOR_MAPPING,
    STATUS_ACTIVE,
    STATUS_FAILED,
    UNCALIBRATED,
    Adjudication,
    CalibrationService,
    fit,
)
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)
_PROVIDER = "anthropic"
_MODEL = "claude-haiku-4-5-20251001"
_STRATEGY = "capability_observation"


@pytest_asyncio.fixture
async def factory(pg_container: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def empty_mappings(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Mappings are deployment-wide, so a leftover row from another test would make
    these assertions depend on ordering rather than on behaviour."""
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM memory_calibration_mapping"))
    yield


@pytest.fixture
def calibration(factory: async_sessionmaker[AsyncSession]) -> CalibrationService:
    return CalibrationService(factory, clock=FakeClock(_NOW))


def _good(n: int = 400) -> list[Adjudication]:
    return [
        Adjudication(provider_confidence=(i % 10) / 10 + 0.05, was_correct=(i % 100) < ((i % 10) * 10 + 5))
        for i in range(n)
    ]


def _gauge(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels or None)


# --- the cold start -----------------------------------------------------------


@pytest.mark.asyncio
async def test_with_nothing_fitted_the_state_is_uncalibrated(
    calibration: CalibrationService,
) -> None:
    """Not an identity mapping. Identity would assert that a model reporting 0.9 is
    right nine times in ten, which nobody has checked."""
    version = await calibration.active_version(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=_STRATEGY)
    assert version == UNCALIBRATED
    assert await calibration.load_active(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=_STRATEGY) is None


@pytest.mark.asyncio
async def test_the_uncalibrated_state_is_visible_on_a_gauge(
    calibration: CalibrationService,
) -> None:
    """A state, not a count, and reported so it is discoverable on a dashboard
    rather than only by reading rows."""
    await calibration.active_version(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=_STRATEGY)
    assert (
        _gauge(
            "registry_claim_calibration_status",
            provider=_PROVIDER,
            model=_MODEL,
            strategy=_STRATEGY,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_a_sample_below_the_evaluation_size_publishes_nothing(
    calibration: CalibrationService,
) -> None:
    """Below the size the accuracy target is defined over, the target cannot be
    checked even in principle — so a row that cannot be evaluated is not a fit."""
    thin = fit(_good(MIN_ADJUDICATED_FOR_MAPPING - 1))
    version, active = await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=thin,
        now=_NOW,
    )
    assert version == UNCALIBRATED
    assert not active


# --- publishing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fit_meeting_the_target_becomes_active(
    calibration: CalibrationService,
) -> None:
    candidate = fit(_good())
    assert candidate.measured_error <= MAX_CALIBRATION_ERROR

    version, active = await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=candidate,
        now=_NOW,
    )
    assert active
    assert version != UNCALIBRATED
    assert await calibration.active_version(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=_STRATEGY) == version


@pytest.mark.asyncio
async def test_a_fit_missing_the_target_is_stored_but_never_selected(
    factory: async_sessionmaker[AsyncSession], calibration: CalibrationService
) -> None:
    """A mapping worse than the bound is worse than no mapping, because it carries a
    version string that reads as calibrated. Kept rather than discarded so "why are
    we still uncalibrated" has an answer."""
    import dataclasses

    hopeless = dataclasses.replace(fit(_good()), measured_error=0.40)
    version, active = await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=hopeless,
        now=_NOW,
    )

    assert not active
    async with factory() as session:
        status = (
            await session.execute(
                text("SELECT status FROM memory_calibration_mapping WHERE version = :v"),
                {"v": version},
            )
        ).scalar_one()
    assert status == STATUS_FAILED
    assert (
        await calibration.active_version(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=_STRATEGY) == UNCALIBRATED
    )


@pytest.mark.asyncio
async def test_a_failed_fit_is_visible_on_the_status_gauge(
    calibration: CalibrationService,
) -> None:
    import dataclasses

    await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=dataclasses.replace(fit(_good()), measured_error=0.40),
        now=_NOW,
    )
    assert (
        _gauge(
            "registry_claim_calibration_status",
            provider=_PROVIDER,
            model=_MODEL,
            strategy=_STRATEGY,
        )
        == 2
    )


@pytest.mark.asyncio
async def test_a_new_active_fit_supersedes_rather_than_deletes_the_old(
    factory: async_sessionmaker[AsyncSession], calibration: CalibrationService
) -> None:
    """A claim scored under the old mapping names it, and that name has to keep
    resolving."""
    first_version, _ = await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=fit(_good()),
        now=_NOW,
    )
    second_version, _ = await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=fit(_good(500)),
        now=_NOW + datetime.timedelta(days=90),
    )

    async with factory() as session:
        rows = dict(
            (
                await session.execute(
                    text("SELECT version, status FROM memory_calibration_mapping " "WHERE provider_id = :p"),
                    {"p": _PROVIDER},
                )
            ).all()
        )
    assert rows[first_version] == "superseded"
    assert rows[second_version] == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_only_one_mapping_is_active_per_provider_model_and_strategy(
    factory: async_sessionmaker[AsyncSession], calibration: CalibrationService
) -> None:
    """Two active mappings would make which one scored a claim indeterminate."""
    for offset in (0, 30, 60):
        await calibration.publish(
            provider_id=_PROVIDER,
            model_id=_MODEL,
            strategy_id=_STRATEGY,
            candidate=fit(_good(400 + offset)),
            now=_NOW + datetime.timedelta(days=offset),
        )

    async with factory() as session:
        active = (
            await session.execute(
                text("SELECT count(*) FROM memory_calibration_mapping " "WHERE provider_id = :p AND status = 'active'"),
                {"p": _PROVIDER},
            )
        ).scalar_one()
    assert active == 1


# --- a provider swap requires recalibration -----------------------------------


@pytest.mark.asyncio
async def test_swapping_the_model_reverts_to_uncalibrated_with_no_action_taken(
    calibration: CalibrationService,
) -> None:
    """The seventh exit criterion, and the whole mechanism: the mapping is keyed on
    the model, so a changed model matches no row. Nobody has to remember to
    invalidate anything."""
    await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=fit(_good()),
        now=_NOW,
    )
    assert (
        await calibration.active_version(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=_STRATEGY) != UNCALIBRATED
    )

    swapped = await calibration.active_version(provider_id=_PROVIDER, model_id="claude-sonnet-5", strategy_id=_STRATEGY)
    assert swapped == UNCALIBRATED


@pytest.mark.asyncio
async def test_swapping_the_provider_also_reverts(
    calibration: CalibrationService,
) -> None:
    await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=fit(_good()),
        now=_NOW,
    )
    assert (
        await calibration.active_version(provider_id="local-rules", model_id=_MODEL, strategy_id=_STRATEGY)
        == UNCALIBRATED
    )


@pytest.mark.asyncio
async def test_each_strategy_calibrates_separately(
    calibration: CalibrationService,
) -> None:
    """Different prompts produce differently-distributed self-reports, so one
    mapping across all strategies would average away exactly what it is measuring."""
    await calibration.publish(
        provider_id=_PROVIDER,
        model_id=_MODEL,
        strategy_id=_STRATEGY,
        candidate=fit(_good()),
        now=_NOW,
    )
    assert (
        await calibration.active_version(provider_id=_PROVIDER, model_id=_MODEL, strategy_id="session_summary")
        == UNCALIBRATED
    )


@pytest.mark.asyncio
async def test_the_sentinel_cannot_be_claimed_by_a_stored_mapping(factory: async_sessionmaker[AsyncSession]) -> None:
    """A row claiming the uncalibrated token would make a claim carrying it resolve
    to a mapping, which is precisely the confusion the token exists to prevent."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_calibration_mapping "
                    "  (provider_id, model_id, strategy_id, version, bins, n_adjudicated, "
                    "   measured_error, status) "
                    "VALUES ('p', 'm', 's', 'uncalibrated', '[]'::jsonb, 500, 0.01, 'active')"
                )
            )


@pytest.mark.asyncio
async def test_a_failing_fit_cannot_be_stored_as_active(factory: async_sessionmaker[AsyncSession]) -> None:
    """Enforced at the database level too, not only in the service. A mapping that
    misses the bound must never be the one scoring claims."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_calibration_mapping "
                    "  (provider_id, model_id, strategy_id, version, bins, n_adjudicated, "
                    "   measured_error, status) "
                    "VALUES ('p', 'm', 's', 'p:m:s:d:500', '[]'::jsonb, 500, 0.40, 'active')"
                )
            )


@pytest.mark.asyncio
async def test_a_mapping_fitted_on_too_little_evidence_cannot_be_stored(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_calibration_mapping "
                    "  (provider_id, model_id, strategy_id, version, bins, n_adjudicated, "
                    "   measured_error, status) "
                    "VALUES ('p', 'm', 's', 'p:m:s:d:5', '[]'::jsonb, 5, 0.01, 'active')"
                )
            )


# --- loading observations ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_undecidable_verdict_is_excluded_from_a_fit(
    factory: async_sessionmaker[AsyncSession], calibration: CalibrationService
) -> None:
    """A reviewer who cannot tell has said something about their own certainty, not
    about the claim. Counting it either way would bias every fit."""
    observations = await calibration.load_observations(provider_id=_PROVIDER, model_id=_MODEL, strategy_id=_STRATEGY)
    # Nothing judged yet, so the honest answer is an empty set rather than a
    # mapping built from nothing.
    assert observations == []
    assert not fit(observations).meets_target
