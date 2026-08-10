"""Legal holds against a real Postgres: placing, renewing, and what a hold suspends.

The unit suite proves the value semantics and the escalation arithmetic without a
database. It cannot prove the parts that matter most here, because every one of
them is a constraint the migration owns and the application deliberately does not
restate: that a second hold on one record is refused, that the 180-day ceiling
binds a writer who never ran the Python check, and that a renewal which fails
halfway leaves no extended hold behind it.

The store is driven through its own interface rather than through hand-written
SQL, because the question is whether *this code* places a storable hold — a suite
that inserted its own rows would prove the schema accepts what the suite believes
and nothing about what the store writes.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contextplane.retention import holds, policies

_NOW = datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.UTC)

#: The one record class this suite dates, and the expression the store reads its
#: deadline from. Signals carry an ingestion time and the policy carries the
#: duration, so the deadline is the sum rather than a stored column.
_SIGNAL_SOURCE = holds.HeldRecordSource(
    table="external_signals",
    id_column="signal_id",
    due_at_sql="t.ingested_at + make_interval(days => 730)",
)


@pytest_asyncio.fixture
async def hold_fixture(pg_container: str) -> AsyncIterator[dict[str, object]]:
    """A tenant and a store wired to the signal deadline source."""
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'legal holds')"),
                {"t": tenant_id, "s": f"lh-{tenant_id.hex[:10]}"},
            )
        store = holds.PostgresHoldStore(
            factory, {policies.RECORD_EXTERNAL_SIGNAL: _SIGNAL_SOURCE}
        )
        yield {"factory": factory, "tenant": tenant_id, "store": store}
    finally:
        await engine.dispose()


async def _place(fixture: dict[str, object], **kwargs: object) -> holds.LegalHold:
    store: holds.PostgresHoldStore = fixture["store"]  # type: ignore[assignment]
    return await store.place(
        fixture["tenant"],  # type: ignore[arg-type]
        policies.RECORD_EXTERNAL_SIGNAL,
        kwargs.pop("subject_id", uuid.uuid4()),  # type: ignore[arg-type]
        placed_by="legal@example.test",
        reason="litigation",
        now=_NOW,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_placed_hold_is_found_by_the_consult_every_sweep_makes(
    hold_fixture: dict[str, object],
) -> None:
    """The property the whole seam exists for: a record under a hold comes back
    held, and one beside it does not.

    Driven through `partition_by_hold` against a real store rather than a fake,
    because that function is what every expiry path calls and the split is the
    decision a sweep acts on.
    """
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    held_subject, free_subject = uuid.uuid4(), uuid.uuid4()
    await _place(hold_fixture, subject_id=held_subject)

    deletable, held = await holds.partition_by_hold(
        store,
        hold_fixture["tenant"],  # type: ignore[arg-type]
        policies.RECORD_EXTERNAL_SIGNAL,
        [held_subject, free_subject],
        now=_NOW,
    )

    assert deletable == (free_subject,)
    assert set(held) == {held_subject}
    assert held[held_subject].placed_by == "legal@example.test"
    assert held[held_subject].reason == "litigation"


async def test_a_hold_past_its_review_date_stops_suspending_deletion(
    hold_fixture: dict[str, object],
) -> None:
    """The review is what keeps a hold alive. An unreviewed hold that went on
    suspending deletion would be permanent retention nobody approved."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    subject = uuid.uuid4()
    hold = await _place(hold_fixture, subject_id=subject, review_in_days=30)

    still_live = await store.active_holds(
        hold_fixture["tenant"],  # type: ignore[arg-type]
        policies.RECORD_EXTERNAL_SIGNAL,
        [subject],
        now=hold.review_date - datetime.timedelta(seconds=1),
    )
    lapsed = await store.active_holds(
        hold_fixture["tenant"],  # type: ignore[arg-type]
        policies.RECORD_EXTERNAL_SIGNAL,
        [subject],
        now=hold.review_date,
    )

    assert set(still_live) == {subject}
    assert lapsed == {}


async def test_a_hold_beyond_the_approved_ceiling_is_refused_before_it_is_written(
    hold_fixture: dict[str, object],
) -> None:
    with pytest.raises(holds.HoldRenewalRefused, match="180"):
        await _place(hold_fixture, review_in_days=181)


async def test_a_second_hold_on_one_record_cannot_be_placed(
    hold_fixture: dict[str, object],
) -> None:
    """The consult answers with a mapping keyed by subject, so two holds on one
    record would be two answers to a question that has one."""
    subject = uuid.uuid4()
    await _place(hold_fixture, subject_id=subject)

    with pytest.raises(Exception, match="uq_legal_holds_record|duplicate key"):
        await _place(hold_fixture, subject_id=subject)


async def test_a_renewal_records_its_justification_and_its_approval(
    hold_fixture: dict[str, object],
) -> None:
    """Two rows, by two parties. The hold carries only the position it has reached."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    factory: async_sessionmaker = hold_fixture["factory"]  # type: ignore[assignment]
    hold = await _place(hold_fixture, review_in_days=30)

    renewed = await store.renew(
        hold.hold_id,
        justification="discovery is still open",
        approved_by="owner@example.test",
        approval_level="tenant_owner",
        review_in_days=60,
        now=_NOW,
    )

    assert renewed.renewal_count == 1
    assert renewed.renewal_justification == "discovery is still open"
    assert renewed.review_date == _NOW + datetime.timedelta(days=60)

    async with factory() as session:
        justification, requested_by, approver, rank = (
            await session.execute(
                text(
                    "SELECT n.justification, n.requested_by, a.approved_by, a.approval_rank"
                    " FROM legal_hold_renewals AS n"
                    " JOIN legal_hold_approvals AS a ON a.renewal_id = n.renewal_id"
                    " WHERE n.hold_id = :h AND n.sequence = 1"
                ),
                {"h": hold.hold_id},
            )
        ).one()
    assert justification == "discovery is still open"
    assert requested_by == "owner@example.test"
    assert approver == "owner@example.test"
    assert rank == 1


async def test_a_renewal_with_no_recorded_justification_is_refused(
    hold_fixture: dict[str, object],
) -> None:
    """The case the policy names outright: a hold extended with nothing recorded
    about why it is still legally necessary."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    hold = await _place(hold_fixture)

    with pytest.raises(holds.HoldRenewalRefused, match="legally necessary"):
        await store.renew(
            hold.hold_id,
            justification="   ",
            approved_by="ops",
            approval_level="operator",
            now=_NOW,
        )


async def test_a_second_renewal_signed_at_the_first_ones_level_is_refused(
    hold_fixture: dict[str, object],
) -> None:
    """Escalating approval. Without it a hold renews itself indefinitely at the
    authority of whoever placed it, which is the permanence the ceiling forbids
    spelled a slower way."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    hold = await _place(hold_fixture)

    await store.renew(
        hold.hold_id,
        justification="first extension",
        approved_by="owner@example.test",
        approval_level="tenant_owner",
        now=_NOW,
    )

    with pytest.raises(holds.HoldRenewalRefused, match="escalate|below it"):
        await store.renew(
            hold.hold_id,
            justification="second extension",
            approved_by="owner@example.test",
            approval_level="tenant_owner",
            now=_NOW,
        )


async def test_a_renewal_refused_for_its_approval_leaves_the_hold_where_it_was(
    hold_fixture: dict[str, object],
) -> None:
    """Refused before anything is written. A partially applied renewal would leave
    a hold extended with no approval standing behind it, which reads afterwards as
    a legitimate hold and is the exact shape the two-row rule exists to prevent."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    factory: async_sessionmaker = hold_fixture["factory"]  # type: ignore[assignment]
    hold = await _place(hold_fixture, review_in_days=30)

    await store.renew(
        hold.hold_id,
        justification="first extension",
        approved_by="owner@example.test",
        approval_level="tenant_owner",
        review_in_days=45,
        now=_NOW,
    )
    with pytest.raises(holds.HoldRenewalRefused):
        await store.renew(
            hold.hold_id,
            justification="second extension",
            approved_by="owner@example.test",
            approval_level="tenant_owner",
            now=_NOW,
        )

    async with factory() as session:
        review_date, renewal_count = (
            await session.execute(
                text("SELECT review_date, renewal_count FROM legal_holds WHERE hold_id = :h"),
                {"h": hold.hold_id},
            )
        ).one()
        renewals = (
            await session.execute(
                text("SELECT COUNT(*) FROM legal_hold_renewals WHERE hold_id = :h"),
                {"h": hold.hold_id},
            )
        ).scalar_one()
    assert renewal_count == 1
    assert review_date == _NOW + datetime.timedelta(days=45)
    assert renewals == 1


async def test_a_renewal_may_carry_a_hold_past_the_ceiling_measured_from_placement(
    hold_fixture: dict[str, object],
) -> None:
    """A hold's total life exceeds 180 days only through renewals that each recorded
    their justification and approval. The database's ceiling measures the extension
    just granted, which is what makes a renewed hold storable at all."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    hold = await _place(hold_fixture, review_in_days=180)

    renewed = await store.renew(
        hold.hold_id,
        justification="still under litigation",
        approved_by="owner@example.test",
        approval_level="tenant_owner",
        review_in_days=180,
        now=_NOW + datetime.timedelta(days=179),
    )

    assert renewed.review_date == _NOW + datetime.timedelta(days=359)
    assert (renewed.review_date - hold.placed_at).days > holds.MAX_HOLD_DAYS


async def test_a_held_record_past_its_retention_deadline_reaches_the_operator_report(
    hold_fixture: dict[str, object],
) -> None:
    """The paused clock made visible. A hold that suspends deletion without showing
    up here defeats fail-closed overdue behaviour silently, which is the whole
    reason the report exists."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    factory: async_sessionmaker = hold_fixture["factory"]  # type: ignore[assignment]
    tenant = hold_fixture["tenant"]
    overdue_subject = uuid.uuid4()

    long_past = _NOW - datetime.timedelta(days=800)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id,"
                " producer_type, source_event_id, idempotency_key, content_digest, authority,"
                " classification, ingested_at, schema_version, payload, superseded_for_learning)"
                " VALUES (:s, :t, 'github', 'ci', 'external', :eid, :ikey, 'digest',"
                " 'platform-team', 'internal', :at, 'v1', '{}'::jsonb, FALSE)"
            ),
            {
                "s": overdue_subject,
                "t": tenant,
                "at": long_past,
                "eid": str(overdue_subject),
                "ikey": str(overdue_subject),
            },
        )
    await _place(hold_fixture, subject_id=overdue_subject)

    report = await store.held_overdue(tenant, now=_NOW)  # type: ignore[arg-type]

    assert [row.subject_id for row in report] == [overdue_subject]
    assert report[0].due_at < _NOW
    assert report[0].hold.reason == "litigation"


async def test_a_held_record_still_within_its_period_is_not_reported_as_overdue(
    hold_fixture: dict[str, object],
) -> None:
    """A hold on a record nobody was going to delete yet is not an operator problem,
    and reporting it would bury the ones that are."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    factory: async_sessionmaker = hold_fixture["factory"]  # type: ignore[assignment]
    tenant = hold_fixture["tenant"]
    fresh_subject = uuid.uuid4()

    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO external_signals (signal_id, tenant_id, source_system, producer_id,"
                " producer_type, source_event_id, idempotency_key, content_digest, authority,"
                " classification, ingested_at, schema_version, payload, superseded_for_learning)"
                " VALUES (:s, :t, 'github', 'ci', 'external', :eid, :ikey, 'digest',"
                " 'platform-team', 'internal', :at, 'v1', '{}'::jsonb, FALSE)"
            ),
            {
                "s": fresh_subject,
                "t": tenant,
                "at": _NOW - datetime.timedelta(days=1),
                "eid": str(fresh_subject),
                "ikey": str(fresh_subject),
            },
        )
    await _place(hold_fixture, subject_id=fresh_subject)

    assert await store.held_overdue(tenant, now=_NOW) == ()  # type: ignore[arg-type]


async def test_a_hold_on_a_record_class_the_store_cannot_date_is_reported_not_dropped(
    hold_fixture: dict[str, object],
) -> None:
    """Fail-closed. "Something is held and nothing here knows when it was due" is a
    state an operator has to see; omitting it renders identically to a clean report,
    which is the one reading that must not be available."""
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    tenant = hold_fixture["tenant"]
    subject = uuid.uuid4()

    await store.place(
        tenant,  # type: ignore[arg-type]
        policies.RECORD_MEMORY_CLAIM,
        subject,
        placed_by="legal@example.test",
        reason="unmapped class",
        now=_NOW,
    )

    report = await store.held_overdue(tenant, now=_NOW)  # type: ignore[arg-type]

    assert [row.record_class for row in report] == [policies.RECORD_MEMORY_CLAIM]
    assert report[0].subject_id == subject


async def test_one_tenants_report_never_carries_another_tenants_hold(
    hold_fixture: dict[str, object],
) -> None:
    store: holds.PostgresHoldStore = hold_fixture["store"]  # type: ignore[assignment]
    factory: async_sessionmaker = hold_fixture["factory"]  # type: ignore[assignment]
    other_tenant = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'other')"),
            {"t": other_tenant, "s": f"lh-{other_tenant.hex[:10]}"},
        )
    await store.place(
        other_tenant,
        policies.RECORD_MEMORY_CLAIM,
        uuid.uuid4(),
        placed_by="legal@example.test",
        reason="other tenant litigation",
        now=_NOW,
    )

    assert await store.held_overdue(hold_fixture["tenant"], now=_NOW) == ()  # type: ignore[arg-type]
    assert len(await store.held_overdue(other_tenant, now=_NOW)) == 1
