"""Neither side going down may corrupt the other, and neither may go down quietly.

Two systems talk here, and the failure that matters is different in each
direction. This module proves both against a real database, because both are
claims about what is *stored* after something went wrong — and a fake would
happily agree that nothing was corrupted.

**Orchestrator down: nothing to corrupt, because nothing was waiting.** Every
outcome submission is initiated by the orchestrator, so an outage is not a
half-finished transaction on this side — it is an absence. What has to be proved
is what happens on recovery: a backlog replayed hours late must land once, with
its original times intact, and must not become a second copy of work already
recorded. The times matter as much as the count. A replay that restamped its
own event time would launder the outage into looking like prompt reporting, and
the lag it hid is one of the things the pilot is measuring.

**Registry down: the answer must be loud.** This is the more dangerous
direction. An outcome that fails to submit is retried; a context request that
quietly returns less than it should is *used*. So a resolution that could not
read everything must never describe itself as whole, and one whose evidence
could not be written must fail rather than return an answer that looks audited
and is not. Both are proved here by breaking the thing underneath and reading
what comes back.

**Loudness is proved, never adjusted.** The quality derivation and the resolve
sequence belong to work already delivered; nothing in this module edits them.
If an assertion below fails, the finding is a defect in that path — the fix is
not to soften the assertion.

One thing this module deliberately does not test: a Registry-to-orchestrator
notification channel. Subscriptions are excluded from the pilot, so no such
channel exists, and a test standing in for one would attest to a design that
was not built.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.context.assembler import ArmOutcome
from contextplane.context.receipts import ContextReceiptService
from contextplane.context.resolve import ContextResolver
from contextplane.context.schemas.envelope import BLOCK_NAMES, ENVELOPE_COMPLETE
from contextplane.context.schemas.reference import normalize_reference
from contextplane.service.governance.authority import AUTHORITY_OBSERVER_EXTRACTION
from contextplane.signals.adapters.control_plane import control_plane_outcome_envelope
from contextplane.signals.ingest import SignalIngestRefused, SignalIngestService
from contextplane.signals.reads import UnjoinedOutcomePage, UnjoinedOutcomeReadService
from contextplane.types import SystemClock, TenantContext

#: When the work actually concluded. Deliberately well before the replay below,
#: so "the outage is visible in the stored times" is a real interval rather than
#: a rounding difference.
_CONCLUDED_AT = datetime.datetime(2026, 8, 9, 11, 52, 19, tzinfo=datetime.UTC)

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


def _register_seat(sync_engine: Engine, *, ceiling: int) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A tenant, an actor, and one outcome seat with a declared ceiling.

    The ceiling is a parameter because one test needs it small enough to reach.
    Setting it per test rather than globally keeps the drain test honest: it
    proves a bounded drain is refused, not that the default happens to be low.
    """
    tenant_id, actor_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :slug, 'availability test')"),
            {"t": tenant_id, "slug": f"avail-{tenant_id.hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, oidc_subject, display_name, actor_kind)"
                " VALUES (:a, :t, :sub, 'CI seat', 'service')"
            ),
            {"a": actor_id, "t": tenant_id, "sub": f"s-{actor_id.hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO sync_sources (source_id, tenant_id, source_type, display_name)"
                " VALUES (:s, :t, 'github-actions', 'github-actions')"
            ),
            {"s": source_id, "t": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO memory_source_governance"
                " (source_id, tenant_id, authority_tier, ingest_ceiling, window_seconds)"
                " VALUES (:s, :t, :tier, :ceiling, 3600)"
            ),
            {"s": source_id, "t": tenant_id, "tier": AUTHORITY_OBSERVER_EXTRACTION, "ceiling": ceiling},
        )
    return tenant_id, actor_id, source_id


@pytest.fixture
def seat(sync_engine: Engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    return _register_seat(sync_engine, ceiling=1000)


def _ctx(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["producer"])


def _ingest(pg_container: str, ctx: TenantContext, envelope: Any) -> Any:
    async def run() -> Any:
        engine = create_async_engine(_async_url(pg_container))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            from contextplane.service.memory.source_governance import SourceGovernanceService

            service = SignalIngestService(
                factory,
                clock=SystemClock(),
                governance=SourceGovernanceService(factory, clock=SystemClock()),
            )
            return await service.ingest(ctx, envelope)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _outcome(
    *,
    source_id: uuid.UUID,
    producer_id: str,
    object_id: str,
    concluded_at: datetime.datetime,
    received_at: datetime.datetime,
    idempotency_key: str,
) -> Any:
    return control_plane_outcome_envelope(
        source_id=source_id,
        source_system="github-actions",
        producer_id=producer_id,
        outcome={
            "object": "workflow_run",
            "object_id": object_id,
            "conclusion": "success",
            "repository": "acme/payments-api",
        },
        references=(
            normalize_reference(
                {
                    "source_system": "github",
                    "source_namespace": "acme",
                    "kind": "repository",
                    "external_id": "acme/payments-api",
                    "classification": "internal",
                    "external_authority": "acme/platform",
                }
            ),
        ),
        concluded_at=concluded_at,
        received_at=received_at,
        idempotency_key=idempotency_key,
    )


def _signal_rows(sync_engine: Engine, tenant_id: uuid.UUID) -> list[Any]:
    with sync_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT signal_id, source_event_id, event_time, observed_time, ingested_at"
                    "  FROM external_signals WHERE tenant_id = :t ORDER BY source_event_id"
                ),
                {"t": tenant_id},
            ).all()
        )


# --- the orchestrator was down -----------------------------------------------


def test_a_backlog_replayed_after_recovery_lands_once_each(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """Recovery is a drain, not a reconciliation.

    Three outcomes that could not be submitted during an outage are submitted
    afterwards. Each is its own occurrence and each lands once. Nothing had to
    be repaired first, because nothing on this side was mid-flight.
    """
    tenant_id, actor_id, source_id = seat
    ctx = _ctx(tenant_id, actor_id)

    for index in range(3):
        _ingest(
            pg_container,
            ctx,
            _outcome(
                source_id=source_id,
                producer_id=str(actor_id),
                object_id=f"run-{index}",
                concluded_at=_CONCLUDED_AT + datetime.timedelta(minutes=index),
                received_at=_NOW,
                idempotency_key=f"drain-{index}-{uuid.uuid4()}",
            ),
        )

    rows = _signal_rows(sync_engine, tenant_id)
    assert [row.source_event_id for row in rows] == [
        "github-actions:workflow_run:run-0:1",
        "github-actions:workflow_run:run-1:1",
        "github-actions:workflow_run:run-2:1",
    ]


def test_a_late_replay_keeps_the_original_times_so_the_outage_stays_visible(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The lag is evidence, and a replay that restamped itself would erase it.

    The submission carries when the work concluded, not when the queue finally
    drained. So after recovery the gap between the concluded instant and the
    admitted instant is large — which is exactly what an operator reading the
    ledger should see, rather than a tidy record implying prompt reporting.
    """
    tenant_id, actor_id, source_id = seat
    ctx = _ctx(tenant_id, actor_id)

    _ingest(
        pg_container,
        ctx,
        _outcome(
            source_id=source_id,
            producer_id=str(actor_id),
            object_id="late-run",
            concluded_at=_CONCLUDED_AT,
            received_at=_CONCLUDED_AT + datetime.timedelta(seconds=5),
            idempotency_key=f"late-{uuid.uuid4()}",
        ),
    )

    (row,) = [r for r in _signal_rows(sync_engine, tenant_id) if "late-run" in r.source_event_id]

    assert row.event_time == _CONCLUDED_AT, "the replay must not restamp when the work concluded"
    assert row.ingested_at > row.event_time, "and the admitted instant is the Registry's own clock"
    assert row.ingested_at - row.event_time > datetime.timedelta(
        hours=1
    ), "the outage has to remain legible in the stored times rather than being laundered by the replay"


def test_the_same_outcome_replayed_twice_is_not_a_second_record(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """A retrying orchestrator must not double-count the thing being measured.

    The realistic case is a dropped response rather than a duplicate send: the
    submission succeeded and the acknowledgement was lost, so the client sends
    it again and has no way to know which happened.
    """
    tenant_id, actor_id, source_id = seat
    ctx = _ctx(tenant_id, actor_id)
    key = f"dropped-ack-{uuid.uuid4()}"

    def submit() -> Any:
        return _ingest(
            pg_container,
            ctx,
            _outcome(
                source_id=source_id,
                producer_id=str(actor_id),
                object_id="retried-run",
                concluded_at=_CONCLUDED_AT,
                received_at=_NOW,
                idempotency_key=key,
            ),
        )

    first = submit()
    second = submit()

    assert second.signal_id == first.signal_id
    assert len([r for r in _signal_rows(sync_engine, tenant_id) if "retried-run" in r.source_event_id]) == 1


def test_a_burst_drain_is_bounded_by_the_ceiling_while_replays_are_not(pg_container: str, sync_engine: Engine) -> None:
    """The one interaction that makes a recovery plan realizable or not.

    Replays of rows already stored are recognised before the ceiling is
    consulted, so re-sending a backlog costs nothing. First-time submissions do
    spend it, so a backlog larger than the window is refused partway through —
    and the refusal is "not right now", not "this is wrong", which is the
    distinction an operator's retry logic depends on.

    Both halves are asserted together deliberately. Either alone would support
    the wrong operational conclusion: that a drain can never be throttled, or
    that retrying a throttled drain makes it worse.
    """
    tenant_id, actor_id, source_id = _register_seat(sync_engine, ceiling=2)
    ctx = _ctx(tenant_id, actor_id)

    stored = [
        _ingest(
            pg_container,
            ctx,
            _outcome(
                source_id=source_id,
                producer_id=str(actor_id),
                object_id=f"burst-{index}",
                concluded_at=_CONCLUDED_AT,
                received_at=_NOW,
                idempotency_key=f"burst-{index}",
            ),
        )
        for index in range(2)
    ]

    # The third first-time submission is over the declared ceiling.
    with pytest.raises(SignalIngestRefused):
        _ingest(
            pg_container,
            ctx,
            _outcome(
                source_id=source_id,
                producer_id=str(actor_id),
                object_id="burst-2",
                concluded_at=_CONCLUDED_AT,
                received_at=_NOW,
                idempotency_key="burst-2",
            ),
        )

    # Re-sending what is already stored still succeeds, over the same exhausted
    # ceiling: a client retrying a drain it could not finish does not make its
    # own situation worse.
    replayed = _ingest(
        pg_container,
        ctx,
        _outcome(
            source_id=source_id,
            producer_id=str(actor_id),
            object_id="burst-0",
            concluded_at=_CONCLUDED_AT,
            received_at=_NOW,
            idempotency_key="burst-0",
        ),
    )

    assert replayed.signal_id == stored[0].signal_id
    assert len(_signal_rows(sync_engine, tenant_id)) == 2, "the refused submission left nothing behind"


# --- the Registry was down ----------------------------------------------------


class _UnreachableStorage(RuntimeError):
    """What a read raises when the store behind it cannot answer.

    Its own type so a test cannot pass by catching something the resolve path
    raised about itself.
    """


class _ArmsThatCannotRead:
    """Context arms whose every read fails, standing in for unreachable storage.

    Injected at the arms rather than by closing a connection pool because the
    property under test is what the *resolution* reports when its sources cannot
    answer — and every arm failing is what unreachable storage looks like from
    where the assembler sits.
    """

    def for_request(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        async def arm() -> ArmOutcome:
            raise _UnreachableStorage("the store behind this arm is unreachable")

        return dict.fromkeys(BLOCK_NAMES, arm)


def _resolver(pg_container: str) -> tuple[ContextResolver, Any]:
    engine = create_async_engine(_async_url(pg_container))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    receipts = ContextReceiptService(session_factory=factory, clock=SystemClock())
    return ContextResolver(arms=_ArmsThatCannotRead(), receipts=receipts), engine


def test_a_resolution_that_could_not_read_never_calls_itself_complete(
    pg_container: str, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The failure mode the whole quality design exists to prevent.

    An orchestrator that receives context it believes is whole will act on it.
    So when nothing could be read, the envelope must say so in the field a
    caller branches on — not merely return fewer items and let the shortfall
    pass for "there was nothing to find".

    This asserts existing behaviour and changes none of it. A failure here is a
    defect in the resolve path, not an assertion to relax.
    """
    tenant_id, actor_id, _source_id = seat
    resolver, engine = _resolver(pg_container)

    async def run() -> Any:
        try:
            return await resolver.resolve(_ctx(tenant_id, actor_id), query="what changed in payments", moment=_NOW)
        finally:
            await engine.dispose()

    resolved = asyncio.run(run())

    assert resolved.envelope.state != ENVELOPE_COMPLETE
    assert resolved.envelope.quality.reasons, "an unserved resolution must say why"
    assert (
        not resolved.envelope.quality.cacheable
    ), "caching this would outlive the outage and hand the next reader a stale picture with no sign of it"
    for name in BLOCK_NAMES:
        assert resolved.envelope.block(name).state == "failed"
        assert resolved.envelope.block(name).reason, f"{name} failed without saying why"


def test_an_unservable_resolution_still_leaves_a_receipt(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The outage is auditable afterwards, not only observable at the time.

    An operator reconstructing what an agent was working from during an incident
    needs the failed resolutions as much as the successful ones — a receipt that
    only existed on the happy path would go missing exactly when it is wanted.
    """
    tenant_id, actor_id, _source_id = seat
    resolver, engine = _resolver(pg_container)

    async def run() -> Any:
        try:
            return await resolver.resolve(_ctx(tenant_id, actor_id), query="during the incident", moment=_NOW)
        finally:
            await engine.dispose()

    resolved = asyncio.run(run())

    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, cacheable FROM context_receipts WHERE receipt_id = :r"),
            {"r": resolved.receipt_id},
        ).one()

    assert row.state == resolved.envelope.state, "the stored receipt records the state the caller was given"
    assert row.cacheable is False


def test_a_resolution_whose_evidence_cannot_be_written_fails_instead_of_answering(
    seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """The strongest form of loudness, and the one worth the availability cost.

    An answer nobody can later show they were given is indistinguishable from an
    audited one at the moment somebody needs the audit. So if the receipt cannot
    be written the resolution fails outright, rather than returning an envelope
    that looks recorded and is not.

    Proved by pointing the receipt writer at a port nothing is listening on,
    which is what a database outage looks like from here.

    The contrast with the two tests above is the whole assertion. Identical
    unreadable arms: with the receipt store up, `resolve` *returns* an envelope
    that declares itself unservable; with it down, `resolve` does not return at
    all. The difference is what "the receipt write is not best-effort" means in
    practice.
    """
    tenant_id, actor_id, _source_id = seat
    dead = create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none")
    factory = async_sessionmaker(dead, expire_on_commit=False)
    resolver = ContextResolver(
        arms=_ArmsThatCannotRead(),
        receipts=ContextReceiptService(session_factory=factory, clock=SystemClock()),
    )

    async def run() -> Any:
        try:
            return await resolver.resolve(_ctx(tenant_id, actor_id), query="anything", moment=_NOW)
        finally:
            await dead.dispose()

    # Pinned to the store's own failure rather than to `Exception`: a resolution
    # that raised something of its own devising would satisfy a bare `raises`
    # while meaning something entirely different happened.
    with pytest.raises(OSError) as raised:
        asyncio.run(run())

    assert isinstance(
        raised.value, ConnectionRefusedError
    ), f"the failure must be the unreachable store, not one the resolver invented: {raised.value!r}"


# --- the runbook's own queries -------------------------------------------------


_RUNBOOK = Path(__file__).parents[2] / "docs" / "06-operations" / "07-lifecycle-pilot-runbook.md"


def _runbook_queries() -> list[str]:
    """Every SQL block the pilot runbook tells an operator to run.

    Extracted from the document rather than copied into this file. A copy would
    drift from the thing operators actually paste, and the copy is the one that
    would stay green.
    """
    blocks: list[str] = []
    collecting = False
    current: list[str] = []
    for line in _RUNBOOK.read_text(encoding="utf-8").splitlines():
        if line.strip() == "```sql":
            collecting, current = True, []
            continue
        if collecting and line.strip() == "```":
            blocks.append("\n".join(current))
            collecting = False
            continue
        if collecting:
            current.append(line)
    return blocks


#: What identifies the unjoined-outcome diagnostic among the runbook's blocks.
#: Matched on the two halves of the join it exists to test -- the outcome-side
#: subject type and the receipt-side exclusion -- rather than on prose, so a
#: reworded comment does not change what the pin above measures.
_UNJOINED_MARKERS = ("'external_signal'", "NOT EXISTS", "'context_item'")


def _unjoined_outcome_queries(queries: list[str]) -> list[str]:
    return [query for query in queries if all(marker in query for marker in _UNJOINED_MARKERS)]


def test_every_query_the_runbook_gives_an_operator_actually_runs(
    pg_container: str, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """A runbook query that does not execute is worse than no runbook.

    It fails at the moment somebody is already having a bad day, and it fails in
    a way that looks like the database is wrong rather than the document. Running
    them here also binds the document to the schema: a column renamed underneath
    breaks this test instead of an operator mid-incident.

    Executed rather than parsed, because a query can be valid SQL and still name
    a column that does not exist.
    """
    tenant_id, _actor_id, _source_id = seat
    queries = _runbook_queries()

    # The count is pinned so a diagnostic cannot be added or dropped without a
    # deliberate edit here. A bare number would go green on any increment,
    # including one that removed a query and added an unrelated one, so the
    # unjoined-outcome diagnostic is identified by shape and subtracted: the
    # remainder is what must equal the three older diagnostics. Delete that
    # query from the runbook and this fails on the remainder, not on a stale
    # total that nothing notices.
    unjoined = _unjoined_outcome_queries(queries)
    assert len(unjoined) == 1, (
        "exactly one runbook query must be the unjoined-outcome diagnostic, " f"found {len(unjoined)}"
    )
    assert len(queries) - len(unjoined) == 3, (
        f"expected the runbook's three older diagnostics beside the unjoined-outcome "
        f"query, found {len(queries) - len(unjoined)}"
    )
    assert len(queries) == 4, f"expected the runbook's four diagnostics, found {len(queries)}"

    params = {
        "tenant": tenant_id,
        "incident_start": _CONCLUDED_AT,
        "incident_end": _NOW,
    }
    engine = create_engine(_sync_url(pg_container))
    try:
        with engine.connect() as conn:
            for query in queries:
                bound = {name: value for name, value in params.items() if f":{name}" in query}
                conn.execute(text(query), bound).all()
    finally:
        engine.dispose()


# --- outcomes that bound and never joined --------------------------------------


def _seed_bound_outcome(
    sync_engine: Engine,
    *,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    external_id: str,
    bound_at: datetime.datetime,
    receipt_count: int = 0,
) -> uuid.UUID:
    """One stored outcome bound to work cited by *receipt_count* receipts.

    Rows are written directly. Driving the submission path would prove the
    adapter, which has its own suite; what is under test here is whether the
    read distinguishes a reference nothing cites from one something does, and
    that distinction is made of exactly these bindings.
    """
    signal_id, reference_id = uuid.uuid4(), uuid.uuid4()
    reference = normalize_reference(
        {
            "source_system": "github-actions",
            "source_namespace": "acme/platform",
            "kind": "deployment",
            "external_id": external_id,
            "classification": "internal",
            "external_authority": "acme/platform",
        }
    )
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_signals"
                " (signal_id, tenant_id, source_system, producer_id, producer_type,"
                "  source_event_id, idempotency_key, content_digest, authority,"
                "  classification, ingested_at, schema_version, payload)"
                " VALUES (:s, :t, 'github-actions', 'ci', 'external', :eid, :key, :digest,"
                "  :authority, 'internal', :now, 'v1', '{}'::jsonb)"
            ),
            {
                "s": signal_id,
                "t": tenant_id,
                "eid": f"evt-{signal_id.hex[:8]}",
                "key": f"key-{signal_id.hex[:8]}",
                "digest": f"digest-{signal_id.hex[:8]}",
                "authority": AUTHORITY_OBSERVER_EXTRACTION,
                "now": bound_at,
            },
        )
        conn.execute(
            text(
                "INSERT INTO context_external_references"
                " (reference_id, tenant_id, source_system, source_namespace, kind, external_id,"
                "  classification, external_authority, collision_key, created_at)"
                " VALUES (:ref, :t, :sys, :ns, :kind, :eid, :cls, :auth, :key, :now)"
                " ON CONFLICT (tenant_id, collision_key) DO NOTHING"
            ),
            {
                "ref": reference_id,
                "t": tenant_id,
                "sys": reference.source_system,
                "ns": reference.source_namespace,
                "kind": reference.kind,
                "eid": reference.external_id,
                "cls": reference.classification,
                "auth": reference.external_authority,
                "key": reference.collision_key(),
                "now": bound_at,
            },
        )
        stored = conn.execute(
            text(
                "SELECT reference_id FROM context_external_references" " WHERE tenant_id = :t AND collision_key = :key"
            ),
            {"t": tenant_id, "key": reference.collision_key()},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO context_reference_bindings"
                " (binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at)"
                " VALUES (:b, :t, :ref, 'external_signal', :subject, :now)"
            ),
            {"b": uuid.uuid4(), "t": tenant_id, "ref": stored, "subject": signal_id, "now": bound_at},
        )
        for _ in range(receipt_count):
            receipt_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO context_receipts"
                    " (receipt_id, tenant_id, state, cacheable, resolved_at, requested_by)"
                    " VALUES (:r, :t, 'complete', TRUE, :now, :actor)"
                ),
                {"r": receipt_id, "t": tenant_id, "now": bound_at, "actor": str(source_id)},
            )
            conn.execute(
                text(
                    "INSERT INTO context_reference_bindings"
                    " (binding_id, tenant_id, reference_id, subject_type, subject_id, bound_at)"
                    " VALUES (:b, :t, :ref, 'context_item', :subject, :now)"
                ),
                {"b": uuid.uuid4(), "t": tenant_id, "ref": stored, "subject": receipt_id, "now": bound_at},
            )
    return signal_id


def _read_unjoined(
    pg_container: str,
    *,
    tenant_id: uuid.UUID,
    older_than: datetime.timedelta,
    bound: int = 50,
) -> UnjoinedOutcomePage:
    async def run() -> UnjoinedOutcomePage:
        engine = create_async_engine(_async_url(pg_container))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = UnjoinedOutcomeReadService(factory)
            ctx = TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["operator"])
            return await service.unjoined(ctx, older_than=older_than, now=_NOW, bound=bound)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_an_outcome_bound_past_the_age_with_no_receipt_is_reported(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The case the read exists for: a right kind, a wrong id, nothing joined."""
    tenant_id, _actor_id, source_id = seat
    signal_id = _seed_bound_outcome(
        sync_engine,
        tenant_id=tenant_id,
        source_id=source_id,
        external_id="deploy-4711-typo",
        bound_at=_NOW - datetime.timedelta(days=2),
    )

    page = _read_unjoined(pg_container, tenant_id=tenant_id, older_than=datetime.timedelta(hours=6))

    assert [item.signal_id for item in page.items] == [signal_id]
    assert page.items[0].external_id == "deploy-4711-typo"
    assert page.items[0].kind == "deployment"


def test_an_outcome_inside_the_age_window_is_not_reported(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """A receipt still in flight is not a defect, so a young binding is silent.

    Reporting it would train an operator to ignore the diagnostic, which costs
    more than the query is worth.
    """
    tenant_id, _actor_id, source_id = seat
    _seed_bound_outcome(
        sync_engine,
        tenant_id=tenant_id,
        source_id=source_id,
        external_id="deploy-just-now",
        bound_at=_NOW - datetime.timedelta(minutes=5),
    )

    page = _read_unjoined(pg_container, tenant_id=tenant_id, older_than=datetime.timedelta(hours=6))

    assert page.items == ()
    assert page.truncated is False


def test_a_joined_outcome_is_never_reported_however_old(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """Age filters the unjoined set; it never promotes a joined outcome into it."""
    tenant_id, _actor_id, source_id = seat
    _seed_bound_outcome(
        sync_engine,
        tenant_id=tenant_id,
        source_id=source_id,
        external_id="deploy-ancient-but-fine",
        bound_at=_NOW - datetime.timedelta(days=400),
        receipt_count=1,
    )

    page = _read_unjoined(pg_container, tenant_id=tenant_id, older_than=datetime.timedelta(hours=6))

    assert page.items == ()


def test_a_reference_cited_by_two_receipts_is_still_excluded_once(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The case a `LEFT JOIN ... IS NULL` would get wrong.

    Two receipts citing one reference multiply the outcome's row. Under a
    left-join-and-null-test the duplicates survive as non-null rows that a
    careless `IS NULL` filter drops, but any shape that tests the *joined* side
    per row rather than per outcome can readmit one. `NOT EXISTS` cannot: the
    outcome is excluded once, whatever cites it.
    """
    tenant_id, _actor_id, source_id = seat
    _seed_bound_outcome(
        sync_engine,
        tenant_id=tenant_id,
        source_id=source_id,
        external_id="deploy-cited-twice",
        bound_at=_NOW - datetime.timedelta(days=3),
        receipt_count=2,
    )

    page = _read_unjoined(pg_container, tenant_id=tenant_id, older_than=datetime.timedelta(hours=6))

    assert page.items == ()


def test_another_tenants_unjoined_outcome_is_not_reported(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The read is per tenant, and a diagnostic is not an exemption from that."""
    tenant_id, _actor_id, _source_id = seat
    other_tenant, _other_actor, other_source = _register_seat(sync_engine, ceiling=1000)
    _seed_bound_outcome(
        sync_engine,
        tenant_id=other_tenant,
        source_id=other_source,
        external_id="deploy-someone-elses",
        bound_at=_NOW - datetime.timedelta(days=2),
    )

    page = _read_unjoined(pg_container, tenant_id=tenant_id, older_than=datetime.timedelta(hours=6))

    assert page.items == ()


def test_an_over_full_page_is_trimmed_and_flagged(
    pg_container: str, sync_engine: Engine, seat: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """The bound holds against real rows, not only against a faked limit."""
    tenant_id, _actor_id, source_id = seat
    for index in range(3):
        _seed_bound_outcome(
            sync_engine,
            tenant_id=tenant_id,
            source_id=source_id,
            external_id=f"deploy-stuck-{index}",
            bound_at=_NOW - datetime.timedelta(days=index + 2),
        )

    page = _read_unjoined(pg_container, tenant_id=tenant_id, older_than=datetime.timedelta(hours=6), bound=2)

    assert len(page.items) == 2
    assert page.truncated is True
    # Oldest binding first, so the pages an operator reads are stable.
    assert [item.external_id for item in page.items] == ["deploy-stuck-2", "deploy-stuck-1"]
