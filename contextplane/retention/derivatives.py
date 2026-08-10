"""The derivative registry, its handler coverage rule, and the work it enqueues.

A derivative is anything built from a record that is not the record: a vector, a
chunk, a full-text document, a summary, a cached answer, an outbox payload, a log
projection, an export, a receipt link, a claim derivative. Every one of them can
outlive the thing it was derived from, and every one of them can hold the erased
person's own words.

**Registration is not bookkeeping — it is the coverage list.** An unregistered
derivative is one that no erasure reaches and no expiry sweeps, and nothing about
the codebase makes it visible: the artefact exists, it answers queries, and the
propagation machinery has never heard of it. So the registry pairs with a handler
per kind and `unhandled_kinds` is release-gating. A kind with no handler is a
build failure, not a runtime surprise.

**Expiry only ever moves earlier.** Re-registering an existing derivative takes
the minimum of the stored and the incoming expiry rather than the incoming one.
A rebuild that read a longer-lived source must not extend the artefact's life past
the shortest-lived source it still contains, and "the last registration wins" is
exactly the rule that would let it.

**One cause enqueues one item.** The outbox's uniqueness is per derivative, per
operation, per trigger, per tombstone, so a sweep that runs twice enqueues once
and a retried erasure schedules no duplicate work. Idempotence lives in the
schema rather than in the caller because the caller is the part that gets retried.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.exceptions import RegistryError
from contextplane.retention import policies

_log = logging.getLogger(__name__)

# --- the closed vocabularies the schema admits ----------------------------

KIND_VECTOR = "vector"
KIND_EMBEDDING_CHUNK = "embedding_chunk"
KIND_FTS_DOCUMENT = "fts_document"
KIND_SUMMARY = "summary"
KIND_CACHE = "cache"
KIND_OUTBOX = "outbox"
KIND_LOG_PROJECTION = "log_projection"
KIND_EXPORT = "export"
KIND_RECEIPT_LINK = "receipt_link"
KIND_CLAIM_DERIVATIVE = "claim_derivative"

#: Every kind the schema will store. Closed, and closed on purpose: a derivative
#: kind nobody declared is a derivative kind no handler covers.
DERIVATIVE_KINDS: tuple[str, ...] = (
    KIND_VECTOR,
    KIND_EMBEDDING_CHUNK,
    KIND_FTS_DOCUMENT,
    KIND_SUMMARY,
    KIND_CACHE,
    KIND_OUTBOX,
    KIND_LOG_PROJECTION,
    KIND_EXPORT,
    KIND_RECEIPT_LINK,
    KIND_CLAIM_DERIVATIVE,
)

OPERATION_REBUILD = "rebuild"
OPERATION_DELETE = "delete"
OPERATION_REDACT = "redact"
OPERATIONS: tuple[str, ...] = (OPERATION_REBUILD, OPERATION_DELETE, OPERATION_REDACT)

TRIGGER_EXPIRY = "expiry"
TRIGGER_ERASURE = "erasure"
TRIGGER_REVOCATION = "revocation"
TRIGGER_POLICY_CHANGE = "policy_change"
TRIGGERS: tuple[str, ...] = (TRIGGER_EXPIRY, TRIGGER_ERASURE, TRIGGER_REVOCATION, TRIGGER_POLICY_CHANGE)


class UnhandledDerivativeKind(RegistryError):
    """Raised when work is scheduled for a kind no handler covers.

    The runtime half of the release gate. If a kind slips past the gate — a
    deployment running an older build, a handler removed without its kind — the
    work item fails loudly instead of being marked done by a dispatcher that
    found nothing to call.
    """


@dataclasses.dataclass(frozen=True)
class Registration:
    """One registered derivative, as the propagation worker needs to see it."""

    derivative_id: uuid.UUID
    tenant_id: uuid.UUID
    derivative_kind: str
    storage_locator: str
    audience_partition: str
    classification: str
    expires_at: datetime.datetime
    blocking: bool


@dataclasses.dataclass(frozen=True)
class SourceRef:
    """One record a derivative was built from, with the expiry it carried.

    `expires_at` is copied at registration rather than joined at sweep time
    because the five source classes store their expiry five different ways, and a
    minimum computed across five joins is a minimum that stops being computed the
    first time one of those tables changes shape.
    """

    record_class: str
    source_id: uuid.UUID
    revision: str | None = None
    expires_at: datetime.datetime | None = None


class DerivativeHandler(Protocol):
    """What one derivative kind can do about an erasure, an expiry or a revocation.

    Three operations, all idempotent, all returning how many artefacts they
    touched. A handler that cannot redact its kind implements `redact` by
    deleting: losing a derivative is recoverable through a rebuild, and leaving
    erased content inside one is not.
    """

    @property
    def kind(self) -> str:
        """The derivative kind this handler owns."""
        ...

    @property
    def version(self) -> str:
        """This handler's version, recorded on every registration it writes.

        Stored so a derivative built by a handler that has since changed can be
        identified rather than assumed rebuildable.
        """
        ...

    async def apply(self, session: AsyncSession, registration: Registration, operation: str) -> int:
        """Perform `operation` on this derivative and report how many artefacts changed.

        Zero is a valid, successful answer: the artefact was already gone, which
        is what a retry of a partially-applied propagation looks like.
        """
        ...


class HandlerRegistry:
    """Which handler owns which derivative kind, and which kinds have none.

    The list a release gate reads. It is a registry rather than a dict literal so
    that registering twice for one kind fails: two handlers for a kind means the
    second silently decides what erasure does, and which one wins would depend on
    import order.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, DerivativeHandler] = {}

    def register(self, handler: DerivativeHandler) -> None:
        """Add a handler, refusing an unknown kind or a second handler for one."""
        if handler.kind not in DERIVATIVE_KINDS:
            msg = f"derivative kind {handler.kind!r} is not one the schema stores"
            raise UnhandledDerivativeKind(msg)
        if handler.kind in self._handlers:
            msg = f"derivative kind {handler.kind!r} already has a handler"
            raise UnhandledDerivativeKind(msg)
        self._handlers[handler.kind] = handler

    def handler_for(self, kind: str) -> DerivativeHandler:
        """The handler for one kind, or a refusal naming the gap."""
        try:
            return self._handlers[kind]
        except KeyError:
            msg = f"no propagation handler is registered for derivative kind {kind!r}"
            raise UnhandledDerivativeKind(msg) from None

    @property
    def kinds(self) -> tuple[str, ...]:
        """Which kinds are covered, in the schema's own order."""
        return tuple(kind for kind in DERIVATIVE_KINDS if kind in self._handlers)

    def unhandled_kinds(self) -> tuple[str, ...]:
        """Kinds the schema stores and nothing propagates — the release-gating set."""
        return tuple(kind for kind in DERIVATIVE_KINDS if kind not in self._handlers)


# --- writes ----------------------------------------------------------------

_UPSERT_REGISTRATION = """
INSERT INTO derivative_registrations (
    tenant_id, derivative_kind, storage_locator, audience_partition, classification,
    rebuild_handler_version, delete_handler_version, redact_handler_version,
    policy_version, expires_at, blocking, sync_status
)
VALUES (
    :tid, :kind, :locator, :audience, :classification,
    :handler_version, :handler_version, :handler_version,
    :policy_version, :expires_at, :blocking, 'pending'
)
ON CONFLICT (tenant_id, derivative_kind, storage_locator, audience_partition)
DO UPDATE SET
    classification = EXCLUDED.classification,
    rebuild_handler_version = EXCLUDED.rebuild_handler_version,
    delete_handler_version = EXCLUDED.delete_handler_version,
    redact_handler_version = EXCLUDED.redact_handler_version,
    policy_version = EXCLUDED.policy_version,
    blocking = EXCLUDED.blocking,
    -- Never later than what is already stored: a rebuild that read a
    -- longer-lived source must not extend the artefact past the shortest-lived
    -- source it still contains.
    expires_at = LEAST(derivative_registrations.expires_at, EXCLUDED.expires_at)
RETURNING derivative_id
"""

_UPSERT_SOURCE_LINK = """
INSERT INTO derivative_source_links (derivative_id, source_record_class, source_id, source_revision, source_expires_at)
VALUES (:did, :cls, :sid, :revision, :source_expires_at)
ON CONFLICT (derivative_id, source_record_class, source_id)
DO UPDATE SET source_revision = EXCLUDED.source_revision, source_expires_at = EXCLUDED.source_expires_at
"""

#: Every derivative that read any of these records, and the work one cause owes
#: it. `SELECT ... ON CONFLICT DO NOTHING` rather than a read-then-write: the
#: check and the insert are one statement, so two concurrent sweeps enqueue one
#: item rather than racing between the check and the write.
#:
#: **The casts in the select list are load-bearing.** A parameter that appears as
#: a column of `INSERT ... SELECT` gets no type from the insert target: the
#: select is planned on its own, so an uncast placeholder resolves to `text` and
#: the `uuid` and `timestamptz` columns then refuse the row. The same parameters
#: written into a `VALUES` list would have been inferred from the target columns,
#: which is why this shape needs to say the types itself.
_ENQUEUE_FOR_SOURCES = """
INSERT INTO derivative_work_outbox (tenant_id, derivative_id, operation, trigger, tombstone_id, available_at)
SELECT DISTINCT r.tenant_id, r.derivative_id,
       CAST(:operation AS text), CAST(:trigger AS text),
       CAST(:tombstone_id AS uuid), CAST(:now AS timestamptz)
  FROM derivative_registrations r
  JOIN derivative_source_links l ON l.derivative_id = r.derivative_id
 WHERE r.tenant_id = :tid
   AND l.source_record_class = :cls
   AND l.source_id = ANY(:sids)
ON CONFLICT DO NOTHING
RETURNING work_id
"""


async def register_derivative(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    storage_locator: str,
    audience_partition: str,
    classification: str,
    handler_version: str,
    sources: Sequence[SourceRef],
    blocking: bool = False,
    fallback_expiry: datetime.datetime | None = None,
) -> uuid.UUID:
    """Register one derivative and every source it read, expiring with the earliest.

    The sources are the argument that matters. A derivative registered with the
    one source that triggered its build inherits that source's expiry and outlives
    the others silently — which is the exact failure the link table exists to make
    impossible, and passing a single source here reintroduces it one caller at a
    time.
    """
    if kind not in DERIVATIVE_KINDS:
        msg = f"derivative kind {kind!r} is not one the schema stores"
        raise UnhandledDerivativeKind(msg)

    expires_at = policies.minimum_expiry(
        (source.expires_at for source in sources),
        fallback=fallback_expiry,
    )

    derivative_id = (
        await session.execute(
            text(_UPSERT_REGISTRATION),
            {
                "tid": tenant_id,
                "kind": kind,
                "locator": storage_locator,
                "audience": audience_partition,
                "classification": classification,
                "handler_version": handler_version,
                "policy_version": policies.POLICY_VERSION,
                "expires_at": expires_at,
                "blocking": blocking,
            },
        )
    ).scalar_one()

    for source in sources:
        await session.execute(
            text(_UPSERT_SOURCE_LINK),
            {
                "did": derivative_id,
                "cls": source.record_class,
                "sid": source.source_id,
                "revision": source.revision,
                "source_expires_at": source.expires_at,
            },
        )

    return uuid.UUID(str(derivative_id))


async def enqueue_for_sources(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    record_class: str,
    source_ids: Sequence[uuid.UUID],
    operation: str,
    trigger: str,
    now: datetime.datetime,
    tombstone_id: uuid.UUID | None = None,
) -> int:
    """Schedule `operation` on every derivative built from these records, once per cause.

    Returns how many items this call actually created — a repeat of the same
    cause returns zero because the schema refused the duplicate, which is what
    makes a re-run of an erasure or a sweep free rather than amplifying.
    """
    if operation not in OPERATIONS:
        msg = f"{operation!r} is not a propagation operation"
        raise UnhandledDerivativeKind(msg)
    if trigger not in TRIGGERS:
        msg = f"{trigger!r} is not a propagation trigger"
        raise UnhandledDerivativeKind(msg)
    if trigger in (TRIGGER_ERASURE, TRIGGER_REVOCATION) and tombstone_id is None:
        # The schema refuses this too; refusing here names the cause instead of
        # surfacing a constraint violation from three frames down.
        msg = f"{trigger!r} work must name the tombstone that ordered it"
        raise UnhandledDerivativeKind(msg)
    if not source_ids:
        return 0

    result = await session.execute(
        text(_ENQUEUE_FOR_SOURCES),
        {
            "tid": tenant_id,
            "cls": record_class,
            "sids": list(source_ids),
            "operation": operation,
            "trigger": trigger,
            "tombstone_id": tombstone_id,
            "now": now,
        },
    )
    enqueued = len(result.fetchall())
    if enqueued:
        _log.info(
            "retention.derivative_work_enqueued: tenant=%s class=%s operation=%s trigger=%s items=%d",
            tenant_id,
            record_class,
            operation,
            trigger,
            enqueued,
        )
    return enqueued


__all__ = [
    "DERIVATIVE_KINDS",
    "KIND_CACHE",
    "KIND_CLAIM_DERIVATIVE",
    "KIND_EMBEDDING_CHUNK",
    "KIND_EXPORT",
    "KIND_FTS_DOCUMENT",
    "KIND_LOG_PROJECTION",
    "KIND_OUTBOX",
    "KIND_RECEIPT_LINK",
    "KIND_SUMMARY",
    "KIND_VECTOR",
    "OPERATIONS",
    "OPERATION_DELETE",
    "OPERATION_REBUILD",
    "OPERATION_REDACT",
    "TRIGGERS",
    "TRIGGER_ERASURE",
    "TRIGGER_EXPIRY",
    "TRIGGER_POLICY_CHANGE",
    "TRIGGER_REVOCATION",
    "DerivativeHandler",
    "HandlerRegistry",
    "Registration",
    "SourceRef",
    "UnhandledDerivativeKind",
    "enqueue_for_sources",
    "register_derivative",
]
