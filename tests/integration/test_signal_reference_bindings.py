"""What a signal cited, stored, and provable against a real Postgres.

The unit suite fakes the session, so it can prove the service *intends* to write
a reference and a binding. It cannot prove the row lands: the reference table has
a unique index the upsert depends on, a CHECK that refuses an unfolded spelling,
a CHECK on classification, and a bindings CHECK that until this revision refused
`external_signal` outright. Every one of those is a way a write that reads
correctly still fails, and a faked session says yes to all of them.

So this suite drives the real service against the real schema, and it is also
where the ingest surface's own real-SQL coverage lives.

**Two properties are the point.** A stored signal is bound to each reference it
carried, so "which references did signal X carry" is a query rather than a
re-reading of a source-specific payload. And a reference row already present is
left exactly as it was found, so one subject's submission cannot rewrite what
another subject is recorded as having cited.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION
from contextplane.service.memory.source_governance import SourceGovernanceService
from contextplane.signals.envelope import (
    ExternalSignalEnvelopeV1,
    content_digest_for,
    normalize_references,
)
from contextplane.signals.ingest import SUBJECT_EXTERNAL_SIGNAL, SignalIngestService
from contextplane.types import SystemClock, TenantContext

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

#: One reference, as a producer sends it. Deliberately carrying an unfolded
#: system and a padded id: normalization is what makes two spellings one row, and
#: a specimen already in normal form would not exercise it.
_REFERENCE: dict[str, Any] = {
    "source_system": "GitHub",
    "source_namespace": "roughcompass/contextplane",
    "kind": "pull_request",
    "external_id": " 412 ",
    "classification": "internal",
    "external_authority": "platform-team",
}

_OTHER_REFERENCE: dict[str, Any] = {**_REFERENCE, "kind": "commit", "external_id": "fd9df6c0"}


@pytest_asyncio.fixture
async def ingest_fixture(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    """A tenant, an actor, a registered source and a declared policy.

    All four are real rows because all four are foreign keys or lookups on the
    path under test; a fixture that invented ids would prove the writes work
    against a database with its constraints switched off.
    """
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ctx = TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'signal bindings')"),
                {"t": tenant_id, "s": f"srb-{tenant_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'binding-test-actor', :sub, now())"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name, "
                    "                          config, is_active, created_at) "
                    "VALUES (:sid, :tid, 'github', 'ci', '{}'::jsonb, TRUE, :now)"
                ),
                {"sid": source_id, "tid": tenant_id, "now": _NOW},
            )

        governance = SourceGovernanceService(factory, clock=SystemClock())
        await governance.declare(ctx, source_id=source_id, authority_tier=AUTHORITY_OBSERVER_EXTRACTION)
        service = SignalIngestService(factory, clock=SystemClock(), governance=governance)

        yield {
            "factory": factory,
            "service": service,
            "ctx": ctx,
            "tenant_id": tenant_id,
            "source_id": source_id,
        }
    finally:
        await engine.dispose()


def _envelope(
    fixture: dict[str, Any], *, references: tuple[dict[str, Any], ...] = (_REFERENCE,), **overrides: Any
) -> ExternalSignalEnvelopeV1:
    unique = uuid.uuid4().hex[:12]
    fields: dict[str, Any] = {
        "source_id": fixture["source_id"],
        "source_system": "github",
        "source_event_id": f"github:workflow_run:{unique}",
        "producer_id": "signal-producer:github:roughcompass/contextplane",
        "producer_type": "external",
        "idempotency_key": f"delivery-{unique}",
        "classification": "internal",
        "schema_version": "external_signal.v1",
        "event_time": _NOW,
        "observed_time": _NOW,
        "references": normalize_references(references),
        "payload": {"conclusion": "success"},
    }
    return ExternalSignalEnvelopeV1(**{**fields, **overrides})


async def _bindings_for(fixture: dict[str, Any], signal_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every reference bound to one signal, joined through the junction."""
    async with fixture["factory"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT r.source_system, r.source_namespace, r.kind, r.external_id, r.classification, "
                    "       r.revision, r.collision_key, b.subject_type "
                    "FROM context_reference_bindings b "
                    "JOIN context_external_references r ON r.reference_id = b.reference_id "
                    "WHERE b.tenant_id = :t AND b.subject_type = :st AND b.subject_id = :s "
                    "ORDER BY r.kind"
                ),
                {"t": fixture["tenant_id"], "st": SUBJECT_EXTERNAL_SIGNAL, "s": signal_id},
            )
        ).mappings()
        return [dict(row) for row in rows]


async def _reference_rows(fixture: dict[str, Any], collision_key: str) -> list[dict[str, Any]]:
    async with fixture["factory"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT reference_id, classification, external_authority, revision, observed_at "
                    "FROM context_external_references WHERE tenant_id = :t AND collision_key = :k"
                ),
                {"t": fixture["tenant_id"], "k": collision_key},
            )
        ).mappings()
        return [dict(row) for row in rows]


# --- what a signal is recorded as having carried ------------------------------


@pytest.mark.asyncio
async def test_a_stored_signal_binds_every_reference_it_carried(ingest_fixture: dict[str, Any]) -> None:
    """The question this task exists to make answerable, asked of storage."""
    envelope = _envelope(ingest_fixture, references=(_REFERENCE, _OTHER_REFERENCE))

    ingested = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], envelope)

    bound = await _bindings_for(ingest_fixture, ingested.signal_id)
    assert [row["kind"] for row in bound] == ["commit", "pull_request"]
    assert {row["subject_type"] for row in bound} == {SUBJECT_EXTERNAL_SIGNAL}


@pytest.mark.asyncio
async def test_the_stored_reference_carries_the_normalized_identity(ingest_fixture: dict[str, Any]) -> None:
    """`GitHub` and ` 412 ` reach the row folded and trimmed. The schema's own
    CHECK refuses anything else, so a write that skipped normalization would fail
    here rather than sit in the table as a row that never collides."""
    envelope = _envelope(ingest_fixture)

    ingested = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], envelope)

    bound = await _bindings_for(ingest_fixture, ingested.signal_id)
    assert len(bound) == 1
    assert bound[0]["source_system"] == "github"
    assert bound[0]["external_id"] == "412"


@pytest.mark.asyncio
async def test_a_signal_carrying_no_references_binds_nothing(ingest_fixture: dict[str, Any]) -> None:
    """An observation about no particular piece of work is a real thing to
    report, and it must not leave a binding to a reference nobody named."""
    envelope = _envelope(ingest_fixture, references=())

    ingested = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], envelope)

    assert await _bindings_for(ingest_fixture, ingested.signal_id) == []


@pytest.mark.asyncio
async def test_two_signals_citing_one_reference_share_the_stored_row(ingest_fixture: dict[str, Any]) -> None:
    """One row per external thing, not one per mention -- otherwise a reader
    counting distinct sources over-counts every redelivery."""
    first = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], _envelope(ingest_fixture))
    second = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], _envelope(ingest_fixture))

    assert first.signal_id != second.signal_id
    key = first.references[0].collision_key()
    assert len(await _reference_rows(ingest_fixture, key)) == 1

    first_bound = await _bindings_for(ingest_fixture, first.signal_id)
    second_bound = await _bindings_for(ingest_fixture, second.signal_id)
    assert first_bound[0]["collision_key"] == second_bound[0]["collision_key"]


@pytest.mark.asyncio
async def test_a_replayed_signal_adds_no_further_bindings(ingest_fixture: dict[str, Any]) -> None:
    """A redelivery converges on the row already stored, and the bindings have to
    converge with it: a retry that appended would make a signal look like it
    cited the same work twice, which reads as corroboration."""
    envelope = _envelope(ingest_fixture, references=(_REFERENCE, _OTHER_REFERENCE))

    first = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], envelope)
    replay = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], envelope)

    assert replay.replayed is True
    assert replay.signal_id == first.signal_id
    assert len(await _bindings_for(ingest_fixture, first.signal_id)) == 2


# --- first write wins ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stored_reference_keeps_the_values_it_first_arrived_with(ingest_fixture: dict[str, Any]) -> None:
    """First write wins, deliberately, and this is the case that names the cost.

    The second submission carries a stronger classification and a revision the
    first did not have. Neither is applied. A reference row is shared by every
    subject citing it, so refreshing it here would change what *other* subjects
    are recorded as having cited, retroactively and with no evidence trail --
    which is a policy decision with its own requirements, not something ingestion
    should make on the strength of whichever submission happened to arrive later.
    """
    await ingest_fixture["service"].ingest(ingest_fixture["ctx"], _envelope(ingest_fixture))

    stronger = {**_REFERENCE, "classification": "restricted", "revision": "v2", "external_authority": "security"}
    second = await ingest_fixture["service"].ingest(
        ingest_fixture["ctx"], _envelope(ingest_fixture, references=(stronger,))
    )

    rows = await _reference_rows(ingest_fixture, second.references[0].collision_key())
    assert len(rows) == 1, "a differing classification must not create a second row for one collision key"
    assert rows[0]["classification"] == "internal"
    assert rows[0]["external_authority"] == "platform-team"
    assert rows[0]["revision"] is None


@pytest.mark.asyncio
async def test_the_later_signal_is_still_bound_to_the_reference_it_named(ingest_fixture: dict[str, Any]) -> None:
    """Declining to refresh the row is not declining to record the citation. The
    second signal cited that work and the binding says so."""
    await ingest_fixture["service"].ingest(ingest_fixture["ctx"], _envelope(ingest_fixture))

    stronger = {**_REFERENCE, "classification": "restricted"}
    second = await ingest_fixture["service"].ingest(
        ingest_fixture["ctx"], _envelope(ingest_fixture, references=(stronger,))
    )

    bound = await _bindings_for(ingest_fixture, second.signal_id)
    assert len(bound) == 1
    assert bound[0]["collision_key"] == second.references[0].collision_key()
    # The binding points at the row as it was first stored, not at a copy taken
    # from this submission.
    assert bound[0]["classification"] == "internal"


# --- storage is added beside identity, not instead of it ----------------------


@pytest.mark.asyncio
async def test_storing_the_bindings_does_not_change_what_identifies_the_signal(
    ingest_fixture: dict[str, Any],
) -> None:
    """The digest is still taken over the normalized envelope alone. If writing
    bindings had folded a reference_id or a stored row into it, an exact
    redelivery would stop converging and start reading as a conflict."""
    envelope = _envelope(ingest_fixture, references=(_REFERENCE, _OTHER_REFERENCE))

    ingested = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], envelope)

    assert ingested.content_digest == content_digest_for(envelope)


@pytest.mark.asyncio
async def test_the_caller_is_still_echoed_the_references_it_sent(ingest_fixture: dict[str, Any]) -> None:
    """The echo is how a producer correlates its own references with what the
    submission was identified by, and it is unchanged by storage."""
    envelope = _envelope(ingest_fixture, references=(_REFERENCE, _OTHER_REFERENCE))

    ingested = await ingest_fixture["service"].ingest(ingest_fixture["ctx"], envelope)

    assert ingested.references == envelope.normalized().references


@pytest.mark.asyncio
async def test_the_signal_row_holds_no_reference_columns_of_its_own(ingest_fixture: dict[str, Any]) -> None:
    """A signal's references live in the junction. A column here would be a
    second answer to the same question, and the two would drift."""
    async with ingest_fixture["factory"]() as session:
        columns = (
            (
                await session.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = 'external_signals'")
                )
            )
            .scalars()
            .all()
        )

    assert not [name for name in columns if "reference" in name]
