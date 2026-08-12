"""What an erasure does to a receipt: minimize it, never delete it.

A receipt is evidence. It records that a resolution happened, what it returned, what
it withheld and why — and things downstream depend on that record existing, most
plainly the feedback rows that cite it. Deleting a receipt to erase somebody would
destroy the audit trail *and* fail outright, because the feedback foreign keys
deliberately do not cascade: a person's report has independent standing, so removing
what it cites is refused rather than silently taking the report with it.

So this module resolves that block by minimization instead of deletion. The receipt
structure stays, its audit linkage stays, and the parts that identify what was cited
are replaced by keyed markers that carry nothing recoverable.

**The item key is the identifying part.** An item key names the thing a resolution
returned — a capability path, a workspace entry, a document fragment — so it is the
field that says what somebody was reading about. It is replaced by a tenant-keyed HMAC
prefix rather than blanked: deterministic in the original, so a retried minimization
writes the same value and stays idempotent; keyed, so the determinism is not also a
lookup table an attacker can probe with candidate keys.

**An exclusion's key is the same field and gets the same treatment.** An exclusion row
names something an arm found and deliberately did not return, so its `item_key` says
what somebody was reading about just as plainly as a returned item's does — arguably
more so, because a withheld key is one somebody went looking for. Only the key is
replaced: the row itself stays, with its block and its reason, because "there was
something you may not see" is the fact the row exists to record and erasing the person
who asked does not make it untrue.

**Reference bindings go entirely.** A binding says "this receipt cited that external
reference". The reference itself is shared material that survives — another subject may
still cite it — but the fact that *this* receipt did is the link being erased, and
nothing cascades it, so it is deleted here explicitly.

**Zero touched is success, not a failure to find work.** A retried propagation item, or
one whose receipt was already minimized, has nothing left to do. Reporting that as an
error would turn the normal recovery path into a compliance incident and, worse, would
make the queue's `failed` count meaningless.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contextplane.context.references import SUBJECT_RECEIPT
from contextplane.retention import derivatives, policies, tombstones
from contextplane.types import Clock, TenantContext

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

    from contextplane.context.schemas.envelope import ContextEnvelopeV1

_log = logging.getLogger(__name__)

#: This handler's version, recorded on every registration it writes. Bumped when the
#: minimization changes shape, because a registration records which version reduced it
#: and a reader comparing two receipts needs to know they were reduced the same way.
HANDLER_VERSION = "receipt-link.v1"

#: How a receipt-link derivative names what it covers. One receipt per registration —
#: see `context/receipts.py` for why the granularity is the receipt rather than the
#: item — so the locator is the receipt id and the handler needs nothing else.
LOCATOR_PREFIX = "receipt:"

#: The subsystem name the receipt participant registers and reports under.
SUBSYSTEM = "receipts"


def _rows_affected(result: object) -> int:
    """How many rows a DML statement touched, as an int rather than an Optional.

    A cast in one place instead of a suppression at each call site: `execute` is typed
    as returning a generic `Result`, which has no `rowcount`, and every caller here has
    just run an UPDATE or DELETE.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)


def locator_for(receipt_id: uuid.UUID) -> str:
    """The storage locator a receipt-link registration carries.

    One function so the registrar and the handler cannot disagree about the spelling.
    A locator the handler could not parse would leave the derivative unreachable while
    the registration looked complete.
    """
    return f"{LOCATOR_PREFIX}{receipt_id}"


def receipt_from_locator(locator: str) -> uuid.UUID:
    """The receipt a locator names, or a refusal.

    Refused rather than defaulted: a locator this handler cannot read is a registration
    written by something that disagreed with `locator_for`, and guessing would minimize
    the wrong receipt or none at all while reporting success.
    """
    if not locator.startswith(LOCATOR_PREFIX):
        msg = f"receipt-link locator {locator!r} does not name a receipt"
        raise derivatives.UnhandledDerivativeKind(msg)
    try:
        return uuid.UUID(locator.removeprefix(LOCATOR_PREFIX))
    except ValueError as exc:
        msg = f"receipt-link locator {locator!r} does not carry a receipt id"
        raise derivatives.UnhandledDerivativeKind(msg) from exc


# Only the keys that still say something. `is_erased_key`'s prefix is the marker a
# previous pass wrote, and re-keying a marker would produce a different marker on every
# run — which is what would make a retry non-idempotent.
_ITEMS_TO_MINIMIZE_SQL = f"""
SELECT i.item_row_id, i.item_key
FROM context_receipt_items AS i
JOIN context_receipts AS r ON r.receipt_id = i.receipt_id
WHERE i.receipt_id = :receipt
  AND r.tenant_id = :tenant
  AND i.item_key IS NOT NULL
  AND i.item_key NOT LIKE '{tombstones.ERASED_KEY_PREFIX}%'
"""  # noqa: S608 - the interpolated value is a module constant, not caller input

_MINIMIZE_ITEM_SQL = """
UPDATE context_receipt_items
SET item_key = :marker
WHERE item_row_id = :row
"""

# The same shape against the withheld half of the receipt. Separate statements rather
# than one union over both tables: they have different primary keys, so the write half
# has to know which table a row came from, and carrying a discriminator through a
# combined read costs more than the second pair.
_EXCLUSIONS_TO_MINIMIZE_SQL = f"""
SELECT e.exclusion_id, e.item_key
FROM context_receipt_exclusions AS e
JOIN context_receipts AS r ON r.receipt_id = e.receipt_id
WHERE e.receipt_id = :receipt
  AND r.tenant_id = :tenant
  AND e.item_key NOT LIKE '{tombstones.ERASED_KEY_PREFIX}%'
"""  # noqa: S608 - the interpolated value is a module constant, not caller input

_MINIMIZE_EXCLUSION_SQL = """
UPDATE context_receipt_exclusions
SET item_key = :marker
WHERE exclusion_id = :row
"""

_DELETE_RECEIPT_BINDINGS_SQL = """
DELETE FROM context_reference_bindings
WHERE tenant_id = :tenant
  AND subject_type = :subject_type
  AND subject_id = :receipt
"""

# Receipts this actor requested. `requested_by` is text — the actor's id as a string,
# not a foreign key — so the comparison is against the string form.
_ACTOR_RECEIPTS_SQL = """
SELECT receipt_id FROM context_receipts
WHERE tenant_id = :tenant AND requested_by = :actor
"""

_RECEIPT_HAS_FEEDBACK_SQL = """
SELECT count(*) FROM context_feedback
WHERE tenant_id = :tenant AND receipt_id = :receipt
"""


class ReceiptLinkHandler:
    """Minimizes one receipt's item keys and removes its reference bindings.

    Every operation reduces rather than removes, including `delete`. That is deliberate
    and is the whole point of this handler: the row a `delete` would remove is evidence
    other rows depend on, and the feedback foreign keys refuse the removal anyway. A
    handler that implemented `delete` literally would fail on exactly the receipts that
    most need erasing — the ones somebody gave feedback on.
    """

    kind = derivatives.KIND_RECEIPT_LINK
    version = HANDLER_VERSION

    def __init__(self, salts: tombstones.TenantSaltResolver) -> None:
        self._salts = salts

    async def _minimize_keys(
        self,
        session: AsyncSession,
        *,
        select_sql: str,
        update_sql: str,
        id_attribute: str,
        receipt_id: uuid.UUID,
        tenant_id: uuid.UUID,
        salt: bytes,
    ) -> int:
        """Replace every still-speaking `item_key` one statement pair names. Returns rows written.

        Shared by the returned items and the withheld ones because the treatment is
        identical and the difference is only which table holds the key — a second copy
        of this loop would be a second place for the marker to be computed differently,
        which is the one thing that would break idempotence.

        `id_attribute` rather than a fixed name: the two tables key their rows
        differently, and aliasing them to a common label in the SELECT would hide which
        table a row came from at exactly the point the UPDATE has to know.
        """
        rows = (
            await session.execute(
                text(select_sql),
                {"receipt": receipt_id, "tenant": tenant_id},
            )
        ).all()

        for row in rows:
            await session.execute(
                text(update_sql),
                {
                    "row": getattr(row, id_attribute),
                    "marker": tombstones.erased_item_key(salt, str(row.item_key)),
                },
            )
        return len(rows)

    async def apply(
        self,
        session: AsyncSession,
        registration: derivatives.Registration,
        operation: str,
    ) -> int:
        """Reduce the receipt this registration names. Returns artefacts touched.

        The count is item keys minimized — the returned ones and the withheld ones
        alike — plus bindings deleted, because all three are artefacts holding what was
        cited. Zero is a valid success: a retried item, or a receipt already reduced,
        has nothing left to do.
        """
        if operation not in derivatives.OPERATIONS:
            msg = f"{operation!r} is not a propagation operation"
            raise derivatives.UnhandledDerivativeKind(msg)

        receipt_id = receipt_from_locator(registration.storage_locator)
        salt = self._salts.salt_for(registration.tenant_id)

        touched = await self._minimize_keys(
            session,
            select_sql=_ITEMS_TO_MINIMIZE_SQL,
            update_sql=_MINIMIZE_ITEM_SQL,
            id_attribute="item_row_id",
            receipt_id=receipt_id,
            tenant_id=registration.tenant_id,
            salt=salt,
        )
        touched += await self._minimize_keys(
            session,
            select_sql=_EXCLUSIONS_TO_MINIMIZE_SQL,
            update_sql=_MINIMIZE_EXCLUSION_SQL,
            id_attribute="exclusion_id",
            receipt_id=receipt_id,
            tenant_id=registration.tenant_id,
            salt=salt,
        )

        touched += _rows_affected(
            await session.execute(
                text(_DELETE_RECEIPT_BINDINGS_SQL),
                {
                    "tenant": registration.tenant_id,
                    "subject_type": SUBJECT_RECEIPT,
                    "receipt": receipt_id,
                },
            )
        )

        _log.info(
            "receipt_link.reduced: receipt=%s operation=%s touched=%d",
            receipt_id,
            operation,
            touched,
        )
        return touched


class ReceiptErasure:
    """Minimizes the receipts an actor requested, including the ones with feedback.

    Registered as an erasure participant, and deliberately not registered anywhere by
    this module: wiring it in is a separate change, so nothing here runs until something
    constructs it.

    **This is where the no-cascade block is resolved.** A receipt with feedback attached
    cannot be deleted — the foreign key refuses it, on purpose, because a person's
    report has standing independent of what it cites. Minimizing instead means the
    erasure succeeds on every receipt rather than failing on the ones that were
    reported on, and the feedback keeps pointing at a receipt that still exists and no
    longer says what was read.

    The report distinguishes the two cases anyway. Both are minimized identically, but
    an operator asking "what did this erasure do" should be able to see that some
    receipts were retained because something cites them, rather than inferring it.
    """

    subsystem = SUBSYSTEM

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        salts: tombstones.TenantSaltResolver,
        *,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._salts = salts
        self._clock = clock
        self._handler = ReceiptLinkHandler(salts)

    async def erase_actor(self, ctx: TenantContext, target_actor_id: uuid.UUID) -> dict[str, int]:
        """Minimize every receipt this actor requested. One transaction.

        Idempotent: the minimization skips keys already carrying the marker, the binding
        delete narrows to rows still present, and a second run therefore reports zero
        without rewriting anything.
        """
        now = self._clock.now()
        counts = {"receipts": 0, "receipts_with_feedback": 0, "artefacts": 0}

        async with self._session_factory() as session:
            receipt_ids = [
                uuid.UUID(str(row[0]))
                for row in (
                    await session.execute(
                        text(_ACTOR_RECEIPTS_SQL),
                        {"tenant": ctx.tenant_id, "actor": str(target_actor_id)},
                    )
                ).all()
            ]

            for receipt_id in receipt_ids:
                cited_by_feedback = int(
                    (
                        await session.execute(
                            text(_RECEIPT_HAS_FEEDBACK_SQL),
                            {"tenant": ctx.tenant_id, "receipt": receipt_id},
                        )
                    ).scalar()
                    or 0
                )
                counts["artefacts"] += await self._handler.apply(
                    session,
                    derivatives.Registration(
                        derivative_id=receipt_id,
                        tenant_id=ctx.tenant_id,
                        derivative_kind=derivatives.KIND_RECEIPT_LINK,
                        storage_locator=locator_for(receipt_id),
                        audience_partition="tenant",
                        classification="internal",
                        expires_at=now,
                        blocking=True,
                    ),
                    derivatives.OPERATION_REDACT,
                )
                counts["receipts"] += 1
                if cited_by_feedback:
                    counts["receipts_with_feedback"] += 1

            await session.commit()

        _log.info("receipts.minimized: actor=%s counts=%s", target_actor_id, counts)
        return counts


#: Which recall source names a record an erasure walks, and which class that is.
#:
#: The strings are the ones the arms stamp on every item they return, restated here
#: rather than imported: both live as module-private constants in packages this one
#: has no business reaching into, and a unit test pins these two against them so the
#: restatement cannot drift into a mapping that silently matches nothing.
#:
#: The two absent sources are absent on purpose. A catalog entity is the registry's
#: own material and a resolved ARC directive is governance material; neither is a
#: record a person authored, so neither carries anybody's content into the receipt
#: and neither is a class the erasure walks.
_SOURCE_RECORD_CLASSES: dict[str, str] = {
    "intent_checkpoint": policies.RECORD_TASK_CHECKPOINT,
    "living-memory": policies.RECORD_MEMORY_CLAIM,
}


def source_refs_for(envelope: ContextEnvelopeV1, *, now: datetime.datetime) -> list[derivatives.SourceRef]:
    """Every erasable record this resolution read, as links the registration stores.

    A receipt quotes what it returned, so it inherits the shortest life of everything
    it quoted — which only works if the registration names *all* of them. Passing the
    one source that happened to trigger the write is the exact failure the link table
    exists to prevent, so this walks every item in every block.

    An item whose key is not a record id is skipped rather than raising. The two
    mapped sources produce ids by construction, so a key that will not parse means an
    arm changed shape underneath this — a real defect, logged at error level, but not
    one that should destroy the receipt the resolution is trying to record. A lost
    registration costs a derivative nobody propagates; a lost receipt costs the
    evidence that the resolution happened at all.
    """
    refs: list[derivatives.SourceRef] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    for block in envelope.blocks:
        for item in block.items:
            record_class = _SOURCE_RECORD_CLASSES.get(item.receipt_item_id.source)
            if record_class is None:
                continue
            try:
                source_id = uuid.UUID(item.receipt_item_id.item_key)
            except ValueError:
                _log.error(
                    "receipt_link.unparsable_source_key: source=%s item_key=%s",
                    item.receipt_item_id.source,
                    item.receipt_item_id.item_key,
                )
                continue
            if (record_class, source_id) in seen:
                # One block may return the same record twice through two arms; the
                # link table is unique per (derivative, class, id) and would reject
                # the duplicate mid-transaction.
                continue
            seen.add((record_class, source_id))
            refs.append(
                derivatives.SourceRef(
                    record_class=record_class,
                    source_id=source_id,
                    expires_at=policies.expiry_deadline(record_class, now),
                )
            )
    return refs


async def register_receipt_links(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    receipt_id: uuid.UUID,
    sources: list[derivatives.SourceRef],
    now: datetime.datetime,
) -> uuid.UUID | None:
    """Register one receipt-link derivative, with every source the receipt read.

    Returns the registration id, or None when the receipt cited nothing bounded and
    nothing to propagate could be recorded. Called from the receipt writer inside its
    own transaction, so a receipt and its registration land together: a receipt with no
    registration is a derivative nothing can find when its sources are erased.

    `blocking` is true because a receipt-link that has not been reduced still names what
    somebody read, and the fail-closed overdue read path keys off that flag.
    """
    if not sources:
        return None
    return await derivatives.register_derivative(
        session,
        tenant_id=tenant_id,
        kind=derivatives.KIND_RECEIPT_LINK,
        storage_locator=locator_for(receipt_id),
        audience_partition="tenant",
        classification="internal",
        handler_version=HANDLER_VERSION,
        sources=sources,
        blocking=True,
        # A receipt is bounded by its own class clock when nothing it cited is: the
        # resolution happened at a known instant, so there is always a horizon.
        fallback_expiry=policies.expiry_deadline(policies.RECORD_CONTEXT_RECEIPT, now),
    )


__all__ = [
    "HANDLER_VERSION",
    "LOCATOR_PREFIX",
    "SUBSYSTEM",
    "ReceiptErasure",
    "ReceiptLinkHandler",
    "locator_for",
    "receipt_from_locator",
    "register_receipt_links",
    "source_refs_for",
]
