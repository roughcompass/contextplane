"""The operator health summary, and the provenance it refuses to drop.

The whole reason this endpoint exists is that the console had no trustworthy
source: parsing the exposition in a browser reports one arbitrary replica as if
it were the service, and pointing at a dashboard tool assumes infrastructure a
deployment may not have.

So the property worth testing is not that a count is returned — it is that no
number can leave here without saying how far to trust it. A queue depth counted
from the table and a counter scraped from this process look identical once
rendered, and only one of them is correct under more than one replica.
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from contextplane.service.platform.operational_health import (
    OperationalHealth,
    Reading,
    collect_operational_health,
)

_NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=datetime.UTC)


def _session_factory(
    *,
    counts: list[int] | None = None,
    fail: bool = False,
    oldest_open_proposal_at: datetime.datetime | None = None,
) -> MagicMock:
    """`counts` supplies one value per `_QUEUE_COUNTS` entry, in order (`_count`
    reads each via `scalar_one`). The call after those is the oldest-open-proposal
    age query, which reads `scalar_one_or_none` instead -- `None` there is the
    real "no proposal is open" case, not a failure, and renders as an age of zero.
    """
    session = AsyncMock()
    if fail:
        session.execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    else:
        from contextplane.service.platform.operational_health import _QUEUE_COUNTS

        values = list(counts if counts is not None else [0] * len(_QUEUE_COUNTS))
        calls = 0

        async def execute(*_a: object, **_kw: object) -> MagicMock:
            nonlocal calls
            calls += 1
            result = MagicMock()
            if calls <= len(values):
                result.scalar_one = MagicMock(return_value=values[calls - 1])
            else:
                result.scalar_one_or_none = MagicMock(return_value=oldest_open_proposal_at)
            return result

        session.execute = execute

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


async def _collect(**kwargs: object) -> OperationalHealth:
    return await collect_operational_health(_session_factory(**kwargs), now=_NOW)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_every_reading_declares_its_scope_and_kind() -> None:
    """The invariant the response type exists to hold.

    A reading without provenance renders as a bare number, and a bare number
    from this endpoint is indistinguishable from a service-wide total when it
    may be one replica's count since it last restarted.
    """
    health = await _collect(counts=[3, 0, 7, 1, 2])
    readings = [*health.queues, *health.data_quality]
    assert readings

    for reading in readings:
        assert reading.scope in {"cluster", "process"}
        assert reading.kind in {"gauge", "counter"}


@pytest.mark.asyncio
async def test_queue_depths_are_cluster_scoped_and_carry_no_instance() -> None:
    # Counted from the table at read time, so they belong to the deployment
    # rather than to whichever replica answered. Attaching an instance would
    # imply the number was only true there.
    health = await _collect(counts=[3, 0, 7, 1, 2])
    for reading in health.queues:
        assert reading.scope == "cluster"
        assert reading.kind == "gauge"
        assert reading.instance is None


@pytest.mark.asyncio
async def test_data_quality_counters_are_process_scoped_and_name_their_replica() -> None:
    """The half a reader must not mistake for a total.

    Without the instance, two reads that landed on different replicas look like
    a counter that went backwards rather than two different processes.
    """
    health = await _collect()
    assert health.data_quality
    for reading in health.data_quality:
        assert reading.scope == "process"
        assert reading.kind == "counter"
        assert reading.instance


@pytest.mark.asyncio
async def test_an_unreadable_table_reports_null_rather_than_zero() -> None:
    """Zero is a claim; null is the absence of one.

    A table that is missing, locked, or unreachable is not an empty queue.
    Reporting one as the other shows the healthiest possible state at exactly
    the moment something is wrong, and an operator would believe it.
    """
    health = await _collect(fail=True)
    assert health.queues
    for reading in health.queues:
        assert reading.value is None


@pytest.mark.asyncio
async def test_the_counted_values_are_reported_as_given() -> None:
    health = await _collect(counts=[3, 0, 7, 1, 5])
    # The trailing 0.0 is the oldest-open-proposal age: no proposal was mocked
    # as open, and an empty review queue reads as zero, not unreadable.
    assert [r.value for r in health.queues] == [3.0, 0.0, 7.0, 1.0, 5.0, 0.0]


@pytest.mark.asyncio
async def test_abandoned_deliveries_say_why_they_matter() -> None:
    # An exhausted delivery is the one queue value that is actionable on sight:
    # a subscriber is missing notifications and cannot know it.
    health = await _collect(counts=[0, 0, 0, 2, 0])
    failed = next(r for r in health.queues if r.key == "webhook_failed")
    assert failed.actionable and "never arrive" in failed.actionable


@pytest.mark.asyncio
async def test_every_data_quality_counter_explains_its_consequence() -> None:
    # These are published despite the per-replica caveat because each is
    # actionable on any non-zero value. A number nobody can act on would not be
    # worth the caveat.
    health = await _collect()
    for reading in health.data_quality:
        assert reading.actionable


@pytest.mark.asyncio
async def test_no_reading_carries_tenant_or_actor_identity() -> None:
    """The disclosure boundary for a surface a tenant admin can reach.

    The gate is tenant-admin because no service-operator identity exists in this
    API. That is only defensible while the payload stays service-global: no
    tenant, no actor, no entity, no row content.
    """
    health = await _collect(counts=[1, 1, 1, 1, 1])
    forbidden = ("tenant", "actor", "entity", "session", "email", "subject")
    for reading in [*health.queues, *health.data_quality]:
        blob = f"{reading.key} {reading.label} {reading.actionable or ''}".lower()
        assert not any(word in blob.split() for word in forbidden)


@pytest.mark.asyncio
async def test_a_declared_counter_with_no_samples_reads_as_zero_not_unavailable() -> None:
    """The distinction the page's whole vocabulary rests on.

    Three of the four data-quality counters carry labels, and prometheus_client
    emits no series for a labelled counter until some label combination is first
    used. On a healthy process nothing has been dropped, so no `reason` label
    has ever been touched and the family publishes nothing at all.

    Reading that as "unavailable" is the worst available answer: it is the same
    word the page uses for a table it could not query, so a perfectly working
    counter renders identically to a broken one — and an operator learns to
    ignore the column that tells them a principal silently lost a role.
    """
    # Importing is what declares them: a counter registers with the default
    # registry when its module is first imported, and in a live process the
    # middleware and parser are always loaded.
    import contextplane.api.middleware.tenant
    import contextplane.auth.entitlements.parser  # noqa: F401

    health = await _collect()
    by_key = {r.key: r for r in health.data_quality}

    # Asserted as "a number, not unavailable" rather than as exactly zero. The
    # distinction this test exists for is zero versus unavailable, and the exact value
    # is not assertable in a shared process: these counters live in the default
    # registry, so any other test in the run that exercises entitlement parsing
    # increments them and the reading is legitimately non-zero by the time this runs.
    # Pinning zero made the test pass alone and fail in the suite, which taught
    # nothing about the behaviour it was written to protect.
    # authority_parse_failures is gone with the legacy SEAL/verb grammar it
    # instrumented: the counter could never increment once the shim died.
    for key in (
        "entitlement_dropped_entries",
        "entitlement_parse_ignored",
    ):
        assert by_key[key].value is not None, (
            f"{key} reads as unavailable; a declared counter with no samples must read as "
            "a number, or a working counter renders identically to a broken one"
        )
        assert by_key[key].value >= 0.0


@pytest.mark.asyncio
async def test_an_undefined_counter_family_is_the_only_null() -> None:
    # `None` stays reserved for a family this build does not define, which is
    # what makes zero trustworthy everywhere else.
    from contextplane.service.platform.operational_health import _counter_total

    assert _counter_total("a_family_no_build_defines_total") is None


def test_a_reading_cannot_be_built_without_provenance() -> None:
    # Structural, not conventional. If scope and kind had defaults they would
    # eventually be omitted at a call site, and the omission would be invisible.
    with pytest.raises(TypeError):
        Reading(key="k", label="l", value=1.0)  # type: ignore[call-arg]
