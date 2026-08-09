"""Three producers, one contract, one table — proven against a real database.

The claim this suite exists to make good is that the generic envelope was not
shaped around whoever happened to be implemented first. A human reporting an
outcome, an agent reporting one, and a CI system reporting its own run all reach
the same table through the same service, with nothing source-specific in between.

It runs against Postgres rather than a fake session because the properties under
test are storage properties: that three producers land in one table, that a
redelivery converges on the row already there, and that a refused submission
leaves nothing behind. A fake would let the code agree with itself about all
three.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from contextplane.exceptions import ValidationError
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION, AUTHORITY_OBSERVER_HUMAN
from contextplane.signals.adapters import (
    GITHUB_ACTIONS_SOURCE_SYSTEM,
    GithubDeliveryRejected,
    direct_envelope,
    github_workflow_run_envelope,
    projected_payload,
)
from contextplane.signals.ingest import SignalIngestService
from contextplane.types import SystemClock, TenantContext

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "signals" / "github_workflow_run_completed.json"
_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _async_url(sync_or_async: str) -> str:
    return sync_or_async.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


@pytest.fixture(scope="module")
def sync_engine(pg_container: str) -> Iterator[Engine]:
    engine = create_engine(_sync_url(pg_container))
    yield engine
    engine.dispose()


@pytest.fixture
def delivery() -> dict[str, Any]:
    """The captured delivery, exactly as the fixture holds it."""
    loaded: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
def tenant_and_sources(sync_engine: Engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A tenant, an actor, and two registered sources: one direct, one external.

    Two sources rather than one because their declared authority differs — a
    person reporting an outcome and a CI system reporting a run are not the same
    kind of witness, and the ledger stores what each was registered with.
    """
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    direct_source, github_source = uuid.uuid4(), uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'adapter test')"),
            {"t": tenant_id, "s": f"ad-{tenant_id.hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind)"
                " VALUES (:a, :t, :sub, 'Adapter Tester', 'human')"
            ),
            {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:8]}"},
        )
        for source_id, kind, tier in (
            (direct_source, "direct", AUTHORITY_OBSERVER_HUMAN),
            (github_source, GITHUB_ACTIONS_SOURCE_SYSTEM, AUTHORITY_OBSERVER_EXTRACTION),
        ):
            conn.execute(
                text(
                    "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name)"
                    " VALUES (:s, :t, :k, :k)"
                ),
                {"s": source_id, "t": tenant_id, "k": kind},
            )
            conn.execute(
                text(
                    "INSERT INTO memory_source_governance (source_id, tenant_id, authority_tier)"
                    " VALUES (:s, :t, :tier)"
                ),
                {"s": source_id, "t": tenant_id, "tier": tier},
            )
    return tenant_id, actor_id, direct_source, github_source


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])


def _ingest(pg_container: str, ctx: TenantContext, envelope: Any) -> Any:
    """Drive the real service against the real database."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from contextplane.service.memory.source_governance import SourceGovernanceService

    async def run() -> Any:
        engine = create_async_engine(_async_url(pg_container))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = SignalIngestService(
                factory,
                clock=SystemClock(),
                governance=SourceGovernanceService(factory, clock=SystemClock()),
            )
            return await service.ingest(ctx, envelope)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_the_fixture_carries_the_delivery_guid_it_is_cross_checked_against(delivery: dict[str, Any]) -> None:
    """The sign-off is mechanical rather than testimonial.

    A fixture that only claimed to be real would attest to somebody's memory. This
    one carries the delivery's own GUID, and that GUID is what the stored
    submission key is checked against below — so the fixture attests to a
    verifiable capture.
    """
    assert delivery["_capture"]["delivery_guid"]
    assert delivery["_capture"]["x_github_event"] == "workflow_run"
    assert delivery["action"] == "completed"


def test_the_projection_keeps_only_the_allowlist(delivery: dict[str, Any]) -> None:
    """The raw body never reaches storage, and this is what that means concretely.

    The fixture deliberately retains the fields GitHub really sends -- commit
    message, actor, sender, log URLs -- because a pre-trimmed fixture would prove
    nothing about the projection.
    """
    projected = projected_payload(delivery["workflow_run"], delivery["repository"])

    assert projected["repository"] == "roughcompass/contextplane"
    assert projected["run_attempt"] == 1
    assert projected["conclusion"] == "success"

    # The places free text and identity actually live upstream.
    for dropped in ("head_commit", "actor", "triggering_actor", "sender", "logs_url", "jobs_url", "display_title"):
        assert dropped not in projected, f"{dropped} survived the projection"
    assert "A Maintainer" not in json.dumps(projected), "a person's name reached the projection"


def test_three_producers_reach_one_table(
    pg_container: str, sync_engine: Engine, tenant_and_sources: tuple[uuid.UUID, ...], delivery: dict[str, Any]
) -> None:
    """The acceptance case: a human, an agent and an external outcome, one ledger.

    Nothing source-specific stands between any of them and storage -- no per-source
    table, no second service, no adapter-side scan.
    """
    tenant_id, actor_id, direct_source, github_source = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)

    human = _ingest(
        pg_container,
        ctx,
        direct_envelope(
            ctx,
            source_id=direct_source,
            producer_type="human",
            observation={"note": "the rollout needed a manual step nobody documented"},
            occurred_at=_NOW,
            idempotency_key="direct-human-0001",
        ),
    )
    agent = _ingest(
        pg_container,
        ctx,
        direct_envelope(
            ctx,
            source_id=direct_source,
            producer_type="agent",
            observation={"note": "retrieved context omitted the runbook"},
            occurred_at=_NOW,
            idempotency_key="direct-agent-0001",
        ),
    )
    external = _ingest(
        pg_container,
        ctx,
        github_workflow_run_envelope(
            ctx,
            source_id=github_source,
            delivery=delivery,
            delivery_guid=delivery["_capture"]["delivery_guid"],
            received_at=_NOW,
            producer_id="signal-producer:github-actions:roughcompass/contextplane",
        ),
    )

    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT producer_type, source_system, idempotency_key, authority FROM external_signals"
                " WHERE tenant_id = :t ORDER BY producer_type"
            ),
            {"t": tenant_id},
        ).all()

    assert [r.producer_type for r in rows] == ["agent", "external", "human"]
    assert {r.source_system for r in rows} == {"direct", GITHUB_ACTIONS_SOURCE_SYSTEM}
    assert len({human.signal_id, agent.signal_id, external.signal_id}) == 3

    # The submission key of the external row is the delivery's own GUID: the
    # fixture's sign-off and the stored record refer to the same capture.
    keys = {r.idempotency_key for r in rows}
    assert delivery["_capture"]["delivery_guid"] in keys


def test_each_source_keeps_the_authority_it_was_registered_with(
    pg_container: str, sync_engine: Engine, tenant_and_sources: tuple[uuid.UUID, ...], delivery: dict[str, Any]
) -> None:
    """A person and a CI system are not the same kind of witness.

    The authority is read from the source's registration, never from the adapter
    or the payload -- an adapter that could name its own would name the strongest.
    """
    tenant_id, actor_id, direct_source, github_source = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)

    human = _ingest(
        pg_container,
        ctx,
        direct_envelope(
            ctx,
            source_id=direct_source,
            producer_type="human",
            observation={"note": "reported by hand"},
            occurred_at=_NOW,
            idempotency_key="direct-authority-0001",
        ),
    )
    external = _ingest(
        pg_container,
        ctx,
        github_workflow_run_envelope(
            ctx,
            source_id=github_source,
            delivery=delivery,
            delivery_guid="authority-check-guid-0001",
            received_at=_NOW,
            producer_id="signal-producer:github-actions:roughcompass/contextplane",
        ),
    )
    assert human.authority == AUTHORITY_OBSERVER_HUMAN
    assert external.authority == AUTHORITY_OBSERVER_EXTRACTION


def test_a_redelivered_webhook_converges_on_the_stored_row(
    pg_container: str, sync_engine: Engine, tenant_and_sources: tuple[uuid.UUID, ...], delivery: dict[str, Any]
) -> None:
    """Webhooks are redelivered as a matter of course; that must not double-count.

    The submission key is the transport's own delivery GUID, so the second arrival
    of one delivery finds the row the first stored.
    """
    tenant_id, actor_id, _direct, github_source = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)

    def send() -> Any:
        return _ingest(
            pg_container,
            ctx,
            github_workflow_run_envelope(
                ctx,
                source_id=github_source,
                delivery=delivery,
                delivery_guid="redelivery-guid-0002",
                received_at=_NOW,
                producer_id="signal-producer:github-actions:roughcompass/contextplane",
            ),
        )

    first, second = send(), send()
    assert first.replayed is False
    assert second.replayed is True
    assert second.signal_id == first.signal_id

    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT count(*) FROM external_signals WHERE idempotency_key = 'redelivery-guid-0002'")
        ).scalar_one()
    assert stored == 1


def test_a_rerun_is_a_second_occurrence_not_an_overwrite(
    pg_container: str, sync_engine: Engine, tenant_and_sources: tuple[uuid.UUID, ...], delivery: dict[str, Any]
) -> None:
    """Both runs happened; the earlier one is what a derivation may already cite."""
    tenant_id, actor_id, _direct, github_source = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)

    rerun = json.loads(json.dumps(delivery))
    rerun["workflow_run"]["run_attempt"] = 2
    rerun["workflow_run"]["updated_at"] = "2026-08-09T12:31:07Z"

    first = _ingest(
        pg_container,
        ctx,
        github_workflow_run_envelope(
            ctx,
            source_id=github_source,
            delivery=delivery,
            delivery_guid="attempt-one-guid",
            received_at=_NOW,
            producer_id="signal-producer:github-actions:roughcompass/contextplane",
        ),
    )
    second = _ingest(
        pg_container,
        ctx,
        github_workflow_run_envelope(
            ctx,
            source_id=github_source,
            delivery=rerun,
            delivery_guid="attempt-two-guid",
            received_at=_NOW,
            producer_id="signal-producer:github-actions:roughcompass/contextplane",
        ),
    )
    assert first.signal_id != second.signal_id
    with sync_engine.connect() as conn:
        event_ids = {
            row.source_event_id
            for row in conn.execute(
                text("SELECT source_event_id FROM external_signals WHERE signal_id IN (:a, :b)"),
                {"a": first.signal_id, "b": second.signal_id},
            ).all()
        }
    assert any(e.endswith(":1") for e in event_ids)
    assert any(e.endswith(":2") for e in event_ids)


def test_an_adapter_submission_still_passes_the_admission_floor(
    pg_container: str, sync_engine: Engine, tenant_and_sources: tuple[uuid.UUID, ...], delivery: dict[str, Any]
) -> None:
    """No adapter has a way around the floor, and none carries a scanner of its own.

    The credential here is fabricated. It is planted in a field the projection
    keeps, because planting it in one the projection drops would test the
    projection rather than the floor -- and the floor is what has to hold when a
    future adapter projects a field this one does not.
    """
    tenant_id, actor_id, direct_source, _github = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)

    with pytest.raises(ValidationError, match="prohibited class"):
        _ingest(
            pg_container,
            ctx,
            direct_envelope(
                ctx,
                source_id=direct_source,
                producer_type="human",
                observation={"note": "cloned with ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"},
                occurred_at=_NOW,
                idempotency_key="floor-check-0001",
            ),
        )

    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT count(*) FROM external_signals WHERE idempotency_key = 'floor-check-0001'")
        ).scalar_one()
    assert stored == 0, "a refused adapter submission reached the ledger"


def test_a_direct_reporter_cannot_report_under_another_identity(
    tenant_and_sources: tuple[uuid.UUID, ...],
) -> None:
    """The adapter takes the reporter from the caller's context, not from an argument.

    There is no parameter to get wrong: attribution that names somebody else is
    worse than anonymous, because it looks attributed.
    """
    tenant_id, actor_id, direct_source, _github = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)
    envelope = direct_envelope(
        ctx,
        source_id=direct_source,
        producer_type="human",
        observation={"note": "mine"},
        occurred_at=_NOW,
    )
    assert envelope.producer_id == str(actor_id)


def test_a_direct_report_keeps_the_two_times_apart(tenant_and_sources: tuple[uuid.UUID, ...]) -> None:
    """Reporting on Monday what happened on Friday is a late report, and it should read as one."""
    tenant_id, actor_id, direct_source, _github = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)
    friday = datetime.datetime(2026, 8, 7, 9, 0, tzinfo=datetime.UTC)
    monday = datetime.datetime(2026, 8, 10, 9, 0, tzinfo=datetime.UTC)
    envelope = direct_envelope(
        ctx,
        source_id=direct_source,
        producer_type="human",
        observation={"note": "late"},
        occurred_at=friday,
        observed_at=monday,
    )
    assert envelope.event_time == friday
    assert envelope.observed_time == monday


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        pytest.param(lambda d: d.update({"action": "requested"}), "wrong action", id="not-completed"),
        pytest.param(lambda d: d["workflow_run"].pop("run_attempt"), "missing attempt", id="no-attempt"),
        pytest.param(lambda d: d["workflow_run"].update({"updated_at": "not a time"}), "bad time", id="bad-time"),
    ],
)
def test_a_delivery_the_adapter_cannot_identify_is_refused(
    delivery: dict[str, Any], mutate: Any, reason: str, tenant_and_sources: tuple[uuid.UUID, ...]
) -> None:
    """Refused rather than best-effort translated.

    A signal whose identity was guessed cannot be deduplicated against the next
    one, so a delivery missing a part of that identity is not translated at all.
    """
    tenant_id, actor_id, _direct, github_source = tenant_and_sources
    broken = json.loads(json.dumps(delivery))
    mutate(broken)
    with pytest.raises(GithubDeliveryRejected):
        github_workflow_run_envelope(
            _ctx(tenant_id, actor_id),
            source_id=github_source,
            delivery=broken,
            delivery_guid="broken-guid",
            received_at=_NOW,
            producer_id="signal-producer:github-actions:roughcompass/contextplane",
        )


def test_a_naive_timestamp_is_refused(delivery: dict[str, Any], tenant_and_sources: tuple[uuid.UUID, ...]) -> None:
    """An instant with no offset is ambiguous by exactly the amount nobody recorded."""
    tenant_id, actor_id, _direct, github_source = tenant_and_sources
    broken = json.loads(json.dumps(delivery))
    broken["workflow_run"]["updated_at"] = "2026-08-09T11:54:31"
    with pytest.raises(GithubDeliveryRejected, match="timezone"):
        github_workflow_run_envelope(
            _ctx(tenant_id, actor_id),
            source_id=github_source,
            delivery=broken,
            delivery_guid="naive-guid",
            received_at=_NOW,
            producer_id="signal-producer:github-actions:roughcompass/contextplane",
        )


def test_one_reporter_may_say_two_things_at_the_same_instant(
    pg_container: str, sync_engine: Engine, tenant_and_sources: tuple[uuid.UUID, ...]
) -> None:
    """Regression: the occurrence id includes the observation, not just who and when.

    The acceptance case found this. With the id derived from actor and timestamp
    alone, a person filing two notes at once — or an agent reporting a batch —
    collided as "the same occurrence with different content" and the second was
    refused as a conflict. Two different observations at one instant are two
    occurrences.
    """
    tenant_id, actor_id, direct_source, _github = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)
    instant = datetime.datetime(2026, 8, 9, 15, 30, tzinfo=datetime.UTC)

    first = _ingest(
        pg_container,
        ctx,
        direct_envelope(
            ctx,
            source_id=direct_source,
            producer_type="human",
            observation={"note": "the runbook was missing a step"},
            occurred_at=instant,
            idempotency_key="same-instant-a",
        ),
    )
    second = _ingest(
        pg_container,
        ctx,
        direct_envelope(
            ctx,
            source_id=direct_source,
            producer_type="human",
            observation={"note": "and the dashboard was stale"},
            occurred_at=instant,
            idempotency_key="same-instant-b",
        ),
    )
    assert first.signal_id != second.signal_id


def test_the_same_observation_resent_is_still_one_occurrence(
    pg_container: str, sync_engine: Engine, tenant_and_sources: tuple[uuid.UUID, ...]
) -> None:
    """The property the digest must not break while fixing the collision above.

    A reporter resending the identical observation under a fresh submission key
    finds the stored row rather than filing a second complaint about one event.
    """
    tenant_id, actor_id, direct_source, _github = tenant_and_sources
    ctx = _ctx(tenant_id, actor_id)
    instant = datetime.datetime(2026, 8, 9, 16, 0, tzinfo=datetime.UTC)
    observation = {"note": "one event, reported twice"}

    def send(key: str) -> Any:
        return _ingest(
            pg_container,
            ctx,
            direct_envelope(
                ctx,
                source_id=direct_source,
                producer_type="human",
                observation=observation,
                occurred_at=instant,
                idempotency_key=key,
            ),
        )

    first = send("resend-key-one")
    second = send("resend-key-two")
    assert second.replayed is True
    assert second.signal_id == first.signal_id
