"""Finding receipts by the work they describe, and not finding other tenants'.

Nobody starts from a receipt id. They start from a commit, a pull request, a
build, and ask what context an agent had when it touched that. These tests drive
that path against a real Postgres.

The tenant predicate is the thread through all of it. Every read here joins
through `context_external_references`, which carries the tenant, so a foreign
row contributes nothing rather than being filtered afterwards. That distinction
is testable and is tested: a count taken before the filter is itself a
disclosure, because "there are four receipts you cannot read" tells somebody
something they were not granted.
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

from contextplane.context.receipts import ContextReceiptService
from contextplane.context.references import SUBJECT_RECEIPT, ReceiptReferenceIndex
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 8, 12, 0, tzinfo=datetime.UTC)

type _Fixture = dict[str, Any]


def _ctx(tenant_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), roles=["producer"])


@pytest_asyncio.fixture
async def wired(pg_container: str) -> AsyncIterator[_Fixture]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
        async with factory() as session, session.begin():
            for tid in (tenant_a, tenant_b):
                await session.execute(
                    text(
                        "INSERT INTO tenants (tenant_id, slug, display_name, created_at, is_active) "
                        "VALUES (:t, :s, :s, :now, TRUE)"
                    ),
                    {"t": tid, "s": f"rr-{tid.hex[:8]}", "now": _NOW},
                )
        yield {
            "factory": factory,
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
            "receipts": ContextReceiptService(session_factory=factory, clock=FakeClock(_NOW)),
            "index": ReceiptReferenceIndex(session_factory=factory),
        }
    finally:
        await engine.dispose()


async def _reference(
    wired: _Fixture,
    *,
    tenant_id: uuid.UUID,
    external_id: str = "abc123",
    kind: str = "commit",
) -> uuid.UUID:
    reference_id = uuid.uuid4()
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_external_references "
                "(reference_id, tenant_id, source_system, source_namespace, kind, external_id, "
                " classification, external_authority, collision_key) "
                "VALUES (:rid, :tid, 'github', 'acme/app', :kind, :eid, 'internal', 'github', :ckey)"
            ),
            {
                "rid": reference_id,
                "tid": tenant_id,
                "kind": kind,
                "eid": external_id,
                # Written by the service from the contract's own digest; these
                # tests seed rows directly, so they supply the same identity.
                "ckey": f"github|acme/app|{kind}|{external_id}",
            },
        )
    return reference_id


async def _receipt(wired: _Fixture, *, tenant_id: uuid.UUID) -> uuid.UUID:
    receipt_id = uuid.uuid4()
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                # `hydration_state` explicitly, because the evidence reads now
                # refuse an unhydrated receipt in the service rather than in the
                # router: an exclusions read that returned nothing for one would
                # say "nothing was withheld" when it means "not recorded yet".
                # These tests are about tenant isolation, so they want a receipt
                # that is servable to its owner.
                "INSERT INTO context_receipts "
                "(receipt_id, tenant_id, intent_id, state, cacheable, hydration_state, "
                " resolved_at, requested_by) "
                "VALUES (:rid, :tid, NULL, 'complete', TRUE, 'complete', :now, 'actor')"
            ),
            {"rid": receipt_id, "tid": tenant_id, "now": _NOW},
        )
    return receipt_id


# --- The path a reader actually takes ------------------------------------------


@pytest.mark.asyncio
async def test_a_receipt_is_findable_by_the_commit_it_cites(wired: _Fixture) -> None:
    """The whole point: a receipt that cannot be found by the work it describes
    is not evidence."""
    tenant = wired["tenant_a"]
    reference_id = await _reference(wired, tenant_id=tenant)
    receipt_id = await _receipt(wired, tenant_id=tenant)
    await wired["index"].bind(_ctx(tenant), receipt_id=receipt_id, reference_ids=[reference_id], bound_at=_NOW)

    found = await wired["index"].receipts_for_reference(
        _ctx(tenant),
        source_system="github",
        source_namespace="acme/app",
        kind="commit",
        external_id="abc123",
    )

    assert [r.receipt_id for r in found] == [receipt_id]


@pytest.mark.asyncio
async def test_the_reverse_direction_answers_what_an_answer_was_about(wired: _Fixture) -> None:
    """Given a receipt, what did it claim to be about. The read an auditor
    makes."""
    tenant = wired["tenant_a"]
    reference_id = await _reference(wired, tenant_id=tenant)
    receipt_id = await _receipt(wired, tenant_id=tenant)
    await wired["index"].bind(_ctx(tenant), receipt_id=receipt_id, reference_ids=[reference_id], bound_at=_NOW)

    refs = await wired["index"].references_for_receipt(_ctx(tenant), receipt_id=receipt_id)

    assert [r.external_id for r in refs] == ["abc123"]


@pytest.mark.asyncio
async def test_an_unbound_reference_finds_nothing(wired: _Fixture) -> None:
    """The negative control. Without it every assertion above could be passing
    because the query returns everything."""
    tenant = wired["tenant_a"]
    await _reference(wired, tenant_id=tenant, external_id="never-bound")

    found = await wired["index"].receipts_for_reference(
        _ctx(tenant),
        source_system="github",
        source_namespace="acme/app",
        kind="commit",
        external_id="never-bound",
    )

    assert found == ()


# --- Authorization --------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_tenants_receipts_are_invisible_to_another(wired: _Fixture) -> None:
    """The predicate is in the SELECT, so a foreign row contributes nothing
    rather than being filtered out after it was already loaded."""
    owner, other = wired["tenant_a"], wired["tenant_b"]
    reference_id = await _reference(wired, tenant_id=owner)
    receipt_id = await _receipt(wired, tenant_id=owner)
    await wired["index"].bind(_ctx(owner), receipt_id=receipt_id, reference_ids=[reference_id], bound_at=_NOW)

    found = await wired["index"].receipts_for_reference(
        _ctx(other),
        source_system="github",
        source_namespace="acme/app",
        kind="commit",
        external_id="abc123",
    )

    assert found == ()


@pytest.mark.asyncio
async def test_a_foreign_receipt_cannot_be_read_by_id(wired: _Fixture) -> None:
    """Guessing a receipt id must not be enough. The tenant is in the SELECT."""
    owner, other = wired["tenant_a"], wired["tenant_b"]
    receipt_id = await _receipt(wired, tenant_id=owner)

    assert await wired["receipts"].get(_ctx(owner), receipt_id=receipt_id) is not None
    assert await wired["receipts"].get(_ctx(other), receipt_id=receipt_id) is None


@pytest.mark.asyncio
async def test_a_foreign_receipts_exclusions_are_not_readable(wired: _Fixture) -> None:
    """The exclusions table carries no tenant of its own, so reading it by
    receipt id alone would hand another tenant's withholding to anyone who
    guessed an id. It joins back through the receipt for exactly that reason."""
    owner, other = wired["tenant_a"], wired["tenant_b"]
    receipt_id = await _receipt(wired, tenant_id=owner)
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO context_receipt_exclusions (receipt_id, block, item_key, reason) "
                "VALUES (:r, 'workspace', 'task-9', 'no active grant')"
            ),
            {"r": receipt_id},
        )

    assert len(await wired["receipts"].exclusions_for(_ctx(owner), receipt_id=receipt_id)) == 1
    assert await wired["receipts"].exclusions_for(_ctx(other), receipt_id=receipt_id) == ()


# --- Binding ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_binding_the_same_reference_twice_does_not_duplicate_it(wired: _Fixture) -> None:
    """Re-recording a resolution must not make its citation list grow."""
    tenant = wired["tenant_a"]
    reference_id = await _reference(wired, tenant_id=tenant)
    receipt_id = await _receipt(wired, tenant_id=tenant)

    first = await wired["index"].bind(_ctx(tenant), receipt_id=receipt_id, reference_ids=[reference_id], bound_at=_NOW)
    second = await wired["index"].bind(_ctx(tenant), receipt_id=receipt_id, reference_ids=[reference_id], bound_at=_NOW)

    assert (first, second) == (1, 0)
    refs = await wired["index"].references_for_receipt(_ctx(tenant), receipt_id=receipt_id)
    assert len(refs) == 1


@pytest.mark.asyncio
async def test_a_receipt_may_cite_several_pieces_of_work(wired: _Fixture) -> None:
    tenant = wired["tenant_a"]
    commit = await _reference(wired, tenant_id=tenant, external_id="c1", kind="commit")
    build = await _reference(wired, tenant_id=tenant, external_id="b1", kind="build")
    receipt_id = await _receipt(wired, tenant_id=tenant)

    added = await wired["index"].bind(_ctx(tenant), receipt_id=receipt_id, reference_ids=[commit, build], bound_at=_NOW)

    assert added == 2
    refs = await wired["index"].references_for_receipt(_ctx(tenant), receipt_id=receipt_id)
    assert {r.kind for r in refs} == {"commit", "build"}


@pytest.mark.asyncio
async def test_several_receipts_may_cite_one_piece_of_work_newest_first(wired: _Fixture) -> None:
    """The read an operator makes when a commit keeps going wrong: what did
    successive resolutions know about it."""
    tenant = wired["tenant_a"]
    reference_id = await _reference(wired, tenant_id=tenant)
    older = await _receipt(wired, tenant_id=tenant)
    newer = await _receipt(wired, tenant_id=tenant)
    async with wired["factory"]() as session, session.begin():
        await session.execute(
            text("UPDATE context_receipts SET resolved_at = :t WHERE receipt_id = :r"),
            {"t": _NOW - datetime.timedelta(hours=1), "r": older},
        )
    for receipt_id in (older, newer):
        await wired["index"].bind(_ctx(tenant), receipt_id=receipt_id, reference_ids=[reference_id], bound_at=_NOW)

    found = await wired["index"].receipts_for_reference(
        _ctx(tenant),
        source_system="github",
        source_namespace="acme/app",
        kind="commit",
        external_id="abc123",
    )

    assert [r.receipt_id for r in found] == [newer, older]


@pytest.mark.asyncio
async def test_bindings_are_recorded_under_the_receipt_subject_type(wired: _Fixture) -> None:
    """Receipts and checkpoints share the junction, so the subject type is what
    keeps one query from returning the other's rows."""
    tenant = wired["tenant_a"]
    reference_id = await _reference(wired, tenant_id=tenant)
    receipt_id = await _receipt(wired, tenant_id=tenant)
    await wired["index"].bind(_ctx(tenant), receipt_id=receipt_id, reference_ids=[reference_id], bound_at=_NOW)

    async with wired["factory"]() as session:
        stored = (
            await session.execute(
                text("SELECT subject_type FROM context_reference_bindings WHERE subject_id = :s"),
                {"s": receipt_id},
            )
        ).scalar_one()

    assert stored == SUBJECT_RECEIPT
