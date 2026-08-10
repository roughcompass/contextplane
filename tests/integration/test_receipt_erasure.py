"""Erasing somebody who left receipts behind, against the real tables.

Two things can only be proven here. The first is the block this work exists to
resolve: `context_feedback` holds foreign keys into `context_receipts` and
`context_receipt_items` with **no cascade**, deliberately, so deleting a receipt
somebody gave feedback on is refused by the database. A unit test with a faked
session cannot refuse anything, so the fix — minimize rather than delete — is
only demonstrably a fix against Postgres.

The second is that a receipt is findable at all. The registration the writer now
makes is what connects a receipt to the records it quoted; if its links are wrong,
every erasure of those records leaves the receipt naming what the person read and
nothing reports a gap.

So these tests plant real receipts, attach real feedback, run the real
minimization, and read the rows back — including the ones that must still be there.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from contextplane.context import derivative_handlers as handlers
from contextplane.context.references import SUBJECT_RECEIPT
from contextplane.retention import derivatives, policies, tombstones
from contextplane.types import TenantContext
from tests.helpers.clock import FakeClock

_NOW = datetime.datetime(2026, 8, 10, 12, 0, tzinfo=datetime.UTC)
_KEY_ID = "test-key"
_KEY_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"


def _salts() -> tombstones.KeyedTenantSalt:
    return tombstones.KeyedTenantSalt({_KEY_ID: bytes.fromhex(_KEY_HEX)}, active_key_id=_KEY_ID)


async def _plant_receipt(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    item_keys: tuple[str, ...] = ("catalog:svc-checkout", "workspace:notes"),
) -> uuid.UUID:
    """One receipt with items, an exclusion and a reference binding.

    The exclusion and the binding are here because a minimization treats the three
    parts differently — the binding row goes, the exclusion row stays and only its key
    is replaced, the item rows likewise — and a fixture that planted only items could
    not tell any of that apart.
    """
    receipt_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO context_receipts (receipt_id, tenant_id, state, cacheable, resolved_at, requested_by) "
            "VALUES (:r, :t, 'complete', FALSE, :now, :actor)"
        ),
        {"r": receipt_id, "t": tenant_id, "now": _NOW, "actor": str(actor_id)},
    )
    for index, item_key in enumerate(item_keys):
        await session.execute(
            text(
                "INSERT INTO context_receipt_items (item_row_id, receipt_id, receipt_item_id, block, source, "
                "                                   item_key) "
                "VALUES (:row, :r, :rid, 'canonical', 'catalog', :key)"
            ),
            {
                "row": uuid.uuid4(),
                "r": receipt_id,
                "rid": f"{receipt_id.hex[:8]}-{index}",
                "key": item_key,
            },
        )
    await session.execute(
        text(
            "INSERT INTO context_receipt_exclusions (exclusion_id, receipt_id, block, item_key, reason) "
            "VALUES (:e, :r, 'canonical', 'catalog:withheld', 'below the trust floor')"
        ),
        {"e": uuid.uuid4(), "r": receipt_id},
    )
    return receipt_id


async def _plant_binding(session: AsyncSession, *, tenant_id: uuid.UUID, receipt_id: uuid.UUID) -> uuid.UUID:
    """One shared reference, cited by this receipt."""
    reference_id = uuid.uuid4()
    external_id = f"pr-{reference_id.hex[:8]}"
    await session.execute(
        text(
            "INSERT INTO context_external_references (reference_id, tenant_id, source_system, source_namespace, "
            "                                        kind, external_id, classification, external_authority, "
            "                                        collision_key, created_at) "
            "VALUES (:ref, :t, 'github', 'roughcompass', 'pull_request', :ext, 'internal', "
            "        'observer_extraction', :collision, :now)"
        ),
        {"ref": reference_id, "t": tenant_id, "ext": external_id, "collision": f"github:{external_id}", "now": _NOW},
    )
    await session.execute(
        text(
            "INSERT INTO context_reference_bindings (binding_id, tenant_id, reference_id, subject_type, subject_id, "
            "                                        bound_at) "
            "VALUES (:b, :t, :ref, :st, :s, :now)"
        ),
        {
            "b": uuid.uuid4(),
            "t": tenant_id,
            "ref": reference_id,
            "st": SUBJECT_RECEIPT,
            "s": receipt_id,
            "now": _NOW,
        },
    )
    return reference_id


async def _plant_feedback(session: AsyncSession, *, tenant_id: uuid.UUID, receipt_id: uuid.UUID) -> uuid.UUID:
    """A report citing the receipt, which is what makes the receipt undeletable.

    `receipt_level` rather than a diagnostic observation: the schema's discriminant
    check requires a feedback row that names a receipt to be one of the two kinds
    that are *about* a receipt, and a diagnostic observation is required to name
    none. So the only feedback that can hold the foreign key is feedback of this
    shape, which makes it the only shape that can create the block.
    """
    feedback_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO context_feedback (feedback_id, tenant_id, kind, receipt_id, rating, learning_eligible, "
            "                              note, reporter_id, reporter_type, idempotency_key, content_digest, "
            "                              created_at) "
            "VALUES (:f, :t, 'receipt_level', :r, 'irrelevant', FALSE, 'thin answer', "
            "        :rid, 'human', :idem, :dig, :now)"
        ),
        {
            "f": feedback_id,
            "t": tenant_id,
            "r": receipt_id,
            "rid": str(uuid.uuid4()),
            "idem": f"fb-{feedback_id.hex[:12]}",
            "dig": f"sha256:{feedback_id.hex}",
            "now": _NOW,
        },
    )
    return feedback_id


@pytest_asyncio.fixture
async def receipts_world(pg_container: str) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(pg_container, connect_args={"prepared_statement_cache_size": 0})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, actor_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO tenants (tenant_id, slug, display_name) VALUES (:t, :s, 'receipt erasure')"),
                {"t": tenant_id, "s": f"rce-{tenant_id.hex[:10]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                    "VALUES (:a, :t, 'actor', :sub, :now)"
                ),
                {"a": actor_id, "t": tenant_id, "sub": f"sub-{actor_id.hex[:10]}", "now": _NOW},
            )
        yield {
            "factory": factory,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "ctx": TenantContext(tenant_id=tenant_id, actor_id=actor_id, roles=["admin"]),
        }
    finally:
        await engine.dispose()


def _participant(world: dict[str, Any]) -> handlers.ReceiptErasure:
    return handlers.ReceiptErasure(world["factory"], _salts(), clock=FakeClock(_NOW))


async def _rows(world: dict[str, Any], sql: str, params: dict[str, Any]) -> list[Any]:
    async with world["factory"]() as session:
        return list((await session.execute(text(sql), params)).mappings().all())


# --- the no-cascade block ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_receipt_with_feedback_cannot_be_deleted(receipts_world: dict[str, Any]) -> None:
    """The premise, asserted rather than assumed.

    If this ever stops failing, minimizing instead of deleting is no longer the
    resolution to anything and the participant beside it should be re-argued.
    """
    from sqlalchemy.exc import IntegrityError

    async with receipts_world["factory"]() as session, session.begin():
        receipt_id = await _plant_receipt(
            session, tenant_id=receipts_world["tenant_id"], actor_id=receipts_world["actor_id"]
        )
        await _plant_feedback(session, tenant_id=receipts_world["tenant_id"], receipt_id=receipt_id)

    with pytest.raises(IntegrityError):
        async with receipts_world["factory"]() as session, session.begin():
            await session.execute(
                text("DELETE FROM context_receipts WHERE receipt_id = :r"),
                {"r": receipt_id},
            )


@pytest.mark.asyncio
async def test_the_erasure_minimizes_the_reported_on_receipt_and_keeps_the_report(
    receipts_world: dict[str, Any],
) -> None:
    """The resolution. The receipt survives so the report keeps pointing at
    something, and it no longer says what was read."""
    async with receipts_world["factory"]() as session, session.begin():
        receipt_id = await _plant_receipt(
            session, tenant_id=receipts_world["tenant_id"], actor_id=receipts_world["actor_id"]
        )
        feedback_id = await _plant_feedback(session, tenant_id=receipts_world["tenant_id"], receipt_id=receipt_id)

    counts = await _participant(receipts_world).erase_actor(receipts_world["ctx"], receipts_world["actor_id"])

    assert counts["receipts"] == 1
    assert counts["receipts_with_feedback"] == 1

    items = await _rows(
        receipts_world,
        "SELECT item_key FROM context_receipt_items WHERE receipt_id = :r",
        {"r": receipt_id},
    )
    assert items and all(tombstones.is_erased_key(row["item_key"]) for row in items)

    surviving = await _rows(
        receipts_world,
        "SELECT f.feedback_id, r.state FROM context_feedback f "
        "  JOIN context_receipts r ON r.receipt_id = f.receipt_id "
        " WHERE f.feedback_id = :f",
        {"f": feedback_id},
    )
    assert [row["feedback_id"] for row in surviving] == [feedback_id]
    # The receipt's own structure is untouched: it still records that a resolution
    # happened and what state it reached. That is the audit linkage the deletion
    # would have destroyed even if the foreign key had allowed it.
    assert surviving[0]["state"] == "complete"


# --- what a minimization leaves behind -----------------------------------------


@pytest.mark.asyncio
async def test_the_reference_binding_goes_and_the_reference_survives(receipts_world: dict[str, Any]) -> None:
    """A binding says "this receipt cited that reference" — the link is what is
    erased. The reference itself is shared material another subject may still cite."""
    async with receipts_world["factory"]() as session, session.begin():
        receipt_id = await _plant_receipt(
            session, tenant_id=receipts_world["tenant_id"], actor_id=receipts_world["actor_id"]
        )
        reference_id = await _plant_binding(session, tenant_id=receipts_world["tenant_id"], receipt_id=receipt_id)

    await _participant(receipts_world).erase_actor(receipts_world["ctx"], receipts_world["actor_id"])

    bindings = await _rows(
        receipts_world,
        "SELECT binding_id FROM context_reference_bindings WHERE subject_id = :s",
        {"s": receipt_id},
    )
    references = await _rows(
        receipts_world,
        "SELECT reference_id FROM context_external_references WHERE reference_id = :ref",
        {"ref": reference_id},
    )
    assert bindings == []
    assert [row["reference_id"] for row in references] == [reference_id]


@pytest.mark.asyncio
async def test_the_erased_keys_are_recognisable_and_carry_nothing_back(
    receipts_world: dict[str, Any],
) -> None:
    """Recognisable so a reader can tell a minimized receipt from an empty one, and
    keyed so the marker is not a lookup table for candidate item keys."""
    async with receipts_world["factory"]() as session, session.begin():
        receipt_id = await _plant_receipt(
            session,
            tenant_id=receipts_world["tenant_id"],
            actor_id=receipts_world["actor_id"],
            item_keys=("catalog:svc-checkout",),
        )

    await _participant(receipts_world).erase_actor(receipts_world["ctx"], receipts_world["actor_id"])

    (item,) = await _rows(
        receipts_world,
        "SELECT item_key FROM context_receipt_items WHERE receipt_id = :r",
        {"r": receipt_id},
    )
    assert item["item_key"].startswith(tombstones.ERASED_KEY_PREFIX)
    assert "svc-checkout" not in item["item_key"]


@pytest.mark.asyncio
async def test_an_exclusion_stops_naming_what_it_withheld(receipts_world: dict[str, Any]) -> None:
    """A withheld key names what somebody was reading about as plainly as a returned
    one — more so, since they went looking for it — so it is minimized the same way.

    The row itself stays. "There was something you may not see" is what an exclusion
    exists to record, and it does not stop being true because the person who asked was
    erased; a reader who finds a thin answer still has to be able to tell that from an
    answer that had nothing behind it.
    """
    async with receipts_world["factory"]() as session, session.begin():
        receipt_id = await _plant_receipt(
            session,
            tenant_id=receipts_world["tenant_id"],
            actor_id=receipts_world["actor_id"],
            item_keys=("catalog:svc-checkout",),
        )

    await _participant(receipts_world).erase_actor(receipts_world["ctx"], receipts_world["actor_id"])

    (exclusion,) = await _rows(
        receipts_world,
        "SELECT item_key, block, reason FROM context_receipt_exclusions WHERE receipt_id = :r",
        {"r": receipt_id},
    )
    assert exclusion["item_key"].startswith(tombstones.ERASED_KEY_PREFIX)
    assert "withheld" not in exclusion["item_key"]
    # The two fields that make the row worth keeping are untouched.
    assert exclusion["block"] == "canonical"
    assert exclusion["reason"] == "below the trust floor"


@pytest.mark.asyncio
async def test_another_actors_receipt_is_untouched(receipts_world: dict[str, Any]) -> None:
    """Same tenant, different requester. A minimization scoped only by tenant would
    reduce everybody's receipts and report it as one person's erasure."""
    colleague = uuid.uuid4()
    async with receipts_world["factory"]() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO actors (actor_id, tenant_id, display_name, oidc_subject, created_at) "
                "VALUES (:a, :t, 'colleague', :sub, :now)"
            ),
            {"a": colleague, "t": receipts_world["tenant_id"], "sub": f"sub-{colleague.hex[:10]}", "now": _NOW},
        )
        theirs = await _plant_receipt(
            session,
            tenant_id=receipts_world["tenant_id"],
            actor_id=colleague,
            item_keys=("catalog:their-service",),
        )
        await _plant_receipt(session, tenant_id=receipts_world["tenant_id"], actor_id=receipts_world["actor_id"])

    await _participant(receipts_world).erase_actor(receipts_world["ctx"], receipts_world["actor_id"])

    (item,) = await _rows(
        receipts_world,
        "SELECT item_key FROM context_receipt_items WHERE receipt_id = :r",
        {"r": theirs},
    )
    assert item["item_key"] == "catalog:their-service"
    # Their withheld key too: the exclusions read is scoped by the same join, so a
    # version that reached everybody's items would reach everybody's exclusions.
    (exclusion,) = await _rows(
        receipts_world,
        "SELECT item_key FROM context_receipt_exclusions WHERE receipt_id = :r",
        {"r": theirs},
    )
    assert exclusion["item_key"] == "catalog:withheld"


@pytest.mark.asyncio
async def test_erasing_twice_writes_nothing_the_second_time(receipts_world: dict[str, Any]) -> None:
    """Idempotence against the real column, not against a fake that remembers.

    The second pass has to recognise its own marker; keying it again would produce
    a different value on every run, and a receipt would never reach a stable state.
    """
    async with receipts_world["factory"]() as session, session.begin():
        receipt_id = await _plant_receipt(
            session,
            tenant_id=receipts_world["tenant_id"],
            actor_id=receipts_world["actor_id"],
            item_keys=("catalog:svc-checkout",),
        )

    keys_sql = (
        "SELECT item_key FROM context_receipt_items WHERE receipt_id = :r "
        "UNION ALL "
        "SELECT item_key FROM context_receipt_exclusions WHERE receipt_id = :r"
    )

    participant = _participant(receipts_world)
    first = await participant.erase_actor(receipts_world["ctx"], receipts_world["actor_id"])
    after_first = await _rows(receipts_world, keys_sql, {"r": receipt_id})
    second = await participant.erase_actor(receipts_world["ctx"], receipts_world["actor_id"])
    after_second = await _rows(receipts_world, keys_sql, {"r": receipt_id})

    # The planted item and the planted exclusion, both minimized on the first pass and
    # both recognised as already done on the second.
    assert first["artefacts"] == 2
    assert second["artefacts"] == 0
    assert [row["item_key"] for row in after_first] == [row["item_key"] for row in after_second]


# --- the registration that makes a receipt reachable ---------------------------


@pytest.mark.asyncio
async def test_a_registered_receipt_link_carries_every_record_the_receipt_quoted(
    receipts_world: dict[str, Any],
) -> None:
    """One registration per receipt; the link table is where the per-record detail
    lives, and it is what an erasure of those records reads to find this receipt."""
    checkpoint_id, claim_id = uuid.uuid4(), uuid.uuid4()
    async with receipts_world["factory"]() as session, session.begin():
        receipt_id = await _plant_receipt(
            session, tenant_id=receipts_world["tenant_id"], actor_id=receipts_world["actor_id"]
        )
        derivative_id = await handlers.register_receipt_links(
            session,
            tenant_id=receipts_world["tenant_id"],
            receipt_id=receipt_id,
            sources=[
                derivatives.SourceRef(
                    record_class=policies.RECORD_TASK_CHECKPOINT,
                    source_id=checkpoint_id,
                    expires_at=None,
                ),
                derivatives.SourceRef(
                    record_class=policies.RECORD_MEMORY_CLAIM,
                    source_id=claim_id,
                    expires_at=None,
                ),
            ],
            now=_NOW,
        )

    links = await _rows(
        receipts_world,
        "SELECT source_record_class, source_id FROM derivative_source_links WHERE derivative_id = :d",
        {"d": derivative_id},
    )
    registration = await _rows(
        receipts_world,
        "SELECT derivative_kind, storage_locator, blocking, expires_at FROM derivative_registrations "
        " WHERE derivative_id = :d",
        {"d": derivative_id},
    )

    assert {(row["source_record_class"], row["source_id"]) for row in links} == {
        (policies.RECORD_TASK_CHECKPOINT, checkpoint_id),
        (policies.RECORD_MEMORY_CLAIM, claim_id),
    }
    assert registration[0]["derivative_kind"] == derivatives.KIND_RECEIPT_LINK
    assert registration[0]["storage_locator"] == handlers.locator_for(receipt_id)
    # Blocking, because a receipt-link that has not been reduced still names what
    # somebody read; the fail-closed overdue read path keys off this flag.
    assert registration[0]["blocking"] is True
    # Both sources are bounded by tenant deletion rather than a duration, so the
    # receipt's own class clock is the horizon. Without a fallback the registration
    # would be refused outright rather than written with a guessed expiry.
    assert registration[0]["expires_at"] == policies.expiry_deadline(policies.RECORD_CONTEXT_RECEIPT, _NOW)


@pytest.mark.asyncio
async def test_the_handler_reduces_a_registration_the_registrar_wrote(
    receipts_world: dict[str, Any],
) -> None:
    """End to end across the seam that a locator mismatch would break silently:
    the registrar writes the locator, the handler parses it, and the right receipt
    is the one that gets reduced."""
    async with receipts_world["factory"]() as session, session.begin():
        receipt_id = await _plant_receipt(
            session,
            tenant_id=receipts_world["tenant_id"],
            actor_id=receipts_world["actor_id"],
            item_keys=("catalog:svc-checkout",),
        )
        derivative_id = await handlers.register_receipt_links(
            session,
            tenant_id=receipts_world["tenant_id"],
            receipt_id=receipt_id,
            sources=[
                derivatives.SourceRef(
                    record_class=policies.RECORD_MEMORY_CLAIM,
                    source_id=uuid.uuid4(),
                    expires_at=None,
                )
            ],
            now=_NOW,
        )

    (stored,) = await _rows(
        receipts_world,
        "SELECT derivative_id, tenant_id, derivative_kind, storage_locator, audience_partition, classification, "
        "       expires_at, blocking FROM derivative_registrations WHERE derivative_id = :d",
        {"d": derivative_id},
    )
    registration = derivatives.Registration(**dict(stored))

    async with receipts_world["factory"]() as session, session.begin():
        touched = await handlers.ReceiptLinkHandler(_salts()).apply(session, registration, derivatives.OPERATION_DELETE)

    # The planted item and the planted exclusion.
    assert touched == 2
    (item,) = await _rows(
        receipts_world,
        "SELECT item_key FROM context_receipt_items WHERE receipt_id = :r",
        {"r": receipt_id},
    )
    assert tombstones.is_erased_key(item["item_key"])
    (exclusion,) = await _rows(
        receipts_world,
        "SELECT item_key FROM context_receipt_exclusions WHERE receipt_id = :r",
        {"r": receipt_id},
    )
    assert tombstones.is_erased_key(exclusion["item_key"])
