"""Receipts: the evidence record, and the hash chain that makes it tamper-evident.

A receipt says what context an agent was given, under what rules, at what
instant, and what it was told it must do. Its value is entirely evidential,
so two properties matter more than anything else here.

**The receipt ID is preallocated.** It is generated before the bundle is
assembled, not by the database at insert time. The bundle refers to its own
receipt, so the ID has to exist first -- and a `REPEATABLE READ` transaction
that hits a serialization failure and retries must produce the *same* ID, or
the retry would look like a second resolution and the caller would hold a
reference to a receipt that never committed.

**Events form a hash chain with an O(1) append.** Each event's digest covers
its predecessor's, so altering any event invalidates every later digest.
Appending locks a single head row rather than re-reading the chain, which
keeps append cost independent of chain length -- a receipt with ten thousand
detail events appends as fast as a fresh one.

Nothing here opens a session. Receipt creation is one part of a larger
resolution transaction, and a receipt that committed independently of the
challenge consumption or the audit row beside it would be exactly the
partial state the transaction exists to prevent.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry.arc import metrics
from registry.arc.schemas.canonical import receipt_event_digest
from registry.arc.service.bundle import ContextBundle
from registry.arc.service.signing import RECEIPT_EVENT_SIGNATURE_PROFILE, ReceiptSigningProvider
from registry.arc.types import ResolutionStatus
from registry.audit import actions
from registry.types import Clock

# The creation event every receipt has, at sequence 0. The head then starts at
# next_sequence 1.
RECEIPT_CREATED_EVENT = "receipt_created"
CREATION_SEQUENCE = 0

EVENT_SOURCE_SYSTEM = "system"
EVENT_SOURCE_HOST = "host"
EVENT_SOURCE_GATEWAY = "gateway"

INTEGRITY_VALID = "valid"
INTEGRITY_FAILED = "integrity_failed"

# Marking runs on its own connection, so a caller that wrongly still holds a
# lock on the receipt row would otherwise hang it indefinitely. Short: this
# transaction touches one row and contends with nothing in normal operation,
# so any wait at all means the caller made a mistake worth surfacing.
MARK_LOCK_TIMEOUT_MS = 2000


def preallocate_receipt_id() -> uuid.UUID:
    """Mint a receipt ID before the bundle that will reference it exists.

    A named function rather than an inline `uuid4()` at each call site: the
    reason this ID cannot come from the database default is a real
    constraint, and a call site that quietly generated its own would satisfy
    the type checker while breaking retry identity.
    """
    return uuid.uuid4()


class ReceiptIntegrityError(Exception):
    """An event append would have broken the chain.

    Raised for a gap, a fork, a duplicate sequence, or a predecessor digest
    that does not match the head. The append is abandoned; marking the
    receipt `integrity_failed` is a separate, fail-closed transaction so the
    mark survives the rollback of whatever was being attempted.
    """


@dataclasses.dataclass(frozen=True)
class SelectedRevision:
    """One revision that contributed to a bundle, as the receipt records it."""

    revision_id: uuid.UUID
    artifact_id: uuid.UUID
    is_mandatory: bool
    was_omitted: bool = False
    omission_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class SelectedDirective:
    """One exact directive the bundle carried, with its audience-gated source.

    The locator and digest fields are recorded whether or not the requesting
    actor was permitted to see them: the receipt is the audit record, and an
    auditor asking later what the agent was actually given must not be
    limited to what the agent itself could read. Redaction happens on the
    read path, not by omitting evidence at write time.
    """

    revision_id: uuid.UUID
    directive_id: uuid.UUID
    artifact_id: uuid.UUID
    is_mandatory: bool
    visibility_decision_id: str
    source_locator: str
    source_revision_locator: str
    content_digest: str
    obligation_fields: dict[str, object]
    context_handle_digest: str
    was_omitted: bool = False
    omission_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class ReplayEnvelope:
    """The sealed original response, retained so an exact retry can replay it.

    Ciphertext rather than plaintext because the response contains resolved
    directive content: a receipt table readable by an operator must not also
    be a copy of every governed statement any agent was ever shown.

    Produced by the caller. This module never encrypts -- it would need a key
    provider whose purpose is response replay, and threading one through here
    would give receipt creation a second reason to fail.
    """

    ciphertext: bytes
    nonce: bytes
    key_id: str


@dataclasses.dataclass(frozen=True)
class ReceiptProvenance:
    """What the deployment was, at the instant of resolution.

    Recorded on every receipt so a replay years later can tell whether a
    difference in outcome is tampering or simply a newer engine. Without
    these, "the same manifest resolves differently now" is unanswerable.
    """

    selection_engine_version: str
    registry_build_revision: str
    canonical_profile_versions: dict[str, str]
    selection_config_digest: str


class ReceiptService:
    """Creates receipts and appends to their event chains.

    Every method takes the caller's session and none of them commit.
    """

    def __init__(self, signing: ReceiptSigningProvider, clock: Clock) -> None:
        self._signing = signing
        self._clock = clock

    async def create_receipt(
        self,
        session: AsyncSession,
        *,
        receipt_id: uuid.UUID,
        challenge_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        host_id: str,
        session_id: str,
        manifest_fingerprint: str,
        attestation_id: str,
        bundle: ContextBundle,
        provenance: ReceiptProvenance,
        replay: ReplayEnvelope,
        evaluated_at: datetime.datetime,
        freshness_basis: str,
        selected_revisions: tuple[SelectedRevision, ...] = (),
        selected_directives: tuple[SelectedDirective, ...] = (),
        freshness_deadline: datetime.datetime | None = None,
    ) -> str:
        """Write the receipt, its selected rows, its creation event, and its head.

        Returns the creation event's digest, which is what the head now
        points at.

        `receipt_id` is a parameter rather than generated here: the bundle
        being recorded already refers to it, and a retry of this transaction
        must reuse it.
        """
        await session.execute(
            text(
                "INSERT INTO arc_receipts ("
                "  receipt_id, challenge_id, tenant_id, actor_id, host_id, session_id,"
                "  manifest_fingerprint, attestation_id, resolution_status,"
                "  selection_engine_version, registry_build_revision, canonical_profile_versions,"
                "  selection_config_digest, evaluated_at, freshness_basis, freshness_deadline,"
                "  blocked_reasons, degraded_reasons, mandatory_directive_count,"
                "  rendered_content_bytes, budget_limit_bytes, integrity_state,"
                "  response_replay_ciphertext, response_replay_nonce, response_replay_key_id"
                ") VALUES ("
                "  :receipt_id, :challenge_id, :tenant_id, :actor_id, :host_id, :session_id,"
                "  :manifest_fingerprint, :attestation_id, :resolution_status,"
                "  :engine_version, :build_revision, :profile_versions,"
                "  :config_digest, :evaluated_at, :freshness_basis, :freshness_deadline,"
                "  :blocked_reasons, :degraded_reasons, :mandatory_count,"
                "  :rendered_bytes, :budget_limit, :integrity_state,"
                "  :ciphertext, :nonce, :replay_key_id"
                ")"
            ),
            {
                "receipt_id": receipt_id,
                "challenge_id": challenge_id,
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "host_id": host_id,
                "session_id": session_id,
                "manifest_fingerprint": manifest_fingerprint,
                "attestation_id": attestation_id,
                "resolution_status": str(bundle.status),
                "engine_version": provenance.selection_engine_version,
                "build_revision": provenance.registry_build_revision,
                "profile_versions": json.dumps(provenance.canonical_profile_versions, sort_keys=True),
                "config_digest": provenance.selection_config_digest,
                "evaluated_at": evaluated_at,
                "freshness_basis": freshness_basis,
                "freshness_deadline": freshness_deadline,
                "blocked_reasons": list(bundle.blocked_reasons) or None,
                "degraded_reasons": list(bundle.degraded_reasons) or None,
                "mandatory_count": len(selected_directives) or len(bundle.directives),
                "rendered_bytes": bundle.rendered_content_bytes,
                "budget_limit": bundle.budget_limit_bytes,
                "integrity_state": INTEGRITY_VALID,
                "ciphertext": replay.ciphertext,
                "nonce": replay.nonce,
                "replay_key_id": replay.key_id,
            },
        )

        for revision in selected_revisions:
            await session.execute(
                text(
                    "INSERT INTO arc_receipt_selected_revisions ("
                    "  receipt_id, revision_id, tenant_id, artifact_id,"
                    "  is_mandatory, was_omitted, omission_reason"
                    ") VALUES (:receipt_id, :revision_id, :tenant_id, :artifact_id,"
                    "          :is_mandatory, :was_omitted, :omission_reason)"
                ),
                {
                    "receipt_id": receipt_id,
                    "revision_id": revision.revision_id,
                    "tenant_id": tenant_id,
                    "artifact_id": revision.artifact_id,
                    "is_mandatory": revision.is_mandatory,
                    "was_omitted": revision.was_omitted,
                    "omission_reason": revision.omission_reason,
                },
            )

        for directive in selected_directives:
            await session.execute(
                text(
                    "INSERT INTO arc_receipt_selected_directives ("
                    "  receipt_id, revision_id, directive_id, tenant_id, artifact_id,"
                    "  is_mandatory, was_omitted, omission_reason, visibility_decision_id,"
                    "  source_locator, source_revision_locator, content_digest,"
                    "  obligation_fields, context_handle_digest"
                    ") VALUES (:receipt_id, :revision_id, :directive_id, :tenant_id, :artifact_id,"
                    "          :is_mandatory, :was_omitted, :omission_reason, :visibility_decision_id,"
                    "          :source_locator, :source_revision_locator, :content_digest,"
                    "          :obligation_fields, :context_handle_digest)"
                ),
                {
                    "receipt_id": receipt_id,
                    "revision_id": directive.revision_id,
                    "directive_id": directive.directive_id,
                    "tenant_id": tenant_id,
                    "artifact_id": directive.artifact_id,
                    "is_mandatory": directive.is_mandatory,
                    "was_omitted": directive.was_omitted,
                    "omission_reason": directive.omission_reason,
                    "visibility_decision_id": directive.visibility_decision_id,
                    "source_locator": directive.source_locator,
                    "source_revision_locator": directive.source_revision_locator,
                    "content_digest": directive.content_digest,
                    "obligation_fields": json.dumps(directive.obligation_fields, sort_keys=True),
                    "context_handle_digest": directive.context_handle_digest,
                },
            )

        return await self._write_creation_event(
            session,
            receipt_id=receipt_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            manifest_fingerprint=manifest_fingerprint,
            status=bundle.status,
            directive_count=len(bundle.directives),
        )

    async def _write_creation_event(
        self,
        session: AsyncSession,
        *,
        receipt_id: uuid.UUID,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID,
        manifest_fingerprint: str,
        status: ResolutionStatus,
        directive_count: int,
    ) -> str:
        """Sequence 0, and the head that starts at 1.

        No predecessor digest: this is the chain's base case, and the
        schema's chain-link constraint requires exactly that shape at
        sequence 0.
        """
        event_id = uuid.uuid4()
        created_at = self._clock.now()
        payload = {"resolution_status": str(status), "directive_count": directive_count}
        digest = self._event_digest(
            event_id=event_id,
            receipt_id=receipt_id,
            tenant_id=tenant_id,
            sequence=CREATION_SEQUENCE,
            event_type=RECEIPT_CREATED_EVENT,
            event_source=EVENT_SOURCE_SYSTEM,
            request_payload_digest=manifest_fingerprint,
            previous_event_digest=None,
            payload=payload,
            created_at=created_at,
        )

        await self._insert_event(
            session,
            event_id=event_id,
            receipt_id=receipt_id,
            tenant_id=tenant_id,
            sequence=CREATION_SEQUENCE,
            event_type=RECEIPT_CREATED_EVENT,
            event_source=EVENT_SOURCE_SYSTEM,
            actor_id=actor_id,
            idempotency_key_digest=None,
            request_payload_digest=manifest_fingerprint,
            previous_event_digest=None,
            payload=payload,
            digest=digest,
            created_at=created_at,
        )

        await session.execute(
            text(
                "INSERT INTO arc_receipt_event_heads (receipt_id, next_sequence, last_event_digest) "
                "VALUES (:receipt_id, :next_sequence, :digest)"
            ),
            {"receipt_id": receipt_id, "next_sequence": CREATION_SEQUENCE + 1, "digest": digest},
        )
        return digest

    async def append_event(
        self,
        session: AsyncSession,
        *,
        receipt_id: uuid.UUID,
        tenant_id: uuid.UUID,
        event_type: str,
        event_source: str,
        request_payload_digest: str,
        payload: dict[str, object],
        actor_id: uuid.UUID | None = None,
        idempotency_key_digest: str | None = None,
        consumed_continuation_token_digest: str | None = None,
    ) -> str:
        """Append one event, advancing the chain by exactly one.

        Locks the head row `FOR UPDATE` and reads the predecessor digest and
        next sequence from it. That single-row lock is what makes append
        O(1): the alternative -- reading the chain to find its end -- would
        make each append cost more than the last, so a long-lived receipt
        would get progressively slower to use.

        Holding the lock for the rest of the caller's transaction is also
        what serializes concurrent appends. Two appends cannot both read
        `next_sequence = n` and both write sequence `n`, because the second
        blocks until the first commits and then sees `n + 1`.

        Returns the new head digest.
        """
        head = (
            await session.execute(
                text(
                    "SELECT next_sequence, last_event_digest FROM arc_receipt_event_heads "
                    "WHERE receipt_id = :rid FOR UPDATE"
                ),
                {"rid": receipt_id},
            )
        ).one_or_none()
        if head is None:
            msg = f"receipt {receipt_id} has no event head; it was never created"
            raise ReceiptIntegrityError(msg)

        sequence = head.next_sequence
        previous_digest = head.last_event_digest

        created_at = self._clock.now()
        event_id = uuid.uuid4()
        digest = self._event_digest(
            event_id=event_id,
            receipt_id=receipt_id,
            tenant_id=tenant_id,
            sequence=sequence,
            event_type=event_type,
            event_source=event_source,
            request_payload_digest=request_payload_digest,
            previous_event_digest=previous_digest,
            payload=payload,
            created_at=created_at,
        )

        await self._insert_event(
            session,
            event_id=event_id,
            receipt_id=receipt_id,
            tenant_id=tenant_id,
            sequence=sequence,
            event_type=event_type,
            event_source=event_source,
            actor_id=actor_id,
            idempotency_key_digest=idempotency_key_digest,
            request_payload_digest=request_payload_digest,
            previous_event_digest=previous_digest,
            payload=payload,
            digest=digest,
            created_at=created_at,
            consumed_continuation_token_digest=consumed_continuation_token_digest,
        )

        # Guarded on the digest we read under the lock. Belt and braces
        # against the lock: if this ever affects zero rows, something moved
        # the head between the locked read and here, and continuing would
        # fork the chain.
        advanced = await session.execute(
            text(
                "UPDATE arc_receipt_event_heads "
                "SET next_sequence = :next, last_event_digest = :digest, updated_at = :now "
                "WHERE receipt_id = :rid AND last_event_digest = :expected_previous"
            ),
            {
                "rid": receipt_id,
                "next": sequence + 1,
                "digest": digest,
                "now": created_at,
                "expected_previous": previous_digest,
            },
        )
        affected: int = advanced.rowcount  # type: ignore[attr-defined]
        if affected != 1:
            msg = f"event head for receipt {receipt_id} moved during append at sequence {sequence}"
            raise ReceiptIntegrityError(msg)

        return digest

    async def verify_chain(self, session: AsyncSession, receipt_id: uuid.UUID) -> None:
        """Re-derive every event digest and confirm the chain reaches the head.

        Deliberately O(n) and deliberately not on the append path. Appending
        verifies against the head alone; this is the auditor's operation,
        run when a chain is being challenged rather than on every write.

        Raises `ReceiptIntegrityError` on the first gap, fork, broken link,
        recomputed-digest mismatch, or bad signature.
        """
        rows = (
            await session.execute(
                text(
                    "SELECT event_id, sequence, event_type, event_source, request_payload_digest, "
                    "       previous_event_digest, event_payload, signer_key_id, event_digest, "
                    "       signature, created_at "
                    "FROM arc_receipt_events WHERE receipt_id = :rid ORDER BY sequence"
                ),
                {"rid": receipt_id},
            )
        ).all()
        if not rows:
            msg = f"receipt {receipt_id} has no events"
            raise ReceiptIntegrityError(msg)

        tenant_id = (
            await session.execute(
                text("SELECT tenant_id FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one()

        previous: str | None = None
        for expected_sequence, row in enumerate(rows):
            if row.sequence != expected_sequence:
                msg = (
                    f"receipt {receipt_id} chain has a gap: expected sequence "
                    f"{expected_sequence}, found {row.sequence}"
                )
                raise ReceiptIntegrityError(msg)
            if row.previous_event_digest != previous:
                msg = f"receipt {receipt_id} event at sequence {row.sequence} does not link to its predecessor"
                raise ReceiptIntegrityError(msg)

            recomputed = receipt_event_digest(
                {
                    "event_id": str(row.event_id),
                    "receipt_id": str(receipt_id),
                    "tenant_id": str(tenant_id),
                    "sequence": row.sequence,
                    "event_type": row.event_type,
                    "event_source": row.event_source,
                    "request_payload_digest": row.request_payload_digest,
                    "previous_event_digest": row.previous_event_digest,
                    "event_payload": row.event_payload,
                    "signer_key_id": row.signer_key_id,
                    "created_at": row.created_at,
                }
            )
            if recomputed != row.event_digest:
                msg = f"receipt {receipt_id} event at sequence {row.sequence} has a tampered payload"
                raise ReceiptIntegrityError(msg)

            if not self._signing.verify(
                bytes.fromhex(row.event_digest), bytes.fromhex(row.signature), key_id=row.signer_key_id
            ):
                msg = f"receipt {receipt_id} event at sequence {row.sequence} has an invalid signature"
                raise ReceiptIntegrityError(msg)

            previous = row.event_digest

        head = (
            await session.execute(
                text(
                    "SELECT next_sequence, last_event_digest FROM arc_receipt_event_heads "
                    "WHERE receipt_id = :rid"
                ),
                {"rid": receipt_id},
            )
        ).one_or_none()
        if head is None:
            msg = f"receipt {receipt_id} has events but no head"
            raise ReceiptIntegrityError(msg)
        if head.last_event_digest != previous or head.next_sequence != len(rows):
            # A truncated chain: events were removed and the head left
            # pointing past them. Verifying events alone would not catch it.
            msg = f"receipt {receipt_id} head does not match the end of its chain"
            raise ReceiptIntegrityError(msg)

    async def mark_integrity_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        receipt_id: uuid.UUID,
        *,
        reason: str,
    ) -> None:
        """Flag a receipt as compromised, in its own committed transaction.

        This is the one place in this module that opens a session, and it
        has to. The append that detected the problem is being rolled back;
        writing the mark on that same session would roll it back too, and
        the receipt would stay `valid` with nothing recording that its chain
        does not verify.

        Fail-closed in the strict sense: if this transaction itself fails,
        the exception propagates. A silently-swallowed failure here would
        leave a compromised receipt looking sound, which is worse than an
        error the operator sees.

        **Call this after rolling back**, not while the detecting
        transaction is still open. Running on a separate connection means a
        caller that still holds a row lock on this receipt would block this
        transaction forever while itself awaiting it -- a genuine deadlock
        that no timeout on the caller's side can break. `lock_timeout` below
        turns that mistake into a prompt error rather than a hung request.
        """
        async with session_factory() as session, session.begin():
            await session.execute(text(f"SET LOCAL lock_timeout = '{MARK_LOCK_TIMEOUT_MS}ms'"))
            await session.execute(
                text("UPDATE arc_receipts SET integrity_state = :state WHERE receipt_id = :rid"),
                {"rid": receipt_id, "state": INTEGRITY_FAILED},
            )
            await session.execute(
                text(
                    "INSERT INTO arc_audit_outbox (tenant_id, event_type, event_payload) "
                    "SELECT tenant_id, :event_type, CAST(:payload AS JSONB) "
                    "FROM arc_receipts WHERE receipt_id = :rid"
                ),
                {
                    "rid": receipt_id,
                    "event_type": actions.ARC_RECEIPT_INTEGRITY_FAILED,
                    "payload": json.dumps({"receipt_id": str(receipt_id), "reason": reason}, sort_keys=True),
                },
            )
        # Counted after the marking transaction commits, so the metric can
        # never report a failure that was rolled back. This one is meant to
        # be alertable: a nonzero rate is a tamper signal, not noise.
        metrics.observe_receipt_integrity_failure()

    async def is_usable(self, session: AsyncSession, receipt_id: uuid.UUID) -> bool:
        """Whether this receipt may still authorize anything.

        A receipt whose chain does not verify cannot: its whole value is
        being trustworthy evidence, and evidence that may have been altered
        authorizes nothing. Callers gate on this rather than on the receipt
        merely existing.
        """
        state = (
            await session.execute(
                text("SELECT integrity_state FROM arc_receipts WHERE receipt_id = :rid"),
                {"rid": receipt_id},
            )
        ).scalar_one_or_none()
        return state == INTEGRITY_VALID

    def _event_digest(
        self,
        *,
        event_id: uuid.UUID,
        receipt_id: uuid.UUID,
        tenant_id: uuid.UUID,
        sequence: int,
        event_type: str,
        event_source: str,
        request_payload_digest: str,
        previous_event_digest: str | None,
        payload: dict[str, object],
        created_at: datetime.datetime,
    ) -> str:
        return receipt_event_digest(
            {
                "event_id": str(event_id),
                "receipt_id": str(receipt_id),
                "tenant_id": str(tenant_id),
                "sequence": sequence,
                "event_type": event_type,
                "event_source": event_source,
                "request_payload_digest": request_payload_digest,
                "previous_event_digest": previous_event_digest,
                "event_payload": payload,
                "signer_key_id": self._signing.active_key_id,
                "created_at": created_at,
            }
        )

    async def _insert_event(
        self,
        session: AsyncSession,
        *,
        event_id: uuid.UUID,
        receipt_id: uuid.UUID,
        tenant_id: uuid.UUID,
        sequence: int,
        event_type: str,
        event_source: str,
        actor_id: uuid.UUID | None,
        idempotency_key_digest: str | None,
        request_payload_digest: str,
        previous_event_digest: str | None,
        payload: dict[str, object],
        digest: str,
        created_at: datetime.datetime,
        consumed_continuation_token_digest: str | None = None,
    ) -> None:
        key_id = self._signing.active_key_id
        # Signed over the raw 32 digest bytes, not their hex text: the hex is
        # a presentation choice, and signing it would make the signature
        # depend on case and encoding rather than on the digest itself.
        signature = self._signing.sign(bytes.fromhex(digest), key_id=key_id)
        await session.execute(
            text(
                "INSERT INTO arc_receipt_events ("
                "  event_id, receipt_id, tenant_id, sequence, event_type, event_source,"
                "  actor_id, signer_key_id, signature_profile, idempotency_key_digest,"
                "  request_payload_digest, previous_event_digest, event_payload,"
                "  consumed_continuation_token_digest, event_digest, signature, created_at"
                ") VALUES ("
                "  :event_id, :receipt_id, :tenant_id, :sequence, :event_type, :event_source,"
                "  :actor_id, :signer_key_id, :signature_profile, :idempotency_key_digest,"
                "  :request_payload_digest, :previous_event_digest, :event_payload,"
                "  :consumed_token_digest, :event_digest, :signature, :created_at"
                ")"
            ),
            {
                "event_id": event_id,
                "receipt_id": receipt_id,
                "tenant_id": tenant_id,
                "sequence": sequence,
                "event_type": event_type,
                "event_source": event_source,
                "actor_id": actor_id,
                "signer_key_id": key_id,
                # The *signature* profile, which names the domain-separation
                # tag a verifier must prepend -- not the digest profile. A
                # verifier handed only the digest profile could recompute the
                # digest but not the bytes that were actually signed.
                "signature_profile": RECEIPT_EVENT_SIGNATURE_PROFILE,
                "idempotency_key_digest": idempotency_key_digest,
                "request_payload_digest": request_payload_digest,
                "previous_event_digest": previous_event_digest,
                "event_payload": json.dumps(payload, sort_keys=True),
                # Recorded under a partial unique index, which is what makes
                # a continuation token single-use: a replay is refused by the
                # database rather than by a check someone could omit.
                "consumed_token_digest": consumed_continuation_token_digest,
                "event_digest": digest,
                "signature": signature.hex(),
                "created_at": created_at,
            },
        )


__all__ = [
    "CREATION_SEQUENCE",
    "EVENT_SOURCE_GATEWAY",
    "EVENT_SOURCE_HOST",
    "EVENT_SOURCE_SYSTEM",
    "INTEGRITY_FAILED",
    "INTEGRITY_VALID",
    "RECEIPT_CREATED_EVENT",
    "ReceiptIntegrityError",
    "ReceiptProvenance",
    "ReceiptService",
    "ReplayEnvelope",
    "SelectedDirective",
    "SelectedRevision",
    "preallocate_receipt_id",
]
