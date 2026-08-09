"""The floor an observation has to clear before anything decides what to do with it.

Split out of `ingest.py` rather than living beside the ledger writes, because
admitting content and recording an observation are different jobs with different
reasons to change: this module answers "may this be stored at all", and nothing
here knows about replay, ceilings, authority or identity.

**It runs before the replay lookup, and that ordering is the whole design.** A
detector added after a row was stored would otherwise let an exact redelivery of
prohibited content return the row already there — an admitted path to content the
floor now prohibits, reached by resending it. Scanning first costs one pass on the
retry path and leaves no such path open.

**Two scans, because the two are separately authored.** A producer can get the
observation right and still paste a credential into a reference URI beside it. The
observation scan covers whichever form it arrived in — the canonical serialization
of the payload mapping, or the evidence-handle URI — under one field type, so a
deployment cannot end up blocking one and admitting the other. A URI is a real
token channel: a credential in a query string is a credential in storage.

**The reference scan reads the full normalized reference, not the replay digest's
own material.** That distinction is not cosmetic. The digest input omits
`authorized_uri`, which is precisely the field a credential lands in, so scanning
it would have left the likeliest token channel in a reference unread while looking
thorough.

**A refusal keeps nothing of what it refused.** The per-class refusal rows come
from the scanner; what this module adds is the caller's audit line carrying the
content *digest*, which is the only handle an operator has for asking whether a
row bearing it is already in the ledger from before the floor existed. The
refusal that reaches the caller names the classes that fired and never what
matched — a message that quoted the value would put it in every client log, which
is the opposite of what refusing it was for.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol

from contextplane.context.admission import (
    FIELD_EXTERNAL_SIGNAL_PAYLOAD,
    FIELD_EXTERNAL_SIGNAL_REFERENCES,
)
from contextplane.exceptions import ValidationError
from contextplane.security.pii_guard import AdmissionRefused, admit_or_refuse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    # Under TYPE_CHECKING only: the envelope is declared in `ingest.py`, which
    # imports this module at runtime. A type-only import keeps the cycle off the
    # import graph while still typing the parameter honestly.
    from contextplane.signals.ingest import ExternalSignalEnvelopeV1
    from contextplane.types import TenantContext

_log = logging.getLogger(__name__)

#: The reason class a content refusal is audited under. Defined here beside the
#: code that raises it; `ingest.py` re-exports it into its closed rejection
#: vocabulary so an auditor still sees one set.
REASON_PROHIBITED_CONTENT = "prohibited_content"


class AuditRejection(Protocol):
    """How this module reports a refusal to whoever is writing the audit trail.

    A callback rather than a service reference: the floor has no business knowing
    what an ingest service is, and inverting it this way keeps the dependency
    pointing away from the caller. It also makes the refusal path testable without
    standing up an ingest service to observe it.
    """

    async def __call__(
        self,
        ctx: TenantContext,
        envelope: ExternalSignalEnvelopeV1,
        /,
        *,
        reason_class: str,
        content_digest: str | None = None,
    ) -> None:
        """Record that this observation was turned away, and under which reason class.

        The two leading parameters are positional-only so an implementation is
        free to name them whatever reads best in its own file; only the shape has
        to match, not the caller's choice of words.
        """
        ...


def canonical_json(value: object) -> str:
    """One JSON spelling per value: sorted keys, no incidental whitespace.

    Shared with `ingest.py` rather than written twice. Two spellings of "the
    canonical form" is how a digest and the text that was scanned to produce it
    quietly stop describing the same bytes.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def observation_text(envelope: ExternalSignalEnvelopeV1) -> str:
    """The observation as one scannable string, whichever form it arrived in.

    Exactly one of payload and evidence handle is set — the envelope enforces
    that — so this never concatenates them and never scans an absent payload as
    though it were content.
    """
    if envelope.payload is not None:
        return canonical_json(dict(envelope.payload))
    return envelope.evidence_handle or ""


def reference_text(envelope: ExternalSignalEnvelopeV1) -> str:
    """Every field of every normalized reference, serialized for scanning.

    Deliberately not the replay digest's material, which omits `authorized_uri`
    — see the module docstring for why that omission would have mattered.
    """
    return canonical_json(
        [
            {
                "source_system": reference.source_system,
                "source_namespace": reference.source_namespace,
                "kind": reference.kind,
                "external_id": reference.external_id,
                "classification": reference.classification,
                "external_authority": reference.external_authority,
                "revision": reference.revision,
                "authorized_uri": reference.authorized_uri,
            }
            for reference in envelope.references
        ]
    )


async def admit_observation(
    session_factory: async_sessionmaker[AsyncSession],
    ctx: TenantContext,
    envelope: ExternalSignalEnvelopeV1,
    *,
    digest: str,
    audit_rejection: AuditRejection,
) -> None:
    """Refuse an observation carrying a prohibited class, before anything is decided.

    Raises `ValidationError` — the service layer's own refusal rather than the
    scanner's. Both transports already translate it to the status a caller can act
    on, and letting a `security.pii_guard` type through would make every adapter
    learn a second refusal vocabulary for the same thing.
    """
    subject = f"{envelope.source_id}:{envelope.source_event_id}"
    scans: tuple[tuple[str, str], ...] = (
        (FIELD_EXTERNAL_SIGNAL_PAYLOAD, observation_text(envelope)),
        (FIELD_EXTERNAL_SIGNAL_REFERENCES, reference_text(envelope)),
    )
    for field_type, text_to_scan in scans:
        if not text_to_scan:
            continue
        try:
            await admit_or_refuse(session_factory, ctx, text_to_scan, field_type, subject=subject)
        except AdmissionRefused as refused:
            classes = sorted(set(refused.decision.classes))
            _log.info(
                "signal_ingest_refused",
                extra={
                    "tenant_id": str(ctx.tenant_id),
                    "source_id": str(envelope.source_id),
                    "reason": REASON_PROHIBITED_CONTENT,
                    "field_type": field_type,
                    # Which detectors fired, never what they matched and never
                    # where: an offset plus a length describes the secret's
                    # position in text an attacker may be able to reconstruct.
                    "pii_classes": classes,
                },
            )
            await audit_rejection(
                ctx,
                envelope,
                reason_class=REASON_PROHIBITED_CONTENT,
                content_digest=digest,
            )
            raise ValidationError(
                "content carries a prohibited class and was not stored: " + ", ".join(classes)
            ) from refused


__all__ = [
    "REASON_PROHIBITED_CONTENT",
    "AuditRejection",
    "admit_observation",
    "canonical_json",
    "observation_text",
    "reference_text",
]
