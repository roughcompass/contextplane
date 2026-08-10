"""Propagation handlers for the three derivative kinds no product domain owns.

Most derivative kinds belong to a domain: a vector belongs to retrieval, a
summary to checkpoints, a claim derivative to memory. Three do not.

``outbox``
    A transactional outbox row is a *carrier*: it holds a copy of the content it
    was enqueued about so that the drain never has to re-read the source. That
    copy is the point (``embedding_outbox.text_to_embed`` is the person's own
    words, already extracted from the record) and it is also the problem — an
    erasure that deletes the record and leaves the queue behind has deleted
    nothing a reader would notice and everything an auditor would. Outboxes span
    retrieval, memory and closure maintenance, so no single domain can own the
    handler.

``log_projection``
    The two log classes this product keeps are erasure-exempt by approved
    retention policy, which makes registering one as an erasure derivative a
    contradiction rather than a case to handle. See ``LogProjectionHandler``.

``export``
    Exports are a registered derivative class in policy and have no store in this
    product. See ``ExportHandler``.

**What these handlers are, and are not.** They delete artefacts. They do not
register them: the family that enqueues into an outbox is the family that knows
which sources that row was built from, and a registrar here would have to guess.
They do not wire themselves into the handler registry either — which handlers a
deployment runs is a wiring decision, and a module that registered itself on
import would make it an import-order decision instead.

**Every refusal raises.** A handler that returned zero for something it could not
resolve would be marked done by the drain, and the queue would empty while the
artefact stayed. Raising leaves the work item to retry and then reach ``failed``,
which the overdue count includes and the fail-closed read paths key off. Zero
touched artefacts is still a valid success, but only for a locator this handler
resolved and found already empty — which is exactly what a retry of a
half-applied propagation looks like.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contextplane.exceptions import RegistryError
from contextplane.retention import derivatives


class UnresolvableLocator(RegistryError):
    """The locator names nothing this handler can reach.

    A refusal, not an error in the usual sense: the work item stays queued, and
    an operator reading the exhausted-retry log learns that a derivative was
    registered pointing at something no handler can delete. That is a
    registration defect, and it should read as one rather than as a deletion
    nobody can find.
    """


class ErasureExemptRecordClass(RegistryError):
    """The locator names a record class approved policy exempts from erasure.

    Distinct from an unresolvable locator because the fix is the opposite one.
    An unresolvable locator means something is missing; this means something was
    registered that should never have been, and deleting it to make the queue
    drain would violate the retention approval rather than satisfy it.
    """


class UnsupportedPropagationOperation(RegistryError):
    """The operation is not one of the three the propagation vocabulary defines."""


#: Handler version recorded on every registration these handlers write. Bumped
#: when what a handler *does* changes, not when this file is edited: the version
#: exists so a derivative built by an older handler can be identified, and a
#: version that moved for a docstring makes that identification meaningless.
_VERSION: Final = "carrier/1"

#: The locator shape these handlers read: ``<kind>://<table>/<row-uuid>``.
#: Opaque to the registry by design, so the shape is agreed here — a registrar
#: writing a locator for a carrier writes this, and `carrier_locator` below is
#: how it does so without re-deriving the format from a regex.
_LOCATOR = re.compile(r"^(?P<scheme>[a-z_]+)://(?P<table>[a-z_][a-z0-9_]*)/(?P<row_id>[0-9a-fA-F-]{36})$")


def carrier_locator(kind: str, table: str, row_id: uuid.UUID) -> str:
    """Build the storage locator a carrier registration should record.

    Exported so that the family registering the derivative and the handler
    deleting it agree on the format by construction rather than by both
    remembering it. A locator these handlers cannot parse is a derivative
    nothing propagates, and the failure surfaces at erasure time — long after
    the registration that caused it.
    """
    return f"{kind}://{table}/{row_id}"


def _parse_locator(kind: str, locator: str) -> tuple[str, uuid.UUID]:
    """Split a carrier locator into its table and row, or refuse it.

    The table is returned as text and immediately looked up in a closed map by
    the caller. It is never interpolated into a statement: the locator is data
    somebody else wrote, and a handler that built ``DELETE FROM {table}`` from it
    would let whoever registers a derivative choose which table gets emptied.
    """
    match = _LOCATOR.match(locator)
    if match is None or match["scheme"] != kind:
        msg = f"{locator!r} is not a {kind!r} locator of the form {kind}://<table>/<row-uuid>"
        raise UnresolvableLocator(msg)
    try:
        row_id = uuid.UUID(match["row_id"])
    except ValueError:
        msg = f"{locator!r} names no valid row id"
        raise UnresolvableLocator(msg) from None
    return match["table"], row_id


def _require_known_operation(operation: str) -> None:
    """Refuse an operation outside the propagation vocabulary.

    The schema constrains the column, so the drain cannot deliver one of these.
    A handler is directly constructible, though, and a caller that invented an
    operation should hear about it rather than have it silently deleted as if it
    had asked for a deletion.
    """
    if operation not in derivatives.OPERATIONS:
        msg = f"{operation!r} is not a propagation operation"
        raise UnsupportedPropagationOperation(msg)


async def _delete_registered_row(
    session: AsyncSession,
    registration: derivatives.Registration,
    *,
    kind: str,
    statements: Mapping[str, str],
    unknown_table_hint: str,
) -> int:
    """Run the statement this map holds for the locator's table, or refuse.

    The tenant clause is not redundant with the primary key. It is the
    authorization half of the delete: a registration row carries the tenant it
    belongs to, and a locator that named another tenant's row would otherwise
    delete it on the strength of a uuid.
    """
    table, row_id = _parse_locator(kind, registration.storage_locator)
    statement = statements.get(table)
    if statement is None:
        msg = f"{kind!r} handler has no statement for table {table!r}: {unknown_table_hint}"
        raise UnresolvableLocator(msg)

    result = await session.execute(text(statement), {"row_id": row_id, "tenant_id": registration.tenant_id})
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# --- outbox ----------------------------------------------------------------

#: Every outbox row a propagation may delete, and the statement that deletes it.
#: A closed map of literal statements written here, keyed by exact table name:
#: the table a locator names selects one of these, and never contributes text to
#: one. Adding an outbox to this product means adding a line here, which is the
#: visible decision it should be.
#:
#: `arc_audit_outbox` is deliberately absent. It carries audit material, which
#: the approved retention policy exempts from erasure, so it belongs with the
#: log classes below rather than here.
_OUTBOX_DELETES: Final[Mapping[str, str]] = {
    "embedding_outbox": "DELETE FROM embedding_outbox WHERE outbox_id = :row_id AND tenant_id = :tenant_id",
    "embedding_outbox_failed": (
        "DELETE FROM embedding_outbox_failed WHERE failed_id = :row_id AND tenant_id = :tenant_id"
    ),
    "memory_extraction_outbox": (
        "DELETE FROM memory_extraction_outbox WHERE outbox_id = :row_id AND tenant_id = :tenant_id"
    ),
    "memory_extraction_outbox_failed": (
        "DELETE FROM memory_extraction_outbox_failed WHERE failed_id = :row_id AND tenant_id = :tenant_id"
    ),
    "closure_outbox": "DELETE FROM closure_outbox WHERE outbox_id = :row_id AND tenant_id = :tenant_id",
}

_OUTBOX_UNKNOWN_HINT: Final = (
    "only the five product outboxes are deletable through a propagation; "
    "audit-bearing and retention-internal queues are not among them"
)


class OutboxHandler:
    """Deletes the queued copy an erasure, expiry or revocation invalidated.

    **All three operations delete, and that is the design rather than a gap.**
    An outbox row is a snapshot of content plus the instruction to process it.
    There is nothing to rebuild in place — re-deriving the payload means reading
    a source that is mid-erasure, and the family that enqueues is the only thing
    that knows whether the row is still wanted, so it re-enqueues if it is. There
    is nothing to partially redact either: a queued payload with its text removed
    is a job that will produce a wrong artefact rather than no artefact. Losing
    queued work is recoverable; leaving an erased person's words in the queue is
    not, and that asymmetry decides every one of the three.
    """

    kind: Final = derivatives.KIND_OUTBOX
    version: Final = _VERSION

    async def apply(self, session: AsyncSession, registration: derivatives.Registration, operation: str) -> int:
        """Delete the outbox row this locator names, and report whether one was there."""
        _require_known_operation(operation)
        return await _delete_registered_row(
            session,
            registration,
            kind=self.kind,
            statements=_OUTBOX_DELETES,
            unknown_table_hint=_OUTBOX_UNKNOWN_HINT,
        )


# --- log projections -------------------------------------------------------

#: The two log classes approved retention policy exempts from erasure, and the
#: reason each is exempt — carried here because a handler that refuses without
#: saying why reads as an unimplemented case.
#:
#: The audit log is retained on an accountability basis and stores no values:
#: its actor references are pseudonymous derived ids, not identifiers. The PII
#: detection log records that a scan fired and what it matched on, in the same
#: derived form. Neither becomes readable by keeping it, and both run their own
#: clocks past tenant deletion — at offboarding the tenant's pseudonymization
#: salt is destroyed, which is what turns the derived ids from pseudonymous into
#: effectively anonymous rather than deletion of the rows.
_ERASURE_EXEMPT_LOGS: Final[Mapping[str, str]] = {
    "audit_log": "retained for accountability; carries no values and its actor references are pseudonymous",
    "pii_detection_log": "exempt on the same basis as the audit log, and retained for the same reason",
}

#: Log projections a propagation may delete. Empty, and empty is the honest
#: state: this product builds no derived copy of either log today. A stub that
#: returned success for an unrecognised projection would report a deletion that
#: never happened, which is the one outcome an erasure record must never contain.
_LOG_PROJECTION_DELETES: Final[Mapping[str, str]] = {}

_LOG_PROJECTION_UNKNOWN_HINT: Final = "this deployment builds no erasable log projection"


class LogProjectionHandler:
    """Refuses the exempt log classes, and deletes registered projections.

    **Refusing an exempt class is the deliverable.** Registering ``audit_log`` or
    ``pii_detection_log`` as an erasure derivative asserts two things that cannot
    both hold: that the rows must go on erasure, and that policy retains them on
    an accountability basis past tenant deletion. Deleting them to drain the
    queue would resolve the contradiction in the direction that destroys the
    accountability record; returning success would resolve it by lying. Raising
    leaves the item to fail loudly and names the registration as the defect.

    A projection built *from* a log is a different thing and is deletable — a
    denormalized copy carries the values the log itself does not. None exists
    today, so every locator refuses; the map above is where one would land.
    """

    kind: Final = derivatives.KIND_LOG_PROJECTION
    version: Final = _VERSION

    async def apply(self, session: AsyncSession, registration: derivatives.Registration, operation: str) -> int:
        """Refuse an exempt log class, or delete the registered projection row."""
        _require_known_operation(operation)
        table, _ = _parse_locator(self.kind, registration.storage_locator)
        exemption = _ERASURE_EXEMPT_LOGS.get(table)
        if exemption is not None:
            msg = (
                f"{table!r} is exempt from erasure and must not be deleted by a propagation "
                f"({exemption}); registering it as a derivative is the defect to fix"
            )
            raise ErasureExemptRecordClass(msg)

        return await _delete_registered_row(
            session,
            registration,
            kind=self.kind,
            statements=_LOG_PROJECTION_DELETES,
            unknown_table_hint=_LOG_PROJECTION_UNKNOWN_HINT,
        )


# --- exports ---------------------------------------------------------------


class ExportHandler:
    """Refuses every locator, because this product stores no export.

    Approved policy makes exports a registered derivative that erasure either
    propagates into or fails closed on. There is no export store here to
    propagate into, so this handler takes the second branch, deliberately and
    for every locator: the item retries, exhausts, and becomes ``failed``, where
    the overdue count includes it and the read paths that must not serve content
    behind an unapplied erasure stay closed.

    The alternative — returning zero touched artefacts — is the shape of a
    success, and the drain would mark the work done. An erasure record would then
    state that exports were handled, on a code path that never looked at one.
    **A refusal that lands in the failure queue is a truthful "not done"; a zero
    is an untruthful "done".**

    When an export store lands, this handler resolves the locator against it and
    the refusal narrows to locators that genuinely name nothing.
    """

    kind: Final = derivatives.KIND_EXPORT
    version: Final = _VERSION

    async def apply(self, session: AsyncSession, registration: derivatives.Registration, operation: str) -> int:
        """Refuse, naming the absent export store rather than reporting a deletion."""
        _require_known_operation(operation)
        msg = (
            f"no export store exists to propagate into, so export locator "
            f"{registration.storage_locator!r} cannot be resolved; this refusal is the fail-closed "
            f"branch of the export retention policy, not a missing implementation"
        )
        raise UnresolvableLocator(msg)


#: The three handlers this module provides, in the order the kinds are declared.
#: A plain tuple rather than a registration call: what a deployment registers is
#: a wiring decision, and this is the list wiring reads. It is also where the
#: type checker proves each class actually satisfies the handler protocol, which
#: is otherwise only discovered when the drain calls one.
CARRIER_HANDLERS: Final[tuple[derivatives.DerivativeHandler, ...]] = (
    OutboxHandler(),
    LogProjectionHandler(),
    ExportHandler(),
)


__all__ = [
    "CARRIER_HANDLERS",
    "ErasureExemptRecordClass",
    "ExportHandler",
    "LogProjectionHandler",
    "OutboxHandler",
    "UnresolvableLocator",
    "UnsupportedPropagationOperation",
    "carrier_locator",
]
